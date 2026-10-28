from __future__ import annotations

import unittest

from schematic import schematic_types_pb2 as sch

from ai_schematic import snapshot


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
