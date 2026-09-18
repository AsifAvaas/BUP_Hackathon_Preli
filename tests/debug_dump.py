"""Dump raw API responses for failing cases to understand the failures."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app

CASES_PATH = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"
)


def main() -> None:
    data = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    target_ids = {"SAMPLE-02", "SAMPLE-04", "SAMPLE-08"}
    for case in data["cases"]:
        if case["id"] not in target_ids:
            continue
        print(f"\n=== {case['id']} {case['label']} ===")
        expected = case["expected_output"]
        with patch(
            "app.main.interpret_notes",
            return_value=expected["directive_interpretation"],
        ):
            client = TestClient(app)
            r = client.post("/optimize-energy", json=case["input"])
        body = r.json()
        hours_by_index = {h["hour"]: h for h in case["input"]["hours"]}

        print(
            f"{'h':>3} {'act':>5} {'bk':>6} {'grid':>7} {'solar':>6} {'energy':>7} "
            f"{'demand':>7} {'lhs':>7} {'rhs':>7} {'ok':>3}"
        )
        prev_energy = case["input"]["battery"]["initial_energy_kwh"]
        for item in body["hourly_plan"]:
            h = item["hour"]
            action = item["battery_action"]
            bk = item["battery_kwh"]
            grid = item["grid_kwh"]
            solar = item["solar_used_kwh"]
            energy_after = item["battery_energy_after_kwh"]
            demand = hours_by_index[h]["demand_kwh"]
            if action == "charge":
                charge, discharge = bk, 0.0
            elif action == "discharge":
                charge, discharge = 0.0, bk
            else:
                charge, discharge = 0.0, 0.0
            lhs = grid + solar + discharge
            rhs = demand + charge
            ok = "✓" if abs(lhs - rhs) < 0.01 else "✗"
            print(
                f"{h:>3} {action:>5} {bk:>6.2f} {grid:>7.2f} {solar:>6.2f} "
                f"{energy_after:>7.2f} {demand:>7.2f} {lhs:>7.2f} {rhs:>7.2f} {ok:>3}"
            )
            prev_energy = energy_after
        print(
            f"  total_cost={body['total_cost_bdt']} "
            f"ref={expected['total_cost_bdt']}"
        )


if __name__ == "__main__":
    main()
