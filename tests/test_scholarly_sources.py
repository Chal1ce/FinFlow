import json
import tempfile
import unittest
from pathlib import Path

from spiders.scholarly_collector import ScholarlyCollector, build_runner
from spiders.scholarly_sources import (
    CrossrefAdapter,
    OpenAlexAdapter,
    ScholarlyCandidate,
    ScholarlySourceAdapter,
    ScholarlyTarget,
)
from remote.paddle_cloud_client import PaddleOCRQueueBusyError
from storage.state_store import StateStore


class FakeHttpClient:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.urls = []

    def request_bytes(self, url, *, headers=None):
        self.urls.append(url)
        if not self.payloads:
            raise AssertionError("unexpected additional HTTP request")
        return json.dumps(self.payloads.pop(0), ensure_ascii=False).encode("utf-8")


class FakeScholarlyAdapter(ScholarlySourceAdapter):
    name = "fake-scholar"
    priority = 1

    def __init__(self, candidate):
        self.candidate = candidate
        self.updated_since_values = []
        super().__init__(FakeHttpClient([]))

    def discover(self, target, *, updated_since=None, cursor=None):
        self.updated_since_values.append(updated_since)
        return [self.candidate]


class FakePdfDownloader:
    def __init__(self, data_root, results=None):
        self.data_root = Path(data_root)
        self.results = list(results or [])
        self.calls = []

    def download(self, candidate):
        self.calls.append(candidate["candidate_uid"])
        if self.results:
            return self.results.pop(0)
        work_uid = str(candidate["work_uid"])
        path = (
            self.data_root
            / "raw_pdfs/scholarly/2024"
            / work_uid
            / f"{work_uid}_hash.pdf"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-test")
        named_path = (
            self.data_root
            / "named_pdfs/scholarly/2024"
            / work_uid
            / "0.pdf"
        )
        named_path.parent.mkdir(parents=True, exist_ok=True)
        named_path.write_bytes(b"%PDF-test")
        return {
            "status": "success",
            "work_uid": work_uid,
            "candidate_uid": candidate["candidate_uid"],
            "source_name": candidate["source_name"],
            "source_id": candidate["source_id"],
            "source_url": candidate["pdf_url"],
            "asset_uid": f"asset-{work_uid}",
            "raw_file_hash": "hash",
            "raw_path": (
                f"raw_pdfs/scholarly/2024/{work_uid}/{work_uid}_hash.pdf"
            ),
            "named_path": (
                f"named_pdfs/scholarly/2024/{work_uid}/0.pdf"
            ),
            "named_number": 0,
            "file_size": 9,
            "page_count": 1,
        }


def sample_candidate(
    *,
    title="Financial reporting and governance",
    source_id=None,
    doi=None,
):
    source_id = source_id or "doi:10.1000/example"
    return ScholarlyCandidate(
        source_name="fake-scholar",
        source_id=source_id,
        title=title,
        work_type="journal-article",
        published_date="2024-05-01",
        source_updated_at="2024-05-02",
        source_url="https://doi.org/10.1000/example",
        pdf_url="https://publisher.example/example.pdf",
        doi=doi or "10.1000/example",
        journal_title="Journal of Financial Data",
        journal_issn="1234-5678",
        publisher="Example Publisher",
        abstract="An abstract.",
        authors=({"name": "A. Author"},),
        open_access=True,
        citation_count=4,
        priority=1,
        discovered_at="2026-08-08T00:00:00+00:00",
    )


class ScholarlySourceTests(unittest.TestCase):
    target = ScholarlyTarget("finance", "financial reporting", 2023, 2024, max_results=10)

    def test_crossref_parses_doi_journal_authors_and_pdf(self):
        payload = {
            "message": {
                "items": [
                    {
                        "DOI": "10.1000/Example",
                        "title": ["<jats:title>Financial reporting</jats:title>"],
                        "type": "journal-article",
                        "published-online": {"date-parts": [[2024, 5, 1]]},
                        "indexed": {"date-time": "2024-05-02T00:00:00Z"},
                        "URL": "https://doi.org/10.1000/Example",
                        "container-title": ["Journal of Finance"],
                        "ISSN": ["1234-5678"],
                        "publisher": "Example Publisher",
                        "abstract": "<jats:p>An abstract.</jats:p>",
                        "author": [{"given": "A", "family": "Author"}],
                        "link": [{"content-type": "application/pdf", "URL": "https://example.org/a.pdf"}],
                        "license": [{"URL": "https://creativecommons.org/licenses/by/4.0/"}],
                        "is-referenced-by-count": 8,
                    }
                ],
                "next-cursor": "",
            }
        }
        adapter = CrossrefAdapter(FakeHttpClient([payload]))

        candidates = adapter.discover(self.target)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].doi, "10.1000/example")
        self.assertEqual(candidates[0].journal_title, "Journal of Finance")
        self.assertEqual(candidates[0].pdf_url, "https://example.org/a.pdf")
        self.assertEqual(candidates[0].abstract, "An abstract.")
        self.assertEqual(candidates[0].authors[0]["family"], "Author")

    def test_openalex_reconstructs_abstract_and_uses_open_access_location(self):
        payload = {
            "results": [
                {
                    "id": "https://openalex.org/W123",
                    "doi": "https://doi.org/10.1000/Example",
                    "title": "Financial reporting",
                    "type": "article",
                    "publication_date": "2024-05-01",
                    "updated_date": "2024-05-03",
                    "primary_location": {
                        "landing_page_url": "https://journal.example/article",
                        "source": {
                            "display_name": "Journal of Finance",
                            "issn": ["1234-5678"],
                            "host_organization_name": "Example Publisher",
                        },
                    },
                    "best_oa_location": {
                        "landing_page_url": "https://oa.example/article",
                        "pdf_url": "https://oa.example/article.pdf",
                    },
                    "open_access": {"is_oa": True},
                    "abstract_inverted_index": {"Financial": [1], "reporting": [0]},
                    "authorships": [{"author": {"display_name": "A. Author"}}],
                    "cited_by_count": 9,
                }
            ],
            "meta": {"next_cursor": None},
        }
        adapter = OpenAlexAdapter(FakeHttpClient([payload]))

        candidates = adapter.discover(self.target)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].work_uid, sample_candidate().work_uid)
        self.assertEqual(candidates[0].abstract, "reporting Financial")
        self.assertEqual(candidates[0].pdf_url, "https://oa.example/article.pdf")
        self.assertTrue(candidates[0].open_access)

    def test_max_results_override_applies_to_all_targets(self):
        config = {
            "data_root": "data",
            "http": {"timeout_seconds": 5, "min_interval_seconds": 0},
            "scholarly": {
                "enabled": True,
                "download_open_access_pdfs": False,
                "sources": [
                    {"name": "crossref", "enabled": True, "priority": 30},
                    {"name": "openalex", "enabled": True, "priority": 40},
                ],
                "targets": [
                    {
                        "name": "finance",
                        "query": "financial reporting",
                        "start_year": 2024,
                        "end_year": 2024,
                        "max_results": 50,
                    }
                ],
            },
        }

        _, targets = build_runner(config, max_results_override=20)

        self.assertEqual([target.max_results for target in targets], [20])

    def test_build_runner_reads_ocr_limit_config(self):
        config = {
            "data_root": "data",
            "http": {"timeout_seconds": 5, "min_interval_seconds": 0},
            "scholarly": {
                "enabled": True,
                "download_open_access_pdfs": False,
                "ocr_max_per_run": 5,
                "ocr_delay_seconds": 2,
                "sources": [
                    {"name": "crossref", "enabled": True, "priority": 30},
                    {"name": "openalex", "enabled": True, "priority": 40},
                ],
                "targets": [
                    {
                        "name": "finance",
                        "query": "financial reporting",
                        "start_year": 2024,
                        "end_year": 2024,
                    }
                ],
            },
        }

        runner, _ = build_runner(config)

        self.assertEqual(runner.ocr_max_per_run, 5)
        self.assertEqual(runner.ocr_delay_seconds, 2)


class ScholarlyIncrementalTests(unittest.TestCase):
    def test_second_sync_marks_same_metadata_unchanged(self):
        target = ScholarlyTarget("finance", "financial reporting", 2023, 2024)
        candidate = sample_candidate()
        adapter = FakeScholarlyAdapter(candidate)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with StateStore(root / "state.db") as state_store:
                runner = ScholarlyCollector(
                    root, [adapter], retry_attempts=1, state_store=state_store
                )
                first = runner.run([target])
                second = runner.run([target])
                self.assertEqual(first["change_counts"]["new"], 1)
                self.assertEqual(second["change_counts"]["unchanged"], 1)
                self.assertIsNone(adapter.updated_since_values[0])
                self.assertIsNotNone(adapter.updated_since_values[1])
                self.assertEqual(state_store.count("scholarly_work"), 1)
                self.assertEqual(state_store.count("scholarly_source_record"), 1)
                self.assertEqual(state_store.count("source_sync_state"), 1)

    def test_downloaded_pdf_can_be_sent_to_ocr_processor(self):
        target = ScholarlyTarget("finance", "financial reporting", 2023, 2024)
        candidate = sample_candidate()
        adapter = FakeScholarlyAdapter(candidate)
        ocr_calls = []

        def fake_ocr(pdf_path, output_path):
            ocr_calls.append((pdf_path, output_path))
            output_path.mkdir(parents=True, exist_ok=True)
            (output_path / "output.md").write_text("# parsed", encoding="utf-8")
            return {"markdown_path": str(output_path / "output.md")}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf_downloader = FakePdfDownloader(root)
            with StateStore(root / "state.db") as state_store:
                runner = ScholarlyCollector(
                    root,
                    [adapter],
                    retry_attempts=1,
                    state_store=state_store,
                    pdf_downloader=pdf_downloader,
                    ocr_processor=fake_ocr,
                    ocr_root=root / "parsed_md/scholarly",
                    ocr_backend="local",
                )
                summary = runner.run([target])

                self.assertEqual(summary["pdf_status_counts"], {"success": 1})
                self.assertEqual(summary["ocr_status_counts"], {"success": 1})
                self.assertEqual(len(ocr_calls), 1)
                self.assertEqual(ocr_calls[0][0].name, "0.pdf")
                self.assertIn("named_pdfs", str(ocr_calls[0][0]))
                self.assertTrue((ocr_calls[0][1] / "input.json").exists())
                self.assertEqual(state_store.count("scholarly_asset"), 1)

                discovery_line = (
                    root / "discovery/scholarly_candidates.jsonl"
                ).read_text(encoding="utf-8").strip().splitlines()[-1]
                discovery_record = json.loads(discovery_line)
                self.assertEqual(
                    discovery_record["ocr_input_path"],
                    f"named_pdfs/scholarly/2024/{candidate.work_uid}/0.pdf",
                )
                self.assertEqual(
                    discovery_record["pdf_named_path"],
                    f"named_pdfs/scholarly/2024/{candidate.work_uid}/0.pdf",
                )

    def test_cloud_queue_busy_marks_run_partial_and_pending_retry(self):
        target = ScholarlyTarget("finance", "financial reporting", 2023, 2024)
        candidate = sample_candidate()
        adapter = FakeScholarlyAdapter(candidate)

        def busy_ocr(pdf_path, output_path):
            raise PaddleOCRQueueBusyError("任务提交队列已满，请稍后重试")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf_downloader = FakePdfDownloader(root)
            runner = ScholarlyCollector(
                root,
                [adapter],
                retry_attempts=1,
                pdf_downloader=pdf_downloader,
                ocr_processor=busy_ocr,
                ocr_root=root / "parsed_md/scholarly",
                ocr_backend="cloud",
            )
            summary = runner.run([target])

            self.assertEqual(summary["status"], "partial")
            self.assertEqual(summary["ocr_status_counts"], {"pending_retry": 1})
            self.assertEqual(summary["processing_errors"][0]["retryable"], True)

    def test_blocked_pdf_marks_run_failed_with_permanent_error(self):
        target = ScholarlyTarget("finance", "financial reporting", 2023, 2024)
        candidate = sample_candidate()
        adapter = FakeScholarlyAdapter(candidate)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf_downloader = FakePdfDownloader(
                root,
                results=[
                    {
                        "status": "blocked",
                        "failure_type": "blocked",
                        "error_msg": "HTTPError: HTTP Error 403: Forbidden",
                    }
                ],
            )

            summary = ScholarlyCollector(
                root,
                [adapter],
                retry_attempts=1,
                pdf_downloader=pdf_downloader,
            ).run([target])

            self.assertEqual(summary["status"], "failed")
            self.assertEqual(summary["pdf_status_counts"], {"blocked": 1})
            self.assertEqual(summary["processing_errors"][0]["retryable"], False)
            self.assertEqual(
                summary["processing_errors"][0]["failure_type"], "blocked"
            )

    def test_known_blocked_pdf_does_not_fail_run(self):
        target = ScholarlyTarget("finance", "financial reporting", 2023, 2024)
        candidate = sample_candidate()
        adapter = FakeScholarlyAdapter(candidate)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf_downloader = FakePdfDownloader(
                root,
                results=[
                    {
                        "status": "blocked",
                        "skipped": True,
                        "failure_type": "blocked",
                        "error_msg": "HTTPError: HTTP Error 403: Forbidden",
                    }
                ],
            )

            summary = ScholarlyCollector(
                root,
                [adapter],
                retry_attempts=1,
                pdf_downloader=pdf_downloader,
            ).run([target])

            self.assertEqual(summary["status"], "success")
            self.assertEqual(summary["pdf_status_counts"], {"blocked": 1})
            self.assertEqual(summary["processing_errors"], [])

    def test_blocked_pdf_with_successful_work_is_partial(self):
        target = ScholarlyTarget("finance", "financial reporting", 2023, 2024)
        first = sample_candidate(
            title="First paper",
            source_id="doi:10.1000/first",
            doi="10.1000/first",
        )
        second = sample_candidate(
            title="Second paper",
            source_id="doi:10.1000/second",
            doi="10.1000/second",
        )
        adapters = [FakeScholarlyAdapter(first), FakeScholarlyAdapter(second)]

        def fake_ocr(pdf_path, output_path):
            output_path.mkdir(parents=True, exist_ok=True)
            (output_path / "output.md").write_text("# parsed", encoding="utf-8")
            return {"markdown_path": str(output_path / "output.md")}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            named_first = root / "named_pdfs/scholarly/2024/first/0.pdf"
            named_first.parent.mkdir(parents=True, exist_ok=True)
            named_first.write_bytes(b"%PDF-test")
            pdf_downloader = FakePdfDownloader(
                root,
                results=[
                    {
                        "status": "success",
                        "work_uid": first.work_uid,
                        "candidate_uid": first.candidate_uid,
                        "asset_uid": "asset-first",
                        "raw_path": "raw_pdfs/scholarly/2024/first/first_hash.pdf",
                        "named_path": "named_pdfs/scholarly/2024/first/0.pdf",
                        "named_number": 0,
                    },
                    {
                        "status": "blocked",
                        "failure_type": "blocked",
                        "error_msg": "HTTPError: HTTP Error 403: Forbidden",
                    },
                ],
            )
            runner = ScholarlyCollector(
                root,
                adapters,
                retry_attempts=1,
                pdf_downloader=pdf_downloader,
                ocr_processor=fake_ocr,
                ocr_root=root / "parsed_md/scholarly",
                ocr_backend="local",
            )
            summary = runner.run([target])

            self.assertEqual(summary["status"], "partial")
            self.assertEqual(
                summary["pdf_status_counts"], {"success": 1, "blocked": 1}
            )
            self.assertEqual(
                summary["ocr_status_counts"], {"success": 1, "not_available": 1}
            )

    def test_retryable_pdf_failure_marks_run_partial(self):
        target = ScholarlyTarget("finance", "financial reporting", 2023, 2024)
        candidate = sample_candidate()
        adapter = FakeScholarlyAdapter(candidate)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf_downloader = FakePdfDownloader(
                root,
                results=[
                    {
                        "status": "failed",
                        "failure_type": "retryable",
                        "error_msg": "HTTPError: HTTP Error 503: Service Unavailable",
                    }
                ],
            )

            summary = ScholarlyCollector(
                root,
                [adapter],
                retry_attempts=1,
                pdf_downloader=pdf_downloader,
            ).run([target])

            self.assertEqual(summary["status"], "partial")
            self.assertEqual(summary["pdf_status_counts"], {"failed": 1})
            self.assertEqual(summary["processing_errors"][0]["retryable"], True)

    def test_ocr_max_per_run_defers_excess_jobs(self):
        target = ScholarlyTarget("finance", "financial reporting", 2023, 2024)
        first = sample_candidate(
            title="First paper",
            source_id="doi:10.1000/first",
            doi="10.1000/first",
        )
        second = sample_candidate(
            title="Second paper",
            source_id="doi:10.1000/second",
            doi="10.1000/second",
        )
        adapters = [FakeScholarlyAdapter(first), FakeScholarlyAdapter(second)]
        ocr_calls = []

        def fake_ocr(pdf_path, output_path):
            ocr_calls.append(pdf_path)
            output_path.mkdir(parents=True, exist_ok=True)
            (output_path / "output.md").write_text("# parsed", encoding="utf-8")
            return {"markdown_path": str(output_path / "output.md")}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf_downloader = FakePdfDownloader(root)
            with StateStore(root / "state.db") as state_store:
                runner = ScholarlyCollector(
                    root,
                    adapters,
                    retry_attempts=1,
                    state_store=state_store,
                    pdf_downloader=pdf_downloader,
                    ocr_processor=fake_ocr,
                    ocr_root=root / "parsed_md/scholarly",
                    ocr_backend="cloud",
                    ocr_max_per_run=1,
                    ocr_delay_seconds=0,
                )
                summary = runner.run([target])

                self.assertEqual(summary["status"], "success")
                self.assertEqual(
                    summary["ocr_status_counts"],
                    {"success": 1, "deferred": 1},
                )
                self.assertEqual(summary["ocr_deferred_count"], 1)
                self.assertEqual(len(ocr_calls), 1)
                # The first adapter completed OCR and advanced; the deferred
                # adapter intentionally does not advance so cron retries it.
                self.assertEqual(state_store.count("source_sync_state"), 1)


if __name__ == "__main__":
    unittest.main()
