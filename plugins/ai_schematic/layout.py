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
) -> OriginLayout:
    """Lay out symbols by topology and move their union to the page margin.

    Main-chain and otherwise unassigned nodes extend along +X.  For each main
    anchor, the first branch extends along +Y and the second along -Y.  Fixed
    origin-to-origin steps control density; bounding boxes may overlap and are
    used only to normalize the visible union to the configured page margin.
    """
    available = [node_id for node_id in node_ids if node_id in bounds]
    if not available:
        return OriginLayout({}, 0, 0, origin_x_nm, origin_y_nm)

    allowed = set(available)
    main_nodes: list[str] = []
    for path in graph.main_paths:
        for node_id in path.ordered_nodes:
            if node_id in allowed and node_id not in main_nodes:
                main_nodes.append(node_id)

    main_set = set(main_nodes)
    branches_by_anchor: dict[str, list[list[str]]] = {}
    assigned_branch_nodes: set[str] = set()
    for path in graph.branches:
        anchor = next(
            (node_id for node_id in path.anchor_node_ids if node_id in main_set),
            None,
        )
        if anchor is None or len(branches_by_anchor.get(anchor, [])) >= 2:
            continue
        branch_nodes = [
            node_id
            for node_id in _nodes_from_anchor(path, anchor)
            if node_id in allowed
            and node_id not in main_set
            and node_id not in assigned_branch_nodes
        ]
        if not branch_nodes:
            continue
        branches_by_anchor.setdefault(anchor, []).append(branch_nodes)
        assigned_branch_nodes.update(branch_nodes)

    # Nodes outside the supported main/two-one-level-branches shape remain
    # drawable and are appended to the +X row instead of being discarded.
    row_nodes = list(main_nodes)
    row_nodes.extend(
        node_id
        for node_id in placement_order(graph)
        if node_id in allowed
        and node_id not in row_nodes
        and node_id not in assigned_branch_nodes
    )
    row_nodes.extend(
        node_id
        for node_id in available
        if node_id not in row_nodes and node_id not in assigned_branch_nodes
    )
    if not row_nodes:
        row_nodes = [available[0]]
        assigned_branch_nodes.discard(available[0])

    origins: dict[str, tuple[int, int]] = {}
    placed_boxes: dict[str, tuple[int, int, int, int]] = {}
    cursor_x = 0

    def place(node_id: str, box_left: int, box_top: int) -> None:
        geometry = bounds[node_id]
        origins[node_id] = (
            box_left - geometry.left,
            box_top - geometry.top,
        )
        placed_boxes[node_id] = (
            box_left,
            box_top,
            box_left + geometry.width,
            box_top + geometry.height,
        )

    for row_node in row_nodes:
        branch_paths = branches_by_anchor.get(row_node, [])
        column_center_x = cursor_x

        main_geometry = bounds[row_node]
        main_left = column_center_x - main_geometry.width // 2
        main_top = -(main_geometry.height // 2)
        place(row_node, main_left, main_top)

        if branch_paths:
            for index, node_id in enumerate(branch_paths[0], start=1):
                geometry = bounds[node_id]
                center_y = index * vertical_step_nm
                place(
                    node_id,
                    column_center_x - geometry.width // 2,
                    center_y - geometry.height // 2,
                )

        if len(branch_paths) > 1:
            for index, node_id in enumerate(branch_paths[1], start=1):
                geometry = bounds[node_id]
                center_y = -index * vertical_step_nm
                place(
                    node_id,
                    column_center_x - geometry.width // 2,
                    center_y - geometry.height // 2,
                )

        cursor_x += horizontal_step_nm

    # Defensive fallback for malformed path data: no resolved symbol is lost.
    for node_id in available:
        if node_id in origins:
            continue
        geometry = bounds[node_id]
        place(node_id, cursor_x - geometry.width // 2, -(geometry.height // 2))
        cursor_x += horizontal_step_nm

    min_left = min(value[0] for value in placed_boxes.values())
    min_top = min(value[1] for value in placed_boxes.values())
    max_right = max(value[2] for value in placed_boxes.values())
    max_bottom = max(value[3] for value in placed_boxes.values())
    shift_x = origin_x_nm - min_left
    shift_y = origin_y_nm - min_top
    normalized = {
        node_id: (origin[0] + shift_x, origin[1] + shift_y)
        for node_id, origin in origins.items()
    }
    return OriginLayout(
        positions=normalized,
        width_nm=max_right - min_left,
        height_nm=max_bottom - min_top,
        origin_x_nm=origin_x_nm,
        origin_y_nm=origin_y_nm,
    )


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
