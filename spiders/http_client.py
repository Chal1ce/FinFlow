"""Small standard-library HTTP client shared by source adapters and downloader."""

from __future__ import annotations

import json
import os
import ssl
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    import certifi
except ImportError:  # pragma: no cover - exercised only in minimal environments
    certifi = None


def trusted_ssl_context() -> ssl.SSLContext:
    """Build a verified HTTPS context using certifi when available."""

    if certifi is not None:
        return ssl.create_default_context(cafile=certifi.where())
    return ssl.create_default_context()


class HttpClient:
    """Rate-limited HTTP client for public source endpoints.

    Requests are intentionally sequential.  This keeps the first version
    polite to public disclosure sites and makes the cron job predictable.
    """

    def __init__(
        self,
        timeout_seconds: float = 30,
        user_agent: str = "fin-doc-governance/0.1",
        min_interval_seconds: float = 0.5,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent
        self.min_interval_seconds = max(0.0, min_interval_seconds)
        self._last_request_at = 0.0

    def request_bytes(
        self,
        url: str,
        *,
        method: str = "GET",
        form: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> bytes:
        self._wait_for_rate_limit()
        request_headers = {"User-Agent": self.user_agent}
        if headers:
            request_headers.update(headers)
        body = None
        if form is not None:
            body = urlencode({key: str(value) for key, value in form.items()}).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8")
        request = Request(url, data=body, headers=request_headers, method=method.upper())
        with urlopen(request, timeout=self.timeout_seconds, context=trusted_ssl_context()) as response:
            return response.read()

    def post_form(self, url: str, form: Mapping[str, Any], *, referer: str | None = None) -> bytes:
        headers = {"Accept": "application/json, text/javascript, */*; q=0.01"}
        if referer:
            headers["Referer"] = referer
        return self.request_bytes(url, method="POST", form=form, headers=headers)

    def _wait_for_rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        remaining = self.min_interval_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)
        self._last_request_at = time.monotonic()


def parse_json_or_jsonp(payload: bytes) -> dict[str, Any]:
    """Parse JSON and JSONP responses returned by official endpoints."""

    text = payload.decode("utf-8-sig").strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        left = text.find("(")
        right = text.rfind(")")
        if left < 0 or right <= left:
            raise ValueError("response is neither JSON nor JSONP")
        value = json.loads(text[left + 1 : right])
    if not isinstance(value, dict):
        raise ValueError("source response must be a JSON object")
    return value


def write_jsonl_atomic(path: Path, records: list[Mapping[str, Any]]) -> None:
    """Write a generated JSONL artifact atomically."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(dict(record), ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
