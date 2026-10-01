import io
import json
import logging
import tempfile
import unittest
from pathlib import Path

from core.context import PipelineContext
from core.logging import LOGGER_ROOT, configure_logging, get_logger, log_event
from storage.state_store import StateStore


class LoggingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self) -> None:
        root = logging.getLogger(LOGGER_ROOT)
        for handler in root.handlers:
            handler.close()
        root.handlers.clear()
        root.addHandler(logging.NullHandler())
        root.setLevel(logging.NOTSET)
        self.directory.cleanup()

    def test_pretty_logs_keep_event_fields_and_respect_level(self) -> None:
        stream = io.StringIO()
        configure_logging(level="INFO", color="never", stream=stream, log_file=None)
        logger = get_logger(__name__)

        log_event(logger, "DEBUG", "hidden_debug_event", batch_id="batch-1")
        log_event(logger, "INFO", "workflow_stage_started", batch_id="batch-1", stage_name="ocr")

        output = stream.getvalue()
        self.assertNotIn("hidden_debug_event", output)
        self.assertIn("| INFO    | workflow_stage_started", output)
        self.assertIn("batch_id=batch-1", output)
        self.assertIn("stage_name=ocr", output)

    def test_json_logs_are_machine_readable(self) -> None:
        stream = io.StringIO()
        configure_logging(level="DEBUG", log_format="json", stream=stream, log_file=None)

        log_event(get_logger(__name__), "DEBUG", "cache_hit", batch_id="batch-1", cache_key="asset-1")

        payload = json.loads(stream.getvalue())
        self.assertEqual(payload["level"], "DEBUG")
        self.assertEqual(payload["event"], "cache_hit")
        self.assertEqual(payload["batch_id"], "batch-1")
        self.assertEqual(payload["cache_key"], "asset-1")

    def test_log_file_rotates_at_configured_size(self) -> None:
        log_path = self.root / "logs" / "pipeline.log"
        configure_logging(
            level="INFO",
            color="never",
            stream=io.StringIO(),
            log_file=log_path,
            max_bytes=120,
            backup_count=2,
        )
        logger = get_logger(__name__)
        for index in range(6):
            log_event(logger, "INFO", "pipeline_step_finished", step_index=index, detail="x" * 80)

        self.assertTrue(log_path.is_file())
        self.assertTrue(log_path.with_name("pipeline.log.1").is_file())

    def test_state_store_emits_pipeline_and_workflow_lifecycle_events(self) -> None:
        stream = io.StringIO()
        configure_logging(level="INFO", color="never", stream=stream, log_file=None)
        context = PipelineContext.create(self.root, batch_id="batch-logging")
        with StateStore(self.root / "state" / "pipeline.db") as store:
            store.start_run(context, dry_run=False)
            step_id = store.start_step(context, "import", entity_uid="asset-1")
            store.finish_step(step_id, "success")
            store.finish_run(context.run_id, "success")
            workflow_run_id = store.start_workflow_run(
                batch_id=context.batch_id,
                pipeline_name="local_financial",
                stages=["ingest"],
                dry_run=False,
            )
            store.start_workflow_stage(workflow_run_id, "ingest", 0)
            store.finish_workflow_stage(workflow_run_id, "ingest", "success")
            store.finish_workflow_run(workflow_run_id, "success")

        output = stream.getvalue()
        self.assertIn("pipeline_run_started", output)
        self.assertIn("pipeline_step_started", output)
        self.assertIn("pipeline_step_finished", output)
        self.assertIn("workflow_stage_started", output)
        self.assertIn("workflow_stage_finished", output)
        self.assertIn("batch_id=batch-logging", output)


if __name__ == "__main__":
    unittest.main()
