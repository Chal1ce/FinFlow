import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from remote.paddle_cloud_client import (
    PaddleOCRCloudClient,
    PaddleOCRCloudError,
    PaddleOCRQueueBusyError,
    PaddleOCRCloudTimeoutError,
)


class FakeResponse:
    def __init__(self, *, payload=None, text="", content=b"", status_code=200):
        self._payload = payload
        self.text = text
        self.content = content
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def cloud_client(session, **kwargs):
    return PaddleOCRCloudClient(
        "https://cloud.example/api/v2/ocr/jobs",
        token="test-token",
        session=session,
        poll_interval_seconds=0.001,
        timeout_seconds=10,
        **kwargs,
    )


class PaddleOCRCloudClientTests(unittest.TestCase):
    def test_submit_file_uses_multipart_and_bearer_auth(self):
        with tempfile.TemporaryDirectory() as directory:
            pdf_path = Path(directory) / "report.pdf"
            pdf_path.write_bytes(b"%PDF-test")
            session = Mock()
            session.post.return_value = FakeResponse(payload={"data": {"jobId": "job-1"}})

            job_id = cloud_client(session).submit_file(pdf_path)

        self.assertEqual(job_id, "job-1")
        request = session.post.call_args
        self.assertEqual(request.kwargs["headers"]["Authorization"], "bearer test-token")
        self.assertEqual(request.kwargs["data"]["model"], "PaddleOCR-VL-1.6")
        self.assertIn("optionalPayload", request.kwargs["data"])
        self.assertIn("file", request.kwargs["files"])

    def test_submit_url_uses_json_payload(self):
        session = Mock()
        session.post.return_value = FakeResponse(payload={"data": {"jobId": "job-url"}})
        client = cloud_client(session, model="custom-model")

        job_id = client.submit_url("https://files.example/report.pdf")

        self.assertEqual(job_id, "job-url")
        request = session.post.call_args
        self.assertEqual(request.kwargs["json"]["fileUrl"], "https://files.example/report.pdf")
        self.assertEqual(request.kwargs["json"]["model"], "custom-model")
        self.assertEqual(
            request.kwargs["headers"]["Content-Type"], "application/json"
        )

    def test_submit_file_retries_when_cloud_queue_is_full(self):
        with tempfile.TemporaryDirectory() as directory:
            pdf_path = Path(directory) / "report.pdf"
            pdf_path.write_bytes(b"%PDF-test")
            session = Mock()
            session.post.side_effect = [
                FakeResponse(
                    payload={"code": 10010, "msg": "任务提交队列已满，请稍后重试"}
                ),
                FakeResponse(
                    payload={"code": 10010, "msg": "任务提交队列已满，请稍后重试"}
                ),
                FakeResponse(payload={"data": {"jobId": "job-3"}}),
            ]
            client = cloud_client(
                session,
                submit_retry_attempts=3,
                submit_retry_backoff_seconds=0,
            )

            with patch("remote.paddle_cloud_client.time.sleep"):
                job_id = client.submit_file(pdf_path)

        self.assertEqual(job_id, "job-3")
        self.assertEqual(session.post.call_count, 3)

    def test_submit_url_raises_queue_busy_after_retries(self):
        session = Mock()
        session.post.return_value = FakeResponse(
            payload={"code": 10010, "msg": "任务提交队列已满，请稍后重试"}
        )
        client = cloud_client(
            session,
            submit_retry_attempts=2,
            submit_retry_backoff_seconds=0,
        )

        with patch("remote.paddle_cloud_client.time.sleep"):
            with self.assertRaisesRegex(PaddleOCRQueueBusyError, "任务提交队列已满"):
                client.submit_url("https://files.example/report.pdf")

        self.assertEqual(session.post.call_count, 2)

    def test_wait_polls_until_done_and_returns_jsonl_url(self):
        session = Mock()
        session.get.side_effect = [
            FakeResponse(payload={"data": {"state": "pending"}}),
            FakeResponse(
                payload={
                    "data": {
                        "state": "running",
                        "extractProgress": {"totalPages": 10, "extractedPages": 4},
                    }
                }
            ),
            FakeResponse(
                payload={
                    "data": {
                        "state": "done",
                        "resultUrl": {"jsonUrl": "https://files.example/result.jsonl"},
                    }
                }
            ),
        ]
        progress = []
        client = cloud_client(session, progress_callback=lambda state, data: progress.append(state))

        with patch("remote.paddle_cloud_client.time.sleep"):
            result_url = client.wait_for_result("job-1")

        self.assertEqual(result_url, "https://files.example/result.jsonl")
        self.assertEqual(progress, ["pending", "running", "done"])
        self.assertEqual(session.get.call_count, 3)
        for request in session.get.call_args_list:
            self.assertEqual(request.kwargs["headers"]["Authorization"], "bearer test-token")

    def test_failed_job_raises_clear_error(self):
        session = Mock()
        session.get.return_value = FakeResponse(
            payload={"data": {"state": "failed", "errorMsg": "bad PDF"}}
        )

        with self.assertRaisesRegex(PaddleOCRCloudError, "bad PDF"):
            cloud_client(session).wait_for_result("job-1")

    def test_wait_timeout_is_reported(self):
        session = Mock()
        session.get.return_value = FakeResponse(payload={"data": {"state": "pending"}})
        client = PaddleOCRCloudClient(
            "https://cloud.example/api/v2/ocr/jobs",
            token="test-token",
            session=session,
            poll_interval_seconds=5,
            timeout_seconds=1,
        )

        with patch("remote.paddle_cloud_client.time.monotonic", side_effect=[0, 2]):
            with self.assertRaises(PaddleOCRCloudTimeoutError):
                client.wait_for_result("job-1")

    def test_jsonl_markdown_and_base64_or_url_images_are_saved(self):
        markdown_image = base64.b64encode(b"markdown-image").decode("ascii")
        jsonl = json.dumps(
            {
                "result": {
                    "layoutParsingResults": [
                        {
                            "markdown": {
                                "text": "# 第1页\n正文",
                                "images": {"assets/chart.png": markdown_image},
                            },
                            "outputImages": {"layout.jpg": "https://files.example/layout.jpg"},
                        }
                    ]
                }
            },
            ensure_ascii=False,
        )
        session = Mock()
        session.get.side_effect = [
            FakeResponse(text=jsonl),
            FakeResponse(content=b"layout-image"),
        ]

        with tempfile.TemporaryDirectory() as directory:
            result = cloud_client(session).download_jsonl_result(
                "https://files.example/result.jsonl", Path(directory) / "parsed"
            )
            root = Path(directory) / "parsed"

            self.assertEqual(result["page_result_count"], 1)
            self.assertEqual(result["saved_image_count"], 2)
            self.assertEqual(
                (root / "output.md").read_text(encoding="utf-8"), "# 第1页\n正文"
            )
            self.assertEqual(
                (root / "pages/page_0001.md").read_text(encoding="utf-8"), "# 第1页\n正文"
            )
            self.assertEqual(
                (root / "images/markdown/assets/chart.png").read_bytes(),
                b"markdown-image",
            )
            self.assertEqual(
                (root / "images/output/layout.jpg_0").read_bytes(), b"layout-image"
            )
            self.assertTrue((root / "result.jsonl").exists())

        self.assertEqual(session.get.call_count, 2)
        for request in session.get.call_args_list:
            headers = request.kwargs["headers"]
            self.assertNotIn("Authorization", headers)
            self.assertIn("User-Agent", headers)


if __name__ == "__main__":
    unittest.main()
