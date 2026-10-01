"""Discovery adapters for official financial-report sources."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from typing import Any, Iterable, Mapping
from urllib.parse import urljoin

from core.ids import logical_report_uid, source_candidate_uid
from .downloader import ReportSpec, SUPPORTED_REPORT_TYPES, utc_now
from .http_client import HttpClient, parse_json_or_jsonp


class SourceDiscoveryError(RuntimeError):
    """Raised when a source response cannot be converted to candidates."""


STOCK_CODE_PATTERN = re.compile(r"^\d{6}$")
REPORT_YEAR_PATTERN = re.compile(r"(20\d{2})年年度报告")
HTML_TAG_PATTERN = re.compile(r"<[^>]+>")


def clean_title(value: object) -> str:
    return unescape(HTML_TAG_PATTERN.sub("", str(value or ""))).strip()


def report_year_from_title(title: str) -> int | None:
    match = REPORT_YEAR_PATTERN.search(title)
    return int(match.group(1)) if match else None


def date_from_millis(value: object) -> str | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


@dataclass(frozen=True)
class CollectionTarget:
    stock_code: str
    start_year: int
    end_year: int
    report_type: str = "annual"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CollectionTarget":
        stock_code = str(value.get("stock_code", "")).zfill(6)
        if not STOCK_CODE_PATTERN.fullmatch(stock_code):
            raise ValueError(f"invalid target stock_code: {stock_code!r}")
        try:
            start_year = int(value["start_year"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("target requires integer start_year") from exc
        end_year_value = value.get("end_year")
        if str(end_year_value).strip().lower() in {"auto", "current"}:
            try:
                end_year_lag = int(value.get("end_year_lag", 1))
            except (TypeError, ValueError) as exc:
                raise ValueError("end_year_lag must be an integer") from exc
            end_year = datetime.now().year - end_year_lag
        else:
            try:
                end_year = int(end_year_value)
            except (TypeError, ValueError) as exc:
                raise ValueError("target requires integer end_year or 'auto'") from exc
        if not 1990 <= start_year <= end_year <= datetime.now().year + 1:
            raise ValueError(f"invalid target year range: {start_year}-{end_year}")
        report_type = str(value.get("report_type", "annual")).strip().lower()
        if report_type not in SUPPORTED_REPORT_TYPES:
            raise ValueError(f"unsupported target report_type: {report_type!r}")
        return cls(stock_code, start_year, end_year, report_type)


@dataclass(frozen=True)
class ReportCandidate:
    source_name: str
    source_id: str
    stock_code: str
    report_year: int
    report_type: str
    title: str
    publish_date: str | None
    source_url: str
    priority: int
    discovered_at: str

    @property
    def candidate_uid(self) -> str:
        return source_candidate_uid(self.source_name, self.source_id, self.source_url)

    @property
    def canonical_uid(self) -> str:
        return logical_report_uid(self.stock_code, self.report_year, self.report_type)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "candidate_uid": self.candidate_uid,
            "canonical_uid": self.canonical_uid,
            "source_name": self.source_name,
            "source_id": self.source_id,
            "stock_code": self.stock_code,
            "report_year": self.report_year,
            "report_type": self.report_type,
            "title": self.title,
            "publish_date": self.publish_date,
            "source_url": self.source_url,
            "source_priority": self.priority,
            "discovered_at": self.discovered_at,
        }

    def to_report_spec(self) -> ReportSpec:
        return ReportSpec(
            stock_code=self.stock_code,
            report_year=self.report_year,
            source_url=self.source_url,
            report_type=self.report_type,
            publish_date=self.publish_date,
            title=self.title,
            source_name=self.source_name,
            source_id=self.source_id,
            candidate_uid=self.candidate_uid,
            canonical_uid=self.canonical_uid,
            source_priority=self.priority,
        )


class SourceAdapter(ABC):
    """Common contract implemented by each report discovery source."""

    name: str
    priority: int

    def __init__(self, http_client: HttpClient, priority: int | None = None) -> None:
        self.http = http_client
        if priority is not None:
            self.priority = priority

    @abstractmethod
    def discover(self, target: CollectionTarget) -> list[ReportCandidate]:
        raise NotImplementedError


class CNInfoAdapter(SourceAdapter):
    """Discover annual reports from CNINFO's public announcement endpoint."""

    name = "cninfo"
    priority = 10
    endpoint = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
    referer = "https://www.cninfo.com.cn/"
    static_pdf_base = "https://static.cninfo.com.cn/"

    def discover(self, target: CollectionTarget) -> list[ReportCandidate]:
        if target.report_type != "annual":
            return []

        query_variants = [
            {
                "stock": f"{target.stock_code},{self._org_id(target.stock_code)}",
                "searchkey": "",
                "secid": self._org_id(target.stock_code),
            },
            {
                "stock": "",
                "searchkey": target.stock_code,
                "secid": "",
            },
        ]
        for query_variant in query_variants:
            candidates = self._discover_with_query(target, query_variant)
            if candidates:
                return candidates
        return []

    def _discover_with_query(
        self, target: CollectionTarget, query_variant: Mapping[str, str]
    ) -> list[ReportCandidate]:
        candidates: list[ReportCandidate] = []
        page_size = 30
        for page_num in range(1, 21):
            form = {
                "pageNum": page_num,
                "pageSize": page_size,
                "column": "szse",
                "tabName": "fulltext",
                "plate": "",
                "category": "category_ndbg_szsh",
                "trade": "",
                "seDate": self._publish_date_range(target),
                "sortName": "announcementTime",
                "sortType": "desc",
                "isHLtitle": "true",
            }
            form.update(query_variant)
            response = self.http.post_form(
                self.endpoint,
                form,
                referer=self.referer,
            )
            try:
                payload = parse_json_or_jsonp(response)
            except ValueError as exc:
                raise SourceDiscoveryError(f"CNINFO returned invalid JSON: {exc}") from exc

            announcements = payload.get("announcements") or []
            if not isinstance(announcements, list):
                raise SourceDiscoveryError("CNINFO announcements field is not a list")
            candidates.extend(self._parse_announcements(announcements, target))
            if not payload.get("hasMore") or len(announcements) < page_size:
                break
        return deduplicate_candidates(candidates)

    def _parse_announcements(
        self, announcements: Iterable[Mapping[str, Any]], target: CollectionTarget
    ) -> list[ReportCandidate]:
        parsed: list[ReportCandidate] = []
        for item in announcements:
            title = clean_title(item.get("announcementTitle"))
            report_year = report_year_from_title(title)
            if report_year is None or not target.start_year <= report_year <= target.end_year:
                continue
            if "摘要" in title or "英文" in title or "取消" in title:
                continue
            adjunct_url = str(item.get("adjunctUrl") or "").strip()
            source_id = str(item.get("announcementId") or "").strip()
            if not adjunct_url or not source_id:
                continue
            parsed.append(
                ReportCandidate(
                    source_name=self.name,
                    source_id=source_id,
                    stock_code=target.stock_code,
                    report_year=report_year,
                    report_type=target.report_type,
                    title=title,
                    publish_date=date_from_millis(item.get("announcementTime")),
                    source_url=urljoin(self.static_pdf_base, adjunct_url),
                    priority=self.priority,
                    discovered_at=utc_now(),
                )
            )
        return parsed

    @staticmethod
    def _publish_date_range(target: CollectionTarget) -> str:
        return f"{target.start_year + 1}-01-01~{target.end_year + 1}-12-31"

    @staticmethod
    def _org_id(stock_code: str) -> str:
        market_prefix = "gssh" if stock_code.startswith("6") else "gssz"
        return f"{market_prefix}{stock_code.zfill(7)}"


class SSEAdapter(SourceAdapter):
    """Discover annual reports from the Shanghai Stock Exchange endpoint."""

    name = "sse"
    priority = 20
    endpoint = "https://query.sse.com.cn/security/stock/queryCompanyBulletin.do"
    referer = "https://www.sse.com.cn/disclosure/listedinfo/regular/"
    pdf_base = "https://www.sse.com.cn"

    def discover(self, target: CollectionTarget) -> list[ReportCandidate]:
        if target.report_type != "annual" or not target.stock_code.startswith(("6", "68")):
            return []

        page_size = 25
        candidates: list[ReportCandidate] = []
        for page_no in range(1, 21):
            response = self.http.post_form(
                self.endpoint,
                {
                    "isPagination": "true",
                    "pageHelp.pageSize": page_size,
                    "pageHelp.pageNo": page_no,
                    "pageHelp.beginPage": 1,
                    "pageHelp.cacheSize": 1,
                    "pageHelp.endPage": 1,
                    "productId": target.stock_code,
                    "securityType": "0101,120100,020100,020200,120200",
                    "reportType2": "DQBG",
                    "reportType": "YEARLY",
                    "beginDate": f"{target.start_year + 1}-01-01",
                    "endDate": f"{target.end_year + 1}-12-31",
                    "jsonCallBack": "finDocGovCallback",
                },
                referer=self.referer,
            )
            try:
                payload = parse_json_or_jsonp(response)
            except ValueError as exc:
                raise SourceDiscoveryError(f"SSE returned invalid JSONP: {exc}") from exc
            result = payload.get("result") or []
            if not isinstance(result, list):
                raise SourceDiscoveryError("SSE result field is not a list")
            candidates.extend(self._parse_result(result, target))
            page_help = payload.get("pageHelp") or {}
            page_count = int(page_help.get("pageCount") or 1)
            if page_no >= page_count or len(result) < page_size:
                break
        return deduplicate_candidates(candidates)

    def _parse_result(
        self, result: Iterable[Mapping[str, Any]], target: CollectionTarget
    ) -> list[ReportCandidate]:
        parsed: list[ReportCandidate] = []
        for item in result:
            title = clean_title(item.get("TITLE"))
            report_year = report_year_from_title(title)
            if report_year is None or not target.start_year <= report_year <= target.end_year:
                continue
            if "摘要" in title or "英文" in title or "取消" in title:
                continue
            relative_url = str(item.get("URL") or "").strip()
            if not relative_url:
                continue
            parsed.append(
                ReportCandidate(
                    source_name=self.name,
                    source_id=relative_url,
                    stock_code=target.stock_code,
                    report_year=report_year,
                    report_type=target.report_type,
                    title=title,
                    publish_date=str(item.get("SSEDATE") or "") or None,
                    source_url=urljoin(self.pdf_base, relative_url),
                    priority=self.priority,
                    discovered_at=utc_now(),
                )
            )
        return parsed


def deduplicate_candidates(candidates: Iterable[ReportCandidate]) -> list[ReportCandidate]:
    """Keep the newest candidate per report/source pair."""

    selected: dict[tuple[str, str], ReportCandidate] = {}
    for candidate in candidates:
        key = (candidate.canonical_uid, candidate.source_name)
        current = selected.get(key)
        if current is None or (candidate.publish_date or "") > (current.publish_date or ""):
            selected[key] = candidate
    return sorted(selected.values(), key=lambda item: (item.report_year, item.source_name))


def build_source_adapters(
    source_configs: Iterable[Mapping[str, Any]], http_client: HttpClient
) -> list[SourceAdapter]:
    """Create enabled adapters from configuration."""

    adapter_types: dict[str, type[SourceAdapter]] = {
        "cninfo": CNInfoAdapter,
        "sse": SSEAdapter,
    }
    adapters: list[SourceAdapter] = []
    for source_config in source_configs:
        if not source_config.get("enabled", True):
            continue
        name = str(source_config.get("name", "")).strip().lower()
        adapter_type = adapter_types.get(name)
        if adapter_type is None:
            available = ", ".join(sorted(adapter_types))
            raise ValueError(f"unknown source {name!r}; available sources: {available}")
        priority = int(source_config.get("priority", adapter_type.priority))
        adapters.append(adapter_type(http_client, priority=priority))
    return sorted(adapters, key=lambda adapter: adapter.priority)
