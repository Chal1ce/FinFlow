"""Incremental discovery adapters for scholarly literature sources.

This module deliberately collects metadata first.  A paper may have no
downloadable PDF, or its PDF may be restricted by the publisher.  The
candidate therefore keeps both a landing page and an optional open-access PDF
URL for the later document-download stage.
"""

from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from html import unescape
from typing import Any, Iterable, Mapping
from urllib.parse import urlencode

from core.ids import normalize_doi, scholarly_candidate_uid, scholarly_work_uid, stable_uid
from .http_client import HttpClient, parse_json_or_jsonp


class ScholarlyDiscoveryError(RuntimeError):
    """Raised when a scholarly source cannot be converted to candidates."""


HTML_TAG_PATTERN = re.compile(r"<[^>]+>")


def clean_text(value: object) -> str:
    return unescape(HTML_TAG_PATTERN.sub("", str(value or ""))).strip()


def _valid_url(value: object) -> str | None:
    text = str(value or "").strip()
    return text if text.startswith(("http://", "https://")) else None


def _date_parts(value: object) -> str | None:
    if not isinstance(value, list) or not value:
        return None
    try:
        year = int(value[0])
        month = int(value[1]) if len(value) > 1 else 1
        day = int(value[2]) if len(value) > 2 else 1
        return f"{year:04d}-{month:02d}-{day:02d}"
    except (TypeError, ValueError):
        return None


def _date_string(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return text[:10] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text[:10]) else None


def _date_for_filter(updated_since: str | None) -> str | None:
    return _date_string(updated_since)


def _abstract_from_inverted_index(value: object) -> str | None:
    """Rebuild OpenAlex's position-indexed abstract representation."""

    if not isinstance(value, dict):
        return None
    words: list[tuple[int, str]] = []
    for word, positions in value.items():
        if not isinstance(positions, list):
            continue
        for position in positions:
            try:
                words.append((int(position), str(word)))
            except (TypeError, ValueError):
                continue
    if not words:
        return None
    return " ".join(word for _, word in sorted(words, key=lambda item: item[0]))


def _authors_from_crossref(items: object) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    authors: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        authors.append(
            {
                "given": clean_text(item.get("given")),
                "family": clean_text(item.get("family")),
                "name": clean_text(item.get("name")),
                "orcid": _valid_url(item.get("ORCID")),
            }
        )
    return authors


def _authors_from_openalex(items: object) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    authors: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        author = item.get("author") or {}
        if not isinstance(author, dict):
            continue
        authors.append(
            {
                "name": clean_text(author.get("display_name")),
                "orcid": _valid_url(author.get("orcid")),
                "author_id": author.get("id"),
                "author_position": item.get("author_position"),
            }
        )
    return authors


def _metadata_hash(values: Mapping[str, Any]) -> str:
    serialized = json.dumps(dict(values), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return stable_uid("scholarly-metadata", serialized)


@dataclass(frozen=True)
class ScholarlyTarget:
    """One repeatable query definition for a scholarly source."""

    name: str
    query: str
    start_year: int
    end_year: int
    work_types: tuple[str, ...] = ("journal-article",)
    max_results: int = 200

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ScholarlyTarget":
        name = str(value.get("name") or value.get("target_id") or "").strip()
        query = str(value.get("query") or "").strip()
        if not name:
            raise ValueError("scholarly target requires name")
        if not query:
            raise ValueError(f"scholarly target {name!r} requires query")
        try:
            start_year = int(value["start_year"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"scholarly target {name!r} requires integer start_year") from exc
        end_value = value.get("end_year")
        if str(end_value).strip().lower() in {"auto", "current"}:
            try:
                lag = int(value.get("end_year_lag", 0))
            except (TypeError, ValueError) as exc:
                raise ValueError("scholarly end_year_lag must be an integer") from exc
            end_year = datetime.now().year - lag
        else:
            try:
                end_year = int(end_value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"scholarly target {name!r} requires end_year") from exc
        if not 1900 <= start_year <= end_year <= datetime.now().year + 1:
            raise ValueError(f"invalid scholarly year range: {start_year}-{end_year}")

        raw_types = value.get("work_types", ("journal-article",))
        if isinstance(raw_types, str):
            raw_types = [raw_types]
        if not isinstance(raw_types, (list, tuple)):
            raise ValueError("scholarly work_types must be a list or string")
        work_types = tuple(str(item).strip().lower() for item in raw_types if str(item).strip())
        if not work_types:
            raise ValueError("scholarly work_types cannot be empty")
        try:
            max_results = int(value.get("max_results", 200))
        except (TypeError, ValueError) as exc:
            raise ValueError("scholarly max_results must be an integer") from exc
        if not 1 <= max_results <= 5000:
            raise ValueError("scholarly max_results must be between 1 and 5000")
        return cls(name, query, start_year, end_year, work_types, max_results)

    @property
    def target_uid(self) -> str:
        return stable_uid("scholarly-target", self.name, self.query, *self.work_types)

    @property
    def start_date(self) -> str:
        return f"{self.start_year:04d}-01-01"

    @property
    def end_date(self) -> str:
        return f"{self.end_year:04d}-12-31"


@dataclass(frozen=True)
class ScholarlyCandidate:
    source_name: str
    source_id: str
    title: str
    work_type: str
    published_date: str | None
    source_updated_at: str | None
    source_url: str
    pdf_url: str | None
    doi: str | None
    journal_title: str | None
    journal_issn: str | None
    publisher: str | None
    abstract: str | None
    authors: tuple[Mapping[str, Any], ...]
    open_access: bool
    citation_count: int | None
    priority: int
    discovered_at: str

    @property
    def candidate_uid(self) -> str:
        return scholarly_candidate_uid(self.source_name, self.source_id)

    @property
    def work_uid(self) -> str:
        return scholarly_work_uid(
            self.doi, source_name=self.source_name, source_id=self.source_id
        )

    @property
    def metadata_hash(self) -> str:
        return _metadata_hash(
            {
                "title": self.title,
                "work_type": self.work_type,
                "published_date": self.published_date,
                "source_updated_at": self.source_updated_at,
                "source_url": self.source_url,
                "pdf_url": self.pdf_url,
                "doi": self.doi,
                "journal_title": self.journal_title,
                "journal_issn": self.journal_issn,
                "publisher": self.publisher,
                "abstract": self.abstract,
                "authors": list(self.authors),
                "open_access": self.open_access,
                "citation_count": self.citation_count,
            }
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "candidate_uid": self.candidate_uid,
            "work_uid": self.work_uid,
            "source_type": "scholarly",
            "source_name": self.source_name,
            "source_id": self.source_id,
            "title": self.title,
            "work_type": self.work_type,
            "published_date": self.published_date,
            "source_updated_at": self.source_updated_at,
            "source_url": self.source_url,
            "pdf_url": self.pdf_url,
            "doi": self.doi,
            "journal_title": self.journal_title,
            "journal_issn": self.journal_issn,
            "publisher": self.publisher,
            "abstract": self.abstract,
            "authors": list(self.authors),
            "open_access": self.open_access,
            "citation_count": self.citation_count,
            "metadata_hash": self.metadata_hash,
            "source_priority": self.priority,
            "discovered_at": self.discovered_at,
        }


class ScholarlySourceAdapter(ABC):
    name: str
    priority: int

    def __init__(self, http_client: HttpClient, priority: int | None = None) -> None:
        self.http = http_client
        if priority is not None:
            self.priority = priority
        self.next_cursor: str | None = None

    @abstractmethod
    def discover(
        self,
        target: ScholarlyTarget,
        *,
        updated_since: str | None = None,
        cursor: str | None = None,
    ) -> list[ScholarlyCandidate]:
        raise NotImplementedError

    def _get_json(self, url: str, operation: str) -> dict[str, Any]:
        try:
            headers = {"Accept": "application/json"}
            if getattr(self, "api_key", None):
                headers["Authorization"] = "Bearer " + self.api_key
            payload = self.http.request_bytes(
                url, headers=headers
            )
            return parse_json_or_jsonp(payload)
        except (OSError, ValueError) as exc:
            raise ScholarlyDiscoveryError(f"{self.name} {operation} failed: {exc}") from exc


class CrossrefAdapter(ScholarlySourceAdapter):
    """Discover journal metadata from Crossref's public REST API."""

    name = "crossref"
    priority = 30
    endpoint = "https://api.crossref.org/works"

    def __init__(
        self,
        http_client: HttpClient,
        priority: int | None = None,
        *,
        rows: int = 100,
        mailto: str | None = None,
    ) -> None:
        super().__init__(http_client, priority)
        self.rows = max(1, min(int(rows), 1000))
        self.mailto = mailto

    def discover(
        self,
        target: ScholarlyTarget,
        *,
        updated_since: str | None = None,
        cursor: str | None = None,
    ) -> list[ScholarlyCandidate]:
        filter_parts = [
            f"from-pub-date:{target.start_date}",
            f"until-pub-date:{target.end_date}",
        ]
        updated_date = _date_for_filter(updated_since)
        if updated_date:
            filter_parts.append(f"from-index-date:{updated_date}")
        if getattr(self, "updated_until", None):
            filter_parts.append(f"until-index-date:{_date_for_filter(self.updated_until)}")
        if len(target.work_types) == 1 and target.work_types[0] == "journal-article":
            filter_parts.append("type:journal-article")

        current_cursor = cursor or "*"
        candidates: list[ScholarlyCandidate] = []
        self.next_cursor = None
        self.scan_complete = False
        page_requests = 0
        while len(candidates) < target.max_results:
            page_requests += 1
            params: dict[str, Any] = {
                "query.bibliographic": target.query,
                "filter": ",".join(filter_parts),
                "rows": min(self.rows, target.max_results),
                "cursor": current_cursor,
            }
            if self.mailto:
                params["mailto"] = self.mailto
            payload = self._get_json(
                f"{self.endpoint}?{urlencode(params)}", "works query"
            )
            message = payload.get("message")
            if not isinstance(message, dict):
                raise ScholarlyDiscoveryError("Crossref response does not contain message")
            items = message.get("items") or []
            if not isinstance(items, list):
                raise ScholarlyDiscoveryError("Crossref message.items is not a list")
            for item in items:
                if not isinstance(item, dict):
                    continue
                candidate = self._parse_item(item)
                if candidate and _in_target_years(candidate.published_date, target):
                    candidates.append(candidate)
            next_cursor = str(message.get("next-cursor") or "")
            if len(items) < params["rows"] or not next_cursor or next_cursor == current_cursor:
                self.scan_complete = True
                self.next_cursor = None
                break
            current_cursor = next_cursor
            self.next_cursor = current_cursor
            if page_requests >= max(1, (target.max_results + self.rows - 1) // self.rows):
                break
        return deduplicate_scholarly_candidates(candidates)

    def _parse_item(self, item: Mapping[str, Any]) -> ScholarlyCandidate | None:
        title_values = item.get("title") or []
        title = clean_text(title_values[0] if isinstance(title_values, list) and title_values else "")
        doi = normalize_doi(item.get("DOI"))
        source_url = _valid_url(item.get("URL")) or (f"https://doi.org/{doi}" if doi else None)
        source_id = doi or str(item.get("URL") or "").strip()
        if not title or not source_id or not source_url:
            return None
        published_date = _date_parts(
            (item.get("published-online") or item.get("published-print") or item.get("issued") or {}).get(
                "date-parts"
            )
            if isinstance(
                item.get("published-online") or item.get("published-print") or item.get("issued") or {},
                dict,
            )
            else None
        )
        links = item.get("link") or []
        pdf_url = None
        if isinstance(links, list):
            for link in links:
                if isinstance(link, dict) and "pdf" in str(link.get("content-type", "")).lower():
                    pdf_url = _valid_url(link.get("URL"))
                    if pdf_url:
                        break
        licenses = item.get("license") or []
        license_url = None
        if isinstance(licenses, list) and licenses and isinstance(licenses[0], dict):
            license_url = _valid_url(licenses[0].get("URL"))
        return ScholarlyCandidate(
            source_name=self.name,
            source_id=source_id,
            title=title,
            work_type=str(item.get("type") or "journal-article"),
            published_date=published_date,
            source_updated_at=_date_string(
                (item.get("indexed") or {}).get("date-time")
                if isinstance(item.get("indexed"), dict)
                else None
            ),
            source_url=source_url,
            pdf_url=pdf_url,
            doi=doi,
            journal_title=clean_text(
                (item.get("container-title") or [None])[0]
                if isinstance(item.get("container-title"), list)
                else item.get("container-title")
            )
            or None,
            journal_issn=str((item.get("ISSN") or [None])[0] or "") or None
            if isinstance(item.get("ISSN"), list)
            else str(item.get("ISSN") or "") or None,
            publisher=clean_text(item.get("publisher")) or None,
            abstract=clean_text(item.get("abstract")) or None,
            authors=tuple(_authors_from_crossref(item.get("author"))),
            open_access=bool(pdf_url or license_url),
            citation_count=_as_int(item.get("is-referenced-by-count")),
            priority=self.priority,
            discovered_at=datetime.now().astimezone().isoformat(),
        )


class OpenAlexAdapter(ScholarlySourceAdapter):
    """Discover scholarly works and open-access locations from OpenAlex."""

    name = "openalex"
    priority = 40
    endpoint = "https://api.openalex.org/works"

    def __init__(
        self,
        http_client: HttpClient,
        priority: int | None = None,
        *,
        per_page: int = 100,
        mailto: str | None = None,
    ) -> None:
        super().__init__(http_client, priority)
        self.per_page = max(1, min(int(per_page), 100))
        self.mailto = mailto
        self.api_key = os.getenv("OPENALEX_API_KEY")

    def discover(
        self,
        target: ScholarlyTarget,
        *,
        updated_since: str | None = None,
        cursor: str | None = None,
    ) -> list[ScholarlyCandidate]:
        filters = [
            f"from_publication_date:{target.start_date}",
            f"to_publication_date:{target.end_date}",
        ]
        updated_date = _date_for_filter(updated_since)
        if updated_date:
            filters.append(f"from_updated_date:{updated_date}")
        if getattr(self, "updated_until", None):
            filters.append(f"to_updated_date:{_date_for_filter(self.updated_until)}")
        openalex_types = {
            "journal-article": "article",
            "journal_article": "article",
            "working-paper": "article",
        }
        types = [openalex_types.get(value, value) for value in target.work_types]
        if types:
            filters.append("type:" + "|".join(sorted(set(types))))

        current_cursor = cursor or "*"
        candidates: list[ScholarlyCandidate] = []
        self.next_cursor = None
        self.scan_complete = False
        page_requests = 0
        while len(candidates) < target.max_results:
            page_requests += 1
            params: dict[str, Any] = {
                "search": target.query,
                "filter": ",".join(filters),
                "per-page": min(self.per_page, target.max_results),
                "cursor": current_cursor,
            }
            if self.mailto:
                params["mailto"] = self.mailto
            payload = self._get_json(
                f"{self.endpoint}?{urlencode(params)}", "works query"
            )
            items = payload.get("results") or []
            if not isinstance(items, list):
                raise ScholarlyDiscoveryError("OpenAlex results is not a list")
            for item in items:
                if not isinstance(item, dict):
                    continue
                candidate = self._parse_item(item)
                if candidate and _in_target_years(candidate.published_date, target):
                    candidates.append(candidate)
            meta = payload.get("meta") or {}
            next_cursor = str(meta.get("next_cursor") or "") if isinstance(meta, dict) else ""
            if not items or not next_cursor or next_cursor == current_cursor:
                self.scan_complete = True
                self.next_cursor = None
                break
            current_cursor = next_cursor
            self.next_cursor = current_cursor
            if page_requests >= max(1, (target.max_results + self.per_page - 1) // self.per_page):
                break
        return deduplicate_scholarly_candidates(candidates)

    def _parse_item(self, item: Mapping[str, Any]) -> ScholarlyCandidate | None:
        title = clean_text(item.get("title"))
        source_id = str(item.get("id") or "").rstrip("/").rsplit("/", 1)[-1]
        if not title or not source_id:
            return None
        doi = normalize_doi(item.get("doi"))
        primary_location = item.get("primary_location") or {}
        if not isinstance(primary_location, dict):
            primary_location = {}
        primary_source = primary_location.get("source") or {}
        if not isinstance(primary_source, dict):
            primary_source = {}
        best_location = item.get("best_oa_location") or {}
        if not isinstance(best_location, dict):
            best_location = {}
        pdf_url = _valid_url(best_location.get("pdf_url"))
        landing_url = _valid_url(best_location.get("landing_page_url"))
        if not landing_url:
            landing_url = _valid_url(primary_location.get("landing_page_url"))
        if not landing_url:
            landing_url = f"https://doi.org/{doi}" if doi else _valid_url(item.get("id"))
        if not landing_url:
            return None
        issns = primary_source.get("issn") or []
        journal_issn = str(issns[0]) if isinstance(issns, list) and issns else None
        return ScholarlyCandidate(
            source_name=self.name,
            source_id=source_id,
            title=title,
            work_type=str(item.get("type") or "article"),
            published_date=_date_string(item.get("publication_date")),
            source_updated_at=_date_string(item.get("updated_date")),
            source_url=landing_url,
            pdf_url=pdf_url,
            doi=doi,
            journal_title=clean_text(primary_source.get("display_name")) or None,
            journal_issn=journal_issn,
            publisher=clean_text((primary_source.get("host_organization_name"))) or None,
            abstract=_abstract_from_inverted_index(item.get("abstract_inverted_index")),
            authors=tuple(_authors_from_openalex(item.get("authorships"))),
            open_access=bool((item.get("open_access") or {}).get("is_oa") or pdf_url),
            citation_count=_as_int(item.get("cited_by_count")),
            priority=self.priority,
            discovered_at=datetime.now().astimezone().isoformat(),
        )


def _as_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _in_target_years(value: str | None, target: ScholarlyTarget) -> bool:
    if not value:
        return True
    try:
        year = int(value[:4])
    except (TypeError, ValueError):
        return False
    return target.start_year <= year <= target.end_year


def deduplicate_scholarly_candidates(
    candidates: Iterable[ScholarlyCandidate],
) -> list[ScholarlyCandidate]:
    selected: dict[str, ScholarlyCandidate] = {}
    for candidate in candidates:
        current = selected.get(candidate.candidate_uid)
        if current is None or (candidate.source_updated_at or "") > (current.source_updated_at or ""):
            selected[candidate.candidate_uid] = candidate
    return sorted(selected.values(), key=lambda item: (item.published_date or "", item.title))


def build_scholarly_adapters(
    source_configs: Iterable[Mapping[str, Any]], http_client: HttpClient
) -> list[ScholarlySourceAdapter]:
    """Create enabled Crossref/OpenAlex adapters from collection config."""

    adapters: list[ScholarlySourceAdapter] = []
    for source_config in source_configs:
        if not source_config.get("enabled", True):
            continue
        name = str(source_config.get("name", "")).strip().lower()
        priority = int(source_config.get("priority", {"crossref": 30, "openalex": 40}.get(name, 100)))
        if name == "crossref":
            adapters.append(
                CrossrefAdapter(
                    http_client,
                    priority=priority,
                    rows=int(source_config.get("rows", 100)),
                    mailto=str(source_config.get("mailto") or "") or None,
                )
            )
        elif name == "openalex":
            adapters.append(
                OpenAlexAdapter(
                    http_client,
                    priority=priority,
                    per_page=int(source_config.get("per_page", 100)),
                    mailto=str(source_config.get("mailto") or "") or None,
                )
            )
        else:
            raise ValueError(
                f"unknown scholarly source {name!r}; available sources: crossref, openalex"
            )
    return sorted(adapters, key=lambda adapter: adapter.priority)
