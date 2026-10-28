"""Legacy session reader; new UI uses namespaced session_store.SessionStore.

The historical default file is read-only. Explicit paths remain supported for
legacy integrations/tests, with recursive credential validation and atomic IO.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from .session_store import MAX_SESSION_BYTES, _assert_secret_free, read_legacy


def default_session_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return base / "RFEDA" / "AI Schematic Generator" / "legacy-session.json"


class SessionStore:
    def __init__(self, path: Path | None = None) -> None:
        self._explicit_path = path is not None
        self.path = path or default_session_path()

    def load(self) -> dict:
        if not self.path.is_file():
            return {}
        if self.path.stat().st_size > MAX_SESSION_BYTES:
            raise ValueError("旧会话文件过大")
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("本地设计状态格式不正确")
        _assert_secret_free(value)
        return value

    def save(self, state: dict) -> None:
        if not self._explicit_path:
            raise ValueError("历史默认会话只读；请使用按服务和工程隔离的新会话存储")
        _assert_secret_free(state)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent,
                                             suffix=".json.part", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(state, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
