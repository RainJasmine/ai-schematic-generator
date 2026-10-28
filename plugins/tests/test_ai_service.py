from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from ai_schematic.ai_service import (
    AiServiceError, ServiceClient, WaitingStopped, _NoRedirect, _opener,
    download_html_report, resume_job, upload_word, get_revision_state, submit_revision, submit_text_job, wait_for_job,
)
from threading import Event
from urllib.request import HTTPRedirectHandler, Request


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
        self.assertIn(b'name="request_id"', post.data)
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

    def test_text_create_keeps_explicit_request_id_and_does_not_retry_post(self) -> None:
        opener = FakeOpener(URLError("response lost"))
        with patch("ai_schematic.ai_service.build_opener", return_value=opener):
            with self.assertRaises(AiServiceError) as error:
                ServiceClient("http://127.0.0.1:8000").submit_text("设计接收链路", request_id="create-001")
        self.assertEqual(error.exception.code, "CONNECTION_FAILED")
        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(json.loads(opener.requests[0][0].data)["request_id"], "create-001")

    def test_upload_reads_current_service_limit(self) -> None:
        opener = FakeOpener({"max_upload_bytes": 3})
        with tempfile.TemporaryDirectory() as directory:
            with patch("ai_schematic.ai_service.build_opener", return_value=opener):
                with self.assertRaises(AiServiceError) as error:
                    ServiceClient("http://127.0.0.1:8000").submit_word(self._word(directory), request_id="create-002")
        self.assertEqual(error.exception.code, "UPLOAD_TOO_LARGE")
        self.assertEqual(len(opener.requests), 1)
        self.assertTrue(opener.requests[0][0].full_url.endswith("/capabilities"))

    def test_needs_attention_is_a_normal_terminal_response(self) -> None:
        terminal = self._job("needs_attention", error=None,
                             summary={"interaction_status": "awaiting_input", "message": "选择本振方案"})
        opener = FakeOpener(terminal)
        with patch("ai_schematic.ai_service.build_opener", return_value=opener):
            result = ServiceClient("http://127.0.0.1:8000").follow_job("a" * 32)
        self.assertEqual(result, terminal)
        self.assertEqual(result["summary"]["interaction_status"], "awaiting_input")

    def test_failed_is_a_normal_terminal_response(self) -> None:
        terminal = self._job("failed", error={"code": "WORKER_FAILED", "message": "worker stopped"})
        with patch("ai_schematic.ai_service.build_opener", return_value=FakeOpener(terminal)):
            result = ServiceClient("http://127.0.0.1:8000").follow_job("a" * 32)
        self.assertEqual(result["error"]["code"], "WORKER_FAILED")

    def test_stop_wait_never_sends_cancellation_or_query(self) -> None:
        stopped = Event()
        stopped.set()
        opener = FakeOpener()
        with patch("ai_schematic.ai_service.build_opener", return_value=opener):
            with self.assertRaises(WaitingStopped):
                ServiceClient("http://127.0.0.1:8000").follow_job("a" * 32, stop_event=stopped)
        self.assertEqual(opener.requests, [])

    def test_structured_http_error_keeps_conflict_fields(self) -> None:
        body = b'{"error":{"code":"GRAPH_HASH_MISMATCH","message":"changed","details":{"revision":2}}}'
        error = HTTPError("http://127.0.0.1:8000/test", 409, "Conflict", {"Retry-After": "5"}, BytesIO(body))
        with patch("ai_schematic.ai_service.build_opener", return_value=FakeOpener(error)):
            with self.assertRaises(AiServiceError) as caught:
                ServiceClient("http://127.0.0.1:8000").revision_state("a" * 32)
        self.assertEqual(caught.exception.code, "GRAPH_HASH_MISMATCH")
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.details, {"revision": 2})
        self.assertEqual(caught.exception.retry_after, "5")

    def test_redirect_handler_blocks_even_foreign_https_without_forwarding_token(self) -> None:
        opener = _opener()
        redirect_handlers = [handler for handler in opener.handlers if isinstance(handler, HTTPRedirectHandler)]
        self.assertEqual(len(redirect_handlers), 1)
        self.assertIsInstance(redirect_handlers[0], _NoRedirect)
        request = Request("http://127.0.0.1:8000/report", headers={"Authorization": "Bearer secret"})
        with self.assertRaises(AiServiceError) as error:
            redirect_handlers[0].redirect_request(request, None, 302, "Found", {}, "https://foreign.example/report")
        self.assertEqual(error.exception.code, "REDIRECT_BLOCKED")

    def test_admin_header_is_sent_only_for_management_calls(self) -> None:
        opener = FakeOpener({}, {})
        with patch("ai_schematic.ai_service.build_opener", return_value=opener):
            client = ServiceClient("http://127.0.0.1:8000", token="access", admin_key="manage")
            client.health()
            client.configuration()
        self.assertIsNone(opener.requests[0][0].get_header("X-ai-eda-admin-key"))
        self.assertEqual(opener.requests[1][0].get_header("X-ai-eda-admin-key"), "manage")
        self.assertEqual(opener.requests[1][0].get_header("Authorization"), "Bearer access")

    def test_result_job_identity_must_match_requested_id(self) -> None:
        with patch("ai_schematic.ai_service.build_opener", return_value=FakeOpener(self._job("succeeded"))):
            with self.assertRaises(AiServiceError) as error:
                ServiceClient("http://127.0.0.1:8000").get_job("b" * 32)
        self.assertEqual(error.exception.code, "JOB_ID_MISMATCH")

    def test_html_download_rejects_non_html_and_preserves_existing_file(self) -> None:
        for media in ("application/json", "application/octet-stream", "text/plain", ""):
            with self.subTest(media=media), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "report.html"
                output.write_bytes(b"old")
                with patch("ai_schematic.ai_service.build_opener", return_value=HtmlOpener(b"new", media)):
                    with self.assertRaises(AiServiceError):
                        ServiceClient("http://127.0.0.1:8000").download_html("/report", output)
                self.assertEqual(output.read_bytes(), b"old")

    def test_html_download_size_limit_preserves_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.html"
            output.write_bytes(b"old")
            with patch("ai_schematic.ai_service.MAX_REPORT_BYTES", 3), \
                 patch("ai_schematic.ai_service.build_opener", return_value=HtmlOpener(b"1234")):
                with self.assertRaises(AiServiceError) as error:
                    ServiceClient("http://127.0.0.1:8000").download_html("/report", output)
            self.assertEqual(error.exception.code, "REPORT_TOO_LARGE")
            self.assertEqual(output.read_bytes(), b"old")
            self.assertEqual(list(Path(directory).iterdir()), [output])

    def test_service_address_cannot_embed_credentials(self) -> None:
        with self.assertRaises(AiServiceError):
            ServiceClient("http://user:password@127.0.0.1:8000")




    def test_text_submission_and_needs_attention_are_distinct_from_http_failure(self) -> None:
        accepted = self._job("queued")
        terminal = self._job("needs_attention", summary={"interaction_status": "awaiting_input"},
                             report_url="/api/v1/jobs/" + "a" * 32 + "/report")
        opener = FakeOpener(accepted, terminal)
        with patch("ai_schematic.ai_service.build_opener", return_value=opener):
            self.assertEqual(submit_text_job("设计接收链路", "http://127.0.0.1:8000",
                                             token="secret", request_id="text-1"), accepted)
            result = wait_for_job("http://127.0.0.1:8000", accepted["status_url"],
                                  token="secret")
        self.assertEqual(result, terminal)
        self.assertEqual(opener.requests[0][0].get_method(), "POST")
        self.assertIn(b'"request_id": "text-1"', opener.requests[0][0].data)
        self.assertEqual(opener.requests[1][0].get_method(), "GET")
        self.assertEqual([request.get_header("Authorization") for request, _ in opener.requests],
                         ["Bearer secret", "Bearer secret"])

    def test_revision_uses_server_baseline_and_contract_fields(self) -> None:
        job_id = "a" * 32
        opener = FakeOpener({"revision": 2, "graph_hash": "f" * 64,
                             "head_job_id": job_id, "pending_job_id": None},
                            self._job("queued"))
        with patch("ai_schematic.ai_service.build_opener", return_value=opener):
            baseline = get_revision_state("http://127.0.0.1:8000", job_id)
            submit_revision("http://127.0.0.1:8000", job_id, "增加滤波器",
                            baseline["revision"], baseline["graph_hash"],
                            request_id="edit-1", selection={"node_ids": ["N1"], "edge_ids": []})
        payload = json.loads(opener.requests[1][0].data)
        self.assertEqual(payload["base_revision"], 2)
        self.assertEqual(payload["base_graph_hash"], "f" * 64)
        self.assertEqual(payload["selection"]["node_ids"], ["N1"])
        self.assertFalse(payload["client_graph_dirty"])
        self.assertNotIn("finish", payload)

if __name__ == "__main__":
    unittest.main()
