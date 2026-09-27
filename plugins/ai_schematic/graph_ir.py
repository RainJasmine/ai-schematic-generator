"""Parse and normalize ``ai-eda-graph-ir/v2`` topology for schematic generation."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import json
from pathlib import Path, PureWindowsPath
from typing import Any

SUPPORTED_SCHEMA = "ai-eda-graph-ir/v2.0"


class GraphIrError(ValueError):
    """Raised when the root document cannot be parsed as supported graph-ir."""


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(text for item in value if (text := _text(item)))


def _optional_positive_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


@dataclass(frozen=True, slots=True)
class Diagnostic:
    severity: str
    code: str
    message: str
    node_id: str = ""
    edge_id: str = ""
    path_id: str = ""
    region_id: str = ""

    def format(self) -> str:
        refs = [
            value
            for value in (
                f"node={self.node_id}" if self.node_id else "",
                f"edge={self.edge_id}" if self.edge_id else "",
                f"path={self.path_id}" if self.path_id else "",
                f"region={self.region_id}" if self.region_id else "",
            )
            if value
        ]
        suffix = f" ({', '.join(refs)})" if refs else ""
        return f"{self.code}: {self.message}{suffix}"


@dataclass(frozen=True, slots=True)
class PortIR:
    port_id: str
    pin_number: str
    pin_name: str
    unit: int | None
    body_style: int | None
    direction: str
    domain: str
    compatible_domains: tuple[str, ...]
    port_role: str
    required: bool


@dataclass(frozen=True, slots=True)
class NodeIR:
    node_id: str
    component_id: str
    component_name: str
    component_type: str
    functional_role: str
    path_id: str
    region_id: str
    signal_domain: str
    library_category: str
    library_subcategory: str
    library_name: str
    kicad_symbol_name: str
    kicad_library_path: str
    ports: tuple[PortIR, ...]
    source: dict[str, Any] = field(compare=False, repr=False)

    def search_terms(self) -> tuple[str, ...]:
        # model_metadata is a compatible extension used by this KiCad adapter.
        ordered = (
            self.kicad_symbol_name,
            self.library_name,
            self.component_name,
            self.component_id,
            self.component_type,
            self.functional_role,
        )
        result: list[str] = []
        seen: set[str] = set()
        for term in ordered:
            normalized = term.casefold().strip()
            if term and normalized not in seen:
                seen.add(normalized)
                result.append(term)
        return tuple(result)

    def library_id_hint(self) -> tuple[str, str] | None:
        """Prefer an explicit ID, then the new metadata's library filename hint."""
        for name in (self.kicad_symbol_name, self.library_name):
            if ":" in name:
                nickname, entry = (part.strip() for part in name.split(":", 1))
                if nickname and entry:
                    return nickname, entry
        if self.kicad_library_path and self.kicad_symbol_name:
            nickname = PureWindowsPath(self.kicad_library_path).stem
            if nickname:
                return nickname, self.kicad_symbol_name
        return None

    def port(self, port_id: str) -> PortIR | None:
        return next((port for port in self.ports if port.port_id == port_id), None)


@dataclass(frozen=True, slots=True)
class EdgeIR:
    edge_id: str
    source_node_id: str
    source_port_id: str
    target_node_id: str
    target_port_id: str
    edge_type: str
    path_id: str
    signal_domain: str
    connection_status: str
    valid: bool
    issues: tuple[str, ...]
    source: dict[str, Any] = field(compare=False, repr=False)


@dataclass(frozen=True, slots=True)
class PathIR:
    path_id: str
    path_type: str
    ordered_nodes: tuple[str, ...]
    edge_ids: tuple[str, ...] = ()
    anchor_node_ids: tuple[str, ...] = ()
    complete: bool = True
    source: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def is_main(self) -> bool:
        return self.path_type == "main_signal"


@dataclass(frozen=True, slots=True)
class RegionIR:
    region_id: str
    path_id: str
    left_anchor: str
    right_anchor: str
    node_ids: tuple[str, ...]
    signal_domain: str
    source: dict[str, Any] = field(compare=False, repr=False)


@dataclass(frozen=True, slots=True)
class GraphIR:
    graph_schema: str
    graph_id: str
    schema_version: str
    metadata: dict[str, Any]
    nodes: tuple[NodeIR, ...]
    edges: tuple[EdgeIR, ...]
    paths: tuple[PathIR, ...]
    regions: tuple[RegionIR, ...]
    orphans: tuple[str, ...]
    diagnostics: tuple[Diagnostic, ...]
    source: dict[str, Any] = field(compare=False, repr=False)

    @classmethod
    def load(cls, path: str | Path) -> "GraphIR":
        source_path = Path(path)
        try:
            with source_path.open("r", encoding="utf-8-sig") as stream:
                raw = json.load(stream)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise GraphIrError(f"无法读取 JSON：{exc}") from exc

        return cls.parse(raw, source_path.stem)

    @classmethod
    def parse(cls, raw: object, fallback_id: str = "ai-generated") -> "GraphIR":
        """Parse a graph object returned directly by the AI HTTP service."""

        if not isinstance(raw, dict):
            raise GraphIrError("JSON 顶层必须是对象")

        graph_schema = _text(raw.get("graph_schema"))
        schema_version = _text(raw.get("schema_version"))
        schema_major = ""
        if graph_schema.startswith("ai-eda-graph-ir/v"):
            schema_major = graph_schema.removeprefix("ai-eda-graph-ir/v").split(".", 1)[0]
        if graph_schema and schema_major != "2":
            raise GraphIrError(
                f"GRAPH_SCHEMA_UNSUPPORTED: 不支持 {graph_schema}，当前支持 {SUPPORTED_SCHEMA}"
            )
        if schema_version and schema_version.split(".", 1)[0] != "2":
            raise GraphIrError(
                f"GRAPH_SCHEMA_UNSUPPORTED: 不支持 schema_version={schema_version}"
            )

        raw_nodes = raw.get("nodes")
        raw_edges = raw.get("edges")
        raw_paths = raw.get("paths", [])
        raw_regions = raw.get("regions", [])
        for name, value in (
            ("nodes", raw_nodes),
            ("edges", raw_edges),
            ("paths", raw_paths),
            ("regions", raw_regions),
        ):
            if not isinstance(value, list):
                raise GraphIrError(f"JSON 字段 {name} 必须是数组")

        diagnostics: list[Diagnostic] = []
        if not graph_schema:
            diagnostics.append(
                Diagnostic("warning", "GRAPH_SCHEMA_MISSING", "未声明 graph_schema，按 v2 兼容输入解析")
            )

        nodes, node_map, endpoint_map = _parse_nodes(raw_nodes, diagnostics)
        edges = _parse_edges(raw_edges, node_map, endpoint_map, diagnostics)
        paths = _parse_paths(raw_paths, node_map, edges, raw_regions, diagnostics)
        regions = _parse_regions(raw_regions, node_map, diagnostics)
        orphans = _find_orphans(nodes, edges)
        for node_id in orphans:
            diagnostics.append(
                Diagnostic("warning", "ORPHAN_NODE", "节点没有有效连接", node_id=node_id)
            )

        main_count = sum(path.is_main for path in paths)
        if main_count == 0:
            diagnostics.append(Diagnostic("warning", "MAIN_PATH_MISSING", "没有显式主链"))
        elif main_count > 1:
            diagnostics.append(Diagnostic("warning", "MULTIPLE_MAIN_PATHS", "存在多条主链"))

        metadata = raw.get("metadata")
        return cls(
            graph_schema=graph_schema or SUPPORTED_SCHEMA,
            graph_id=_text(raw.get("graph_id")) or fallback_id,
            schema_version=schema_version or "2.0",
            metadata=metadata if isinstance(metadata, dict) else {},
            nodes=tuple(nodes),
            edges=tuple(edges),
            paths=tuple(paths),
            regions=tuple(regions),
            orphans=tuple(orphans),
            diagnostics=tuple(diagnostics),
            source=raw,
        )

    @property
    def node_map(self) -> dict[str, NodeIR]:
        return {node.node_id: node for node in self.nodes}

    @property
    def connections(self) -> tuple[EdgeIR, ...]:
        """Connections safe for schematic drawing; edges remain the sole source."""
        return tuple(edge for edge in self.edges if edge.valid)

    @property
    def main_paths(self) -> tuple[PathIR, ...]:
        return tuple(path for path in self.paths if path.is_main)

    @property
    def branches(self) -> tuple[PathIR, ...]:
        return tuple(path for path in self.paths if not path.is_main)

    @property
    def warnings(self) -> tuple[str, ...]:
        return tuple(item.format() for item in self.diagnostics if item.severity == "warning")

    @property
    def errors(self) -> tuple[str, ...]:
        return tuple(item.format() for item in self.diagnostics if item.severity == "error")

    def summary(self) -> str:
        return (
            f"图 {self.graph_id}：{len(self.nodes)} 个节点，"
            f"{len(self.connections)}/{len(self.edges)} 条有效连接"
        )


def _parse_nodes(
    raw_nodes: list[Any], diagnostics: list[Diagnostic]
) -> tuple[list[NodeIR], dict[str, NodeIR], dict[tuple[str, str], PortIR]]:
    nodes: list[NodeIR] = []
    node_map: dict[str, NodeIR] = {}
    endpoint_map: dict[tuple[str, str], PortIR] = {}
    for index, item in enumerate(raw_nodes):
        if not isinstance(item, dict):
            diagnostics.append(Diagnostic("error", "NODE_ID_MISSING", f"nodes[{index}] 不是对象"))
            continue
        node_id = _text(item.get("node_id"))
        if not node_id:
            diagnostics.append(Diagnostic("error", "NODE_ID_MISSING", f"nodes[{index}] 缺少 node_id"))
            continue
        if node_id in node_map:
            diagnostics.append(Diagnostic("error", "NODE_ID_DUPLICATE", "节点 ID 重复，保留首次定义", node_id=node_id))
            continue

        raw_ports = item.get("ports", [])
        if isinstance(raw_ports, dict):
            raw_ports = [raw_ports]
        if not isinstance(raw_ports, list):
            raw_ports = []
            diagnostics.append(Diagnostic("error", "PORT_ID_MISSING", "ports 必须是数组", node_id=node_id))
        ports: list[PortIR] = []
        local_ids: set[str] = set()
        for port_index, raw_port in enumerate(raw_ports):
            if not isinstance(raw_port, dict):
                diagnostics.append(Diagnostic("error", "PORT_ID_MISSING", f"ports[{port_index}] 不是对象", node_id=node_id))
                continue
            port_id = _text(raw_port.get("port_id"))
            if not port_id:
                diagnostics.append(Diagnostic("error", "PORT_ID_MISSING", f"ports[{port_index}] 缺少 port_id", node_id=node_id))
                continue
            if port_id in local_ids:
                diagnostics.append(Diagnostic("error", "PORT_ID_DUPLICATE", "同一节点内端口 ID 重复，保留首次定义", node_id=node_id))
                continue
            local_ids.add(port_id)
            port = PortIR(
                port_id=port_id,
                pin_number=_text(raw_port.get("pin_number")),
                pin_name=_text(raw_port.get("pin_name")),
                unit=_optional_positive_int(raw_port.get("unit")),
                body_style=_optional_positive_int(raw_port.get("body_style")),
                direction=_text(raw_port.get("direction")) or "unknown",
                domain=_text(raw_port.get("domain")),
                compatible_domains=_string_tuple(raw_port.get("compatible_domains")),
                port_role=_text(raw_port.get("port_role")),
                required=bool(raw_port.get("required", False)),
            )
            ports.append(port)
            endpoint_map[(node_id, port_id)] = port

        model = item.get("model_metadata")
        model = model if isinstance(model, dict) else {}
        node = NodeIR(
            node_id=node_id,
            component_id=_text(item.get("component_id")),
            component_name=_text(item.get("component_name")),
            component_type=_text(item.get("component_type")),
            functional_role=_text(item.get("functional_role")),
            path_id=_text(item.get("path_id")),
            region_id=_text(item.get("region_id")),
            signal_domain=_text(item.get("signal_domain")),
            library_category=_text(model.get("library_category")),
            library_subcategory=_text(model.get("library_subcategory")),
            library_name=_text(model.get("library_name")),
            kicad_symbol_name=_text(model.get("kicad_symbol_name"))
                              or _text(item.get("kicad_symbol_name")),
            kicad_library_path=_text(model.get("kicad_library_path"))
                               or _text(item.get("kicad_library_path")),
            ports=tuple(ports),
            source=item,
        )
        nodes.append(node)
        node_map[node_id] = node
    return nodes, node_map, endpoint_map


def _domain_supported(port: PortIR, edge_domain: str) -> bool:
    if not edge_domain or not port.domain or port.domain == "MULTI":
        return True
    return port.domain == edge_domain or edge_domain in port.compatible_domains


def _parse_edges(
    raw_edges: list[Any],
    node_map: dict[str, NodeIR],
    endpoint_map: dict[tuple[str, str], PortIR],
    diagnostics: list[Diagnostic],
) -> list[EdgeIR]:
    edges: list[EdgeIR] = []
    edge_ids: set[str] = set()
    for index, item in enumerate(raw_edges):
        if not isinstance(item, dict):
            diagnostics.append(Diagnostic("error", "EDGE_ID_MISSING", f"edges[{index}] 不是对象"))
            continue
        edge_id = _text(item.get("edge_id"))
        if not edge_id:
            diagnostics.append(Diagnostic("error", "EDGE_ID_MISSING", f"edges[{index}] 缺少 edge_id"))
            continue
        if edge_id in edge_ids:
            diagnostics.append(Diagnostic("error", "EDGE_ID_DUPLICATE", "边 ID 重复，保留首次定义", edge_id=edge_id))
            continue
        edge_ids.add(edge_id)
        source_node_id = _text(item.get("source_node_id"))
        source_port_id = _text(item.get("source_port_id"))
        target_node_id = _text(item.get("target_node_id"))
        target_port_id = _text(item.get("target_port_id"))
        status = _text(item.get("connection_status")) or "connected"
        edge_domain = _text(item.get("signal_domain"))
        issues: list[str] = []

        def error(code: str, message: str) -> None:
            issues.append(code)
            diagnostics.append(Diagnostic("error", code, message, edge_id=edge_id))

        if source_node_id not in node_map:
            error("EDGE_SOURCE_NODE_MISSING", "源节点不存在")
        if target_node_id not in node_map:
            error("EDGE_TARGET_NODE_MISSING", "目标节点不存在")
        source_port = endpoint_map.get((source_node_id, source_port_id))
        target_port = endpoint_map.get((target_node_id, target_port_id))
        if source_node_id in node_map and source_port is None:
            error("EDGE_SOURCE_PORT_MISSING", "源端口不存在")
        if target_node_id in node_map and target_port is None:
            error("EDGE_TARGET_PORT_MISSING", "目标端口不存在")
        if status != "connected":
            issues.append("EDGE_NOT_CONNECTED")
            diagnostics.append(Diagnostic("warning", "EDGE_NOT_CONNECTED", f"connection_status={status}", edge_id=edge_id))
        if source_port and source_port.direction not in ("output", "bidirectional", "unknown", ""):
            issues.append("PORT_DIRECTION_SUSPICIOUS")
            diagnostics.append(Diagnostic("warning", "PORT_DIRECTION_SUSPICIOUS", "源端口方向可疑", edge_id=edge_id))
        if target_port and target_port.direction not in ("input", "bidirectional", "unknown", ""):
            issues.append("PORT_DIRECTION_SUSPICIOUS")
            diagnostics.append(Diagnostic("warning", "PORT_DIRECTION_SUSPICIOUS", "目标端口方向可疑", edge_id=edge_id))
        if (source_port and not _domain_supported(source_port, edge_domain)) or (
            target_port and not _domain_supported(target_port, edge_domain)
        ):
            issues.append("PORT_DOMAIN_MISMATCH")
            diagnostics.append(Diagnostic("warning", "PORT_DOMAIN_MISMATCH", "连接信号域不被端口支持", edge_id=edge_id))
        structural = any(code.startswith("EDGE_") and code != "EDGE_NOT_CONNECTED" for code in issues)
        edges.append(
            EdgeIR(
                edge_id=edge_id,
                source_node_id=source_node_id,
                source_port_id=source_port_id,
                target_node_id=target_node_id,
                target_port_id=target_port_id,
                edge_type=_text(item.get("edge_type")),
                path_id=_text(item.get("path_id")),
                signal_domain=edge_domain,
                connection_status=status,
                valid=not structural and status == "connected",
                issues=tuple(dict.fromkeys(issues)),
                source=item,
            )
        )
    return edges


def _parse_paths(
    raw_paths: list[Any],
    node_map: dict[str, NodeIR],
    edges: list[EdgeIR],
    raw_regions: list[Any],
    diagnostics: list[Diagnostic],
) -> list[PathIR]:
    direct: dict[tuple[str, str], list[EdgeIR]] = defaultdict(list)
    for edge in edges:
        if edge.valid:
            direct[(edge.source_node_id, edge.target_node_id)].append(edge)
    path_ids: set[str] = set()
    prelim: list[tuple[dict[str, Any], str, str, tuple[str, ...], tuple[str, ...], bool]] = []
    for index, item in enumerate(raw_paths):
        if not isinstance(item, dict):
            continue
        path_id = _text(item.get("path_id")) or f"path-{index + 1}"
        if path_id in path_ids:
            diagnostics.append(Diagnostic("warning", "PATH_ID_DUPLICATE", "路径 ID 重复，保留首次定义", path_id=path_id))
            continue
        path_ids.add(path_id)
        raw_ordered = item.get("ordered_nodes", [])
        if not isinstance(raw_ordered, list):
            raw_ordered = []
            diagnostics.append(Diagnostic("warning", "PATH_NODE_MISSING", "ordered_nodes 不是数组", path_id=path_id))
        ordered = tuple(value for value in (_text(entry) for entry in raw_ordered) if value)
        complete = True
        edge_ids: list[str] = []
        for node_id in ordered:
            if node_id not in node_map:
                complete = False
                diagnostics.append(Diagnostic("warning", "PATH_NODE_MISSING", "路径引用未知节点", node_id=node_id, path_id=path_id))
        for left, right in zip(ordered, ordered[1:]):
            matches = direct.get((left, right), [])
            if matches:
                edge_ids.extend(edge.edge_id for edge in matches)
            elif direct.get((right, left)):
                complete = False
                diagnostics.append(Diagnostic("warning", "PATH_DIRECTION_MISMATCH", "路径顺序与真实边方向相反", path_id=path_id))
            else:
                complete = False
                diagnostics.append(Diagnostic("warning", "PATH_EDGE_MISSING", f"相邻节点 {left} -> {right} 没有真实边", path_id=path_id))
        edge_ids.extend(edge.edge_id for edge in edges if edge.valid and edge.path_id == path_id)
        prelim.append((item, path_id, _text(item.get("path_type")), ordered, tuple(dict.fromkeys(edge_ids)), complete))

    main_nodes = {
        node_id
        for _, _, path_type, ordered, _, _ in prelim
        if path_type == "main_signal"
        for node_id in ordered
    }
    result: list[PathIR] = []
    for item, path_id, path_type, ordered, edge_ids, complete in prelim:
        anchors: list[str] = []
        if path_type != "main_signal":
            branch_nodes = set(ordered)
            other_path_nodes = {
                node_id
                for _, other_id, _, other_ordered, _, _ in prelim
                if other_id != path_id
                for node_id in other_ordered
            }
            branch_only_nodes = branch_nodes - other_path_nodes
            # Priority 1: a real edge crossing from this path into another path.
            for edge in edges:
                if not edge.valid:
                    continue
                if edge.source_node_id in branch_only_nodes and edge.target_node_id in other_path_nodes:
                    anchors.append(edge.target_node_id)
                elif edge.target_node_id in branch_only_nodes and edge.source_node_id in other_path_nodes:
                    anchors.append(edge.source_node_id)
            # Priority 2: an explicitly shared node in ordered_nodes.
            if not anchors:
                anchors.extend(node_id for node_id in ordered if node_id in other_path_nodes)
            # Priority 3: region anchors are layout hints only.
            if not anchors:
                for region in raw_regions:
                    if isinstance(region, dict) and _text(region.get("path_id")) == path_id:
                        anchors.extend(
                            value
                            for value in (_text(region.get("left_anchor")), _text(region.get("right_anchor")))
                            if value in other_path_nodes or value in main_nodes
                        )
            if not anchors:
                diagnostics.append(Diagnostic("warning", "BRANCH_ANCHOR_UNCLEAR", "支路没有明确汇入或分出节点", path_id=path_id))
        result.append(
            PathIR(
                path_id=path_id,
                path_type=path_type,
                ordered_nodes=ordered,
                edge_ids=edge_ids,
                anchor_node_ids=tuple(dict.fromkeys(anchors)),
                complete=complete,
                source=item,
            )
        )
    return result


def _parse_regions(
    raw_regions: list[Any], node_map: dict[str, NodeIR], diagnostics: list[Diagnostic]
) -> list[RegionIR]:
    result: list[RegionIR] = []
    region_ids: set[str] = set()
    for index, item in enumerate(raw_regions):
        if not isinstance(item, dict):
            continue
        region_id = _text(item.get("region_id")) or f"region-{index + 1}"
        if region_id in region_ids:
            continue
        region_ids.add(region_id)
        node_ids = _string_tuple(item.get("node_ids"))
        for node_id in node_ids:
            if node_id not in node_map:
                diagnostics.append(Diagnostic("warning", "REGION_NODE_MISSING", "区域引用未知节点", node_id=node_id, region_id=region_id))
        result.append(
            RegionIR(
                region_id=region_id,
                path_id=_text(item.get("path_id")),
                left_anchor=_text(item.get("left_anchor")),
                right_anchor=_text(item.get("right_anchor")),
                node_ids=node_ids,
                signal_domain=_text(item.get("signal_domain")),
                source=item,
            )
        )
    return result


def _find_orphans(nodes: list[NodeIR], edges: list[EdgeIR]) -> list[str]:
    connected: set[str] = set()
    for edge in edges:
        if edge.valid:
            connected.add(edge.source_node_id)
            connected.add(edge.target_node_id)
    return [node.node_id for node in nodes if node.node_id not in connected]
