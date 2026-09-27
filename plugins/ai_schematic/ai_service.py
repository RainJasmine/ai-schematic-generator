"""Upload a Word document to a local FastAPI service and read Graph IR JSON."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
import uuid


DEFAULT_URL = "http://127.0.0.1:8000/generate"
MAX_RESPONSE_BYTES = 32 * 1024 * 1024


class AiServiceError(RuntimeError):
    pass


def _read_json(response: object) -> object:
    data = response.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        raise AiServiceError("AI 返回的 JSON 超过 32 MiB")
    try:
        return json.loads(data.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AiServiceError(f"AI 返回的内容不是有效 JSON：{exc}") from exc


def upload_word(path: str | Path, url: str | None = None) -> dict:
    """POST multipart field ``file``; accept JSON or a same-origin JSON download URL."""
    source = Path(path)
    if source.suffix.lower() not in {".doc", ".docx"}:
        raise AiServiceError("请选择 .doc 或 .docx 文件")
    if not source.is_file():
        raise AiServiceError("Word 文件不存在")
    endpoint = url or os.environ.get("KICAD_AI_GENERATE_URL", DEFAULT_URL)
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise AiServiceError("AI 服务地址必须是 HTTP(S) URL")

    boundary = f"kicad-{uuid.uuid4().hex}"
    safe_name = source.name.replace('"', "_").replace("\r", "_").replace("\n", "_")
    prefix = (f"--{boundary}\r\n"
              f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'
              "Content-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document\r\n\r\n").encode()
    body = prefix + source.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    request = Request(endpoint, body, headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Accept": "application/json",
    }, method="POST")
    try:
        with urlopen(request, timeout=180) as response:
            payload = _read_json(response)
        if not isinstance(payload, dict):
            raise AiServiceError("AI 返回的 JSON 顶层必须是对象")
        # Common FastAPI patterns: direct Graph IR, a JSON envelope, or a
        # URL for a JSON file produced asynchronously by the endpoint.
        if "nodes" in payload and "edges" in payload:
            return payload
        for key in ("graph", "graph_ir", "result"):
            value = payload.get(key)
            if isinstance(value, dict) and "nodes" in value and "edges" in value:
                return value
        download = payload.get("download_url") or payload.get("file_url")
        if isinstance(download, str) and download:
            target = urljoin(endpoint, download)
            target_parts = urlparse(target)
            if (target_parts.scheme, target_parts.netloc) != (parsed.scheme, parsed.netloc):
                raise AiServiceError("JSON 下载地址必须与 AI 服务同源")
            with urlopen(Request(target, headers={"Accept": "application/json"}), timeout=60) as response:
                value = _read_json(response)
            if isinstance(value, dict) and "nodes" in value and "edges" in value:
                return value
        raise AiServiceError("AI 响应中找不到 Graph IR 的 nodes 与 edges")
    except HTTPError as exc:
        detail = exc.read(2048).decode("utf-8", errors="replace")
        raise AiServiceError(f"AI 服务返回 HTTP {exc.code}：{detail}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise AiServiceError(f"无法连接 AI 服务：{exc}") from exc
