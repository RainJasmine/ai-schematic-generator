"""Local non-secret state for resuming AI design jobs and graph revisions."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile


def default_session_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return base / "SunvEDA" / "AI Schematic Generator" / "session.json"


class SessionStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_session_path()

    def load(self) -> dict:
        if not self.path.is_file():
            return {}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("本地设计状态格式不正确")
        return value

    def save(self, state: dict) -> None:
        if any(key in state for key in ("token", "api_key", "admin_key", "password")):
            raise ValueError("本地设计状态不能保存访问令牌或密钥")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent,
                                             suffix=".json.part", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(state, stream, ensure_ascii=False, separators=(",", ":"))
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
