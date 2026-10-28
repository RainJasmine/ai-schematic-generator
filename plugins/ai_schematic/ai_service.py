"""RF EDA HTTP contract, bounded downloads and explicit mutation retries.

The UI persists a frozen request before mutation. POST is never automatically
retried: uncertain responses are recovered with the SAME request_id and payload.
Access credentials live only on a client instance and never enter sessions.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import uuid

DEFAULT_URL = "http://127.0.0.1:8000"
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_REPORT_BYTES = 64 * 1024 * 1024
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # Legacy helper only; ServiceClient reads capabilities.
MAX_JSON_REQUEST_BYTES = 128 * 1024
TERMINAL_STATES = frozenset({"succeeded", "needs_attention", "failed"})
ProgressCallback = Callable[[dict], None]
_ID = re.compile(r"^[0-9a-f]{32}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


class AiServiceError(RuntimeError):
    """Stable fields let callers recover without parsing prose."""
    def __init__(self, message: str, *, code: str = "", status: int | None = None,
                 details: object = None, retry_after: str | None = None) -> None:
        super().__init__(message)
        self.code, self.status, self.details = code, status, details
        self.retry_after = retry_after


class WaitingStopped(AiServiceError):
    """Local waiting stopped; the remote job was NOT cancelled."""
    def __init__(self) -> None:
        super().__init__("已停止本地等待，服务端任务继续运行，可稍后恢复。", code="WAITING_STOPPED")


class _NoRedirect(HTTPRedirectHandler):
    """Do not forward credentials or change POST semantics through redirects."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AiServiceError("服务返回重定向；请核对并直接填写最终服务根地址。", code="REDIRECT_BLOCKED", status=code)


def _opener():
    return build_opener(ProxyHandler({}), _NoRedirect())


def _service_origin(url: str) -> tuple[str, str | None, int]:
    parsed = urlparse(url)
    try:
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("port out of range")
        return parsed.scheme, parsed.hostname, port if port is not None else (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise AiServiceError("地址端口无效", code="INVALID_SERVICE_URL") from exc


def _service_root(url: str) -> str:
    parsed = urlparse(str(url).strip().rstrip("/"))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AiServiceError("AI 服务地址必须是 HTTP(S) URL", code="INVALID_SERVICE_URL")
    if parsed.username is not None or parsed.password is not None:
        raise AiServiceError("服务地址不能包含用户名或密码，请使用独立令牌输入框。", code="INVALID_SERVICE_URL")
    if parsed.path not in {"", "/api/v1/jobs", "/generate"} or parsed.query or parsed.fragment:
        raise AiServiceError("请填写 AI 服务根地址，例如 http://127.0.0.1:8000", code="INVALID_SERVICE_URL")
    _, host, port = _service_origin(url.strip().rstrip("/"))
    host = host.lower()
    if ":" in host:
        host = f"[{host}]"
    if port != (443 if parsed.scheme == "https" else 80):
        host += f":{port}"
    return f"{parsed.scheme}://{host}"


def _job_url(root: str, path: str) -> str:
    if not isinstance(path, str) or not path:
        raise AiServiceError("任务响应缺少查询或下载地址", code="INVALID_RESPONSE")
    target = urljoin(root + "/", path)
    parsed = urlparse(target)
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise AiServiceError("任务地址含无效凭据或片段", code="INVALID_RESPONSE")
    if _service_origin(target) != _service_origin(root):
        raise AiServiceError("任务查询和下载地址必须与 AI 服务同源", code="FOREIGN_URL")
    return target


def _read_json(response: object) -> dict:
    data = response.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        raise AiServiceError("AI 返回的 JSON 超过允许大小", code="RESPONSE_TOO_LARGE")
    try:
        value = json.loads(data.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AiServiceError("AI 返回的内容不是有效 JSON", code="INVALID_JSON") from exc
    if not isinstance(value, dict):
        raise AiServiceError("AI 返回的 JSON 顶层必须是对象", code="INVALID_RESPONSE")
    return value


def _http_error(exc: HTTPError) -> AiServiceError:
    # Never echo unstructured HTML bodies that may contain a proxy credential.
    code, message, details = f"HTTP_{exc.code}", "服务请求未成功，请检查地址、权限或服务状态。", None
    try:
        raw = json.loads(exc.read(64 * 1024).decode("utf-8", errors="replace"))
        error = raw.get("error")
        if isinstance(error, dict):
            code = str(error.get("code") or code)
            message = str(error.get("message") or message)
            details = error.get("details")
        elif exc.code == 422:
            message, details = "请求字段校验失败，请检查格式、长度和范围。", raw.get("detail")
    except (ValueError, AttributeError, OSError):
        pass
    return AiServiceError(f"HTTP {exc.code} {code}: {message}", code=code, status=exc.code,
                          details=details, retry_after=exc.headers.get("Retry-After") if exc.headers else None)


def _request_json(opener: object, request: Request, timeout: int = 65) -> dict:
    try:
        with opener.open(request, timeout=timeout) as response:
            return _read_json(response)
    except HTTPError as exc:
        raise _http_error(exc) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise AiServiceError("无法连接 AI 服务，请核对服务地址并稍后恢复原任务。", code="CONNECTION_FAILED") from exc


def _request_id(value: str) -> str:
    if not isinstance(value, str) or not _REQUEST_ID.fullmatch(value):
        raise AiServiceError("request_id 应为1～100位字母、数字、下划线、点或短横线", code="INVALID_REQUEST_ID")
    return value


def _validate_text(user_text: str, run_name: str, *, required: bool = False) -> None:
    if not isinstance(user_text, str) or not isinstance(run_name, str):
        raise AiServiceError("需求文本和任务名必须是文字", code="INVALID_INPUT")
    if len(user_text) > 20000 or len(run_name) > 100:
        raise AiServiceError("需求文本最多20000字符，任务名称最多100字符", code="INVALID_INPUT")
    if required and not user_text.strip():
        raise AiServiceError("纯文字设计要求不能为空", code="INVALID_INPUT")


def _validate_revision(payload: dict) -> None:
    if not isinstance(payload, dict):
        raise AiServiceError("反馈请求必须是JSON对象", code="INVALID_INPUT")
    fields = {"request_id", "base_revision", "base_graph_hash", "instruction", "selection",
              "client_graph_dirty", "continuation_job_id", "display_aliases", "canvas_policy"}
    if set(payload) - fields:
        raise AiServiceError("反馈包含未声明字段", code="INVALID_INPUT")
    _request_id(payload.get("request_id"))
    revision, graph_hash, instruction = payload.get("base_revision"), payload.get("base_graph_hash"), payload.get("instruction")
    if type(revision) is not int or revision < 1 or not isinstance(graph_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", graph_hash):
        raise AiServiceError("反馈基准版本或哈希无效", code="INVALID_INPUT")
    if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 12000:
        raise AiServiceError("反馈内容应为1～12000字符", code="INVALID_INPUT")
    policy = payload.get("canvas_policy", "verify_local")
    if policy not in ("verify_local", "backend_only"):
        raise AiServiceError("反馈画布策略无效", code="INVALID_INPUT")
    if policy == "verify_local" and type(payload.get("client_graph_dirty")) is not bool:
        raise AiServiceError("反馈须明确声明本地图是否有未同步修改", code="INVALID_INPUT")
    if policy == "backend_only" and payload.get("client_graph_dirty") is not None:
        raise AiServiceError("仅后端反馈不应声称已检查当前画布", code="INVALID_INPUT")
    selection = payload.get("selection", {})
    if not isinstance(selection, dict) or set(selection) - {"node_ids", "edge_ids"}:
        raise AiServiceError("反馈选择字段无效", code="INVALID_INPUT")
    for key in ("node_ids", "edge_ids"):
        values = selection.get(key, [])
        if not isinstance(values, list) or len(values) > 50 or any(not isinstance(value, str) or not 1 <= len(value) <= 160 for value in values):
            raise AiServiceError("反馈节点和连接各最多50个真实ID", code="INVALID_INPUT")
    continuation = payload.get("continuation_job_id")
    if continuation is not None and (not isinstance(continuation, str) or not re.fullmatch(r"[0-9a-f]{32}", continuation)):
        raise AiServiceError("反馈接续任务ID无效", code="INVALID_INPUT")
    aliases = payload.get("display_aliases", {})
    if not isinstance(aliases, dict) or len(aliases) > 100 or any(not isinstance(key, str) or not isinstance(value, str) or not 1 <= len(value) <= 120 for key, value in aliases.items()):
        raise AiServiceError("显示别名应为最多100项节点ID与名称映射", code="INVALID_INPUT")


def _form_field(boundary: str, name: str, value: str) -> bytes:
    return (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            f'{value}\r\n').encode("utf-8")


def _upload_request(word: Path, endpoint: str, headers: dict[str, str], user_text: str, run_name: str,
                    request_id: str | None = None, max_upload_bytes: int = MAX_UPLOAD_BYTES) -> Request:
    if word.suffix.lower() != ".docx":
        raise AiServiceError("AI 服务只接受未加密的 .docx 文件", code="INVALID_INPUT")
    if not word.is_file():
        raise AiServiceError("Word 文件不存在", code="INPUT_MISSING")
    if word.stat().st_size > max_upload_bytes:
        raise AiServiceError(f"Word 文件超过当前服务的 {max_upload_bytes / 1024 / 1024:g} MiB 上传上限", code="UPLOAD_TOO_LARGE")
    _validate_text(user_text, run_name)
    request_id = _request_id(request_id or uuid.uuid4().hex)
    boundary = f"kicad-{uuid.uuid4().hex}"
    safe_name = word.name.replace('"', "_").replace("\r", "_").replace("\n", "_")
    prefix = (f"--{boundary}\r\n"
              f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'
              "Content-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document\r\n\r\n").encode("utf-8")
    with word.open("rb") as stream:
        data = stream.read(max_upload_bytes + 1)
    if len(data) > max_upload_bytes:
        raise AiServiceError("Word 文件超过当前服务上传上限", code="UPLOAD_TOO_LARGE")
    body = (prefix + data + b"\r\n" + _form_field(boundary, "user_text", user_text)
            + _form_field(boundary, "run_name", run_name) + _form_field(boundary, "request_id", request_id)
            + f"--{boundary}--\r\n".encode())
    return Request(endpoint, body, headers={**headers, "Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST")


def _wait(delay: float, stop_event=None) -> None:
    if stop_event is not None:
        if stop_event.wait(delay):
            raise WaitingStopped()
    else:
        time.sleep(delay)


def _check_stopped(stop_event) -> None:
    if stop_event is not None and stop_event.is_set():
        raise WaitingStopped()


def _get_with_retry(opener: object, url: str, headers: dict[str, str], *, timeout: int = 65, stop_event=None) -> dict:
    request = Request(url, headers=headers)
    for attempt in range(3):
        _check_stopped(stop_event)
        try:
            return _request_json(opener, request, timeout=timeout)
        except AiServiceError as exc:
            if exc.code != "CONNECTION_FAILED" or attempt == 2:
                raise
            _wait(2 ** attempt, stop_event)
    raise AssertionError("unreachable")


def _job_path(job_id_or_url: str, suffix: str = "") -> str:
    if isinstance(job_id_or_url, str) and _ID.fullmatch(job_id_or_url):
        return f"/api/v1/jobs/{job_id_or_url}{suffix}"
    if suffix:
        raise AiServiceError("该操作需要32位任务ID", code="INVALID_JOB_ID")
    return job_id_or_url


class ServiceClient:
    """One service and credential context; recreate it when switching services."""
    def __init__(self, service_url: str, token: str = "", admin_key: str = "", timeout: int = 65) -> None:
        self.root = _service_root(service_url)
        self._token, self._admin_key = token, admin_key
        self.timeout, self._opener, self._caps = timeout, _opener(), None

    @property
    def service_url(self) -> str:
        return self.root

    def _headers(self, *, admin: bool = False, accept: str = "application/json") -> dict[str, str]:
        headers = {"Accept": accept}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if admin and self._admin_key:
            headers["X-AI-EDA-Admin-Key"] = self._admin_key
        return headers

    def get_json(self, path: str, *, admin: bool = False, stop_event=None) -> dict:
        return _get_with_retry(self._opener, _job_url(self.root, path), self._headers(admin=admin),
                               timeout=self.timeout, stop_event=stop_event)

    def post_json(self, path: str, payload: dict, *, admin: bool = False) -> dict:
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(data) > MAX_JSON_REQUEST_BYTES:
            raise AiServiceError("JSON 请求超过128 KiB", code="REQUEST_TOO_LARGE")
        headers = {**self._headers(admin=admin), "Content-Type": "application/json; charset=utf-8"}
        return _request_json(self._opener, Request(_job_url(self.root, path), data=data,
                             headers=headers, method="POST"), timeout=self.timeout)

    def health(self) -> dict:
        return self.get_json("/health")

    def capabilities(self) -> dict:
        self._caps = self.get_json("/api/v1/capabilities")
        return self._caps

    def submit_word(self, path: str | Path, *, user_text: str = "", run_name: str = "",
                    request_id: str, max_upload_bytes: int | None = None) -> dict:
        _request_id(request_id)
        _validate_text(user_text, run_name)
        if max_upload_bytes is None:
            caps = self._caps if self._caps is not None else self.capabilities()
            max_upload_bytes = caps.get("max_upload_bytes")
        if isinstance(max_upload_bytes, bool) or not isinstance(max_upload_bytes, int) or max_upload_bytes < 1:
            raise AiServiceError("服务未提供有效上传大小限制", code="INVALID_CAPABILITIES")
        request = _upload_request(Path(path), self.root + "/api/v1/jobs", self._headers(),
                                  user_text, run_name, request_id, max_upload_bytes)
        return self._accepted(_request_json(self._opener, request, timeout=self.timeout))

    def submit_text(self, user_text: str, *, run_name: str = "", request_id: str) -> dict:
        _validate_text(user_text, run_name, required=True)
        return self._accepted(self.post_json("/api/v1/jobs/text", {
            "request_id": _request_id(request_id), "user_text": user_text, "run_name": run_name}))

    def submit_frozen(self, request: dict) -> dict:
        """Retry the exact saved payload, rejecting modified Word snapshots."""
        from .session_store import SessionStore
        if not SessionStore.verify_frozen(request):
            raise AiServiceError("保存的提交内容已变化或文件缺失；请保留原请求，勿以新ID盲目重试。", code="FROZEN_REQUEST_CHANGED")
        if request["kind"] == "revision":
            return self.submit_revision(request["parent_job_id"], request["payload"])
        if request["kind"] == "word":
            return self.submit_word(request["path"], user_text=request["user_text"],
                                    run_name=request["run_name"], request_id=request["request_id"])
        return self.submit_text(request["user_text"], run_name=request["run_name"], request_id=request["request_id"])

    def _accepted(self, job: dict) -> dict:
        job_id = job.get("job_id")
        if not isinstance(job_id, str) or not _ID.fullmatch(job_id):
            raise AiServiceError("响应缺少有效job_id；原请求可能已受理，请使用原request_id核对。", code="INVALID_RESPONSE")
        status = urlparse(_job_url(self.root, job.get("status_url")))
        if status.path.rstrip("/") != f"/api/v1/jobs/{job_id}" or status.query:
            raise AiServiceError("响应的任务ID和查询地址不一致", code="JOB_ID_MISMATCH")
        result_url = job.get("result_url")
        if result_url:
            result = urlparse(_job_url(self.root, result_url))
            if result.path.rstrip("/") != f"/api/v1/jobs/{job_id}/graph" or result.query:
                raise AiServiceError("响应的任务ID和图下载地址不一致", code="JOB_ID_MISMATCH")
        return job

    def _checked_job(self, job: dict, path: str) -> dict:
        self._accepted(job)
        expected = urlparse(_job_url(self.root, path)).path.rstrip("/").split("/")[-1]
        if _ID.fullmatch(expected) and job["job_id"] != expected:
            raise AiServiceError("查询响应的任务ID与请求不一致", code="JOB_ID_MISMATCH")
        return job

    def get_job(self, job_id_or_status_url: str) -> dict:
        path = _job_path(job_id_or_status_url)
        return self._checked_job(self.get_json(path), path)

    def follow_job(self, job_id_or_status_url: str, *, on_progress: ProgressCallback | None = None,
                   stop_event=None, wait_seconds: float = 3900) -> dict:
        deadline = time.monotonic() + wait_seconds
        path = _job_path(job_id_or_status_url)
        while True:
            _check_stopped(stop_event)
            job = self._checked_job(self.get_json(path, stop_event=stop_event), path)
            if on_progress:
                on_progress(job)
            status = job.get("status")
            if status in TERMINAL_STATES:
                return job
            if status not in {"queued", "running"}:
                raise AiServiceError("任务返回未知状态", code="INVALID_JOB_STATUS", details=job)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AiServiceError("本地等待超时；服务端可能仍运行，请继续查询原任务。", code="WAIT_TIMEOUT")
            delay = job.get("poll_after_seconds", 2)
            if isinstance(delay, bool) or not isinstance(delay, (int, float)) or not math.isfinite(delay):
                delay = 2
            _wait(min(max(delay, 1), 30, remaining), stop_event)

    def graph(self, job_id_or_url: str) -> dict:
        path = _job_path(job_id_or_url, "/graph") if isinstance(job_id_or_url, str) and _ID.fullmatch(job_id_or_url) else job_id_or_url
        graph = self.get_json(path)
        if not isinstance(graph.get("nodes"), list) or not isinstance(graph.get("edges"), list):
            raise AiServiceError("下载的Graph IR缺少有效nodes/edges数组", code="INVALID_GRAPH")
        return graph

    def revision_state(self, job_id: str) -> dict:
        return self.get_json(_job_path(job_id, "/revision-state"))

    def submit_revision(self, parent_job_id: str, payload: dict) -> dict:
        _validate_revision(payload)
        return self._accepted(self.post_json(_job_path(parent_job_id, "/revisions"), payload))

    def revision_result(self, job_id: str) -> dict:
        return self.get_json(_job_path(job_id, "/revision-result"))

    def requirements(self, job_id: str) -> dict:
        return self.get_json(_job_path(job_id, "/requirements"))

    def configuration(self) -> dict:
        return self.get_json("/api/v1/admin/configuration", admin=True)

    def validate_configuration(self, payload: dict) -> dict:
        return self.post_json("/api/v1/admin/configuration/validate", payload, admin=True)

    def save_configuration(self, payload: dict) -> dict:
        return self.post_json("/api/v1/admin/configuration", payload, admin=True)

    def test_model(self, payload: dict) -> dict:
        return self.post_json("/api/v1/admin/models/test", payload, admin=True)

    def knowledge(self, *, refresh_database: bool = False) -> dict:
        return self.get_json("/api/v1/admin/knowledge" + ("?refresh_database=true" if refresh_database else ""), admin=True)

    def build_knowledge(self, payload: dict) -> dict:
        return self.post_json("/api/v1/admin/knowledge/builds", payload, admin=True)

    def publish_knowledge(self, payload: dict) -> dict:
        return self.post_json("/api/v1/admin/knowledge/publications", payload, admin=True)

    def maintenance(self, job_id: str) -> dict:
        if not isinstance(job_id, str) or not _ID.fullmatch(job_id):
            raise AiServiceError("维护任务ID无效", code="INVALID_JOB_ID")
        result = self.get_json(f"/api/v1/admin/maintenance/{job_id}", admin=True)
        if result.get("job_id") != job_id or result.get("status") not in TERMINAL_STATES | {"queued", "running"}:
            raise AiServiceError("维护查询响应与任务不一致", code="INVALID_RESPONSE")
        return result

    def download_html(self, report_url: str, destination: str | Path) -> Path:
        return _download_html(self._opener, self.root, report_url, destination, self._headers(accept="text/html"), self.timeout)


def _download_html(opener, root: str, report_url: str, destination: str | Path,
                   headers: dict[str, str], timeout: int) -> Path:
    target, output = _job_url(root, report_url), Path(destination)
    if output.suffix.lower() not in {".html", ".htm"}:
        raise AiServiceError("报告保存文件名必须以.html或.htm结尾", code="INVALID_DESTINATION")
    temporary = None
    try:
        with opener.open(Request(target, headers=headers), timeout=timeout) as response:
            media = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if media not in {"text/html", "application/xhtml+xml"}:
                raise AiServiceError("报告接口未返回HTML文件", code="INVALID_REPORT_TYPE")
            raw_length = response.headers.get("Content-Length", "")
            if raw_length.isdigit() and int(raw_length) > MAX_REPORT_BYTES:
                raise AiServiceError("HTML报告超过允许大小", code="REPORT_TOO_LARGE")
            output.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=output.parent, suffix=".html.part", delete=False) as stream:
                temporary, total = Path(stream.name), 0
                while chunk := response.read(min(1024 * 1024, MAX_REPORT_BYTES - total + 1)):
                    total += len(chunk)
                    if total > MAX_REPORT_BYTES:
                        raise AiServiceError("HTML报告超过允许大小", code="REPORT_TOO_LARGE")
                    stream.write(chunk)
        os.replace(temporary, output)
    except HTTPError as exc:
        raise _http_error(exc) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise AiServiceError("HTML报告下载未完成，原文件已保留。", code="DOWNLOAD_FAILED") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return output


def _terminal_graph(client: ServiceClient, job: dict) -> dict:
    if job.get("status") != "succeeded":
        error = job.get("error") or {}
        if not isinstance(error, dict):
            error = {}
        raise AiServiceError(f"任务 {job.get('job_id', '')} {job.get('status', '')}："
                             f"{error.get('code', '')} {error.get('message', '')}".strip(),
                             code=error.get("code") or "JOB_NEEDS_ATTENTION", details=job)
    return client.graph(job.get("result_url"))


def upload_word(path: str | Path, url: str | None = None, *, token: str = "", user_text: str = "", run_name: str = "",
                wait_seconds: float = 3900, on_progress: ProgressCallback | None = None, request_id: str | None = None,
                max_upload_bytes: int = MAX_UPLOAD_BYTES, stop_event=None) -> dict:
    """Compatibility helper. New UIs should freeze requests and use ServiceClient."""
    root = url or os.environ.get("KICAD_AI_SERVICE_URL") or os.environ.get("KICAD_AI_GENERATE_URL", DEFAULT_URL)
    client = ServiceClient(root, token)
    job = client.submit_word(path, user_text=user_text, run_name=run_name,
                              request_id=request_id or uuid.uuid4().hex, max_upload_bytes=max_upload_bytes)
    if on_progress:
        on_progress(job)
    return _terminal_graph(client, client.follow_job(job["status_url"], on_progress=on_progress,
                                                     wait_seconds=wait_seconds, stop_event=stop_event))


def resume_job(url: str, status_url: str, *, token: str = "", wait_seconds: float = 3900,
               on_progress: ProgressCallback | None = None, stop_event=None) -> dict:
    """Compatibility graph helper; errors retain the terminal JobView in details."""
    client = ServiceClient(url, token)
    return _terminal_graph(client, client.follow_job(status_url, on_progress=on_progress,
                                                     wait_seconds=wait_seconds, stop_event=stop_event))


def download_html_report(url: str, report_url: str, destination: str | Path, *, token: str = "") -> Path:
    return ServiceClient(url, token).download_html(report_url, destination)


# Keep the dev 0.2 public helpers for other plugin modules and external clients.
def get_capabilities(url: str, *, token: str = "") -> dict:
    return ServiceClient(url, token).capabilities()


def get_job(url: str, job_id: str, *, token: str = "") -> dict:
    return ServiceClient(url, token).get_job(job_id)


def submit_word_job(path, url, *, token="", user_text="", run_name="", request_id=""):
    return ServiceClient(url, token).submit_word(path, user_text=user_text, run_name=run_name,
                                                request_id=request_id or uuid.uuid4().hex)


def submit_text_job(text, url, *, token="", run_name="", request_id=""):
    return ServiceClient(url, token).submit_text(text, run_name=run_name, request_id=request_id or uuid.uuid4().hex)


def wait_for_job(url, status_url, *, token="", wait_seconds=3900, on_progress=None, stop_event=None):
    return ServiceClient(url, token).follow_job(status_url, wait_seconds=wait_seconds,
                                               on_progress=on_progress, stop_event=stop_event)


def download_graph(url, result_url, *, token=""):
    return ServiceClient(url, token).graph(result_url)


def get_revision_state(url, job_id, *, token=""):
    return ServiceClient(url, token).revision_state(job_id)


def submit_revision(url, parent_job_id, instruction, base_revision, base_graph_hash, *, token="",
                    request_id, selection=None, client_graph_dirty=False, continuation_job_id=""):
    payload = {"request_id": request_id, "base_revision": base_revision, "base_graph_hash": base_graph_hash,
               "instruction": instruction, "selection": selection or {"node_ids": [], "edge_ids": []},
               "client_graph_dirty": client_graph_dirty}
    if continuation_job_id:
        payload["continuation_job_id"] = continuation_job_id
    return ServiceClient(url, token).submit_revision(parent_job_id, payload)


def get_revision_result(url, revision_result_url, *, token=""):
    return ServiceClient(url, token).get_json(revision_result_url)


def get_requirements(url, requirements_url, *, token=""):
    return ServiceClient(url, token).get_json(requirements_url)
