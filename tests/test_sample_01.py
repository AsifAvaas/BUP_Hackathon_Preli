from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app

TOLERANCE = 0.01

CASES_PATH = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"
)


def _load_case(case_id: str) -> dict:
    data = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    for case in data["cases"]:
        if case["id"] == case_id:
            return case
    raise KeyError(case_id)


def test_health() -> None:
    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_optimize_energy_sample_01() -> None:
    case = _load_case("SAMPLE-01")
    expected = case["expected_output"]

    # LLM boundary is mocked with the case's ground-truth interpretation so this
    # test exercises guardrails + optimizer + API wiring without a live API key.
    with patch(
        "app.main.interpret_notes",
        return_value=expected["directive_interpretation"],
    ):
        client = TestClient(app)
        r = client.post("/optimize-energy", json=case["input"])

    assert r.status_code == 200
    body = r.json()

    assert body["scenario_id"] == case["input"]["scenario_id"]

    # Directive interpretation must match ground truth in note_index order.
    assert len(body["directive_interpretation"]) == len(expected["directive_interpretation"])
    for got, want in zip(body["directive_interpretation"], expected["directive_interpretation"]):
        assert got["note_index"] == want["note_index"]
        assert got["applies"] == want["applies"]
        assert got["directive_type"] == want["directive_type"]
        assert got["structured_adjustment"] == want["structured_adjustment"]

    hourly_plan = body["hourly_plan"]
    assert len(hourly_plan) == 24
    assert [item["hour"] for item in hourly_plan] == list(range(24))

    hours_by_index = {h["hour"]: h for h in case["input"]["hours"]}
    battery = case["input"]["battery"]

    # Energy balance holds every hour: grid + solar_used + discharge == demand + charge.
    for item in hourly_plan:
        demand = hours_by_index[item["hour"]]["demand_kwh"]
        charge = item["battery_kwh"] if item["battery_action"] == "charge" else 0.0
        discharge = item["battery_kwh"] if item["battery_action"] == "discharge" else 0.0
        lhs = item["grid_kwh"] + item["solar_used_kwh"] + discharge
        rhs = demand + charge
        assert abs(lhs - rhs) <= TOLERANCE, f"hour {item['hour']}: {lhs} != {rhs}"

    # End-of-day battery neutrality.
    assert abs(hourly_plan[-1]["battery_energy_after_kwh"] - battery["initial_energy_kwh"]) <= TOLERANCE

    # Reported totals are self-consistent with the returned hourly_plan.
    recomputed_grid = round(sum(i["grid_kwh"] for i in hourly_plan), 2)
    recomputed_cost = round(
        sum(i["grid_kwh"] * hours_by_index[i["hour"]]["tariff_bdt_per_kwh"] for i in hourly_plan),
        2,
    )
    recomputed_peak = round(max(i["grid_kwh"] for i in hourly_plan), 2)
    assert abs(body["total_grid_kwh"] - recomputed_grid) <= TOLERANCE
    assert abs(body["total_cost_bdt"] - recomputed_cost) <= TOLERANCE
    assert abs(body["peak_grid_kwh"] - recomputed_peak) <= TOLERANCE

    # Cost is optimal: matches the reference optimal cost within tolerance.
    assert abs(body["total_cost_bdt"] - expected["total_cost_bdt"]) <= 1.0


def test_llm_parser_falls_back_to_no_op_without_api_key(monkeypatch) -> None:
    import asyncio

    from app.llm_parser import interpret_notes
    from app.schemas import BatteryData

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    battery = BatteryData(
        capacity_kwh=200,
        initial_energy_kwh=100,
        minimum_energy_kwh=40,
        max_charge_kwh_per_hour=50,
        max_discharge_kwh_per_hour=50,
    )
    notes = ["Solar drops to 20% from 1pm to 3pm.", "Cafeteria menu changes tomorrow."]
    result = asyncio.run(interpret_notes(notes, battery))

    assert len(result) == len(notes)
    assert all(item["directive_type"] == "no_op" and item["applies"] is False for item in result)
