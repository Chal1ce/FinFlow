"""Resumable local training publication. Caller must hold the daily pipeline lock."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from core.flywheel_files import register_file, safe_path, sha256, write_json
from storage.state_store import utc_now
from training.flywheel_corpus import build_snapshot, checksums, read_jsonl, verify
from training.mixture import build as build_mixture, verify_mixture
from training.mixture_data import load_pool
from training.near_dedup import apply_report, index_packages, scan
from training.quality_report import publish_report
from workflow.flywheel_config import digest

VERSION = "daily-training-release-v1"


class PublicationBlocked(ValueError):
    """A safe machine-readable reason; contains no source or provider response."""


def identity(path):
    path = Path(path)
    verify(path)
    return {"manifest_sha256": sha256(path / "manifest.json"), "inventory_sha256": sha256(path / "checksums.sha256")}


def verify_release(path):
    path = Path(path)
    manifest = verify(path)
    if manifest.get("schema_version") != VERSION or not manifest.get("tracks"):
        raise ValueError("invalid training release")
    for kind, record in manifest["tracks"].items():
        if kind not in {"cpt", "sft"} or identity(path / kind) != record["identity"]:
            raise ValueError("training release component identity mismatch")
        if verify_mixture(path / kind)["kind"] != kind:
            raise ValueError("training release component kind mismatch")
        report = verify(path / "reports" / kind)
        if report.get("package", {}).get("manifest_sha256") != record["identity"]["manifest_sha256"]:
            raise ValueError("training release report identity mismatch")
    return manifest


class DailyTrainingRelease:
    def __init__(self, config, store, context, *, deadline=None):
        self.config, self.store, self.context = config, store, context
        self.root, self.options = config.root, config.release_options
        self.deadline = deadline
        self.stages = []
        self.track_summaries = {}
        self.current_stage = "inputs"

    def _check_time(self):
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise TimeoutError("daily publication deadline reached")

    def _stage(self, name, key, path, build, *, parents=(), validator=verify):
        """A completed directory is recovered after rename even if the DB write was interrupted."""
        self.current_stage = name
        uid = digest([VERSION, name, key])
        previous = self.store.get("training-stage", uid)
        record = {
            **(previous or {}),
            "stage_uid": uid,
            "stage": name,
            "input_identity": key,
            "path": self.context.relative_path(path),
            "run_id": self.context.run_id,
            "attempts": (previous or {}).get("attempts", 0),
            "status": "running",
        }
        exists = path.exists()
        if not exists:
            self._check_time()
            if previous and previous.get("identity"):
                raise ValueError("completed publication artifact is missing; restore it before retrying")
            record["attempts"] += 1
        self.store.put("training-stage", uid, record)
        started = time.monotonic()
        try:
            if not exists:
                build()
            manifest = validator(path)
            fingerprint = identity(path)
            if previous and previous.get("identity") and previous["identity"] != fingerprint:
                raise ValueError("cached publication artifact changed")
            artifact = register_file(
                self.store,
                self.context,
                path / "manifest.json",
                "training-publication-" + name,
                tuple(sorted(set(parents))),
                identity=uid,
                inventory_sha256=fingerprint["inventory_sha256"],
            )
            record.update(status="success", identity=fingerprint, artifact_uid=artifact)
            self.store.put("training-stage", uid, record)
            self.stages.append(
                {
                    "stage": name,
                    "stage_uid": uid,
                    "status": "reused" if exists else "built",
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "path": record["path"],
                }
            )
            return {"path": path, "manifest": manifest, "identity": fingerprint, "artifact_uid": artifact}
        except BaseException as exc:
            self.store.put("training-stage", uid, {**record, "status": "failed", "error_type": type(exc).__name__})
            raise

    def _input(self, path, manifest):
        """Bind immutable source packages to existing candidate/sample artifacts."""
        parents = set()
        if manifest["schema_version"] == "financial-sft-dataset-v2":
            for row in read_jsonl(path / "samples.jsonl"):
                current = self.store.connection.execute(
                    "SELECT status FROM training_candidate WHERE candidate_uid=?", (row["candidate_uid"],)
                ).fetchone()
                policy = self.config.policy.get("source_policy", {}).get(row.get("source", {}).get("source_name"), {})
                if not current or current["status"] != "accepted" or policy.get("training") != "approved":
                    raise PublicationBlocked("sft_upstream_approval_changed")
                parents.add(row["artifact_uid"])
        else:
            for row in read_jsonl(path / "provenance.jsonl"):
                current = self.store.connection.execute(
                    "SELECT status FROM training_candidate WHERE candidate_uid=?", (row["candidate_uid"],)
                ).fetchone()
                policy = self.config.policy.get("source_policy", {}).get(row.get("source", {}).get("source_name"), {})
                if not current or current["status"] != "accepted" or policy.get("training") != "approved":
                    raise PublicationBlocked("historical_cpt_approval_changed")
                parents.add(row["artifact_uid"])
                if row.get("decision", {}).get("artifact_uid"):
                    parents.add(row["decision"]["artifact_uid"])
        fingerprint = identity(path)
        artifact = register_file(
            self.store,
            self.context,
            path / "manifest.json",
            "training-publication-input",
            tuple(sorted(parents)),
            identity=digest(fingerprint),
            inventory_sha256=fingerprint["inventory_sha256"],
        )
        self.store.put(
            "training-input",
            digest(fingerprint),
            {
                "path": self.context.relative_path(path),
                "identity": fingerprint,
                "artifact_uid": artifact,
                "schema_version": manifest["schema_version"],
            },
        )
        return artifact, fingerprint

    def _cpt(self, recipe_uid):
        selected = {"pretrain": [], "origin_deltas": []}
        parents, fingerprints = [], []
        for folder in selected:
            for path in sorted((self.root / "training" / folder).glob("*")):
                self._check_time()
                if not path.is_dir() or path.name.startswith("."):
                    continue
                manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
                if manifest.get("recipe_uid") != recipe_uid:
                    continue
                expected = "continued-pretraining-delta-v2" if folder == "pretrain" else "training-lineage-delta-v1"
                if manifest.get("schema_version") != expected:
                    raise ValueError("unexpected CPT input schema")
                artifact, fingerprint = self._input(path, manifest)
                parents.append(artifact)
                fingerprints.append({"folder": folder, "name": path.name, **fingerprint})
                selected[folder].append(path.name)
        if not selected["pretrain"]:
            return None
        key = {"recipe_uid": recipe_uid, "inputs": fingerprints}
        name = "daily-" + digest(key)
        path = self.root / "training" / "snapshots" / name
        return self._stage(
            "cpt-snapshot",
            key,
            path,
            lambda: build_snapshot(self.root, name, selected["pretrain"], origin_deltas=selected["origin_deltas"]),
            parents=parents,
        )

    def _track(self, kind, package):
        inputs = [str(package["path"])]
        mix = dict(self.options["tracks"][kind])
        mix["inputs"] = inputs
        parents = [package["artifact_uid"]]
        rows, pool = load_pool(inputs, mix["group_fields"])
        decisions = None
        near = {"status": "disabled", "excluded": 0}
        if self.options["near_dedup"]:
            options = self.options["near_options"]
            # Configuration changes receive a separate index; previous fingerprints remain intact.
            index_recipe = digest([options, package["manifest"].get("recipe_uid")])
            database = self.root / "indexes" / ("daily-" + kind + "-" + index_recipe + ".sqlite")
            self.current_stage = kind + "-index"
            self._check_time()
            index_key = digest([kind, index_recipe, package["identity"]])
            before = self.store.get("training-index", index_key)
            started = time.monotonic()
            indexed = index_packages(inputs, database, options)
            self.store.put(
                "training-index",
                index_key,
                {
                    "status": "success",
                    "run_id": self.context.run_id,
                    "path": self.context.relative_path(database),
                    "input": package["identity"],
                    **indexed,
                },
            )
            self.stages.append(
                {
                    **indexed,
                    "stage": self.current_stage,
                    "status": "reused" if before and not indexed.get("added") else "indexed",
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                }
            )
            key = {"input": package["identity"], "options": options}
            report_path = self.root / "reports" / "near-dedup" / (kind + "-" + digest(key))
            report = self._stage(kind + "-dedup", key, report_path, lambda: scan(inputs, database, report_path, options), parents=parents)
            decisions, manifest = apply_report(rows, pool, report_path)
            near = {
                "status": "success",
                "excluded": manifest["counts"].get("exclude", 0),
                "retained": manifest["counts"].get("keep", 0),
                "relations": manifest["relations"],
                "path": self.context.relative_path(report_path),
            }
            mix["near_dedup_report"] = str(report_path)
            parents.append(report["artifact_uid"])
        self.current_stage = kind + "-mixture"
        eligible = [r for r in rows if r["split"] == "train" and (decisions is None or decisions[r["id"]]["action"] == "keep")]
        if mix["budget"] is None:
            mix["budget"] = sum(r["token_count"] if mix["unit"] == "tokens" else 1 for r in eligible)
        if not mix["budget"]:
            raise PublicationBlocked("no_eligible_training_records")
        key = {"input": package["identity"], "mixture": mix}
        path = self.root / "training" / "mixtures" / (kind + "-" + digest(key))
        result = self._stage(
            kind + "-mixture",
            key,
            path,
            lambda: build_mixture(mix, path, allow_partial_artifact=True),
            parents=parents,
            validator=verify_mixture,
        )
        manifest = result["manifest"]
        result["summary"] = {
            "input_samples": len(rows),
            "near_dedup": near,
            "train_samples": manifest["train_samples"],
            "validation_samples": manifest["validation_samples"],
            "unit": manifest["unit"],
            "target": manifest["budget"],
            "actual": manifest["actual"],
            "shortfall": manifest["shortfall"],
            "groups": manifest["groups"],
        }
        self.track_summaries[kind] = result["summary"]
        if manifest["train_samples"] == 0:
            raise PublicationBlocked("empty_training_selection")
        if manifest["shortfall"] and not mix["allow_shortfall"]:
            raise PublicationBlocked("mixture_quota_shortfall")
        for row in read_jsonl(path / "records.jsonl"):
            previous = self.store.get("training-export-sample", row["id"]) or {}
            origins = list({digest(o): o for o in [*previous.get("origins", []), *row["origins"]]}.values())
            self.store.put(
                "training-export-sample",
                row["id"],
                {
                    "sample_id": row["id"],
                    "content_hash": row["content_hash"],
                    "kind": kind,
                    "origins": origins,
                    "mixture_artifact_uid": result["artifact_uid"],
                },
            )
        return result

    def latest(self):
        pointer = self.root / "training" / "latest.json"
        if not pointer.exists():
            return None
        record = json.loads(pointer.read_text(encoding="utf-8"))
        path = safe_path(self.root, record["path"])
        manifest = verify_release(path)
        if identity(path) != record["identity"] or manifest["release_id"] != record["release_id"]:
            raise ValueError("latest training pointer identity mismatch")
        # The atomically replaced pointer is authoritative after a process crash.
        self.store.put("training-latest", "current", record)
        return record

    def run(self, *, cpt_recipe, sft_dataset=None):
        if not self.options["enabled"]:
            return {"status": "disabled"}
        old = None
        committed = None
        try:
            old = self.latest()
            packages = {}
            cpt = self._cpt(cpt_recipe)
            if cpt:
                packages["cpt"] = cpt
            if self.config.sft is not None:
                if not sft_dataset or not sft_dataset.get("path"):
                    raise PublicationBlocked("sft_snapshot_unavailable")
                path = Path(sft_dataset["path"])
                manifest = verify(path)
                if manifest.get("recipe_uid") != self.config.sft.version or manifest.get("status") != "success":
                    raise PublicationBlocked("sft_snapshot_incomplete_or_recipe_mismatch")
                if not manifest.get("samples"):
                    raise PublicationBlocked("sft_snapshot_empty")
                if manifest.get("source_policy_sha256") != digest(self.config.policy.get("source_policy", {})):
                    raise PublicationBlocked("sft_source_policy_changed")
                artifact, fingerprint = self._input(path, manifest)
                packages["sft"] = {"path": path, "manifest": manifest, "artifact_uid": artifact, "identity": fingerprint}
            if not packages:
                return {"status": "not_available", "stages": self.stages, "latest": old}
            key = {"engine": VERSION, "options": self.options, "inputs": {k: p["identity"] for k, p in packages.items()}}
            uid = "release-" + digest(key)
            destination = self.root / "training" / "releases" / uid
            if old and old["release_id"] == uid:
                return {
                    "status": "no_change",
                    "release_id": uid,
                    "latest": old,
                    "stages": self.stages,
                    "tracks": verify_release(destination)["summary"],
                }
            selected = {kind: self._track(kind, package) for kind, package in packages.items()}
            reports = {}
            for kind, package in selected.items():
                baseline = safe_path(self.root, old["path"]) / "reports" / kind if old else None
                if baseline and (
                    not baseline.exists() or verify(baseline)["package"].get("tokenizer") != package["manifest"].get("tokenizer")
                ):
                    baseline = None
                report_key = {
                    "input": package["identity"],
                    "baseline": identity(baseline) if baseline else None,
                    "targets": self.options["coverage_targets"],
                }
                path = self.root / "reports" / "releases" / (kind + "-" + digest(report_key))
                reports[kind] = self._stage(
                    kind + "-report",
                    report_key,
                    path,
                    lambda p=package, out=path, base=baseline: publish_report(
                        p["path"], out, baseline=base, targets=self.options["coverage_targets"]
                    ),
                    parents=[package["artifact_uid"]],
                )
            parents = [p["artifact_uid"] for p in [*selected.values(), *reports.values()]]

            def publish():
                destination.parent.mkdir(parents=True, exist_ok=True)
                staging = Path(tempfile.mkdtemp(prefix=".release-", dir=destination.parent))
                try:
                    for kind, package in selected.items():
                        shutil.copytree(package["path"], staging / kind)
                        shutil.copytree(reports[kind]["path"], staging / "reports" / kind)
                    write_json(
                        staging / "manifest.json",
                        {
                            "schema_version": VERSION,
                            "release_id": uid,
                            "created_at": utc_now(),
                            "run_id": self.context.run_id,
                            "recipe": key,
                            "previous_release": old,
                            "tracks": {k: {"path": k, "identity": p["identity"]} for k, p in selected.items()},
                            "summary": {k: p["summary"] for k, p in selected.items()},
                            "scope": "cumulative-selected-training-snapshot",
                            "policy": "immutable; latest advances only after verification",
                        },
                    )
                    checksums(staging)
                    verify_release(staging)
                    os.rename(staging, destination)
                finally:
                    if staging.exists():
                        shutil.rmtree(staging)

            result = self._stage("release", key, destination, publish, parents=parents, validator=verify_release)
            self.current_stage = "latest"
            pointer = {
                "release_id": uid,
                "path": self.context.relative_path(destination),
                "identity": result["identity"],
                "artifact_uid": result["artifact_uid"],
                "published_at": utc_now(),
            }
            self.store.put("training-release", uid, pointer)
            # No provider calls or mutable source writes occur after publication starts.
            write_json(self.root / "training" / "latest.json", pointer)
            committed = pointer
            self.store.put("training-latest", "current", pointer)
            return {
                "status": "success",
                "release_id": uid,
                "latest": pointer,
                "stages": self.stages,
                "tracks": result["manifest"]["summary"],
                "previous_release_id": old["release_id"] if old else None,
            }
        except Exception as exc:
            if committed:
                return {
                    "status": "success",
                    "release_id": committed["release_id"],
                    "latest": committed,
                    "stages": self.stages,
                    "tracks": self.track_summaries,
                    "warnings": ["latest_database_sync_pending"],
                    "error_type": type(exc).__name__,
                }
            return {
                "status": "deferred" if isinstance(exc, TimeoutError) else "failed",
                "stage": self.current_stage,
                "error_type": type(exc).__name__,
                "reason": str(exc)
                if isinstance(exc, PublicationBlocked)
                else "deadline_reached"
                if isinstance(exc, TimeoutError)
                else "artifact_or_configuration_error",
                "stages": self.stages,
                "latest": old,
                "tracks": self.track_summaries,
            }
