import tempfile
import unittest
from pathlib import Path

from core.ids import raw_asset_uid
from spiders.collector import CollectionRunner
from spiders.source_adapters import CollectionTarget, ReportCandidate
from spiders.downloader import sha256_file


class FakeAdapter:
    def __init__(self, name, priority, candidates):
        self.name = name
        self.priority = priority
        self.candidates = candidates

    def discover(self, _target):
        return self.candidates


class FakeDownloader:
    VALID_PDF = (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Page >>\nendobj\n"
        b"trailer\n<<>>\nstartxref\n0\n%%EOF\n"
        b"%" + b"test-padding\n" * 12
    )

    def __init__(self, data_root):
        self.calls = []
        self.data_root = Path(data_root)

    def download(self, spec):
        self.calls.append(spec)
        if spec.source_name == "cninfo":
            return {"status": "failed", "error_msg": "simulated source failure"}
        path = self.data_root / "raw_pdfs/2023/600519/report.pdf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.VALID_PDF)
        raw_hash = sha256_file(path)
        return {
            "status": "success",
            "report_uid": spec.canonical_uid,
            "candidate_uid": spec.candidate_uid,
            "source_name": spec.source_name,
            "source_id": spec.source_id,
            "source_url": spec.source_url,
            "raw_file_hash": raw_hash,
            "asset_uid": raw_asset_uid(raw_hash),
            "raw_path": "raw_pdfs/2023/600519/report.pdf",
            "file_size": len(self.VALID_PDF),
            "page_count": 1,
        }


class CollectorTests(unittest.TestCase):
    def test_primary_failure_falls_back_to_secondary_source(self):
        target = CollectionTarget("600519", 2023, 2023)
        primary = ReportCandidate(
            source_name="cninfo",
            source_id="cninfo-1",
            stock_code="600519",
            report_year=2023,
            report_type="annual",
            title="贵州茅台2023年年度报告",
            publish_date="2024-04-03",
            source_url="https://static.cninfo.com.cn/report.pdf",
            priority=10,
            discovered_at="2026-08-08T00:00:00+00:00",
        )
        backup = ReportCandidate(
            source_name="sse",
            source_id="sse-1",
            stock_code="600519",
            report_year=2023,
            report_type="annual",
            title="贵州茅台2023年年度报告",
            publish_date="2024-04-03",
            source_url="https://www.sse.com.cn/report.pdf",
            priority=20,
            discovered_at="2026-08-08T00:00:00+00:00",
        )

        with tempfile.TemporaryDirectory() as directory:
            downloader = FakeDownloader(directory)
            runner = CollectionRunner(
                data_root=Path(directory),
                adapters=[FakeAdapter("cninfo", 10, [primary]), FakeAdapter("sse", 20, [backup])],
                downloader=downloader,
                retry_attempts=1,
            )
            summary = runner.run([target])

        self.assertEqual(summary["status_counts"], {"success": 1})
        self.assertEqual([spec.source_name for spec in downloader.calls], ["cninfo", "sse"])


if __name__ == "__main__":
    unittest.main()
