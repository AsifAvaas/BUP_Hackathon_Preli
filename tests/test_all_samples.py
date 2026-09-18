"""Run all 10 public sample cases end-to-end through the API.

For each case we exercise the full pipeline (LLM boundary -> guardrails ->
optimizer -> response) by mocking the LLM with the case's ground-truth
interpretation (so the test is hermetic and deterministic, just like
test_sample_01.py does for SAMPLE-01), then independently re-verify:

1. Directive interpretation matches ground truth (type, applies, adjustment).
2. hourly_plan length, hour coverage, and battery neutrality.
3. Per-hour energy balance: grid + solar_used + discharge == demand + charge.
4. solar_used_kwh never exceeds effective solar (after any solar_reduction).
5. Battery bounds: energy_after_kwh in [max(base_min, directive_reserve), cap],
   charge/discharge rate limits, no-charge / no-discharge windows enforced.
6. max_grid_window caps enforced in the listed hours.
7. minimum_battery_reserve enforced in the listed hours.
8. Reported totals are self-consistent with hourly_plan.
9. Cost is within 1.0 BDT of the reference optimal cost (LP optimality check).

Then prints a per-case PASS/FAIL table with cost delta vs reference.
"""

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


def _load_cases() -> list[dict]:
    data = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    return data["cases"]


def _flatten_directive(adj: dict | None) -> tuple[set[int], dict]:
    if adj is None:
        return set(), {}
    hours = set(adj.get("hours", []) or [])
    params = {k: v for k, v in adj.items() if k != "hours"}
    return hours, params


def _verify_case(case: dict, body: dict) -> list[str]:
    """Verify a single response against ground truth + problem-statement rules.

    Returns a list of human-readable failure strings. Empty list == pass.
    """
    failures: list[str] = []
    expected = case["expected_output"]
    case_id = case["id"]

    # 1. Scenario id echo.
    if body["scenario_id"] != case["input"]["scenario_id"]:
        failures.append(f"scenario_id mismatch: {body['scenario_id']}")

    # 2. Directive interpretation coverage and shape.
    got_dirs = body["directive_interpretation"]
    want_dirs = expected["directive_interpretation"]
    if len(got_dirs) != len(want_dirs):
        failures.append(
            f"directive_interpretation length {len(got_dirs)} != {len(want_dirs)}"
        )
    for i, (g, w) in enumerate(zip(got_dirs, want_dirs)):
        if g["note_index"] != w["note_index"]:
            failures.append(f"dir[{i}] note_index mismatch")
        if g["applies"] != w["applies"]:
            failures.append(f"dir[{i}] applies {g['applies']} != {w['applies']}")
        if g["directive_type"] != w["directive_type"]:
            failures.append(
                f"dir[{i}] directive_type {g['directive_type']} != {w['directive_type']}"
            )
        sg = g.get("structured_adjustment") or {}
        sw = w.get("structured_adjustment") or {}
        if set((sg.get("hours") or [])) != set((sw.get("hours") or [])):
            failures.append(f"dir[{i}] hours {sg.get('hours')} != {sw.get('hours')}")
        for key in ("factor", "minimum_energy_kwh", "max_grid_kwh"):
            if key in sg and key in sw:
                if abs(float(sg[key]) - float(sw[key])) > TOLERANCE:
                    failures.append(
                        f"dir[{i}] {key} {sg[key]} != {sw[key]}"
                    )

    # 3. hourly_plan coverage + neutrality.
    plan = body["hourly_plan"]
    if len(plan) != 24:
        failures.append(f"hourly_plan length {len(plan)} != 24")
    if [it["hour"] for it in plan] != list(range(24)):
        failures.append("hourly_plan hours not 0..23 ordered")

    battery = case["input"]["battery"]
    if abs(plan[-1]["battery_energy_after_kwh"] - battery["initial_energy_kwh"]) > TOLERANCE:
        failures.append(
            f"end-of-day battery {plan[-1]['battery_energy_after_kwh']} != initial {battery['initial_energy_kwh']}"
        )

    # 4. Build hour index + directive maps.
    hours_by_index = {h["hour"]: h for h in case["input"]["hours"]}
    capacity = battery["capacity_kwh"]
    base_min = battery["minimum_energy_kwh"]
    max_ch = battery["max_charge_kwh_per_hour"]
    max_dis = battery["max_discharge_kwh_per_hour"]

    eff_solar = {h: hdat["solar_kwh"] for h, hdat in hours_by_index.items()}
    reserve_min = {h: base_min for h in range(24)}
    no_charge: set[int] = set()
    no_discharge: set[int] = set()
    max_grid: dict[int, float] = {}

    for g in got_dirs:
        if not g["applies"]:
            continue
        adj = g.get("structured_adjustment") or {}
        adj_hours, params = _flatten_directive(adj)
        if g["directive_type"] == "solar_reduction":
            factor = float(params.get("factor", 1.0))
            for h in adj_hours:
                eff_solar[h] = hours_by_index[h]["solar_kwh"] * factor
        elif g["directive_type"] == "minimum_battery_reserve":
            r = float(params["minimum_energy_kwh"])
            for h in adj_hours:
                reserve_min[h] = max(reserve_min[h], r)
        elif g["directive_type"] == "no_charge_window":
            no_charge.update(adj_hours)
        elif g["directive_type"] == "no_discharge_window":
            no_discharge.update(adj_hours)
        elif g["directive_type"] == "max_grid_window":
            cap = float(params["max_grid_kwh"])
            for h in adj_hours:
                max_grid[h] = min(max_grid.get(h, cap), cap)

    # 5. Per-hour energy balance, bounds, and directive application.
    prev_energy = battery["initial_energy_kwh"]
    for item in plan:
        h = item["hour"]
        demand = hours_by_index[h]["demand_kwh"]
        grid = item["grid_kwh"]
        solar_used = item["solar_used_kwh"]
        action = item["battery_action"]
        bk = item["battery_kwh"]
        energy_after = item["battery_energy_after_kwh"]

        # action consistency
        if action == "charge":
            charge, discharge = bk, 0.0
        elif action == "discharge":
            charge, discharge = 0.0, bk
        else:  # idle
            charge, discharge = 0.0, 0.0
            if bk != 0:
                failures.append(f"h{h}: idle with battery_kwh={bk}")
        if action == "charge" and h in no_charge:
            failures.append(f"h{h}: charge action in no_charge_window")
        if action == "discharge" and h in no_discharge:
            failures.append(f"h{h}: discharge action in no_discharge_window")

        # rate limits
        if charge > max_ch + TOLERANCE:
            failures.append(f"h{h}: charge {charge} > max {max_ch}")
        if discharge > max_dis + TOLERANCE:
            failures.append(f"h{h}: discharge {discharge} > max {max_dis}")

        # energy balance
        lhs = grid + solar_used + discharge
        rhs = demand + charge
        if abs(lhs - rhs) > TOLERANCE:
            failures.append(f"h{h}: balance {lhs:.4f} != {rhs:.4f}")

        # effective solar cap
        if solar_used > eff_solar[h] + TOLERANCE:
            failures.append(
                f"h{h}: solar_used {solar_used:.4f} > effective {eff_solar[h]:.4f}"
            )

        # battery bounds
        lo = reserve_min[h]
        hi = capacity
        if energy_after < lo - TOLERANCE:
            failures.append(
                f"h{h}: battery_energy_after {energy_after} < reserve {lo}"
            )
        if energy_after > hi + TOLERANCE:
            failures.append(f"h{h}: battery_energy_after {energy_after} > cap {hi}")

        # state transition
        expected_after = prev_energy + charge - discharge
        if abs(expected_after - energy_after) > TOLERANCE:
            failures.append(
                f"h{h}: state transition {expected_after} != {energy_after}"
            )

        # max_grid_window cap
        if h in max_grid and grid > max_grid[h] + TOLERANCE:
            failures.append(f"h{h}: grid {grid} > max {max_grid[h]}")

        prev_energy = energy_after

    # 6. Reported totals must match the returned plan.
    rec_grid = round(sum(it["grid_kwh"] for it in plan), 2)
    rec_cost = round(
        sum(
            it["grid_kwh"] * hours_by_index[it["hour"]]["tariff_bdt_per_kwh"]
            for it in plan
        ),
        2,
    )
    rec_peak = round(max(it["grid_kwh"] for it in plan), 2)
    if abs(body["total_grid_kwh"] - rec_grid) > TOLERANCE:
        failures.append(f"total_grid_kwh reported {body['total_grid_kwh']} != plan {rec_grid}")
    if abs(body["total_cost_bdt"] - rec_cost) > TOLERANCE:
        failures.append(f"total_cost_bdt reported {body['total_cost_bdt']} != plan {rec_cost}")
    if abs(body["peak_grid_kwh"] - rec_peak) > TOLERANCE:
        failures.append(f"peak_grid_kwh reported {body['peak_grid_kwh']} != plan {rec_peak}")

    # 7. Cost-optimality check vs reference schedule (within 1 BDT).
    #    Reference is one valid optimal; LP degeneracy means multiple optimal
    #    schedules exist. We require we're at or below reference cost.
    ref_cost = expected["total_cost_bdt"]
    if body["total_cost_bdt"] > ref_cost + 1.0:
        failures.append(
            f"total_cost_bdt {body['total_cost_bdt']} > ref optimal {ref_cost} + 1.0"
        )

    return failures


def _run_case(case: dict) -> tuple[str, list[str], dict]:
    """Run a single case through the API with LLM mocked to ground truth."""
    expected = case["expected_output"]
    # LLM mocked with the ground-truth interpretation list (as raw dicts,
    # before pydantic round-trip, just like a real model would emit).
    with patch("app.main.interpret_notes", return_value=expected["directive_interpretation"]):
        client = TestClient(app)
        r = client.post("/optimize-energy", json=case["input"])
    if r.status_code != 200:
        return case["id"], [f"HTTP {r.status_code}: {r.text}"], {}
    failures = _verify_case(case, r.json())
    return case["id"], failures, r.json()


def main() -> int:
    cases = _load_cases()
    print(
        f"Running {len(cases)} public sample cases through "
        f"POST /optimize-energy (LLM mocked to ground truth)\n"
    )
    print(
        f"{'CASE':<10} {'STATUS':<6} {'COST (BDT)':<14} {'REF (BDT)':<14} "
        f"{'DELTA':<10} {'NOTES'}"
    )
    print("-" * 90)

    all_passed = True
    cost_deltas: list[float] = []

    for case in cases:
        cid, failures, body = _run_case(case)
        if failures:
            status = "FAIL"
            all_passed = False
        else:
            status = "PASS"
        if body:
            cost = body["total_cost_bdt"]
            ref = case["expected_output"]["total_cost_bdt"]
            delta = round(cost - ref, 2)
            cost_deltas.append(delta)
            notes = "ok"
        else:
            cost = ref = delta = 0.0
            notes = "; ".join(failures[:3])
        print(
            f"{cid:<10} {status:<6} {cost:<14.2f} {ref:<14.2f} "
            f"{delta:<+10.2f} {notes}"
        )
        if failures:
            print(f"  failures for {cid}:")
            for f in failures:
                print(f"   - {f}")

    print("-" * 90)
    if all_passed:
        print(f"ALL {len(cases)} CASES PASSED")
        if cost_deltas:
            avg = sum(cost_deltas) / len(cost_deltas)
            print(
                f"avg cost delta vs reference optimal = {avg:+.4f} BDT "
                f"(0.0 = exactly matched the public reference schedule)"
            )
        return 0
    print("SOME CASES FAILED")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
