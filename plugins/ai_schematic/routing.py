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
        return (max(pin[0], box[2]) + CLEARANCE if dx >= 0
                else min(pin[0], box[0]) - CLEARANCE, pin[1])
    return (pin[0], max(pin[1], box[3]) + CLEARANCE if dy >= 0
            else min(pin[1], box[1]) - CLEARANCE)


def _point_on_segment(point: Point, segment: Segment) -> bool:
    a, b = segment
    return ((b[0] - a[0]) * (point[1] - a[1]) == (b[1] - a[1]) * (point[0] - a[0])
            and min(a[0], b[0]) <= point[0] <= max(a[0], b[0])
            and min(a[1], b[1]) <= point[1] <= max(a[1], b[1]))


def _segments_intersect(first: Segment, second: Segment) -> bool:
    """Inclusive contact test: T junctions and single shared endpoints count."""
    a, b = first
    c, d = second

    def cross(p: Point, q: Point, r: Point) -> int:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    ab_c, ab_d, cd_a, cd_b = cross(a, b, c), cross(a, b, d), cross(c, d, a), cross(c, d, b)
    if ((ab_c > 0 > ab_d or ab_d > 0 > ab_c)
            and (cd_a > 0 > cd_b or cd_b > 0 > cd_a)):
        return True
    return ((ab_c == 0 and _point_on_segment(c, first))
            or (ab_d == 0 and _point_on_segment(d, first))
            or (cd_a == 0 and _point_on_segment(a, second))
            or (cd_b == 0 and _point_on_segment(b, second)))


def _crossing_cost(segment: Segment, occupied: list[Segment]) -> int:
    return CROSSING_COST * sum(_segments_intersect(segment, other) for other in occupied)


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
          obstacles: list[tuple[int, int, int, int]], occupied: list[Segment], *,
          net_id: str = "", occupied_nets: list[str] | None = None,
          forbidden_points: set[Point] | None = None,
          grid_nm: int | tuple[int, int] = 0,
          max_search_states: int = 200_000) -> list[Segment] | None:
    """Route an orthogonal connection with hard electrical contact protection.

    Every intersection with an unrelated/unknown net is forbidden, including T
    contacts and endpoint touches.  Contact or shared wire is permitted only
    when the caller explicitly supplies the same nonempty electrical net ID.
    Source/target escape legs undergo the same checks as the searched path.
    Library pin angles are not exposed by this protocol; the escape direction
    therefore remains a geometric heuristic and never bypasses an obstacle.
    """
    if occupied_nets is not None and len(occupied_nets) != len(occupied):
        raise ValueError("occupied_nets must align with occupied segments")
    if type(max_search_states) is not int or max_search_states < 1:
        raise ValueError("max_search_states must be a positive integer")
    if isinstance(grid_nm, tuple):
        grid_x, grid_y = (max(0, int(value)) for value in grid_nm)
    else:
        grid_x = grid_y = max(0, int(grid_nm))

    def floor_grid(value: int, grid: int) -> int:
        return value // grid * grid if grid else value

    def ceil_grid(value: int, grid: int) -> int:
        return -((-value) // grid) * grid if grid else value

    def escaped(pin: Point, origin: Point, box, peer: Point) -> Point:
        point = _escape(pin, origin, box, peer)
        if point[0] != pin[0]:
            return (ceil_grid(point[0], grid_x) if point[0] > pin[0]
                    else floor_grid(point[0], grid_x), pin[1])
        return (pin[0], ceil_grid(point[1], grid_y) if point[1] > pin[1]
                else floor_grid(point[1], grid_y))
    other_wires = [segment for index, segment in enumerate(occupied)
                   if not (net_id and occupied_nets is not None and occupied_nets[index] == net_id)]
    blocked_points = set(forbidden_points or ()) - {start, end}

    def wire_blocked(segment: Segment) -> bool:
        return (any(_segments_intersect(segment, other) for other in other_wires)
                or any(_point_on_segment(point, segment) for point in blocked_points))

    if start == end:
        return [] if not wire_blocked((start, end)) else None
    departure = escaped(start, source_origin, source_box, end)
    arrival = escaped(end, target_origin, target_box, start)
    source_leg, target_leg = (start, departure), (arrival, end)
    if (any(_intersects_body(*source_leg, box) for box in obstacles if box != source_box)
            or any(_intersects_body(*target_leg, box) for box in obstacles if box != target_box)
            or wire_blocked(source_leg) or wire_blocked(target_leg)):
        return None
    xs = {departure[0], arrival[0], floor_grid((departure[0] + arrival[0]) // 2, grid_x)}
    ys = {departure[1], arrival[1], floor_grid((departure[1] + arrival[1]) // 2, grid_y)}
    for left, top, right, bottom in obstacles:
        xs.update((floor_grid(left - CLEARANCE, grid_x), ceil_grid(right + CLEARANCE, grid_x)))
        ys.update((floor_grid(top - CLEARANCE, grid_y), ceil_grid(bottom + CLEARANCE, grid_y)))
    for a, b in other_wires:
        for point in (a, b):
            xs.update((floor_grid(point[0] - CLEARANCE, grid_x), point[0], ceil_grid(point[0] + CLEARANCE, grid_x)))
            ys.update((floor_grid(point[1] - CLEARANCE, grid_y), point[1], ceil_grid(point[1] + CLEARANCE, grid_y)))
    for x, y in blocked_points:
        xs.update((floor_grid(x - CLEARANCE, grid_x), ceil_grid(x + CLEARANCE, grid_x)))
        ys.update((floor_grid(y - CLEARANCE, grid_y), ceil_grid(y + CLEARANCE, grid_y)))
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
            segment = (point, next_point)
            if (any(_intersects_body(point, next_point, box) for box in obstacles)
                    or wire_blocked(segment)):
                continue
            next_state = (nx, ny, axis)
            step = (abs(next_point[0] - point[0]) + abs(next_point[1] - point[1])
                    + (BEND_COST if direction and direction != axis else 0))
            next_cost = cost + step
            if next_cost < distance.get(next_state, 1 << 62):
                if next_state not in distance and len(distance) >= max_search_states:
                    return None  # Bounded planning failure, never partial output.
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
    segments = _compress([start, *points, end])
    # Compression must never turn a safe searched path into an unsafe result.
    for index, segment in enumerate(segments):
        exempt = set()
        if index == 0:
            exempt.add(source_box)
        if index == len(segments) - 1:
            exempt.add(target_box)
        if (wire_blocked(segment)
                or any(_intersects_body(*segment, box) for box in obstacles if box not in exempt)):
            return None
    return segments
