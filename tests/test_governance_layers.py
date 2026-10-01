import tempfile
import unittest
from pathlib import Path

from core.context import PipelineContext
from core.ids import artifact_uid, logical_report_uid, raw_asset_uid
from qc.validators import qc_passed, validate_asset, validate_candidate
from spiders.source_adapters import ReportCandidate
from storage.state_store import StateStore


VALID_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj\n<< /Type /Page >>\nendobj\n"
    b"trailer\n<<>>\nstartxref\n0\n%%EOF\n"
    b"%" + b"test-padding\n" * 12
)


class GovernanceLayerTests(unittest.TestCase):
    def candidate(self):
        return ReportCandidate(
            source_name="cninfo",
            source_id="1219506510",
            stock_code="600519",
            report_year=2023,
            report_type="annual",
            title="贵州茅台2023年年度报告",
            publish_date="2024-04-03",
            source_url="https://static.cninfo.com.cn/report.pdf",
            priority=10,
            discovered_at="2026-08-08T00:00:00+00:00",
        )

    def test_logical_report_id_is_independent_from_raw_asset_id(self):
        self.assertEqual(
            logical_report_uid("600519", 2023, "annual"),
            self.candidate().canonical_uid,
        )
        self.assertNotEqual(raw_asset_uid("hash-a"), raw_asset_uid("hash-b"))

    def test_candidate_and_asset_qc(self):
        candidate = self.candidate()
        candidate_checks = validate_candidate(candidate)
        self.assertTrue(qc_passed(candidate_checks))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "raw_pdfs/2023/600519/report.pdf"
            path.parent.mkdir(parents=True)
            path.write_bytes(VALID_PDF)
            import hashlib

            raw_hash = hashlib.sha256(VALID_PDF).hexdigest()
            asset_checks = validate_asset(
                {
                    "raw_path": "raw_pdfs/2023/600519/report.pdf",
                    "raw_file_hash": raw_hash,
                    "file_size": len(VALID_PDF),
                },
                root,
            )
            self.assertTrue(qc_passed(asset_checks))

    def test_state_store_records_run_step_asset_and_qc(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = PipelineContext.create(root)
            with StateStore(root / "state/pipeline.db") as store:
                store.start_run(context, dry_run=False)
                step_id = store.start_step(context, "download", "candidate-1")
                store.finish_step(step_id, "success")
                store.upsert_asset(
                    context,
                    {
                        "asset_uid": "asset-1",
                        "report_uid": "report-1",
                        "candidate_uid": "candidate-1",
                        "source_url": "https://example.com/report.pdf",
                        "raw_file_hash": "hash-1",
                        "status": "success",
                    },
                )
                store.record_qc(
                    context,
                    "asset-1",
                    "raw_asset",
                    [{"check_name": "pdf_structure", "status": "pass", "value": {"pages": 1}}],
                )
                store.finish_run(context.run_id, "success")

                self.assertEqual(store.count("pipeline_run"), 1)
                self.assertEqual(store.count("pipeline_step"), 1)
                self.assertEqual(store.count("report_asset"), 1)
                self.assertEqual(store.count("qc_result"), 1)
                row = store.fetch_one("SELECT status FROM pipeline_run WHERE run_id = ?", (context.run_id,))
                self.assertEqual(row["status"], "success")

    def test_batch_id_is_reused_across_run_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = PipelineContext.create(root, batch_id="batch-local-practice")
            second = PipelineContext.create(root, batch_id="batch-local-practice")
            self.assertEqual(first.batch_id, second.batch_id)
            self.assertNotEqual(first.run_id, second.run_id)

            with StateStore(root / "state/pipeline.db") as store:
                store.start_run(first, dry_run=False)
                store.finish_run(first.run_id, "partial")
                store.start_run(second, dry_run=False)
                store.finish_run(second.run_id, "success")

                rows = store.connection.execute(
                    "SELECT attempt, batch_id FROM pipeline_run ORDER BY attempt"
                ).fetchall()
                self.assertEqual([row["attempt"] for row in rows], [1, 2])
                self.assertEqual(store.get_batch(first.batch_id)["status"], "success")

    def test_document_registry_and_artifact_are_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = PipelineContext.create(root, batch_id="batch-metadata")
            with StateStore(root / "state/pipeline.db") as store:
                store.start_run(context, dry_run=False)
                store.upsert_document(
                    context,
                    {
                        "document_uid": "doc-1",
                        "document_type": "financial-report",
                        "title": "Annual report",
                        "metadata": {"stock_code": "600519"},
                    },
                )
                store.upsert_artifact(
                    context,
                    {
                        "artifact_uid": artifact_uid("raw-pdf", "asset-1"),
                        "artifact_type": "raw-pdf",
                        "document_uid": "doc-1",
                        "asset_uid": "asset-1",
                        "path": "raw_pdfs/report.pdf",
                        "sha256": "hash-1",
                    },
                )
                store.upsert_artifact(
                    context,
                    {
                        "artifact_uid": artifact_uid("raw-pdf", "asset-1"),
                        "artifact_type": "raw-pdf",
                        "document_uid": "doc-1",
                        "asset_uid": "asset-1",
                        "path": "raw_pdfs/report.pdf",
                        "sha256": "hash-1",
                    },
                )
                self.assertEqual(store.count("document_registry"), 1)
                self.assertEqual(store.count("artifact"), 1)


if __name__ == "__main__":
    unittest.main()
