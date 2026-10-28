"""Serializable schematic ownership and conservative electrical snapshots.

Snapshots contain document identifiers and protobuf objects, never IPC tokens.
Symbol translation and field placement are cosmetic. Wire geometry is checked
through the physical pin/net partition, so moving a connected drawing is allowed
while reconnecting two ports is not silently treated as a cosmetic edit.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Iterable

from google.protobuf.any_pb2 import Any
from google.protobuf.json_format import MessageToDict
from google.protobuf.message import Message
from schematic import schematic_types_pb2 as sch
from .symbol_matching import pin_matches


KINDS = {cls.DESCRIPTOR.full_name: cls for cls in (sch.Symbol, sch.Line, sch.Junction)}


def decode_item(value: Message) -> Message | None:
    if isinstance(value, Any):
        cls = KINDS.get(value.TypeName())
        if cls is None:
            return None
        result = cls()
        if not value.Unpack(result):
            raise ValueError("无法解码原理图对象")
        return result
    return value if value.DESCRIPTOR.full_name in KINDS else None


def item_id(value: Message) -> str:
    item = decode_item(value)
    return item.id.value if item is not None else ""


def electrical_items(values: Iterable[Message]) -> list[Message]:
    """Unknown electrical types cannot be safely ignored during a merge."""
    result = []
    for packed in values:
        item = decode_item(packed)
        if item is not None:
            if not item.id.value:
                raise ValueError("宿主返回了缺少UUID的原理图对象")
            result.append(item)
        elif isinstance(packed, Any):
            kind = packed.TypeName().rsplit(".", 1)[-1]
            if kind not in {"Text", "TextBox", "Shape", "Bitmap", "Marker", "SymbolField"}:
                raise ValueError(f"当前适配器不能安全核验对象类型 {kind}，请在独立图纸导入")
    ids = [item.id.value for item in result]
    if len(ids) != len(set(ids)):
        raise ValueError("KiCad 返回了重复对象UUID")
    return result


def encode_items(items: Iterable[Message]) -> list[dict]:
    return [{"type": item.DESCRIPTOR.full_name,
             "protobuf": base64.b64encode(item.SerializeToString(deterministic=True)).decode("ascii")}
            for item in items]


def restore_items(records: list[dict]) -> list[Message]:
    result = []
    for record in records:
        cls = KINDS.get(record.get("type"))
        if cls is None:
            raise ValueError("图纸快照含未知对象类型")
        item = cls()
        item.ParseFromString(base64.b64decode(record["protobuf"], validate=True))
        result.append(item)
    return result


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def reference(symbol: Message) -> str:
    return next((field.value for field in symbol.fields
                 if field.name.casefold() == "reference" or (not field.name and field.field_id == 0)), "")


def semantic_record(item: Message) -> dict:
    record = MessageToDict(item, preserving_proto_field_name=True)
    record.pop("bounding_box", None)
    if isinstance(item, sch.Symbol):
        record.pop("position", None)
        # Returned pin positions are absolute; translations preserve local pin
        # geometry, pin identities and symbol orientation.
        record["pins"] = sorted(({
            "number": pin.number, "name": pin.name, "unit": pin.unit,
            "body_style": pin.body_style,
            "x": pin.position.x_nm - item.position.x_nm,
            "y": pin.position.y_nm - item.position.y_nm,
        } for pin in item.pins), key=lambda pin: (pin["number"], pin["unit"], pin["body_style"], pin["name"]))
        fields = []
        for field in item.fields:
            if field.name.casefold() == "reference" or (not field.name and field.field_id == 0):
                continue  # reference changes are display aliases, not rewiring
            fields.append({"name": field.name, "value": field.value, "field_id": field.field_id})
        record["fields"] = sorted(fields, key=lambda field: (field["field_id"], field["name"]))
    elif isinstance(item, sch.Line):
        record.pop("start", None)
        record.pop("end", None)
    elif isinstance(item, sch.Junction):
        record.pop("position", None)
    return record


def point_on_segment(point: tuple[int, int], start: tuple[int, int], end: tuple[int, int]) -> bool:
    x, y = point
    ax, ay = start
    bx, by = end
    return (min(ax, bx) <= x <= max(ax, bx) and min(ay, by) <= y <= max(ay, by)
            and (x - ax) * (by - ay) == (y - ay) * (bx - ax))


def pin_token(symbol: Message, pin: Message) -> str:
    # Explicit unit/body preserve the identity of same-number multiunit pins.
    return f"pin:{symbol.id.value}:{pin.unit or symbol.unit}:{pin.body_style or symbol.body_style}:{pin.number}"


def net_components(items: list[Message]) -> tuple[dict[str, tuple[int, int]], dict[tuple[int, int], tuple[int, int]]]:
    """Geometric connectivity: T contacts join, interior crossings need a junction.

    This verifies this adapter's explicit wires/pins. It is not KiCad ERC and
    deliberately cannot resolve global labels, buses or hidden power nets.
    """
    points: dict[str, tuple[int, int]] = {}
    segments = []
    common_pins: dict[tuple[str, str], tuple[int, int]] = {}
    parent: dict[tuple[int, int], tuple[int, int]] = {}

    def find(point):
        parent.setdefault(point, point)
        if parent[point] != point:
            parent[point] = find(parent[point])
        return parent[point]

    def union(a, b):
        a, b = find(a), find(b)
        if a != b:
            parent[max(a, b)] = min(a, b)

    for item in items:
        if isinstance(item, sch.Symbol):
            for pin in item.pins:
                token = pin_token(item, pin)
                point = (int(pin.position.x_nm), int(pin.position.y_nm))
                if token in points and points[token] != point:
                    raise ValueError("同一物理引脚返回了不同位置，无法核验连接")
                points[token] = point
                if pin.unit == 0 and reference(item):
                    common = (reference(item), pin.number)
                    if common in common_pins:
                        union(common_pins[common], point)
                    else:
                        common_pins[common] = point
        elif isinstance(item, sch.Line) and item.layer == sch.SL_WIRE:
            a = (int(item.start.x_nm), int(item.start.y_nm))
            b = (int(item.end.x_nm), int(item.end.y_nm))
            points[f"wire:{item.id.value}"] = a
            points[f"wire-end:{item.id.value}"] = b
            segments.append((a, b))
            union(a, b)
        elif isinstance(item, sch.Junction):
            points[f"junction:{item.id.value}"] = (int(item.position.x_nm), int(item.position.y_nm))
    for point in points.values():
        find(point)
        for start, end in segments:
            if point_on_segment(point, start, end):
                union(point, start)
    return points, {point: find(point) for point in points.values()}


def net_signature(items: list[Message], owned_ids: set[str]) -> list[list[str]]:
    points, components = net_components(items)
    grouped: dict[tuple[int, int], list[str]] = {}
    affected = set()
    for token, point in points.items():
        component = components[point]
        grouped.setdefault(component, []).append(token)
        # UUIDs never contain ':', so this also captures owned wire endpoints.
        if token.split(":", 2)[1] in owned_ids:
            affected.add(component)
    return sorted(sorted(tokens) for component, tokens in grouped.items() if component in affected)


def make_snapshot(items: list[Message], owned_ids: set[str]) -> dict:
    owned = {item.id.value: semantic_record(item) for item in items if item.id.value in owned_ids}
    if set(owned) != owned_ids:
        raise ValueError("部分插件对象已不存在，不能建立完整快照")
    return {"format": 1, "owned": owned, "nets": net_signature(items, owned_ids),
            "items": encode_items(item for item in items if item.id.value in owned_ids)}


def changed(snapshot: dict, current: list[Message], owned_ids: set[str]) -> bool:
    if snapshot.get("format") != 1:
        raise ValueError("当前图没有可核验的快照，请重新导入到独立图纸")
    return (snapshot.get("owned") != {item.id.value: semantic_record(item)
                                     for item in current if item.id.value in owned_ids}
            or snapshot.get("nets") != net_signature(current, owned_ids))


def require_isolated_ownership(items: list[Message], owned_ids: set[str]) -> None:
    """A foreign object may coexist, but may not attach to a managed net."""
    owned = [item for item in items if item.id.value in owned_ids]
    if net_signature(items, owned_ids) != net_signature(owned, owned_ids):
        raise ValueError("插件网络接入了未托管对象，无法确认完整连接；请保留本地工作并使用独立图纸。")


def physical_pin(symbols: list[Message], port) -> tuple[Message, Message] | None:
    matches = []
    for symbol in symbols:
        if port.unit is not None and port.unit not in (0, symbol.unit):
            continue
        if port.body_style is not None and port.body_style not in (0, symbol.body_style):
            continue
        for pin in symbol.pins:
            if pin_matches(port, pin):
                matches.append((symbol, pin))
    if len(matches) == 1:
        return matches[0]
    # The same common package pin can be shown in multiple units. Keep every
    # drawn unit, but use one deterministic access point for a declared common
    # port; never guess between distinct references or distinct physical pins.
    if matches and (port.unit == 0 or all(pin.unit == 0 for _, pin in matches)):
        identities = {(reference(symbol), pin.number, pin.name.strip().casefold()) for symbol, pin in matches}
        if len(identities) == 1 and next(iter(identities))[0]:
            return min(matches, key=lambda pair: (pair[0].unit, pair[0].body_style, pair[0].id.value))
    return None


def validate_graph_connections(graph, node_symbols: dict[str, list[Message]], all_items: list[Message]) -> None:
    """Compare Graph IR port-net partition against actual returned geometry."""
    points, components = net_components(all_items)
    ports = {}
    parent = {}

    def find(key):
        parent.setdefault(key, key)
        if parent[key] != key:
            parent[key] = find(parent[key])
        return parent[key]

    def union(a, b):
        a, b = find(a), find(b)
        if a != b:
            parent[max(a, b)] = min(a, b)

    identities = {}
    for node in graph.nodes:
        for port in node.ports:
            if not port.pin_number:
                continue
            physical = physical_pin(node_symbols[node.node_id], port)
            if physical is None:
                raise ValueError(f"{node.node_id}/{port.port_id} 没有唯一的实际引脚")
            symbol, pin = physical
            key = (node.node_id, port.port_id)
            token = pin_token(symbol, pin)
            ports[key] = components[points[token]]
            find(key)
            if token in identities:
                union(key, identities[token])
            identities[token] = key
    for edge in graph.connections:
        source = (edge.source_node_id, edge.source_port_id)
        target = (edge.target_node_id, edge.target_port_id)
        if source not in ports or target not in ports:
            raise ValueError(f"{edge.edge_id} 缺少实际引脚")
        union(source, target)
    expected_to_actual, actual_to_expected = {}, {}
    for port, actual in ports.items():
        expected = find(port)
        if expected in expected_to_actual and expected_to_actual[expected] != actual:
            raise ValueError(f"Graph IR连接未形成完整网络：{port[0]}/{port[1]}")
        if actual in actual_to_expected and actual_to_expected[actual] != expected:
            raise ValueError(f"检测到Graph IR未声明的引脚短接：{port[0]}/{port[1]}")
        expected_to_actual[expected] = actual
        actual_to_expected[actual] = expected
    represented = set(identities)
    declared_components = set(ports.values())
    declared_common = set()
    for symbols in node_symbols.values():
        for symbol in symbols:
            for pin in symbol.pins:
                if pin_token(symbol, pin) in represented and pin.unit == 0:
                    declared_common.add((reference(symbol), pin.number))
    for symbols in node_symbols.values():
        for symbol in symbols:
            for pin in symbol.pins:
                token = pin_token(symbol, pin)
                if token in represented or (pin.unit == 0 and (reference(symbol), pin.number) in declared_common):
                    continue
                if components[points[token]] in declared_components:
                    raise ValueError(f"检测到未在Graph IR端口中声明的引脚接入：{reference(symbol)}/{pin.number}")
