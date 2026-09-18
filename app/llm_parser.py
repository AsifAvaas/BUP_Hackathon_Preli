from __future__ import annotations

import json
import logging
import os

from anthropic import AsyncAnthropic

from app.schemas import BatteryData

logger = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5-20251001"

DIRECTIVE_TYPES = [
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
]

SYSTEM_PROMPT = """You interpret campus operator notes for a 24-hour energy scheduling system.

Convert every note into exactly one directive. Supported directive types:
- solar_reduction: {"hours": [int...], "factor": number} — factor is the USABLE fraction remaining (an 80% reduction means factor=0.2).
- minimum_battery_reserve: {"hours": [int...], "minimum_energy_kwh": number} — if the note gives a percentage, convert it to kWh using the given battery capacity.
- no_charge_window: {"hours": [int...]}
- no_discharge_window: {"hours": [int...]}
- max_grid_window: {"hours": [int...], "max_grid_kwh": number}
- no_op: structured_adjustment is null — use this when the note does not affect the 24-hour energy schedule.

Rules:
- Time windows are start-inclusive, end-exclusive. "1 PM to 3 PM" -> hours [13,14]. "6 PM until 9 PM" -> hours [18,19,20].
- hours must be unique integers 0-23 in ascending order.
- Do not invent demand, solar, tariff, battery limits, or any directive type not listed above.
- Every note produces exactly one entry. applies=true for every non-no_op directive; applies=false only for no_op.
- If a note is ambiguous, unrelated, or not energy-relevant, mark it no_op rather than guessing.
"""

TOOL_SCHEMA = {
    "name": "submit_interpretations",
    "description": "Submit one structured directive interpretation per operator note, in note_index order.",
    "input_schema": {
        "type": "object",
        "properties": {
            "interpretations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "note_index": {"type": "integer"},
                        "applies": {"type": "boolean"},
                        "directive_type": {"type": "string", "enum": DIRECTIVE_TYPES},
                        "structured_adjustment": {
                            "type": ["object", "null"],
                            "properties": {
                                "hours": {"type": "array", "items": {"type": "integer"}},
                                "factor": {"type": "number"},
                                "minimum_energy_kwh": {"type": "number"},
                                "max_grid_kwh": {"type": "number"},
                            },
                        },
                        "explanation": {"type": "string"},
                    },
                    "required": [
                        "note_index",
                        "applies",
                        "directive_type",
                        "structured_adjustment",
                        "explanation",
                    ],
                },
            },
        },
        "required": ["interpretations"],
    },
}


def _fallback_no_op(operator_notes: list[str]) -> list[dict]:
    return [
        {
            "note_index": i,
            "applies": False,
            "directive_type": "no_op",
            "structured_adjustment": None,
            "explanation": "Fallback: interpretation unavailable, treated as no_op.",
        }
        for i in range(len(operator_notes))
    ]


def _build_user_message(operator_notes: list[str], battery: BatteryData) -> str:
    notes_block = "\n".join(f"{i}: {note}" for i, note in enumerate(operator_notes))
    return (
        f"Battery capacity_kwh: {battery.capacity_kwh}\n"
        f"Battery minimum_energy_kwh (base reserve): {battery.minimum_energy_kwh}\n\n"
        f"Operator notes:\n{notes_block}\n\n"
        "Call submit_interpretations with one entry per note above, in the same order."
    )


async def interpret_notes(operator_notes: list[str], battery: BatteryData) -> list[dict]:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY missing; falling back to no_op interpretations")
        return _fallback_no_op(operator_notes)

    try:
        client = AsyncAnthropic(api_key=api_key)
        response = await client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=[TOOL_SCHEMA],
            tool_choice={"type": "tool", "name": "submit_interpretations"},
            messages=[{"role": "user", "content": _build_user_message(operator_notes, battery)}],
        )
        for block in response.content:
            if block.type == "tool_use" and block.name == "submit_interpretations":
                interpretations = block.input.get("interpretations", [])
                if isinstance(interpretations, list) and interpretations:
                    return interpretations
        logger.warning("LLM response missing usable tool_use block; falling back to no_op")
        return _fallback_no_op(operator_notes)
    except Exception:
        logger.exception("LLM interpretation call failed; falling back to no_op")
        return _fallback_no_op(operator_notes)
