"""Match Graph IR binding metadata to KiCad library symbols."""

from __future__ import annotations

import re

from .graph_ir import NodeIR


def _normalized(value: str) -> str:
    return re.sub(r"[^0-9a-z]+", "", value.casefold())


def candidate_score(node: NodeIR, summary: object) -> int:
    candidates = (_normalized(summary.display_name),
                  _normalized(summary.library_id.entry_name))
    terms = [_normalized(value) for value in node.search_terms() if value]
    score = 0
    for index, term in enumerate(terms):
        if term in candidates:
            score = max(score, 200 - index * 10)
        elif term and any(term in candidate or candidate in term for candidate in candidates):
            score = max(score, 100 - index * 5)
    hint = node.library_id_hint()
    if hint and summary.library_id.library_nickname.casefold() == hint[0].casefold():
        score += 20
    return score


def matching_variant(node: NodeIR, definition: object) -> object | None:
    """Match port IDs to library pin numbers and, when supplied, names."""
    for variant in definition.variants:
        if any((port.unit is not None and port.unit != variant.unit)
               or (port.body_style is not None and port.body_style != variant.body_style)
               for port in node.ports):
            continue
        if all(
            any(pin.number == port.pin_number
                and (not port.pin_name or _normalized(pin.name) == _normalized(port.pin_name))
                for pin in variant.pins)
            for port in node.ports if port.pin_number
        ):
            return variant
    return None
