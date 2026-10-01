import io
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError

from spiders.scholarly_downloader import ScholarlyPdfDownloader


class FakeHttpClient:
    def __init__(self, payload: bytes, errors=None):
        self.payload = payload
        self.errors = list(errors or [])
        self.calls = []

    def request_bytes(self, url, *, headers=None):
        self.calls.append((url, headers))
        if self.errors:
            error = self.errors.pop(0)
            if isinstance(error, Exception):
                raise error
        return self.payload


class PayloadQueueHttpClient:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def request_bytes(self, url, *, headers=None):
        self.calls.append(url)
        return self.payloads.pop(0)


VALID_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj\n<< /Type /Page >>\nendobj\n"
    b"2 0 obj\n<< /Type /Page >>\nendobj\n"
    b"trailer\n<<>>\nstartxref\n0\n%%EOF\n"
    b"%" + b"test-padding\n" * 12
)


def pdf_variant(seed: bytes) -> bytes:
    return VALID_PDF + b"%variant-" + seed + b"\n"


def candidate(**overrides):
    value = {
        "work_uid": "work-123",
        "candidate_uid": "candidate-123",
        "source_name": "openalex",
        "source_id": "W123",
        "source_url": "https://journal.example/article",
        "pdf_url": "https://journal.example/article.pdf",
        "title": "Financial reporting and governance",
        "published_date": "2024-05-01",
        "open_access": True,
    }
    value.update(overrides)
    return value


def http_error(code: int) -> HTTPError:
    return HTTPError(
        "https://journal.example/article.pdf",
        code,
        f"HTTP {code}",
        {},
        io.BytesIO(b""),
    )


class ScholarlyDownloaderTests(unittest.TestCase):
    def test_open_access_pdf_is_saved_and_second_run_is_idempotent(self):
        http = FakeHttpClient(VALID_PDF)
        with tempfile.TemporaryDirectory() as directory:
            downloader = ScholarlyPdfDownloader(directory, http_client=http)
            first = downloader.download(candidate())
            second = downloader.download(candidate())

            self.assertEqual(first["status"], "success")
            self.assertEqual(second["status"], "skipped")
            self.assertEqual(len(http.calls), 1)
            self.assertTrue((Path(directory) / first["raw_path"]).exists())
            self.assertTrue((Path(directory) / first["named_path"]).exists())
            self.assertEqual(first["named_path"], second["named_path"])
            named_name = Path(first["named_path"]).name
            self.assertTrue(named_name.endswith(".pdf"))
            self.assertEqual(named_name, "0.pdf")
            self.assertEqual(
                (Path(directory) / first["named_path"]).read_bytes(),
                VALID_PDF,
            )
            self.assertTrue(
                (Path(directory) / "manifests/scholarly_documents.jsonl").exists()
            )

    def test_changed_pdf_url_is_downloaded_again(self):
        http = FakeHttpClient(VALID_PDF)
        with tempfile.TemporaryDirectory() as directory:
            downloader = ScholarlyPdfDownloader(directory, http_client=http)
            first = downloader.download(candidate())
            changed = downloader.download(
                candidate(
                    pdf_url="https://journal.example/revised.pdf",
                    source_updated_at="2024-06-01",
                )
            )

            self.assertEqual(changed["status"], "duplicate")
            self.assertEqual(len(http.calls), 2)
            self.assertTrue((Path(directory) / changed["named_path"]).exists())
            self.assertEqual(changed["named_path"], first["named_path"])

    def test_named_copies_use_sequential_numbers_from_zero(self):
        http = PayloadQueueHttpClient(
            [pdf_variant(b"a"), pdf_variant(b"b"), pdf_variant(b"c")]
        )
        with tempfile.TemporaryDirectory() as directory:
            downloader = ScholarlyPdfDownloader(directory, http_client=http)
            first = downloader.download(
                candidate(
                    pdf_url="https://journal.example/a.pdf",
                    candidate_uid="candidate-a",
                    work_uid="work-a",
                )
            )
            second = downloader.download(
                candidate(
                    pdf_url="https://journal.example/b.pdf",
                    candidate_uid="candidate-b",
                    work_uid="work-b",
                )
            )

            self.assertEqual(Path(first["named_path"]).name, "0.pdf")
            self.assertEqual(Path(second["named_path"]).name, "1.pdf")

            reloaded = ScholarlyPdfDownloader(directory, http_client=http)
            third = reloaded.download(
                candidate(
                    pdf_url="https://journal.example/c.pdf",
                    candidate_uid="candidate-c",
                    work_uid="work-c",
                )
            )

            self.assertEqual(Path(third["named_path"]).name, "2.pdf")

    def test_non_open_access_or_missing_pdf_stays_metadata_only(self):
        http = FakeHttpClient(VALID_PDF)
        with tempfile.TemporaryDirectory() as directory:
            downloader = ScholarlyPdfDownloader(directory, http_client=http)
            self.assertEqual(
                downloader.download(candidate(open_access=False))["status"],
                "metadata_only",
            )
            self.assertEqual(
                downloader.download(candidate(pdf_url=None))["status"],
                "metadata_only",
            )
            self.assertEqual(http.calls, [])

    def test_html_at_pdf_url_is_quarantined(self):
        http = FakeHttpClient(b"<html>login required</html>" + b"x" * 120)
        with tempfile.TemporaryDirectory() as directory:
            result = ScholarlyPdfDownloader(directory, http_client=http).download(candidate())

            self.assertEqual(result["status"], "failed")
            self.assertIn("PDF", result["error_msg"])
            self.assertTrue((Path(directory) / result["quarantine_path"]).exists())

    def test_forbidden_pdf_retries_once_with_browser_headers(self):
        http = FakeHttpClient(VALID_PDF, errors=[http_error(403)])
        with tempfile.TemporaryDirectory() as directory:
            result = ScholarlyPdfDownloader(directory, http_client=http).download(
                candidate()
            )

            self.assertEqual(result["status"], "success")
            self.assertEqual(len(http.calls), 2)
            fallback_headers = http.calls[1][1]
            self.assertIn("Referer", fallback_headers)
            self.assertIn("Mozilla", fallback_headers["User-Agent"])

    def test_repeated_forbidden_pdf_is_blocked_without_more_retries(self):
        http = FakeHttpClient(VALID_PDF, errors=[http_error(403), http_error(403)])
        with tempfile.TemporaryDirectory() as directory:
            result = ScholarlyPdfDownloader(
                directory, http_client=http, download_attempts=5
            ).download(candidate())

            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["failure_type"], "blocked")
            self.assertEqual(result["http_status"], 403)
            self.assertEqual(len(http.calls), 2)

    def test_known_blocked_pdf_is_not_downloaded_again(self):
        http = FakeHttpClient(VALID_PDF, errors=[http_error(403), http_error(403)])
        with tempfile.TemporaryDirectory() as directory:
            downloader = ScholarlyPdfDownloader(directory, http_client=http)
            first = downloader.download(candidate())
            second = downloader.download(candidate())

            self.assertEqual(first["status"], "blocked")
            self.assertEqual(second["status"], "blocked")
            self.assertTrue(second["skipped"])
            self.assertEqual(len(http.calls), 2)

    def test_retryable_http_error_recovers_after_backoff(self):
        http = FakeHttpClient(
            VALID_PDF, errors=[http_error(503), http_error(503)]
        )
        with tempfile.TemporaryDirectory() as directory:
            result = ScholarlyPdfDownloader(
                directory,
                http_client=http,
                download_attempts=3,
                download_backoff_seconds=0,
            ).download(candidate())

            self.assertEqual(result["status"], "success")
            self.assertEqual(len(http.calls), 3)

    def test_retryable_http_error_is_failed_after_attempts(self):
        http = FakeHttpClient(
            VALID_PDF,
            errors=[http_error(503), http_error(503), http_error(503)],
        )
        with tempfile.TemporaryDirectory() as directory:
            result = ScholarlyPdfDownloader(
                directory,
                http_client=http,
                download_attempts=3,
                download_backoff_seconds=0,
            ).download(candidate())

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["failure_type"], "retryable")
            self.assertEqual(result["http_status"], 503)
            self.assertEqual(len(http.calls), 3)


if __name__ == "__main__":
    unittest.main()
