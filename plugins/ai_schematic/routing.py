"""Choose symbol rotations and route orthogonal wires around placed symbols."""

from __future__ import annotations

from heapq import heappop, heappush

from .layout import SCHEMATIC_IU_PER_MM, SymbolBounds


Point = tuple[int, int]
Segment = tuple[Point, Point]
CLEARANCE = SCHEMATIC_IU_PER_MM
BEND_COST = SCHEMATIC_IU_PER_MM
CROSSING_COST = 100 * SCHEMATIC_IU_PER_MM

# These are SYMBOL_ORIENTATION_T values from eeschema/symbol.h.
ORIENTATIONS = (3, 4, 5, 6)


def rotate(point: Point, orientation: int) -> Point:
    x, y = point
    return ((x, y), (y, -x), (-x, -y), (-y, x))[orientation - 3]


def rotated_bounds(bounds: SymbolBounds, orientation: int) -> SymbolBounds:
    corners = (
        (bounds.left, bounds.top), (bounds.right, bounds.top),
        (bounds.left, bounds.bottom), (bounds.right, bounds.bottom),
    )
    xs, ys = zip(*(rotate(point, orientation) for point in corners))
    return SymbolBounds(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))


def choose_orientations(graph: object, resolved: dict, positions: dict[str, Point]) -> dict[str, int]:
    """Point each connected library pin toward its peer, preferring no rotation on ties."""
    incident: dict[str, list[tuple[str, str]]] = {node_id: [] for node_id in positions}
    for edge in graph.connections:
        if edge.source_node_id in incident and edge.target_node_id in positions:
            incident[edge.source_node_id].append((edge.source_port_id, edge.target_node_id))
        if edge.target_node_id in incident and edge.source_node_id in positions:
            incident[edge.target_node_id].append((edge.target_port_id, edge.source_node_id))

    choices: dict[str, int] = {}
    for node_id, origin in positions.items():
        value = resolved[node_id]
        variant = next((item for item in value.definition.variants
                        if item.unit == value.unit and item.body_style == value.body_style), None)
        if variant is None:
            choices[node_id] = ORIENTATIONS[0]
            continue
        pins = {pin.number: (int(pin.position.x_nm), int(pin.position.y_nm))
                for pin in variant.pins}
        scores: list[tuple[int, int]] = []
        for orientation in ORIENTATIONS:
            score = 0
            for port_id, peer_id in incident[node_id]:
                port = value.node.port(port_id)
                local = pins.get(port.pin_number) if port else None
                if local is None:
                    continue
                dx, dy = rotate(local, orientation)
                px, py = positions[peer_id]
                toward = (px - origin[0], py - origin[1])
                score += abs(origin[0] + dx - px) + abs(origin[1] + dy - py)
                # A pin pointing into the symbol forces an immediate U-turn.
                if dx * toward[0] + dy * toward[1] < 0:
                    score += 2 * (abs(dx) + abs(dy))
            scores.append((score, orientation))
        choices[node_id] = min(scores)[1]
    return choices


def placed_bounds(bounds: SymbolBounds, origin: Point) -> tuple[int, int, int, int]:
    return (origin[0] + bounds.left, origin[1] + bounds.top,
            origin[0] + bounds.right, origin[1] + bounds.bottom)


def _intersects_body(start: Point, end: Point, box: tuple[int, int, int, int]) -> bool:
    left, top, right, bottom = box
    if start[0] == end[0]:
        return left < start[0] < right and max(start[1], end[1]) > top and min(start[1], end[1]) < bottom
    return top < start[1] < bottom and max(start[0], end[0]) > left and min(start[0], end[0]) < right


def _escape(pin: Point, origin: Point, box: tuple[int, int, int, int], peer: Point) -> Point:
    dx, dy = pin[0] - origin[0], pin[1] - origin[1]
    if dx == dy == 0:
        dx, dy = peer[0] - pin[0], peer[1] - pin[1]
    if abs(dx) >= abs(dy):
        return (box[2] + CLEARANCE if dx >= 0 else box[0] - CLEARANCE, pin[1])
    return (pin[0], box[3] + CLEARANCE if dy >= 0 else box[1] - CLEARANCE)


def _crossing_cost(segment: Segment, occupied: list[Segment]) -> int:
    (a, b) = segment
    cost = 0
    for c, d in occupied:
        if a[1] == b[1] and c[1] == d[1] and a[1] == c[1]:
            if max(min(a[0], b[0]), min(c[0], d[0])) < min(max(a[0], b[0]), max(c[0], d[0])):
                cost += CROSSING_COST
        elif a[0] == b[0] and c[0] == d[0] and a[0] == c[0]:
            if max(min(a[1], b[1]), min(c[1], d[1])) < min(max(a[1], b[1]), max(c[1], d[1])):
                cost += CROSSING_COST
        elif a[1] == b[1] and c[0] == d[0]:
            if min(a[0], b[0]) < c[0] < max(a[0], b[0]) and min(c[1], d[1]) < a[1] < max(c[1], d[1]):
                cost += CROSSING_COST
        elif a[0] == b[0] and c[1] == d[1]:
            if min(a[1], b[1]) < c[1] < max(a[1], b[1]) and min(c[0], d[0]) < a[0] < max(c[0], d[0]):
                cost += CROSSING_COST
    return cost


def _compress(points: list[Point]) -> list[Segment]:
    result: list[Segment] = []
    for start, end in zip(points, points[1:]):
        if start == end:
            continue
        if result and ((result[-1][0][0] == result[-1][1][0] == start[0] == end[0])
                       or (result[-1][0][1] == result[-1][1][1] == start[1] == end[1])):
            result[-1] = (result[-1][0], end)
        else:
            result.append((start, end))
    return result


def route(start: Point, end: Point, source_origin: Point, target_origin: Point,
          source_box: tuple[int, int, int, int], target_box: tuple[int, int, int, int],
          obstacles: list[tuple[int, int, int, int]], occupied: list[Segment]) -> list[Segment] | None:
    """Find a low-crossing Manhattan route outside symbol bodies."""
    if start == end:
        return []
    departure = _escape(start, source_origin, source_box, end)
    arrival = _escape(end, target_origin, target_box, start)
    xs = {departure[0], arrival[0], (departure[0] + arrival[0]) // 2}
    ys = {departure[1], arrival[1], (departure[1] + arrival[1]) // 2}
    for left, top, right, bottom in obstacles:
        xs.update((left - CLEARANCE, right + CLEARANCE))
        ys.update((top - CLEARANCE, bottom + CLEARANCE))
    x_values, y_values = sorted(xs), sorted(ys)
    x_index = {value: index for index, value in enumerate(x_values)}
    y_index = {value: index for index, value in enumerate(y_values)}
    first = (x_index[departure[0]], y_index[departure[1]], 0)
    goal = (x_index[arrival[0]], y_index[arrival[1]])
    queue = [(0, first)]
    distance = {first: 0}
    previous: dict[tuple[int, int, int], tuple[int, int, int]] = {}
    finish = None
    while queue:
        cost, state = heappop(queue)
        if cost != distance[state]:
            continue
        ix, iy, direction = state
        if (ix, iy) == goal:
            finish = state
            break
        point = (x_values[ix], y_values[iy])
        for nx, ny, axis in ((ix - 1, iy, 1), (ix + 1, iy, 1),
                             (ix, iy - 1, 2), (ix, iy + 1, 2)):
            if not (0 <= nx < len(x_values) and 0 <= ny < len(y_values)):
                continue
            next_point = (x_values[nx], y_values[ny])
            if any(_intersects_body(point, next_point, box) for box in obstacles):
                continue
            next_state = (nx, ny, axis)
            step = (abs(next_point[0] - point[0]) + abs(next_point[1] - point[1])
                    + (BEND_COST if direction and direction != axis else 0)
                    + _crossing_cost((point, next_point), occupied))
            next_cost = cost + step
            if next_cost < distance.get(next_state, 1 << 62):
                distance[next_state] = next_cost
                previous[next_state] = state
                heappush(queue, (next_cost, next_state))
    if finish is None:
        return None
    points = []
    while finish != first:
        points.append((x_values[finish[0]], y_values[finish[1]]))
        finish = previous[finish]
    points.append(departure)
    points.reverse()
    return _compress([start, *points, end])
