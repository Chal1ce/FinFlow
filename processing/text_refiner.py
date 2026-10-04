"""Optional, source-preserving CPT branch using ReScraper-style line operations.

This is an independently implemented protocol adapter, not the upstream training pipeline.
No generated program is evaluated as Python or shell code.
"""

from __future__ import annotations

import json
import re
from collections import Counter

from core.flywheel_files import register_file, safe_path, sha256, write_json
from core.ids import stable_uid


TAGS = {"<keep>", "<delete>", "<edit>", "<rewrite>"}
RM = re.compile(r"rm ([1-9][0-9]*)(?:-([1-9][0-9]*))?")
SUB = re.compile(r'sub ([1-9][0-9]*):\s*(".*")')
SYNTAX = re.compile(r"<\s*/?\s*(?:lid:|extract\b|keep\b|delete\b|edit\b|rewrite\b)|^\s*(?:rm \d|sub \d)", re.I | re.M)
FINANCIAL = re.compile(
    r"[+\-−]?\d+(?:[.,]\d+)*(?:[%％])?|[%％$€£¥￥]|人民币|美元|港元|万元|亿元|百分点|同比|环比|"
    r"千克|公斤|吨|股|倍|元|万|亿|不|未|无|否|非|仅|除外|至少|至多|可能|预计|n't|\b(?:not|no|never|without|unless|except|only|"
    r"may|might|approximately|million|billion|percent|USD|CNY|RMB|HKD|EUR|GBP)\b",
    re.I,
)
PROTECTED = re.compile(r"<\s*/?\s*(?:table|tr|td|th)\b|^\s*\|.*\||!\[|\$\$|\\(?:begin|\[|\()", re.I | re.M)


class InvalidProgram(ValueError):
    """Message is a stable reason code, never provider/source content."""


def numbered_lines(text, start=0):
    offset, rows = start, []
    for index, raw in enumerate(text.splitlines(keepends=True), 1):
        content = raw.rstrip("\r\n")
        rows.append({"line": index, "text": content, "char_start": offset, "char_end": offset + len(content)})
        offset += len(raw)
    return rows


def execute_program(text, program, options):
    """Strictly parse staged or decision-first output and apply deletions atomically."""
    source = text.splitlines(keepends=True)
    lines = program.strip().splitlines()
    decision = lines.pop(0).strip() if lines and lines[0].strip() in TAGS else None
    if not lines or lines.pop(0).strip() != "<extract>":
        raise InvalidProgram("missing_extract")
    positions = [i for i, line in enumerate(lines) if line.strip() in TAGS]
    if not positions:
        raise InvalidProgram("missing_decision")
    pos = positions[0]
    tag = lines[pos].strip()
    if decision and decision != tag:
        raise InvalidProgram("conflicting_decisions")
    extraction, body = lines[:pos], lines[pos + 1 :]
    if tag != "<rewrite>" and any(line.strip() in TAGS for line in body):
        raise InvalidProgram("multiple_decisions")
    if tag in {"<keep>", "<delete>"} and any(line.strip() for line in body):
        raise InvalidProgram("unexpected_payload")
    removed, spans, operations = set(), {}, []
    for phase, commands in (("extract", extraction), ("edit", body if tag == "<edit>" else [])):
        for raw in commands:
            command = raw.strip()
            if not command:
                continue
            match = RM.fullmatch(command)
            if match:
                first, last = int(match[1]), int(match[2] or match[1])
                if not 1 <= first <= last <= len(source):
                    raise InvalidProgram("line_out_of_range")
                ids = set(range(first, last + 1))
                if ids & (removed | set(spans)):
                    raise InvalidProgram("overlapping_operations")
                removed.update(ids)
                operations.append({"phase": phase, "op": "rm", "start": first, "end": last})
                continue
            match = SUB.fullmatch(command) if phase == "edit" else None
            if not match:
                raise InvalidProgram("invalid_operation")
            index = int(match[1])
            try:
                value = json.loads(match[2])
            except ValueError:
                raise InvalidProgram("invalid_substring") from None
            if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
                raise InvalidProgram("invalid_substring")
            if not 1 <= index <= len(source) or index in removed:
                raise InvalidProgram("line_out_of_range_or_removed")
            original = source[index - 1].rstrip("\r\n")
            if original.count(value) != 1:
                raise InvalidProgram("substring_missing_or_ambiguous")
            begin = original.index(value)
            end = begin + len(value)
            if any(begin < b and end > a for a, b in spans.get(index, [])):
                raise InvalidProgram("overlapping_operations")
            spans.setdefault(index, []).append((begin, end))
            operations.append({"phase": phase, "op": "sub", "line": index, "start": begin, "end": end, "text": value})
    if tag == "<edit>" and not any(op["phase"] == "edit" for op in operations):
        raise InvalidProgram("empty_edit")
    deleted_text, kept = [], []
    for index, line in enumerate(source, 1):
        if index in removed:
            deleted_text.append(line)
            continue
        for start, end in sorted(spans.get(index, []), reverse=True):
            deleted_text.append(line[start:end])
            line = line[:start] + line[end:]
        kept.append(line)
    action = tag[1:-1]
    if action == "delete":
        output = ""
    elif action == "rewrite":
        output = "\n".join(body).strip()
    else:
        output = "".join(kept)
    issues = []
    if action == "rewrite" and not options["allow_rewrite"]:
        issues.append("rewrite_disabled")
    if action != "delete" and (not output.strip() or SYNTAX.search(output)):
        issues.append("empty_or_leaked_program")
    ratio = len(output) / max(len(text), 1)
    if action != "delete" and ratio < options["min_retain_ratio"]:
        issues.append("retention_below_minimum")
    if options["protect_financial"]:
        if action == "rewrite":
            if Counter(FINANCIAL.findall(text)) != Counter(FINANCIAL.findall(output)):
                issues.append("financial_markers_changed")
        elif action == "delete":
            if FINANCIAL.search(text):
                issues.append("financial_content_removed")
        elif any(FINANCIAL.search(value) for value in deleted_text):
            issues.append("financial_content_removed")
        elif Counter(FINANCIAL.findall(text)) != Counter(FINANCIAL.findall(output)):
            issues.append("financial_markers_changed")
    return {"action": action, "text": output, "operations": operations, "retain_ratio": ratio, "issues": issues}


class TextRefiner:
    def __init__(self, pipeline):
        self.pipeline = pipeline
        self.config, self.store, self.context = pipeline.config, pipeline.store, pipeline.context
        self.options = self.config.refiner

    def enqueue(self, governed):
        if not self.options["enabled"]:
            return
        artifact = governed["artifact_uid"]
        enqueue_uid = stable_uid(artifact, self.config.refiner_identity)
        if self.store.get("refiner-enqueued", enqueue_uid):
            return
        quality = governed.get("quality_artifact_uid")
        if not quality:
            return  # Legacy records must first pass the existing OCR quality stage.
        for index, segment in enumerate(self.pipeline.splitter.split(governed["text"])):
            chunks = [
                dict(row)
                for row in self.store.connection.execute(
                    "SELECT chunk_version_uid,content_type,page,metadata_json FROM document_chunk "
                    "WHERE governed_uid=? AND char_end>? AND char_start<? ORDER BY chunk_index",
                    (governed["governed_uid"], segment["char_start"], segment["char_end"]),
                )
            ]
            protected = any(c["content_type"] in {"table", "formula", "figure"} for c in chunks)
            # Block text checks cover mixed semantic chunks as well as split table fragments.
            for block in governed.get("blocks", []):
                if block.get("content_type") not in {"table", "formula", "figure"}:
                    continue
                value = block.get("text", "").strip()
                start = governed["text"].find(value) if value else -1
                if start < 0:
                    protected = True
                while start >= 0:
                    if start < segment["char_end"] and start + len(value) > segment["char_start"]:
                        protected = True
                        break
                    start = governed["text"].find(value, start + len(value))
            payload = {
                "text": segment["text"],
                "work_uid": governed["work_uid"],
                "governed_uid": governed["governed_uid"],
                "source": {"source_name": governed["source_name"], "source_id": governed.get("source_id")},
                "ocr_quality": governed.get("quality", {}),
                "evidence_artifact_uid": artifact,
                "quality_artifact_uid": quality,
                "method": "refined",
                "protected_content": protected,
                "source_offsets": {"char_start": segment["char_start"], "char_end": segment["char_end"], "segment": index},
                "source_chunk_versions": [c["chunk_version_uid"] for c in chunks],
                "refiner_identity": self.config.refiner_identity,
            }
            self.store.enqueue("refine", stable_uid(artifact, index), payload, version=self.config.refiner_identity)
        self.store.put("refiner-enqueued", enqueue_uid, {"artifact_uid": artifact, "refiner_identity": self.config.refiner_identity})

    def run(self, payload, task):
        if not self.options["enabled"] or payload["refiner_identity"] != self.config.refiner_identity:
            raise ValueError("inactive refiner task must not be claimed")
        text = payload["text"]
        evidence = self.store.connection.execute(
            "SELECT path,sha256 FROM artifact WHERE artifact_uid=?", (payload["evidence_artifact_uid"],)
        ).fetchone()
        if not evidence or sha256(safe_path(self.pipeline.root, evidence["path"])) != evidence["sha256"]:
            raise ValueError("refiner source checksum mismatch")
        source = json.loads(safe_path(self.pipeline.root, evidence["path"]).read_text(encoding="utf-8"))
        offsets = payload["source_offsets"]
        if source["text"][offsets["char_start"] : offsets["char_end"]] != text:
            raise ValueError("refiner source offsets mismatch")
        rows = numbered_lines(text, offsets["char_start"])
        directory = self.pipeline.root / "processed" / "refiner" / task["task_uid"]
        parents = [payload["evidence_artifact_uid"], payload["quality_artifact_uid"]]
        source_policy = self.config.policy.get("source_policy", {}).get(payload["source"]["source_name"], {})
        reason = (
            "source_not_approved"
            if source_policy.get("training") != "approved"
            else "ocr_quality_not_passed"
            if self.config.policy.get("require_ocr_pass", True) and payload["ocr_quality"].get("status") != "pass"
            else "protected_content"
            if payload["protected_content"] or PROTECTED.search(text)
            else "input_too_long"
            if len(text) > self.options["max_input_chars"]
            else "source_contains_protocol"
            if SYNTAX.search(text)
            else None
        )
        cached = False
        if reason:
            result = {"status": "skipped", "reason": reason, "action": "none", "text": "", "issues": []}
        else:
            user = f"allow_rewrite={str(self.options['allow_rewrite']).lower()}\nSOURCE:\n" + "\n".join(
                f"<lid:{row['line']}> {row['text']}" for row in rows
            )
            messages = [{"role": "system", "content": self.options["system_prompt"]}, {"role": "user", "content": user}]
            prompt = json.dumps(messages, ensure_ascii=False)
            response_path = directory / "response.json"
            cached = response_path.exists()
            response = self.pipeline._model_result("refine", prompt, response_path, messages=messages)
            parents.append(
                register_file(self.store, self.context, response_path, "refiner-response", tuple(parents), identity=task["task_uid"])
            )
            try:
                if response.get("finish_reason") != "stop" or response.get("refusal"):
                    raise InvalidProgram("refused_or_truncated")
                result = execute_program(text, response["text"], self.options)
                result["status"] = "needs_review" if result["issues"] else "valid"
            except InvalidProgram as exc:
                result = {"status": "needs_review", "action": "invalid", "issues": [str(exc)], "text": ""}
        record = {
            **result,
            "schema_version": "finflow-refiner-v1",
            "refiner_identity": self.config.refiner_identity,
            "mode": self.options["mode"],
            "options": {key: value for key, value in self.options.items() if key != "system_prompt"},
            "model": self.config.roles["refine"].identity(),
            "source_lines": rows,
            "source_offsets": offsets,
            "source_chunk_versions": payload["source_chunk_versions"],
            "input_chars": len(text),
            "output_chars": len(result["text"]),
            "evidence_artifact_uid": payload["evidence_artifact_uid"],
        }
        path = directory / "result.json"
        if path.exists():
            if json.loads(path.read_text(encoding="utf-8")) != record:
                raise ValueError("refiner result checkpoint mismatch")
        else:
            write_json(path, record)
        artifact = register_file(self.store, self.context, path, "refiner-result", tuple(parents), identity=task["task_uid"])
        candidate = None
        if record["status"] == "valid" and record["action"] != "delete" and self.options["mode"] == "apply":
            candidate = self.pipeline._candidate(
                {
                    **payload,
                    "evidence_text": text,
                    "text": record["text"],
                    "refiner_action": record["action"],
                    "refiner_artifact_uid": artifact,
                    "offsets_refer_to": "source_evidence_not_refined_text",
                },
                parents=(artifact, payload["quality_artifact_uid"]),
                dependency=task["task_uid"],
            )
        return {
            "task_status": "needs_review" if record["status"] == "needs_review" else "succeeded",
            "refiner_status": record["status"],
            "action": record["action"],
            "mode": self.options["mode"],
            "artifact_uid": artifact,
            "candidate_uid": candidate,
            "response_cache_hit": cached,
            "input_chars": len(text),
            "output_chars": len(result["text"]),
            "issues": record["issues"],
            "reason": record.get("reason"),
        }
