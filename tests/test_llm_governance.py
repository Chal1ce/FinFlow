import unittest
from unittest.mock import patch

from processing.llm_governance import (
    GovernanceError,
    MockGovernanceModel,
    NoneGovernanceModel,
    OpenAIGovernanceModel,
    build_governance_model,
    detect_language,
)


class MockGovernanceConfig:
    llm_backend = "mock"
    llm_api_url = None
    llm_api_key = None
    llm_model = "test-model"
    llm_timeout_seconds = 5
    llm_retry_attempts = 1


class LLMGovernanceTests(unittest.TestCase):
    def test_mock_model_enriches_document_and_table(self):
        model = MockGovernanceModel()
        result = model.govern_document(
            {
                "title": "ESG and Financial Performance",
                "text": "ESG performance improves financial performance. "
                "Companies with stronger governance raise firm value.",
            },
            [
                {
                    "chunk_uid": "chunk-table",
                    "content_type": "table",
                    "text": "| A | B |\n| --- | --- |\n| 1 | 2 |",
                }
            ],
        )

        self.assertEqual(result["model"], "mock")
        self.assertEqual(result["document"]["language"], "en")
        self.assertIn("financial", " ".join(result["document"]["topics"]).lower())
        self.assertIn("2 行", result["chunk_enrichments"]["chunk-table"]["summary"])
        enrichment = result["chunk_enrichments"]["chunk-table"]
        self.assertEqual(enrichment["table_source"], "mock")
        self.assertTrue(enrichment["context_text"])
        self.assertIn(enrichment["table"], enrichment["retrieval_text"])

    def test_mock_skips_document_without_table(self):
        result = MockGovernanceModel().govern_document(
            {"title": "No tables", "text": "Narrative content."},
            [
                {
                    "chunk_uid": "chunk-text",
                    "content_type": "section",
                    "title_context": "1. Introduction",
                    "text": "Narrative content.",
                }
            ],
        )

        self.assertEqual(result["llm_status"], "skipped")
        self.assertEqual(result["skip_reason"], "no_table")
        self.assertEqual(result["document"], {})
        self.assertEqual(
            result["chunk_enrichments"]["chunk-text"]["context_status"],
            "skipped",
        )

    def test_mock_detects_chinese(self):
        self.assertEqual(detect_language("ESG 表现与公司财务绩效"), "zh")
        self.assertEqual(detect_language("ESG performance and firm value"), "en")

    def test_none_model_returns_empty_governance(self):
        result = NoneGovernanceModel().govern_document({"title": "x"}, [])

        self.assertEqual(result["document"], {})
        self.assertEqual(result["chunk_enrichments"], {})

    def test_none_model_keeps_original_table_and_title_context(self):
        result = NoneGovernanceModel().govern_document(
            {"title": "x"},
            [
                {
                    "chunk_uid": "chunk-table",
                    "content_type": "table",
                    "title_context": "2. Results",
                    "text": "| A | B |\n| --- | --- |\n| 1 | 2 |",
                }
            ],
        )

        enrichment = result["chunk_enrichments"]["chunk-table"]
        self.assertEqual(enrichment["table_source"], "original")
        self.assertEqual(enrichment["table_status"], "disabled")
        self.assertIn("2. Results", enrichment["context_text"])
        self.assertIn("| A | B |", enrichment["retrieval_text"])

    def test_openai_json_extraction_handles_code_fence(self):
        content = '```json\n{"title": "ESG", "language": "en", "topics": [], "summary": "s", "quality_score": 80, "quality_flags": []}\n```'
        self.assertEqual(
            OpenAIGovernanceModel._extract_json_object(content)["title"],
            "ESG",
        )

    def test_openai_backend_uses_sdk_client_and_env_base_url(self):
        calls = []

        class FakeCompletions:
            def create(self, **kwargs):
                calls.append(kwargs)
                return type(
                    "Response",
                    (),
                    {
                        "choices": [
                            type(
                                "Choice",
                                (),
                                {
                                    "message": type(
                                        "Message",
                                        (),
                                        {"content": '{"ok": true}'},
                                    )()
                                },
                            )()
                        ]
                    },
                )()

        class FakeOpenAI:
            def __init__(self, **kwargs):
                self.options = kwargs
                self.chat = type(
                    "Chat", (), {"completions": FakeCompletions()}
                )()

        with patch("processing.llm_governance.OpenAI", FakeOpenAI):
            model = OpenAIGovernanceModel(
                "https://llm.example/v1",
                "secret",
                "test-model",
                timeout_seconds=12,
                retry_attempts=1,
            )
            result = model._chat_json([{"role": "user", "content": "hello"}])

        self.assertEqual(result, {"ok": True})
        self.assertEqual(model.client.options["api_key"], "secret")
        self.assertEqual(model.client.options["base_url"], "https://llm.example/v1")
        self.assertEqual(model.client.options["timeout"], 12)
        self.assertEqual(model.client.options["max_retries"], 0)
        self.assertEqual(calls[0]["model"], "test-model")
        self.assertEqual(calls[0]["response_format"], {"type": "json_object"})

    def test_openai_runs_table_document_and_chunk_context_as_one_flow(self):
        model = OpenAIGovernanceModel(
            "https://llm.example/v1", "secret", "test-model", retry_attempts=1
        )
        calls = []

        def fake_chat(messages):
            content = messages[-1]["content"]
            calls.append(content)
            if "表格：" in content:
                return {"summary": "该表展示收入变化。", "quality_flags": []}
            if "当前 chunk（原文）" in content:
                return {"context_text": "本段位于结果章节，说明收入变化。", "quality_flags": []}
            if "表格摘要：" in content:
                self.assertIn("该表展示收入变化。", content)
                return {
                    "title": "ESG and Firm Value",
                    "language": "en",
                    "topics": ["ESG"],
                    "summary": "The document studies ESG.",
                    "quality_score": 90,
                    "quality_flags": [],
                }
            raise AssertionError(f"unexpected prompt: {content}")

        chunks = [
            {
                "chunk_uid": "chunk-table",
                "content_type": "table",
                "title_context": "2. Results",
                "text": "| A | B |\n| --- | --- |\n| 1 | 2 |",
            },
            {
                "chunk_uid": "chunk-text",
                "content_type": "section",
                "title_context": "2. Results",
                "text": "Revenue increased.",
            },
        ]

        with patch.object(model, "_chat_json", side_effect=fake_chat):
            result = model.govern_document(
                {"title": "ESG and Firm Value", "text": "Revenue increased."},
                chunks,
            )

        self.assertEqual(result["llm_status"], "success")
        self.assertEqual(len(calls), 4)
        table = result["chunk_enrichments"]["chunk-table"]
        self.assertEqual(table["table"], "该表展示收入变化。")
        self.assertEqual(table["table_source"], "openai-sdk")
        self.assertEqual(
            result["chunk_enrichments"]["chunk-text"]["context_source"],
            "openai-sdk",
        )

    def test_openai_summarizes_every_table(self):
        model = OpenAIGovernanceModel(
            "https://llm.example/v1", "secret", "test-model", retry_attempts=1
        )
        calls = []

        def fake_chat(messages):
            content = messages[-1]["content"]
            calls.append(content)
            if "表格：" in content:
                return {"summary": "表格摘要。", "quality_flags": []}
            if "当前 chunk（原文）" in content:
                return {"context_text": "表格所在章节的上下文。", "quality_flags": []}
            if "表格摘要：" in content:
                return {
                    "title": "Document",
                    "language": "en",
                    "topics": [],
                    "summary": "Document summary.",
                    "quality_score": 90,
                    "quality_flags": [],
                }
            raise AssertionError(f"unexpected prompt: {content}")

        chunks = [
            {
                "chunk_uid": f"chunk-table-{index}",
                "content_type": "table",
                "title_context": "2. Results",
                "text": f"| A | B |\n| --- | --- |\n| {index} | value |",
            }
            for index in range(3)
        ]

        with patch.object(model, "_chat_json", side_effect=fake_chat):
            result = model.govern_document(
                {"title": "Document", "text": "Document text."}, chunks
            )

        self.assertEqual(result["llm_status"], "success")
        self.assertEqual(len(calls), 7)  # 3 tables + 1 document + 3 chunks
        for chunk in chunks:
            enrichment = result["chunk_enrichments"][chunk["chunk_uid"]]
            self.assertEqual(enrichment["table_status"], "success")
            self.assertEqual(enrichment["table_source"], "openai-sdk")

    def test_openai_skips_all_calls_without_table(self):
        model = OpenAIGovernanceModel(
            "https://llm.example/v1", "secret", "test-model", retry_attempts=1
        )
        chunks = [
            {
                "chunk_uid": "chunk-text",
                "content_type": "section",
                "title_context": "1. Introduction",
                "text": "Narrative content.",
            }
        ]

        with patch.object(
            model,
            "_chat_json",
            side_effect=AssertionError("no-table document must not call the LLM"),
        ):
            result = model.govern_document(
                {"title": "No tables", "text": "Narrative content."}, chunks
            )

        self.assertEqual(result["llm_status"], "skipped")
        self.assertEqual(result["document_status"], "skipped")
        self.assertEqual(result["skip_reason"], "no_table")
        self.assertEqual(result["document"], {})
        self.assertEqual(
            result["chunk_enrichments"]["chunk-text"]["context_status"],
            "skipped",
        )
        self.assertEqual(
            result["chunk_enrichments"]["chunk-text"]["retrieval_text"],
            "当前内容位于章节：1. Introduction。\nNarrative content.",
        )

    def test_openai_failure_is_raised_without_local_fallback(self):
        model = OpenAIGovernanceModel(
            "https://llm.example/v1", "secret", "test-model", retry_attempts=1
        )
        chunks = [
            {
                "chunk_uid": "chunk-table",
                "content_type": "table",
                "title_context": "2. Results",
                "text": "| A | B |\n| --- | --- |\n| 1 | 2 |",
            },
            {
                "chunk_uid": "chunk-text",
                "content_type": "section",
                "title_context": "2. Results",
                "text": "Revenue increased.",
            },
        ]
        with patch.object(model, "_chat_json", side_effect=GovernanceError("table unavailable")):
            with self.assertRaisesRegex(GovernanceError, "table unavailable"):
                model.govern_document(
                {"title": "ESG", "text": "Revenue increased."}, chunks
                )

    def test_auto_build_without_key_uses_mock(self):
        config = MockGovernanceConfig()
        config.llm_backend = "auto"

        model = build_governance_model(config)

        self.assertIsInstance(model, MockGovernanceModel)

    def test_openai_build_requires_key(self):
        config = MockGovernanceConfig()
        config.llm_backend = "openai"

        with self.assertRaises(Exception):
            build_governance_model(config)


if __name__ == "__main__":
    unittest.main()
