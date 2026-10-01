"""Build traceable continued-pretraining corpora from immutable delivery releases."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from core.logging import get_logger, log_event
from delivery.publisher import LocalDeliveryPublisher


LOGGER = get_logger(__name__)
DATASET_SCHEMA_VERSION = "continued-pretraining-dataset-v1"
CORPUS_SCHEMA_VERSION = "continued-pretraining-sample-v1"
V5_RELEASE_FORMAT = "financial-document-delivery-v5"
V6_RELEASE_FORMAT = "financial-document-delivery-v6"
REDACTION_VERSION = "direct-contact-redaction-v1"
RIGHTS_STATUS = "unknown"
USAGE_SCOPE = "internal-only"
_EMAIL_PATTERN = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?![\w.-])")
_PHONE_PATTERN = re.compile(
    r"(?<!\d)(?:\+?86[-\s]?)?(?:1[3-9](?:[-\s]?\d){9}|0\d{2,3}[-\s]?\d{7,8})(?!\d)"
)


class TrainingDataError(RuntimeError):
    """Raised when a release cannot safely produce a local training dataset."""


@dataclass(frozen=True, order=True)
class ReleaseReference:
    """One immutable delivery release selected as a corpus source."""

    batch_id: str
    release_id: str

    @property
    def value(self) -> str:
        return f"{self.batch_id}/{self.release_id}"

    @classmethod
    def parse(cls, value: str) -> "ReleaseReference":
        parts = str(value).replace("\\", "/").split("/")
        if len(parts) != 2:
            raise ValueError("release must use BATCH_ID/RELEASE_ID")
        return cls(
            batch_id=_safe_component(parts[0], "batch_id"),
            release_id=_safe_component(parts[1], "release_id"),
        )


class PretrainDatasetBuilder:
    """Build an immutable, model-agnostic corpus from verified local releases."""

    def __init__(
        self,
        data_root: Path | str,
        *,
        output_root: Path | str | None = None,
    ) -> None:
        self.data_root = Path(data_root)
        self.output_root = Path(output_root) if output_root is not None else self.data_root / "training" / "pretrain"

    def build(
        self,
        *,
        dataset_id: str,
        releases: Iterable[ReleaseReference | str],
    ) -> dict[str, Any]:
        """Build one non-overwriting training corpus from selected release snapshots."""

        safe_dataset_id = _safe_component(dataset_id, "dataset_id")
        release_refs = self._normalized_releases(releases)
        destination = self.output_root / safe_dataset_id
        if destination.exists():
            raise FileExistsError(f"training dataset already exists: {destination}")

        log_event(
            LOGGER,
            "INFO",
            "pretrain_dataset_started",
            dataset_id=safe_dataset_id,
            release_count=len(release_refs),
            output_path=str(destination),
        )
        staging_path: Path | None = None
        try:
            candidates: list[dict[str, Any]] = []
            source_releases: list[dict[str, Any]] = []
            for release_ref in release_refs:
                records, source_release = self._release_records(release_ref)
                candidates.extend(records)
                source_releases.append(source_release)

            corpus: list[dict[str, str]] = []
            provenance: list[dict[str, Any]] = []
            excluded: list[dict[str, Any]] = []
            seen_hashes: dict[str, str] = {}
            source_fidelity_counts: Counter[str] = Counter()
            quality_status_counts: Counter[str] = Counter()
            redaction_counts: Counter[str] = Counter()

            for candidate in candidates:
                source_fidelity = str(candidate["source_fidelity"])
                source_fidelity_counts[source_fidelity] += 1
                quality_status = str(candidate.get("ocr_status") or "not_available")
                quality_status_counts[quality_status] += 1
                original_text = str(candidate.get("text") or "")
                redacted_text, redactions = redact_direct_contacts(original_text)
                redaction_counts.update(redactions)
                original_hash = _sha256_text(original_text)
                training_hash = _sha256_text(redacted_text)
                base_provenance = {
                    **self._provenance_fields(candidate),
                    "original_text_sha256": original_hash,
                    "training_text_sha256": training_hash,
                    "redaction": redactions,
                }
                if not redacted_text.strip():
                    item = {**base_provenance, "reason": "empty_training_text"}
                    excluded.append(item)
                    provenance.append({**base_provenance, "disposition": "excluded"})
                    continue

                sample_id = f"pretrain-{training_hash[:32]}"
                if training_hash in seen_hashes:
                    item = {
                        **base_provenance,
                        "sample_id": seen_hashes[training_hash],
                        "reason": "duplicate_training_text",
                    }
                    excluded.append(item)
                    provenance.append({**base_provenance, "sample_id": seen_hashes[training_hash], "disposition": "duplicate"})
                    continue

                seen_hashes[training_hash] = sample_id
                corpus.append(
                    {
                        "schema_version": CORPUS_SCHEMA_VERSION,
                        "sample_id": sample_id,
                        "text": redacted_text,
                    }
                )
                provenance.append({**base_provenance, "sample_id": sample_id, "disposition": "included"})

            destination.parent.mkdir(parents=True, exist_ok=True)
            staging_path = Path(tempfile.mkdtemp(prefix=f".{safe_dataset_id}.", dir=destination.parent))
            _write_jsonl(staging_path / "corpus.jsonl", corpus)
            _write_jsonl(staging_path / "provenance.jsonl", provenance)
            _write_jsonl(staging_path / "excluded.jsonl", excluded)
            manifest = {
                "schema_version": DATASET_SCHEMA_VERSION,
                "dataset_id": safe_dataset_id,
                "created_at": _utc_now(),
                "rights_status": RIGHTS_STATUS,
                "usage_scope": USAGE_SCOPE,
                "text_projection": "deterministic_governed_markdown",
                "redaction_version": REDACTION_VERSION,
                "source_releases": source_releases,
                "counts": {
                    "input_documents": len(candidates),
                    "unique_samples": len(corpus),
                    "duplicate_documents": sum(1 for item in excluded if item["reason"] == "duplicate_training_text"),
                    "excluded_documents": len(excluded),
                },
                "source_fidelity_counts": dict(sorted(source_fidelity_counts.items())),
                "ocr_quality_status_counts": dict(sorted(quality_status_counts.items())),
                "redaction_counts": dict(sorted(redaction_counts.items())),
            }
            _write_json(staging_path / "manifest.json", manifest)
            _write_checksums(
                staging_path,
                ("corpus.jsonl", "provenance.jsonl", "excluded.jsonl", "manifest.json"),
            )
            os.replace(staging_path, destination)
            staging_path = None
            result = {
                "status": "success",
                "dataset_id": safe_dataset_id,
                "dataset_path": str(destination),
                **manifest["counts"],
            }
            log_event(LOGGER, "INFO", "pretrain_dataset_finished", **result)
            return result
        except Exception as exc:
            log_event(
                LOGGER,
                "ERROR",
                "pretrain_dataset_failed",
                dataset_id=safe_dataset_id,
                error_msg=f"{type(exc).__name__}: {exc}",
            )
            raise
        finally:
            if staging_path is not None and staging_path.exists():
                shutil.rmtree(staging_path)

    def _normalized_releases(self, releases: Iterable[ReleaseReference | str]) -> list[ReleaseReference]:
        release_refs = {
            value if isinstance(value, ReleaseReference) else ReleaseReference.parse(value)
            for value in releases
        }
        if not release_refs:
            raise ValueError("at least one release is required")
        return sorted(release_refs)

    def _release_records(self, release_ref: ReleaseReference) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        verifier = LocalDeliveryPublisher(self.data_root)
        verification = verifier.verify(release_ref.batch_id, release_ref.release_id)
        if verification["status"] != "success":
            raise TrainingDataError(
                f"release {release_ref.value!r} did not pass checksum verification: {verification['errors']}"
            )
        release_path = self.data_root / "published" / release_ref.batch_id / release_ref.release_id
        manifest = _read_json_object(release_path / "manifest.json")
        format_version = str(manifest.get("format_version") or "")
        source_release = {
            "batch_id": release_ref.batch_id,
            "release_id": release_ref.release_id,
            "format_version": format_version,
            "manifest_sha256": _sha256_file(release_path / "manifest.json"),
        }
        quality_by_asset = self._quality_by_asset(release_path)
        if format_version == V6_RELEASE_FORMAT:
            return self._v6_records(release_path, release_ref, quality_by_asset), source_release
        if format_version == V5_RELEASE_FORMAT:
            return self._v5_records(release_path, release_ref, quality_by_asset), source_release
        raise TrainingDataError(
            f"release {release_ref.value!r} has unsupported format version {format_version!r}"
        )

    @staticmethod
    def _quality_by_asset(release_path: Path) -> dict[str, dict[str, Any]]:
        quality_path = release_path / "ocr-quality.jsonl"
        if not quality_path.is_file():
            return {}
        return {
            str(record.get("asset_uid")): record
            for record in _read_jsonl(quality_path)
            if str(record.get("asset_uid") or "")
        }

    def _v6_records(
        self,
        release_path: Path,
        release_ref: ReleaseReference,
        quality_by_asset: Mapping[str, Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        path = release_path / "governed-documents.jsonl"
        if not path.is_file():
            raise TrainingDataError(f"v6 release {release_ref.value!r} is missing governed-documents.jsonl")
        return [
            self._candidate(record, release_ref, "full_governed_document", quality_by_asset)
            for record in _read_jsonl(path)
        ]

    def _v5_records(
        self,
        release_path: Path,
        release_ref: ReleaseReference,
        quality_by_asset: Mapping[str, Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        metadata_path = release_path / "metadata.jsonl"
        chunks_path = release_path / "chunks.jsonl"
        if not metadata_path.is_file() or not chunks_path.is_file():
            raise TrainingDataError(
                f"v5 release {release_ref.value!r} requires metadata.jsonl and chunks.jsonl"
            )
        documents = {
            str(record.get("governed_uid")): record
            for record in _read_jsonl(metadata_path)
            if str(record.get("governed_uid") or "")
        }
        chunks: dict[str, list[dict[str, Any]]] = {governed_uid: [] for governed_uid in documents}
        for chunk in _read_jsonl(chunks_path):
            governed_uid = str(chunk.get("governed_uid") or "")
            if governed_uid in chunks:
                chunks[governed_uid].append(chunk)

        records: list[dict[str, Any]] = []
        for governed_uid, document in sorted(documents.items()):
            pieces = []
            for chunk in sorted(chunks[governed_uid], key=lambda item: int(item.get("chunk_index", 0))):
                metadata = _as_mapping(chunk.get("metadata"))
                text = str(metadata.get("text") or "")
                if text:
                    pieces.append(text)
            reconstructed = {**document, "text": "\n\n".join(pieces)}
            records.append(
                self._candidate(
                    reconstructed,
                    release_ref,
                    "lossy_chunk_reconstruction",
                    quality_by_asset,
                )
            )
        return records

    @staticmethod
    def _candidate(
        record: Mapping[str, Any],
        release_ref: ReleaseReference,
        source_fidelity: str,
        quality_by_asset: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        asset_uid = str(record.get("asset_uid") or "")
        quality = quality_by_asset.get(asset_uid, {})
        metadata = _as_mapping(record.get("metadata"))
        checks = quality.get("qc_checks") if isinstance(quality, Mapping) else []
        warning_count = sum(
            1
            for check in checks or []
            if isinstance(check, Mapping) and check.get("status") == "warn"
        )
        return {
            "release_batch_id": release_ref.batch_id,
            "release_id": release_ref.release_id,
            "source_fidelity": source_fidelity,
            "governed_uid": record.get("governed_uid"),
            "document_uid": record.get("document_uid"),
            "work_uid": record.get("work_uid"),
            "asset_uid": asset_uid or None,
            "backend": record.get("backend"),
            "rule_version": record.get("rule_version"),
            "document_type": record.get("document_type") or metadata.get("document_type"),
            "title": record.get("title") or metadata.get("title"),
            "source_name": record.get("source_name") or metadata.get("source_name"),
            "source_id": record.get("source_id") or metadata.get("source_id"),
            "ocr_status": quality.get("status") if isinstance(quality, Mapping) else None,
            "ocr_warning_count": warning_count,
            "text": record.get("text"),
        }

    @staticmethod
    def _provenance_fields(candidate: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: candidate.get(key)
            for key in (
                "release_batch_id",
                "release_id",
                "source_fidelity",
                "governed_uid",
                "document_uid",
                "work_uid",
                "asset_uid",
                "backend",
                "rule_version",
                "document_type",
                "title",
                "source_name",
                "source_id",
                "ocr_status",
                "ocr_warning_count",
            )
        }


def redact_direct_contacts(text: str) -> tuple[str, dict[str, int]]:
    """Replace direct contact details without modifying the remaining governed text."""

    without_emails, email_count = _EMAIL_PATTERN.subn("<EMAIL>", text)
    redacted, phone_count = _PHONE_PATTERN.subn("<PHONE>", without_emails)
    return redacted, {"emails": email_count, "phones": phone_count}


def _safe_component(value: str, label: str) -> str:
    text = str(value).strip()
    candidate = Path(text)
    if not text or text in {".", ".."} or candidate.is_absolute() or candidate.name != text:
        raise ValueError(f"{label} must be a single path component")
    return text


def _as_mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise TrainingDataError(f"cannot read {path}: {exc}") from exc
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise TrainingDataError(f"invalid JSONL in {path} at line {line_number}") from exc
        if not isinstance(value, dict):
            raise TrainingDataError(f"JSONL record in {path} at line {line_number} must be an object")
        records.append(value)
    return records


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TrainingDataError(f"cannot read release manifest {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise TrainingDataError(f"release manifest {path} must be a JSON object")
    return value


def _write_jsonl(path: Path, records: Iterable[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(dict(record), ensure_ascii=False, sort_keys=True))
            handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(dict(value), handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _write_checksums(directory: Path, names: Iterable[str]) -> None:
    with (directory / "checksums.sha256").open("w", encoding="ascii", newline="\n") as handle:
        for name in names:
            handle.write(f"{_sha256_file(directory / name)}  {name}\n")
        handle.flush()
        os.fsync(handle.fileno())


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
