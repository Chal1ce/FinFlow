"""End-to-end governance runner: cleaning, chunking, LLM enrichment and QC.

The runner discovers every PaddleOCR output under ``parsed_md/scholarly``,
creates a governed Markdown/JSON artifact, builds semantic chunks, optionally
enriches them with an LLM, and records lineage in SQLite.  Re-running without
``--force`` skips documents that already have governed output, so the stage is
safe for cron and resumable after failures.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from config import load_config as load_runtime_config
from core.context import PipelineContext
from core.ids import artifact_uid, chunk_enrichment_uid, stable_uid
from core.logging import add_logging_arguments, configure_logging_from_args, get_logger, log_event
from processing.cleaners import (
    Block,
    clean_blocks,
    drop_reference_section,
    render_governed_markdown,
)
from processing.chunker import chunk_blocks
from processing.llm_governance import (
    GovernanceError,
    GovernanceModel,
    NoneGovernanceModel,
    build_governance_model,
)
from qc.validators import validate_governed_chunk, validate_governed_document
from spiders.collector import RunLock
from spiders.downloader import sha256_file, utc_now
from spiders.http_client import write_jsonl_atomic
from storage.state_store import StateStore


LOGGER = get_logger(__name__)

HTML_TABLE_PATTERN = re.compile(r"<table\b[^>]*>.*?</table\s*>", re.IGNORECASE | re.DOTALL)
MARKDOWN_HEADING_PATTERN = re.compile(r"^\s*#{1,6}\s+\S")
MARKDOWN_TABLE_DIVIDER_PATTERN = re.compile(
    r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*$"
)


def load_collection_config(path: Path | str) -> dict[str, Any]:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("collection config must be a JSON object")
    return value


def load_manifest(data_root: Path) -> dict[str, dict[str, Any]]:
    """Load source manifests for all OCR document types by immutable asset ID."""

    records: dict[str, dict[str, Any]] = {}
    for name in ("scholarly_documents.jsonl", "local_documents.jsonl"):
        manifest_path = data_root / "manifests" / name
        if not manifest_path.exists():
            continue
        with manifest_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                asset_uid = record.get("asset_uid")
                if asset_uid:
                    records[str(asset_uid)] = record
    return records


def _markdown_blocks(text: str, page: int | None) -> list[Block]:
    """Split cloud Markdown into heading, table and paragraph source blocks."""

    blocks: list[Block] = []

    def append(block_label: str, value: str) -> None:
        value = value.strip()
        if not value:
            return
        blocks.append(
            Block(
                page=page,
                block_index=len(blocks),
                block_label=block_label,
                content_type="paragraph",
                text=value,
            )
        )

    def append_non_html(value: str) -> None:
        paragraph_lines: list[str] = []

        def flush_paragraph() -> None:
            nonlocal paragraph_lines
            append("text", "\n".join(paragraph_lines))
            paragraph_lines = []

        lines = value.splitlines()
        index = 0
        while index < len(lines):
            line = lines[index]
            if MARKDOWN_HEADING_PATTERN.match(line):
                flush_paragraph()
                append("paragraph_title", line)
                index += 1
                continue
            if (
                index + 1 < len(lines)
                and "|" in line
                and MARKDOWN_TABLE_DIVIDER_PATTERN.match(lines[index + 1])
            ):
                flush_paragraph()
                table_lines = [line, lines[index + 1]]
                index += 2
                while index < len(lines) and lines[index].strip() and "|" in lines[index]:
                    table_lines.append(lines[index])
                    index += 1
                append("table", "\n".join(table_lines))
                continue
            if not line.strip() and paragraph_lines:
                flush_paragraph()
            elif line.strip():
                paragraph_lines.append(line)
            index += 1
        flush_paragraph()

    cursor = 0
    for match in HTML_TABLE_PATTERN.finditer(text):
        append_non_html(text[cursor : match.start()])
        append("table", match.group(0))
        cursor = match.end()
    append_non_html(text[cursor:])
    return blocks


def load_ocr_pages(parsed_dir: Path) -> list[tuple[int | None, list[Block]]]:
    """Load OCR blocks, including tables embedded in cloud Markdown results."""

    result_path = parsed_dir / "result.json"
    if not result_path.exists():
        page_paths = sorted((parsed_dir / "pages").glob("page_*.md"))
        if page_paths:
            pages: list[tuple[int | None, list[Block]]] = []
            for page_index, page_path in enumerate(page_paths):
                blocks = _markdown_blocks(page_path.read_text(encoding="utf-8"), page_index)
                if blocks:
                    pages.append((page_index, blocks))
            return pages
        text = (parsed_dir / "output.md").read_text(encoding="utf-8")
        return [(None, _markdown_blocks(text, None))]

    raw = json.loads(result_path.read_text(encoding="utf-8"))
    pages: list[tuple[int | None, list[Block]]] = []
    for page_index, page in enumerate(raw.get("layoutParsingResults") or []):
        blocks: list[Block] = []
        pruned = page.get("prunedResult") or {}
        for block_index, block in enumerate(pruned.get("parsing_res_list") or []):
            text = str(block.get("block_content") or "").strip()
            if not text:
                continue
            bbox = tuple(float(value) for value in (block.get("block_bbox") or ()))
            blocks.append(
                Block(
                    page=page_index,
                    block_index=block_index,
                    block_label=str(block.get("block_label") or "text"),
                    content_type="paragraph",
                    text=text,
                    bbox=bbox or None,
                )
            )
        if not blocks:
            markdown = page.get("markdown") or {}
            text = str(markdown.get("text") or "").strip()
            if text:
                blocks.extend(_markdown_blocks(text, page_index))
        if blocks:
            pages.append((page_index, blocks))
    return pages


def discover_documents(
    data_root: Path, manifest: Mapping[str, Mapping[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Discover parsed OCR outputs in stable path order across document types."""

    documents: list[dict[str, Any]] = []
    manifest = manifest or load_manifest(data_root)
    for storage_group in ("scholarly", "financial"):
        parsed_root = data_root / "parsed_md" / storage_group
        if not parsed_root.exists():
            continue
        for output_path in sorted(parsed_root.rglob("output.md")):
            relative = output_path.relative_to(parsed_root).parts
            if len(relative) != 4:
                continue
            work_uid, asset_uid, backend = relative[0], relative[1], relative[2]
            manifest_record = manifest.get(asset_uid, {})
            # The work/document ID is logical, while asset_uid stays tied to
            # one immutable PDF version. Neither is inferred from output paths.
            document_uid = str(
                manifest_record.get("logical_document_uid")
                or manifest_record.get("document_uid")
                or manifest_record.get("work_uid")
                or work_uid
            )
            documents.append(
                {
                    "storage_group": storage_group,
                    "work_uid": work_uid,
                    "asset_uid": asset_uid,
                    "backend": backend,
                    "parsed_dir": output_path.parent,
                    "output_path": output_path,
                    "document_uid": document_uid,
                    "document_type": str(
                        manifest_record.get("document_type")
                        or manifest_record.get("source_type")
                        or storage_group
                    ),
                    "title": str(manifest_record.get("title") or ""),
                    "source_name": str(manifest_record.get("source_name") or ""),
                    "source_id": str(manifest_record.get("source_id") or ""),
                    "manifest_record": dict(manifest_record),
                }
            )
    return documents


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(dict(value), handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


class GovernanceRunner:
    """Process parsed OCR output through the full governance pipeline."""

    def __init__(
        self,
        data_root: Path | str,
        collection_config: Mapping[str, Any],
        *,
        runtime_config: Any | None = None,
        state_store: StateStore | None = None,
        llm_model: GovernanceModel | None = None,
        max_docs: int | None = None,
        force: bool = False,
    ) -> None:
        if max_docs is not None and max_docs < 1:
            raise ValueError("max_docs must be greater than zero")
        self.data_root = Path(data_root)
        self.collection_config = collection_config
        self.runtime_config = runtime_config or load_runtime_config()
        self.governance_config = self.runtime_config.governance
        self.state_store = state_store
        self.llm_model = llm_model or NoneGovernanceModel()
        self.max_docs = max_docs
        self.force = force

    def run(
        self, *, dry_run: bool = False, batch_id: str | None = None,
        documents: list[dict[str, Any]] | None = None, version_identity: str | None = None
    ) -> dict[str, Any]:
        context = PipelineContext.create(
            self.data_root,
            batch_id=batch_id,
            config_version="governance-v1",
            rule_version=self.governance_config.rule_version,
            model_version=self.llm_model.version if self.llm_model else None,
        )
        documents = discover_documents(self.data_root) if documents is None else documents
        if self.max_docs is not None:
            documents = documents[: self.max_docs]

        counts = {"processed": 0, "skipped": 0, "failed": 0, "preview": 0}
        processing_errors: list[dict[str, Any]] = []
        total_chunks = 0
        lock_path = self.data_root / ".governance.lock"
        state_store = self.state_store or StateStore(
            self.runtime_config.paths.state_db
        )
        owns_state_store = self.state_store is None

        with RunLock(lock_path):
            state_store.start_run(context, dry_run=dry_run)
            try:
                for document in documents:
                    governed_uid = stable_uid(
                        "governed",
                        document["work_uid"],
                        document["asset_uid"],
                        document["backend"],
                        self.governance_config.rule_version,
                        *([version_identity] if version_identity else []),
                    )
                    document["governed_uid"] = governed_uid
                    if version_identity:
                        document["version_identity"] = version_identity
                    step_id = state_store.start_step(
                        context,
                        "govern",
                        governed_uid,
                        metadata={
                            "work_uid": document["work_uid"],
                            "asset_uid": document["asset_uid"],
                            "backend": document["backend"],
                            "output_dir": str(self._governed_dir(document)),
                        },
                    )
                    try:
                        result = self._process_document(
                            context, document, state_store, dry_run=dry_run
                        )
                        counts[result["status"]] = counts.get(result["status"], 0) + 1
                        if result["status"] == "processed":
                            total_chunks += int(result["chunk_count"])
                        state_store.finish_step(step_id, "success")
                    except (OSError, ValueError, RuntimeError, GovernanceError) as exc:
                        counts["failed"] += 1
                        processing_errors.append(
                            {
                                "stage": "governance",
                                "work_uid": document["work_uid"],
                                "asset_uid": document["asset_uid"],
                                "backend": document["backend"],
                                "error_msg": f"{type(exc).__name__}: {exc}",
                            }
                        )
                        state_store.finish_step(
                            step_id, "failed", f"{type(exc).__name__}: {exc}"
                        )

                if not dry_run:
                    self._aggregate_chunks(include_versions=bool(version_identity))
                run_status = "failed" if processing_errors else "success"
                summary = {
                    "run_uid": context.run_id,
                    "batch_id": context.batch_id,
                    "started_at": context.started_at,
                    "finished_at": utc_now(),
                    "dry_run": dry_run,
                    "document_count": len(documents),
                    "processed_count": counts["processed"],
                    "skipped_count": counts["skipped"],
                    "failed_count": counts["failed"],
                    "preview_count": counts["preview"],
                    "chunk_count": total_chunks,
                    "llm_backend": self.llm_model.name if self.llm_model else None,
                    "rule_version": self.governance_config.rule_version,
                    "processing_errors": processing_errors,
                    "status": run_status,
                }
                state_store.finish_run(
                    context.run_id,
                    run_status,
                    "one or more documents could not be governed" if processing_errors else None,
                )
                self._write_summary(summary)
                print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
                return summary
            except Exception as exc:
                state_store.finish_run(context.run_id, "failed", f"{type(exc).__name__}: {exc}")
                raise
            finally:
                if owns_state_store:
                    state_store.close()

    def _governed_dir(self, document: Mapping[str, Any]) -> Path:
        if document.get("version_identity"):
            return self.data_root / "processed" / "governed_versions" / str(document["governed_uid"])
        return (
            self.data_root
            / "processed"
            / "governed"
            / str(document.get("storage_group") or "scholarly")
            / document["work_uid"]
            / document["asset_uid"]
            / document["backend"]
        )

    def _chunks_file(self, document: Mapping[str, Any]) -> Path:
        if document.get("version_identity"):
            return self.data_root / "processed" / "chunk_versions" / f"{document['governed_uid']}.jsonl"
        return (
            self.data_root
            / "processed"
            / "chunks"
            / str(document.get("storage_group") or "scholarly")
            / document["work_uid"]
            / f"{document['asset_uid']}.jsonl"
        )

    def _process_document(
        self,
        context: PipelineContext,
        document: Mapping[str, Any],
        state_store: StateStore,
        *,
        dry_run: bool,
    ) -> dict[str, Any]:
        governed_dir = self._governed_dir(document)
        chunks_file = self._chunks_file(document)
        if not dry_run and not self.force:
            if self._is_complete_for_document(governed_dir, document):
                return {"status": "skipped", "chunk_count": 0}
        if not dry_run and self.force:
            # A forced run supersedes the prior successful attempt.  Remove its
            # completion marker before any LLM request so a later failure is
            # retryable without requiring another --force.
            (governed_dir / ".complete").unlink(missing_ok=True)

        pages = load_ocr_pages(Path(document["parsed_dir"]))
        all_blocks: list[Block] = []
        cleaning_stats: dict[str, int] = {}
        cleaning_steps: list[str] = []
        for _page_index, page_blocks in pages:
            result = clean_blocks(
                page_blocks,
                drop_labels=self.governance_config.drop_block_labels,
            )
            all_blocks.extend(result.blocks)
            for key, value in result.stats.items():
                cleaning_stats[key] = cleaning_stats.get(key, 0) + value
            cleaning_steps = result.steps
        reference_result = drop_reference_section(all_blocks)
        all_blocks = reference_result.blocks
        cleaning_stats.update(
            {
                "reference_section_detected": int(reference_result.detected),
                "reference_blocks_removed": reference_result.removed_blocks,
                "reference_chars_removed": reference_result.removed_chars,
            }
        )
        cleaning_stats["kept_blocks"] = len(all_blocks)
        cleaning_stats["char_count"] = sum(len(block.text) for block in all_blocks)
        cleaning_steps = [*cleaning_steps, "drop_reference_section"]
        if not all_blocks:
            raise ValueError("cleaning produced no content blocks")
        all_blocks = [
            replace(block, block_index=index) for index, block in enumerate(all_blocks)
        ]

        governed_md = render_governed_markdown(all_blocks)
        chunks = chunk_blocks(
            all_blocks,
            governed_uid=document["governed_uid"],
            document_uid=document["document_uid"],
            work_uid=document["work_uid"],
            asset_uid=document["asset_uid"],
            backend=document["backend"],
            max_chars=self.governance_config.chunk_max_chars,
            min_chars=self.governance_config.chunk_min_chars,
        )
        if not chunks:
            raise ValueError("chunking produced no chunks")

        chunk_records = [
            {
                **chunk.as_mapping(),
                "run_id": context.run_id,
                "batch_id": context.batch_id,
                "rule_version": self.governance_config.rule_version,
            }
            for chunk in chunks
        ]
        input_path = context.relative_path(Path(document["output_path"]))
        output_path = context.relative_path(governed_dir / "governed.md")
        chunks_path = context.relative_path(chunks_file)
        llm_path = context.relative_path(governed_dir / "llm.json")
        governed_record = {
            "governed_uid": document["governed_uid"],
            "document_uid": document["document_uid"],
            "document_type": document.get("document_type", "scholarly"),
            "work_uid": document["work_uid"],
            "asset_uid": document["asset_uid"],
            "backend": document["backend"],
            "run_id": context.run_id,
            "batch_id": context.batch_id,
            "rule_version": self.governance_config.rule_version,
            "input_path": input_path,
            "output_path": output_path,
            "chunks_path": chunks_path,
            "llm_path": llm_path,
            "title": document["title"],
            "source_name": document["source_name"],
            "source_id": document["source_id"],
            "candidate_uid": document["manifest_record"].get("candidate_uid"),
            "raw_path": document["manifest_record"].get("raw_path"),
            "named_path": document["manifest_record"].get("named_path"),
            "pdf_url": document["manifest_record"].get("pdf_url"),
            "blocks": [block.as_mapping() for block in all_blocks],
            "text": governed_md,
            "char_count": len(governed_md),
            "block_count": len(all_blocks),
            "stats": cleaning_stats,
            "cleaning_steps": cleaning_steps,
        }
        if dry_run:
            return {"status": "preview", "chunk_count": len(chunks)}

        state_store.upsert_document(
            context,
            {
                "document_uid": document["document_uid"],
                "document_type": document.get("document_type", "scholarly"),
                "title": document.get("title"),
                "source_name": document.get("source_name"),
                "source_id": document.get("source_id"),
                "metadata": document.get("manifest_record") or {},
            },
        )

        document_governance = self.llm_model.govern_document(
            {
                "governed_uid": document["governed_uid"],
                "document_uid": document["document_uid"],
                "title": document["title"],
                "source_name": document["source_name"],
                "text": governed_md,
            },
            chunk_records,
        )
        enrichment_by_chunk = document_governance.get("chunk_enrichments") or {}
        llm_model = str(document_governance.get("model") or self.llm_model.name)
        llm_model_version = str(
            document_governance.get("model_version") or self.llm_model.version
        )
        llm_prompt_version = str(
            document_governance.get("prompt_version") or "llm-governance-v1"
        )
        for chunk_record in chunk_records:
            enrichment = enrichment_by_chunk.get(
                chunk_record["chunk_uid"]
            ) or enrichment_by_chunk.get(chunk_record["chunk_version_uid"])
            if isinstance(enrichment, Mapping):
                chunk_record.update(dict(enrichment))
            chunk_record.update(
                {
                    "llm_model": llm_model,
                    "llm_model_version": llm_model_version,
                    "llm_prompt_version": llm_prompt_version,
                    "llm_enrichment_uid": chunk_enrichment_uid(
                        chunk_record["chunk_version_uid"],
                        llm_model_version,
                        llm_prompt_version,
                    ),
                }
            )

        governed_dir.mkdir(parents=True, exist_ok=True)
        (governed_dir / "governed.md").write_text(governed_md, encoding="utf-8")
        _write_json_atomic(governed_dir / "governed.json", governed_record)
        _write_json_atomic(governed_dir / "llm.json", document_governance)
        chunks_file.parent.mkdir(parents=True, exist_ok=True)
        write_jsonl_atomic(chunks_file, chunk_records)

        raw_artifact_id = None
        raw_path_value = document["manifest_record"].get("raw_path")
        if raw_path_value:
            raw_path = self.data_root / str(raw_path_value)
            if raw_path.exists():
                raw_artifact_id = artifact_uid("raw-pdf", document["asset_uid"])
                state_store.upsert_artifact(
                    context,
                    {
                        "artifact_uid": raw_artifact_id,
                        "artifact_type": "raw-pdf",
                        "document_uid": document["document_uid"],
                        "asset_uid": document["asset_uid"],
                        "path": context.relative_path(raw_path),
                        "sha256": document["manifest_record"].get("raw_file_hash"),
                        "metadata": {
                            "source_url": document["manifest_record"].get("pdf_url")
                        },
                    },
                )

        parsed_dir = Path(document["parsed_dir"])
        ocr_source_name = (
            "result.json" if (parsed_dir / "result.json").exists() else "output.md"
        )
        ocr_artifact_id = None
        for ocr_name in ("result.json", "output.md"):
            ocr_path = parsed_dir / ocr_name
            if not ocr_path.exists():
                continue
            current_artifact_id = artifact_uid(
                "ocr-output",
                document["document_uid"],
                document["asset_uid"],
                document["backend"],
                ocr_name,
            )
            state_store.upsert_artifact(
                context,
                {
                    "artifact_uid": current_artifact_id,
                    "artifact_type": "ocr-output",
                    "document_uid": document["document_uid"],
                    "asset_uid": document["asset_uid"],
                    "path": context.relative_path(ocr_path),
                    "sha256": sha256_file(ocr_path),
                    "parent_artifact_uid": raw_artifact_id,
                    "metadata": {
                        "backend": document["backend"],
                        "used_for_governance": ocr_name == ocr_source_name,
                    },
                },
            )
            if ocr_name == ocr_source_name:
                ocr_artifact_id = current_artifact_id

        governed_json_artifact_id = artifact_uid(
            "governed-json", document["governed_uid"], "governed.json"
        )
        for artifact_type, path, artifact_id, parent_id in (
            (
                "governed-markdown",
                governed_dir / "governed.md",
                artifact_uid("governed-markdown", document["governed_uid"], "governed.md"),
                ocr_artifact_id,
            ),
            ("governed-json", governed_dir / "governed.json", governed_json_artifact_id, ocr_artifact_id),
            (
                "llm-result",
                governed_dir / "llm.json",
                artifact_uid(
                    "llm-result",
                    document["governed_uid"],
                    llm_model_version,
                    llm_prompt_version,
                    "llm.json",
                ),
                governed_json_artifact_id,
            ),
            (
                "chunk-jsonl",
                chunks_file,
                artifact_uid(
                    "chunk-jsonl",
                    document["governed_uid"],
                    llm_model_version,
                    llm_prompt_version,
                    chunks_file.name,
                ),
                governed_json_artifact_id,
            ),
        ):
            if path.exists():
                state_store.upsert_artifact(
                    context,
                    {
                        "artifact_uid": artifact_id,
                        "artifact_type": artifact_type,
                        "document_uid": document["document_uid"],
                        "asset_uid": document["asset_uid"],
                        "governed_uid": document["governed_uid"],
                        "path": context.relative_path(path),
                        "sha256": sha256_file(path),
                        "parent_artifact_uid": parent_id,
                        "metadata": {
                            "rule_version": self.governance_config.rule_version,
                            "backend": document["backend"],
                            "llm_model": llm_model,
                            "llm_model_version": llm_model_version,
                            "llm_prompt_version": llm_prompt_version,
                        },
                    },
                )

        state_store.upsert_governed_document(context, governed_record)
        state_store.replace_document_chunks(
            context, document["governed_uid"], chunk_records
        )
        state_store.record_llm_governance(
            context,
            document["governed_uid"],
            "document",
            llm_model,
            status=str(document_governance.get("llm_status") or "success"),
            result=document_governance,
            error_msg=(
                "; ".join(str(item.get("error")) for item in document_governance.get("errors", []))
                or None
            ),
        )
        for chunk_record in chunk_records:
            chunk_result = {
                key: chunk_record[key]
                for key in (
                    "chunk_uid",
                    "chunk_id",
                    "chunk_version_uid",
                    "llm_enrichment_uid",
                    "table",
                    "table_source",
                    "table_status",
                    "context_text",
                    "context_source",
                    "context_status",
                    "retrieval_text",
                    "quality_flags",
                    "quality_score",
                    "llm_model_version",
                    "llm_prompt_version",
                )
                if key in chunk_record
            }
            state_store.record_llm_governance(
                context,
                chunk_record["llm_enrichment_uid"],
                "chunk",
                llm_model,
                status=str(chunk_record.get("llm_status") or "fallback"),
                result=chunk_result,
                error_msg=chunk_record.get("llm_error"),
            )
        state_store.record_qc(
            context,
            document["governed_uid"],
            "governed",
            [check.as_mapping() for check in validate_governed_document(governed_record)],
        )
        for chunk_record in chunk_records:
            state_store.record_qc(
                context,
                chunk_record["chunk_uid"],
                "chunk",
                [check.as_mapping() for check in validate_governed_chunk(chunk_record)],
            )
        _write_json_atomic(
            governed_dir / ".complete",
            {
                "governed_uid": document["governed_uid"],
                "run_id": context.run_id,
                "batch_id": context.batch_id,
                "finished_at": utc_now(),
            },
        )
        return {"status": "processed", "chunk_count": len(chunks)}

    @staticmethod
    def _is_complete_for_document(
        governed_dir: Path, document: Mapping[str, Any]
    ) -> bool:
        """Only skip a result materialized for this exact governed identity."""

        marker_path = governed_dir / ".complete"
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return (
            isinstance(marker, Mapping)
            and marker.get("governed_uid") == document.get("governed_uid")
        )

    def _aggregate_chunks(self, *, include_versions: bool = False) -> None:
        chunk_root = self.data_root / "processed" / "chunks"
        records: list[dict[str, Any]] = []
        paths = list(chunk_root.rglob("*.jsonl"))
        if include_versions:
            paths.extend((self.data_root / "processed" / "chunk_versions").glob("*.jsonl"))
        current_versions = ({r[0]: r[1] for r in self.state_store.connection.execute(
            "SELECT chunk_uid,chunk_version_uid FROM document_chunk")} if include_versions and self.state_store else {})
        for path in sorted(paths):
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        record = json.loads(line)
                        if not current_versions or current_versions.get(record.get("chunk_uid")) == record.get("chunk_version_uid"):
                            records.append(record)
        if include_versions:
            records = list({r.get("chunk_uid", r.get("chunk_id")): r for r in records}.values())
        write_jsonl_atomic(self.runtime_config.paths.chunks_jsonl, records)

    def _write_summary(self, summary: Mapping[str, Any]) -> None:
        path = self.data_root / "runs" / f"{summary['run_uid']}-governance.json"
        _write_json_atomic(path, summary)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Clean, chunk and enrich PaddleOCR scholarly outputs"
    )
    parser.add_argument("--config", type=Path, default=Path("config/collection.json"))
    parser.add_argument("--data-root", type=Path, help="override data_root from config")
    parser.add_argument(
        "--batch-id", help="reuse a business batch ID when resuming a local run"
    )
    parser.add_argument("--dry-run", action="store_true", help="plan without writing")
    parser.add_argument(
        "--force", action="store_true", help="reprocess documents with governed output"
    )
    parser.add_argument("--max-docs", type=int, help="process at most N documents")
    parser.add_argument(
        "--llm-backend",
        choices=("auto", "mock", "openai", "none"),
        default=None,
        help="override LLM backend from .env",
    )
    add_logging_arguments(parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        configure_logging_from_args(args)
    except (OSError, ValueError) as exc:
        print(f"error: {type(exc).__name__}: {exc}")
        return 2
    log_event(
        LOGGER,
        "INFO",
        "command_started",
        command="processing.governance_runner",
        config_path=str(args.config),
        batch_id=args.batch_id,
        dry_run=args.dry_run,
        force=args.force,
    )
    try:
        collection_config = load_collection_config(args.config)
        data_root = args.data_root or Path(collection_config.get("data_root", "data"))
        runtime_config = load_runtime_config()
        llm_model = build_governance_model(
            runtime_config.governance, args.llm_backend
        )
        runner = GovernanceRunner(
            data_root,
            collection_config,
            runtime_config=runtime_config,
            llm_model=llm_model,
            max_docs=args.max_docs,
            force=args.force,
        )
        summary = runner.run(dry_run=args.dry_run, batch_id=args.batch_id)
    except (OSError, ValueError, RuntimeError, GovernanceError) as exc:
        log_event(
            LOGGER,
            "ERROR",
            "command_failed",
            command="processing.governance_runner",
            error_msg=f"{type(exc).__name__}: {exc}",
        )
        print(f"error: {type(exc).__name__}: {exc}")
        return 2
    log_event(
        LOGGER,
        "INFO" if summary["status"] != "failed" else "ERROR",
        "command_finished",
        command="processing.governance_runner",
        batch_id=summary.get("batch_id"),
        status=summary["status"],
    )
    return 1 if summary["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
