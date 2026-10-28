from __future__ import annotations

import unittest
import os
from unittest.mock import patch

from common.commands import editor_commands_pb2 as commands
from schematic import schematic_commands_pb2 as library, schematic_types_pb2 as sch

from ai_schematic.api_client import ApiCapabilityError, ApiError, ApiTransportError, KiCadClient
from ai_schematic.generator import (GenerationUncertain, adopt_legacy_placement,
                                    capture_target, clear_graph_selection, generate,
                                    prepare_feedback, select_graph_objects, verify_uncertain)
from ai_schematic.graph_ir import GraphIR
from ai_schematic.routing import rotate, rotated_bounds
from ai_schematic.layout import SymbolBounds


def graph(*, extra: bool = False):
    nodes = [{"node_id": identity, "component_id": "Bound",
              "model_metadata": {"kicad_symbol_name": "Bound"},
              "ports": [{"port_id": port, "pin_number": number, "pin_name": name}]}
             for identity, port, number, name in (("A", "out", "2", "OUT"), ("B", "in", "1", "IN"))]
    if extra:
        nodes.append({"node_id": "C", "component_id": "Bound", "ports": []})
    return GraphIR.parse({"graph_schema": "ai-eda-graph-ir/v2.0", "nodes": nodes,
        "edges": [{"edge_id": "E", "source_node_id": "A", "source_port_id": "out",
                   "target_node_id": "B", "target_port_id": "in", "connection_status": "connected"}],
        "paths": [{"path_id": "main", "path_type": "main_signal", "ordered_nodes": ["A", "B"]}]})


class FakeClient:
    require_success = staticmethod(KiCadClient.require_success)

    def __init__(self):
        self.objects = {}
        self.context = library.ActiveSchematicContext()
        self.context.screen_filename = "target.kicad_sch"
        self.context.coordinate_unit = "eeschema_iu"
        self.context.grid.x_nm = self.context.grid.y_nm = 1000
        self.begins = 0
        self.creates = 0
        self.ends = []
        self.fail_symbol = False
        self.fail_line = False
        self.transport_line = False
        self.fail_rollback = False
        self.fail_commit = False
        self.unsupported = False
        self.wrong_rollback = False
        self.selected = []
        self.backup = {}
        self.page_clears = 0
        self.clear_unsupported = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def active_context(self):
        return self.context

    def get_items(self, document):
        if self.unsupported:
            raise ApiCapabilityError("no GetItems", code="AS_UNHANDLED")
        result = commands.GetItemsResponse(status=1)
        for item in self.objects.values():
            result.items.add().Pack(item)
        return result

    def selection(self, document):
        return commands.SelectionResponse()

    def clear_selection(self, document):
        self.selected.clear()

    def add_to_selection(self, document, identities):
        self.selected.extend(identities)

    def search_symbols(self, query, exact, **kwargs):
        result = library.SearchSymbolLibraryResponse()
        if query == "Bound":
            summary = result.symbols.add()
            summary.library_id.library_nickname = "Test"
            summary.library_id.entry_name = summary.display_name = "Bound"
            summary.reference_prefix = "U"
        return result

    def symbol_definition(self, library_id):
        result = library.SymbolLibraryDefinition()
        variant = result.variants.add(unit=1, body_style=1)
        variant.bounding_box.position.x_nm = variant.bounding_box.position.y_nm = -40000
        variant.bounding_box.size.x_nm = variant.bounding_box.size.y_nm = 80000
        for number, name, x in (("1", "IN", -50000), ("2", "OUT", 50000)):
            pin = variant.pins.add(number=number, name=name, unit=1, body_style=1)
            pin.position.x_nm = x
        return result

    def begin_commit(self):
        self.begins += 1
        self.backup = dict(self.objects)
        return "commit"

    def clear_schematic_page(self, document):
        if self.clear_unsupported:
            raise ApiCapabilityError("No clear command", code="KICAD_CLEAR_UNSUPPORTED")
        self.page_clears += 1
        count = len(self.objects)
        self.objects.clear()
        return count

    def delete_items(self, document, ids):
        for identity in ids:
            del self.objects[identity]

    def reconnect_for_verification(self):
        return None

    def create_items(self, document, items):
        self.creates += 1
        if isinstance(items[0], sch.Line) and self.transport_line:
            raise ApiTransportError("connection lost")
        result = commands.CreateItemsResponse(status=1)
        for original in items:
            item = type(original)()
            item.CopyFrom(original)
            entry = result.created_items.add()
            if (isinstance(item, sch.Symbol) and self.fail_symbol) or (isinstance(item, sch.Line) and self.fail_line):
                entry.status.code = commands.ISC_INVALID_DATA
                continue
            if isinstance(item, sch.Symbol):
                definitions = self.symbol_definition(item.library_id).variants
                definition = next(value for value in definitions
                                  if value.unit == item.unit and value.body_style == item.body_style)
                bounds = rotated_bounds(SymbolBounds(-40000, -40000, 80000, 80000), item.orientation)
                item.bounding_box.position.x_nm = item.position.x_nm + bounds.left
                item.bounding_box.position.y_nm = item.position.y_nm + bounds.top
                item.bounding_box.size.x_nm, item.bounding_box.size.y_nm = bounds.width, bounds.height
                common = [pin for value in definitions if value.unit == 0 for pin in value.pins]
                for pin in [*definition.pins, *common]:
                    actual = item.pins.add()
                    actual.number, actual.name = pin.number, pin.name
                    actual.unit, actual.body_style = pin.unit, pin.body_style
                    x, y = rotate((pin.position.x_nm, pin.position.y_nm), item.orientation)
                    actual.position.x_nm, actual.position.y_nm = item.position.x_nm+x, item.position.y_nm+y
            entry.status.code = commands.ISC_OK
            entry.item.Pack(item)
            self.objects[item.id.value] = item
        return result

    def end_commit(self, identity, commit, message=""):
        self.ends.append(commit)
        if not commit:
            if self.fail_rollback:
                raise ApiTransportError("rollback disconnected")
            if not self.wrong_rollback:
                self.objects = dict(self.backup)
        elif self.fail_commit:
            raise ApiTransportError("commit acknowledgement lost")


class GeneratorTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.patch = patch("ai_schematic.generator.KiCadClient", return_value=self.client)
        mock = self.patch.start()
        mock.require_success = KiCadClient.require_success
        self.addCleanup(self.patch.stop)

    def test_import_snapshot_and_duplicate_are_safe_noop(self):
        target = capture_target()
        report = generate(graph(), lambda _: None, target=target)
        count = len(self.client.objects)
        self.assertEqual(report.created_nodes, ["A", "B"])
        self.assertEqual(report.created_edges, ["E"])
        self.assertTrue(report.revision_supported)
        self.assertFalse(prepare_feedback(report.to_dict())["client_graph_dirty"])
        repeated = generate(graph(), lambda _: None, target=target, previous=report.to_dict())
        self.assertTrue(repeated.reused)
        self.assertEqual(len(self.client.objects), count)
        self.assertEqual(self.client.begins, 1)

    def test_partial_symbol_or_wire_failure_rolls_back_all(self):
        for attr in ("fail_symbol", "fail_line"):
            with self.subTest(attr=attr):
                setattr(self.client, attr, True)
                with self.assertRaises(ApiError) as error:
                    generate(graph(), lambda _: None)
                self.assertTrue(error.exception.rollback_verified)
                self.assertEqual(self.client.objects, {})
                self.assertFalse(self.client.ends[-1])
                setattr(self.client, attr, False)

    def test_missing_model_and_invalid_graph_never_begin_mutation(self):
        raw = dict(graph().source)
        raw["nodes"] = [{"node_id": "bad", "component_id": "Missing", "ports": []}]
        raw["edges"] = []
        with self.assertRaisesRegex(ApiError, "精确型号"):
            generate(GraphIR.parse(raw), lambda _: None)
        self.assertEqual(self.client.begins, 0)
        raw["nodes"] *= 2
        with self.assertRaisesRegex(ApiError, "阻断错误"):
            generate(GraphIR.parse(raw), lambda _: None)
        self.assertEqual(self.client.begins, 0)

    def test_target_change_rejected_before_creating(self):
        target = capture_target()
        self.client.context.screen_filename = "other.kicad_sch"
        with self.assertRaisesRegex(ApiError, "图纸已切换"):
            generate(graph(), lambda _: None, target=target)
        self.assertEqual(self.client.begins, 0)

    def test_unknown_coordinate_unit_requires_verified_calibration(self):
        self.client.context.coordinate_unit = "nm"
        with patch.dict(os.environ, {"RF_EDA_SCHEMATIC_UNITS_PER_MM": ""}):
            with self.assertRaises(ApiCapabilityError) as error:
                capture_target()
            self.assertEqual(error.exception.code, "KICAD_COORDINATE_UNIT_UNVERIFIED")
        with patch.dict(os.environ, {"RF_EDA_SCHEMATIC_UNITS_PER_MM": "10000"}):
            self.assertEqual(capture_target()["units_per_mm"], 10000)
        with patch.dict(os.environ, {"RF_EDA_SCHEMATIC_UNITS_PER_MM": "1000000"}):
            with self.assertRaises(ApiCapabilityError):
                capture_target()

    def test_host_schematic_internal_unit_imports_with_standard_iu_geometry(self):
        self.client.context.coordinate_unit = "schematic_internal_unit"
        with patch.dict(os.environ, {"RF_EDA_SCHEMATIC_UNITS_PER_MM": ""}):
            target = capture_target()
            self.assertEqual(target["units_per_mm"], 10000)
            self.assertEqual(target["coordinate_unit"], "schematic_internal_unit")
            report = generate(graph(), lambda _: None, target=target)
        self.assertEqual(report.created_nodes, ["A", "B"])
        self.assertEqual(report.created_edges, ["E"])
        self.assertTrue(report.revision_supported)

    def test_readback_missing_disables_mutation(self):
        self.client.unsupported = True
        with self.assertRaises(ApiCapabilityError):
            generate(graph(), lambda _: None)
        self.assertEqual(self.client.begins, 0)

    def test_initial_create_without_readback_uses_existing_write_apis_only(self):
        with patch.object(self.client, "get_items", side_effect=AssertionError("Unexpected full read")):
            target = capture_target(readback=False)
            report = generate(graph(), lambda _: None, target=target)
            self.assertEqual(report.created_nodes, ["A", "B"])
            self.assertEqual(report.created_edges, ["E"])
            self.assertFalse(report.readback_verified)
            self.assertFalse(report.revision_supported)
            self.assertEqual(report.snapshot, {})
            repeated = generate(graph(), lambda _: None, target=target, previous=report.to_dict())
            self.assertTrue(repeated.reused)
            self.assertEqual(self.client.begins, 1)

    def test_full_page_update_without_snapshot_never_deletes_existing_objects(self):
        target = capture_target(readback=False)
        report = generate(graph(), lambda _: None, target=target)
        old = {identity: item.SerializeToString() for identity, item in self.client.objects.items()}
        with patch.object(self.client, "get_items", side_effect=AssertionError("Unexpected full read")), \
             patch.object(self.client, "delete_items", side_effect=AssertionError("Unexpected delete")):
            with self.assertRaises(ApiCapabilityError) as error:
                generate(graph(extra=True), lambda _: None, target=target, previous=report.to_dict())
        self.assertEqual(error.exception.code, "KICAD_FULL_REPLACE_UNAVAILABLE")
        self.assertEqual(old, {identity: item.SerializeToString() for identity, item in self.client.objects.items()})
        self.assertEqual(self.client.begins, 1)

    def test_initial_write_failure_rolls_back_without_claiming_readback_verification(self):
        self.client.fail_line = True
        target = capture_target(readback=False)
        with patch.object(self.client, "get_items", side_effect=AssertionError("Unexpected full read")):
            with self.assertRaises(ApiError) as error:
                generate(graph(), lambda _: None, target=target)
        self.assertTrue(error.exception.rollback_confirmed)
        self.assertFalse(error.exception.rollback_verified)
        self.assertEqual(self.client.objects, {})

    def test_dirty_old_graph_blocks_replacement(self):
        report = generate(graph(), lambda _: None)
        item = self.client.objects[report.node_items["A"][0]]
        item.library_id.entry_name = "Changed"
        self.assertTrue(prepare_feedback(report.to_dict())["client_graph_dirty"])
        with self.assertRaisesRegex(ApiError, "未同步"):
            generate(graph(extra=True), lambda _: None, previous=report.to_dict())
        self.assertEqual(self.client.begins, 1)

    def test_replacement_preserves_owned_positions_and_unrelated_items(self):
        report = generate(graph(), lambda _: None)
        position = self.client.objects[report.node_items["A"][0]].position.SerializeToString()
        other = sch.Line()
        other.id.value, other.layer = "foreign", sch.SL_WIRE
        other.start.y_nm, other.end.y_nm = -500000, -500000
        other.end.x_nm = 10000
        self.client.objects[other.id.value] = other
        revised = generate(graph(extra=True), lambda _: None, previous=report.to_dict())
        self.assertEqual(position, self.client.objects[revised.node_items["A"][0]].position.SerializeToString())
        self.assertIn("foreign", self.client.objects)
        self.assertTrue(all(identity not in self.client.objects for identity in report.node_items["A"]))

    def test_rollback_failure_is_uncertain_and_checkpoint_blocks_replay(self):
        self.client.transport_line = self.client.fail_rollback = True
        checkpoints = []
        with self.assertRaises(GenerationUncertain):
            generate(graph(), lambda _: None, on_checkpoint=checkpoints.append)
        self.assertTrue(checkpoints[-1]["planned_ids"])
        with self.assertRaises(GenerationUncertain):
            verify_uncertain(checkpoints[-1])
        self.client.objects.clear()  # user explicitly undid partial objects
        self.assertTrue(verify_uncertain(checkpoints[-1])["verified"])

    def test_lost_commit_ack_is_uncertain_not_replayed_or_dropped(self):
        self.client.fail_commit = True
        with self.assertRaises(GenerationUncertain):
            generate(graph(), lambda _: None)
        self.assertEqual(self.client.ends, [True])

    def test_old_dev_mapping_can_be_adopted_without_creating_objects(self):
        report = generate(graph(), lambda _: None)
        legacy = {"document_token": report.document_token, "item_ids": report.item_ids,
                  "node_item_ids": report.node_item_ids, "edge_item_ids": report.edge_item_ids,
                  "item_fingerprints": {}, "document_fingerprint": ""}
        before = len(self.client.objects), self.client.begins, self.client.creates
        adopted = adopt_legacy_placement(legacy, graph())
        self.assertEqual(adopted.node_items, report.node_items)
        self.assertTrue(adopted.revision_supported)
        self.assertEqual(before, (len(self.client.objects), self.client.begins, self.client.creates))
        changed = self.client.objects[report.node_items["A"][0]].fields.add()
        changed.name, changed.value, changed.field_id = "Gain", "30", 5
        with self.assertRaises(ApiError) as error:
            adopt_legacy_placement(legacy, graph())
        self.assertEqual(error.exception.code, "CLIENT_GRAPH_UNSYNCED")

    def test_target_canvas_change_during_model_lookup_aborts_before_journal(self):
        report = generate(graph(), lambda _: None)
        original = self.client.symbol_definition
        def changed(library_id):
            self.client.objects[report.node_items["A"][0]].library_id.entry_name = "Edited"
            return original(library_id)
        checkpoints = []
        with patch.object(self.client, "symbol_definition", side_effect=changed):
            with self.assertRaises(ApiError):
                generate(graph(extra=True), lambda _: None, previous=report.to_dict(), on_checkpoint=checkpoints.append)
        self.assertEqual(checkpoints, [])
        self.assertEqual(self.client.begins, 1)
        self.assertIn(report.node_items["A"][0], self.client.objects)

    def test_rollback_ack_without_restored_state_is_uncertain(self):
        self.client.fail_line = self.client.wrong_rollback = True
        with self.assertRaises(GenerationUncertain):
            generate(graph(), lambda _: None)
        self.assertTrue(self.client.objects)

    def test_journal_failure_never_begins_transaction(self):
        def cannot_save(checkpoint):
            raise OSError("disk full")
        with self.assertRaises(ApiError) as error:
            generate(graph(), lambda _: None, on_checkpoint=cannot_save)
        self.assertTrue(error.exception.rollback_verified)
        self.assertEqual(self.client.begins, 0)

    def test_selection_optional_for_whole_graph_feedback_and_safe_highlights(self):
        report = generate(graph(), lambda _: None)
        with patch.object(self.client, "selection", side_effect=ApiCapabilityError("no selection")):
            prepared = prepare_feedback(report.to_dict())
        self.assertEqual(prepared["selection"], {"node_ids": [], "edge_ids": []})
        self.assertTrue(prepared["warnings"])
        select_graph_objects(report.to_dict(), ["A"], ["E"])
        self.assertEqual(set(self.client.selected), set(report.node_items["A"] + report.edge_items["E"]))
        clear_graph_selection(report.to_dict())
        self.assertEqual(self.client.selected, [])
        with self.assertRaises(ApiError):
            select_graph_objects(report.to_dict(), ["foreign"])

    def test_multiunit_symbols_share_reference_and_preserve_unit_placement(self):
        original = self.client.symbol_definition
        def multi(library_id):
            value = original(library_id)
            first = value.variants[0]
            del first.pins[1:]
            second = value.variants.add(unit=2, body_style=1)
            second.bounding_box.CopyFrom(first.bounding_box)
            pin = second.pins.add(number="2", name="OUT", unit=2, body_style=1)
            pin.position.x_nm = 50000
            common = value.variants.add(unit=0, body_style=1)
            pin = common.pins.add(number="3", name="GND", unit=0, body_style=1)
            pin.position.y_nm = 50000
            return value
        raw = graph().source
        for node in raw["nodes"]:
            node["ports"] = [{"port_id": "in", "pin_number": "1", "pin_name": "IN", "unit": 1},
                             {"port_id": "out", "pin_number": "2", "pin_name": "OUT", "unit": 2},
                             {"port_id": "gnd", "pin_number": "3", "pin_name": "GND", "unit": 0}]
        multi_graph = GraphIR.parse(raw)
        with patch.object(self.client, "symbol_definition", side_effect=multi):
            report = generate(multi_graph, lambda _: None)
            self.assertEqual(len(report.node_items["A"]), 2)
            self.assertEqual({self.client.objects[i].fields[0].value for i in report.node_items["A"]}, {"U1"})
            self.assertFalse(prepare_feedback(report.to_dict())["client_graph_dirty"])


if __name__ == "__main__":
    unittest.main()
