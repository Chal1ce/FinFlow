"""Versioned daily pipeline configuration; credentials stay in environment variables."""

from __future__ import annotations

import hashlib
import json
import os
import math
from urllib.parse import urlsplit
from dataclasses import dataclass
from pathlib import Path

from config import load_config
from training.cpt_methods import TRANSFORMS


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


@dataclass(frozen=True)
class ModelRole:
    role: str
    endpoint: str
    key: str
    model: str
    version: str
    timeout: float
    max_tokens: int

    @classmethod
    def load(cls, role: str) -> "ModelRole":
        if role == "govern":
            runtime = load_config().governance
            return cls(
                role,
                runtime.llm_api_url or "https://api.openai.com/v1",
                runtime.llm_api_key or "",
                runtime.llm_model,
                os.getenv("LLM_MODEL_VERSION", runtime.llm_model),
                runtime.llm_timeout_seconds,
                int(os.getenv("LLM_MAX_TOKENS", "4096")),
            )
        prefix = (
            "FIN_DOC_" + role.upper()
            if role.startswith("sft_")
            else "FIN_DOC_VISION"
            if role == "vision"
            else f"FIN_DOC_PRETRAIN_{role.upper()}"
        )
        return cls(
            role,
            os.getenv(prefix + "_API_URL", ""),
            os.getenv(prefix + "_API_KEY", ""),
            os.getenv(prefix + "_MODEL", ""),
            os.getenv(prefix + "_MODEL_VERSION", ""),
            float(os.getenv(prefix + "_TIMEOUT", "120")),
            int(os.getenv(prefix + "_MAX_TOKENS", "4096")),
        )

    def identity(self) -> dict:
        return {
            "role": self.role,
            "endpoint": self.endpoint,
            "model": self.model,
            "version": self.version or self.model,
            "max_tokens": self.max_tokens,
        }


class FlywheelConfig:
    def __init__(self, path: Path | str, *, data_root: Path | None = None):
        self.runtime = load_config()  # existing dotenv parser; never source .env as shell code
        self.path = Path(path)
        self.policy = json.loads(self.path.read_text(encoding="utf-8"))
        self.root = (data_root or self.runtime.paths.data_root).resolve()
        self.collection = Path(self.policy.get("collection_config", "config/collection.json"))
        if not self.collection.is_absolute():
            self.collection = self.path.resolve().parent.parent / self.collection
        self.backend = os.getenv("FIN_DOC_FLYWHEEL_OCR_BACKEND", self.policy.get("ocr_backend", "local"))
        configured_options = (
            self.runtime.local_paddle.optional_payload if self.backend == "local" else self.runtime.cloud_paddle.optional_payload
        )
        override = json.loads(os.getenv("FIN_DOC_FLYWHEEL_OCR_OPTIONS", "{}"))
        if not isinstance(override, dict):
            raise ValueError("FIN_DOC_FLYWHEEL_OCR_OPTIONS must be an object")
        self.ocr_options = {**configured_options, **self.policy.get("ocr_options", {}), **override}
        self.methods = self.policy.get("methods", ["original", "visual"])
        roles = ["vision", "translate", "rewrite", "review", "govern"]
        if set(self.methods) & TRANSFORMS.keys():
            roles.append("synthesize")
        self.roles = {role: ModelRole.load(role) for role in roles}
        requested_governance = self.policy.get("governance_backend", "none")
        self.governance_backend = (
            ("openai" if self.roles["govern"].key else "none") if requested_governance == "auto" else requested_governance
        )
        self.tokenizer = Path(os.getenv("FIN_DOC_PRETRAIN_TOKENIZER", ""))
        self.max_tokens = int(os.getenv("FIN_DOC_PRETRAIN_MAX_TOKENS", self.policy.get("max_tokens", 2048)))
        self.task_limit = int(os.getenv("FIN_DOC_FLYWHEEL_MAX_TASKS", self.policy.get("max_tasks", 100)))
        self.deadline_seconds = float(os.getenv("FIN_DOC_FLYWHEEL_MAX_SECONDS", self.policy.get("max_seconds", 7200)))
        self.model_limit = int(os.getenv("FIN_DOC_FLYWHEEL_MAX_MODEL_REQUESTS", self.policy.get("max_model_requests", 50)))
        self.sft = None
        sft = self.policy.get("sft", {})
        if not isinstance(sft, dict) or type(sft.get("enabled", False)) is not bool:
            raise ValueError("sft must be an object with boolean enabled")
        if sft.get("enabled"):
            from training.sft_config import SFTConfig

            sft_path = Path(sft.get("config", "config/sft.json"))
            if not sft_path.is_absolute():
                sft_path = self.path.resolve().parent.parent / sft_path
            self.sft = SFTConfig(sft_path)
        self.version = digest(
            {
                "policy": {k: v for k, v in self.policy.items() if k != "sft"},
                "models": {k: v.identity() for k, v in self.roles.items()},
                "backend": self.backend,
                "governance_backend": self.governance_backend,
                "rule_version": self.runtime.governance.rule_version,
                "chunk_rules": [
                    self.runtime.governance.chunk_max_chars,
                    self.runtime.governance.chunk_min_chars,
                    self.runtime.governance.drop_block_labels,
                ],
                "max_tokens": self.max_tokens,
                "tokenizer_sha256": hashlib.sha256(self.tokenizer.read_bytes()).hexdigest() if self.tokenizer.is_file() else None,
                "ocr_options": self.ocr_options,
            }
        )

    def preflight(self) -> dict:
        errors = []
        if self.sft is not None:
            errors.extend(self.sft.preflight()["errors"])
        if self.backend not in {"local", "cloud"}:
            errors.append("OCR backend must be local or cloud")
        elif self.backend == "local" and not self.runtime.local_paddle.api_url:
            errors.append("PADDLEOCR_LOCAL_API_URL is required")
        elif self.backend == "cloud" and not self.runtime.cloud_paddle.token:
            errors.append("PADDLEOCR cloud token is required")
        required = {"review"} | ({"vision"} if "visual" in self.methods else set())
        required |= set(self.methods) & {"translate", "rewrite"}
        if set(self.methods) & TRANSFORMS.keys():
            required.add("synthesize")
        if self.governance_backend == "openai":
            required.add("govern")
        elif self.governance_backend != "none":
            errors.append("governance_backend must be auto/openai/none; mock is not allowed")
        for role in required:
            model = self.roles[role]
            if not model.endpoint or not model.key or not model.model:
                errors.append(f"{role}: API_URL, API_KEY and MODEL are required")
            endpoint = urlsplit(model.endpoint)
            if (
                endpoint.scheme not in {"http", "https"}
                or not endpoint.hostname
                or endpoint.username
                or endpoint.password
                or endpoint.query
            ):
                errors.append(f"{role}: API_URL must be an HTTP base URL without credentials/query parameters")
            if model.timeout <= 0 or not math.isfinite(model.timeout) or model.max_tokens <= 0:
                errors.append(f"{role}: timeout and max_tokens must be positive")
        if not self.tokenizer.is_file():
            errors.append("FIN_DOC_PRETRAIN_TOKENIZER must point to a tokenizer.json file")
        if (
            self.max_tokens < 32
            or self.task_limit < 1
            or self.deadline_seconds <= 0
            or not math.isfinite(self.deadline_seconds)
            or self.model_limit < 1
        ):
            errors.append("pipeline limits are invalid")
        if not set(self.methods) <= {"original", "visual", "translate", "rewrite", *TRANSFORMS}:
            errors.append("unknown candidate method")
        if not self.methods:
            errors.append("enable at least one candidate method")
        if not 0 <= float(self.policy.get("validation_fraction", 0.05)) < 1:
            errors.append("validation_fraction must be in [0,1)")
        retry_seconds = float(self.policy.get("retry_seconds", 300))
        if int(self.policy.get("max_attempts", 3)) < 1 or retry_seconds < 0 or not math.isfinite(retry_seconds):
            errors.append("retry policy is invalid")
        if not isinstance(self.policy.get("source_policy", {}), dict):
            errors.append("source_policy must be an object")
        for name in ("useDocOrientationClassify", "useDocUnwarping", "useTableRecognition"):
            if name in self.ocr_options and not isinstance(self.ocr_options[name], bool):
                errors.append(f"OCR {name} must be boolean")
        if not self.collection.is_file():
            errors.append("collection config does not exist")
        for package in ("tokenizers", "PIL", "fitz", "openai"):
            try:
                __import__(package)
            except ImportError:
                errors.append(f"missing dependency: {package}")
        if self.tokenizer.is_file():
            try:
                from tokenizers import Tokenizer

                Tokenizer.from_file(str(self.tokenizer))
            except ImportError:
                pass  # already reported in dependency checks
            except Exception:
                errors.append("tokenizer.json cannot be loaded by tokenizers")
        return {
            "status": "failed" if errors else "success",
            "errors": errors,
            "config_version": self.version,
            "data_root": str(self.root),
            "methods": self.methods,
        }
