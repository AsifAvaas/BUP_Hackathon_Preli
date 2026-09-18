from __future__ import annotations

import logging

from pydantic import ValidationError

from app.schemas import BatteryData, DirectiveInterpretation, DirectiveType

logger = logging.getLogger(__name__)

_ALLOWED_TYPES = {d.value for d in DirectiveType}
_HOURS_TYPES = {
    DirectiveType.solar_reduction.value,
    DirectiveType.minimum_battery_reserve.value,
    DirectiveType.no_charge_window.value,
    DirectiveType.no_discharge_window.value,
    DirectiveType.max_grid_window.value,
}


def _fallback_entry(note_index: int, explanation: str) -> DirectiveInterpretation:
    return DirectiveInterpretation(
        note_index=note_index,
        applies=False,
        directive_type=DirectiveType.no_op,
        structured_adjustment=None,
        explanation=explanation,
    )


def _clean_hours(raw_hours: object) -> list[int]:
    if not isinstance(raw_hours, list):
        return []
    cleaned: set[int] = set()
    for h in raw_hours:
        try:
            hi = int(h)
        except (TypeError, ValueError):
            continue
        if 0 <= hi <= 23:
            cleaned.add(hi)
    return sorted(cleaned)


def _sanitize_entry(
    item: object, note_index: int, battery: BatteryData
) -> DirectiveInterpretation:
    if not isinstance(item, dict):
        return _fallback_entry(note_index, "Malformed interpretation; treated as no_op.")

    directive_type = item.get("directive_type")
    if directive_type not in _ALLOWED_TYPES:
        return _fallback_entry(note_index, "Unsupported directive type; treated as no_op.")

    explanation = str(item.get("explanation") or "").strip() or "No explanation provided."

    if directive_type == DirectiveType.no_op.value:
        return DirectiveInterpretation(
            note_index=note_index,
            applies=False,
            directive_type=DirectiveType.no_op,
            structured_adjustment=None,
            explanation=explanation,
        )

    raw_adjustment = item.get("structured_adjustment")
    raw_adjustment = raw_adjustment if isinstance(raw_adjustment, dict) else {}

    hours = _clean_hours(raw_adjustment.get("hours")) if directive_type in _HOURS_TYPES else []
    if directive_type in _HOURS_TYPES and not hours:
        return _fallback_entry(note_index, "No valid hours extracted; treated as no_op.")

    adjustment: dict = {"hours": hours}
    try:
        if directive_type == DirectiveType.solar_reduction.value:
            factor = float(raw_adjustment.get("factor", 1.0))
            adjustment["factor"] = min(1.0, max(0.0, factor))
        elif directive_type == DirectiveType.minimum_battery_reserve.value:
            reserve = float(raw_adjustment.get("minimum_energy_kwh", 0.0))
            adjustment["minimum_energy_kwh"] = min(battery.capacity_kwh, max(0.0, reserve))
        elif directive_type == DirectiveType.max_grid_window.value:
            cap = float(raw_adjustment.get("max_grid_kwh", 0.0))
            adjustment["max_grid_kwh"] = max(0.0, cap)
        # no_charge_window / no_discharge_window only need hours.
    except (TypeError, ValueError):
        return _fallback_entry(note_index, "Invalid numeric value; treated as no_op.")

    try:
        return DirectiveInterpretation(
            note_index=note_index,
            applies=True,
            directive_type=DirectiveType(directive_type),
            structured_adjustment=adjustment,
            explanation=explanation,
        )
    except ValidationError:
        return _fallback_entry(note_index, "Failed guardrail validation; treated as no_op.")


def sanitize_directives(
    raw_directives: object, battery: BatteryData, num_notes: int
) -> list[DirectiveInterpretation]:
    by_index: dict[int, DirectiveInterpretation] = {}

    if isinstance(raw_directives, list):
        for item in raw_directives:
            if not isinstance(item, dict):
                continue
            try:
                idx = int(item.get("note_index"))
            except (TypeError, ValueError):
                continue
            if idx < 0 or idx >= num_notes or idx in by_index:
                continue
            by_index[idx] = _sanitize_entry(item, idx, battery)

    result = []
    for idx in range(num_notes):
        if idx in by_index:
            result.append(by_index[idx])
        else:
            logger.warning("Missing interpretation for note_index=%d; using no_op fallback", idx)
            result.append(_fallback_entry(idx, "Missing interpretation; treated as no_op."))
    return result
