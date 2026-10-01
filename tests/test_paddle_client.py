import base64
import io
import json
import tempfile
import unittest
from http.client import IncompleteRead
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfReader, PdfWriter

from remote.paddle_client import PaddleOCRClient, PaddleServingError, safe_relative_path


def write_pdf(path: Path, page_count: int) -> None:
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=612, height=792)
    with path.open("wb") as handle:
        writer.write(handle)


def request_page_count(payload: dict) -> int:
    encoded = str(payload["file"])
    return len(PdfReader(io.BytesIO(base64.b64decode(encoded))).pages)


class PaddleClientTests(unittest.TestCase):
    def test_response_base64_is_saved_as_markdown_and_images(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf_path = root / "report.pdf"
            write_pdf(pdf_path, 1)
            markdown_image = base64.b64encode(b"markdown-image").decode("ascii")
            output_image = base64.b64encode(b"output-image").decode("ascii")
            response = {
                "result": {
                    "layoutParsingResults": [
                        {
                            "markdown": {
                                "text": "# 第1页\n正文",
                                "images": {"assets/chart.png": markdown_image},
                            },
                            "outputImages": {"layout.jpg": output_image},
                        }
                    ]
                }
            }
            client = PaddleOCRClient("http://server:8080/layout-parsing")
            with patch.object(client, "_post_json", return_value=response):
                result = client.process_pdf(pdf_path, root / "parsed")

            self.assertEqual(result["page_result_count"], 1)
            self.assertEqual(
                (root / "parsed/output.md").read_text(encoding="utf-8"), "# 第1页\n正文"
            )
            self.assertEqual(
                (root / "parsed/images/markdown/page_0001/assets/chart.png").read_bytes(),
                b"markdown-image",
            )
            self.assertEqual(
                (root / "parsed/images/output/page_0001_layout.jpg").read_bytes(),
                b"output-image",
            )
            self.assertIn(
                "layoutParsingResults",
                (root / "parsed/result.json").read_text(encoding="utf-8"),
            )

    def test_response_url_can_be_downloaded(self):
        client = PaddleOCRClient("http://server:8080/layout-parsing")
        with patch("remote.paddle_client.urlopen") as open_url:
            response = open_url.return_value.__enter__.return_value
            response.read.return_value = b"url-image"
            self.assertEqual(client._decode_binary("https://server/image.png"), b"url-image")

    def test_incomplete_serving_response_is_wrapped(self):
        client = PaddleOCRClient("http://server:8080/layout-parsing")
        with patch("remote.paddle_client.urlopen") as open_url:
            response = open_url.return_value.__enter__.return_value
            response.read.side_effect = IncompleteRead(b"partial", 10)
            with self.assertRaisesRegex(PaddleServingError, "serving response was incomplete"):
                client._post_json({"file": "encoded"})

    def test_incomplete_url_response_is_wrapped(self):
        client = PaddleOCRClient("http://server:8080/layout-parsing")
        with patch("remote.paddle_client.urlopen") as open_url:
            response = open_url.return_value.__enter__.return_value
            response.read.side_effect = IncompleteRead(b"partial", 10)
            with self.assertRaisesRegex(PaddleServingError, "binary asset response was incomplete"):
                client._decode_binary("https://server/image.png")

    def test_unsafe_response_path_is_replaced(self):
        self.assertEqual(safe_relative_path("../../secret.txt"), Path("asset.bin"))
        self.assertEqual(safe_relative_path("assets/chart.png"), Path("assets/chart.png"))

    def test_invalid_server_result_raises(self):
        client = PaddleOCRClient("http://server:8080/layout-parsing")
        with patch.object(client, "_post_json", return_value={"errorMsg": "bad input"}):
            with tempfile.TemporaryDirectory() as directory:
                pdf_path = Path(directory) / "report.pdf"
                write_pdf(pdf_path, 1)
                with self.assertRaises(PaddleServingError):
                    client.process_pdf(pdf_path, Path(directory) / "parsed")

    def test_long_pdf_is_split_and_reassembled_in_page_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf_path = root / "report.pdf"
            write_pdf(pdf_path, 5)
            calls: list[int] = []

            def response(payload: dict):
                page_count = request_page_count(payload)
                calls.append(page_count)
                start = sum(calls[:-1]) + 1
                return {
                    "result": {
                        "layoutParsingResults": [
                            {"markdown": {"text": f"# Page {page}"}}
                            for page in range(start, start + page_count)
                        ]
                    }
                }

            client = PaddleOCRClient(
                "http://server:8080/layout-parsing", page_batch_size=2
            )
            with patch.object(client, "_post_json", side_effect=response):
                result = client.process_pdf(pdf_path, root / "parsed")

            self.assertEqual(calls, [2, 2, 1])
            self.assertEqual(result["page_result_count"], 5)
            self.assertEqual(result["segment_count"], 3)
            self.assertEqual(
                (root / "parsed/output.md").read_text(encoding="utf-8"),
                "# Page 1\n\n# Page 2\n\n# Page 3\n\n# Page 4\n\n# Page 5",
            )
            final_result = json.loads((root / "parsed/result.json").read_text(encoding="utf-8"))
            self.assertEqual(len(final_result["layoutParsingResults"]), 5)
            manifest = json.loads((root / "parsed/ocr_segments.json").read_text(encoding="utf-8"))
            self.assertEqual(
                [(item["start_page"], item["end_page"], item["status"]) for item in manifest["segments"]],
                [(1, 2, "success"), (3, 4, "success"), (5, 5, "success")],
            )

    def test_failed_segment_does_not_stop_later_segments_and_is_retried_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf_path = root / "report.pdf"
            write_pdf(pdf_path, 5)
            client = PaddleOCRClient(
                "http://server:8080/layout-parsing", page_batch_size=2
            )
            first_calls: list[int] = []

            def first_response(payload: dict):
                page_count = request_page_count(payload)
                first_calls.append(page_count)
                if len(first_calls) == 2:
                    raise PaddleServingError("simulated segment failure")
                return {
                    "result": {
                        "layoutParsingResults": [
                            {"markdown": {"text": "successful page"}}
                            for _ in range(page_count)
                        ]
                    }
                }

            with patch.object(client, "_post_json", side_effect=first_response):
                with self.assertRaisesRegex(PaddleServingError, "segment-0002"):
                    client.process_pdf(pdf_path, root / "parsed")

            self.assertEqual(first_calls, [2, 2, 1])
            self.assertFalse((root / "parsed/output.md").exists())
            manifest = json.loads((root / "parsed/ocr_segments.json").read_text(encoding="utf-8"))
            self.assertEqual(
                [item["status"] for item in manifest["segments"]],
                ["success", "failed", "success"],
            )

            retry_calls: list[int] = []

            def retry_response(payload: dict):
                page_count = request_page_count(payload)
                retry_calls.append(page_count)
                return {
                    "result": {
                        "layoutParsingResults": [
                            {"markdown": {"text": "retried page"}}
                            for _ in range(page_count)
                        ]
                    }
                }

            with patch.object(client, "_post_json", side_effect=retry_response):
                result = client.process_pdf(pdf_path, root / "parsed")

            self.assertEqual(retry_calls, [2])
            self.assertEqual(result["page_result_count"], 5)
            self.assertTrue((root / "parsed/output.md").exists())


if __name__ == "__main__":
    unittest.main()
