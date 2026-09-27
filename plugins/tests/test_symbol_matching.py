from __future__ import annotations

from types import SimpleNamespace
import unittest

from ai_schematic.graph_ir import GraphIR
from ai_schematic.symbol_matching import candidate_score, matching_variant


class SymbolMatchingTests(unittest.TestCase):
    def test_new_binding_uses_pin_number_name_and_variant(self) -> None:
        graph = GraphIR.parse({
            "nodes": [{
                "node_id": "MIXER", "model_metadata": {
                    "kicad_symbol_name": "Mixer",
                    "kicad_library_path": r"F:\symbols\Mixers.kicad_sym",
                },
                "ports": [{"port_id": "in_1", "pin_number": "1", "pin_name": "in",
                           "unit": 2, "body_style": 1}],
            }],
            "edges": [],
        })
        node = graph.nodes[0]
        wrong_name = SimpleNamespace(number="1", name="out")
        right_name = SimpleNamespace(number="1", name="in")
        variants = [
            SimpleNamespace(unit=1, body_style=1, pins=[right_name]),
            SimpleNamespace(unit=2, body_style=1, pins=[wrong_name]),
            SimpleNamespace(unit=2, body_style=1, pins=[right_name]),
        ]
        self.assertIs(matching_variant(node, SimpleNamespace(variants=variants)), variants[2])
        self.assertIsNone(matching_variant(node, SimpleNamespace(variants=variants[:2])))
        preferred = SimpleNamespace(display_name="Mixer", library_id=SimpleNamespace(
            library_nickname="Mixers", entry_name="Mixer"))
        other = SimpleNamespace(display_name="Mixer", library_id=SimpleNamespace(
            library_nickname="Other", entry_name="Mixer"))
        self.assertGreater(candidate_score(node, preferred), candidate_score(node, other))


if __name__ == "__main__":
    unittest.main()
