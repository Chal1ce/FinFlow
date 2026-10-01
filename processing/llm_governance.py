"""Pluggable LLM governance layer for document and chunk enrichment.

The default mock model keeps the pipeline deterministic and free to run
without a key.  ``LLM_API_URL``/``LLM_API_KEY``/``LLM_MODEL`` enable an
OpenAI-compatible chat endpoint for semantic completion when needed. The
OpenAI Python SDK is used for the network backend; runtime settings remain in
environment variables. Documents without table chunks skip LLM enrichment
entirely and retain deterministic title-context projections. When the OpenAI
backend is selected, an LLM request or response failure fails the document
instead of silently substituting a local result.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol

try:
    from openai import OpenAI, OpenAIError
except ImportError:  # pragma: no cover - exercised in minimal environments
    OpenAI = None

    class OpenAIError(Exception):
        """Fallback type used when the optional SDK is not installed."""


STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "that",
    "this",
    "from",
    "are",
    "was",
    "were",
    "has",
    "have",
    "been",
    "their",
    "its",
    "into",
    "which",
    "where",
    "when",
    "while",
    "also",
    "will",
    "can",
    "may",
    "not",
    "but",
    "than",
    "then",
    "such",
    "more",
    "most",
    "through",
    "between",
    "about",
    "after",
    "before",
    "under",
    "over",
}

LLM_PROMPT_VERSION = "llm-governance-v2"


class GovernanceError(RuntimeError):
    """Raised when an LLM request cannot be completed."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def detect_language(text: str) -> str:
    sample = text[:4000]
    cjk_count = sum(1 for character in sample if "\u4e00" <= character <= "\u9fff")
    return "zh" if cjk_count / max(len(sample), 1) > 0.15 else "en"


def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z][A-Za-z\-]{2,}", text)


def _topics(text: str, limit: int = 5) -> list[str]:
    counts: Counter[str] = Counter()
    for word in _words(text):
        normalized = word.lower()
        if len(normalized) >= 4 and normalized not in STOPWORDS:
            counts[normalized] += 1
    return [word.title() for word, _count in counts.most_common(limit)]


def _first_sentences(text: str, max_chars: int = 320) -> str:
    affiliation_pattern = re.compile(
        r"@|corresponding author|e-mail|university|school of|department of|college of",
        re.IGNORECASE,
    )
    body_lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
        and not re.match(r"^#{1,6}\s+", line)
        and not affiliation_pattern.search(line)
    ]
    first_content = next(
        (
            index
            for index, line in enumerate(body_lines)
            if len(line) >= 60 or re.search(r"[.;。；]", line)
        ),
        0,
    )
    body_lines = body_lines[first_content:]
    cleaned = " ".join(body_lines)
    sentences = re.split(r"(?<=[.;。；!?！？])\s+", cleaned)
    summary = ""
    for sentence in sentences:
        candidate = f"{summary} {sentence}".strip()
        if len(candidate) > max_chars and summary:
            break
        summary = candidate
        if len(summary) >= max_chars:
            break
    return summary[:max_chars]


def _quality_score(text: str) -> int:
    words = _words(text)
    if not words:
        return 0
    long_word_ratio = sum(1 for word in words if len(word) >= 18) / len(words)
    return max(0, min(100, round(100 - long_word_ratio * 400)))


def _table_shape(text: str) -> tuple[int, int]:
    rows: list[str] = []
    for line in text.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells and all(re.fullmatch(r":?-{2,}:?", cell) for cell in cells if cell):
            continue
        rows.append(line)
    columns = max((line.count("|") - 1 for line in rows), default=0)
    return len(rows), columns


def _table_summary(text: str) -> str:
    rows, columns = _table_shape(text)
    lines = [line for line in text.splitlines() if line.strip().startswith("|")]
    first_row = "，".join(cell.strip() for cell in lines[0].strip("|").split("|")[:4]) if lines else ""
    return f"表格包含 {rows} 行、{columns} 列；首行内容为：{first_row}。"


def _chunk_key(chunk: Mapping[str, Any]) -> str:
    return str(
        chunk.get("chunk_uid")
        or chunk.get("chunk_version_uid")
        or chunk.get("chunk_id")
        or ""
    )


def _fallback_context_text(chunk: Mapping[str, Any]) -> str:
    """Return deterministic context when contextualization is unavailable."""

    title_context = str(chunk.get("title_context") or "").strip()
    if title_context:
        return f"当前内容位于章节：{title_context}。"
    return ""


def _retrieval_text(
    chunk: Mapping[str, Any], context_text: str, table_text: str | None = None
) -> str:
    """Build derived text for retrieval without replacing source ``text``."""

    body = table_text if chunk.get("content_type") == "table" and table_text else str(
        chunk.get("text") or ""
    )
    return "\n".join(part for part in (context_text.strip(), body.strip()) if part)


def fallback_chunk_enrichment(
    chunk: Mapping[str, Any],
    *,
    status: str = "fallback",
    error: str | None = None,
) -> dict[str, Any]:
    """Create a safe chunk enrichment that never discards OCR content."""

    context_text = _fallback_context_text(chunk)
    result: dict[str, Any] = {
        "context_text": context_text,
        "context_source": "title_context" if context_text else "none",
        "context_status": status,
        "retrieval_text": _retrieval_text(chunk, context_text),
    }
    if chunk.get("content_type") == "table":
        original = str(chunk.get("text") or "")
        result.update(
            {
                "table": original,
                "table_source": "original",
                "table_status": status,
            }
        )
    if error:
        result["llm_error"] = error
    result["llm_status"] = status
    return result


def _skipped_governance(
    model: str,
    model_version: str,
    prompt_version: str,
    chunks: list[Mapping[str, Any]],
    *,
    reason: str = "no_table",
) -> dict[str, Any]:
    """Return deterministic fallbacks when a document has no table to enrich."""

    return {
        "model": model,
        "model_version": model_version,
        "prompt_version": prompt_version,
        "llm_status": "skipped",
        "document_status": "skipped",
        "skip_reason": reason,
        "document": {},
        "chunk_enrichments": {
            _chunk_key(chunk): fallback_chunk_enrichment(chunk, status="skipped")
            for chunk in chunks
        },
        "generated_at": utc_now(),
    }


def _set_retrieval_text(
    chunk: Mapping[str, Any], enrichment: dict[str, Any]
) -> dict[str, Any]:
    """Add the final retrieval projection after table/context enrichment."""

    enrichment["retrieval_text"] = _retrieval_text(
        chunk,
        str(enrichment.get("context_text") or ""),
        str(enrichment.get("table") or "") or None,
    )
    statuses = [str(enrichment.get("context_status") or "fallback")]
    if chunk.get("content_type") == "table":
        statuses.append(str(enrichment.get("table_status") or "fallback"))
    if all(value in {"success", "mock"} for value in statuses):
        enrichment["llm_status"] = "success"
    elif any(value in {"success", "mock"} for value in statuses):
        enrichment["llm_status"] = "partial"
    return enrichment


def _mock_context_text(chunk: Mapping[str, Any], table_text: str | None = None) -> str:
    title_context = str(chunk.get("title_context") or "").strip()
    body = table_text or str(chunk.get("text") or "")
    summary = _first_sentences(body, 180)
    if title_context and summary:
        return f"本段位于“{title_context}”章节，主要内容是：{summary}"
    if title_context:
        return f"本段位于“{title_context}”章节。"
    return summary


class GovernanceModel(Protocol):
    name: str
    version: str

    def govern_document(
        self, document: Mapping[str, Any], chunks: list[Mapping[str, Any]]
    ) -> dict[str, Any]:
        ...


class MockGovernanceModel:
    """Deterministic heuristic model used for tests and keyless runs."""

    name = "mock"
    version = "mock-heuristic-v1"
    prompt_version = LLM_PROMPT_VERSION

    def govern_document(
        self, document: Mapping[str, Any], chunks: list[Mapping[str, Any]]
    ) -> dict[str, Any]:
        if not any(
            chunk.get("content_type") == "table"
            and str(chunk.get("text") or "").strip()
            for chunk in chunks
        ):
            return _skipped_governance(
                self.name, self.version, self.prompt_version, chunks
            )

        text = str(document.get("text") or "")
        title = str(document.get("title") or "").strip()
        language = detect_language(f"{title}\n{text}")
        quality_score = _quality_score(text)
        chunk_enrichments: dict[str, Any] = {}
        for chunk in chunks:
            chunk_text = str(chunk.get("text") or "")
            flags: list[str] = []
            if len(chunk_text) < 40:
                flags.append("short_chunk")
            if _quality_score(chunk_text) < 55:
                flags.append("ocr_noise_suspected")
            enrichment = fallback_chunk_enrichment(chunk, status="success")
            if chunk.get("content_type") == "table":
                table_summary = _table_summary(chunk_text)
                enrichment.update(
                    {
                        "table": table_summary,
                        "table_source": "mock",
                        "table_status": "success",
                        "summary": table_summary,
                    }
                )
            enrichment["context_text"] = _mock_context_text(
                chunk,
                str(enrichment.get("table") or "") or None,
            )
            enrichment["context_source"] = "mock"
            enrichment["context_status"] = "success"
            enrichment["quality_flags"] = flags
            enrichment["quality_score"] = _quality_score(chunk_text)
            chunk_enrichments[_chunk_key(chunk)] = _set_retrieval_text(
                chunk, enrichment
            )
        return {
            "model": self.name,
            "model_version": self.version,
            "prompt_version": self.prompt_version,
            "llm_status": "success",
            "document": {
                "title": title,
                "language": language,
                "topics": _topics(f"{title}\n{text}", 5),
                "summary": _first_sentences(text),
                "quality_score": quality_score,
                "quality_flags": ["empty_document"] if not text else [],
            },
            "chunk_enrichments": chunk_enrichments,
            "generated_at": utc_now(),
        }


class NoneGovernanceModel:
    """No-op model used when LLM governance is explicitly disabled."""

    name = "none"
    version = "none"
    prompt_version = LLM_PROMPT_VERSION

    def govern_document(
        self, document: Mapping[str, Any], chunks: list[Mapping[str, Any]]
    ) -> dict[str, Any]:
        chunk_enrichments = {
            _chunk_key(chunk): fallback_chunk_enrichment(chunk, status="disabled")
            for chunk in chunks
        }
        return {
            "model": self.name,
            "model_version": self.version,
            "prompt_version": self.prompt_version,
            "llm_status": "disabled",
            "document": {},
            "chunk_enrichments": chunk_enrichments,
            "generated_at": utc_now(),
        }


class OpenAIGovernanceModel:
    """OpenAI SDK enrichment client for one governed document.

    Table summaries are generated first.  The document summary and each chunk's
    context bridge then use those summaries as input.  A failed request or an
    invalid response raises ``GovernanceError`` so the caller can mark the
    document and the overall run as failed.
    """

    name = "openai-sdk"
    prompt_version = LLM_PROMPT_VERSION

    def __init__(
        self,
        api_url: str | None,
        api_key: str,
        model: str,
        *,
        timeout_seconds: float = 60,
        retry_attempts: int = 2,
    ) -> None:
        if OpenAI is None:
            raise GovernanceError("openai package is required for the OpenAI backend")
        self.api_url = api_url
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.retry_attempts = max(1, retry_attempts)
        self.version = model
        client_options: dict[str, Any] = {
            "api_key": api_key,
            "timeout": timeout_seconds,
            # The application owns retry semantics and reports final failures.
            "max_retries": 0,
        }
        if self.api_url:
            client_options["base_url"] = self.api_url
        self.client = OpenAI(**client_options)

    def _chat_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(self.retry_attempts):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=0,
                    response_format={"type": "json_object"},
                )
                content = response.choices[0].message.content
                if not content:
                    raise GovernanceError("LLM response content was empty")
                return self._extract_json_object(content)
            except (OpenAIError, GovernanceError, KeyError, ValueError, TypeError) as exc:
                last_error = exc
                if attempt + 1 < self.retry_attempts:
                    time.sleep(min(2**attempt, 10))
        raise GovernanceError(f"LLM request failed: {last_error}")

    @staticmethod
    def _extract_json_object(content: str) -> dict[str, Any]:
        text = content.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        start = text.find("{")
        if start < 0:
            raise GovernanceError("LLM response did not contain a JSON object")
        for end in range(len(text), start, -1):
            candidate = text[start:end].rstrip()
            if not candidate.endswith("}"):
                continue
            try:
                value = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
        raise GovernanceError("LLM response contained invalid JSON")

    def _summarize_table(
        self, chunk: Mapping[str, Any], neighboring_context: str = ""
    ) -> dict[str, Any]:
        text = str(chunk.get("text") or "")[:4000]
        messages = [
            {
                "role": "system",
                "content": (
                    "你是金融文档治理助手。只输出 JSON 对象，字段为 summary 和 "
                    "quality_flags。只能根据输入内容总结，不得编造表格中不存在的事实。"
                ),
            },
            {
                "role": "user",
                "content": (
                    "用一句话总结下面表格，并给出质量问题标记（如 short_table、noisy_ocr）。\n"
                    f"章节上下文：{chunk.get('title_context') or '无'}\n"
                    f"相邻内容：{neighboring_context or '无'}\n\n表格：\n{text}"
                ),
            },
        ]
        result = self._chat_json(messages)
        return {
            "summary": str(result.get("summary", "")),
            "quality_flags": list(result.get("quality_flags", []) or []),
        }

    def _contextualize_chunk(
        self,
        chunk: Mapping[str, Any],
        *,
        document_summary: str,
        table_summary: str = "",
        neighboring_context: str = "",
    ) -> dict[str, Any]:
        text = str(chunk.get("text") or "")[:3000]
        messages = [
            {
                "role": "system",
                "content": (
                    "你是金融文档检索增强助手。只输出 JSON 对象，字段为 "
                    "context_text 和 quality_flags。生成最小必要的上下文桥接，"
                    "只能使用输入信息，不得补造数值、结论、机构或因果关系。"
                ),
            },
            {
                "role": "user",
                "content": (
                    "为当前 chunk 生成一到两句自包含的上下文说明，帮助脱离原文位置后仍能理解。\n"
                    f"文档摘要：{document_summary or '无'}\n"
                    f"章节路径：{chunk.get('title_context') or '无'}\n"
                    f"相邻内容：{neighboring_context or '无'}\n"
                    f"表格摘要：{table_summary or '无'}\n"
                    f"当前 chunk（原文）：\n{text}"
                ),
            },
        ]
        result = self._chat_json(messages)
        return {
            "context_text": str(result.get("context_text", "")).strip(),
            "quality_flags": list(result.get("quality_flags", []) or []),
        }

    @staticmethod
    def _neighboring_context(
        chunks: list[Mapping[str, Any]], index: int, limit: int = 600
    ) -> str:
        neighbors: list[str] = []
        if index > 0:
            previous = str(chunks[index - 1].get("text") or "").strip()
            if previous:
                neighbors.append(f"前一块：{previous[:limit]}")
        if index + 1 < len(chunks):
            following = str(chunks[index + 1].get("text") or "").strip()
            if following:
                neighbors.append(f"后一块：{following[:limit]}")
        return "\n".join(neighbors)

    def govern_document(
        self, document: Mapping[str, Any], chunks: list[Mapping[str, Any]]
    ) -> dict[str, Any]:
        table_chunks = [
            (index, chunk)
            for index, chunk in enumerate(chunks)
            if chunk.get("content_type") == "table"
            and str(chunk.get("text") or "").strip()
        ]
        if not table_chunks:
            return _skipped_governance(
                self.name, self.version, self.prompt_version, chunks
            )

        text = str(document.get("text") or "")[:8000]
        title = str(document.get("title") or "")
        chunk_enrichments: dict[str, Any] = {
            _chunk_key(chunk): fallback_chunk_enrichment(chunk)
            for chunk in chunks
        }

        for index, chunk in table_chunks:
            key = _chunk_key(chunk)
            table_result = self._summarize_table(
                chunk, self._neighboring_context(chunks, index)
            )
            summary = table_result["summary"].strip()
            if not summary:
                raise GovernanceError("table summary was empty")
            enrichment = fallback_chunk_enrichment(chunk, status="success")
            enrichment.update(
                {
                    "table": summary,
                    "table_source": self.name,
                    "table_status": "success",
                    "summary": summary,
                    "quality_flags": list(table_result.get("quality_flags") or []),
                }
            )
            chunk_enrichments[key] = _set_retrieval_text(chunk, enrichment)

        table_summaries = [
            {
                "chunk_uid": _chunk_key(chunk),
                "title_context": chunk.get("title_context"),
                "table": chunk_enrichments[_chunk_key(chunk)].get("table"),
                "table_source": chunk_enrichments[_chunk_key(chunk)].get("table_source"),
            }
            for _index, chunk in table_chunks
            if _chunk_key(chunk) in chunk_enrichments
        ]
        document_messages = [
            {
                "role": "system",
                "content": (
                    "你是金融文档治理助手。只输出 JSON 对象，字段为 title、language、"
                    "topics、summary、quality_score、quality_flags。只能根据输入内容，"
                    "不得编造医学、金融或统计事实。"
                ),
            },
            {
                "role": "user",
                "content": (
                    "请补全和总结以下金融/学术文档元数据。\n"
                    f"文档标题：{title}\n"
                    f"表格摘要：{json.dumps(table_summaries, ensure_ascii=False)}\n"
                    f"文档正文（截断）：\n{text}"
                ),
            },
        ]
        result = self._chat_json(document_messages)
        document_governance = {
            "title": str(result.get("title") or title),
            "language": str(result.get("language") or detect_language(text)),
            "topics": list(result.get("topics") or []),
            "summary": str(result.get("summary") or ""),
            "quality_score": int(result.get("quality_score") or 0),
            "quality_flags": list(result.get("quality_flags") or []),
        }

        document_summary = str(document_governance.get("summary") or "")
        for index, chunk in enumerate(chunks):
            key = _chunk_key(chunk)
            enrichment = dict(chunk_enrichments.get(key) or fallback_chunk_enrichment(chunk))
            table_summary = str(enrichment.get("table") or "")
            context_result = self._contextualize_chunk(
                chunk,
                document_summary=document_summary,
                table_summary=table_summary,
                neighboring_context=self._neighboring_context(chunks, index),
            )
            context_text = context_result["context_text"]
            if not context_text:
                raise GovernanceError("chunk context was empty")
            enrichment.update(
                {
                    "context_text": context_text,
                    "context_source": self.name,
                    "context_status": "success",
                    "quality_flags": list(
                        dict.fromkeys(
                            list(enrichment.get("quality_flags") or [])
                            + list(context_result.get("quality_flags") or [])
                        )
                    ),
                }
            )
            chunk_enrichments[key] = _set_retrieval_text(chunk, enrichment)

        return {
            "model": self.name,
            "model_version": self.version,
            "prompt_version": self.prompt_version,
            "llm_status": "success",
            "document_status": "success",
            "errors": [],
            "document": document_governance,
            "chunk_enrichments": chunk_enrichments,
            "generated_at": utc_now(),
        }


def build_governance_model(
    config: Any,
    backend: str | None = None,
) -> GovernanceModel:
    """Build the configured model; auto prefers OpenAI when credentials exist."""

    backend = (backend or config.llm_backend).lower()
    if backend == "none":
        return NoneGovernanceModel()
    if backend == "mock":
        return MockGovernanceModel()
    if backend == "openai":
        if not config.llm_api_key:
            raise GovernanceError("LLM_API_KEY is required for backend=openai")
        return OpenAIGovernanceModel(
            config.llm_api_url,
            config.llm_api_key,
            config.llm_model,
            timeout_seconds=config.llm_timeout_seconds,
            retry_attempts=config.llm_retry_attempts,
        )
    if backend == "auto":
        if config.llm_api_key:
            return OpenAIGovernanceModel(
                config.llm_api_url,
                config.llm_api_key,
                config.llm_model,
                timeout_seconds=config.llm_timeout_seconds,
                retry_attempts=config.llm_retry_attempts,
            )
        return MockGovernanceModel()
    raise GovernanceError(f"unknown LLM backend: {backend}")


__all__ = [
    "GovernanceError",
    "GovernanceModel",
    "MockGovernanceModel",
    "NoneGovernanceModel",
    "OpenAIGovernanceModel",
    "build_governance_model",
    "detect_language",
]
