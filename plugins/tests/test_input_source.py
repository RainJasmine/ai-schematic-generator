from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_schematic.input_source import load_graph_input


class InputSourceTests(unittest.TestCase):
    def test_local_json_generates_graph_without_upload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "local-graph.json"
            path.write_text(json.dumps({"nodes": [], "edges": []}), encoding="utf-8")
            with patch("ai_schematic.input_source.upload_word") as upload:
                graph = load_graph_input(path, "json", "http://127.0.0.1:8000/generate")
            upload.assert_not_called()
        self.assertEqual(graph.graph_id, "local-graph")

    def test_word_keeps_upload_path(self) -> None:
        with patch("ai_schematic.input_source.upload_word",
                   return_value={"graph_id": "remote", "nodes": [], "edges": []}) as upload:
            graph = load_graph_input("proposal.docx", "word", "http://127.0.0.1:8000/generate")
        upload.assert_called_once_with("proposal.docx", "http://127.0.0.1:8000/generate",
                                       token="", user_text="", run_name="", on_progress=None)
        self.assertEqual(graph.graph_id, "remote")


if __name__ == "__main__":
    unittest.main()
