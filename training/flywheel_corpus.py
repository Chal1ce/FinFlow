"""Tokenizer-bounded immutable daily deltas and deduplicated cumulative snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from importlib.metadata import version
from pathlib import Path

from core.flywheel_files import safe_path, sha256, write_json
from core.ids import stable_uid
from storage.state_store import utc_now
from training.pretrain import redact_direct_contacts
from workflow.flywheel_config import digest


def jsonl(path, records):
    with Path(path).open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_jsonl(path):
    with Path(path).open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def checksums(root):
    root = Path(root)
    entries = [
        f"{sha256(path)}  {path.relative_to(root).as_posix()}"
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "checksums.sha256"
    ]
    (root / "checksums.sha256").write_text("\n".join(entries) + "\n", encoding="ascii")


def verify(root):
    root = Path(root)
    seen = set()
    for line in (root / "checksums.sha256").read_text(encoding="ascii").splitlines():
        expected, name = line.split("  ", 1)
        if name in seen or sha256(safe_path(root, name)) != expected:
            raise ValueError("snapshot checksum mismatch")
        seen.add(name)
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and p.name != "checksums.sha256"}
    if seen != actual or "manifest.json" not in seen:
        raise ValueError("snapshot inventory mismatch")
    return json.loads((root / "manifest.json").read_text(encoding="utf-8"))


class TokenSplitter:
    version = "offset-preserving-token-split-v1"

    def __init__(self, path, max_tokens):
        from tokenizers import Tokenizer

        self.tokenizer = Tokenizer.from_file(str(path))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        self.identity = {
            "sha256": sha256(path),
            "version": self.version,
            "max_tokens": max_tokens,
            "tokenizers_version": version("tokenizers"),
        }
        self.max_tokens = max_tokens

    def count(self, text):
        return len(self.tokenizer.encode(text, add_special_tokens=False).ids)

    def split(self, text):
        start = 0
        while start < len(text):
            # Token offsets propose a boundary; recount the exact emitted text.
            encoding = self.tokenizer.encode(text[start:], add_special_tokens=False)
            if len(encoding.ids) <= self.max_tokens:
                end = len(text)
            else:
                end = start + encoding.offsets[self.max_tokens - 1][1]
                if end <= start:
                    end = min(len(text), start + self.max_tokens)
                while end > start and self.count(text[start:end]) > self.max_tokens:
                    end -= 1
                if end == start:
                    raise ValueError("tokenizer cannot encode one character within the token budget")
            value = text[start:end]
            if value.strip():
                yield {"text": value, "char_start": start, "char_end": end, "token_count": self.count(value)}
            start = end


class FlywheelCorpusBuilder:
    def __init__(self, root, store, splitter, *, validation_fraction=0.05):
        self.root, self.store, self.splitter = Path(root), store, splitter
        if not 0 <= validation_fraction < 1:
            raise ValueError("validation fraction must be in [0,1)")
        self.fraction = validation_fraction
        self.recipe_uid = digest(
            {"tokenizer": splitter.identity, "validation_fraction": validation_fraction, "redaction_version": "direct-contact-redaction-v1"}
        )

    def build(self, dataset_id, *, release):
        if not dataset_id or Path(dataset_id).name != dataset_id or dataset_id in {".", ".."}:
            raise ValueError("invalid dataset id")
        destination = self.root / "training" / "pretrain" / dataset_id
        if destination.exists():
            verify(destination)
            self._index(destination)  # recover directory rename before SQLite commit
            return {"status": "recovered", "dataset_id": dataset_id, "path": str(destination)}
        release_path = safe_path(self.root, release["path"])
        release_manifest = verify(release_path)
        if (
            release_manifest.get("format_version") != "financial-document-delivery-v7"
            or sha256(release_path / "manifest.json") != release["manifest_sha256"]
        ):
            raise ValueError("training input release identity mismatch")
        rows = [row for row in read_jsonl(release_path / "training-candidates.jsonl") if row["status"] == "accepted"]
        artifacts = {row["artifact_uid"]: row for row in read_jsonl(release_path / "artifacts.jsonl")}
        corpus, provenance, excluded = [], [], []
        known = {
            row[0]: dict(row)
            for row in self.store.connection.execute("SELECT * FROM training_sample WHERE recipe_uid=?", (self.recipe_uid,))
        }
        prepared, parent, content_works = [], {}, {}

        def find(work):
            parent.setdefault(work, work)
            if parent[work] != work:
                parent[work] = find(parent[work])
            return parent[work]

        for row in rows:
            candidate, decision = json.loads(row["data_json"]), json.loads(row["decision_json"])
            original = safe_path(release_path, artifacts[candidate["artifact_uid"]]["path"])
            if sha256(original) != candidate["sha256"]:
                raise ValueError("accepted candidate content changed")
            record = json.loads(original.read_text(encoding="utf-8"))
            text, redactions = redact_direct_contacts(record["text"])
            segments = list(self.splitter.split(text))
            work = candidate["work_uid"]
            find(work)
            for segment in segments:
                content_hash = hashlib.sha256(segment["text"].encode()).hexdigest()
                if content_hash in content_works:
                    parent[find(work)] = find(content_works[content_hash])
                content_works[content_hash] = work
            prepared.append((candidate, decision, segments, redactions))
        groups, constraints = {}, {}
        for work in parent:
            group = find(work)
            groups.setdefault(group, []).append(work)
            previous_assignment = self.store.get("work-split", stable_uid(self.recipe_uid, work))
            if previous_assignment:
                constraints.setdefault(group, set()).add(previous_assignment["split"])
        for content_hash, work in content_works.items():
            if content_hash in known:
                constraints.setdefault(find(work), set()).add(known[content_hash]["split"])
        assignments = {}
        for group, works in groups.items():
            choices = constraints.get(group, set())
            assignments[group] = (
                next(iter(choices))
                if len(choices) == 1
                else None
                if len(choices) > 1
                else "validation"
                if int(stable_uid(min(works), length=8), 16) / 0xFFFFFFFF < self.fraction
                else "train"
            )
        for candidate, decision, segments, redactions in prepared:
            proposed_split = assignments[find(candidate["work_uid"])]
            if proposed_split is None:
                self.store.put(
                    "split-conflict",
                    candidate["candidate_uid"],
                    {
                        "candidate_uid": candidate["candidate_uid"],
                        "work_uid": candidate["work_uid"],
                        "reason": "logical_work_connects_existing_train_and_validation_groups",
                        "status": "needs_review",
                        "recipe_uid": self.recipe_uid,
                    },
                )
                continue
            for segment_index, segment in enumerate(segments):
                content_hash = hashlib.sha256(segment["text"].encode()).hexdigest()
                sample_uid = "pretrain-" + stable_uid(content_hash, self.recipe_uid)
                previous = known.get(content_hash)
                split = previous["split"] if previous else proposed_split
                origin = {
                    "sample_uid": sample_uid,
                    "content_hash": content_hash,
                    "candidate_uid": candidate["candidate_uid"],
                    "work_uid": candidate["work_uid"],
                    "artifact_uid": candidate["artifact_uid"],
                    "segment": segment_index,
                    "method": candidate["method"],
                    "source": candidate["source"],
                    "decision": decision,
                    "tokenizer": self.splitter.identity,
                    "recipe_uid": self.recipe_uid,
                    "source_offsets": candidate.get("source_offsets"),
                    "source_chunk_versions": candidate.get("source_chunk_versions", []),
                    "visual_asset_uid": (candidate.get("asset") or {}).get("visual_asset_uid"),
                    "description_uid": candidate.get("description_uid"),
                    "redaction": redactions,
                    "char_start": segment["char_start"],
                    "char_end": segment["char_end"],
                    "offset_space": "redacted_candidate",
                    "split": split,
                    "requested_split": proposed_split,
                    "token_count": segment["token_count"],
                    "first_dataset": previous["dataset_id"] if previous else dataset_id,
                }
                if self.store.connection.execute(
                    "SELECT 1 FROM training_origin WHERE sample_uid=? AND candidate_uid=? AND segment=?",
                    (sample_uid, candidate["candidate_uid"], segment_index),
                ).fetchone():
                    continue
                if previous:
                    excluded.append({**origin, "reason": "duplicate_training_text", "first_dataset": previous["dataset_id"]})
                else:
                    corpus.append(
                        {
                            "schema_version": "continued-pretraining-sample-v2",
                            "sample_id": sample_uid,
                            "text": segment["text"],
                            "split": split,
                            "token_count": segment["token_count"],
                        }
                    )
                    known[content_hash] = {"sample_uid": sample_uid, "dataset_id": dataset_id, "split": split}
                provenance.append(origin)
        split_conflicts = [r for r in self.store.records("split-conflict") if r.get("recipe_uid") == self.recipe_uid]
        if not provenance:
            return {"status": "no_change", "samples": 0, "split_conflicts": split_conflicts}
        excluded.extend(split_conflicts)
        # No new text: publish lineage only, outside the training-dataset directory.
        schema_version = "continued-pretraining-delta-v2" if corpus else "training-lineage-delta-v1"
        if not corpus:
            destination = self.root / "training" / "origin_deltas" / dataset_id
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".dataset-", dir=destination.parent))
        try:
            jsonl(staging / "corpus.jsonl", corpus)
            jsonl(staging / "provenance.jsonl", provenance)
            jsonl(staging / "excluded.jsonl", excluded)
            write_json(
                staging / "manifest.json",
                {
                    "schema_version": schema_version,
                    "dataset_id": dataset_id,
                    "created_at": utc_now(),
                    "release": release,
                    "tokenizer": self.splitter.identity,
                    "recipe_uid": self.recipe_uid,
                    "samples": len(corpus),
                    "tokens": sum(x["token_count"] for x in corpus),
                    "origins": len(provenance),
                    "split_conflicts": split_conflicts,
                    "split_policy": "logical-work-hash-v1",
                    "version_policy": "append-distinct-approved-content",
                },
            )
            checksums(staging)
            verify(staging)
            os.replace(staging, destination)
            self._index(destination)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return {
            "status": "success" if corpus else "no_change",
            "dataset_id": dataset_id,
            "samples": len(corpus),
            "origins": len(provenance),
            "split_conflicts": split_conflicts,
            "kind": "training" if corpus else "lineage",
            "path": str(destination),
        }

    def _index(self, destination):
        manifest = verify(destination)
        with self.store.connection:
            for origin in read_jsonl(destination / "provenance.jsonl"):
                work_key = stable_uid(origin["recipe_uid"], origin["work_uid"])
                current_assignment = self.store.get("work-split", work_key)
                if current_assignment and current_assignment["split"] != origin["split"]:
                    raise ValueError("logical work changed dataset split")
                # Use this transaction rather than put(), whose context would commit early.
                self.store.connection.execute(
                    "INSERT OR IGNORE INTO flywheel_record VALUES(?,?,?,?)",
                    ("work-split", work_key, json.dumps({"split": origin["split"], "work_uid": origin["work_uid"]}), utc_now()),
                )
                self.store.connection.execute(
                    "INSERT OR IGNORE INTO training_sample VALUES(?,?,?,?,?,?)",
                    (
                        origin["content_hash"],
                        origin["recipe_uid"],
                        origin["sample_uid"],
                        origin.get("first_dataset", manifest["dataset_id"]),
                        origin["split"],
                        utc_now(),
                    ),
                )
                self.store.connection.execute(
                    "INSERT OR IGNORE INTO training_origin VALUES(?,?,?,?)",
                    (origin["sample_uid"], origin["candidate_uid"], origin["segment"], json.dumps(origin, ensure_ascii=False)),
                )


def build_snapshot(root, dataset_id, deltas, *, origin_deltas=()):
    """Explicit delta selection; checksum verification, exact dedupe, all provenance retained."""
    root = Path(root)
    destination = safe_path(root / "training" / "snapshots", dataset_id)
    if destination.exists():
        raise FileExistsError(destination)
    if not deltas:
        raise ValueError("select at least one delta")
    corpus, origins, manifests, excluded = {}, [], [], []
    tokenizer_identity = None
    recipe_uid = None
    for delta in deltas:
        source = safe_path(root / "training" / "pretrain", delta)
        manifest = verify(source)
        if recipe_uid and manifest["recipe_uid"] != recipe_uid:
            raise ValueError("cannot combine different dataset recipes")
        recipe_uid = manifest["recipe_uid"]
        if tokenizer_identity and manifest["tokenizer"] != tokenizer_identity:
            raise ValueError("cannot combine different tokenizer recipes")
        tokenizer_identity = manifest["tokenizer"]
        manifests.append({"dataset_id": delta, "manifest_sha256": sha256(source / "manifest.json")})
        for item in read_jsonl(source / "corpus.jsonl"):
            key = hashlib.sha256(item["text"].encode()).hexdigest()
            if key in corpus and corpus[key]["split"] != item["split"]:
                raise ValueError("duplicate sample crosses train/validation groups")
            corpus.setdefault(key, item)
        origins.extend(read_jsonl(source / "provenance.jsonl"))
        excluded.extend(read_jsonl(source / "excluded.jsonl"))
    for delta in origin_deltas:
        source = safe_path(root / "training" / "origin_deltas", delta)
        manifest = verify(source)
        if (
            manifest["tokenizer"] != tokenizer_identity
            or manifest["recipe_uid"] != recipe_uid
            or manifest["schema_version"] != "training-lineage-delta-v1"
        ):
            raise ValueError("lineage delta recipe mismatch")
        origins.extend(read_jsonl(source / "provenance.jsonl"))
        excluded.extend(read_jsonl(source / "excluded.jsonl"))
    unresolved = {x["sample_uid"] for x in origins} - {x["sample_id"] for x in corpus.values()}
    if unresolved:
        raise ValueError("selected deltas omit original sample datasets referenced by duplicate provenance")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".snapshot-", dir=destination.parent))
    try:
        jsonl(staging / "corpus.jsonl", corpus.values())
        jsonl(staging / "provenance.jsonl", origins)
        jsonl(staging / "excluded.jsonl", excluded)
        write_json(staging / "near-duplicate-report.json", near_duplicate_report(list(corpus.values())))
        write_json(
            staging / "manifest.json",
            {
                "schema_version": "continued-pretraining-snapshot-v2",
                "dataset_id": dataset_id,
                "deltas": manifests,
                "origin_deltas": list(origin_deltas),
                "samples": len(corpus),
                "tokenizer": tokenizer_identity,
                "recipe_uid": recipe_uid,
                "version_policy": "append-distinct-approved-content",
            },
        )
        checksums(staging)
        verify(staging)
        os.replace(staging, destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {"status": "success", "path": str(destination), "samples": len(corpus)}


def near_duplicate_report(samples, *, max_pairs=10000, threshold=0.8):
    """Bounded diagnostic only: character n-gram Jaccard never removes training text."""
    import re

    features = []
    for sample in samples:
        normalized = re.sub(r"\s+", "", sample["text"])[:4000]
        grams = {normalized[index : index + 8] for index in range(0, max(0, len(normalized) - 7), 4)}
        features.append((sample["sample_id"], grams))
    matches, compared, truncated = [], 0, False
    for index, (uid, grams) in enumerate(features):
        for other_uid, other in features[index + 1 :]:
            if compared >= max_pairs:
                truncated = True
                break
            if not grams or not other:
                continue
            compared += 1
            overlap = len(grams & other) / len(grams | other)
            if overlap >= threshold:
                matches.append({"sample_ids": [uid, other_uid], "jaccard": round(overlap, 4)})
        if truncated:
            break
    return {
        "status": "partial" if truncated else "complete",
        "method": "character-8gram-jaccard-stride4",
        "threshold": threshold,
        "compared_pairs": compared,
        "max_pairs": max_pairs,
        "text_prefix_limit": 4000,
        "matches": matches,
        "action": "report_only",
    }
