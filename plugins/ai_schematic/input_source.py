"""Load Graph IR from a local JSON file or from the Word AI service."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Literal

from .ai_service import ServiceClient, _terminal_graph, upload_word
from .session_store import SessionStore
from .graph_ir import GraphIR


InputKind = Literal["word", "json", "text"]


def load_graph_input(path: str | Path, kind: InputKind, endpoint: str = "", *,
                     token: str = "", user_text: str = "", run_name: str = "",
                     on_progress: Callable[[dict], None] | None = None,
                     request_id: str | None = None, stop_event=None) -> GraphIR:
    if kind == "json":
        return GraphIR.load(path)
    if kind == "word":
        extra = {}
        if request_id is not None:
            extra["request_id"] = request_id
        if stop_event is not None:
            extra["stop_event"] = stop_event
        return GraphIR.parse(upload_word(path, endpoint, token=token, user_text=user_text,
                                         run_name=run_name, on_progress=on_progress, **extra))
    if kind == "text":
        # Compatibility helper; the UI uses a persisted frozen request instead.
        client = ServiceClient(endpoint, token)
        job = client.submit_text(user_text, run_name=run_name,
                                 request_id=request_id or SessionStore.new_request_id())
        if on_progress:
            on_progress(job)
        terminal = client.follow_job(job["status_url"], on_progress=on_progress, stop_event=stop_event)
        return GraphIR.parse(_terminal_graph(client, terminal))
    raise ValueError(f"不支持的输入类型：{kind}")
