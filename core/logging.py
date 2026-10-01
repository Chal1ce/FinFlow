"""Small dependency-free logging helpers for local command-line workflows."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, TextIO


LOGGER_ROOT = "fin_doc_governance"
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
LOG_FORMATS = ("pretty", "json")
LOG_COLORS = ("auto", "always", "never")
DEFAULT_LOG_FILE = Path("logs") / "fin-doc-governance.log"
DEFAULT_LOG_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_LOG_BACKUP_COUNT = 5
_SAFE_FIELD_VALUE = re.compile(r"^[A-Za-z0-9_./:@+-]+$")
_LEVEL_COLORS = {
    "DEBUG": "\033[36m",
    "INFO": "\033[32m",
    "WARNING": "\033[33m",
    "ERROR": "\033[31m",
    "CRITICAL": "\033[31m",
}
_RESET = "\033[0m"


class PrettyFormatter(logging.Formatter):
    """Render compact, grep-friendly log records for interactive terminals."""

    def __init__(self, *, color: bool) -> None:
        super().__init__()
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created).astimezone().isoformat(
            sep=" ", timespec="milliseconds"
        )
        level = f"{record.levelname:<7}"
        if self.color:
            level = f"{_LEVEL_COLORS.get(record.levelname, '')}{level}{_RESET}"
        fields = getattr(record, "event_fields", {})
        suffix = "".join(
            f" {key}={_format_field(value)}"
            for key, value in sorted(fields.items())
            if value is not None
        )
        message = record.getMessage()
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"
        return f"{timestamp} | {level} | {message}{suffix}"


class JsonFormatter(logging.Formatter):
    """Render one JSON object per line for file collection or external parsing."""

    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "event_fields", {})
        payload = {
            "timestamp": datetime.fromtimestamp(record.created).astimezone().isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "event": record.getMessage(),
            **fields,
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def get_logger(name: str) -> logging.Logger:
    """Return a project logger which stays silent until a CLI configures it."""

    root = logging.getLogger(LOGGER_ROOT)
    if not root.handlers:
        root.addHandler(logging.NullHandler())
        root.propagate = False
    suffix = name.rsplit(".", maxsplit=1)[-1]
    return logging.getLogger(f"{LOGGER_ROOT}.{suffix}")


def log_event(
    logger: logging.Logger,
    level: str,
    event: str,
    **fields: Any,
) -> None:
    """Emit one structured event while retaining standard logging semantics."""

    logger.log(getattr(logging, level.upper()), event, extra={"event_fields": fields})


def add_logging_arguments(parser: argparse.ArgumentParser) -> None:
    """Add consistent logging controls to a command-line entrypoint."""

    parser.add_argument(
        "--log-level",
        choices=LOG_LEVELS,
        default=os.getenv("FIN_DOC_LOG_LEVEL", "INFO").upper(),
        help="terminal log threshold; defaults to FIN_DOC_LOG_LEVEL or INFO",
    )
    parser.add_argument(
        "--log-format",
        choices=LOG_FORMATS,
        default=os.getenv("FIN_DOC_LOG_FORMAT", "pretty").lower(),
        help="terminal log format; defaults to FIN_DOC_LOG_FORMAT or pretty",
    )
    parser.add_argument(
        "--log-color",
        choices=LOG_COLORS,
        default=os.getenv("FIN_DOC_LOG_COLOR", "auto").lower(),
        help="ANSI color mode; defaults to FIN_DOC_LOG_COLOR or auto",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=Path(os.getenv("FIN_DOC_LOG_FILE", str(DEFAULT_LOG_FILE))),
        help="rotating local log file; defaults to FIN_DOC_LOG_FILE or logs/fin-doc-governance.log",
    )
    parser.add_argument(
        "--log-max-bytes",
        type=int,
        default=_environment_int("FIN_DOC_LOG_MAX_BYTES", DEFAULT_LOG_MAX_BYTES),
        help="maximum bytes in one log file before rotation",
    )
    parser.add_argument(
        "--log-backup-count",
        type=int,
        default=_environment_int("FIN_DOC_LOG_BACKUP_COUNT", DEFAULT_LOG_BACKUP_COUNT),
        help="number of rotated log files to retain",
    )


def configure_logging(
    *,
    level: str = "INFO",
    log_format: str = "pretty",
    color: str = "auto",
    stream: TextIO | None = None,
    log_file: Path | str | None = DEFAULT_LOG_FILE,
    max_bytes: int = DEFAULT_LOG_MAX_BYTES,
    backup_count: int = DEFAULT_LOG_BACKUP_COUNT,
) -> None:
    """Configure terminal and bounded local logs without touching root logging."""

    normalized_level = level.upper()
    normalized_format = log_format.lower()
    normalized_color = color.lower()
    if normalized_level not in LOG_LEVELS:
        raise ValueError(f"unsupported log level: {level}")
    if normalized_format not in LOG_FORMATS:
        raise ValueError(f"unsupported log format: {log_format}")
    if normalized_color not in LOG_COLORS:
        raise ValueError(f"unsupported log color mode: {color}")
    if max_bytes <= 0:
        raise ValueError("log max_bytes must be positive")
    if backup_count < 0:
        raise ValueError("log backup_count must not be negative")
    output = stream or sys.stderr
    use_color = normalized_color == "always" or (
        normalized_color == "auto"
        and bool(getattr(output, "isatty", lambda: False)())
        and not os.getenv("NO_COLOR")
    )
    formatter: logging.Formatter
    if normalized_format == "json":
        formatter = JsonFormatter()
    else:
        formatter = PrettyFormatter(color=use_color)
    terminal_handler = logging.StreamHandler(output)
    terminal_handler.setFormatter(formatter)
    root = logging.getLogger(LOGGER_ROOT)
    for existing_handler in root.handlers:
        root.removeHandler(existing_handler)
        existing_handler.close()
    root.addHandler(terminal_handler)
    if log_file is not None:
        destination = Path(log_file)
        destination.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            destination,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(
            JsonFormatter() if normalized_format == "json" else PrettyFormatter(color=False)
        )
        root.addHandler(file_handler)
    root.setLevel(getattr(logging, normalized_level))
    root.propagate = False


def configure_logging_from_args(args: argparse.Namespace) -> None:
    """Configure logging from parsers that use :func:`add_logging_arguments`."""

    configure_logging(
        level=str(args.log_level),
        log_format=str(args.log_format),
        color=str(args.log_color),
        log_file=args.log_file,
        max_bytes=int(args.log_max_bytes),
        backup_count=int(args.log_backup_count),
    )


def _format_field(value: Any) -> str:
    if isinstance(value, str):
        compact = value.replace("\n", "\\n")
        return compact if _SAFE_FIELD_VALUE.fullmatch(compact) else json.dumps(compact, ensure_ascii=False)
    if isinstance(value, (int, float, bool)):
        return str(value).lower() if isinstance(value, bool) else str(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _environment_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default
