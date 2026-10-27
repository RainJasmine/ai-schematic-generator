"""Submit a DOCX job, poll its status, and download the resulting Graph IR."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import ProxyHandler, Request, build_opener
import uuid


DEFAULT_URL = "http://127.0.0.1:8000"
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
ProgressCallback = Callable[[dict], None]


class AiServiceError(RuntimeError):
    pass


REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


def _service_root(url: str) -> str:
    parsed = urlparse(url.strip().rstrip("/"))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise AiServiceError("AI 服务地址必须是 HTTP(S) URL")
    if parsed.path not in {"", "/api/v1/jobs", "/generate"} or parsed.query or parsed.fragment:
        raise AiServiceError("请填写 AI 服务根地址，例如 http://127.0.0.1:8000")
    return f"{parsed.scheme}://{parsed.netloc}"


def _job_url(root: str, path: str) -> str:
    if not isinstance(path, str) or not path:
        raise AiServiceError("任务响应缺少查询或下载地址")
    target = urljoin(root + "/", path)
    parsed = urlparse(target)
    origin = urlparse(root)
    if (parsed.scheme, parsed.netloc) != (origin.scheme, origin.netloc):
        raise AiServiceError("任务查询和下载地址必须与 AI 服务同源")
    return target


def _read_json(response: object) -> dict:
    data = response.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        raise AiServiceError("AI 返回的 JSON 超过 32 MiB")
    try:
        value = json.loads(data.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AiServiceError(f"AI 返回的内容不是有效 JSON：{exc}") from exc
    if not isinstance(value, dict):
        raise AiServiceError("AI 返回的 JSON 顶层必须是对象")
    return value


def _request_json(opener: object, request: Request, timeout: int = 60) -> dict:
    try:
        with opener.open(request, timeout=timeout) as response:
            return _read_json(response)
    except HTTPError as exc:
        detail = exc.read(2048).decode("utf-8", errors="replace")
        try:
            error = json.loads(detail).get("error", {})
            detail = f"{error.get('code', '')}: {error.get('message', '')}".strip(": ") or detail
        except (ValueError, AttributeError):
            pass
        raise AiServiceError(f"{request.full_url} 返回 HTTP {exc.code}：{detail}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise AiServiceError(f"无法连接 {request.full_url}：{exc}") from exc


def _form_field(boundary: str, name: str, value: str) -> bytes:
    return (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            f'{value}\r\n').encode("utf-8")


def _upload_request(word: Path, endpoint: str, headers: dict[str, str],
                    user_text: str, run_name: str, request_id: str = "") -> Request:
    if word.suffix.lower() != ".docx":
        raise AiServiceError("AI 服务只接受未加密的 .docx 文件")
    if not word.is_file():
        raise AiServiceError("Word 文件不存在")
    if word.stat().st_size > MAX_UPLOAD_BYTES:
        raise AiServiceError("Word 文件超过服务默认的 20 MiB 上传上限")
    if len(user_text) > 20000 or len(run_name) > 100:
        raise AiServiceError("补充要求最多 20000 字符，任务名称最多 100 字符")
    if request_id and not REQUEST_ID_PATTERN.fullmatch(request_id):
        raise AiServiceError("请求 ID 格式不正确")
    boundary = f"kicad-{uuid.uuid4().hex}"
    safe_name = word.name.replace('"', "_").replace("\r", "_").replace("\n", "_")
    prefix = (f"--{boundary}\r\n"
              f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'
              "Content-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document\r\n\r\n").encode("utf-8")
    body = (prefix + word.read_bytes() + b"\r\n" + _form_field(boundary, "user_text", user_text)
            + _form_field(boundary, "run_name", run_name))
    if request_id:
        body += _form_field(boundary, "request_id", request_id)
    body += f"--{boundary}--\r\n".encode()
    return Request(endpoint, body, headers={
        **headers,
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }, method="POST")


def _get_with_retry(opener: object, url: str, headers: dict[str, str]) -> dict:
    request = Request(url, headers=headers)
    for attempt in range(3):
        try:
            return _request_json(opener, request)
        except AiServiceError as exc:
            if (isinstance(exc.__cause__, HTTPError)
                    or not isinstance(exc.__cause__, (URLError, TimeoutError, OSError))
                    or attempt == 2):
                raise
            time.sleep(2 ** attempt)
    raise AssertionError("unreachable")


def _follow_job(root: str, status_url: str, *, token: str, wait_seconds: float,
                on_progress: ProgressCallback | None) -> dict:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    opener = build_opener(ProxyHandler({}))
    status_url = _job_url(root, status_url)
    deadline = time.monotonic() + wait_seconds
    while True:
        job = _get_with_retry(opener, status_url, headers)
        job_id = job.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            raise AiServiceError("任务查询响应缺少 job_id")
        if on_progress:
            on_progress(job)
        status = job.get("status")
        if status == "succeeded":
            result_url = _job_url(root, job.get("result_url"))
            graph = _get_with_retry(opener, result_url, headers)
            if "nodes" not in graph or "edges" not in graph:
                raise AiServiceError("下载的 Graph IR 缺少 nodes 或 edges")
            return graph
        if status in {"failed", "needs_attention"}:
            error = job.get("error") or {}
            if not isinstance(error, dict):
                error = {}
            raise AiServiceError(f"任务 {job_id} {status}：{error.get('code', '')} {error.get('message', '')}".strip())
        if status not in {"queued", "running"}:
            raise AiServiceError(f"任务 {job_id} 返回未知状态：{status}")
        if time.monotonic() >= deadline:
            raise AiServiceError(f"等待任务 {job_id} 超时；可继续查询 {status_url}，请勿重复上传")
        delay = job.get("poll_after_seconds", 2)
        time.sleep(min(max(delay if isinstance(delay, (int, float)) else 2, 1), 30))


def upload_word(path: str | Path, url: str | None = None, *, token: str = "",
                user_text: str = "", run_name: str = "", wait_seconds: float = 3900,
                on_progress: ProgressCallback | None = None) -> dict:
    """Submit a Word job, then wait for and return its raw Graph IR JSON."""
    root = _service_root(url or os.environ.get("KICAD_AI_SERVICE_URL")
                         or os.environ.get("KICAD_AI_GENERATE_URL", DEFAULT_URL))
    endpoint = root + "/api/v1/jobs"
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    opener = build_opener(ProxyHandler({}))
    request = _upload_request(Path(path), endpoint, headers, user_text, run_name)
    job = _request_json(opener, request)
    job_id, status_url = job.get("job_id"), job.get("status_url")
    if not isinstance(job_id, str) or not job_id:
        raise AiServiceError("任务提交响应缺少 job_id")
    _job_url(root, status_url)
    if on_progress:
        on_progress(job)
    return _follow_job(root, status_url, token=token, wait_seconds=wait_seconds,
                       on_progress=on_progress)


def resume_job(url: str, status_url: str, *, token: str = "", wait_seconds: float = 3900,
               on_progress: ProgressCallback | None = None) -> dict:
    """Continue an existing server job without submitting the Word file again."""
    return _follow_job(_service_root(url), status_url, token=token,
                       wait_seconds=wait_seconds, on_progress=on_progress)


def _headers(token: str) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _json_request(url: str, path: str, *, token: str = "",
                  body: dict | None = None) -> dict:
    root = _service_root(url)
    target = _job_url(root, path)
    headers = _headers(token)
    payload = None
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        if len(payload) > 128 * 1024:
            raise AiServiceError("请求 JSON 超过 128 KiB")
        headers["Content-Type"] = "application/json; charset=utf-8"
    request = Request(target, data=payload, headers=headers,
                      method="POST" if payload is not None else "GET")
    opener = build_opener(ProxyHandler({}))
    return _request_json(opener, request)


def get_capabilities(url: str, *, token: str = "") -> dict:
    return _json_request(url, "/api/v1/capabilities", token=token)


def get_job(url: str, job_id: str, *, token: str = "") -> dict:
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise AiServiceError("任务 ID 必须是 32 位小写十六进制")
    return _json_request(url, f"/api/v1/jobs/{job_id}", token=token)


def submit_word_job(path: str | Path, url: str, *, token: str = "",
                    user_text: str = "", run_name: str = "",
                    request_id: str = "") -> dict:
    root = _service_root(url)
    request = _upload_request(Path(path), root + "/api/v1/jobs", _headers(token),
                              user_text, run_name, request_id)
    return _request_json(build_opener(ProxyHandler({})), request)


def submit_text_job(text: str, url: str, *, token: str = "", run_name: str = "",
                    request_id: str = "") -> dict:
    if not text.strip() or len(text) > 20000 or len(run_name) > 100:
        raise AiServiceError("文字要求应为 1～20000 字符，任务名称最多 100 字符")
    if request_id and not REQUEST_ID_PATTERN.fullmatch(request_id):
        raise AiServiceError("请求 ID 格式不正确")
    body = {"user_text": text, "run_name": run_name}
    if request_id:
        body["request_id"] = request_id
    return _json_request(url, "/api/v1/jobs/text", token=token, body=body)


def wait_for_job(url: str, status_url: str, *, token: str = "",
                 wait_seconds: float = 3900,
                 on_progress: ProgressCallback | None = None) -> dict:
    """Poll until terminal; return needs_attention/failed as job data, not exceptions."""
    root = _service_root(url)
    target = _job_url(root, status_url)
    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + wait_seconds
    while True:
        job = _get_with_retry(opener, target, _headers(token))
        if not isinstance(job.get("job_id"), str) or not job["job_id"]:
            raise AiServiceError("任务查询响应缺少 job_id")
        if on_progress:
            on_progress(job)
        if job.get("status") in {"succeeded", "needs_attention", "failed"}:
            return job
        if job.get("status") not in {"queued", "running"}:
            raise AiServiceError(f"未知任务状态：{job.get('status')}")
        if time.monotonic() >= deadline:
            raise AiServiceError(f"等待任务 {job['job_id']} 超时；可继续查询 {target}")
        delay = job.get("poll_after_seconds", 2)
        time.sleep(min(max(delay if isinstance(delay, (int, float)) else 2, 1), 30))


def download_graph(url: str, result_url: str, *, token: str = "") -> dict:
    graph = _json_request(url, result_url, token=token)
    if "nodes" not in graph or "edges" not in graph:
        raise AiServiceError("下载的 Graph IR 缺少 nodes 或 edges")
    return graph


def get_revision_state(url: str, job_id: str, *, token: str = "") -> dict:
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise AiServiceError("任务 ID 必须是 32 位小写十六进制")
    return _json_request(url, f"/api/v1/jobs/{job_id}/revision-state", token=token)


def submit_revision(url: str, parent_job_id: str, instruction: str,
                    base_revision: int, base_graph_hash: str, *, token: str = "",
                    request_id: str, selection: dict | None = None,
                    client_graph_dirty: bool = False,
                    continuation_job_id: str = "") -> dict:
    if not re.fullmatch(r"[0-9a-f]{32}", parent_job_id):
        raise AiServiceError("父任务 ID 必须是 32 位小写十六进制")
    if not REQUEST_ID_PATTERN.fullmatch(request_id):
        raise AiServiceError("请求 ID 格式不正确")
    if not instruction.strip() or len(instruction) > 12000:
        raise AiServiceError("反馈内容应为 1～12000 字符")
    if base_revision < 1 or not re.fullmatch(r"[0-9a-f]{64}", base_graph_hash):
        raise AiServiceError("反馈基准版本或哈希无效")
    if client_graph_dirty:
        raise AiServiceError("本地图纸有未同步修改，请先保存工作副本")
    body = {"request_id": request_id, "base_revision": base_revision,
            "base_graph_hash": base_graph_hash, "instruction": instruction,
            "selection": selection or {"node_ids": [], "edge_ids": []},
            "client_graph_dirty": False}
    if continuation_job_id:
        body["continuation_job_id"] = continuation_job_id
    return _json_request(url, f"/api/v1/jobs/{parent_job_id}/revisions",
                         token=token, body=body)


def get_revision_result(url: str, revision_result_url: str, *, token: str = "") -> dict:
    return _json_request(url, revision_result_url, token=token)


def get_requirements(url: str, requirements_url: str, *, token: str = "") -> dict:
    return _json_request(url, requirements_url, token=token)


def download_html_report(url: str, report_url: str, destination: str | Path,
                         *, token: str = "") -> Path:
    """Download a server-provided HTML report URL to a local file."""
    target = _job_url(_service_root(url), report_url)
    output = Path(destination)
    if output.suffix.lower() not in {".html", ".htm"}:
        raise AiServiceError("报告保存文件名必须以 .html 或 .htm 结尾")
    headers = {"Accept": "text/html"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(target, headers=headers)
    opener = build_opener(ProxyHandler({}))
    temporary: Path | None = None
    try:
        with opener.open(request, timeout=60) as response:
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type == "application/json":
                raise AiServiceError("报告接口返回了 JSON，未获取到 HTML 文件")
            output.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=output.parent, suffix=".html.part", delete=False) as stream:
                temporary = Path(stream.name)
                while chunk := response.read(1024 * 1024):
                    stream.write(chunk)
        os.replace(temporary, output)
    except HTTPError as exc:
        detail = exc.read(2048).decode("utf-8", errors="replace")
        try:
            error = json.loads(detail).get("error", {})
            detail = f"{error.get('code', '')}: {error.get('message', '')}".strip(": ") or detail
        except (ValueError, AttributeError):
            pass
        raise AiServiceError(f"{target} 返回 HTTP {exc.code}：{detail}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise AiServiceError(f"无法下载 HTML 报告：{exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return output
