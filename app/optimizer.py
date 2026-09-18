from __future__ import annotations

import shutil

import pulp

from app.schemas import (
    BatteryAction,
    BatteryData,
    DirectiveInterpretation,
    DirectiveType,
    HourData,
    HourlyPlanItem,
    ScenarioRequest,
)

EPSILON = 1e-4


SOLVER_TIME_LIMIT_SECONDS = 15


def _solver() -> pulp.LpSolver:
    cbc_path = shutil.which("cbc")
    if cbc_path:
        return pulp.COIN_CMD(path=cbc_path, msg=False, timeLimit=SOLVER_TIME_LIMIT_SECONDS)
    return pulp.PULP_CBC_CMD(msg=False, timeLimit=SOLVER_TIME_LIMIT_SECONDS)


class _EffectiveInputs:
    def __init__(
        self,
        effective_solar: list[float],
        reserve_min: list[float],
        no_charge: set[int],
        no_discharge: set[int],
        max_grid_cap: dict[int, float],
    ) -> None:
        self.effective_solar = effective_solar
        self.reserve_min = reserve_min
        self.no_charge = no_charge
        self.no_discharge = no_discharge
        self.max_grid_cap = max_grid_cap


def _build_effective_inputs(
    hours_by_index: list[HourData],
    battery: BatteryData,
    directives: list[DirectiveInterpretation],
) -> _EffectiveInputs:
    effective_solar = [h.solar_kwh for h in hours_by_index]
    reserve_min = [battery.minimum_energy_kwh] * 24
    no_charge: set[int] = set()
    no_discharge: set[int] = set()
    max_grid_cap: dict[int, float] = {}

    for d in directives:
        if not d.applies or d.structured_adjustment is None:
            continue
        adj = d.structured_adjustment
        hours = adj.hours or []
        if d.directive_type == DirectiveType.solar_reduction and adj.factor is not None:
            for h in hours:
                effective_solar[h] = hours_by_index[h].solar_kwh * adj.factor
        elif (
            d.directive_type == DirectiveType.minimum_battery_reserve
            and adj.minimum_energy_kwh is not None
        ):
            for h in hours:
                reserve_min[h] = max(reserve_min[h], adj.minimum_energy_kwh)
        elif d.directive_type == DirectiveType.no_charge_window:
            no_charge.update(hours)
        elif d.directive_type == DirectiveType.no_discharge_window:
            no_discharge.update(hours)
        elif d.directive_type == DirectiveType.max_grid_window and adj.max_grid_kwh is not None:
            for h in hours:
                cap = adj.max_grid_kwh
                max_grid_cap[h] = min(max_grid_cap.get(h, cap), cap)

    return _EffectiveInputs(effective_solar, reserve_min, no_charge, no_discharge, max_grid_cap)


def _solve_lp(
    hours_by_index: list[HourData], battery: BatteryData, eff: _EffectiveInputs
) -> tuple[str, dict | None]:
    prob = pulp.LpProblem("CampusEnergyOptimization", pulp.LpMinimize)

    grid = [pulp.LpVariable(f"grid_{h}", lowBound=0) for h in range(24)]
    solar_used = [
        pulp.LpVariable(f"solar_used_{h}", lowBound=0, upBound=eff.effective_solar[h])
        for h in range(24)
    ]
    charge_cap = [0 if h in eff.no_charge else battery.max_charge_kwh_per_hour for h in range(24)]
    discharge_cap = [
        0 if h in eff.no_discharge else battery.max_discharge_kwh_per_hour for h in range(24)
    ]
    charge = [
        pulp.LpVariable(f"charge_{h}", lowBound=0, upBound=charge_cap[h]) for h in range(24)
    ]
    discharge = [
        pulp.LpVariable(f"discharge_{h}", lowBound=0, upBound=discharge_cap[h])
        for h in range(24)
    ]
    is_charging = [
        pulp.LpVariable(f"is_charging_{h}", cat="Binary") for h in range(24)
    ]
    energy = [
        pulp.LpVariable(f"energy_{h}", lowBound=eff.reserve_min[h], upBound=battery.capacity_kwh)
        for h in range(24)
    ]

    prob += pulp.lpSum(
        grid[h] * hours_by_index[h].tariff_bdt_per_kwh for h in range(24)
    )

    for h in range(24):
        prob += (
            grid[h] + solar_used[h] + discharge[h] == hours_by_index[h].demand_kwh + charge[h]
        )
        if h in eff.max_grid_cap:
            prob += grid[h] <= eff.max_grid_cap[h]
        # Forbid simultaneous charge and discharge in the same hour: the response
        # schema can only report one battery_action per hour, so a solver-chosen
        # "wash trade" (nonzero charge and discharge that cancel out) would be
        # invisible in hourly_plan and break the reported energy balance.
        prob += charge[h] <= charge_cap[h] * is_charging[h]
        prob += discharge[h] <= discharge_cap[h] * (1 - is_charging[h])
        if h == 0:
            prob += energy[0] == battery.initial_energy_kwh + charge[0] - discharge[0]
        else:
            prob += energy[h] == energy[h - 1] + charge[h] - discharge[h]

    prob += energy[23] == battery.initial_energy_kwh

    status = prob.solve(_solver())
    status_str = pulp.LpStatus[status]
    if status_str != "Optimal":
        return status_str, None

    values = {
        "grid": [v.value() or 0.0 for v in grid],
        "solar_used": [v.value() or 0.0 for v in solar_used],
        "charge": [v.value() or 0.0 for v in charge],
        "discharge": [v.value() or 0.0 for v in discharge],
        "energy": [v.value() or 0.0 for v in energy],
    }
    return status_str, values


def _build_hourly_plan(hours_by_index: list[HourData], values: dict) -> list[HourlyPlanItem]:
    plan = []
    for h in range(24):
        c = round(values["charge"][h], 4)
        d = round(values["discharge"][h], 4)
        if c > EPSILON:
            action, magnitude = BatteryAction.charge, c
        elif d > EPSILON:
            action, magnitude = BatteryAction.discharge, d
        else:
            action, magnitude = BatteryAction.idle, 0.0
        plan.append(
            HourlyPlanItem(
                hour=h,
                grid_kwh=round(max(0.0, values["grid"][h]), 4),
                solar_used_kwh=round(max(0.0, values["solar_used"][h]), 4),
                battery_action=action,
                battery_kwh=magnitude,
                battery_energy_after_kwh=round(values["energy"][h], 4),
            )
        )
    return plan


def _summarize(
    directives: list[DirectiveInterpretation], hours_by_index: list[HourData], values: dict, fell_back: bool
) -> str:
    applied = [d.directive_type.value for d in directives if d.applies]
    if fell_back:
        return (
            "Directives conflicted with a feasible schedule, so the plan falls back to the "
            "baseline optimization with directives disabled while preserving energy balance "
            "and end-of-day battery neutrality."
        )
    if not applied:
        return (
            "No operator directives applied; the schedule minimizes grid cost using solar and "
            "battery flexibility while restoring the initial battery level by hour 23."
        )
    return (
        f"Applies {', '.join(applied)} while minimizing grid cost and restoring the initial "
        "battery level by hour 23."
    )


def solve_schedule(req: ScenarioRequest, directives: list[DirectiveInterpretation]) -> dict:
    hours_by_index: list[HourData] = [None] * 24  # type: ignore[list-item]
    for h in req.hours:
        hours_by_index[h.hour] = h

    eff = _build_effective_inputs(hours_by_index, req.battery, directives)
    status, values = _solve_lp(hours_by_index, req.battery, eff)

    fell_back = False
    if values is None:
        fell_back = True
        eff = _build_effective_inputs(hours_by_index, req.battery, [])
        status, values = _solve_lp(hours_by_index, req.battery, eff)
        if values is None:
            raise RuntimeError(f"Optimizer failed to find a feasible schedule (status={status})")

    hourly_plan = _build_hourly_plan(hours_by_index, values)
    total_grid_kwh = round(sum(item.grid_kwh for item in hourly_plan), 2)
    total_cost_bdt = round(
        sum(item.grid_kwh * hours_by_index[item.hour].tariff_bdt_per_kwh for item in hourly_plan),
        2,
    )
    peak_grid_kwh = round(max(item.grid_kwh for item in hourly_plan), 2)

    return {
        "scenario_id": req.scenario_id,
        "directive_interpretation": directives,
        "hourly_plan": hourly_plan,
        "total_grid_kwh": total_grid_kwh,
        "total_cost_bdt": total_cost_bdt,
        "peak_grid_kwh": peak_grid_kwh,
        "plan_summary": _summarize(directives, hours_by_index, values, fell_back),
    }
