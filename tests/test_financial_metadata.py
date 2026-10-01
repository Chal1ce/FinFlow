import json
import tempfile
import unittest
from pathlib import Path

from processing.financial_metadata import (
    FinancialMetadataReviewService,
    FinancialMetadataRunner,
    FinancialReportVersionService,
    load_financial_metadata,
    load_financial_metadata_overrides,
)
from storage.state_store import StateStore


def local_record(
    asset_uid: str,
    *,
    title: str,
    import_metadata: dict | None = None,
) -> dict:
    return {
        "schema_version": "local-document-v1",
        "batch_id": "batch-metadata",
        "batch_ids": ["batch-metadata"],
        "document_uid": f"document-{asset_uid}",
        "work_uid": f"document-{asset_uid}",
        "report_uid": f"document-{asset_uid}",
        "asset_uid": asset_uid,
        "document_type": "financial-report",
        "title": title,
        "source_name": "local-import",
        "source_id": f"{asset_uid}.pdf",
        "source_url": f"file:///{asset_uid}.pdf",
        "raw_path": f"raw_pdfs/imported/{asset_uid}.pdf",
        "raw_file_hash": "a" * 64 if asset_uid == "asset-one" else "b" * 64,
        "status": "success",
        "ocr_status": "success",
        "ocr_backend": "local",
        "ocr_output_dir": f"parsed_md/financial/document-{asset_uid}/{asset_uid}/local",
        "import_metadata": import_metadata or {},
    }


class FinancialMetadataTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def _write_manifest(self, records: list[dict]) -> None:
        path = self.root / "manifests/local_documents.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(
            "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
            encoding="utf-8",
        )

    def test_explicit_import_metadata_has_priority_and_is_persisted(self):
        record = local_record(
            "asset-one",
            title="600519 Example Holdings 2023 Annual Report",
            import_metadata={
                "stock_code": "000001",
                "company_name": "Explicit Company",
                "report_year": 2024,
                "report_type": "annual",
                "report_period": "2024",
                "announcement_date": "2025-03-30",
                "language": "en",
                "document_variant": "corrected",
            },
        )
        self._write_manifest([record])

        result = FinancialMetadataRunner(self.root).run(batch_id="batch-metadata")

        self.assertEqual(result["status"], "success")
        metadata = load_financial_metadata(self.root)["asset-one"]
        self.assertEqual(metadata["stock_code"], "000001")
        self.assertEqual(metadata["company_name"], "Explicit Company")
        self.assertEqual(metadata["report_year"], 2024)
        self.assertEqual(metadata["metadata_sources"]["stock_code"], "explicit_import")
        self.assertEqual(metadata["status"], "complete")
        with StateStore(self.root / "state/pipeline.db") as store:
            stored = store.connection.execute(
                "SELECT metadata_json FROM financial_document_metadata WHERE asset_uid = ?",
                ("asset-one",),
            ).fetchone()
            checks = store.connection.execute(
                "SELECT check_name FROM qc_result WHERE qc_stage = 'financial_metadata'"
            ).fetchall()
        self.assertIsNotNone(stored)
        self.assertTrue(any(row["check_name"] == "metadata_report_type" for row in checks))

    def test_filename_rules_infer_financial_identity(self):
        record = local_record(
            "asset-one", title="600519 Guizhou Moutai 2023 Annual Report"
        )
        self._write_manifest([record])

        result = FinancialMetadataRunner(self.root).run(batch_id="batch-metadata")

        self.assertEqual(result["status"], "success")
        metadata = load_financial_metadata(self.root)["asset-one"]
        self.assertEqual(metadata["stock_code"], "600519")
        self.assertEqual(metadata["company_name"], "Guizhou Moutai")
        self.assertEqual(metadata["report_year"], 2023)
        self.assertEqual(metadata["report_type"], "annual")
        self.assertEqual(metadata["report_period"], "2023")
        self.assertEqual(metadata["metadata_sources"]["stock_code"], "title")

    def test_version_conflicts_are_warned_without_failing_metadata_stage(self):
        first = local_record("asset-one", title="600519 Guizhou Moutai 2023 Annual Report")
        second = local_record("asset-two", title="600519 Guizhou Moutai 2023 Annual Report")
        self._write_manifest([first, second])

        result = FinancialMetadataRunner(self.root).run(batch_id="batch-metadata")

        self.assertEqual(result["status"], "success")
        metadata = load_financial_metadata(self.root)
        for record in metadata.values():
            version_check = next(
                check
                for check in record["qc_checks"]
                if check["check_name"] == "metadata_version_conflict"
            )
            self.assertEqual(version_check["status"], "warn")
        self.assertGreater(result["warning_count"], 0)

    def test_manual_override_is_audited_and_regenerates_metadata_without_ocr(self):
        record = local_record("asset-one", title="Financial report")
        self._write_manifest([record])
        FinancialMetadataRunner(self.root).run(batch_id="batch-metadata")
        review_service = FinancialMetadataReviewService(self.root)

        before_review = review_service.review(batch_id="batch-metadata")
        result = review_service.apply_override(
            batch_id="batch-metadata",
            asset_uid="asset-one",
            overrides={
                "stock_code": "000001",
                "company_name": "Reviewed Company",
                "report_year": 2024,
                "report_type": "annual",
                "language": "en",
            },
            reason="checked against the signed annual report cover",
        )

        self.assertEqual(before_review["review_count"], 1)
        self.assertEqual(result["status"], "success")
        metadata = load_financial_metadata(self.root)["asset-one"]
        self.assertEqual(metadata["status"], "complete")
        self.assertEqual(metadata["company_name"], "Reviewed Company")
        self.assertEqual(metadata["report_period"], "2024")
        self.assertEqual(metadata["metadata_sources"]["company_name"], "manual_override")
        override_record = load_financial_metadata_overrides(self.root)["asset-one"]
        self.assertEqual(override_record["overrides"]["stock_code"], "000001")
        with StateStore(self.root / "state/pipeline.db") as store:
            audits = store.list_financial_metadata_override_audits(["asset-one"])
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]["reason"], "checked against the signed annual report cover")
        self.assertEqual(len(audits[0]["field_changes"]), 4)
        self.assertEqual(review_service.review(batch_id="batch-metadata")["review_count"], 0)

    def test_report_version_selection_preserves_candidates_and_audits_decision(self):
        metadata = {
            "stock_code": "600519",
            "company_name": "Example Company",
            "report_year": 2023,
            "report_type": "annual",
            "language": "en",
        }
        first = local_record(
            "asset-one", title="600519 Example Company 2023 Annual Report", import_metadata=metadata
        )
        second = local_record(
            "asset-two", title="600519 Example Company 2023 Annual Report", import_metadata=metadata
        )
        self._write_manifest([first, second])
        FinancialMetadataRunner(self.root).run(batch_id="batch-metadata")
        version_service = FinancialReportVersionService(self.root)

        review = version_service.review(batch_id="batch-metadata")

        self.assertEqual(review["group_count"], 1)
        self.assertEqual(review["review_count"], 1)
        group = review["groups"][0]
        self.assertEqual(group["version_count"], 2)
        candidate = next(
            version["asset_uid"]
            for version in group["versions"]
            if version["asset_uid"] != group["active_asset_uid"]
        )
        result = version_service.select(
            batch_id="batch-metadata",
            report_group_uid=group["report_group_uid"],
            asset_uid=candidate,
            reason="reviewed corrected source document",
        )

        self.assertEqual(result["status"], "success")
        selected_review = version_service.review(batch_id="batch-metadata")
        self.assertEqual(selected_review["review_count"], 0)
        self.assertEqual(selected_review["groups"][0]["active_asset_uid"], candidate)
        with StateStore(self.root / "state/pipeline.db") as store:
            audits = store.list_financial_report_version_selection_audits(
                [group["report_group_uid"]]
            )
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]["selected_asset_uid"], candidate)


if __name__ == "__main__":
    unittest.main()
