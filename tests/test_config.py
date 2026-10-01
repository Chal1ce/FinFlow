import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import _load_dotenv, load_config


class ConfigTests(unittest.TestCase):
    def test_runtime_settings_are_loaded_from_environment(self):
        environment = {
            "FIN_DOC_DATA_ROOT": "/tmp/fin-doc-test-data",
            "PADDLEOCR_LOCAL_API_URL": "http://127.0.0.1:18080/layout-parsing",
            "PADDLEOCR_LOCAL_TOKEN": "local-test-token",
            "PADDLEOCR_LOCAL_PAGE_BATCH_SIZE": "20",
            "PADDLEOCR_CLOUD_TOKEN": "cloud-test-token",
            "PADDLEOCR_CLOUD_MODEL": "test-model",
            "PADDLEOCR_CLOUD_POLL_INTERVAL_SECONDS": "2.5",
            "PADDLEOCR_CLOUD_TIMEOUT_SECONDS": "120",
            "PADDLEOCR_CLOUD_SUBMIT_RETRY_ATTEMPTS": "4",
            "PADDLEOCR_CLOUD_SUBMIT_RETRY_BACKOFF_SECONDS": "15",
            "PADDLEOCR_CLOUD_OPTIONAL_PAYLOAD": '{"useChartRecognition": true}',
        }
        with patch.dict(os.environ, environment, clear=True):
            config = load_config()

        self.assertEqual(config.paths.data_root, Path("/tmp/fin-doc-test-data"))
        self.assertEqual(
            config.paths.raw_pdfs, Path("/tmp/fin-doc-test-data/raw_pdfs")
        )
        self.assertEqual(config.local_paddle.token, "local-test-token")
        self.assertEqual(config.local_paddle.page_batch_size, 20)
        self.assertTrue(config.local_paddle.optional_payload["useTableRecognition"])
        self.assertEqual(config.cloud_paddle.token, "cloud-test-token")
        self.assertEqual(config.cloud_paddle.model, "test-model")
        self.assertEqual(config.cloud_paddle.poll_interval_seconds, 2.5)
        self.assertEqual(config.cloud_paddle.timeout_seconds, 120)
        self.assertEqual(config.cloud_paddle.submit_retry_attempts, 4)
        self.assertEqual(config.cloud_paddle.submit_retry_backoff_seconds, 15)
        self.assertEqual(config.cloud_paddle.optional_payload, {"useChartRecognition": True})

    def test_cloud_token_has_no_source_code_default(self):
        with patch.dict(os.environ, {}, clear=True):
            config = load_config()
        self.assertIsNone(config.cloud_paddle.token)
        self.assertEqual(
            config.cloud_paddle.job_url,
            "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs",
        )

    def test_governance_settings_are_loaded_from_environment(self):
        environment = {
            "FIN_DOC_PROCESSED": "/tmp/fin-doc/processed",
            "FIN_DOC_GOVERNED": "/tmp/fin-doc/processed/governed",
            "FIN_DOC_CHUNKS_DIR": "/tmp/fin-doc/processed/chunks",
            "FIN_DOC_CHUNKS_JSONL": "/tmp/fin-doc/processed/chunks.jsonl",
            "FIN_DOC_RULE_VERSION": "ocr-cleaning-v2",
            "FIN_DOC_DROP_BLOCK_LABELS": '["header", "footer"]',
            "FIN_DOC_CHUNK_MAX_CHARS": "1800",
            "FIN_DOC_CHUNK_MIN_CHARS": "100",
            "LLM_BACKEND": "openai",
            "LLM_API_URL": "https://llm.example/v1",
            "LLM_API_KEY": "secret-key",
            "LLM_MODEL": "test-llm",
        }
        with patch.dict(os.environ, environment, clear=True):
            config = load_config()

        self.assertEqual(config.governance.rule_version, "ocr-cleaning-v2")
        self.assertEqual(config.governance.drop_block_labels, ("header", "footer"))
        self.assertEqual(config.governance.chunk_max_chars, 1800)
        self.assertEqual(config.governance.chunk_min_chars, 100)
        self.assertEqual(config.governance.llm_backend, "openai")
        self.assertEqual(config.governance.llm_model, "test-llm")
        self.assertEqual(config.paths.governed, Path("/tmp/fin-doc/processed/governed"))
        self.assertEqual(config.paths.chunks_jsonl, Path("/tmp/fin-doc/processed/chunks.jsonl"))

    def test_dotenv_is_loaded_without_overriding_shell_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                'PADDLEOCR_CLOUD_TOKEN="from-file"\n'
                "# a comment\n"
                "PADDLEOCR_CLOUD_MODEL=file-model # inline comment\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"PADDLEOCR_CLOUD_MODEL": "shell-model"}, clear=True):
                _load_dotenv(env_file)

                self.assertEqual(os.environ["PADDLEOCR_CLOUD_TOKEN"], "from-file")
                self.assertEqual(os.environ["PADDLEOCR_CLOUD_MODEL"], "shell-model")


if __name__ == "__main__":
    unittest.main()
