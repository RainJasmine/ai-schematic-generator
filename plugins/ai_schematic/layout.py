"""Deterministic main-chain and anchored-branch placement for graph-ir nodes."""

from __future__ import annotations

from dataclasses import dataclass

from .graph_ir import GraphIR, PathIR

# This KiCad checkout carries Eeschema internal units in the ``*_nm`` API
# fields. Eeschema uses 10,000 IU/mm.
SCHEMATIC_IU_PER_MM = 10_000
DEFAULT_GRID_NM = 25_400
HORIZONTAL_SPACING_NM = 350_000
VERTICAL_SPACING_NM = 250_000
SEQUENTIAL_MAX_COLUMNS = 5
SYMBOL_HORIZONTAL_STEP_NM = 30 * SCHEMATIC_IU_PER_MM
SYMBOL_VERTICAL_STEP_NM = 30 * SCHEMATIC_IU_PER_MM
LAYOUT_ORIGIN_OFFSET_NM = 30 * SCHEMATIC_IU_PER_MM


@dataclass(frozen=True, slots=True)
class SymbolBounds:
    """A library symbol bounding box relative to the symbol origin."""

    left: int
    top: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height


@dataclass(frozen=True, slots=True)
class OriginLayout:
    """Placed symbol origins and their visible bounding rectangle."""

    positions: dict[str, tuple[int, int]]
    width_nm: int
    height_nm: int
    origin_x_nm: int = 0
    origin_y_nm: int = 0


def symbol_bounds_from_box(box: object) -> SymbolBounds:
    """Convert an API ``Box2`` into immutable layout geometry."""
    return SymbolBounds(
        left=int(box.position.x_nm),
        top=int(box.position.y_nm),
        width=max(0, int(box.size.x_nm)),
        height=max(0, int(box.size.y_nm)),
    )


def _snap(value: int, grid: int) -> int:
    if grid <= 0:
        return value
    return round(value / grid) * grid


def _branch_anchor_index(graph: GraphIR, path: PathIR) -> int:
    main_order = {
        node_id: index
        for main in graph.main_paths
        for index, node_id in enumerate(main.ordered_nodes)
    }
    indexes = [main_order[node_id] for node_id in path.anchor_node_ids if node_id in main_order]
    return min(indexes) if indexes else 1 << 30


def ordered_paths(graph: GraphIR) -> tuple[PathIR, ...]:
    """Main paths first, then anchored branches, then stable path-id order."""
    mains = sorted(graph.main_paths, key=lambda path: path.path_id)
    branches = sorted(
        graph.branches,
        key=lambda path: (
            not bool(path.anchor_node_ids),
            _branch_anchor_index(graph, path),
            path.path_id,
        ),
    )
    return tuple(mains + branches)


def placement_order(graph: GraphIR) -> tuple[str, ...]:
    """Return every drawable node once in deterministic placement-chain order."""
    result: list[str] = []
    known = graph.node_map
    for path in ordered_paths(graph):
        for node_id in path.ordered_nodes:
            if node_id in known and node_id not in result:
                result.append(node_id)
    result.extend(node.node_id for node in graph.nodes if node.node_id not in result)
    return tuple(result)


def _nodes_from_anchor(path: PathIR, anchor: str) -> list[str]:
    """Return branch nodes in order from the main-chain anchor outwards."""
    ordered = list(path.ordered_nodes)
    if anchor not in ordered:
        return ordered

    anchor_index = ordered.index(anchor)
    before = list(reversed(ordered[:anchor_index]))
    after = ordered[anchor_index + 1 :]
    if before and after:
        # A one-level branch should terminate at its anchor. Preserve every node
        # deterministically if an input path places the anchor in the middle.
        return after + before
    return after or before


def layout_symbols_in_origin_rectangle(
    graph: GraphIR,
    node_ids: list[str],
    bounds: dict[str, SymbolBounds],
    horizontal_step_nm: int = SYMBOL_HORIZONTAL_STEP_NM,
    vertical_step_nm: int = SYMBOL_VERTICAL_STEP_NM,
    origin_x_nm: int = LAYOUT_ORIGIN_OFFSET_NM,
    origin_y_nm: int = LAYOUT_ORIGIN_OFFSET_NM,
    *,
    grid_nm: int | tuple[int, int] = 0,
    page_bounds: tuple[int, int, int, int] | None = None,
    clearance_nm: int = 2 * SCHEMATIC_IU_PER_MM,
) -> OriginLayout:
    """Lay out all topology paths without symbol-body collisions.

    Steps are minimum center spacing, enlarged for actual body dimensions.
    Branches keep their own lanes, including third and nested branches; shared
    nodes are placed once.  Optional grid/page values use the same units as the
    captured host context.  A viewport is not a paper boundary.  If explicit
    paper bounds cannot contain the result, fail instead of clipping objects.
    """
    available = list(dict.fromkeys(node_id for node_id in node_ids if node_id in bounds))
    if not available:
        return OriginLayout({}, 0, 0, origin_x_nm, origin_y_nm)
    allowed = set(available)
    if isinstance(grid_nm, tuple):
        grid_x, grid_y = (max(0, int(value)) for value in grid_nm)
    else:
        grid_x = grid_y = max(0, int(grid_nm))
    step_x = max(1, int(horizontal_step_nm))
    step_y = max(1, int(vertical_step_nm))
    gap = max(1, int(clearance_nm))
    origins: dict[str, tuple[int, int]] = {}
    boxes: dict[str, tuple[int, int, int, int]] = {}

    def shift_on_grid(delta: int, grid: int) -> int:
        return -((-delta) // grid) * grid if grid else delta

    def collides(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
        return a[0] < b[2] + gap and a[2] + gap > b[0] and a[1] < b[3] + gap and a[3] + gap > b[1]

    def place(node_id: str, center_x: int, center_y: int, axis: int = 0, sign: int = 1) -> None:
        geometry = bounds[node_id]
        x = _snap(center_x - geometry.left - geometry.width // 2, grid_x)
        y = _snap(center_y - geometry.top - geometry.height // 2, grid_y)
        # Each iteration moves past at least one occupied box in a monotonic
        # direction; this also makes asymmetric and large symbols safe.
        while True:
            box = (x + geometry.left, y + geometry.top,
                   x + geometry.right, y + geometry.bottom)
            conflicts = [other for other in boxes.values() if collides(box, other)]
            if not conflicts:
                break
            if axis == 0:
                delta = (max(other[2] + gap - box[0] for other in conflicts)
                         if sign > 0 else max(box[2] + gap - other[0] for other in conflicts))
                x += sign * shift_on_grid(max(1, delta), grid_x)
            else:
                delta = (max(other[3] + gap - box[1] for other in conflicts)
                         if sign > 0 else max(box[3] + gap - other[1] for other in conflicts))
                y += sign * shift_on_grid(max(1, delta), grid_y)
        origins[node_id] = (x, y)
        boxes[node_id] = box

    def center(node_id: str) -> tuple[int, int]:
        box = boxes[node_id]
        return ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)

    def place_row(ordered: list[str], row_y: int) -> None:
        previous: str | None = None
        for node_id in ordered:
            if node_id not in allowed:
                continue
            if node_id in origins:
                previous = node_id
                continue
            desired_x = center(previous)[0] + step_x if previous else 0
            place(node_id, desired_x, row_y)
            previous = node_id

    for path in graph.main_paths:
        new_nodes = [node_id for node_id in path.ordered_nodes if node_id in allowed and node_id not in origins]
        if not new_nodes:
            continue
        y = 0 if not boxes else max(box[3] for box in boxes.values()) + step_y + max(bounds[n].height for n in new_nodes) // 2
        place_row(list(path.ordered_nodes), y)

    branch_counts: dict[str, int] = {}
    pending = list(graph.branches)
    while pending:
        next_pending = []
        progressed = False
        for path in pending:
            unplaced = [node_id for node_id in path.ordered_nodes if node_id in allowed and node_id not in origins]
            if not unplaced:
                continue
            anchor = next((node_id for node_id in path.anchor_node_ids if node_id in origins), None)
            if anchor is None:
                anchor = next((node_id for node_id in path.ordered_nodes if node_id in origins), None)
            if anchor is None:
                next_pending.append(path)
                continue
            count = branch_counts.get(anchor, 0)
            branch_counts[anchor] = count + 1
            sign = 1 if count % 2 == 0 else -1
            ax, ay = center(anchor)
            # First siblings retain the familiar down/up placement; additional
            # siblings get a separate column rather than joining the main row.
            bx = ax + (count // 2) * step_x
            previous = anchor
            for node_id in _nodes_from_anchor(path, anchor):
                if node_id not in allowed or node_id in origins:
                    continue
                py = center(previous)[1]
                desired_y = py + sign * step_y
                place(node_id, bx, desired_y, axis=1, sign=sign)
                previous = node_id
            progressed = True
        if not next_pending:
            break
        if not progressed:
            # Disconnected paths get a separate row.  After one such path is
            # placed, dependent nested paths can resolve their shared anchor.
            path = next_pending.pop(0)
            nodes = [n for n in path.ordered_nodes if n in allowed and n not in origins]
            if nodes:
                y = max((box[3] for box in boxes.values()), default=0) + step_y + max(bounds[n].height for n in nodes) // 2
                place_row(nodes, y)
        pending = next_pending

    remaining = [node_id for node_id in placement_order(graph) if node_id in allowed and node_id not in origins]
    remaining.extend(n for n in available if n not in origins and n not in remaining)
    if remaining:
        y = max((box[3] for box in boxes.values()), default=-step_y) + step_y + max(bounds[n].height for n in remaining) // 2
        place_row(remaining, y)

    min_left = min(box[0] for box in boxes.values())
    min_top = min(box[1] for box in boxes.values())
    # Translate by whole grid multiples, preserving each pin origin's grid.
    shift_x = shift_on_grid(origin_x_nm - min_left, grid_x)
    shift_y = shift_on_grid(origin_y_nm - min_top, grid_y)
    normalized = {n: (x + shift_x, y + shift_y) for n, (x, y) in origins.items()}
    left, top = min_left + shift_x, min_top + shift_y
    right = max(box[2] for box in boxes.values()) + shift_x
    bottom = max(box[3] for box in boxes.values()) + shift_y
    if page_bounds is not None:
        pl, pt, pr, pb = page_bounds
        if left < pl or top < pt or right > pr or bottom > pb:
            raise ValueError("LAYOUT_PAGE_OVERFLOW: symbol layout exceeds the supplied paper bounds")
    return OriginLayout(normalized, right - left, bottom - top, left, top)



def layout_symbols_sequentially(
    node_ids: list[str], context: object
) -> dict[str, tuple[int, int]]:
    """Place symbols in a compact, viewport-centered grid in input order."""
    if not node_ids:
        return {}

    viewport = context.visible_canvas
    grid_x = int(context.grid.x_nm) or DEFAULT_GRID_NM
    grid_y = int(context.grid.y_nm) or DEFAULT_GRID_NM
    center_x = int(viewport.position.x_nm + viewport.size.x_nm // 2)
    center_y = int(viewport.position.y_nm + viewport.size.y_nm // 2)
    if viewport.size.x_nm == 0 or viewport.size.y_nm == 0:
        center_x = int(context.cursor.x_nm) if context.HasField("cursor") else 0
        center_y = int(context.cursor.y_nm) if context.HasField("cursor") else 0

    viewport_columns = (
        int(viewport.size.x_nm) // HORIZONTAL_SPACING_NM
        if viewport.size.x_nm > 0
        else SEQUENTIAL_MAX_COLUMNS
    )
    columns = max(1, min(SEQUENTIAL_MAX_COLUMNS, len(node_ids), viewport_columns))
    rows = (len(node_ids) + columns - 1) // columns
    start_y = center_y - (rows - 1) * VERTICAL_SPACING_NM // 2
    positions: dict[str, tuple[int, int]] = {}

    for index, node_id in enumerate(node_ids):
        row = index // columns
        column = index % columns
        row_count = min(columns, len(node_ids) - row * columns)
        start_x = center_x - (row_count - 1) * HORIZONTAL_SPACING_NM // 2
        positions[node_id] = (
            _snap(start_x + column * HORIZONTAL_SPACING_NM, grid_x),
            _snap(start_y + row * VERTICAL_SPACING_NM, grid_y),
        )

    return positions


def layout_nodes(
    graph: GraphIR,
    context: object,
    drawable_node_ids: set[str] | None = None,
) -> dict[str, tuple[int, int]]:
    """Place drawable nodes left-to-right and branches relative to their main anchor."""
    viewport = context.visible_canvas
    grid_x = int(context.grid.x_nm) or DEFAULT_GRID_NM
    grid_y = int(context.grid.y_nm) or DEFAULT_GRID_NM
    center_x = int(viewport.position.x_nm + viewport.size.x_nm // 2)
    center_y = int(viewport.position.y_nm + viewport.size.y_nm // 2)
    if viewport.size.x_nm == 0 or viewport.size.y_nm == 0:
        center_x = int(context.cursor.x_nm) if context.HasField("cursor") else 0
        center_y = int(context.cursor.y_nm) if context.HasField("cursor") else 0

    positions: dict[str, tuple[int, int]] = {}
    node_map = graph.node_map
    drawable = (
        set(node_map)
        if drawable_node_ids is None
        else set(drawable_node_ids).intersection(node_map)
    )
    lane = 0

    # Main-chain placement owns shared nodes and therefore runs first.
    for path in sorted(graph.main_paths, key=lambda value: value.path_id):
        ordered = [node_id for node_id in path.ordered_nodes if node_id in drawable]
        if not ordered:
            continue
        width = (len(ordered) - 1) * HORIZONTAL_SPACING_NM
        start_x = center_x - width // 2
        y = center_y + lane * VERTICAL_SPACING_NM
        for column, node_id in enumerate(ordered):
            positions.setdefault(
                node_id,
                (_snap(start_x + column * HORIZONTAL_SPACING_NM, grid_x), _snap(y, grid_y)),
            )
        lane += 1

    branches = sorted(
        graph.branches,
        key=lambda path: (
            not bool(path.anchor_node_ids),
            _branch_anchor_index(graph, path),
            path.path_id,
        ),
    )
    for path in branches:
        ordered = [node_id for node_id in path.ordered_nodes if node_id in drawable]
        unplaced = [node_id for node_id in ordered if node_id not in positions]
        if not unplaced:
            continue
        branch_y = center_y + lane * VERTICAL_SPACING_NM
        anchor = next((node_id for node_id in path.anchor_node_ids if node_id in positions), None)
        if anchor and anchor in ordered:
            anchor_column = ordered.index(anchor)
            anchor_x = positions[anchor][0]
            for column, node_id in enumerate(ordered):
                if node_id not in positions:
                    positions[node_id] = (
                        _snap(anchor_x + (column - anchor_column) * HORIZONTAL_SPACING_NM, grid_x),
                        _snap(branch_y, grid_y),
                    )
        else:
            width = (len(unplaced) - 1) * HORIZONTAL_SPACING_NM
            start_x = center_x - width // 2
            for column, node_id in enumerate(unplaced):
                positions[node_id] = (
                    _snap(start_x + column * HORIZONTAL_SPACING_NM, grid_x),
                    _snap(branch_y, grid_y),
                )
        lane += 1

    # Connected nodes omitted from paths and true orphans are never discarded.
    remaining = [
        node_id
        for node_id in placement_order(graph)
        if node_id in drawable and node_id not in positions
    ]
    columns = max(1, min(6, len(remaining)))
    for index, node_id in enumerate(remaining):
        column = index % columns
        extra_row = lane + index // columns
        width = (columns - 1) * HORIZONTAL_SPACING_NM
        positions[node_id] = (
            _snap(center_x - width // 2 + column * HORIZONTAL_SPACING_NM, grid_x),
            _snap(center_y + extra_row * VERTICAL_SPACING_NM, grid_y),
        )
    return positions
