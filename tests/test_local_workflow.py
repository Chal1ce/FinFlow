import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from delivery.publisher import LocalDeliveryPublisher
from ingestion.local_documents import LocalPdfImporter, load_local_documents
from processing.financial_metadata import (
    FinancialMetadataReviewService,
    FinancialReportVersionService,
)
from storage.state_store import StateStore
from workflow.runner import LocalWorkflowRunner


VALID_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj\n<< /Type /Page >>\nendobj\n"
    b"trailer\n<<>>\nstartxref\n0\n%%EOF\n"
    b"%" + b"test-padding\n" * 12
)


def create_governance_fixture(root: Path) -> None:
    parsed = root / "parsed_md/scholarly/work1/asset1/local"
    parsed.mkdir(parents=True)
    (parsed / "result.json").write_text(
        json.dumps(
            {
                "layoutParsingResults": [
                    {
                        "prunedResult": {
                            "parsing_res_list": [
                                {"block_label": "doc_title", "block_content": "ESG and Firm Value"},
                                {"block_label": "paragraph_title", "block_content": "1. Introduction"},
                                {
                                    "block_label": "text",
                                    "block_content": "ESG improves firm value through governance.",
                                },
                            ]
                        },
                        "markdown": {"text": "ESG and Firm Value"},
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (parsed / "output.md").write_text(
        "# ESG and Firm Value\n\nESG improves firm value.", encoding="utf-8"
    )
    manifests = root / "manifests"
    manifests.mkdir(parents=True)
    (manifests / "scholarly_documents.jsonl").write_text(
        json.dumps(
            {
                "asset_uid": "asset1",
                "work_uid": "work1",
                "document_uid": "doc1",
                "title": "ESG and Firm Value",
                "source_name": "crossref",
                "source_id": "10.1/test",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


class LocalWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.project_root = Path(__file__).resolve().parents[1]
        self.environment = {
            "FIN_DOC_DATA_ROOT": str(self.root),
            "FIN_DOC_STATE_DB": str(self.root / "state/pipeline.db"),
            "FIN_DOC_PROCESSED": str(self.root / "processed"),
            "FIN_DOC_GOVERNED": str(self.root / "processed/governed"),
            "FIN_DOC_CHUNKS_DIR": str(self.root / "processed/chunks"),
            "FIN_DOC_CHUNKS_JSONL": str(self.root / "processed/chunks.jsonl"),
            "LLM_BACKEND": "mock",
        }

    def tearDown(self):
        self.directory.cleanup()

    def test_local_pipeline_governs_validates_and_publishes_immutable_release(self):
        create_governance_fixture(self.root)
        with patch.dict(os.environ, self.environment, clear=False):
            runner = LocalWorkflowRunner(
                data_root=self.root,
                collection_config_path=self.project_root / "config/collection.json",
            )
            result = runner.run(
                "govern_and_publish", batch_id="batch-published", llm_backend="mock"
            )

        self.assertEqual(result["status"], "success")
        release_path = self.root / "published/batch-published" / result["workflow_run_id"]
        self.assertTrue((release_path / "metadata.jsonl").is_file())
        self.assertTrue((release_path / "governed-documents.jsonl").is_file())
        self.assertTrue((release_path / "financial_metadata.jsonl").is_file())
        self.assertTrue((release_path / "ocr-quality.jsonl").is_file())
        self.assertTrue((release_path / "metadata-override-audit.jsonl").is_file())
        self.assertTrue((release_path / "financial-report-versions.jsonl").is_file())
        self.assertTrue((release_path / "version-selection-audit.jsonl").is_file())
        self.assertTrue((release_path / "metadata-qc-summary.json").is_file())
        self.assertTrue((release_path / "ocr-quality-summary.json").is_file())
        self.assertTrue((release_path / "chunks.jsonl").is_file())
        self.assertTrue((release_path / "artifacts.jsonl").is_file())
        self.assertTrue((release_path / "manifest.json").is_file())
        self.assertTrue((release_path / "checksums.sha256").is_file())
        self.assertTrue((self.root / "published/batch-published/latest.json").is_file())

        manifest = json.loads((release_path / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["document_count"], 1)
        self.assertEqual(manifest["governed_document_count"], 1)
        self.assertGreater(manifest["chunk_count"], 0)
        self.assertEqual(manifest["format_version"], "financial-document-delivery-v6")
        self.assertEqual(manifest["financial_metadata_count"], 0)
        self.assertEqual(manifest["ocr_quality_count"], 0)
        self.assertEqual(manifest["metadata_override_audit_count"], 0)
        self.assertEqual(manifest["financial_report_version_count"], 0)
        self.assertEqual(manifest["version_selection_audit_count"], 0)
        checksum_lines = (release_path / "checksums.sha256").read_text(encoding="ascii").splitlines()
        self.assertEqual(len(checksum_lines), 12)
        governed_records = [
            json.loads(line)
            for line in (release_path / "governed-documents.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
        ]
        governed_path = next((self.root / "processed/governed").rglob("governed.md"))
        self.assertEqual(governed_records[0]["text"], governed_path.read_text(encoding="utf-8"))

        with StateStore(self.root / "state/pipeline.db") as store:
            workflow = store.get_workflow_run(result["workflow_run_id"])
            stages = store.get_workflow_stages(result["workflow_run_id"])
        self.assertEqual(workflow["status"], "success")
        self.assertEqual([stage["status"] for stage in stages], ["success", "success", "success"])

    def test_resume_skips_successful_stages_and_retries_the_failed_stage(self):
        definition_path = self.root / "pipelines.json"
        definition_path.write_text(
            json.dumps(
                {
                    "pipelines": {
                        "test": {"description": "test", "stages": ["first", "second"]}
                    }
                }
            ),
            encoding="utf-8",
        )
        calls = []
        failure = {"enabled": True}

        def first(*_args):
            calls.append("first")
            return {"status": "success"}

        def second(*_args):
            calls.append("second")
            if failure["enabled"]:
                raise RuntimeError("simulated failure")
            return {"status": "success"}

        runner = LocalWorkflowRunner(
            data_root=self.root,
            pipeline_config_path=definition_path,
            stage_handlers={"first": first, "second": second},
        )
        first_result = runner.run("test", batch_id="batch-retry")
        self.assertEqual(first_result["status"], "failed")
        self.assertEqual(calls, ["first", "second"])

        failure["enabled"] = False
        resumed = runner.run(
            "test", resume_from_run_id=first_result["workflow_run_id"]
        )
        self.assertEqual(resumed["status"], "success")
        self.assertEqual(calls, ["first", "second", "second"])
        stage_statuses = [
            item["status"] for item in runner.status(resumed["workflow_run_id"])["stage_runs"]
        ]
        self.assertEqual(stage_statuses, ["skipped", "success"])

    def test_dry_run_plans_delivery_without_creating_a_release(self):
        create_governance_fixture(self.root)
        with patch.dict(os.environ, self.environment, clear=False):
            runner = LocalWorkflowRunner(
                data_root=self.root,
                collection_config_path=self.project_root / "config/collection.json",
            )
            result = runner.run(
                "govern_and_publish", batch_id="batch-preview", dry_run=True, llm_backend="mock"
            )

        self.assertEqual(result["status"], "success")
        self.assertFalse((self.root / "published").exists())
        validate_stage = next(item for item in result["stages"] if item["stage_name"] == "validate")
        publish_stage = next(item for item in result["stages"] if item["stage_name"] == "publish")
        self.assertTrue(validate_stage["dry_run"])
        self.assertTrue(publish_stage["dry_run"])

    def test_full_financial_pipeline_imports_ocr_governs_and_publishes(self):
        source_pdf = self.root / "input.pdf"
        source_pdf.write_bytes(VALID_PDF)

        def fake_ocr(_source: Path, output_dir: Path):
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "output.md").write_text(
                "# Financial report\n\nRevenue increased.", encoding="utf-8"
            )
            (output_dir / "result.json").write_text(
                json.dumps(
                    {
                        "layoutParsingResults": [
                            {
                                "prunedResult": {
                                    "parsing_res_list": [
                                        {"block_label": "doc_title", "block_content": "Financial report"},
                                        {"block_label": "text", "block_content": "Revenue increased."},
                                    ]
                                }
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            return {"page_result_count": 1}

        with patch.dict(os.environ, self.environment, clear=False):
            runner = LocalWorkflowRunner(
                data_root=self.root,
                collection_config_path=self.project_root / "config/collection.json",
                ocr_processor=fake_ocr,
            )
            result = runner.run(
                "local_financial",
                batch_id="batch-financial",
                input_path=source_pdf,
                ocr_backend="local",
                llm_backend="mock",
                document_metadata={
                    "stock_code": "600519",
                    "report_year": 2023,
                    "report_type": "annual",
                    "title": "贵州茅台2023年年度报告",
                },
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(
            [stage["stage_name"] for stage in result["stages"]],
            ["ingest", "ocr", "ocr_quality", "metadata", "govern", "validate", "publish"],
        )
        self.assertTrue((self.root / "raw_pdfs/imported").exists())
        self.assertTrue((self.root / "parsed_md/financial").exists())
        self.assertTrue((self.root / "processed/governed/financial").exists())
        release_path = self.root / "published/batch-financial" / result["workflow_run_id"]
        self.assertTrue((release_path / "manifest.json").is_file())
        metadata_records = [
            json.loads(line)
            for line in (release_path / "financial_metadata.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        self.assertEqual(len(metadata_records), 1)
        quality_records = [
            json.loads(line)
            for line in (release_path / "ocr-quality.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(len(quality_records), 1)
        ocr_quality_summary = json.loads(
            (release_path / "ocr-quality-summary.json").read_text(encoding="utf-8")
        )
        self.assertFalse(ocr_quality_summary["blocking"])
        qc_summary = json.loads(
            (release_path / "metadata-qc-summary.json").read_text(encoding="utf-8")
        )
        self.assertFalse(qc_summary["blocking"])
        review_result = FinancialMetadataReviewService(self.root).apply_override(
            batch_id="batch-financial",
            asset_uid=metadata_records[0]["asset_uid"],
            overrides={"company_name": "Reviewed Financial Company"},
            reason="checked against the report cover",
        )
        self.assertEqual(review_result["status"], "success")
        reviewed_release = LocalDeliveryPublisher(self.root).publish(
            "batch-financial", "manual-review-1"
        )
        override_audits = [
            json.loads(line)
            for line in (
                Path(reviewed_release["release_path"]) / "metadata-override-audit.jsonl"
            ).read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(len(override_audits), 1)
        self.assertEqual(override_audits[0]["reason"], "checked against the report cover")

    def test_ocr_resume_reuses_persisted_input_parameters(self):
        source_pdf = self.root / "input.pdf"
        source_pdf.write_bytes(VALID_PDF)
        attempts = {"count": 0}

        def flaky_ocr(_source: Path, output_dir: Path):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RuntimeError("temporary OCR failure")
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "output.md").write_text("Recovered OCR output.", encoding="utf-8")
            return {"page_result_count": 1}

        with patch.dict(os.environ, self.environment, clear=False):
            runner = LocalWorkflowRunner(
                data_root=self.root,
                collection_config_path=self.project_root / "config/collection.json",
                ocr_processor=flaky_ocr,
            )
            first = runner.run(
                "local_financial",
                batch_id="batch-ocr-retry",
                input_path=source_pdf,
                ocr_backend="local",
                llm_backend="mock",
            )
            resumed = runner.run(
                "local_financial", resume_from_run_id=first["workflow_run_id"], llm_backend="mock"
            )

        self.assertEqual(first["failed_stage"], "ocr")
        self.assertEqual(resumed["status"], "success")
        self.assertEqual(attempts["count"], 2)
        resumed_stages = runner.status(resumed["workflow_run_id"])["stage_runs"]
        self.assertEqual(resumed_stages[0]["status"], "skipped")
        self.assertEqual(resumed_stages[1]["status"], "success")

    def test_delivery_uses_manually_selected_report_version(self):
        source_directory = self.root / "version-input"
        source_directory.mkdir()
        (source_directory / "original.pdf").write_bytes(VALID_PDF)
        (source_directory / "corrected.pdf").write_bytes(VALID_PDF + b"corrected")

        def fake_ocr(_source: Path, output_dir: Path):
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "output.md").write_text(
                "# Example Company 2023 Annual Report\n\nRevenue increased.", encoding="utf-8"
            )
            (output_dir / "result.json").write_text(
                json.dumps(
                    {
                        "layoutParsingResults": [
                            {
                                "prunedResult": {
                                    "parsing_res_list": [
                                        {
                                            "block_label": "doc_title",
                                            "block_content": "Example Company 2023 Annual Report",
                                        },
                                        {"block_label": "text", "block_content": "Revenue increased."},
                                    ]
                                }
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            return {"page_result_count": 1}

        with patch.dict(os.environ, self.environment, clear=False):
            runner = LocalWorkflowRunner(
                data_root=self.root,
                collection_config_path=self.project_root / "config/collection.json",
                ocr_processor=fake_ocr,
            )
            result = runner.run(
                "local_financial",
                batch_id="batch-versions",
                input_path=source_directory,
                ocr_backend="local",
                llm_backend="mock",
                document_metadata={
                    "stock_code": "600519",
                    "company_name": "Example Company",
                    "report_year": 2023,
                    "report_type": "annual",
                    "language": "en",
                    "title": "Example Company 2023 Annual Report",
                },
            )

        initial_release = self.root / "published/batch-versions" / result["workflow_run_id"]
        initial_manifest = json.loads((initial_release / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(initial_manifest["document_count"], 1)
        version_service = FinancialReportVersionService(self.root)
        review = version_service.review(batch_id="batch-versions")
        self.assertEqual(review["review_count"], 1)
        group = review["groups"][0]
        candidate = next(
            item["asset_uid"]
            for item in group["versions"]
            if item["asset_uid"] != group["active_asset_uid"]
        )
        version_service.select(
            batch_id="batch-versions",
            report_group_uid=group["report_group_uid"],
            asset_uid=candidate,
            reason="selected corrected PDF after manual comparison",
        )
        reviewed_release = LocalDeliveryPublisher(self.root).publish(
            "batch-versions", "version-review-1"
        )
        release_path = Path(reviewed_release["release_path"])
        released_metadata = [
            json.loads(line)
            for line in (release_path / "financial_metadata.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        version_audits = [
            json.loads(line)
            for line in (release_path / "version-selection-audit.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        self.assertEqual([record["asset_uid"] for record in released_metadata], [candidate])
        self.assertEqual(len(version_audits), 1)
        self.assertEqual(version_audits[0]["selected_asset_uid"], candidate)

    def test_full_pipeline_dry_run_needs_no_ocr_service_or_manifest(self):
        source_pdf = self.root / "preview.pdf"
        source_pdf.write_bytes(VALID_PDF)
        with patch.dict(os.environ, self.environment, clear=False):
            runner = LocalWorkflowRunner(
                data_root=self.root,
                collection_config_path=self.project_root / "config/collection.json",
            )
            result = runner.run(
                "local_financial",
                batch_id="batch-full-preview",
                input_path=source_pdf,
                ocr_backend="local",
                llm_backend="mock",
                dry_run=True,
            )

        self.assertEqual(result["status"], "success")
        self.assertFalse((self.root / "raw_pdfs").exists())
        self.assertFalse((self.root / "published").exists())

    def test_reimported_asset_remains_available_to_each_batch(self):
        source_pdf = self.root / "shared.pdf"
        source_pdf.write_bytes(VALID_PDF)
        importer = LocalPdfImporter(self.root)

        first = importer.run(source_pdf, batch_id="batch-one")
        second = importer.run(source_pdf, batch_id="batch-two")

        self.assertEqual(first["status"], "success")
        self.assertEqual(second["status"], "success")
        record = next(iter(load_local_documents(self.root).values()))
        self.assertEqual(record["batch_ids"], ["batch-one", "batch-two"])


if __name__ == "__main__":
    unittest.main()
