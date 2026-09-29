from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from ai_schematic.ai_service import AiServiceError, download_html_report, resume_job, upload_word


class FakeOpener:
    def __init__(self, *replies: dict) -> None:
        self.replies = list(replies)
        self.requests = []

    def open(self, request: object, timeout: int) -> BytesIO:
        self.requests.append((request, timeout))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return BytesIO(json.dumps(reply).encode())


class HtmlOpener:
    def __init__(self, content: bytes, content_type: str = "text/html; charset=utf-8") -> None:
        self.content = content
        self.content_type = content_type
        self.request = None

    def open(self, request: object, timeout: int) -> BytesIO:
        self.request = request
        response = BytesIO(self.content)
        response.headers = {"Content-Type": self.content_type}
        return response


class AiServiceTests(unittest.TestCase):
    def _word(self, directory: str) -> Path:
        path = Path(directory) / "技术协议.docx"
        path.write_bytes(b"word-data")
        return path

    def _job(self, status: str, **extra: object) -> dict:
        return {
            "job_id": "a" * 32,
            "status_url": "/api/v1/jobs/" + "a" * 32,
            "result_url": "/api/v1/jobs/" + "a" * 32 + "/graph" if status == "succeeded" else None,
            "status": status,
            "poll_after_seconds": 2,
            **extra,
        }

    def test_submit_poll_and_download_raw_graph(self) -> None:
        graph = {"graph_schema": "ai-eda-graph-ir/v2.0", "nodes": [], "edges": []}
        opener = FakeOpener(self._job("queued"), self._job("running"),
                            self._job("succeeded", summary={"conversion_warnings": [{"code": "WARN"}]}),
                            graph)
        updates = []
        with tempfile.TemporaryDirectory() as directory:
            with patch("ai_schematic.ai_service.build_opener", return_value=opener), \
                 patch("ai_schematic.ai_service.time.sleep") as sleep:
                result = upload_word(self._word(directory), "http://127.0.0.1:8000",
                                     token="secret", user_text="补充要求", run_name="测试任务",
                                     on_progress=updates.append)
        self.assertEqual(result, graph)
        self.assertEqual([job["status"] for job in updates], ["queued", "running", "succeeded"])
        self.assertEqual([request.full_url for request, _ in opener.requests], [
            "http://127.0.0.1:8000/api/v1/jobs",
            "http://127.0.0.1:8000/api/v1/jobs/" + "a" * 32,
            "http://127.0.0.1:8000/api/v1/jobs/" + "a" * 32,
            "http://127.0.0.1:8000/api/v1/jobs/" + "a" * 32 + "/graph",
        ])
        post = opener.requests[0][0]
        self.assertEqual(post.get_method(), "POST")
        self.assertIn(b'name="file"; filename="', post.data)
        self.assertIn(b"word-data", post.data)
        self.assertIn(b'name="user_text"', post.data)
        self.assertIn("补充要求".encode(), post.data)
        self.assertIn(b'name="run_name"', post.data)
        self.assertEqual([request.get_header("Authorization") for request, _ in opener.requests],
                         ["Bearer secret"] * 4)
        sleep.assert_called_once_with(2)

    def test_failed_job_does_not_download_graph(self) -> None:
        opener = FakeOpener(self._job("queued"), self._job("needs_attention",
                            error={"code": "WORKFLOW_INCOMPLETE", "message": "unresolved edges"}))
        with tempfile.TemporaryDirectory() as directory:
            with patch("ai_schematic.ai_service.build_opener", return_value=opener):
                with self.assertRaisesRegex(AiServiceError, "WORKFLOW_INCOMPLETE"):
                    upload_word(self._word(directory))
        self.assertEqual(len(opener.requests), 2)

    def test_resume_queries_without_upload(self) -> None:
        graph = {"nodes": [], "edges": []}
        opener = FakeOpener(self._job("succeeded"), graph)
        with patch("ai_schematic.ai_service.build_opener", return_value=opener):
            self.assertEqual(resume_job("http://127.0.0.1:8000",
                                        "/api/v1/jobs/" + "a" * 32), graph)
        self.assertEqual([request.get_method() for request, _ in opener.requests], ["GET", "GET"])

    def test_rejects_foreign_result_url(self) -> None:
        opener = FakeOpener(self._job("succeeded", result_url="http://other.example/graph"))
        with patch("ai_schematic.ai_service.build_opener", return_value=opener):
            with self.assertRaisesRegex(AiServiceError, "同源"):
                resume_job("http://127.0.0.1:8000", "/api/v1/jobs/" + "a" * 32)

    def test_old_doc_is_rejected_before_upload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.doc"
            path.write_bytes(b"old")
            with self.assertRaisesRegex(AiServiceError, ".docx"):
                upload_word(path)

    def test_http_error_shows_server_code_without_retrying_upload(self) -> None:
        error = HTTPError("http://127.0.0.1:8000/api/v1/jobs", 401, "Unauthorized", {},
                          BytesIO(b'{"error":{"code":"UNAUTHORIZED","message":"bad token"}}'))
        opener = FakeOpener(error)
        with tempfile.TemporaryDirectory() as directory:
            with patch("ai_schematic.ai_service.build_opener", return_value=opener):
                with self.assertRaisesRegex(AiServiceError, "UNAUTHORIZED: bad token"):
                    upload_word(self._word(directory), token="secret")
        self.assertEqual(len(opener.requests), 1)

    def test_status_network_retry_keeps_same_job(self) -> None:
        graph = {"nodes": [], "edges": []}
        opener = FakeOpener(URLError("temporary disconnect"), self._job("succeeded"), graph)
        with patch("ai_schematic.ai_service.build_opener", return_value=opener), \
             patch("ai_schematic.ai_service.time.sleep") as sleep:
            self.assertEqual(resume_job("http://127.0.0.1:8000",
                                        "/api/v1/jobs/" + "a" * 32), graph)
        self.assertEqual([request.get_method() for request, _ in opener.requests],
                         ["GET", "GET", "GET"])
        sleep.assert_called_once_with(1)

    def test_html_report_download_uses_token_and_writes_file(self) -> None:
        opener = HtmlOpener(b"<!doctype html><html>report</html>")
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "report.html"
            with patch("ai_schematic.ai_service.build_opener", return_value=opener):
                result = download_html_report("http://127.0.0.1:8000",
                                              "/api/v1/jobs/" + "a" * 32 + "/report",
                                              destination, token="secret")
            self.assertEqual(result, destination)
            self.assertEqual(destination.read_bytes(), opener.content)
            self.assertEqual(list(Path(directory).iterdir()), [destination])
        self.assertEqual(opener.request.get_header("Authorization"), "Bearer secret")
        self.assertEqual(opener.request.get_method(), "GET")

    def test_html_report_rejects_foreign_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(AiServiceError, "同源"):
                download_html_report("http://127.0.0.1:8000", "http://other.example/report",
                                     Path(directory) / "report.html")

    def test_html_report_http_error_preserves_existing_file(self) -> None:
        report_url = "/api/v1/jobs/" + "a" * 32 + "/report"
        error = HTTPError("http://127.0.0.1:8000" + report_url, 409, "Conflict", {},
                          BytesIO(b'{"error":{"code":"REPORT_NOT_READY","message":"running"}}'))
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "report.html"
            destination.write_bytes(b"previous report")
            with patch("ai_schematic.ai_service.build_opener", return_value=FakeOpener(error)):
                with self.assertRaisesRegex(AiServiceError, "REPORT_NOT_READY: running"):
                    download_html_report("http://127.0.0.1:8000", report_url, destination)
            self.assertEqual(destination.read_bytes(), b"previous report")
            self.assertEqual(list(Path(directory).iterdir()), [destination])


if __name__ == "__main__":
    unittest.main()
