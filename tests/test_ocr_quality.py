import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ingestion.local_documents import LocalDocumentOcrRunner, LocalPdfImporter, load_local_documents
from qc.ocr_quality import OcrQualityReviewService, OcrQualityRunner, load_ocr_quality
from remote.paddle_client import PaddleServingError
from storage.state_store import StateStore
from workflow.cli import main as cli_main


VALID_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj\n<< /Type /Page >>\nendobj\n"
    b"trailer\n<<>>\nstartxref\n0\n%%EOF\n"
    b"%" + b"test-padding\n" * 12
)


class OcrQualityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def _write_local_manifest(self, record: dict) -> None:
        path = self.root / "manifests/local_documents.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")

    def test_quality_warnings_are_reviewable_without_failing_the_stage(self):
        output_dir = self.root / "parsed_md/financial/document-one/asset-one/local"
        output_dir.mkdir(parents=True)
        (output_dir / "output.md").write_text("\ufffd?!", encoding="utf-8")
        (output_dir / "result.json").write_text(
            json.dumps({"layoutParsingResults": [{"prunedResult": {}}]}), encoding="utf-8"
        )
        self._write_local_manifest(
            {
                "asset_uid": "asset-one",
                "document_uid": "document-one",
                "batch_id": "batch-quality",
                "batch_ids": ["batch-quality"],
                "status": "success",
                "ocr_status": "success",
                "ocr_backend": "local",
                "ocr_output_dir": "parsed_md/financial/document-one/asset-one/local",
                "ocr_result": {"page_result_count": 1},
                "page_count": 2,
                "raw_file_hash": "a" * 64,
            }
        )

        result = OcrQualityRunner(self.root).run(batch_id="batch-quality")

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["counts"]["processed"], 1)
        self.assertEqual(result["quality_statuses"], {"needs_review": 1})
        self.assertGreater(result["warning_count"], 0)
        quality = load_ocr_quality(self.root)["asset-one"]
        self.assertEqual(quality["status"], "needs_review")
        self.assertEqual(quality["metrics"]["expected_page_count"], 2)
        self.assertEqual(quality["metrics"]["parsed_page_count"], 1)
        warning_names = {
            check["check_name"] for check in quality["qc_checks"] if check["status"] == "warn"
        }
        self.assertTrue(
            {
                "ocr_text_sufficient",
                "ocr_page_coverage",
                "ocr_replacement_characters",
            }.issubset(warning_names)
        )
        review = OcrQualityReviewService(self.root).review(batch_id="batch-quality")
        self.assertEqual(review["asset_count"], 1)
        self.assertEqual(review["review_count"], 1)
        self.assertEqual(review["items"][0]["asset_uid"], "asset-one")
        self.assertEqual(review["items"][0]["status"], "needs_review")
        with StateStore(self.root / "state/pipeline.db") as store:
            self.assertEqual(store.count("ocr_quality"), 1)

    def test_forced_retry_archives_prior_output_and_records_the_attempt(self):
        source = self.root / "input.pdf"
        source.write_bytes(VALID_PDF)
        LocalPdfImporter(self.root).run(source, batch_id="batch-retry")
        outputs = iter(("First OCR output.", "Replacement OCR output."))

        def fake_ocr(_source: Path, output_dir: Path):
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "output.md").write_text(next(outputs), encoding="utf-8")
            (output_dir / "result.json").write_text(
                json.dumps({"layoutParsingResults": [{"prunedResult": {}}]}), encoding="utf-8"
            )
            return {"page_result_count": 1}

        runner = LocalDocumentOcrRunner(self.root, processor=fake_ocr)
        first = runner.run(batch_id="batch-retry", backend="local")
        retry = runner.run(
            batch_id="batch-retry",
            backend="local",
            force=True,
            retry_reason="quality review found an incomplete page",
        )

        self.assertEqual(first["status"], "success")
        self.assertEqual(retry["status"], "success")
        self.assertEqual(retry["counts"]["processed"], 1)
        record = next(iter(load_local_documents(self.root).values()))
        self.assertEqual(len(record["ocr_attempts"]), 2)
        first_attempt, retry_attempt = record["ocr_attempts"]
        self.assertEqual(first_attempt["status"], "success")
        self.assertEqual(retry_attempt["status"], "success")
        self.assertTrue(retry_attempt["forced"])
        self.assertEqual(retry_attempt["retry_reason"], "quality review found an incomplete page")
        archived = self.root / retry_attempt["archived_output_dir"]
        self.assertTrue(archived.is_dir())
        self.assertEqual((archived / "output.md").read_text(encoding="utf-8"), "First OCR output.")
        current_output = self.root / record["ocr_output_dir"] / "output.md"
        self.assertEqual(current_output.read_text(encoding="utf-8"), "Replacement OCR output.")
        attempt_file = json.loads(
            (self.root / record["ocr_output_dir"] / "attempt.json").read_text(encoding="utf-8")
        )
        self.assertEqual(attempt_file["attempt_uid"], retry_attempt["attempt_uid"])

    def test_single_ocr_failure_does_not_stop_following_documents(self):
        first = self.root / "first.pdf"
        second = self.root / "second.pdf"
        first.write_bytes(VALID_PDF)
        second.write_bytes(VALID_PDF + b"second")
        LocalPdfImporter(self.root).run(first, batch_id="batch-continue")
        LocalPdfImporter(self.root).run(second, batch_id="batch-continue")
        calls = []

        def fake_ocr(source: Path, output_dir: Path):
            calls.append(source.name)
            if len(calls) == 1:
                raise PaddleServingError("serving response was incomplete")
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "output.md").write_text("second OCR output", encoding="utf-8")
            return {"page_result_count": 1}

        result = LocalDocumentOcrRunner(self.root, processor=fake_ocr).run(
            batch_id="batch-continue", backend="local"
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["counts"], {"processed": 1, "skipped": 0, "preview": 0, "failed": 1})
        self.assertEqual(len(calls), 2)
        records = load_local_documents(self.root)
        self.assertEqual(sum(record.get("ocr_status") == "failed" for record in records.values()), 1)
        self.assertEqual(sum(record.get("ocr_status") == "success" for record in records.values()), 1)

    def test_segmented_ocr_artifacts_keep_page_range_lineage(self):
        source = self.root / "input.pdf"
        source.write_bytes(VALID_PDF)
        LocalPdfImporter(self.root).run(source, batch_id="batch-segments")

        def fake_ocr(_source: Path, output_dir: Path):
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "output.md").write_text("complete OCR", encoding="utf-8")
            (output_dir / "result.json").write_text(
                json.dumps({"layoutParsingResults": [{}]}), encoding="utf-8"
            )
            segment_path = output_dir / "segments/segment-0001/result.json"
            segment_path.parent.mkdir(parents=True)
            segment_path.write_text(
                json.dumps({"layoutParsingResults": [{}]}), encoding="utf-8"
            )
            (output_dir / "ocr_segments.json").write_text(
                json.dumps(
                    {
                        "segments": [
                            {
                                "segment_uid": "segment-0001",
                                "segment_index": 0,
                                "start_page": 1,
                                "end_page": 1,
                                "status": "success",
                                "result_path": "segments/segment-0001/result.json",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            return {"page_result_count": 1, "segment_count": 1}

        result = LocalDocumentOcrRunner(self.root, processor=fake_ocr).run(
            batch_id="batch-segments", backend="local"
        )

        self.assertEqual(result["status"], "success")
        with StateStore(self.root / "state/pipeline.db") as store:
            artifact = store.fetch_one(
                "SELECT parent_artifact_uid, metadata_json FROM artifact "
                "WHERE artifact_type = 'ocr-output-segment'"
            )
        self.assertTrue(artifact["parent_artifact_uid"])
        self.assertIn('"start_page": 1', artifact["metadata_json"])
        self.assertIn('"end_page": 1', artifact["metadata_json"])

    def test_retry_command_returns_failure_without_refreshing_quality(self):
        with (
            patch("workflow.cli.LocalDocumentOcrRunner") as ocr_runner,
            patch("workflow.cli.OcrQualityRunner") as quality_runner,
            patch("workflow.cli._print"),
        ):
            ocr_runner.return_value.run.return_value = {
                "status": "failed",
                "errors": [{"asset_uid": "asset-one", "error_msg": "temporary failure"}],
            }

            exit_code = cli_main(
                [
                    "--data-root",
                    str(self.root),
                    "ocr-retry",
                    "--batch-id",
                    "batch-retry",
                    "--asset-id",
                    "asset-one",
                    "--ocr-backend",
                    "local",
                    "--reason",
                    "retry after local review",
                ]
            )

        self.assertEqual(exit_code, 1)
        quality_runner.return_value.run.assert_not_called()

    def test_quality_command_returns_failure_status(self):
        with patch("workflow.cli.OcrQualityRunner") as quality_runner, patch("workflow.cli._print"):
            quality_runner.return_value.run.return_value = {
                "status": "failed",
                "errors": [{"asset_uid": "asset-one", "error_msg": "unreadable output"}],
            }

            exit_code = cli_main(
                ["--data-root", str(self.root), "ocr-quality", "--batch-id", "batch-quality"]
            )

        self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()
