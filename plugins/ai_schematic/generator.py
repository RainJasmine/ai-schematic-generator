"""Resolve graph-ir nodes and create a schematic in one KiCad transaction."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import base64
import os
import uuid
from collections.abc import Callable

from common.commands import editor_commands_pb2
from schematic import schematic_types_pb2

from .api_client import ApiCapabilityError, ApiError, ApiTransportError, KiCadClient
from .graph_ir import GraphIR, NodeIR
from .layout import (
    SCHEMATIC_IU_PER_MM,
    SymbolBounds,
    layout_symbols_in_origin_rectangle,
    placement_order,
    symbol_bounds_from_box,
)
from .routing import choose_orientations, placed_bounds, rotate, rotated_bounds, route
from .symbol_matching import binding_matches, candidate_score, matching_variants
from . import snapshot as snapshots


Progress = Callable[[str], None]

# KiCad's CreateItems handler maps 0 to SYM_ORIENT_0.
SYM_ORIENT_0 = 0
DEFAULT_SYMBOL_SIZE_NM = 10 * SCHEMATIC_IU_PER_MM


@dataclass(slots=True)
class ResolvedSymbol:
    node: NodeIR
    summary: object
    definition: object | None
    unit: int = 1
    body_style: int = 1
    variants: tuple = ()


class GenerationUncertain(ApiError):
    """A mutation may have reached KiCad; the UI must not replay it."""

    uncertain = True

    def __init__(self, message: str) -> None:
        super().__init__(message, code="KICAD_RESULT_UNCERTAIN")


@dataclass(slots=True)
class GenerationReport:
    graph_id: str
    created_nodes: list[str] = field(default_factory=list)
    skipped_nodes: dict[str, str] = field(default_factory=dict)
    created_edges: list[str] = field(default_factory=list)
    skipped_edges: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    node_items: dict[str, list[str]] = field(default_factory=dict)
    edge_items: dict[str, list[str]] = field(default_factory=dict)
    node_references: dict[str, str] = field(default_factory=dict)
    target: dict = field(default_factory=dict)
    snapshot: dict = field(default_factory=dict)
    graph_hash: str = ""
    revision_supported: bool = False
    readback_verified: bool = True
    page_replaced: bool = False
    deleted_items_count: int = 0
    reused: bool = False
    uncertain: bool = False

    def to_dict(self) -> dict:
        return {"format": 2, **asdict(self)}

    @classmethod
    def from_dict(cls, value: dict) -> "GenerationReport":
        return cls(**{key: value[key] for key in cls.__dataclass_fields__ if key in value})

    # Compatibility accessors for dev-0.2 callers. New persistence must save
    # to_dict(), since the old flat format cannot represent multiunit snapshots.
    @property
    def item_ids(self) -> list[str]:
        return sorted(_owned_ids(self.to_dict()))

    @property
    def node_item_ids(self) -> dict[str, str]:
        return {node: items[0] for node, items in self.node_items.items() if items}

    @property
    def edge_item_ids(self) -> dict[str, list[str]]:
        return self.edge_items

    @property
    def document_token(self) -> str:
        return base64.b64decode(self.target.get("document_pb64", "")).hex()

    @property
    def document_fingerprint(self) -> str:
        return snapshots.digest(self.snapshot) if self.snapshot else ""

    @property
    def item_fingerprints(self) -> dict[str, str]:
        return {identity: snapshots.digest(record) for identity, record in self.snapshot.get("owned", {}).items()}

    def format(self) -> str:
        lines = [
            f"图：{self.graph_id}",
            ("已核验现有图，未重复创建" if self.readback_verified else "按本地成功记录防止重复写入，未读取当前图") if self.reused else "导入已完成",
            f"符号节点：{len(self.created_nodes)}",
            f"已创建连接：{len(self.created_edges)}",
            f"跳过符号：{len(self.skipped_nodes)}",
            f"跳过连接：{len(self.skipped_edges)}",
        ]
        if self.skipped_nodes:
            lines.append("\n跳过的符号：")
            lines.extend(f"- {key}: {value}" for key, value in self.skipped_nodes.items())
        if self.skipped_edges:
            lines.append("\n跳过的连接：")
            lines.extend(f"- {key}: {value}" for key, value in self.skipped_edges.items())
        if self.warnings:
            lines.append("\n警告：")
            lines.extend(f"- {value}" for value in self.warnings)
        lines.append("\n已检查显式引脚/导线连接；未进行RF数值验证或完整KiCad ERC。" if self.readback_verified else
                     "\n仅检查创建响应中的引脚/导线；未回读图纸，未进行RF数值验证或完整ERC。")
        return "\n".join(lines)


def _variant(definition: object, unit: int = 1, body_style: int = 1) -> object | None:
    return next(
        (
            value
            for value in definition.variants
            if value.unit == unit and value.body_style == body_style
        ),
        None,
    )


def _resolve_symbol(client: KiCadClient, node: NodeIR) -> ResolvedSymbol | None:
    summaries: dict[tuple[str, str], object] = {}

    def add_summaries(response: object) -> None:
        for summary in response.symbols:
            key = (
                summary.library_id.library_nickname,
                summary.library_id.entry_name,
            )
            if key not in summaries:
                clone = type(summary)()
                clone.CopyFrom(summary)
                summaries[key] = clone

    def best_match() -> ResolvedSymbol | None:
        ranked = sorted(summaries.values(),
                        key=lambda value: candidate_score(node, value), reverse=True)
        matches = []
        for summary in ranked:
            if not binding_matches(node, summary):
                continue
            try:
                definition = client.symbol_definition(summary.library_id)
            except ApiTransportError:
                raise
            except ApiError:
                continue
            variants = tuple(sorted(matching_variants(node, definition),
                                    key=lambda value: (value.unit, value.body_style)))
            if variants:
                variant = variants[0]
                matches.append(ResolvedSymbol(node=node, summary=summary, definition=definition,
                    unit=variant.unit, body_style=variant.body_style, variants=variants))
        if len(matches) > 1:
            raise ApiError(f"{node.node_id} 存在多个同名兼容库符号，请配置明确库映射",
                           code="MODEL_BINDING_AMBIGUOUS")
        return matches[0] if matches else None

    library_hint = node.library_id_hint()
    if library_hint:
        nickname, entry = library_hint
        add_summaries(client.search_symbols(entry, exact=True,
                                            library_nickname=nickname))
        if match := best_match():
            return match
    bound_entry = node.kicad_symbol_name or node.library_name or node.component_name or node.component_id
    if ":" in bound_entry:
        bound_entry = bound_entry.split(":", 1)[1]
    # A bound model is an identity, not a suggestion. Never broaden to a
    # functional-role/type substring and silently import another component.
    for term in (bound_entry,):
        if not term:
            continue
        add_summaries(client.search_symbols(term, exact=True))
        if match := best_match():
            return match
    return None


def _resolved_bounds(resolved: ResolvedSymbol) -> tuple[SymbolBounds, bool]:
    """Return unit geometry and whether a fallback size had to be used."""
    if resolved.definition is not None:
        variant = _variant(resolved.definition, resolved.unit, resolved.body_style)
        candidates = [
            variant.bounding_box if variant is not None else None,
            resolved.definition.bounding_box,
        ]
        for candidate in candidates:
            if candidate is None:
                continue
            geometry = symbol_bounds_from_box(candidate)
            if geometry.width > 0 and geometry.height > 0:
                return geometry, False
    half = DEFAULT_SYMBOL_SIZE_NM // 2
    return SymbolBounds(-half, -half, DEFAULT_SYMBOL_SIZE_NM, DEFAULT_SYMBOL_SIZE_NM), True


def _make_symbol(resolved: ResolvedSymbol, position: tuple[int, int], orientation: int = SYM_ORIENT_0,
                 reference: str = "") -> object:
    symbol = schematic_types_pb2.Symbol()
    symbol.id.value = str(uuid.uuid4())
    symbol.position.x_nm, symbol.position.y_nm = position
    symbol.library_id.CopyFrom(resolved.summary.library_id)
    symbol.schematic_library_name = (
        f"{resolved.summary.library_id.library_nickname}:"
        f"{resolved.summary.library_id.entry_name}"
    )
    symbol.unit = resolved.unit
    symbol.body_style = resolved.body_style
    symbol.orientation = orientation
    reference_prefix = resolved.summary.reference_prefix.rstrip("?") or "U"
    symbol.reference_prefix = reference_prefix
    if reference:
        field = symbol.fields.add()
        field.name = "Reference"
        field.field_id = 0
        field.value = reference

    # KiCad creates all library fields from LIB_SYMBOL. Only send explicit
    # overrides; the library definition's field coordinates are local and
    # must not be copied over the placed instance coordinates.
    return symbol


def _make_line(start: tuple[int, int], end: tuple[int, int]) -> object:
    line = schematic_types_pb2.Line()
    line.id.value = str(uuid.uuid4())
    line.start.x_nm, line.start.y_nm = start
    line.end.x_nm, line.end.y_nm = end
    line.layer = schematic_types_pb2.SL_WIRE
    return line



def _document(target: dict):
    from common.types import base_types_pb2
    document = base_types_pb2.DocumentSpecifier()
    try:
        document.ParseFromString(base64.b64decode(target["document_pb64"], validate=True))
    except Exception as exc:
        raise ApiError("目标图纸标识损坏，请重新选择图纸", code="KICAD_TARGET_INVALID") from exc
    return document


def _context_record(context) -> dict:
    unit = str(context.coordinate_unit).strip().casefold()
    # ActiveSchematicContext 的定制宿主使用 schematic_internal_unit 声明
    # 原理图内部单位。它与 Eeschema IU 是同一配置：10,000 IU/mm。
    # KiCad include/base_units.h 的 SCH_IU_PER_MM=1e4；这里不根据 *_nm
    # 字段名推导单位，也不接受真正的 nm 或未知声明。
    known_iu = {"iu", "eeschema_iu", "eeschema-iu", "eeschema_internal_units",
                "schematic_internal_unit", "10000_iu_per_mm"}
    calibration = os.environ.get("RF_EDA_SCHEMATIC_UNITS_PER_MM", "").strip()
    # This adapter targets the checked-out Eeschema-IU protocol, whose *_nm
    # names do not describe SI nanometres. Never infer a scale from field names.
    # A different or absent host declaration requires an explicit calibration;
    # even then this adapter only implements the verified 10,000-IU/mm profile.
    if calibration and calibration != str(SCHEMATIC_IU_PER_MM):
        raise ApiCapabilityError("当前适配器仅支持10000 IU/mm；校准值与适配器不一致，请使用匹配的宿主/适配器。",
                                 code="KICAD_COORDINATE_UNIT_UNVERIFIED")
    if unit not in known_iu and not calibration:
        raise ApiCapabilityError(
            f"宿主坐标单位 {context.coordinate_unit!r} 尚未核验。适配器要求10000 Eeschema IU/mm；"
            "请先核对定制宿主，再显式设置RF_EDA_SCHEMATIC_UNITS_PER_MM=10000，不能按字段名猜测尺度。",
            code="KICAD_COORDINATE_UNIT_UNVERIFIED")
    encoded = base64.b64encode(context.document.SerializeToString(deterministic=True)).decode("ascii")
    screen = context.screen_filename
    return {"key": snapshots.digest({"document": encoded, "screen": os.path.normcase(screen)}),
            "document_pb64": encoded, "screen_filename": screen,
            "coordinate_unit": context.coordinate_unit,
            "units_per_mm": SCHEMATIC_IU_PER_MM,
            "coordinate_policy": "eeschema_iu_10000",
            "grid": [int(context.grid.x_nm), int(context.grid.y_nm)], "readback": True}


def _canvas_items(client, document) -> list:
    try:
        return snapshots.electrical_items(client.get_items(document).items)
    except ApiCapabilityError as exc:
        raise ApiCapabilityError("宿主不支持完整图纸对象读取（GetItems）；已阻止修改原理图，请核对匹配的 KiCad 构建。",
                                 code="KICAD_READBACK_UNSUPPORTED") from exc
    except ValueError as exc:
        raise ApiCapabilityError(str(exc), code="KICAD_READBACK_UNSUPPORTED") from exc


def capture_target(*, readback: bool = True) -> dict:
    """定位目标；readback=False 只读取身份/坐标，不请求图纸对象。"""
    with KiCadClient() as client:
        context = client.active_context()
        target = _context_record(context)
        if readback:
            _canvas_items(client, context.document)
        target["readback"] = readback
        return target


def _verify_target(client, expected: dict):
    context = client.active_context()
    actual = _context_record(context)
    if expected.get("key") != actual["key"]:
        raise ApiError("活动工程或图纸已切换，请回到提交时的目标图纸再导入。", code="KICAD_TARGET_CHANGED")
    return context


def _owned_ids(previous: dict) -> set[str]:
    return {item_id for name in ("node_items", "edge_items")
            for item_ids in previous.get(name, {}).values() for item_id in item_ids}


def _inventory(items: list) -> str:
    records = sorted(snapshots.encode_items(items), key=lambda record: (record["type"], record["protobuf"]))
    return snapshots.digest(records)


def _verify_owned(previous: dict, current: list) -> None:
    owned = _owned_ids(previous)
    if not owned or not previous.get("snapshot"):
        raise ApiCapabilityError("当前成功图缺少完整的对象映射或图纸快照，不能安全反馈/替换。",
                                 code="KICAD_SNAPSHOT_MISSING")
    try:
        dirty = snapshots.changed(previous["snapshot"], current, owned)
    except ValueError as exc:
        raise ApiError(str(exc), code="KICAD_SNAPSHOT_INVALID") from exc
    if dirty:
        raise ApiError("原理图含未同步的结构、参数或连接修改，请保留本地工作；不能覆盖。",
                       code="CLIENT_GRAPH_UNSYNCED")


def prepare_feedback(previous: dict) -> dict:
    """Read real selection/aliases and compare the owned drawing with its snapshot."""
    with KiCadClient() as client:
        context = _verify_target(client, previous.get("target", {}))
        current = _canvas_items(client, context.document)
        dirty = False
        try:
            _verify_owned(previous, current)
        except ApiError as exc:
            if exc.code != "CLIENT_GRAPH_UNSYNCED":
                raise
            dirty = True
        reverse_nodes = {item: node for node, items in previous.get("node_items", {}).items() for item in items}
        reverse_edges = {item: edge for edge, items in previous.get("edge_items", {}).items() for item in items}
        warnings = []
        try:
            selected = {snapshots.item_id(item) for item in client.selection(context.document).items}
        except ApiCapabilityError:
            selected = set()
            warnings.append("宿主未提供选择读取，本轮按整图反馈处理。")
        aliases = {}
        for item in current:
            if isinstance(item, schematic_types_pb2.Symbol) and item.id.value in reverse_nodes:
                label = snapshots.reference(item)
                if label:
                    aliases[reverse_nodes[item.id.value]] = label
        return {"client_graph_dirty": dirty,
                "selection": {"node_ids": sorted({reverse_nodes[item] for item in selected if item in reverse_nodes}),
                              "edge_ids": sorted({reverse_edges[item] for item in selected if item in reverse_edges})},
                "display_aliases": aliases, "target": _context_record(context), "warnings": warnings}


def verify_managed_placement(placement: dict) -> dict[str, list[str]]:
    """Compatibility wrapper; never turn an unverified legacy record into clean."""
    result = prepare_feedback(placement)
    if result["client_graph_dirty"]:
        raise ApiError("原理图含未同步编辑，请保留本地工作。", code="CLIENT_GRAPH_UNSYNCED")
    return result["selection"]


def select_graph_objects(previous: dict, node_ids: list[str] | None = None,
                         edge_ids: list[str] | None = None) -> dict:
    """Highlight actual owned IDs only; selecting is not a schematic edit."""
    node_ids, edge_ids = node_ids or [], edge_ids or []
    if set(node_ids) - set(previous.get("node_items", {})) or set(edge_ids) - set(previous.get("edge_items", {})):
        raise ApiError("候选引用不属于当前成功图。", code="SELECTION_NOT_FOUND")
    with KiCadClient() as client:
        context = _verify_target(client, previous.get("target", {}))
        _verify_owned(previous, _canvas_items(client, context.document))
        item_ids = sorted({identity for node in node_ids for identity in previous["node_items"][node]}
                          | {identity for edge in edge_ids for identity in previous["edge_items"][edge]})
        client.clear_selection(context.document)
        if item_ids:
            client.add_to_selection(context.document, item_ids)
        return {"selected": len(item_ids)}


def clear_graph_selection(previous: dict) -> dict:
    return select_graph_objects(previous)


def adopt_legacy_placement(placement: dict, graph: GraphIR | dict, *, target: dict | None = None) -> GenerationReport:
    """Explicitly recover a dev-0.2 import, without regenerating/replacing it.

    Raw historical byte hashes cannot prove a modified drawing clean. Recovery
    therefore checks every mapped object against the supplied successful graph,
    actual library defaults and physical net partition. Unknown fields/models,
    missing objects or unmanaged attachments fail closed.
    """
    if not isinstance(graph, GraphIR):
        graph = GraphIR.parse(graph)
    if graph.errors or any(not edge.valid for edge in graph.edges):
        raise ApiError("成功图含阻断错误，不能恢复旧图快照。", code="GRAPH_INVALID")
    node_map = placement.get("node_item_ids") or {}
    edge_map = placement.get("edge_item_ids") or {}
    if set(node_map) != {node.node_id for node in graph.nodes} or set(edge_map) != {edge.edge_id for edge in graph.edges}:
        raise ApiError("旧版对象映射与服务成功图不完整一致，不能安全恢复。", code="KICAD_LEGACY_MAPPING_INVALID")
    mapped_ids = list(node_map.values()) + [identity for values in edge_map.values() for identity in values]
    if (any(not isinstance(identity, str) or not identity for identity in mapped_ids)
            or len(mapped_ids) != len(set(mapped_ids)) or set(mapped_ids) != set(placement.get("item_ids") or [])):
        raise ApiError("旧版对象UUID缺失或重复，不能安全恢复。", code="KICAD_LEGACY_MAPPING_INVALID")
    with KiCadClient() as client:
        context = client.active_context() if target is None else _verify_target(client, target)
        if context.document.SerializeToString().hex() != placement.get("document_token"):
            raise ApiError("当前图纸与旧版会话不一致。", code="KICAD_TARGET_CHANGED")
        current = _canvas_items(client, context.document)
        current_map = {item.id.value: item for item in current}
        if set(mapped_ids) - set(current_map):
            raise ApiError("旧图部分对象已不存在，请保留工作副本。", code="KICAD_LEGACY_MAPPING_INVALID")
        node_symbols = {}
        used_references = set()
        foreign_references = {snapshots.reference(item) for item in current
                              if item.id.value not in mapped_ids and isinstance(item, schematic_types_pb2.Symbol)}
        for node in graph.nodes:
            item = current_map[node_map[node.node_id]]
            if not isinstance(item, schematic_types_pb2.Symbol):
                raise ApiError("节点映射不是实际符号。", code="KICAD_LEGACY_MAPPING_INVALID")
            if not binding_matches(node, item):
                raise ApiError(f"{node.node_id} 的实际型号与服务图不符。", code="MODEL_BINDING_MISMATCH")
            definition = client.symbol_definition(item.library_id)
            variants = matching_variants(node, definition)
            if len(variants) != 1 or (variants[0].unit, variants[0].body_style) != (item.unit, item.body_style):
                raise ApiError(f"{node.node_id} 的单元/端口不能完整核验。", code="MODEL_BINDING_MISMATCH")
            # The old generator sent no parameter-field overrides. Preserve
            # aliases/layout, but do not adopt unknown edited parameter fields.
            expected_fields = {field.name: field.value for field in definition.default_fields
                               if field.name.casefold() != "reference"}
            actual_fields = {field.name: field.value for field in item.fields
                             if field.name.casefold() != "reference" and not (not field.name and field.field_id == 0)}
            if actual_fields != expected_fields:
                raise ApiError(f"{node.node_id} 的参数字段不等于库默认值，请保留本地工作后人工核对。",
                               code="CLIENT_GRAPH_UNSYNCED")
            ref = snapshots.reference(item)
            if not ref or ref in used_references or ref in foreign_references:
                raise ApiError("旧图位号缺失或重复，无法恢复。", code="KICAD_REFERENCE_CONFLICT")
            used_references.add(ref)
            node_symbols[node.node_id] = [item]
        for edge, identities in edge_map.items():
            if not identities or any(not isinstance(current_map[identity], schematic_types_pb2.Line)
                                     or current_map[identity].layer != schematic_types_pb2.SL_WIRE for identity in identities):
                raise ApiError(f"{edge} 的导线映射无效。", code="KICAD_LEGACY_MAPPING_INVALID")
        try:
            snapshots.validate_graph_connections(graph, node_symbols, current)
            snapshots.require_isolated_ownership(current, set(mapped_ids))
        except ValueError as exc:
            raise ApiError(str(exc), code="KICAD_CONNECTION_MISMATCH") from exc
        report = GenerationReport(graph.graph_id, created_nodes=[node.node_id for node in graph.nodes],
            created_edges=[edge.edge_id for edge in graph.edges], node_items={node: [identity] for node, identity in node_map.items()},
            edge_items={edge: list(identities) for edge, identities in edge_map.items()},
            node_references={node: snapshots.reference(items[0]) for node, items in node_symbols.items()},
            target=_context_record(context), snapshot=snapshots.make_snapshot(current, set(mapped_ids)),
            graph_hash=snapshots.digest(graph.source), revision_supported=True, reused=True,
            warnings=["已只读核验并恢复旧图快照；未重跑AI、未改图。库条目/端口已核验，不含库文件摘要或完整ERC验证。"])
        return report


def verify_uncertain(checkpoint: dict, previous: dict | None = None) -> dict:
    """Clear uncertainty only after proving the candidate objects are absent.

    The user first reviews/undoes the partial import in KiCad. Merely finding
    candidate objects is never interpreted as proof of a committed operation.
    """
    initial_only = checkpoint.get("operation") == "initial_create_no_readback" and not previous and not checkpoint.get("previous_ids")
    if not checkpoint.get("planned_ids") or not checkpoint.get("target") or (not initial_only and not checkpoint.get("before_inventory")):
        raise GenerationUncertain("缺少完整的绘图日志，不能自动清除不确定状态，请先人工核对图纸。")
    with KiCadClient() as client:
        context = _verify_target(client, checkpoint.get("target", {}))
        current = _canvas_items(client, context.document)
        ids = {item.id.value for item in current}
        if ids.intersection(checkpoint.get("planned_ids", [])):
            raise GenerationUncertain("仍检测到上次操作的对象，请先在KiCad核验并撤销该次操作，再重新核验。")
        if previous:
            _verify_owned(previous, current)
        elif set(checkpoint.get("previous_ids", [])) - ids:
            raise GenerationUncertain("旧图对象尚未完全恢复，请先在KiCad恢复原图。")
        if not initial_only and _inventory(current) != checkpoint["before_inventory"]:
            raise GenerationUncertain("图纸尚未恢复到操作前的完整状态，请先核对旧图及非托管对象。")
        return {"verified": True, "message": ("已核验首轮计划对象不存在；未核验其他图纸内容，可重新导入原结果。" if initial_only else "已核验候选对象不存在，旧图完整，可重新导入。"),
                "state": "verified_rolled_back", "uncertain": False,
                "target": _context_record(context)}


def _unit_resolved(value: ResolvedSymbol, variant) -> ResolvedSymbol:
    return ResolvedSymbol(value.node, value.summary, value.definition, variant.unit, variant.body_style, (variant,))


def _instance_geometry(value: ResolvedSymbol) -> tuple[list[ResolvedSymbol], list[tuple[int, int]], SymbolBounds]:
    units = [_unit_resolved(value, variant) for variant in value.variants]
    geometries = [_resolved_bounds(unit)[0] for unit in units]
    step = max(box.height for box in geometries) + 5 * SCHEMATIC_IU_PER_MM
    offsets = [(0, index * step) for index in range(len(units))]
    left = min(box.left + offset[0] for box, offset in zip(geometries, offsets))
    top = min(box.top + offset[1] for box, offset in zip(geometries, offsets))
    right = max(box.right + offset[0] for box, offset in zip(geometries, offsets))
    bottom = max(box.bottom + offset[1] for box, offset in zip(geometries, offsets))
    return units, offsets, SymbolBounds(left, top, right-left, bottom-top)


def _item_box(item) -> tuple[int, int, int, int] | None:
    if isinstance(item, schematic_types_pb2.Symbol):
        box = item.bounding_box
        if box.size.x_nm <= 0 or box.size.y_nm <= 0:
            raise ApiError("已有符号缺少可用包围盒，无法安全避障；请在独立空白图纸导入。",
                           code="KICAD_GEOMETRY_MISSING")
        return (int(box.position.x_nm), int(box.position.y_nm),
                int(box.position.x_nm + box.size.x_nm), int(box.position.y_nm + box.size.y_nm))
    if isinstance(item, schematic_types_pb2.Line):
        return (min(item.start.x_nm, item.end.x_nm), min(item.start.y_nm, item.end.y_nm),
                max(item.start.x_nm, item.end.x_nm), max(item.start.y_nm, item.end.y_nm))
    if isinstance(item, schematic_types_pb2.Junction):
        return (item.position.x_nm, item.position.y_nm, item.position.x_nm, item.position.y_nm)
    return None


def _overlap(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _edge_nets(graph: GraphIR) -> dict[str, str]:
    parent = {}
    def endpoint(node_id, port_id):
        port = graph.node_map[node_id].port(port_id)
        return (node_id, port.unit or 0, port.pin_number or port_id)
    def find(value):
        parent.setdefault(value, value)
        if parent[value] != value:
            parent[value] = find(parent[value])
        return parent[value]
    for edge in graph.connections:
        a = find(endpoint(edge.source_node_id, edge.source_port_id))
        b = find(endpoint(edge.target_node_id, edge.target_port_id))
        if a != b:
            parent[max(a, b)] = min(a, b)
    return {edge.edge_id: repr(find(endpoint(edge.source_node_id, edge.source_port_id)))
            for edge in graph.connections}


def _check_created(response, requested: list, cls) -> list:
    KiCadClient.require_success(response)
    if len(response.created_items) != len(requested):
        raise ApiError("KiCad 返回的对象创建结果数量不匹配", code="KICAD_CREATE_FAILED")
    result = []
    for original, created in zip(requested, response.created_items):
        item = cls()
        if created.status.code != editor_commands_pb2.ISC_OK or not created.item.Unpack(item):
            raise ApiError(created.status.error_message or "KiCad 拒绝部分对象，取消整次导入",
                           code="KICAD_CREATE_FAILED")
        if item.id.value != original.id.value:
            raise ApiError("KiCad 返回的对象UUID与请求不一致，取消整次导入", code="KICAD_CREATE_FAILED")
        if isinstance(item, schematic_types_pb2.Symbol):
            if (item.library_id != original.library_id or item.unit != original.unit
                    or item.body_style != original.body_style or item.position != original.position
                    or item.orientation != original.orientation):
                raise ApiError("KiCad返回的符号型号/位置与请求不一致，取消整次导入。",
                               code="KICAD_CREATE_FAILED")
        result.append(item)
    return result


def generate(graph: GraphIR, progress: Progress, replace: dict | None = None, *,
             require_complete: bool = True, target: dict | None = None,
             previous: dict | None = None, on_checkpoint: Callable[[dict], None] | None = None,
             replace_all: bool = False) -> GenerationReport:
    """Import a graph; replace_all=True clears and redraws the entire target page.

    The caller must persist the checkpoint before mutation. If the process dies
    after it, leave the session uncertain and require explicit host verification;
    never turn a network/IPC retry into another CreateItems call.
    """
    if graph.errors or any(not edge.valid for edge in graph.edges):
        raise ApiError("Graph IR含阻断错误，未修改图纸：" + "; ".join(graph.errors)[:2000],
                       code="GRAPH_INVALID")
    if not graph.nodes:
        raise ApiError("Graph IR没有可导入节点，未修改图纸", code="GRAPH_EMPTY")
    if previous is not None and replace is not None:
        raise ApiError("不能同时提供 previous 和 replace", code="KICAD_BASE_INVALID")
    previous = previous if previous is not None else replace or {}
    if previous.get("uncertain"):
        raise GenerationUncertain("上次图纸操作结果尚未确认，禁止重复创建。")
    readback = not replace_all and (target is None or target.get("readback", True))
    if replace_all:
        # 用户明确点击更新：忽略旧布局、快照和 UUID，只按新 Graph IR 重画当前页。
        previous = {}
    if previous and not readback:
        if previous.get("target", {}).get("key") != target.get("key"):
            raise ApiError("旧图属于另一张图纸，不能复用或替换。", code="KICAD_TARGET_CHANGED")
        if previous.get("graph_hash") == snapshots.digest(graph.source) and not previous.get("uncertain"):
            # 本地成功收据仅用于防重，不能宣称当前画布仍与之相同。
            with KiCadClient() as client:
                _verify_target(client, target)
            reused = GenerationReport.from_dict(previous)
            reused.reused = True
            progress("本地记录显示此版本已提交到 KiCad，未重复写入；未读取当前图纸。")
            return reused
        raise ApiCapabilityError(
            "整页更新需要先保存当前图纸的完整快照，并由宿主事务替换旧图。接口尚未接入，原图未删除；新版 Graph IR 已保留。",
            code="KICAD_FULL_REPLACE_UNAVAILABLE")
    if previous and not previous.get("snapshot"):
        raise ApiCapabilityError("旧版图纸需先执行“恢复旧版会话”核验，不能直接替换。",
                                 code="KICAD_LEGACY_SNAPSHOT_REQUIRED")
    if previous.get("uncertain"):
        raise GenerationUncertain("上次图纸操作结果尚未核验，禁止重复创建。")
    graph_hash = snapshots.digest(graph.source)
    report = GenerationReport(graph.graph_id, warnings=list(graph.warnings), graph_hash=graph_hash)
    with KiCadClient() as client:
        progress("正在定位目标图纸…" if not readback else "核对目标图纸及对象读取能力…")
        context = client.active_context() if target is None else _verify_target(client, target)
        report.target = _context_record(context)
        target = report.target
        target["readback"] = readback
        report.readback_verified = readback
        current = _canvas_items(client, context.document) if readback else []
        if previous:
            if previous.get("target", {}).get("key") != target["key"]:
                raise ApiError("成功图属于另一工程/图纸，不能替换当前图纸。", code="KICAD_TARGET_CHANGED")
            _verify_owned(previous, current)
            if previous.get("graph_hash") == graph_hash:
                reused = GenerationReport.from_dict(previous)
                reused.reused = True
                progress("该成功版本已导入且快照核验通过，未重复创建。")
                return reused
        owned = _owned_ids(previous)
        foreign = [item for item in current if item.id.value not in owned]
        resolved = {}
        for index, node in enumerate(graph.nodes, 1):
            progress(f"[{index}/{len(graph.nodes)}] 精确匹配符号：{node.node_id}")
            value = _resolve_symbol(client, node)
            if value is None:
                raise ApiError(f"{node.node_id} 缺少精确型号及引脚兼容的符号；未修改图纸。",
                               code="MODEL_BINDING_MISSING")
            resolved[node.node_id] = value
        node_ids = list(placement_order(graph))
        instances = {node: _instance_geometry(resolved[node]) for node in node_ids}
        bounds = {node: instances[node][2] for node in node_ids}
        grid = tuple(max(0, int(value)) for value in target.get("grid", [0, 0]))
        layout = layout_symbols_in_origin_rectangle(graph, node_ids, bounds, grid_nm=grid)
        orientations = choose_orientations(graph, resolved, layout.positions)
        oriented = {node: rotated_bounds(bounds[node], orientations[node]) for node in node_ids}
        layout = layout_symbols_in_origin_rectangle(graph, node_ids, oriented, grid_nm=grid)
        positions = dict(layout.positions)
        current_map = {item.id.value: item for item in current}
        foreign_boxes = [_item_box(item) for item in foreign]
        foreign_boxes = [box for box in foreign_boxes if box is not None]
        # Fresh imports are placed in a new clear region, instead of reusing a
        # fixed page origin and overwriting an existing drawing.
        if foreign_boxes and not previous:
            right = max(box[2] for box in foreign_boxes)
            left = min(placed_bounds(oriented[node], positions[node])[0] for node in node_ids)
            dx = max(0, right + 30 * SCHEMATIC_IU_PER_MM - left)
            if grid[0] > 0:
                dx = ((dx + grid[0] - 1) // grid[0]) * grid[0]
            positions = {node: (point[0] + dx, point[1]) for node, point in positions.items()}
        # Keep stable-node placement. New nodes go to a separate clear column;
        # resizing an existing model must still pass the collision check below.
        preserved = set()
        prior_units = {}
        for node in node_ids:
            prior = [current_map[item] for item in previous.get("node_items", {}).get(node, []) if item in current_map]
            if prior:
                prior_units[node] = {(item.unit, item.body_style): item for item in prior}
                primary = min(prior, key=lambda item: (item.unit, item.body_style))
                positions[node] = (int(primary.position.x_nm), int(primary.position.y_nm))
                orientations[node] = primary.orientation or 3
                oriented[node] = rotated_bounds(bounds[node], orientations[node])
                preserved.add(node)
        if previous:
            right = max([box[2] for box in foreign_boxes] +
                        [placed_bounds(oriented[node], positions[node])[2] for node in preserved] + [0])
            for node in node_ids:
                if node not in preserved:
                    box = placed_bounds(oriented[node], positions[node])
                    positions[node] = (positions[node][0] + right + 30 * SCHEMATIC_IU_PER_MM - box[0], positions[node][1])
                    right = placed_bounds(oriented[node], positions[node])[2]
        refs = {snapshots.reference(item) for item in foreign if isinstance(item, schematic_types_pb2.Symbol)}
        requested, instance_nodes, planned_boxes = [], [], []
        for node in node_ids:
            prior = [current_map[item] for item in previous.get("node_items", {}).get(node, []) if item in current_map]
            ref = snapshots.reference(prior[0]) if prior else ""
            prefix = resolved[node].summary.reference_prefix.rstrip("?") or "U"
            if not ref:
                number = 1
                while f"{prefix}{number}" in refs:
                    number += 1
                ref = f"{prefix}{number}"
            if ref in refs:
                raise ApiError("器件位号与其他对象重复，请先整理位号。", code="KICAD_REFERENCE_CONFLICT")
            refs.add(ref)
            report.node_references[node] = ref
            units, offsets, _ = instances[node]
            for unit, offset in zip(units, offsets):
                ox, oy = rotate(offset, orientations[node])
                point = (positions[node][0]+ox, positions[node][1]+oy)
                orientation = orientations[node]
                prior_unit = prior_units.get(node, {}).get((unit.unit, unit.body_style))
                if prior_unit is not None:
                    point = (int(prior_unit.position.x_nm), int(prior_unit.position.y_nm))
                    orientation = prior_unit.orientation or 3
                requested.append(_make_symbol(unit, point, orientation, ref))
                instance_nodes.append(node)
                planned_boxes.append(placed_bounds(rotated_bounds(_resolved_bounds(unit)[0], orientation), point))
        for index, box in enumerate(planned_boxes):
            if any(_overlap(box, other) for other in planned_boxes[index+1:]):
                raise ApiError("保留的布局与新型号尺寸冲突，请调整布局后再导入。", code="KICAD_LAYOUT_COLLISION")
            if any(_overlap(box, other) for other in foreign_boxes):
                raise ApiError("目标位置存在非本插件对象，请保留本地工作并选择独立图纸。", code="KICAD_LAYOUT_COLLISION")
        before_inventory = _inventory(current) if readback else None
        # All checks that can safely happen before the mutation journal do so
        # here. A stale drawing must not acquire a misleading mutation marker.
        _verify_target(client, target)
        fresh = _canvas_items(client, context.document) if readback else []
        if previous:
            _verify_owned(previous, fresh)
        if readback and _inventory(fresh) != before_inventory:
            raise ApiError("图纸在导入准备期间发生修改，请保留当前图并重新导入已有服务结果。",
                           code="KICAD_CANVAS_CHANGED")
        checkpoint = {"state": "mutating", "uncertain": True, "graph_hash": graph_hash,
                      "target": target, "previous_ids": sorted(owned),
                      "before_inventory": before_inventory,
                      "operation": "full_page_replace" if replace_all else "verified_replace" if readback else "initial_create_no_readback",
                      "planned_ids": [item.id.value for item in requested]}
        if on_checkpoint:
            try:
                on_checkpoint(checkpoint)  # persist failure aborts BEFORE mutation
            except Exception as exc:
                error = ApiError("无法持久保存绘图日志，未开始修改原理图。", code="KICAD_JOURNAL_SAVE_FAILED")
                error.rollback_verified = True  # no BeginCommit was sent
                raise error from exc
        commit_id = None
        commit_attempted = False
        try:
            commit_id = client.begin_commit()
            if not commit_id:
                raise GenerationUncertain("KiCad未返回有效事务标识，事务结果待核验。")
            # Beginning a commit is not an exclusive editor lock. Check the
            # complete canvas again immediately before any object mutation.
            _verify_target(client, target)
            latest = _canvas_items(client, context.document) if readback else []
            if previous:
                _verify_owned(previous, latest)
            if readback and _inventory(latest) != before_inventory:
                raise ApiError("图纸在事务开始后发生修改，旧图已保留。", code="KICAD_CANVAS_CHANGED")
            if replace_all:
                progress("正在事务内清空当前图页…")
                report.deleted_items_count = client.clear_schematic_page(context.document)
                report.page_replaced = True
            if owned:
                client.delete_items(context.document, sorted(owned))
            progress(f"完整创建 {len(requested)} 个符号单元…")
            created = _check_created(client.create_items(context.document, requested), requested, schematic_types_pb2.Symbol)
            node_symbols = {node: [] for node in node_ids}
            for node, symbol in zip(instance_nodes, created):
                if snapshots.reference(symbol) != report.node_references[node]:
                    raise ApiError("宿主未保留请求位号，无法安全关联多单元器件。", code="KICAD_REFERENCE_MISMATCH")
                node_symbols[node].append(symbol)
                report.node_items.setdefault(node, []).append(symbol.id.value)
            requested_lines, line_edges = [], []
            occupied, occupied_nets = [], []
            forbidden_points = []
            for item in foreign:
                if isinstance(item, schematic_types_pb2.Line) and item.layer == schematic_types_pb2.SL_WIRE:
                    occupied.append(((int(item.start.x_nm), int(item.start.y_nm)),
                                     (int(item.end.x_nm), int(item.end.y_nm))))
                    occupied_nets.append(f"foreign:{item.id.value}")
                elif isinstance(item, schematic_types_pb2.Symbol):
                    forbidden_points.extend((int(pin.position.x_nm), int(pin.position.y_nm)) for pin in item.pins)
                elif isinstance(item, schematic_types_pb2.Junction):
                    forbidden_points.append((int(item.position.x_nm), int(item.position.y_nm)))
            nets = _edge_nets(graph)
            net_pins = {}
            for edge in graph.connections:
                for node, port in ((edge.source_node_id, edge.source_port_id),
                                   (edge.target_node_id, edge.target_port_id)):
                    physical = snapshots.physical_pin(node_symbols[node], graph.node_map[node].port(port))
                    if physical is not None:
                        net_pins.setdefault(nets[edge.edge_id], set()).add(snapshots.pin_token(*physical))
            for edge in graph.connections:
                source = snapshots.physical_pin(node_symbols[edge.source_node_id], graph.node_map[edge.source_node_id].port(edge.source_port_id))
                dest = snapshots.physical_pin(node_symbols[edge.target_node_id], graph.node_map[edge.target_node_id].port(edge.target_port_id))
                if source is None or dest is None:
                    raise ApiError(f"{edge.edge_id} 没有唯一的物理引脚，取消整次导入。", code="KICAD_PIN_MISSING")
                source_symbol, source_pin = source
                dest_symbol, dest_pin = dest
                start = (int(source_pin.position.x_nm), int(source_pin.position.y_nm))
                end = (int(dest_pin.position.x_nm), int(dest_pin.position.y_nm))
                segments = route(start, end,
                    (source_symbol.position.x_nm, source_symbol.position.y_nm),
                    (dest_symbol.position.x_nm, dest_symbol.position.y_nm),
                    _item_box(source_symbol), _item_box(dest_symbol),
                    [_item_box(item) for item in created] + foreign_boxes, occupied,
                    net_id=nets[edge.edge_id], occupied_nets=occupied_nets, grid_nm=grid,
                    forbidden_points={*forbidden_points,
                        *((int(pin.position.x_nm), int(pin.position.y_nm))
                          for symbol in created for pin in symbol.pins
                          if snapshots.pin_token(symbol, pin) not in net_pins.get(nets[edge.edge_id], set()))})
                if not segments:
                    raise ApiError(f"{edge.edge_id} 无法形成安全走线，取消整次导入。", code="KICAD_ROUTE_FAILED")
                for start_point, end_point in segments:
                    requested_lines.append(_make_line(start_point, end_point))
                    line_edges.append(edge.edge_id)
                occupied.extend(segments)
                occupied_nets.extend([nets[edge.edge_id]] * len(segments))
            created_lines = []
            if requested_lines:
                if on_checkpoint:
                    on_checkpoint({**checkpoint,
                        "planned_ids": [item.id.value for item in [*requested, *requested_lines]]})
                progress(f"创建并核对响应中的 {len(requested_lines)} 段导线…")
                created_lines = _check_created(client.create_items(context.document, requested_lines), requested_lines, schematic_types_pb2.Line)
            for edge, line in zip(line_edges, created_lines):
                report.edge_items.setdefault(edge, []).append(line.id.value)
            # Re-read instead of trusting CreateItems status: endpoint geometry
            # must still match Graph IR and no unexpected pin nets may appear.
            actual = _canvas_items(client, context.document) if readback else [*created, *created_lines]
            actual_map = {item.id.value: item for item in actual}
            managed_ids = _owned_ids(report.to_dict())
            actual_foreign = [item for item in actual if item.id.value not in managed_ids]
            if _inventory(actual_foreign) != _inventory(foreign):
                raise ApiError("非托管图纸对象在绘图期间发生变化，取消本次导入并核验撤销结果。",
                               code="KICAD_CANVAS_CHANGED")
            actual_foreign_boxes = [_item_box(item) for item in actual_foreign]
            actual_foreign_boxes = [box for box in actual_foreign_boxes if box is not None]
            actual_managed_boxes = [_item_box(item) for item in actual
                                    if item.id.value in managed_ids and isinstance(item, schematic_types_pb2.Symbol)]
            if (any(_overlap(box, other) for index, box in enumerate(actual_managed_boxes)
                    for other in actual_managed_boxes[index+1:])
                    or any(_overlap(box, other) for box in actual_managed_boxes for other in actual_foreign_boxes)):
                raise ApiError("回读图纸发现符号重叠，取消本次导入。", code="KICAD_LAYOUT_COLLISION")
            reread_symbols = {node: [actual_map[item] for item in ids if item in actual_map]
                              for node, ids in report.node_items.items()}
            snapshots.validate_graph_connections(graph, reread_symbols, actual)
            new_owned = _owned_ids(report.to_dict())
            snapshots.require_isolated_ownership(actual, new_owned)
            # 无回读模式只验证创建响应中的几何连接，不把响应伪装成图纸快照。
            if readback:
                report.snapshot = snapshots.make_snapshot(actual, new_owned)
            _verify_target(client, target)
            commit_attempted = True
            client.end_commit(commit_id, commit=True, message=f"RF EDA import {graph.graph_id}")
        except Exception as exc:
            if commit_attempted:
                raise GenerationUncertain("KiCad事务提交未获确认；请核验当前图纸，禁止直接重复导入。") from exc
            if commit_id is None:
                raise GenerationUncertain("KiCad事务开启未获确认；请核验当前图纸后再操作。") from exc
            try:
                if isinstance(exc, ApiTransportError):
                    client.reconnect_for_verification()
                client.end_commit(commit_id, commit=False)
                restored = _canvas_items(client, context.document) if readback else []
                if readback and _inventory(restored) != before_inventory:
                    raise ApiError("撤销后图纸未恢复至操作前状态", code="KICAD_ROLLBACK_MISMATCH")
            except Exception as rollback_error:
                raise GenerationUncertain("图纸导入未完成，且无法确认撤销结果。请在KiCad核验/撤销后再操作；不要重复生成。") from rollback_error
            if isinstance(exc, ApiError):
                exc.rollback_verified = readback
                exc.rollback_confirmed = not readback
                raise
            error = ApiError(str(exc), code="KICAD_IMPORT_FAILED")
            error.rollback_verified = readback
            error.rollback_confirmed = not readback
            raise error from exc
        report.created_nodes = list(node_ids)
        report.created_edges = [edge.edge_id for edge in graph.connections]
        report.revision_supported = readback
        if not readback:
            report.warnings.append("未进行画布快照或后端对齐；仅核对创建响应中的型号与连接。" if replace_all else "首轮未读取原图内容；仅核对创建响应，不保证避开已有对象。建议使用独立 AI 图页。")
        if previous:
            report.warnings.append("已保留稳定节点各单元的坐标和位号；替换后的KiCad对象UUID及映射已更新。")
        if readback:
            report.warnings.append("已核验显式引脚和导线的几何网络；不涵盖全局标签/隐藏电源或完整ERC。")
        progress("图已创建，UUID 映射已保存；未读取图纸内容。" if not readback else "完整图已导入，图纸快照和对象映射已保存。")
        return report
