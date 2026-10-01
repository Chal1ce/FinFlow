"""Stable identifiers used across discovery, raw assets and pipeline runs."""

from __future__ import annotations

import hashlib


def normalize_doi(value: object) -> str | None:
    """Normalize DOI text so different providers share one document identity."""

    if value in (None, ""):
        return None
    normalized = str(value).strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
            break
    normalized = normalized.rstrip(" .;,")
    return normalized or None


def stable_uid(*parts: object, length: int = 32) -> str:
    """Create a deterministic identifier from normalized string parts."""

    payload = "|".join(str(part).strip() for part in parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:length]


def logical_report_uid(
    stock_code: str,
    report_year: int,
    report_type: str,
    *,
    language: str = "zh-CN",
    document_variant: str = "full",
) -> str:
    """Identify the logical report independently of a file version.

    A corrected PDF should keep the same logical report ID while receiving a
    different asset ID and file hash.
    """

    return stable_uid(
        "logical-report",
        stock_code,
        report_year,
        report_type,
        language,
        document_variant,
    )


def source_candidate_uid(source_name: str, source_id: str, source_url: str) -> str:
    """Identify one discovered source record."""

    return stable_uid("source-candidate", source_name, source_id or source_url)


def raw_asset_uid(raw_file_hash: str) -> str:
    """Identify one immutable file asset by its content fingerprint."""

    return stable_uid("raw-asset", raw_file_hash)


def logical_chunk_uid(
    document_uid: str,
    content_type: str,
    title_context: str,
    text_hash: str,
    occurrence: int = 0,
) -> str:
    """Identify a logical content chunk independently of a run or rule version.

    The text hash intentionally participates in the identity: an edited block
    is a new logical chunk, while the same normalized block keeps its ID when
    regenerated under a different governed artifact.
    """

    return stable_uid(
        "logical-chunk",
        document_uid,
        content_type,
        title_context,
        text_hash,
        occurrence,
    )


def chunk_version_uid(governed_uid: str, chunk_uid: str) -> str:
    """Identify one chunk materialization in a governed artifact version."""

    return stable_uid("chunk-version", governed_uid, chunk_uid)


def chunk_enrichment_uid(
    chunk_version_uid_value: str,
    model_version: str,
    prompt_version: str,
) -> str:
    """Identify one LLM enrichment of a materialized chunk.

    The source chunk identity deliberately stays independent from the model.
    Changing a prompt or model creates a new enrichment identity while keeping
    the original normalized chunk and its provenance addressable.
    """

    return stable_uid(
        "chunk-enrichment",
        chunk_version_uid_value,
        model_version,
        prompt_version,
    )


def artifact_uid(artifact_type: str, *parents: object) -> str:
    """Identify a deterministic derived artifact for its logical parents."""

    return stable_uid("artifact", artifact_type, *parents)


def scholarly_work_uid(doi: object = None, *, source_name: str = "", source_id: str = "") -> str:
    """Return a cross-source stable ID for a scholarly work.

    DOI is preferred because Crossref and OpenAlex can then resolve to the
    same logical work.  A provider-specific fallback is used for records that
    do not have a DOI.
    """

    normalized_doi = normalize_doi(doi)
    if normalized_doi:
        return stable_uid("scholarly-work", "doi", normalized_doi)
    return stable_uid("scholarly-work", source_name, source_id)


def scholarly_candidate_uid(source_name: str, source_id: str) -> str:
    """Return a stable ID for one source's scholarly metadata record."""

    return stable_uid("scholarly-candidate", source_name, source_id)
