"""Load Graph IR from a local JSON file or from the Word AI service."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Literal

from .ai_service import upload_word
from .graph_ir import GraphIR


InputKind = Literal["word", "json"]


def load_graph_input(path: str | Path, kind: InputKind, endpoint: str = "", *,
                     token: str = "", user_text: str = "", run_name: str = "",
                     on_progress: Callable[[dict], None] | None = None) -> GraphIR:
    if kind == "json":
        return GraphIR.load(path)
    if kind == "word":
        return GraphIR.parse(upload_word(path, endpoint, token=token, user_text=user_text,
                                         run_name=run_name, on_progress=on_progress))
    raise ValueError(f"不支持的输入类型：{kind}")
