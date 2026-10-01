"""Persisted workflow orchestration around the existing local domain modules."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from config import load_config as load_runtime_config
from core.context import PipelineContext
from delivery.publisher import LocalDeliveryPublisher
from ingestion.local_documents import (
    CollectedReportAdopter,
    LocalDocumentOcrRunner,
    LocalPdfImporter,
    OcrProcessor,
)
from processing.financial_metadata import FinancialMetadataRunner
from processing.governance_runner import GovernanceRunner, load_collection_config
from processing.llm_governance import build_governance_model
from qc.ocr_quality import OcrQualityRunner
from storage.state_store import StateStore


class WorkflowDefinitionError(ValueError):
    """Raised when a pipeline definition cannot be executed locally."""


class WorkflowStageError(RuntimeError):
    """Raised when a stage returns an unsuccessful result."""


StageHandler = Callable[[str, str, bool, bool, str | None, Mapping[str, Any]], Mapping[str, Any]]


class LocalWorkflowRunner:
    """Run local pipeline definitions with durable stage-level recovery state."""

    def __init__(
        self,
        *,
        data_root: Path | str | None = None,
        collection_config_path: Path | str | None = None,
        pipeline_config_path: Path | str | None = None,
        stage_handlers: Mapping[str, StageHandler] | None = None,
        ocr_processor: OcrProcessor | None = None,
    ) -> None:
        project_root = Path(__file__).resolve().parents[1]
        self.collection_config_path = Path(
            collection_config_path or project_root / "config" / "collection.json"
        )
        self.pipeline_config_path = Path(
            pipeline_config_path or project_root / "config" / "pipelines.json"
        )
        self.data_root = Path(data_root) if data_root else self._configured_data_root()
        self.definitions = self._load_definitions(self.pipeline_config_path)
        self.ocr_processor = ocr_processor
        handlers: dict[str, StageHandler] = {
            "ingest": self._run_import,
            "adopt_collected": self._run_adopt_collected,
            "ocr": self._run_ocr,
            "ocr_quality": self._run_ocr_quality,
            "metadata": self._run_metadata,
            "govern": self._run_govern,
            "validate": self._run_validate,
            "publish": self._run_publish,
        }
        handlers.update(stage_handlers or {})
        self.stage_handlers = handlers

    def list_pipelines(self) -> list[dict[str, Any]]:
        return [
            {"name": name, **definition}
            for name, definition in sorted(self.definitions.items())
        ]

    def run(
        self,
        pipeline_name: str,
        *,
        batch_id: str | None = None,
        dry_run: bool = False,
        from_stage: str | None = None,
        until_stage: str | None = None,
        force: bool = False,
        llm_backend: str | None = None,
        input_path: Path | str | None = None,
        ocr_backend: str | None = None,
        document_metadata: Mapping[str, Any] | None = None,
        resume_from_run_id: str | None = None,
    ) -> dict[str, Any]:
        definition = self._definition(pipeline_name)

        with StateStore(self.data_root / "state" / "pipeline.db") as store:
            resume_statuses: dict[str, str] = {}
            resolved_batch_id = batch_id
            available_stages = definition["stages"]
            parameters: dict[str, Any] = {}
            if input_path is not None:
                parameters["input_path"] = str(Path(input_path).resolve())
            if ocr_backend is not None:
                parameters["ocr_backend"] = ocr_backend
            if document_metadata is not None:
                parameters["document_metadata"] = dict(document_metadata)
            if resume_from_run_id:
                previous = store.get_workflow_run(resume_from_run_id)
                if previous is None:
                    raise WorkflowDefinitionError(
                        f"workflow run does not exist: {resume_from_run_id}"
                    )
                if previous["pipeline_name"] != pipeline_name:
                    raise WorkflowDefinitionError(
                        "resume pipeline does not match the original workflow run"
                    )
                if batch_id and batch_id != previous["batch_id"]:
                    raise WorkflowDefinitionError(
                        "resume batch_id does not match the original workflow run"
                    )
                resolved_batch_id = previous["batch_id"]
                parameters = {**previous["parameters"], **parameters}
                if from_stage is None and until_stage is None:
                    available_stages = list(previous["stages"])
                resume_statuses = {
                    item["stage_name"]: item["status"]
                    for item in store.get_workflow_stages(resume_from_run_id)
                }
            stages = self._select_stages(available_stages, from_stage, until_stage)
            if not stages:
                raise WorkflowDefinitionError("pipeline selection does not contain any stages")
            if not resolved_batch_id:
                resolved_batch_id = PipelineContext.create(self.data_root).batch_id

            workflow_run_id = store.start_workflow_run(
                batch_id=resolved_batch_id,
                pipeline_name=pipeline_name,
                stages=stages,
                parameters=parameters,
                dry_run=dry_run,
                resume_from_run_id=resume_from_run_id,
            )
            stage_results: list[dict[str, Any]] = []
            for position, stage_name in enumerate(stages):
                store.start_workflow_stage(workflow_run_id, stage_name, position)
                if resume_statuses.get(stage_name) in {"success", "skipped"}:
                    result = {
                        "status": "skipped",
                        "reason": "completed in resumed workflow",
                        "resume_from_run_id": resume_from_run_id,
                    }
                    store.finish_workflow_stage(
                        workflow_run_id, stage_name, "skipped", output=result
                    )
                    stage_results.append({"stage_name": stage_name, **result})
                    continue
                try:
                    handler = self.stage_handlers.get(stage_name)
                    if handler is None:
                        raise WorkflowDefinitionError(
                            f"pipeline stage has no local handler: {stage_name}"
                        )
                    result = dict(
                        handler(
                            resolved_batch_id,
                            workflow_run_id,
                            dry_run,
                            force,
                            llm_backend,
                            parameters,
                        )
                    )
                    if result.get("status") == "skipped":
                        store.finish_workflow_stage(
                            workflow_run_id, stage_name, "skipped", output=result
                        )
                        stage_results.append({"stage_name": stage_name, **result})
                        store.finish_workflow_run(workflow_run_id, "skipped")
                        return {
                            "status": "skipped",
                            "workflow_run_id": workflow_run_id,
                            "pipeline_name": pipeline_name,
                            "batch_id": resolved_batch_id,
                            "dry_run": dry_run,
                            "reason": result.get("reason"),
                            "stages": stage_results,
                        }
                    if result.get("status") not in {None, "success"}:
                        raise WorkflowStageError(
                            f"stage {stage_name} returned status {result.get('status')!r}"
                        )
                    result.setdefault("status", "success")
                    store.finish_workflow_stage(
                        workflow_run_id, stage_name, "success", output=result
                    )
                    stage_results.append({"stage_name": stage_name, **result})
                except Exception as exc:
                    message = f"{type(exc).__name__}: {exc}"
                    store.finish_workflow_stage(
                        workflow_run_id, stage_name, "failed", error_msg=message
                    )
                    store.finish_workflow_run(workflow_run_id, "failed", message)
                    return {
                        "status": "failed",
                        "workflow_run_id": workflow_run_id,
                        "pipeline_name": pipeline_name,
                        "batch_id": resolved_batch_id,
                        "failed_stage": stage_name,
                        "error_msg": message,
                        "stages": stage_results,
                    }

            store.finish_workflow_run(workflow_run_id, "success")
            return {
                "status": "success",
                "workflow_run_id": workflow_run_id,
                "pipeline_name": pipeline_name,
                "batch_id": resolved_batch_id,
                "dry_run": dry_run,
                "stages": stage_results,
            }

    def status(self, workflow_run_id: str) -> dict[str, Any]:
        with StateStore(self.data_root / "state" / "pipeline.db") as store:
            run = store.get_workflow_run(workflow_run_id)
            if run is None:
                raise WorkflowDefinitionError(f"workflow run does not exist: {workflow_run_id}")
            run["stage_runs"] = store.get_workflow_stages(workflow_run_id)
            return run

    def validate(self, batch_id: str) -> dict[str, Any]:
        return LocalDeliveryPublisher(self.data_root).validate(batch_id)

    def publish(self, batch_id: str, release_id: str, *, dry_run: bool = False) -> dict[str, Any]:
        return LocalDeliveryPublisher(self.data_root).publish(
            batch_id, release_id, dry_run=dry_run
        )

    def verify_release(self, batch_id: str, release_id: str) -> dict[str, Any]:
        return LocalDeliveryPublisher(self.data_root).verify(batch_id, release_id)

    def backup_state(self, output_path: Path | str | None = None) -> dict[str, Any]:
        if output_path is None:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            output_path = self.data_root / "backups" / f"pipeline-{timestamp}.sqlite3"
        with StateStore(self.data_root / "state" / "pipeline.db") as store:
            return store.backup_to(output_path)

    def overview(self, *, limit: int = 20) -> dict[str, Any]:
        """Summarize local operational state without a separate monitoring service."""

        with StateStore(self.data_root / "state" / "pipeline.db") as store:
            batches = store.list_batches(limit=limit)
            workflow_runs = store.list_workflow_runs(limit=limit)
            active_or_failed = [
                run for run in workflow_runs if run["status"] in {"failed", "running"}
            ]
            metadata_reviews = store.connection.execute(
                "SELECT COUNT(*) AS count FROM financial_document_metadata "
                "WHERE status = 'needs_review'"
            ).fetchone()["count"]
            ocr_reviews = store.connection.execute(
                "SELECT COUNT(*) AS count FROM ocr_quality WHERE status = 'needs_review'"
            ).fetchone()["count"]
        return {
            "status": "success",
            "data_root": str(self.data_root),
            "recent_batches": batches,
            "attention_workflow_runs": active_or_failed,
            "review_counts": {
                "financial_metadata": int(metadata_reviews),
                "ocr_quality": int(ocr_reviews),
            },
            "latest_releases": self._latest_releases(),
        }

    def list_runs(
        self,
        *,
        limit: int = 20,
        batch_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        with StateStore(self.data_root / "state" / "pipeline.db") as store:
            return store.list_workflow_runs(limit=limit, batch_id=batch_id, status=status)

    def _run_govern(
        self,
        batch_id: str,
        _workflow_run_id: str,
        dry_run: bool,
        force: bool,
        llm_backend: str | None,
        _parameters: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        collection_config = load_collection_config(self.collection_config_path)
        runtime_config = self._runtime_config_for_data_root()
        model = build_governance_model(runtime_config.governance, llm_backend)
        runner = GovernanceRunner(
            self.data_root,
            collection_config,
            runtime_config=runtime_config,
            llm_model=model,
            force=force,
        )
        return runner.run(dry_run=dry_run, batch_id=batch_id)

    def _run_import(
        self,
        batch_id: str,
        _workflow_run_id: str,
        dry_run: bool,
        _force: bool,
        _llm_backend: str | None,
        parameters: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        input_path = parameters.get("input_path")
        if not input_path:
            raise WorkflowDefinitionError(
                "pipeline stage ingest requires --input <pdf-or-directory>"
            )
        metadata = parameters.get("document_metadata") or {}
        if not isinstance(metadata, Mapping):
            raise WorkflowDefinitionError("document_metadata must be a JSON object")
        return LocalPdfImporter(self.data_root).run(
            str(input_path), batch_id=batch_id, metadata=metadata, dry_run=dry_run
        )

    def _run_adopt_collected(
        self,
        batch_id: str,
        _workflow_run_id: str,
        dry_run: bool,
        _force: bool,
        _llm_backend: str | None,
        _parameters: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return CollectedReportAdopter(self.data_root).run(batch_id=batch_id, dry_run=dry_run)

    def _run_ocr(
        self,
        batch_id: str,
        _workflow_run_id: str,
        dry_run: bool,
        force: bool,
        _llm_backend: str | None,
        parameters: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        backend = str(parameters.get("ocr_backend") or "").strip()
        if not backend:
            raise WorkflowDefinitionError(
                "pipeline stage ocr requires --ocr-backend local or cloud"
            )
        if dry_run:
            return {
                "status": "success",
                "dry_run": True,
                "planned_action": f"run {backend} OCR for batch {batch_id}",
            }
        return LocalDocumentOcrRunner(
            self.data_root, processor=self.ocr_processor
        ).run(batch_id=batch_id, backend=backend, dry_run=dry_run, force=force)

    def _run_metadata(
        self,
        batch_id: str,
        _workflow_run_id: str,
        dry_run: bool,
        force: bool,
        _llm_backend: str | None,
        _parameters: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if dry_run:
            return {
                "status": "success",
                "dry_run": True,
                "planned_action": f"normalize financial metadata for batch {batch_id}",
            }
        return FinancialMetadataRunner(self.data_root).run(
            batch_id=batch_id, dry_run=dry_run, force=force
        )

    def _run_ocr_quality(
        self,
        batch_id: str,
        _workflow_run_id: str,
        dry_run: bool,
        force: bool,
        _llm_backend: str | None,
        _parameters: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if dry_run:
            return {
                "status": "success",
                "dry_run": True,
                "planned_action": f"assess OCR quality for batch {batch_id}",
            }
        return OcrQualityRunner(self.data_root).run(
            batch_id=batch_id, dry_run=dry_run, force=force
        )

    def _run_validate(
        self,
        batch_id: str,
        _workflow_run_id: str,
        dry_run: bool,
        _force: bool,
        _llm_backend: str | None,
        _parameters: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if dry_run:
            return {
                "status": "success",
                "dry_run": True,
                "planned_action": f"validate batch {batch_id}",
            }
        return LocalDeliveryPublisher(self.data_root).validate(batch_id)

    def _run_publish(
        self,
        batch_id: str,
        workflow_run_id: str,
        dry_run: bool,
        _force: bool,
        _llm_backend: str | None,
        _parameters: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if dry_run:
            return {
                "status": "success",
                "dry_run": True,
                "planned_action": f"publish batch {batch_id} as {workflow_run_id}",
            }
        return LocalDeliveryPublisher(self.data_root).publish(
            batch_id, workflow_run_id, dry_run=dry_run
        )

    def _configured_data_root(self) -> Path:
        return load_runtime_config().paths.data_root

    def _runtime_config_for_data_root(self):
        runtime = load_runtime_config()
        processed = self.data_root / "processed"
        return replace(
            runtime,
            paths=replace(
                runtime.paths,
                data_root=self.data_root,
                raw_pdfs=self.data_root / "raw_pdfs",
                parsed_md=self.data_root / "parsed_md",
                processed=processed,
                governed=processed / "governed",
                chunks_dir=processed / "chunks",
                chunks_jsonl=processed / "chunks.jsonl",
                manifests=self.data_root / "manifests",
                state_db=self.data_root / "state" / "pipeline.db",
            ),
        )

    def _latest_releases(self) -> list[dict[str, Any]]:
        published_root = self.data_root / "published"
        if not published_root.is_dir():
            return []
        releases: list[dict[str, Any]] = []
        for pointer_path in sorted(published_root.glob("*/latest.json"), reverse=True):
            try:
                pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                releases.append(
                    {
                        "batch_id": pointer_path.parent.name,
                        "status": "invalid_pointer",
                        "path": str(pointer_path),
                    }
                )
                continue
            if isinstance(pointer, Mapping):
                releases.append({**dict(pointer), "status": "success", "path": str(pointer_path)})
        return releases

    def _definition(self, pipeline_name: str) -> dict[str, Any]:
        try:
            return self.definitions[pipeline_name]
        except KeyError as exc:
            choices = ", ".join(sorted(self.definitions))
            raise WorkflowDefinitionError(
                f"unknown pipeline {pipeline_name!r}; available: {choices}"
            ) from exc

    @staticmethod
    def _select_stages(
        stages: list[str], from_stage: str | None, until_stage: str | None
    ) -> list[str]:
        start = 0
        end = len(stages)
        if from_stage:
            if from_stage not in stages:
                raise WorkflowDefinitionError(f"unknown --from-stage: {from_stage}")
            start = stages.index(from_stage)
        if until_stage:
            if until_stage not in stages:
                raise WorkflowDefinitionError(f"unknown --until-stage: {until_stage}")
            end = stages.index(until_stage) + 1
        if start >= end:
            raise WorkflowDefinitionError("--from-stage must not follow --until-stage")
        return stages[start:end]

    @staticmethod
    def _load_definitions(path: Path) -> dict[str, dict[str, Any]]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise WorkflowDefinitionError(f"cannot read pipeline config {path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise WorkflowDefinitionError(f"invalid pipeline config {path}: {exc}") from exc
        pipelines = value.get("pipelines") if isinstance(value, dict) else None
        if not isinstance(pipelines, dict) or not pipelines:
            raise WorkflowDefinitionError("pipeline config must contain a non-empty pipelines object")
        definitions: dict[str, dict[str, Any]] = {}
        for name, item in pipelines.items():
            if not isinstance(name, str) or not isinstance(item, dict):
                raise WorkflowDefinitionError("pipeline names and definitions must be objects")
            stages = item.get("stages")
            if not isinstance(stages, list) or not stages or not all(
                isinstance(stage, str) and stage for stage in stages
            ):
                raise WorkflowDefinitionError(f"pipeline {name!r} must define non-empty stages")
            if len(set(stages)) != len(stages):
                raise WorkflowDefinitionError(f"pipeline {name!r} has duplicate stages")
            definitions[name] = {
                "description": str(item.get("description") or ""),
                "stages": stages,
            }
        return definitions
