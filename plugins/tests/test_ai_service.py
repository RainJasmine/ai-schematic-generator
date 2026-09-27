from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_schematic.ai_service import AiServiceError, upload_word
from ai_schematic.graph_ir import GraphIR


class _Response(BytesIO):
    pass


class AiServiceTests(unittest.TestCase):
    def _word(self, directory: str) -> Path:
        path = Path(directory) / "input.docx"
        path.write_bytes(b"word-data")
        return path

    def test_multipart_upload_and_direct_graph(self) -> None:
        graph = {"graph_schema": "ai-eda-graph-ir/v2.0", "nodes": [], "edges": []}

        def fake_open(request: object, timeout: int) -> _Response:
            self.assertEqual(timeout, 180)
            self.assertEqual(request.get_method(), "POST")
            self.assertIn(b'name="file"; filename="input.docx"', request.data)
            self.assertIn(b"word-data", request.data)
            return _Response(json.dumps(graph).encode())

        with tempfile.TemporaryDirectory() as directory:
            with patch("ai_schematic.ai_service.urlopen", side_effect=fake_open):
                result = upload_word(self._word(directory))
        self.assertEqual(GraphIR.parse(result).graph_schema, "ai-eda-graph-ir/v2.0")

    def test_download_url_must_be_same_origin(self) -> None:
        payload = {"download_url": "http://other.example/graph.json"}
        with tempfile.TemporaryDirectory() as directory:
            with patch("ai_schematic.ai_service.urlopen",
                       return_value=_Response(json.dumps(payload).encode())):
                with self.assertRaisesRegex(AiServiceError, "同源"):
                    upload_word(self._word(directory))

    def test_same_origin_download(self) -> None:
        envelope = {"download_url": "/output/graph.json"}
        graph = {"nodes": [], "edges": []}
        replies = [_Response(json.dumps(envelope).encode()),
                   _Response(json.dumps(graph).encode())]
        with tempfile.TemporaryDirectory() as directory:
            with patch("ai_schematic.ai_service.urlopen", side_effect=replies) as opened:
                self.assertEqual(upload_word(self._word(directory)), graph)
        self.assertEqual(opened.call_args.args[0].full_url,
                         "http://127.0.0.1:8000/output/graph.json")


if __name__ == "__main__":
    unittest.main()
