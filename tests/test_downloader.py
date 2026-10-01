import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from spiders.downloader import ReportDownloader, ReportSpec, inspect_pdf


VALID_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj\n<< /Type /Page >>\nendobj\n"
    b"trailer\n<<>>\nstartxref\n0\n%%EOF\n"
    b"%" + b"test-padding\n" * 12
)
INVALID_PDF = b"this is an html block page, not a pdf"


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body
        self.offset = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self.body)
        chunk = self.body[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


class DownloaderTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_root = Path(self.temp_dir.name) / "data"

    def tearDown(self):
        self.temp_dir.cleanup()

    def spec(self, url="https://example.test/report.pdf"):
        return ReportSpec(
            stock_code="600519",
            report_year=2023,
            source_url=url,
            title="测试年度报告",
        )

    def test_inspect_pdf(self):
        path = Path(self.temp_dir.name) / "report.pdf"
        path.write_bytes(VALID_PDF)

        inspection = inspect_pdf(path)

        self.assertTrue(inspection.valid)
        self.assertEqual(inspection.page_count, 1)

    def test_download_writes_raw_file_and_manifest(self):
        downloader = ReportDownloader(self.data_root)
        with patch("spiders.downloader.urlopen", return_value=FakeResponse(VALID_PDF)):
            result = downloader.download(self.spec())

        self.assertEqual(result["status"], "success")
        raw_path = self.data_root / result["raw_path"]
        self.assertTrue(raw_path.exists())
        self.assertEqual(raw_path.read_bytes(), VALID_PDF)
        manifest_path = self.data_root / "manifests" / "reports.jsonl"
        records = [json.loads(line) for line in manifest_path.read_text().splitlines()]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["report_uid"], result["report_uid"])

    def test_same_url_is_idempotent(self):
        downloader = ReportDownloader(self.data_root)
        with patch("spiders.downloader.urlopen", return_value=FakeResponse(VALID_PDF)) as mocked:
            first = downloader.download(self.spec())
            second = downloader.download(self.spec())

        self.assertEqual(first["status"], "success")
        self.assertEqual(second["status"], "skipped")
        mocked.assert_called_once()

    def test_batch_association_is_additive_for_reused_assets(self):
        downloader = ReportDownloader(self.data_root)
        with patch("spiders.downloader.urlopen", return_value=FakeResponse(VALID_PDF)):
            first = downloader.download(self.spec())
        downloader.associate_batch(first, "batch-one")
        reused = downloader.download(self.spec())
        associated = downloader.associate_batch(reused, "batch-two")

        manifest_path = self.data_root / "manifests" / "reports.jsonl"
        record = json.loads(manifest_path.read_text(encoding="utf-8").strip())
        self.assertEqual(reused["status"], "skipped")
        self.assertEqual(associated["batch_ids"], ["batch-one", "batch-two"])
        self.assertEqual(record["status"], "success")
        self.assertEqual(record["batch_ids"], ["batch-one", "batch-two"])

    def test_same_content_from_another_url_is_deduplicated(self):
        downloader = ReportDownloader(self.data_root)
        with patch("spiders.downloader.urlopen", side_effect=[FakeResponse(VALID_PDF), FakeResponse(VALID_PDF)]):
            first = downloader.download(self.spec("https://example.test/first.pdf"))
            second = downloader.download(self.spec("https://example.test/second.pdf"))

        self.assertEqual(first["status"], "success")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(first["raw_path"], second["raw_path"])

    def test_invalid_file_goes_to_quarantine(self):
        downloader = ReportDownloader(self.data_root)
        with patch("spiders.downloader.urlopen", return_value=FakeResponse(INVALID_PDF)):
            result = downloader.download(self.spec())

        self.assertEqual(result["status"], "failed")
        quarantine_path = self.data_root / result["quarantine_path"]
        self.assertTrue(quarantine_path.exists())
        self.assertFalse((self.data_root / "raw_pdfs").exists())


if __name__ == "__main__":
    unittest.main()
