"""Execution context shared by all pipeline stages."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PipelineContext:
    """Immutable identity and version information for one pipeline run."""

    batch_id: str
    run_id: str
    data_root: Path
    started_at: str
    config_version: str = "collection-v1"
    rule_version: str = "rules-v1"
    model_version: str | None = None

    @classmethod
    def create(
        cls,
        data_root: Path | str,
        *,
        batch_id: str | None = None,
        config_version: str = "collection-v1",
        rule_version: str = "rules-v1",
        model_version: str | None = None,
    ) -> "PipelineContext":
        """Create a run context, optionally continuing an existing batch.

        ``batch_id`` identifies the business batch and may be reused across
        retries or process restarts.  ``run_id`` always identifies one
        concrete execution attempt.
        """

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        resolved_batch_id = str(batch_id or "").strip()
        if not resolved_batch_id:
            resolved_batch_id = f"batch-{timestamp}-{uuid.uuid4().hex[:8]}"
        return cls(
            batch_id=resolved_batch_id,
            run_id=f"run-{timestamp}-{uuid.uuid4().hex[:8]}",
            data_root=Path(data_root),
            started_at=datetime.now(timezone.utc).isoformat(),
            config_version=config_version,
            rule_version=rule_version,
            model_version=model_version,
        )

    def as_mapping(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "run_id": self.run_id,
            "data_root": str(self.data_root),
            "started_at": self.started_at,
            "config_version": self.config_version,
            "rule_version": self.rule_version,
            "model_version": self.model_version,
        }

    def relative_path(self, path: Path | str) -> str:
        """Return a portable data-root-relative path for lineage records."""

        return Path(path).resolve().relative_to(self.data_root.resolve()).as_posix()
