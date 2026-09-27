"""Load Graph IR from a local JSON file or from the Word AI service."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from .ai_service import upload_word
from .graph_ir import GraphIR


InputKind = Literal["word", "json"]


def load_graph_input(path: str | Path, kind: InputKind, endpoint: str = "") -> GraphIR:
    if kind == "json":
        return GraphIR.load(path)
    if kind == "word":
        return GraphIR.parse(upload_word(path, endpoint))
    raise ValueError(f"不支持的输入类型：{kind}")
