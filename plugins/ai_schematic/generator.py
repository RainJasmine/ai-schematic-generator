"""Resolve graph-ir nodes and create a schematic in one KiCad transaction."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import uuid
from collections.abc import Callable

from common.commands import editor_commands_pb2
from schematic import schematic_types_pb2

from .api_client import ApiError, ApiTransportError, KiCadClient
from .graph_ir import EdgeIR, GraphIR, NodeIR
from .layout import (
    SCHEMATIC_IU_PER_MM,
    SymbolBounds,
    layout_symbols_in_origin_rectangle,
    placement_order,
    symbol_bounds_from_box,
)
from .routing import choose_orientations, placed_bounds, rotated_bounds, route
from .symbol_matching import candidate_score, matching_variant


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


@dataclass(slots=True)
class GenerationReport:
    graph_id: str
    created_nodes: list[str] = field(default_factory=list)
    skipped_nodes: dict[str, str] = field(default_factory=dict)
    created_edges: list[str] = field(default_factory=list)
    skipped_edges: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    item_ids: list[str] = field(default_factory=list)
    item_fingerprints: dict[str, str] = field(default_factory=dict)
    document_token: str = ""
    document_fingerprint: str = ""
    node_item_ids: dict[str, str] = field(default_factory=dict)
    edge_item_ids: dict[str, list[str]] = field(default_factory=dict)

    def format(self) -> str:
        lines = [
            f"图：{self.graph_id}",
            f"已创建符号：{len(self.created_nodes)}",
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
        for summary in ranked:
            try:
                definition = client.symbol_definition(summary.library_id)
            except ApiError:
                continue
            variant = matching_variant(node, definition)
            if variant is not None:
                return ResolvedSymbol(node=node, summary=summary, definition=definition,
                                      unit=variant.unit, body_style=variant.body_style)
        return None

    library_hint = node.library_id_hint()
    if library_hint:
        nickname, entry = library_hint
        add_summaries(client.search_symbols(entry, exact=True,
                                            library_nickname=nickname))
        if match := best_match():
            return match
    for term in node.search_terms():
        add_summaries(client.search_symbols(term, exact=True))
        if match := best_match():
            return match
    for term in node.search_terms():
        add_summaries(client.search_symbols(term, exact=False))
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


def _make_symbol(resolved: ResolvedSymbol, position: tuple[int, int], orientation: int = SYM_ORIENT_0) -> object:
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

    # KiCad creates all library fields from LIB_SYMBOL. Only send explicit
    # overrides; the library definition's field coordinates are local and
    # must not be copied over the placed instance coordinates.
    return symbol


def _pin_position(symbol: object, node: NodeIR, port_id: str) -> tuple[int, int] | None:
    port = node.port(port_id)
    if port is None:
        return None
    pin = next((value for value in symbol.pins if value.number == port.pin_number), None)
    if pin is None:
        return None
    return int(pin.position.x_nm), int(pin.position.y_nm)


def _make_line(start: tuple[int, int], end: tuple[int, int]) -> object:
    line = schematic_types_pb2.Line()
    line.id.value = str(uuid.uuid4())
    line.start.x_nm, line.start.y_nm = start
    line.end.x_nm, line.end.y_nm = end
    line.layer = schematic_types_pb2.SL_WIRE
    return line


def _fingerprints(client: KiCadClient, document: object,
                  item_ids: list[str]) -> dict[str, str]:
    response = client.get_items_by_id(document, item_ids)
    found: dict[str, str] = {}
    for packed in response.items:
        for item_type in (schematic_types_pb2.Symbol, schematic_types_pb2.Line):
            item = item_type()
            if packed.Unpack(item) and item.id.value:
                found[item.id.value] = hashlib.sha256(packed.SerializeToString()).hexdigest()
                break
    if set(found) != set(item_ids):
        raise ApiError("原理图中有插件创建的对象缺失，不能安全覆盖")
    return found


def verify_managed_placement(placement: dict) -> dict[str, list[str]]:
    """Fail closed if the current schematic differs from the saved AI baseline."""
    with KiCadClient() as client:
        context = client.active_context()
        if context.document.SerializeToString().hex() != placement.get("document_token"):
            raise ApiError("当前图纸与已发布 AI 图纸不同")
        expected_document = placement.get("document_fingerprint")
        if not expected_document:
            raise ApiError("没有完整图纸快照，不能确认本地修改状态")
        actual_document = hashlib.sha256(
            client.document_snapshot(context.document).encode("utf-8")
        ).hexdigest()
        if actual_document != expected_document:
            raise ApiError("当前原理图已发生本地更改；请保存工作副本后处理")
        ids = placement.get("item_ids") or []
        if not ids or _fingerprints(client, context.document, ids) != placement.get("item_fingerprints"):
            raise ApiError("插件生成的符号或导线已更改，不能提交反馈")
        selected = client.get_selection(context.document)
        node_by_item = {value: key for key, value in (placement.get("node_item_ids") or {}).items()}
        edge_by_item = {item_id: edge_id
                        for edge_id, item_ids in (placement.get("edge_item_ids") or {}).items()
                        for item_id in item_ids}
        nodes: set[str] = set()
        edges: set[str] = set()
        for packed in selected.items:
            for item_type in (schematic_types_pb2.Symbol, schematic_types_pb2.Line):
                item = item_type()
                if packed.Unpack(item):
                    if item.id.value in node_by_item:
                        nodes.add(node_by_item[item.id.value])
                    if item.id.value in edge_by_item:
                        edges.add(edge_by_item[item.id.value])
                    break
        return {"node_ids": sorted(nodes)[:50], "edge_ids": sorted(edges)[:50]}


def generate(graph: GraphIR, progress: Progress,
             replace: dict | None = None, *,
             require_complete: bool = False) -> GenerationReport:
    report = GenerationReport(
        graph_id=graph.graph_id,
        warnings=[*(f"解析错误：{value}" for value in graph.errors), *graph.warnings],
    )
    for edge in graph.edges:
        if not edge.valid:
            report.skipped_edges[edge.edge_id] = ", ".join(edge.issues) or "连接无效"

    with KiCadClient() as client:
        progress("读取当前活动原理图和画布信息…")
        context = client.active_context()
        strict = replace is not None or require_complete
        report.document_token = context.document.SerializeToString().hex()
        if replace is not None:
            if replace.get("document_token") != report.document_token:
                raise ApiError("当前原理图与上次生成的图纸不同，不能覆盖旧图")
            old_ids = replace.get("item_ids") or []
            old_fingerprints = replace.get("item_fingerprints") or {}
            if not old_ids or not old_fingerprints:
                raise ApiError("旧图缺少对象校验记录，不能安全提交反馈图")
            if _fingerprints(client, context.document, old_ids) != old_fingerprints:
                raise ApiError("插件创建的原理图对象已被修改，请先保存工作副本")
            if hashlib.sha256(client.document_snapshot(context.document).encode("utf-8")).hexdigest() != replace.get("document_fingerprint"):
                raise ApiError("本地图纸在反馈任务运行期间发生修改，旧图已保留")
        if strict and (graph.errors or len(graph.connections) != len(graph.edges)):
            raise ApiError("Graph IR 存在无效连接，原理图未修改")
        resolved: dict[str, ResolvedSymbol] = {}

        for index, node in enumerate(graph.nodes, start=1):
            progress(f"[{index}/{len(graph.nodes)}] 搜索符号：{node.node_id}")
            try:
                value = _resolve_symbol(client, node)
            except ApiTransportError:
                raise
            except ApiError as exc:
                report.skipped_nodes[node.node_id] = str(exc)
                continue
            if value is None:
                report.skipped_nodes[node.node_id] = (
                    "符号库中没有匹配且引脚兼容的符号"
                )
                continue
            resolved[node.node_id] = value
            progress(
                f"  → {value.summary.library_id.library_nickname}:"
                f"{value.summary.library_id.entry_name}"
            )

        if not resolved:
            if strict:
                raise ApiError("没有找到可创建的符号，原理图未修改")
            progress("没有找到任何可创建的符号，未修改原理图")
            return report
        if strict and len(resolved) != len(graph.nodes):
            raise ApiError("图中有符号未能匹配，原理图未修改")

        pin_uses: dict[tuple[str, str], set[str]] = {}
        for edge in graph.connections:
            for node_id, port_id in ((edge.source_node_id, edge.source_port_id),
                                     (edge.target_node_id, edge.target_port_id)):
                node = graph.node_map.get(node_id)
                port = node.port(port_id) if node else None
                if port and port.pin_number and node_id in resolved:
                    pin_uses.setdefault((node_id, port.pin_number), set()).add(edge.edge_id)
        for (node_id, pin_number), edge_ids in sorted(pin_uses.items()):
            if len(edge_ids) > 1:
                report.warnings.append(
                    f"{node_id} 引脚 {pin_number} 被多条连接共用：{', '.join(sorted(edge_ids))}"
                )

        node_order = [graph.node_map[node_id] for node_id in placement_order(graph)
                      if node_id in resolved]
        symbol_bounds: dict[str, SymbolBounds] = {}
        for node in node_order:
            geometry, used_fallback = _resolved_bounds(resolved[node.node_id])
            symbol_bounds[node.node_id] = geometry
            if used_fallback:
                report.warnings.append(f"{node.node_id}: 符号包围盒为空，按 10 mm × 10 mm 参与布局")
        node_ids = [node.node_id for node in node_order]
        layout = layout_symbols_in_origin_rectangle(graph, node_ids, symbol_bounds)
        # Choose rotations from actual library pin geometry and connected peers.
        # Re-layout with rotated bounds so the page margin remains at 30 mm.
        orientations = choose_orientations(graph, resolved, layout.positions)
        for _ in range(2):
            oriented_bounds = {node_id: rotated_bounds(symbol_bounds[node_id], orientations[node_id])
                               for node_id in node_ids}
            layout = layout_symbols_in_origin_rectangle(graph, node_ids, oriented_bounds)
            updated = choose_orientations(graph, resolved, layout.positions)
            if updated == orientations:
                break
            orientations = updated
        oriented_bounds = {node_id: rotated_bounds(symbol_bounds[node_id], orientations[node_id])
                           for node_id in node_ids}
        layout = layout_symbols_in_origin_rectangle(graph, node_ids, oriented_bounds)
        positions = layout.positions
        progress(
            f"布局区域：{layout.width_nm / SCHEMATIC_IU_PER_MM:.3f} mm × "
            f"{layout.height_nm / SCHEMATIC_IU_PER_MM:.3f} mm，左上角："
            f"({layout.origin_x_nm / SCHEMATIC_IU_PER_MM:.3f} mm, "
            f"{layout.origin_y_nm / SCHEMATIC_IU_PER_MM:.3f} mm)"
        )

        commit_id = client.begin_commit()
        committed = False
        try:
            requested_symbols = [
                _make_symbol(resolved[node.node_id], positions[node.node_id],
                             orientations[node.node_id])
                for node in node_order
            ]
            progress(f"创建 {len(requested_symbols)} 个符号…")
            create_response = client.create_items(context.document, requested_symbols)
            created_symbols: dict[str, object] = {}

            for node, result in zip(node_order, create_response.created_items):
                if result.status.code != editor_commands_pb2.ISC_OK:
                    report.skipped_nodes[node.node_id] = (
                        result.status.error_message or "KiCad 拒绝创建该符号")
                    continue
                created = schematic_types_pb2.Symbol()
                if not result.item.Unpack(created):
                    report.skipped_nodes[node.node_id] = "创建响应不是原理图 Symbol"
                    continue
                created_symbols[node.node_id] = created
                report.created_nodes.append(node.node_id)
                report.item_ids.append(created.id.value)
                report.node_item_ids[node.node_id] = created.id.value

            if len(create_response.created_items) != len(requested_symbols):
                raise ApiError("KiCad 返回的符号创建结果数量不匹配")

            if not created_symbols:
                for edge in graph.connections:
                    report.skipped_edges[edge.edge_id] = "端点符号缺失或创建失败"

            if created_symbols:
                requested_lines: list[object] = []
                line_edges: list[str] = []
                node_map = graph.node_map
                boxes = {node_id: placed_bounds(oriented_bounds[node_id], positions[node_id])
                         for node_id in created_symbols}
                occupied = []
                for edge in graph.connections:
                    source_symbol = created_symbols.get(edge.source_node_id)
                    target_symbol = created_symbols.get(edge.target_node_id)
                    if source_symbol is None or target_symbol is None:
                        report.skipped_edges[edge.edge_id] = "端点符号缺失或创建失败"
                        continue

                    start = _pin_position(
                        source_symbol, node_map[edge.source_node_id], edge.source_port_id
                    )
                    end = _pin_position(
                        target_symbol, node_map[edge.target_node_id], edge.target_port_id
                    )
                    if start is None or end is None:
                        report.skipped_edges[edge.edge_id] = "端口没有对应的已创建引脚"
                        continue

                    segments = route(start, end, positions[edge.source_node_id],
                                     positions[edge.target_node_id],
                                     boxes[edge.source_node_id], boxes[edge.target_node_id],
                                     list(boxes.values()), occupied)
                    if segments is None:
                        report.skipped_edges[edge.edge_id] = "无法避开符号找到正交走线"
                        continue
                    if not segments:
                        report.skipped_edges[edge.edge_id] = "连接端点重合"
                        continue
                    for segment_start, segment_end in segments:
                        requested_lines.append(_make_line(segment_start, segment_end))
                        line_edges.append(edge.edge_id)
                    occupied.extend(segments)

                failed_edges: set[str] = set()
                successful_edges: set[str] = set()
                if requested_lines:
                    progress(f"创建 {len(requested_lines)} 段导线…")
                    line_response = client.create_items(context.document, requested_lines)
                    if len(line_response.created_items) != len(requested_lines):
                        raise ApiError("KiCad 返回的导线创建结果数量不匹配")
                    for edge_id, requested, result in zip(line_edges, requested_lines,
                                                          line_response.created_items):
                        if result.status.code == editor_commands_pb2.ISC_OK:
                            successful_edges.add(edge_id)
                            created = schematic_types_pb2.Line()
                            if result.item.Unpack(created) and created.id.value:
                                item_id = created.id.value
                            else:
                                item_id = requested.id.value
                            report.item_ids.append(item_id)
                            report.edge_item_ids.setdefault(edge_id, []).append(item_id)
                        else:
                            failed_edges.add(edge_id)
                            report.skipped_edges[edge_id] = (
                                result.status.error_message or "KiCad 拒绝创建导线")

                # A failed segment can leave a visually connected but electrically
                # open path. Roll back this single commit instead of keeping it.
                if failed_edges:
                    raise ApiError("KiCad 拒绝部分导线，已取消本次生成：" +
                                   ", ".join(sorted(failed_edges)))

                report.created_edges.extend(sorted(successful_edges - failed_edges))
            if strict and (report.skipped_nodes or report.skipped_edges):
                raise ApiError("图未能完整绘制，已取消本次生成")
            if replace is not None:
                deletion = client.delete_items(context.document, old_ids)
                if (len(deletion.deleted_items) != len(old_ids)
                        or any(row.status != editor_commands_pb2.IDS_OK
                               for row in deletion.deleted_items)):
                    raise ApiError("旧图对象删除未全部成功，已取消本次更新")
            client.end_commit(
                commit_id,
                commit=True,
                message=f"Generate schematic from AI graph {graph.graph_id}",
            )
            committed = True
            try:
                report.item_fingerprints = _fingerprints(client, context.document,
                                                         report.item_ids)
                report.document_fingerprint = hashlib.sha256(
                    client.document_snapshot(context.document).encode("utf-8")
                ).hexdigest()
            except ApiError as exc:
                report.warnings.append(f"无法记录对象校验信息，后续反馈暂不可用：{exc}")
        finally:
            if not committed:
                try:
                    client.end_commit(commit_id, commit=False)
                except Exception:
                    pass

    return report
