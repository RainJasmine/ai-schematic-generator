from __future__ import annotations

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from ai_schematic.graph_ir import GraphIR, GraphIrError
from ai_schematic.layout import (
    SymbolBounds,
    layout_nodes,
    layout_symbols_in_origin_rectangle,
    layout_symbols_sequentially,
    placement_order,
)


class GraphIrTests(unittest.TestCase):
    def _load(self, value: object) -> GraphIR:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "graph.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            return GraphIR.load(path)

    def test_single_port_object_is_normalized(self) -> None:
        graph = self._load(
            {
                "graph_id": "demo",
                "nodes": [
                    {
                        "node_id": "N1",
                        "component_id": "SOURCE",
                        "ports": {"port_id": "OUT", "pin_number": "1"},
                    }
                ],
                "edges": [],
            }
        )
        self.assertEqual(graph.nodes[0].ports[0].pin_number, "1")

    def test_bound_symbol_metadata_and_port_pin_identity(self) -> None:
        graph = self._load(
            {
                "graph_schema": "ai-eda-graph-ir/v2.0",
                "nodes": [
                    {
                        "node_id": "MIXER",
                        "component_id": "MIXER_BASIC",
                        "model_metadata": {
                            "library_name": "Mixer",
                            "kicad_symbol_name": "Mixer",
                            "kicad_library_path": r"F:\symbols\Mixers.kicad_sym",
                        },
                        "ports": [
                            {"port_id": "in_1", "pin_number": "1", "pin_name": "in",
                             "unit": 1, "body_style": 1, "direction": "input"}
                        ],
                    },
                    {"node_id": "NEXT", "ports": [{"port_id": "out", "pin_number": "2"}]},
                ],
                "edges": [
                    {"edge_id": "E1", "source_node_id": "NEXT", "source_port_id": "out",
                     "target_node_id": "MIXER", "target_port_id": "in_1",
                     "connection_status": "connected"}
                ],
            }
        )
        mixer = graph.node_map["MIXER"]
        self.assertEqual(mixer.search_terms()[0], "Mixer")
        self.assertEqual(mixer.library_id_hint(), ("Mixers", "Mixer"))
        self.assertEqual((mixer.port("in_1").pin_number, mixer.port("in_1").pin_name),
                         ("1", "in"))
        self.assertEqual((mixer.port("in_1").unit, mixer.port("in_1").body_style),
                         (1, 1))
        self.assertEqual([edge.edge_id for edge in graph.connections], ["E1"])

    def test_duplicate_node_ids_are_diagnosed_without_overwriting(self) -> None:
        graph = self._load(
            {
                "graph_schema": "ai-eda-graph-ir/v2.0",
                "schema_version": "2.0",
                "nodes": [{"node_id": "N1"}, {"node_id": "N1"}],
                "edges": [],
            }
        )
        self.assertEqual([node.node_id for node in graph.nodes], ["N1"])
        self.assertTrue(any(item.code == "NODE_ID_DUPLICATE" for item in graph.diagnostics))

    def test_pin_number_is_optional_but_endpoint_is_node_and_port(self) -> None:
        graph = self._load(
            {
                "graph_schema": "ai-eda-graph-ir/v2.0",
                "schema_version": "2.0",
                "nodes": [
                    {"node_id": "A", "ports": [{"port_id": "OUT", "direction": "output"}]},
                    {"node_id": "B", "ports": [{"port_id": "OUT", "direction": "input"}]},
                ],
                "edges": [
                    {
                        "edge_id": "AB",
                        "source_node_id": "A",
                        "source_port_id": "OUT",
                        "target_node_id": "B",
                        "target_port_id": "OUT",
                        "connection_status": "connected",
                    }
                ],
            }
        )
        self.assertEqual(graph.nodes[0].ports[0].pin_number, "")
        self.assertEqual([edge.edge_id for edge in graph.connections], ["AB"])

    def test_paths_validate_order_but_never_create_connections(self) -> None:
        graph = self._load(
            {
                "graph_schema": "ai-eda-graph-ir/v2.0",
                "schema_version": "2.0",
                "nodes": [{"node_id": "A"}, {"node_id": "B"}],
                "edges": [],
                "paths": [
                    {
                        "path_id": "main",
                        "path_type": "main_signal",
                        "ordered_nodes": ["A", "B"],
                    }
                ],
            }
        )
        self.assertEqual(graph.connections, ())
        self.assertFalse(graph.main_paths[0].complete)
        self.assertTrue(any(item.code == "PATH_EDGE_MISSING" for item in graph.diagnostics))

    def test_unconnected_and_missing_endpoint_edges_are_not_drawable(self) -> None:
        graph = self._load(
            {
                "graph_schema": "ai-eda-graph-ir/v2.0",
                "schema_version": "2.0",
                "nodes": [
                    {"node_id": "A", "ports": [{"port_id": "OUT"}]},
                    {"node_id": "B", "ports": [{"port_id": "IN"}]},
                ],
                "edges": [
                    {
                        "edge_id": "pending",
                        "source_node_id": "A",
                        "source_port_id": "OUT",
                        "target_node_id": "B",
                        "target_port_id": "IN",
                        "connection_status": "planned",
                    },
                    {
                        "edge_id": "bad-port",
                        "source_node_id": "A",
                        "source_port_id": "MISSING",
                        "target_node_id": "B",
                        "target_port_id": "IN",
                    },
                ],
            }
        )
        self.assertEqual(graph.connections, ())
        self.assertEqual({edge.edge_id for edge in graph.edges}, {"pending", "bad-port"})
        self.assertTrue(any(item.code == "EDGE_NOT_CONNECTED" for item in graph.diagnostics))
        self.assertTrue(any(item.code == "EDGE_SOURCE_PORT_MISSING" for item in graph.diagnostics))

    def test_branch_anchor_uses_shared_main_node(self) -> None:
        graph = self._load(
            {
                "graph_schema": "ai-eda-graph-ir/v2.0",
                "schema_version": "2.0",
                "nodes": [
                    {"node_id": "input", "ports": [{"port_id": "OUT"}]},
                    {
                        "node_id": "mixer",
                        "ports": [{"port_id": "RF"}, {"port_id": "LO"}],
                    },
                    {"node_id": "lo", "ports": [{"port_id": "OUT"}]},
                ],
                "edges": [
                    {
                        "edge_id": "RF",
                        "source_node_id": "input",
                        "source_port_id": "OUT",
                        "target_node_id": "mixer",
                        "target_port_id": "RF",
                    },
                    {
                        "edge_id": "LO",
                        "source_node_id": "lo",
                        "source_port_id": "OUT",
                        "target_node_id": "mixer",
                        "target_port_id": "LO",
                    },
                ],
                "paths": [
                    {"path_id": "main", "path_type": "main_signal", "ordered_nodes": ["input", "mixer"]},
                    {"path_id": "lo-path", "path_type": "local_oscillator", "ordered_nodes": ["lo", "mixer"]},
                ],
            }
        )
        self.assertEqual(graph.branches[0].anchor_node_ids, ("mixer",))
        self.assertEqual([node.node_id for node in graph.nodes].count("mixer"), 1)

    def test_unsupported_major_schema_stops_parsing(self) -> None:
        with self.assertRaisesRegex(GraphIrError, "GRAPH_SCHEMA_UNSUPPORTED"):
            self._load(
                {
                    "graph_schema": "ai-eda-graph-ir/v3.0",
                    "schema_version": "3.0",
                    "nodes": [],
                    "edges": [],
                }
            )

    def test_placement_order_is_main_then_anchored_branch_then_other_nodes(self) -> None:
        graph = self._load(
            {
                "graph_schema": "ai-eda-graph-ir/v2.0",
                "schema_version": "2.0",
                "nodes": [
                    {"node_id": "lo", "ports": [{"port_id": "OUT"}]},
                    {"node_id": "A", "ports": [{"port_id": "OUT"}]},
                    {"node_id": "B", "ports": [{"port_id": "IN"}, {"port_id": "LO"}]},
                    {"node_id": "free"},
                ],
                "edges": [
                    {"edge_id": "AB", "source_node_id": "A", "source_port_id": "OUT", "target_node_id": "B", "target_port_id": "IN"},
                    {"edge_id": "LO", "source_node_id": "lo", "source_port_id": "OUT", "target_node_id": "B", "target_port_id": "LO"},
                ],
                "paths": [
                    {"path_id": "z-main", "path_type": "main_signal", "ordered_nodes": ["A", "B"]},
                    {"path_id": "a-lo", "path_type": "local_oscillator", "ordered_nodes": ["lo", "B"]},
                ],
            }
        )
        self.assertEqual(placement_order(graph), ("A", "B", "lo", "free"))

    def test_layout_compacts_main_path_when_first_node_is_not_drawable(self) -> None:
        graph = self._load(
            {
                "graph_id": "drawable-subset",
                "nodes": [
                    {"node_id": node_id, "ports": []}
                    for node_id in ("missing", "A", "B")
                ],
                "edges": [],
                "paths": [
                    {
                        "path_id": "main",
                        "path_type": "main_signal",
                        "ordered_nodes": ["missing", "A", "B"],
                    }
                ],
            }
        )

        context = SimpleNamespace(
            visible_canvas=SimpleNamespace(
                position=SimpleNamespace(x_nm=0, y_nm=0),
                size=SimpleNamespace(x_nm=2_000_000, y_nm=1_000_000),
            ),
            grid=SimpleNamespace(x_nm=25_400, y_nm=25_400),
            cursor=SimpleNamespace(x_nm=0, y_nm=0),
            HasField=lambda _name: False,
        )

        positions = layout_nodes(graph, context, {"A", "B"})

        self.assertEqual(set(positions), {"A", "B"})
        self.assertLess(abs(positions["B"][0] - positions["A"][0]), 500_000)

    def test_sequential_layout_places_every_symbol_in_input_order(self) -> None:
        context = SimpleNamespace(
            visible_canvas=SimpleNamespace(
                position=SimpleNamespace(x_nm=0, y_nm=0),
                size=SimpleNamespace(x_nm=2_000_000, y_nm=1_000_000),
            ),
            grid=SimpleNamespace(x_nm=25_400, y_nm=25_400),
            cursor=SimpleNamespace(x_nm=0, y_nm=0),
            HasField=lambda _name: False,
        )
        node_ids = [f"N{index}" for index in range(7)]

        positions = layout_symbols_sequentially(node_ids, context)

        self.assertEqual(list(positions), node_ids)
        self.assertEqual(len(set(positions.values())), len(node_ids))
        self.assertLessEqual(len({value[1] for value in positions.values()}), 2)

    def test_origin_rectangle_uses_main_x_first_branch_down_second_branch_up(self) -> None:
        graph = self._load(
            {
                "graph_schema": "ai-eda-graph-ir/v2.0",
                "schema_version": "2.0",
                "nodes": [
                    {
                        "node_id": node_id,
                        "ports": [
                            {"port_id": "IN"},
                            {"port_id": "OUT"},
                            {"port_id": "BRANCH"},
                        ],
                    }
                    for node_id in ("A", "B", "D1", "D2", "U1")
                ],
                "edges": [
                    {"edge_id": "AB", "source_node_id": "A", "source_port_id": "OUT", "target_node_id": "B", "target_port_id": "IN"},
                    {"edge_id": "D12", "source_node_id": "D1", "source_port_id": "OUT", "target_node_id": "D2", "target_port_id": "IN"},
                    {"edge_id": "D2B", "source_node_id": "D2", "source_port_id": "OUT", "target_node_id": "B", "target_port_id": "BRANCH"},
                    {"edge_id": "U1B", "source_node_id": "U1", "source_port_id": "OUT", "target_node_id": "B", "target_port_id": "BRANCH"},
                ],
                "paths": [
                    {"path_id": "main", "path_type": "main_signal", "ordered_nodes": ["A", "B"]},
                    {"path_id": "z-first-down", "path_type": "branch", "ordered_nodes": ["D1", "D2", "B"]},
                    {"path_id": "a-second-up", "path_type": "branch", "ordered_nodes": ["U1", "B"]},
                ],
            }
        )
        bounds = {
            "A": SymbolBounds(-50_000, -40_000, 100_000, 80_000),
            "B": SymbolBounds(-100_000, -60_000, 200_000, 120_000),
            "D1": SymbolBounds(-40_000, -50_000, 80_000, 100_000),
            "D2": SymbolBounds(-30_000, -30_000, 60_000, 60_000),
            "U1": SymbolBounds(-40_000, -40_000, 80_000, 80_000),
        }

        layout = layout_symbols_in_origin_rectangle(
            graph,
            list(bounds),
            bounds,
            horizontal_step_nm=100_000,
            vertical_step_nm=50_000,
        )
        absolute = {
            node_id: (
                layout.positions[node_id][0] + geometry.left,
                layout.positions[node_id][1] + geometry.top,
                layout.positions[node_id][0] + geometry.right,
                layout.positions[node_id][1] + geometry.bottom,
            )
            for node_id, geometry in bounds.items()
        }

        self.assertEqual(min(box[0] for box in absolute.values()), 300_000)
        self.assertEqual(min(box[1] for box in absolute.values()), 300_000)
        self.assertEqual(max(box[2] for box in absolute.values()), 300_000 + layout.width_nm)
        self.assertEqual(max(box[3] for box in absolute.values()), 300_000 + layout.height_nm)
        self.assertEqual((layout.origin_x_nm, layout.origin_y_nm), (300_000, 300_000))
        self.assertEqual(layout.positions["B"][0] - layout.positions["A"][0], 100_000)
        self.assertEqual(layout.positions["D2"][1] - layout.positions["B"][1], 50_000)
        self.assertEqual(layout.positions["D1"][1] - layout.positions["D2"][1], 50_000)
        self.assertEqual(layout.positions["U1"][1] - layout.positions["B"][1], -50_000)


if __name__ == "__main__":
    unittest.main()
