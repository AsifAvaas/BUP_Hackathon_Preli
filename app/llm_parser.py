from __future__ import annotations

import logging
import os

from google import genai
from google.genai import types
from pydantic import BaseModel

from app.schemas import BatteryData

logger = logging.getLogger(__name__)

MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest")

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
- Only fill the structured_adjustment fields relevant to the chosen directive_type; leave the rest null.
- If a note is ambiguous, unrelated, or not energy-relevant, mark it no_op rather than guessing.
"""


class _RawAdjustment(BaseModel):
    hours: list[int] | None = None
    factor: float | None = None
    minimum_energy_kwh: float | None = None
    max_grid_kwh: float | None = None


class _RawInterpretation(BaseModel):
    note_index: int
    applies: bool
    directive_type: str
    structured_adjustment: _RawAdjustment | None = None
    explanation: str


class _RawResponse(BaseModel):
    interpretations: list[_RawInterpretation]


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
        "Return one interpretation entry per note above, in the same order."
    )


async def interpret_notes(operator_notes: list[str], battery: BatteryData) -> list[dict]:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        logger.warning("GEMINI_API_KEY missing; falling back to no_op interpretations")
        return _fallback_no_op(operator_notes)

    try:
        client = genai.Client(api_key=api_key)
        response = await client.aio.models.generate_content(
            model=MODEL,
            contents=_build_user_message(operator_notes, battery),
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema=_RawResponse,
            ),
        )
        parsed: _RawResponse | None = response.parsed
        if parsed is not None and parsed.interpretations:
            return [item.model_dump() for item in parsed.interpretations]
        logger.warning("LLM response missing usable structured output; falling back to no_op")
        return _fallback_no_op(operator_notes)
    except Exception:
        logger.exception("LLM interpretation call failed; falling back to no_op")
        return _fallback_no_op(operator_notes)
