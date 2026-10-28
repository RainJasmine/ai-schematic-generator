"""Secret-free recovery state isolated by service and KiCad project.

Word snapshots preserve the multipart filename and bytes for request_id retries.
Credentials and sensitive management forms never enter this local cache.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid
import time
import re
from contextlib import contextmanager
from copy import deepcopy

from .ai_service import AiServiceError, _request_id, _service_root, _validate_text

_FORBIDDEN = frozenset({"token", "access_token", "bearer_token", "api_key", "admin_key",
    "authorization", "x_ai_eda_admin_key", "password", "neo4j_password", "secret",
    "credential", "neo4j_credential", "provider_updates", "credentials"})
MAX_SESSION_BYTES = 64 * 1024 * 1024


class SessionConflict(AiServiceError):
    def __init__(self):
        super().__init__("同一工程会话已由其他窗口更新，请重新读取并核对，不应覆盖原请求或导入记录。",
                         code="SESSION_CONFLICT")


@contextmanager
def _file_lock(path: Path, timeout=3):
    """OS lock is released on process exit; no unsafe stale-lock deletion."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    deadline, acquired = time.monotonic() + timeout, False
    try:
        if stream.seek(0, 2) == 0:
            stream.write(b"0")
            stream.flush()
        while not acquired:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise SessionConflict()
                time.sleep(.025)
        yield
    finally:
        if acquired:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def _assert_secret_free(value: object) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).casefold().replace("-", "_")
            # 知识库同步批次是“源摘要16位-随机12位”，用于识别库数据代际，不是认证令牌。
            # 精确校验这一项，不能为所有 *_token 字段放行。
            library_identity = normalized == "kicad_sync_token" and (
                child is None or isinstance(child, str) and (child == "" or re.fullmatch(r"[0-9a-f]{16}-[0-9a-f]{12}", child) is not None))
            if normalized != "document_token" and not library_identity and (normalized in _FORBIDDEN or normalized.endswith(("_api_key", "_password", "_secret", "_token"))):
                raise ValueError("会话缓存禁止保存凭据、令牌或敏感管理表单")
            _assert_secret_free(child)
    elif isinstance(value, list):
        for child in value:
            _assert_secret_free(child)


def _digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, value: dict) -> None:
    data = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")
    if len(data) > MAX_SESSION_BYTES:
        raise ValueError("会话过大；历史记录应保存版本索引，不应重复保存所有完整图")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            temporary.chmod(0o600)
        os.replace(temporary, path)
        if os.name != "nt":
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class SessionStore:
    def __init__(self, directory: str | Path | None = None) -> None:
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / ".local" / "share"))
        self.directory = Path(directory) if directory is not None else base / "RFEDA" / "AI Schematic Generator" / "sessions"
        self._versions = {}

    @staticmethod
    def new_request_id(prefix: str = "create") -> str:
        return _request_id(f"{prefix}-{uuid.uuid4().hex}")

    def _path(self, service_url: str, project_id: str) -> tuple[Path, str, str]:
        root, project = _service_root(service_url), str(project_id)
        key = hashlib.sha256(json.dumps([root, project], ensure_ascii=False).encode()).hexdigest()
        return self.directory / f"{key}.json", root, project

    def _read(self, path, root, project):
        if not path.exists():
            return {}, 0
        try:
            if path.stat().st_size > MAX_SESSION_BYTES:
                raise ValueError("会话文件过大")
            payload = json.loads(path.read_text(encoding="utf-8"))
            if (payload.get("schema_version") != 2 or payload.get("service_url") != root
                    or payload.get("project_id") != project or not isinstance(payload.get("state"), dict)
                    or type(payload.get("revision")) is not int or payload["revision"] < 1):
                raise ValueError("会话来源或格式不匹配")
            _assert_secret_free(payload["state"])
            return payload["state"], payload["revision"]
        except (OSError, UnicodeError, ValueError, AttributeError) as exc:
            raise AiServiceError("本地会话无法安全读取；请保留缓存并核对原任务ID，勿重复提交。",
                                 code="SESSION_INVALID") from exc

    def load(self, service_url: str, project_id: str = "") -> dict:
        path, root, project = self._path(service_url, project_id)
        with _file_lock(path.with_suffix(".lock")):
            state, revision = self._read(path, root, project)
        self._versions[str(path)] = revision
        if revision and state:
            state["_store_revision"] = revision
        return state

    def save(self, service_url: str, project_id: str, state: dict, *, expected_version=None) -> int:
        if not isinstance(state, dict):
            raise ValueError("会话状态必须是JSON对象")
        _assert_secret_free(state)
        path, root, project = self._path(service_url, project_id)
        expected = expected_version if expected_version is not None else state.get("_store_revision", self._versions.get(str(path), 0))
        with _file_lock(path.with_suffix(".lock")):
            _, actual = self._read(path, root, project)
            if actual != expected:
                raise SessionConflict()
            saved = {key: value for key, value in state.items() if key != "_store_revision"}
            revision = actual + 1
            _atomic_json(path, {"schema_version": 2, "revision": revision,
                               "service_url": root, "project_id": project, "state": saved})
        state["_store_revision"] = revision
        self._versions[str(path)] = revision
        return revision

    def mutate(self, service_url: str, project_id: str, callback) -> dict:
        """Apply a small journal transition to the latest state under one OS lock.

        The callback must not perform HTTP/KiCad operations while holding the lock.
        It may raise to reject a conflicting logical request before any network IO.
        """
        path, root, project = self._path(service_url, project_id)
        with _file_lock(path.with_suffix(".lock")):
            current, revision = self._read(path, root, project)
            state = deepcopy(current)
            result = callback(state)
            if result is not None:
                state = result
            if not isinstance(state, dict):
                raise ValueError("会话状态必须是JSON对象")
            state.pop("_store_revision", None)
            _assert_secret_free(state)
            revision += 1
            _atomic_json(path, {"schema_version": 2, "revision": revision,
                               "service_url": root, "project_id": project, "state": state})
        self._versions[str(path)] = revision
        state["_store_revision"] = revision
        return state

    transaction = mutate

    def clear(self, service_url: str, project_id: str = "") -> None:
        """Clear this index, preserving documents and other sessions."""
        path, _, _ = self._path(service_url, project_id)
        # Record an empty version instead of deleting: old writers must still fail CAS.
        self.mutate(service_url, project_id, lambda current: {})

    def freeze_create(self, kind: str, path: str | Path = "", user_text: str = "", run_name: str = "") -> dict:
        """Snapshot Word bytes BEFORE sending, or freeze a text-only payload."""
        if kind not in {"word", "text"}:
            raise AiServiceError("仅支持Word或纯文字首轮任务", code="INVALID_INPUT")
        _validate_text(user_text, run_name, required=kind == "text")
        request_id = self.new_request_id()
        frozen = {"kind": kind, "request_id": request_id, "path": "", "user_text": user_text, "run_name": run_name}
        if kind == "word":
            source = Path(path)
            if source.suffix.lower() != ".docx" or not source.is_file():
                raise AiServiceError("请选择存在的.docx文件", code="INPUT_MISSING")
            # Basename contributes to the server request fingerprint.
            destination = self.directory / "uploads" / request_id / source.name
            destination.parent.mkdir(parents=True, exist_ok=False)
            try:
                with source.open("rb") as src, destination.open("xb") as dst:
                    while chunk := src.read(1024 * 1024):
                        dst.write(chunk)
                    dst.flush()
                    os.fsync(dst.fileno())
                if os.name != "nt":
                    destination.chmod(0o600)
                frozen.update(path=str(destination), filename=source.name, file_sha256=_digest_file(destination))
            except Exception:
                destination.unlink(missing_ok=True)
                destination.parent.rmdir()
                raise
        frozen["payload_sha256"] = self.payload_hash(frozen)
        return frozen

    @staticmethod
    def payload_hash(request: dict) -> str:
        keys = (("kind", "parent_job_id", "payload") if request.get("kind") == "revision" else
                ("kind", "request_id", "user_text", "run_name", "filename", "file_sha256"))
        payload = {key: request.get(key) for key in keys}
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @staticmethod
    def freeze_revision(parent_job_id: str, payload: dict) -> dict:
        if not re.fullmatch(r"[0-9a-f]{32}", parent_job_id):
            raise AiServiceError("父任务ID无效", code="INVALID_JOB_ID")
        from .ai_service import _validate_revision
        _validate_revision(payload)
        frozen = {"kind": "revision", "parent_job_id": parent_job_id, "payload": deepcopy(payload)}
        _assert_secret_free(frozen)
        frozen["payload_sha256"] = SessionStore.payload_hash(frozen)
        return frozen

    @staticmethod
    def verify_frozen(request: dict) -> bool:
        try:
            if request.get("kind") == "revision":
                from .ai_service import _validate_revision
                _validate_revision(request["payload"])
                return (bool(re.fullmatch(r"[0-9a-f]{32}", request["parent_job_id"]))
                        and request.get("payload_sha256") == SessionStore.payload_hash(request))
            _request_id(request["request_id"])
            _validate_text(request["user_text"], request["run_name"], required=request["kind"] == "text")
            if request.get("payload_sha256") != SessionStore.payload_hash(request):
                return False
            if request["kind"] == "text":
                return True
            if request["kind"] != "word":
                return False
            path = Path(request["path"])
            return (path.name == request["filename"] and path.suffix.lower() == ".docx"
                    and _digest_file(path) == request["file_sha256"])
        except (KeyError, TypeError, ValueError, OSError, AiServiceError):
            return False


def read_legacy(path: str | Path) -> dict:
    """Read a user-selected legacy file without changing it or trusting ownership.

    Only identifiers and the old object mapping are offered for explicit host
    verification. Never migrate a secret, old executable request or imported flag.
    A lost legacy POST has no immutable bytes/base/selection and cannot be replayed.
    """
    source = Path(path)
    if source.stat().st_size > MAX_SESSION_BYTES:
        raise AiServiceError("旧会话文件过大", code="LEGACY_SESSION_INVALID")
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("not an object")
        result = {"legacy_source": str(source), "history": []}
        for key in ("service_url", "current_service_url", "last_service_url"):
            if raw.get(key):
                result[key] = _service_root(raw[key])
        def job(value):
            if not isinstance(value, dict) or not re.fullmatch(r"[0-9a-f]{32}", str(value.get("job_id", ""))):
                return None
            identifier = value["job_id"]
            row = {"job_id": identifier, "status": value.get("status"),
                   "status_url": f"/api/v1/jobs/{identifier}", "run_name": str(value.get("run_name") or "")[:100]}
            for key, suffix in (("result_url", "/graph"), ("report_url", "/report"),
                                ("requirements_url", "/requirements"), ("revision_result_url", "/revision-result")):
                if value.get(key):
                    row[key] = f"/api/v1/jobs/{identifier}{suffix}"
            return row
        result["current_job"], result["last_job"] = job(raw.get("current_job")), job(raw.get("last_job"))
        placement = raw.get("placement")
        if isinstance(placement, dict):
            fields = {"document_token", "document_fingerprint", "item_ids", "item_fingerprints", "node_item_ids", "edge_item_ids"}
            result["placement"] = {key: deepcopy(value) for key, value in placement.items() if key in fields}
        if raw.get("pending"):
            pending = raw["pending"]
            request_id = pending.get("request_id") if isinstance(pending, dict) else None
            result["pending"] = {"legacy_incomplete": True}
            if isinstance(request_id, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", request_id):
                result["pending"]["request_id"] = request_id
        _assert_secret_free(result)
        return result
    except (ValueError, TypeError, AttributeError, KeyError, UnicodeError) as exc:
        raise AiServiceError("旧会话无法安全读取；请保留文件并核对原任务ID。", code="LEGACY_SESSION_INVALID") from exc
