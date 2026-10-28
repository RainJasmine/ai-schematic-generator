"""Match Graph IR binding metadata to KiCad library symbols."""

from __future__ import annotations

import re
import unicodedata

from .graph_ir import NodeIR


def _normalized(value: str) -> str:
    """Search normalization must retain polarity and non-Latin names."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value).strip()).casefold()


def binding_matches(node: NodeIR, summary: object) -> bool:
    """Require the actual bound entry; a server library path is only a hint.

    Search ranking never authorizes substituting another model.  Explicit
    ``nickname:entry`` identities must match both parts exactly.  A server-side
    filename, however, may have a different client library nickname.
    """
    bound = node.kicad_symbol_name or node.library_name
    entry = str(summary.library_id.entry_name).strip()
    nickname = str(summary.library_id.library_nickname).strip()
    if bound:
        if ":" in bound:
            expected_library, expected_entry = (part.strip() for part in bound.split(":", 1))
            return nickname == expected_library and entry == expected_entry
        return entry == bound.strip()
    return any(entry == value.strip() for value in (node.component_name, node.component_id) if value)


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
    """Compatibility helper for callers able to place only one unit."""
    variants = matching_variants(node, definition)
    return variants[0] if len(variants) == 1 else None


def pin_matches(port: object, pin: object) -> bool:
    """Pin identities retain ``+``/``-``, overbar markup and Unicode letters."""
    return (str(pin.number) == port.pin_number
            and (not port.pin_name or _normalized(str(pin.name)) == _normalized(port.pin_name)))


def matching_variants(node: NodeIR, definition: object) -> tuple[object, ...]:
    """Cover all mapped ports with a deterministic set of actual symbol units.

    Unit-0/body-0 pins apply to each applicable unit.  Common unit 0 is never
    placed independently.  Different alternate body styles cannot be mixed in
    one symbol instance.  A multi-unit result requires the generator to place
    each returned unit under the same reference.
    """
    all_variants = list(definition.variants)
    variants = [v for v in all_variants if int(v.unit) > 0 and int(v.body_style) > 0]
    ports = [port for port in node.ports if port.pin_number]
    required_styles = {port.body_style for port in node.ports if port.body_style not in (None, 0)}
    if len(required_styles) > 1:
        return ()
    styles = list(dict.fromkeys(int(v.body_style) for v in variants))
    if required_styles:
        styles = [style for style in styles if style in required_styles]
    for style in styles:
        candidates = [v for v in variants if int(v.body_style) == style]
        coverage: list[set[int]] = []
        for variant in candidates:
            common = [pin for v in all_variants
                      if int(v.unit) in (0, int(variant.unit))
                      and int(v.body_style) in (0, style)
                      and (int(v.unit) == 0 or int(v.body_style) == 0)
                      for pin in v.pins]
            pins = [*variant.pins, *common]
            common_unit_pins = [pin for v in all_variants if int(v.unit) == 0
                                and int(v.body_style) in (0, style) for pin in v.pins]
            common_unit_pins.extend(pin for pin in variant.pins if getattr(pin, "unit", variant.unit) == 0)
            common_body_pins = [pin for v in all_variants if int(v.body_style) == 0
                                and int(v.unit) in (0, int(variant.unit)) for pin in v.pins]
            common_body_pins.extend(pin for pin in variant.pins
                                    if getattr(pin, "body_style", variant.body_style) == 0)
            coverage.append({i for i, port in enumerate(ports)
                             if (port.unit in (None, 0) or port.unit == variant.unit)
                             and any(pin_matches(port, pin)
                                     and (port.body_style != 0 or pin in common_body_pins) for pin in
                                     (common_unit_pins if port.unit == 0 else pins))})
        wanted = set(range(len(ports)))
        for index, variant in enumerate(candidates):
            if coverage[index] == wanted:
                return (variant,)
        selected: list[int] = []
        selected_units: set[int] = set()
        remaining = set(wanted)
        while remaining:
            eligible = [i for i, v in enumerate(candidates)
                        if int(v.unit) not in selected_units and coverage[i] & remaining]
            if not eligible:
                break
            best = max(eligible, key=lambda i: (len(coverage[i] & remaining), -i))
            selected.append(best)
            selected_units.add(int(candidates[best].unit))
            remaining -= coverage[best]
        if not remaining and selected:
            return tuple(candidates[i] for i in selected)
        if not ports and candidates:
            return (candidates[0],)
    return ()
