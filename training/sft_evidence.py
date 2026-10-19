"""Read governed v7 evidence for SFT and agents using the same upstream decisions."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from core.flywheel_files import safe_path, sha256
from core.ids import stable_uid
from training.flywheel_corpus import read_jsonl
from training.pretrain import redact_direct_contacts
from training.sft_config import task_supports
from training.sft_export import inspect_image
from workflow.flywheel_config import digest


class EvidenceLoader:
    def __init__(self, root, connection, policy, source_policy):
        self.root = Path(root).resolve()
        self.connection = connection
        self.policy = policy
        self.source_policy = source_policy

    def artifact_file(self, release_path, artifacts, uid):
        artifact = artifacts[uid]
        path = safe_path(release_path, artifact["path"])
        local = self.connection.execute("SELECT sha256 FROM artifact WHERE artifact_uid=?", (uid,)).fetchone()
        if not local or local[0] != artifact["sha256"] or sha256(path) != artifact["sha256"]:
            raise ValueError("SFT requires verified evidence registered in this data root")
        return path

    def load(self, release_path):
        artifacts = {r["artifact_uid"]: r for r in read_jsonl(release_path / "artifacts.jsonl")}
        evidence, excluded = [], []
        for row in read_jsonl(release_path / "training-candidates.jsonl"):
            if row["status"] != "accepted" or row["method"] not in {"original", "visual"}:
                continue
            candidate = json.loads(row["data_json"])
            uid = row["candidate_uid"]
            current = self.connection.execute("SELECT * FROM training_candidate WHERE candidate_uid=?", (uid,)).fetchone()
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
            original = self.artifact_file(release_path, artifacts, row["artifact_uid"])
            if sha256(original) != candidate["sha256"]:
                raise ValueError("SFT candidate checksum mismatch")
            record = json.loads(original.read_text(encoding="utf-8"))
            decision = json.loads(row["decision_json"])
            self.artifact_file(release_path, artifacts, decision["artifact_uid"])
            parents = [row["artifact_uid"], decision["artifact_uid"]]
            asset = record.get("asset") or {}
            image_path = None
            kind = asset.get("asset_kind") if row["method"] == "visual" else "text"
            if not any(task_supports(task, kind) for task in self.policy["tasks"]):
                continue
            image_info = None
            if row["method"] == "visual":
                # Generated descriptions are never substituted for original visual evidence.
                if kind not in {"image", "table"} or asset.get("status") != "success":
                    excluded.append({"candidate_uid": uid, "status": "excluded", "reason": "visual_asset_not_ready"})
                    continue
                image_path = self.artifact_file(release_path, artifacts, asset["artifact_uid"])
                context_path = self.artifact_file(release_path, artifacts, asset["context_artifact_uid"])
                context_record = json.loads(context_path.read_text(encoding="utf-8"))
                parents.extend([asset["artifact_uid"], asset["context_artifact_uid"]])
                try:
                    image_info = inspect_image(image_path, asset["sha256"], self.policy)
                except (ValueError, OSError) as exc:
                    excluded.append(
                        {
                            "candidate_uid": uid,
                            "status": "needs_review",
                            "reason": "image_validation_failed",
                            "error_type": type(exc).__name__,
                        }
                    )
                    continue
                if kind == "table":
                    if not asset.get("table_artifact_uid"):
                        excluded.append({"candidate_uid": uid, "status": "excluded", "reason": "missing_table_representation"})
                        continue
                    table_path = self.artifact_file(release_path, artifacts, asset["table_artifact_uid"])
                    table = json.loads(table_path.read_text(encoding="utf-8"))
                    text = "表格 OCR：\n" + table["markdown"] + "\n邻近 OCR 上下文：\n" + context_record.get("context", "")
                    parents.append(asset["table_artifact_uid"])
                else:
                    text = context_record.get("context", "")
            else:
                text = record["text"]
            text, redactions = redact_direct_contacts(text)
            if (not text.strip() and not image_path) or len(text) > self.policy.get("max_evidence_chars", 12000):
                excluded.append({"candidate_uid": uid, "status": "excluded", "reason": "evidence_empty_or_over_character_limit"})
                continue
            evidence.append(
                {
                    "evidence_uid": stable_uid("sft-evidence-v1", uid, digest(text), decision["artifact_uid"]),
                    "candidate_uid": uid,
                    "work_uid": record["work_uid"],
                    "kind": kind,
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
                    "image_path": image_path.resolve().relative_to(self.root).as_posix() if image_path else None,
                    "image_sha256": asset.get("sha256") if image_path else None,
                    "image_artifact_uid": asset.get("artifact_uid") if image_path else None,
                    "image_info": image_info,
                    "image_origin": {
                        key: asset.get(key)
                        for key in (
                            "document_uid",
                            "source_asset_uid",
                            "ocr_artifact_uid",
                            "coordinate_space",
                            "bbox",
                            "extraction_method",
                            "crop_transform",
                        )
                    }
                    if image_path
                    else None,
                    "upstream_decision_artifact_uid": decision["artifact_uid"],
                }
            )
        return evidence, excluded
