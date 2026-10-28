from __future__ import annotations

import unittest

from schematic import schematic_types_pb2 as sch
from schematic import schematic_commands_pb2 as library

from ai_schematic import snapshot
from ai_schematic.graph_ir import GraphIR


def symbol(identity: str, x: int, name: str = "Bound"):
    item = sch.Symbol()
    item.id.value = identity
    item.position.x_nm = x
    item.library_id.entry_name = name
    item.unit = item.body_style = 1
    item.orientation = 3
    field = item.fields.add()
    field.name, field.field_id, field.value = "Reference", 0, "U1"
    pin = item.pins.add()
    pin.number, pin.name, pin.unit, pin.body_style = "1", "IN+", 1, 1
    pin.position.x_nm = x
    return item


def wire(identity: str, a: tuple, b: tuple):
    item = sch.Line()
    item.id.value = identity
    item.start.x_nm, item.start.y_nm = a
    item.end.x_nm, item.end.y_nm = b
    item.layer = sch.SL_WIRE
    return item


class SnapshotTests(unittest.TestCase):
    def ground_case(self):
        mixer = symbol("mixer", 0)
        ports = [{"port_id": "in", "pin_number": "1", "pin_name": "IN+"}]
        definition = library.SymbolLibraryDefinition()
        variant = definition.variants.add(unit=1, body_style=1)
        for number in ("4", "5"):
            actual = mixer.pins.add(number=number, name="GND", unit=1, body_style=1)
            actual.position.x_nm = actual.position.y_nm = 20
            local = variant.pins.add(number=number, name="GND", unit=1, body_style=1)
            local.position.CopyFrom(actual.position)
            ports.append({"port_id": "GND_" + number, "pin_number": number,
                "pin_name": "GND", "port_role": "ground", "domain": "POWER", "unit": 1, "body_style": 1})
        graph = GraphIR.parse({"nodes": [{"node_id": "mixer_1", "component_id": "Mixer", "ports": ports}], "edges": []})
        return graph, mixer, definition

    def test_library_stacked_grounds_are_one_physical_contact(self):
        graph, mixer, definition = self.ground_case()
        symbols = {"mixer_1": [mixer]}
        with self.assertRaisesRegex(ValueError, "未声明的引脚短接"):
            snapshot.validate_graph_connections(graph, symbols, [mixer])
        snapshot.validate_graph_connections(graph, symbols, [mixer], library_definitions={"mixer_1": definition})
        nets = snapshot.expected_pin_nets(graph, symbols, library_definitions={"mixer_1": definition})
        self.assertEqual(nets[("mixer_1", "GND_4")], nets[("mixer_1", "GND_5")])
        self.assertNotEqual(nets[("mixer_1", "in")], nets[("mixer_1", "GND_4")])

    def test_response_overlap_cannot_override_distinct_library_pins(self):
        graph, mixer, definition = self.ground_case()
        definition.variants[0].pins[1].position.x_nm += 10
        with self.assertRaisesRegex(ValueError, "未声明的引脚短接"):
            snapshot.validate_graph_connections(graph, {"mixer_1": [mixer]}, [mixer], library_definitions={"mixer_1": definition})

    def test_library_stack_with_different_response_positions_is_rejected(self):
        graph, mixer, definition = self.ground_case()
        mixer.pins[2].position.x_nm += 10
        with self.assertRaisesRegex(ValueError, "位置与符号库定义不一致"):
            snapshot.validate_graph_connections(graph, {"mixer_1": [mixer]}, [mixer], library_definitions={"mixer_1": definition})

    def test_signal_wire_touching_stacked_ground_still_fails(self):
        graph, mixer, definition = self.ground_case()
        line = wire("short", (0, 0), (20, 20))
        with self.assertRaisesRegex(ValueError, "未声明的引脚短接"):
            snapshot.validate_graph_connections(graph, {"mixer_1": [mixer]}, [mixer, line], library_definitions={"mixer_1": definition})

    def test_same_name_grounds_on_distinct_instances_do_not_merge(self):
        graph, mixer, definition = self.ground_case()
        other = sch.Symbol(); other.CopyFrom(mixer); other.id.value = "other"
        other.fields[0].value = "U2"
        raw = dict(graph.source); raw["nodes"] = [*raw["nodes"], {**raw["nodes"][0], "node_id": "mixer_2"}]
        with self.assertRaisesRegex(ValueError, "未声明的引脚短接"):
            snapshot.validate_graph_connections(GraphIR.parse(raw), {"mixer_1": [mixer], "mixer_2": [other]},
                [mixer, other], library_definitions={"mixer_1": definition, "mixer_2": definition})

    def test_stacked_signal_pins_do_not_get_ground_exception(self):
        graph, mixer, definition = self.ground_case()
        raw = dict(graph.source)
        raw["nodes"] = [{**raw["nodes"][0], "ports": [{**p, "port_role": "main_signal_input", "domain": "RF"} for p in raw["nodes"][0]["ports"]]}]
        with self.assertRaisesRegex(ValueError, "未声明的引脚短接"):
            snapshot.validate_graph_connections(GraphIR.parse(raw), {"mixer_1": [mixer]}, [mixer], library_definitions={"mixer_1": definition})

    def test_stacked_grounds_cannot_bridge_two_declared_independent_nets(self):
        graph, mixer, definition = self.ground_case()
        raw = dict(graph.source); raw["nodes"] = list(raw["nodes"]); raw["edges"] = []
        peers = []
        for number in ("4", "5"):
            identity = "peer" + number
            peer = symbol(identity, 100 * int(number)); peers.append(peer)
            raw["nodes"].append({"node_id": identity, "component_id": "Peer", "ports": [{"port_id": "in", "pin_number": "1", "pin_name": "IN+"}]})
            raw["edges"].append({"edge_id": "E" + number, "source_node_id": "mixer_1", "source_port_id": "GND_" + number,
                "target_node_id": identity, "target_port_id": "in", "connection_status": "connected"})
        with self.assertRaisesRegex(ValueError, "重叠接地脚被分配到不同网络"):
            snapshot.expected_pin_nets(GraphIR.parse(raw), {"mixer_1": [mixer], "peer4": [peers[0]], "peer5": [peers[1]]}, library_definitions={"mixer_1": definition})

    def test_single_connected_stacked_ground_preserves_alias_connectivity(self):
        graph, mixer, definition = self.ground_case()
        peer = symbol("peer", 100)
        raw = dict(graph.source)
        raw["nodes"] = [*raw["nodes"], {"node_id": "peer", "component_id": "Peer", "ports": [{"port_id": "in", "pin_number": "1", "pin_name": "IN+"}]}]
        raw["edges"] = [{"edge_id": "G", "source_node_id": "mixer_1", "source_port_id": "GND_4", "target_node_id": "peer", "target_port_id": "in", "connection_status": "connected"}]
        line = wire("ground", (20, 20), (100, 0))
        snapshot.validate_graph_connections(GraphIR.parse(raw), {"mixer_1": [mixer], "peer": [peer]},
            [mixer, peer, line], library_definitions={"mixer_1": definition})

    def test_translation_and_reference_alias_preserve_semantics(self):
        a, b = symbol("a", 0), symbol("b", 100)
        line = wire("w", (0, 0), (100, 0))
        owned = {"a", "b", "w"}
        saved = snapshot.make_snapshot([a, b, line], owned)
        a.position.y_nm += 25
        a.pins[0].position.y_nm += 25
        a.fields[0].value = "U99"
        b.position.y_nm += 25
        b.pins[0].position.y_nm += 25
        line.start.y_nm += 25
        line.end.y_nm += 25
        self.assertFalse(snapshot.changed(saved, [a, b, line], owned))

    def test_disconnect_or_model_parameter_change_is_dirty(self):
        a, b = symbol("a", 0), symbol("b", 100)
        line = wire("w", (0, 0), (100, 0))
        saved = snapshot.make_snapshot([a, b, line], {"a", "b", "w"})
        line.end.x_nm = 90
        self.assertTrue(snapshot.changed(saved, [a, b, line], {"a", "b", "w"}))
        line.end.x_nm = 100
        a.library_id.entry_name = "Other"
        self.assertTrue(snapshot.changed(saved, [a, b, line], {"a", "b", "w"}))

    def test_foreign_wire_attached_to_owned_net_is_dirty(self):
        a, b = symbol("a", 0), symbol("b", 100)
        line = wire("w", (0, 0), (100, 0))
        owned = {"a", "b", "w"}
        saved = snapshot.make_snapshot([a, b, line], owned)
        branch = wire("foreign", (50, 0), (50, 100))
        self.assertTrue(snapshot.changed(saved, [a, b, line, branch], owned))
        branch.start.y_nm = 50
        self.assertFalse(snapshot.changed(saved, [a, b, line, branch], owned))

    def test_serialization_roundtrip_preserves_physical_items(self):
        items = [symbol("a", 0), wire("w", (0, 0), (100, 0))]
        self.assertEqual(items, snapshot.restore_items(snapshot.encode_items(items)))

    def test_crossing_does_not_join_without_junction_but_t_does(self):
        horizontal = wire("h", (0, 50), (100, 50))
        vertical = wire("v", (50, 0), (50, 100))
        a, b = symbol("a", 0), symbol("b", 50)
        a.pins[0].position.y_nm = 50
        points, components = snapshot.net_components([horizontal, vertical, a, b])
        self.assertNotEqual(components[points[snapshot.pin_token(a, a.pins[0])]],
                            components[points[snapshot.pin_token(b, b.pins[0])]])
        vertical.start.y_nm = 50
        b.pins[0].position.y_nm = 100
        points, components = snapshot.net_components([horizontal, vertical, a, b])
        self.assertEqual(components[points[snapshot.pin_token(a, a.pins[0])]],
                         components[points[snapshot.pin_token(b, b.pins[0])]])


if __name__ == "__main__":
    unittest.main()
