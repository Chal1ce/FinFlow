"""Resumable evidence-based SFT generation from verified, locally approved v7 releases."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from collections import Counter
from pathlib import Path

from core.flywheel_files import register_file, safe_path, sha256, write_json
from core.ids import stable_uid
from processing.visual_description import ModelBudgetExceeded, RoleClient
from storage.state_store import utc_now
from training.flywheel_corpus import checksums, jsonl, read_jsonl, verify
from training.pretrain import redact_direct_contacts
from training.sft_recipes import generation_prompt, normalize_sample, parse_object, review_decision, review_prompt
from training.sft_splits import assign_splits
from workflow.flywheel_config import digest


class SFTBuilder:
    def __init__(self, root, store, context, config, source_policy, *, client=None):
        self.root, self.store, self.context = Path(root).resolve(), store, context
        self.config, self.source_policy = config, source_policy
        self.client = client or RoleClient(config)

    def _artifact_file(self, release_path, artifacts, uid):
        artifact = artifacts[uid]
        path = safe_path(release_path, artifact["path"])
        local = self.store.connection.execute("SELECT sha256 FROM artifact WHERE artifact_uid=?", (uid,)).fetchone()
        if not local or local[0] != artifact["sha256"] or sha256(path) != artifact["sha256"]:
            raise ValueError("SFT requires verified evidence registered in this data root")
        return path

    def _evidence(self, release_path):
        artifacts = {r["artifact_uid"]: r for r in read_jsonl(release_path / "artifacts.jsonl")}
        evidence, excluded = [], []
        for row in read_jsonl(release_path / "training-candidates.jsonl"):
            if row["status"] != "accepted" or row["method"] not in {"original", "visual"}:
                continue
            candidate = json.loads(row["data_json"])
            uid = row["candidate_uid"]
            current = self.store.connection.execute("SELECT * FROM training_candidate WHERE candidate_uid=?", (uid,)).fetchone()
            reason = None
            if not current or current["status"] != "accepted" or current["decision_json"] != row["decision_json"]:
                reason = "upstream_decision_changed_republish_evidence"
            elif current["data_json"] != row["data_json"] or current["artifact_uid"] != row["artifact_uid"]:
                reason = "upstream_candidate_changed"
            elif self.source_policy.get(candidate["source"].get("source_name"), {}).get("training") != "approved":
                reason = "source_training_not_approved"
            if reason:
                excluded.append({"candidate_uid": uid, "status": "excluded", "reason": reason})
                continue
            original = self._artifact_file(release_path, artifacts, row["artifact_uid"])
            if sha256(original) != candidate["sha256"]:
                raise ValueError("SFT candidate checksum mismatch")
            record = json.loads(original.read_text(encoding="utf-8"))
            decision = json.loads(row["decision_json"])
            self._artifact_file(release_path, artifacts, decision["artifact_uid"])
            parents = [row["artifact_uid"], decision["artifact_uid"]]
            asset = record.get("asset") or {}
            image_path = None
            if row["method"] == "visual":
                if asset.get("asset_kind") != "table" or not asset.get("table_artifact_uid"):
                    continue  # Text SFT never treats a generated image description as ground truth.
                table_path = self._artifact_file(release_path, artifacts, asset["table_artifact_uid"])
                table = json.loads(table_path.read_text(encoding="utf-8"))
                text = table["markdown"]
                if not text.strip():
                    excluded.append({"candidate_uid": uid, "status": "excluded", "reason": "empty_table_representation"})
                    continue
                # Include the OCR context for units/captions, not the generated description.
                text = "表格 OCR：\n" + text + "\n邻近 OCR 上下文：\n" + asset.get("context", "")
                image_path = self._artifact_file(release_path, artifacts, asset["artifact_uid"])
                parents.extend([asset["table_artifact_uid"], asset["artifact_uid"], asset["context_artifact_uid"]])
                self._artifact_file(release_path, artifacts, asset["context_artifact_uid"])
                if sha256(image_path) != asset["sha256"]:
                    raise ValueError("SFT table image checksum mismatch")
            else:
                text = record["text"]
            text, redactions = redact_direct_contacts(text)
            if not text.strip() or len(text) > self.config.policy.get("max_evidence_chars", 12000):
                excluded.append({"candidate_uid": uid, "status": "excluded", "reason": "evidence_empty_or_over_character_limit"})
                continue
            evidence.append(
                {
                    "evidence_uid": stable_uid("sft-evidence-v1", uid, digest(text), decision["artifact_uid"]),
                    "candidate_uid": uid,
                    "work_uid": record["work_uid"],
                    "kind": "table" if image_path else "text",
                    "text": text,
                    "content_hash": hashlib.sha256(text.encode()).hexdigest(),
                    "source": record["source"],
                    "parents": parents,
                    "source_offsets": record.get("source_offsets"),
                    "source_chunk_versions": record.get("source_chunk_versions", []),
                    "visual_asset_uid": asset.get("visual_asset_uid"),
                    "page": asset.get("page"),
                    "block_index": asset.get("block_index"),
                    "redaction": redactions,
                    "image_path": self.context.relative_path(image_path) if image_path else None,
                    "image_sha256": asset.get("sha256") if image_path else None,
                    "upstream_decision_artifact_uid": decision["artifact_uid"],
                }
            )
        return evidence, excluded

    def _model(self, role, prompt, parents, *, evidence=None):
        image_path = evidence.get("image_path") if evidence else None
        image_hash = evidence.get("image_sha256") if evidence else None
        identity = digest({"role": self.config.roles[role].identity(), "prompt": prompt, "image_sha256": image_hash})
        path = self.root / "training" / "sft" / "responses" / f"{identity}.json"
        previous = self.store.get("sft-response", identity)
        if path.exists():
            cached = json.loads(path.read_text(encoding="utf-8"))
            if cached.get("identity") != identity or (previous and previous["sha256"] != sha256(path)):
                raise ValueError("SFT response checkpoint mismatch")
            response = cached["response"]
        else:
            if time.monotonic() >= self.deadline:
                raise ModelBudgetExceeded("SFT time limit reached")
            response = self.client.generate(
                role, prompt, image=safe_path(self.root, image_path) if image_path else None, image_hash=image_hash
            )
            write_json(path, {"identity": identity, "prompt": prompt, "response": response})
        artifact = register_file(self.store, self.context, path, "sft-model-response", tuple(parents), identity=identity)
        self.store.put("sft-response", identity, {"sha256": sha256(path)})
        return response, artifact

    def _generate_job(self, task, evidence, job_uid):
        existing = self.store.get("sft-job", job_uid)
        if existing:
            path = safe_path(self.root, existing["path"])
            if sha256(path) != existing["sha256"]:
                raise ValueError("SFT job checkpoint mismatch")
            result = json.loads(path.read_text(encoding="utf-8"))
            artifact_ids = {sample["artifact_uid"] for sample in result["samples"]}
            artifact_ids.update(sample["generation_artifact_uid"] for sample in result["samples"])
            artifact_ids.update(entry["artifact_uid"] for entry in result["audit"] if entry.get("artifact_uid"))
            for uid in artifact_ids:
                record = self.store.connection.execute("SELECT path,sha256 FROM artifact WHERE artifact_uid=?", (uid,)).fetchone()
                if not record or sha256(safe_path(self.root, record[0])) != record[1]:
                    raise ValueError("SFT cached artifact checksum mismatch")
            return result
        prompt = generation_prompt(task, evidence, self.config)
        response, generated_artifact = self._model("sft_generate", prompt, evidence["parents"])
        samples, audit = [], []
        parents = [*evidence["parents"], generated_artifact]
        try:
            raw = parse_object(response).get("samples")
            if not isinstance(raw, list) or len(raw) > self.config.policy.get("samples_per_task", 2):
                raise ValueError("invalid_sample_count")
        except (ValueError, KeyError, IndexError, TypeError):
            raw = []
            audit.append({"status": "needs_review", "reason": "invalid_generation_response", "artifact_uid": generated_artifact})
        if not raw and not audit:
            audit.append({"status": "skipped", "reason": "generator_found_no_supported_samples"})
        seen = set()
        for index, item in enumerate(raw):
            try:
                sample = normalize_sample(task, item, evidence, self.config)
            except (ValueError, KeyError, TypeError):
                audit.append({"index": index, "status": "rejected", "reason": "deterministic_validation_failed"})
                continue
            content_hash = digest(sample["messages"])
            if content_hash in seen:
                audit.append({"index": index, "status": "excluded", "reason": "duplicate_generated_sample"})
                continue
            seen.add(content_hash)
            prompt = review_prompt(sample, evidence, self.config)
            reviewed, review_artifact = self._model("sft_review", prompt, parents, evidence=evidence)
            parents.append(review_artifact)
            try:
                decision = review_decision(reviewed)
            except (ValueError, KeyError, IndexError, TypeError):
                decision = {"status": "needs_review", "reasons": ["invalid_review_response"]}
            audit.append({"index": index, **decision, "artifact_uid": review_artifact})
            if decision["status"] != "accepted":
                continue
            sample_uid = "sft-" + stable_uid(self.config.version, job_uid, digest(sample["messages"]))
            sample = {
                **sample,
                "schema_version": "financial-sft-sample-v1",
                "sample_id": sample_uid,
                "synthetic": True,
                "split": evidence["split"],
                "recipe_uid": self.config.version,
                "evidence_uid": evidence["evidence_uid"],
                "work_uid": evidence["work_uid"],
                "candidate_uid": evidence["candidate_uid"],
                "decision": decision,
                "content_hash": digest(sample["messages"]),
                "generation_artifact_uid": generated_artifact,
                "review_artifact_uid": review_artifact,
            }
            path = self.root / "training" / "sft" / "samples" / f"{sample_uid}.json"
            if path.exists():
                if json.loads(path.read_text(encoding="utf-8")) != sample:
                    raise ValueError("SFT sample identity mismatch")
            else:
                write_json(path, sample)
            artifact = register_file(self.store, self.context, path, "sft-sample", tuple(parents), identity=sample_uid)
            sample = {**sample, "artifact_uid": artifact}
            self.store.put("sft-sample", sample_uid, sample)
            samples.append(sample)
        result = {"job_uid": job_uid, "task": task, "evidence_uid": evidence["evidence_uid"], "samples": samples, "audit": audit}
        path = self.root / "training" / "sft" / "jobs" / f"{job_uid}.json"
        write_json(path, result)
        artifact = register_file(self.store, self.context, path, "sft-job", tuple(parents), identity=job_uid)
        self.store.put("sft-job", job_uid, {"path": self.context.relative_path(path), "sha256": sha256(path), "artifact_uid": artifact})
        return result

    def build(self, dataset_id, release, *, max_seconds=None):
        if not dataset_id or Path(dataset_id).name != dataset_id or dataset_id in {".", ".."}:
            raise ValueError("dataset_id must be a directory name")
        check = self.config.preflight()
        if check["errors"]:
            raise ValueError("; ".join(check["errors"]))
        started = time.monotonic()
        seconds = (
            min(self.config.policy.get("max_seconds", 600), max_seconds)
            if max_seconds is not None
            else self.config.policy.get("max_seconds", 600)
        )
        self.deadline = started + max(0, seconds)
        release_path = safe_path(self.root / "published" / "flywheel", release)
        manifest = verify(release_path)
        if manifest.get("format_version") != "financial-document-delivery-v7":
            raise ValueError("SFT requires a v7 evidence release")
        release_record = {"path": self.context.relative_path(release_path), "manifest_sha256": sha256(release_path / "manifest.json")}
        destination = safe_path(self.root / "training" / "sft" / "datasets", dataset_id)
        if destination.exists():
            previous = verify(destination)
            if previous.get("recipe_uid") != self.config.version or previous.get("release") != release_record:
                raise ValueError("dataset_id already belongs to a different SFT recipe/release")
            self._index(destination, previous)
            # A snapshot is historical and immutable. Use a new id after changing approval policy.
            return {"status": "existing", "path": str(destination), "manifest": previous}
        evidence, audit = self._evidence(release_path)
        assign_splits(self.store, evidence, self.config.policy.get("validation_fraction", 0.05), self.root)
        samples, jobs, completed, pending, errors = {}, [], 0, 0, []
        stopped = False
        for item in sorted(evidence, key=lambda e: e["evidence_uid"]):
            if item["split"] is None:
                audit.append({"evidence_uid": item["evidence_uid"], "status": "needs_review", "reason": "split_conflict"})
                continue
            for task in self.config.policy["tasks"]:
                if (task == "table_calculation") != (item["kind"] == "table"):
                    continue
                job_uid = stable_uid("sft-job-v1", self.config.version, item["evidence_uid"], item["split"], task)
                cached = self.store.get("sft-job", job_uid)
                if not cached and (stopped or completed >= self.config.policy.get("max_jobs", 20) or time.monotonic() - started >= seconds):
                    pending += 1
                    continue
                try:
                    result = self._generate_job(task, item, job_uid)
                except ModelBudgetExceeded:
                    stopped = True
                    pending += 1
                    continue
                except Exception as exc:
                    # Do not persist SDK messages, credential-bearing URLs or request payloads in errors.
                    errors.append({"job_uid": job_uid, "error_type": type(exc).__name__})
                    pending += 1
                    stopped = True
                    continue
                if not cached:
                    completed += 1
                job_record = self.store.get("sft-job", job_uid)
                jobs.append({"job_uid": job_uid, **job_record})
                audit.extend({"job_uid": job_uid, "evidence_uid": item["evidence_uid"], **entry} for entry in result["audit"])
                for sample in result["samples"]:
                    key = sample["content_hash"]
                    if key in samples:
                        if samples[key]["split"] != sample["split"]:
                            raise ValueError("duplicate SFT content crossed splits")
                        audit.append(
                            {
                                "sample_id": sample["sample_id"],
                                "duplicate_of": samples[key]["sample_id"],
                                "status": "excluded",
                                "reason": "exact_duplicate_messages",
                                "artifact_uid": sample["artifact_uid"],
                            }
                        )
                    else:
                        samples[key] = sample
        counts = Counter(s["task"] for s in samples.values())
        status = "partial" if pending or errors or any(x["status"] == "needs_review" for x in audit) else "success"
        manifest = {
            "schema_version": "financial-sft-dataset-v1",
            "dataset_id": dataset_id,
            "created_at": utc_now(),
            "status": status,
            "recipe_uid": self.config.version,
            "recipe": self.config.policy,
            "models": {k: v.identity() for k, v in self.config.roles.items()},
            "release": release_record,
            "source_policy_sha256": digest(self.source_policy),
            "samples": len(samples),
            "tasks": dict(counts),
            "splits": dict(Counter(s["split"] for s in samples.values())),
            "completed_jobs": completed,
            "pending_jobs": pending,
            "errors": errors,
            "model_requests": self.client.requests,
            "model_usage": self.client.usage,
            "split_policy": "persistent-logical-work-and-exact-context-v1-inherits-cpt",
            "scope": "snapshot-of-current-accepted-results-for-selected-release-and-recipe",
            "training_format": "messages; no tokenizer, chat template, packing or truncation applied",
            "audit_counts": dict(Counter(a["status"] for a in audit)),
        }
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".sft-", dir=destination.parent))
        try:
            rows = sorted(samples.values(), key=lambda s: s["sample_id"])
            jsonl(staging / "samples.jsonl", rows)
            for split in ("train", "validation"):
                jsonl(
                    staging / f"{split}.jsonl",
                    ({"sample_id": s["sample_id"], "messages": s["messages"]} for s in rows if s["split"] == split),
                )
            jsonl(staging / "evidence.jsonl", evidence)
            jsonl(staging / "audit.jsonl", audit)
            jsonl(staging / "jobs.jsonl", jobs)
            write_json(staging / "manifest.json", manifest)
            checksums(staging)
            verify(staging)
            os.replace(staging, destination)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        self._index(destination, manifest)
        return {
            "status": status,
            "dataset_id": dataset_id,
            "path": str(destination),
            "samples": len(samples),
            "tasks": dict(counts),
            "pending_jobs": pending,
            "errors": errors,
            "model_requests": self.client.requests,
            "model_usage": self.client.usage,
        }

    def _index(self, destination, manifest):
        artifact = register_file(
            self.store,
            self.context,
            destination / "manifest.json",
            "sft-dataset",
            tuple(j["artifact_uid"] for j in read_jsonl(destination / "jobs.jsonl")),
            identity=manifest["dataset_id"],
        )
        self.store.put(
            "sft-dataset",
            manifest["dataset_id"],
            {"path": self.context.relative_path(destination), "artifact_uid": artifact, "manifest": manifest},
        )
