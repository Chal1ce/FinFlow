"""Optional collection scope for backfill and queued work in a shared data root."""

from core.ids import logical_report_uid
from spiders.source_adapters import CollectionTarget


class CollectionScope:
    def __init__(self, collection):
        self.targets = [CollectionTarget.from_mapping(value) for value in collection.get("targets", [])]
        self.financial_sources = {row["name"] for row in collection.get("sources", []) if row.get("enabled", True)}
        scholarly = collection.get("scholarly") or {}
        self.scholarly_sources = {
            row["name"] for row in scholarly.get("sources", []) if scholarly.get("enabled", True) and row.get("enabled", True)
        }
        self.work_uids = {
            logical_report_uid(target.stock_code, year, target.report_type)
            for target in self.targets
            for year in range(target.start_year, target.end_year + 1)
        }

    def __call__(self, payload):
        source = payload.get("source") or payload.get("manifest_record") or payload
        name = source.get("source_name") or payload.get("source_name")
        if name in self.scholarly_sources:
            return True
        if name not in self.financial_sources:
            return False
        work = payload.get("work_uid") or source.get("canonical_uid") or source.get("document_uid")
        if work in self.work_uids:
            return True
        try:
            year = int(source.get("report_year", 0))
        except (TypeError, ValueError):
            return False
        return any(
            str(source.get("stock_code", "")).zfill(6) == target.stock_code
            and target.start_year <= year <= target.end_year
            and source.get("report_type", "annual") == target.report_type
            for target in self.targets
        )
