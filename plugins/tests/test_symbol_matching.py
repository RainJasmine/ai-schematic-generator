from __future__ import annotations

from types import SimpleNamespace
import unittest

from ai_schematic.graph_ir import GraphIR
from ai_schematic.symbol_matching import binding_matches, candidate_score, matching_variant, matching_variants


class SymbolMatchingTests(unittest.TestCase):
    def _node(self, ports: list[dict], **metadata: str) -> object:
        return GraphIR.parse({"nodes": [{"node_id": "N", "ports": ports,
                                        "model_metadata": metadata}], "edges": []}).nodes[0]

    def test_pin_name_preserves_polarity_and_unicode(self) -> None:
        for expected, actual in (("IN+", "IN-"), ("输入", "输出"), ("~{ENABLE}", "ENABLE")):
            with self.subTest(expected=expected, actual=actual):
                node = self._node([{"port_id": "P", "pin_number": "1", "pin_name": expected}])
                variant = SimpleNamespace(unit=1, body_style=1,
                                          pins=[SimpleNamespace(number="1", name=actual)])
                self.assertIsNone(matching_variant(node, SimpleNamespace(variants=[variant])))

    def test_bound_entry_cannot_be_replaced_by_same_pin_generic_symbol(self) -> None:
        node = self._node([], kicad_symbol_name="ActualMixer",
                          kicad_library_path=r"C:\server\RF.kicad_sym")
        generic = SimpleNamespace(library_id=SimpleNamespace(library_nickname="RF", entry_name="Mixer"))
        alias = SimpleNamespace(library_id=SimpleNamespace(library_nickname="ClientAlias", entry_name="ActualMixer"))
        self.assertFalse(binding_matches(node, generic))
        self.assertTrue(binding_matches(node, alias))
        explicit = self._node([], kicad_symbol_name="RF:ActualMixer")
        self.assertFalse(binding_matches(explicit, alias))

    def test_multiple_units_and_common_unit_zero_pins_are_covered(self) -> None:
        node = self._node([
            {"port_id": "A", "pin_number": "1", "pin_name": "A", "unit": 1},
            {"port_id": "B", "pin_number": "2", "pin_name": "B", "unit": 2},
            {"port_id": "G", "pin_number": "3", "pin_name": "GND", "unit": 0, "body_style": 0},
        ])
        variants = [
            SimpleNamespace(unit=0, body_style=0, pins=[SimpleNamespace(number="3", name="GND")]),
            SimpleNamespace(unit=1, body_style=1, pins=[SimpleNamespace(number="1", name="A")]),
            SimpleNamespace(unit=2, body_style=1, pins=[SimpleNamespace(number="2", name="B")]),
        ]
        definition = SimpleNamespace(variants=variants)
        self.assertEqual({v.unit for v in matching_variants(node, definition)}, {1, 2})
        self.assertIsNone(matching_variant(node, definition))

    def test_common_unit_marker_does_not_select_an_ordinary_unit_pin(self) -> None:
        node = self._node([{"port_id": "P", "pin_number": "1", "unit": 0}])
        definition = SimpleNamespace(variants=[SimpleNamespace(
            unit=1, body_style=1, pins=[SimpleNamespace(number="1", name="IN", unit=1)])])
        self.assertEqual(matching_variants(node, definition), ())

    def test_alternative_body_styles_are_not_combined(self) -> None:
        node = self._node([
            {"port_id": "A", "pin_number": "1", "body_style": 1},
            {"port_id": "B", "pin_number": "2", "body_style": 2},
        ])
        variants = [SimpleNamespace(unit=1, body_style=style,
                                    pins=[SimpleNamespace(number=str(style), name="")]) for style in (1, 2)]
        self.assertEqual(matching_variants(node, SimpleNamespace(variants=variants)), ())

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
