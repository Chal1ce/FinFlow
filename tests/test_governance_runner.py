import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import load_config as load_runtime_config
from processing.governance_runner import GovernanceRunner, load_ocr_pages
from processing.llm_governance import GovernanceError, build_governance_model
from storage.state_store import StateStore


def create_fixture(root: Path) -> None:
    parsed = root / "parsed_md/scholarly/work1/asset1/local"
    parsed.mkdir(parents=True)
    result = {
        "layoutParsingResults": [
            {
                "prunedResult": {
                    "parsing_res_list": [
                        {"block_label": "header", "block_content": "Journal page 1"},
                        {
                            "block_label": "doc_title",
                            "block_content": "ESG and Firm Value",
                        },
                        {
                            "block_label": "paragraph_title",
                            "block_content": "1. Introduction",
                        },
                        {
                            "block_label": "text",
                            "block_content": "ESG improves firm value through governance.",
                        },
                        {
                            "block_label": "table",
                            "block_content": (
                                "<table><tr><td>A</td><td>B</td></tr>"
                                "<tr><td>1</td><td>2</td></tr></table>"
                            ),
                        },
                    ]
                },
                "markdown": {"text": "ESG and Firm Value"},
            }
        ]
    }
    (parsed / "result.json").write_text(json.dumps(result), encoding="utf-8")
    (parsed / "output.md").write_text(
        "# ESG and Firm Value\n\nESG improves firm value.", encoding="utf-8"
    )

    manifest_dir = root / "manifests"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "scholarly_documents.jsonl").write_text(
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


class GovernanceRunnerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        create_fixture(self.root)
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

    def runner(self):
        with patch.dict(os.environ, self.environment, clear=False):
            runtime = load_runtime_config()
            model = build_governance_model(runtime.governance, "mock")
            return GovernanceRunner(
                self.root,
                {},
                runtime_config=runtime,
                llm_model=model,
            )

    def test_dry_run_does_not_write_governed_outputs(self):
        with patch.dict(os.environ, self.environment, clear=False):
            summary = self.runner().run(dry_run=True)

        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["processed_count"], 0)
        self.assertEqual(summary["preview_count"], 1)
        self.assertFalse((self.root / "processed").exists())

    def test_real_run_writes_artifacts_and_state(self):
        runner = self.runner()
        with patch.dict(os.environ, self.environment, clear=False):
            summary = runner.run(dry_run=False)

        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["processed_count"], 1)
        governed = self.root / "processed/governed/scholarly/work1/asset1/local"
        self.assertTrue((governed / "governed.md").exists())
        self.assertTrue((governed / "governed.json").exists())
        self.assertTrue((governed / "llm.json").exists())
        self.assertTrue((governed / ".complete").exists())
        chunk_file = self.root / "processed/chunks/scholarly/work1/asset1.jsonl"
        self.assertTrue(chunk_file.exists())
        self.assertTrue((self.root / "processed/chunks.jsonl").exists())

        chunk_records = [
            json.loads(line)
            for line in chunk_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        table_record = next(
            record for record in chunk_records if record["content_type"] == "table"
        )
        self.assertTrue(table_record["table"])
        self.assertEqual(table_record["table_source"], "mock")
        self.assertTrue(table_record["context_text"])
        self.assertTrue(table_record["retrieval_text"])
        self.assertTrue(table_record["llm_enrichment_uid"])

        with StateStore(self.root / "state/pipeline.db") as store:
            self.assertEqual(store.count("pipeline_batch"), 1)
            self.assertEqual(store.count("document_registry"), 1)
            self.assertGreater(store.count("artifact"), 0)
            self.assertEqual(store.count("governed_document"), 1)
            self.assertGreater(store.count("document_chunk"), 0)
            chunk = store.fetch_one(
                "SELECT chunk_id, chunk_version_uid, chunk_uid FROM document_chunk LIMIT 1"
            )
            self.assertTrue(chunk["chunk_id"])
            self.assertEqual(chunk["chunk_version_uid"], chunk["chunk_uid"])
            self.assertGreater(store.count("llm_governance"), 1)
            metadata = store.fetch_one(
                "SELECT metadata_json FROM document_chunk WHERE content_type = 'table' LIMIT 1"
            )
            self.assertIn('"table"', metadata["metadata_json"])
            self.assertIn('"context_text"', metadata["metadata_json"])
            row = store.fetch_one(
                "SELECT COUNT(*) AS count FROM qc_result WHERE qc_stage = 'governed'"
            )
            self.assertGreater(row["count"], 0)

    def test_cloud_markdown_tables_are_parsed_as_table_blocks_and_chunks(self):
        parsed = self.root / "parsed_md/scholarly/work1/asset1/local"
        (parsed / "result.json").unlink()
        page_dir = parsed / "pages"
        page_dir.mkdir()
        (page_dir / "page_0001.md").write_text(
            "# ESG and Firm Value\n\n"
            "The following table reports the sample.\n\n"
            "<table><tr><td>Year</td><td>Value</td></tr>"
            "<tr><td>2024</td><td>10</td></tr></table>",
            encoding="utf-8",
        )
        pages = load_ocr_pages(parsed)
        self.assertEqual(pages[0][0], 0)
        self.assertEqual([block.block_label for block in pages[0][1]], ["paragraph_title", "text", "table"])

        with patch.dict(os.environ, self.environment, clear=False):
            summary = self.runner().run(dry_run=False)

        self.assertEqual(summary["status"], "success")
        chunk_file = self.root / "processed/chunks/scholarly/work1/asset1.jsonl"
        chunks = [
            json.loads(line)
            for line in chunk_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        table = next(chunk for chunk in chunks if chunk["content_type"] == "table")
        self.assertEqual(table["page"], 0)
        self.assertIn("| Year | Value |", table["text"])
        self.assertEqual(table["table_source"], "mock")

    def test_result_json_markdown_fallback_parses_tables(self):
        parsed = self.root / "parsed_md/scholarly/work1/asset1/local"
        (parsed / "result.json").write_text(
            json.dumps(
                {
                    "layoutParsingResults": [
                        {
                            "markdown": {
                                "text": "## Results\n\n"
                                "| Year | Value |\n| --- | --- |\n| 2024 | 10 |"
                            }
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        pages = load_ocr_pages(parsed)

        self.assertEqual([block.block_label for block in pages[0][1]], ["paragraph_title", "table"])

    def test_governance_removes_reference_tail_and_records_statistics(self):
        parsed = self.root / "parsed_md/scholarly/work1/asset1/local"
        result = json.loads((parsed / "result.json").read_text(encoding="utf-8"))
        blocks = result["layoutParsingResults"][0]["prunedResult"]["parsing_res_list"]
        blocks.extend(
            [
                {"block_label": "text", "block_content": "References"},
                {"block_label": "reference", "block_content": "[1] A source that must not be chunked."},
            ]
        )
        (parsed / "result.json").write_text(json.dumps(result), encoding="utf-8")

        with patch.dict(os.environ, self.environment, clear=False):
            summary = self.runner().run(dry_run=False)

        self.assertEqual(summary["status"], "success")
        governed = self.root / "processed/governed/scholarly/work1/asset1/local/governed.json"
        record = json.loads(governed.read_text(encoding="utf-8"))
        self.assertNotIn("References", record["text"])
        self.assertEqual(record["stats"]["reference_section_detected"], 1)
        self.assertEqual(record["stats"]["reference_blocks_removed"], 2)
        self.assertIn("drop_reference_section", record["cleaning_steps"])

    def test_second_run_is_idempotent_and_skips(self):
        with patch.dict(os.environ, self.environment, clear=False):
            first = self.runner().run(dry_run=False, batch_id="batch-governance-resume")
            summary = self.runner().run(
                dry_run=False, batch_id="batch-governance-resume"
            )

        self.assertEqual(summary["status"], "success")
        self.assertEqual(first["batch_id"], summary["batch_id"])
        self.assertEqual(summary["skipped_count"], 1)
        self.assertEqual(summary["processed_count"], 0)
        with StateStore(self.root / "state/pipeline.db") as store:
            attempts = store.connection.execute(
                "SELECT attempt FROM pipeline_run WHERE batch_id = ? ORDER BY attempt",
                ("batch-governance-resume",),
            ).fetchall()
            self.assertEqual([row["attempt"] for row in attempts], [1, 2])

    def test_old_rule_completion_marker_does_not_skip_new_governed_identity(self):
        governed = self.root / "processed/governed/scholarly/work1/asset1/local"
        governed.mkdir(parents=True)
        (governed / ".complete").write_text(
            json.dumps({"governed_uid": "previous-rule-version"}), encoding="utf-8"
        )

        with patch.dict(os.environ, self.environment, clear=False):
            summary = self.runner().run(dry_run=False)

        self.assertEqual(summary["processed_count"], 1)
        marker = json.loads((governed / ".complete").read_text(encoding="utf-8"))
        self.assertNotEqual(marker["governed_uid"], "previous-rule-version")

    def test_llm_failure_marks_document_and_run_failed_without_completion_marker(self):
        runner = self.runner()
        governed = self.root / "processed/governed/scholarly/work1/asset1/local"
        governed.mkdir(parents=True)
        (governed / ".complete").write_text("prior success\n", encoding="utf-8")
        runner.force = True
        with patch.object(
            runner.llm_model,
            "govern_document",
            side_effect=GovernanceError("LLM request failed: invalid endpoint"),
        ):
            with patch.dict(os.environ, self.environment, clear=False):
                summary = runner.run(dry_run=False, batch_id="batch-llm-failure")

        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["failed_count"], 1)
        self.assertIn("LLM request failed: invalid endpoint", summary["processing_errors"][0]["error_msg"])
        self.assertFalse((governed / ".complete").exists())
        with StateStore(self.root / "state/pipeline.db") as store:
            step = store.fetch_one(
                "SELECT status, error_msg FROM pipeline_step WHERE step_name = 'govern'"
            )
        self.assertEqual(step["status"], "failed")
        self.assertIn("LLM request failed: invalid endpoint", step["error_msg"])


if __name__ == "__main__":
    unittest.main()
