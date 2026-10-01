import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.context import PipelineContext
from core.ids import raw_asset_uid
from delivery.publisher import LocalDeliveryPublisher
from ingestion.local_documents import load_local_documents
from storage.state_store import StateStore
from workflow.runner import LocalWorkflowRunner


VALID_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj\n<< /Type /Page >>\nendobj\n"
    b"trailer\n<<>>\nstartxref\n0\n%%EOF\n"
    b"%" + b"test-padding\n" * 12
)


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class LocalOperationTests(unittest.TestCase):
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

    def test_collected_reports_flow_into_local_delivery_with_original_provenance(self):
        raw_path = self.root / "raw_pdfs/2023/600519/report.pdf"
        raw_path.parent.mkdir(parents=True)
        raw_path.write_bytes(VALID_PDF)
        report = {
            "status": "success",
            "batch_id": "batch-collected",
            "batch_ids": ["batch-collected"],
            "report_uid": "report-600519-2023-annual",
            "document_uid": "report-600519-2023-annual",
            "asset_uid": raw_asset_uid(sha256(VALID_PDF)),
            "raw_path": "raw_pdfs/2023/600519/report.pdf",
            "raw_file_hash": sha256(VALID_PDF),
            "source_name": "exchange",
            "source_id": "notice-1",
            "source_url": "https://example.test/report.pdf",
            "stock_code": "600519",
            "report_year": 2023,
            "report_type": "annual",
            "publish_date": "2024-04-03",
            "title": "Example Company 2023 Annual Report",
            "company_name": "Example Company",
        }
        manifests = self.root / "manifests"
        manifests.mkdir()
        (manifests / "reports.jsonl").write_text(
            json.dumps(report, ensure_ascii=False) + "\n", encoding="utf-8"
        )

        def fake_ocr(_source: Path, output_dir: Path):
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "output.md").write_text(
                "# Example Company Annual Report\n\nRevenue increased.", encoding="utf-8"
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
                                            "block_content": "Example Company Annual Report",
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
                "collected_financial",
                batch_id="batch-collected",
                ocr_backend="local",
                llm_backend="mock",
            )

        self.assertEqual(result["status"], "success")
        record = load_local_documents(self.root)[report["asset_uid"]]
        self.assertEqual(record["raw_path"], report["raw_path"])
        self.assertEqual(record["source_url"], report["source_url"])
        self.assertEqual(record["source_metadata"]["report_manifest"]["source_id"], "notice-1")
        release_path = self.root / "published/batch-collected" / result["workflow_run_id"]
        verification = LocalDeliveryPublisher(self.root).verify(
            "batch-collected", result["workflow_run_id"]
        )
        self.assertTrue((release_path / "manifest.json").is_file())
        self.assertEqual(verification["status"], "success")

    def test_empty_collected_batch_is_a_safe_terminal_skip(self):
        runner = LocalWorkflowRunner(data_root=self.root)
        result = runner.run(
            "collected_financial", batch_id="batch-empty", ocr_backend="local"
        )

        self.assertEqual(result["status"], "skipped")
        status = runner.status(result["workflow_run_id"])
        self.assertEqual(status["status"], "skipped")
        self.assertEqual(status["stage_runs"][0]["stage_name"], "adopt_collected")

    def test_release_verification_reports_tampering(self):
        release = self.root / "published/batch-a/release-a"
        release.mkdir(parents=True)
        payload = b"verified payload"
        (release / "manifest.json").write_bytes(payload)
        (release / "checksums.sha256").write_text(
            f"{sha256(payload)}  manifest.json\n", encoding="ascii"
        )

        publisher = LocalDeliveryPublisher(self.root)
        self.assertEqual(publisher.verify("batch-a", "release-a")["status"], "success")
        (release / "manifest.json").write_bytes(b"tampered payload")
        result = publisher.verify("batch-a", "release-a")

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["errors"][0]["path"], "manifest.json")

    def test_state_backup_is_consistent_and_operational_queries_are_available(self):
        state_path = self.root / "state/pipeline.db"
        with StateStore(state_path) as store:
            context = PipelineContext.create(self.root, batch_id="batch-state")
            store.start_run(context, dry_run=False)
            store.finish_run(context.run_id, "success")
            workflow_run_id = store.start_workflow_run(
                batch_id="batch-state",
                pipeline_name="collected_financial",
                stages=["adopt_collected"],
                dry_run=False,
            )
            store.finish_workflow_run(workflow_run_id, "success")
            backup = store.backup_to(self.root / "backups/pipeline.sqlite3")
            self.assertEqual(store.list_batches()[0]["batch_id"], "batch-state")
            self.assertEqual(store.list_workflow_runs()[0]["workflow_run_id"], workflow_run_id)

        self.assertEqual(backup["status"], "success")
        backup_connection = sqlite3.connect(backup["backup_path"])
        try:
            count = backup_connection.execute("SELECT COUNT(*) FROM pipeline_batch").fetchone()[0]
            integrity = backup_connection.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            backup_connection.close()
        self.assertEqual(count, 1)
        self.assertEqual(integrity, "ok")


if __name__ == "__main__":
    unittest.main()
