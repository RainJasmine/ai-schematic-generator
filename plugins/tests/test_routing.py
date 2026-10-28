from __future__ import annotations

from types import SimpleNamespace
import unittest

from ai_schematic.layout import SymbolBounds
from ai_schematic.routing import (
    _intersects_body,
    _crossing_cost,
    _point_on_segment,
    _segments_intersect,
    choose_orientations,
    route,
    rotated_bounds,
)


class RoutingTests(unittest.TestCase):
    def test_grid_detour_keeps_physical_pin_endpoints_exact(self) -> None:
        source = (-50_000, -50_000, 50_000, 50_000)
        middle = (130_000, -50_000, 170_000, 50_000)
        target = (250_000, -50_000, 350_000, 50_000)
        start, end = (50_003, 7), (250_003, 7)
        segments = route(start, end, (0, 0), (300_000, 0), source, target,
                         [source, middle, target], [], grid_nm=25_400)
        self.assertIsNotNone(segments)
        self.assertEqual(segments[0][0], start)
        self.assertEqual(segments[-1][1], end)
        self.assertFalse(any(_intersects_body(a, b, middle) for a, b in segments))

    def test_search_budget_fails_without_partial_route(self) -> None:
        source = (-50_000, -50_000, 50_000, 50_000)
        target = (250_000, -50_000, 350_000, 50_000)
        self.assertIsNone(route((50_000, 0), (250_000, 0), (0, 0), (300_000, 0),
                                source, target, [source, target], [], max_search_states=1))

    def test_unrelated_wire_t_contact_is_avoided(self) -> None:
        source = (-50_000, -50_000, 50_000, 50_000)
        target = (250_000, -50_000, 350_000, 50_000)
        existing = ((150_000, 0), (150_000, 100_000))
        self.assertGreater(_crossing_cost(((50_000, 0), (250_000, 0)), [existing]), 0)
        segments = route((50_000, 0), (250_000, 0), (0, 0), (300_000, 0),
                         source, target, [source, target], [existing])
        self.assertIsNotNone(segments)
        self.assertFalse(any(_segments_intersect(segment, existing) for segment in segments or []))

    def test_only_explicit_same_net_can_share_a_pin_and_wire(self) -> None:
        source = (-50_000, -50_000, 50_000, 50_000)
        target = (250_000, -50_000, 350_000, 50_000)
        existing = ((50_000, 0), (150_000, 0))
        args = ((50_000, 0), (250_000, 0), (0, 0), (300_000, 0),
                source, target, [source, target], [existing])
        self.assertIsNone(route(*args))
        self.assertIsNone(route(*args, net_id="new-net", occupied_nets=["other-net"]))
        same = route(*args, net_id="shared-net", occupied_nets=["shared-net"])
        self.assertIsNotNone(same)
        self.assertTrue(any(_segments_intersect(segment, existing) for segment in same or []))

    def test_escape_leg_cannot_cross_a_third_body(self) -> None:
        source = (-50_000, -50_000, 50_000, 50_000)
        target = (250_000, -50_000, 350_000, 50_000)
        blocker = (55_000, -10_000, 58_000, 10_000)
        self.assertIsNone(route((50_000, 0), (250_000, 0), (0, 0), (300_000, 0),
                                source, target, [source, target, blocker], []))

    def test_unrelated_unused_pin_is_not_crossed(self) -> None:
        source = (-50_000, -50_000, 50_000, 50_000)
        target = (250_000, -50_000, 350_000, 50_000)
        pin = (150_000, 0)
        segments = route((50_000, 0), (250_000, 0), (0, 0), (300_000, 0),
                         source, target, [source, target], [], forbidden_points={pin})
        self.assertIsNotNone(segments)
        self.assertFalse(any(_point_on_segment(pin, segment) for segment in segments or []))

    def test_rotation_points_a_single_output_pin_toward_its_peer(self) -> None:
        pin = SimpleNamespace(number="1", position=SimpleNamespace(x_nm=50_000, y_nm=0))
        variant = SimpleNamespace(unit=1, body_style=1, pins=[pin])

        def symbol() -> object:
            node = SimpleNamespace(port=lambda _: SimpleNamespace(pin_number="1"))
            return SimpleNamespace(node=node, definition=SimpleNamespace(variants=[variant]),
                                   unit=1, body_style=1)

        edge = SimpleNamespace(source_node_id="source", source_port_id="OUT",
                               target_node_id="target", target_port_id="IN")
        graph = SimpleNamespace(connections=[edge])
        choices = choose_orientations(graph, {"source": symbol(), "target": symbol()},
                                      {"source": (0, 200_000), "target": (0, 0)})
        self.assertEqual(choices["source"], 4)  # right-facing pin turns upward
        self.assertEqual(choices["target"], 6)  # right-facing pin turns downward
        self.assertEqual(rotated_bounds(SymbolBounds(-20, -10, 40, 20), 4),
                         SymbolBounds(-10, -20, 20, 40))

    def test_route_avoids_intermediate_symbol_body(self) -> None:
        source = (-50_000, -50_000, 50_000, 50_000)
        middle = (130_000, -50_000, 170_000, 50_000)
        target = (250_000, -50_000, 350_000, 50_000)
        segments = route((50_000, 0), (250_000, 0), (0, 0), (300_000, 0),
                         source, target, [source, middle, target], [])
        self.assertIsNotNone(segments)
        assert segments is not None
        self.assertEqual(segments[0][0], (50_000, 0))
        self.assertEqual(segments[-1][1], (250_000, 0))
        self.assertFalse(any(_intersects_body(start, end, middle)
                             for start, end in segments))
        self.assertTrue(all(start[0] == end[0] or start[1] == end[1]
                            for start, end in segments))

    def test_route_avoids_an_existing_wire_when_a_detour_exists(self) -> None:
        source = (-50_000, -50_000, 50_000, 50_000)
        target = (250_000, -50_000, 350_000, 50_000)
        occupied = [((100_000, 0), (200_000, 0))]
        segments = route((50_000, 0), (250_000, 0), (0, 0), (300_000, 0),
                         source, target, [source, target], occupied)
        self.assertIsNotNone(segments)
        assert segments is not None
        self.assertFalse(any(a[1] == b[1] == 0 and
                             max(a[0], b[0]) > 100_000 and min(a[0], b[0]) < 200_000
                             for a, b in segments))


if __name__ == "__main__":
    unittest.main()
