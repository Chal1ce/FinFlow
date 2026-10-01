"""Runtime configuration for the financial document governance pipeline.

Business collection targets remain in ``config/collection.json``.  This module
holds environment-specific settings such as paths, PaddleOCR endpoints and
timeouts, so secrets and deployment details do not need to be committed.

If a ``.env`` file exists in the project root or current working directory it is
loaded at import time.  Values already present in the real environment take
precedence and are never overwritten.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        result = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if result <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return result


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        result = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if result < 1:
        raise ValueError(f"{name} must be at least one")
    return result


def _env_json_object(name: str, default: dict[str, Any]) -> dict[str, Any]:
    value = os.getenv(name)
    if value is None or not value.strip():
        return dict(default)
    try:
        result = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must contain a valid JSON object") from exc
    if not isinstance(result, dict):
        raise ValueError(f"{name} must contain a JSON object")
    return result


def _env_json_list(name: str, default: list[str]) -> list[str]:
    value = os.getenv(name)
    if value is None or not value.strip():
        return list(default)
    try:
        result = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must contain a valid JSON array") from exc
    if not isinstance(result, list) or not all(isinstance(item, str) for item in result):
        raise ValueError(f"{name} must contain a JSON array of strings")
    return result


def _configured_path(name: str, default: Path) -> Path:
    value = os.getenv(name)
    return Path(value).expanduser() if value and value.strip() else default


def _strip_inline_comment(value: str) -> str:
    quote: str | None = None
    for index, character in enumerate(value):
        if character in {"'", '"'}:
            quote = None if quote == character else character
        elif character == "#" and quote is None:
            return value[:index]
    return value


def _load_dotenv(path: Path | str | None = None) -> None:
    """Load KEY=VALUE pairs from a .env file without overriding the shell env."""

    if path is None:
        candidates = [Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"]
        env_path = next((candidate for candidate in candidates if candidate.exists()), None)
    else:
        env_path = Path(path)
    if env_path is None or not env_path.exists():
        return

    with env_path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :].lstrip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key or key in os.environ:
                continue
            value = _strip_inline_comment(value).strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            os.environ[key] = value


@dataclass(frozen=True)
class PathsConfig:
    """Canonical locations for raw and derived pipeline artifacts."""

    data_root: Path
    raw_pdfs: Path
    parsed_md: Path
    processed: Path
    governed: Path
    chunks_dir: Path
    chunks_jsonl: Path
    manifests: Path
    state_db: Path


@dataclass(frozen=True)
class LocalPaddleConfig:
    """Settings for a self-hosted PaddleX Basic Serving endpoint."""

    api_url: str
    token: str | None
    timeout_seconds: float
    page_batch_size: int
    optional_payload: dict[str, Any]
    user_agent: str


@dataclass(frozen=True)
class CloudPaddleConfig:
    """Settings for the official asynchronous PaddleOCR cloud API."""

    job_url: str
    token: str | None
    model: str
    poll_interval_seconds: float
    timeout_seconds: float
    submit_retry_attempts: int
    submit_retry_backoff_seconds: float
    optional_payload: dict[str, Any]
    user_agent: str


@dataclass(frozen=True)
class GovernanceConfig:
    """Settings for deterministic cleaning, semantic chunking and LLM governance."""

    rule_version: str
    drop_block_labels: tuple[str, ...]
    chunk_max_chars: int
    chunk_min_chars: int
    llm_backend: str
    llm_api_url: str | None
    llm_api_key: str | None
    llm_model: str
    llm_timeout_seconds: float
    llm_retry_attempts: int


@dataclass(frozen=True)
class AppConfig:
    paths: PathsConfig
    local_paddle: LocalPaddleConfig
    cloud_paddle: CloudPaddleConfig
    governance: GovernanceConfig


def load_config() -> AppConfig:
    """Load runtime configuration from environment variables.

    The cloud token is intentionally never given a default value.  The token
    pasted into a shell or chat must not be copied into source code.
    """

    data_root = _configured_path("FIN_DOC_DATA_ROOT", Path("data"))
    processed = _configured_path("FIN_DOC_PROCESSED", data_root / "processed")
    paths = PathsConfig(
        data_root=data_root,
        raw_pdfs=_configured_path("FIN_DOC_RAW_PDFS", data_root / "raw_pdfs"),
        parsed_md=_configured_path("FIN_DOC_PARSED_MD", data_root / "parsed_md"),
        processed=processed,
        governed=_configured_path("FIN_DOC_GOVERNED", processed / "governed"),
        chunks_dir=_configured_path("FIN_DOC_CHUNKS_DIR", processed / "chunks"),
        chunks_jsonl=_configured_path("FIN_DOC_CHUNKS_JSONL", processed / "chunks.jsonl"),
        manifests=_configured_path("FIN_DOC_MANIFESTS", data_root / "manifests"),
        state_db=_configured_path("FIN_DOC_STATE_DB", data_root / "state" / "pipeline.db"),
    )

    llm_api_url = os.getenv("LLM_API_URL")
    llm_api_key = os.getenv("LLM_API_KEY")
    governance_config = GovernanceConfig(
        rule_version=os.getenv("FIN_DOC_RULE_VERSION", "ocr-cleaning-v3"),
        drop_block_labels=tuple(
            _env_json_list(
                "FIN_DOC_DROP_BLOCK_LABELS",
                [
                    "header",
                    "footer",
                    "number",
                    "image",
                    "chart",
                    "vision_footnote",
                ],
            )
        ),
        chunk_max_chars=_env_int("FIN_DOC_CHUNK_MAX_CHARS", 2000),
        chunk_min_chars=_env_int("FIN_DOC_CHUNK_MIN_CHARS", 80),
        llm_backend=os.getenv("LLM_BACKEND", "auto"),
        llm_api_url=llm_api_url,
        llm_api_key=llm_api_key,
        llm_model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
        llm_timeout_seconds=_env_float("LLM_TIMEOUT_SECONDS", 60),
        llm_retry_attempts=_env_int("LLM_RETRY_ATTEMPTS", 2),
    )

    local_api_url = os.getenv("PADDLEOCR_LOCAL_API_URL")
    if local_api_url is None:
        # Keep compatibility with the first version of the local client.
        local_api_url = os.getenv("PADDLEOCR_API_URL", "")
    local_token = os.getenv("PADDLEOCR_LOCAL_TOKEN")
    if local_token is None:
        local_token = os.getenv("PADDLEOCR_TOKEN")

    cloud_token = os.getenv("PADDLEOCR_CLOUD_TOKEN")
    cloud_optional_payload = _env_json_object(
        "PADDLEOCR_CLOUD_OPTIONAL_PAYLOAD",
        {
            "useDocOrientationClassify": False,
            "useDocUnwarping": False,
            "useChartRecognition": False,
        },
    )
    local_optional_payload = _env_json_object(
        "PADDLEOCR_LOCAL_OPTIONS",
        {
            "useDocOrientationClassify": True,
            "useDocUnwarping": True,
            "useTextlineOrientation": True,
            "useTableRecognition": True,
            "useFormulaRecognition": True,
            "useChartRecognition": True,
        },
    )

    return AppConfig(
        paths=paths,
        local_paddle=LocalPaddleConfig(
            api_url=local_api_url.rstrip("/"),
            token=local_token,
            timeout_seconds=_env_float("PADDLEOCR_LOCAL_TIMEOUT_SECONDS", 900),
            page_batch_size=_env_int("PADDLEOCR_LOCAL_PAGE_BATCH_SIZE", 20),
            optional_payload=local_optional_payload,
            user_agent=os.getenv("FIN_DOC_USER_AGENT", "fin-doc-governance/0.1"),
        ),
        cloud_paddle=CloudPaddleConfig(
            job_url=os.getenv(
                "PADDLEOCR_CLOUD_JOB_URL",
                "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs",
            ).rstrip("/"),
            token=cloud_token,
            model=os.getenv("PADDLEOCR_CLOUD_MODEL", "PaddleOCR-VL-1.6"),
            poll_interval_seconds=_env_float("PADDLEOCR_CLOUD_POLL_INTERVAL_SECONDS", 5),
            timeout_seconds=_env_float("PADDLEOCR_CLOUD_TIMEOUT_SECONDS", 1800),
            submit_retry_attempts=_env_int(
                "PADDLEOCR_CLOUD_SUBMIT_RETRY_ATTEMPTS", 3
            ),
            submit_retry_backoff_seconds=_env_float(
                "PADDLEOCR_CLOUD_SUBMIT_RETRY_BACKOFF_SECONDS", 10
            ),
            optional_payload=cloud_optional_payload,
            user_agent=os.getenv("FIN_DOC_USER_AGENT", "fin-doc-governance/0.1"),
        ),
        governance=governance_config,
    )


_load_dotenv()


__all__ = [
    "AppConfig",
    "CloudPaddleConfig",
    "GovernanceConfig",
    "LocalPaddleConfig",
    "PathsConfig",
    "load_config",
]
