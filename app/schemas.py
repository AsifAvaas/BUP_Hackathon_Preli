from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, field_validator, model_serializer, model_validator


class DirectiveType(str, Enum):
    solar_reduction = "solar_reduction"
    minimum_battery_reserve = "minimum_battery_reserve"
    no_charge_window = "no_charge_window"
    no_discharge_window = "no_discharge_window"
    max_grid_window = "max_grid_window"
    no_op = "no_op"


class BatteryAction(str, Enum):
    charge = "charge"
    discharge = "discharge"
    idle = "idle"


class HourData(BaseModel):
    hour: int = Field(ge=0, le=23)
    demand_kwh: float = Field(ge=0)
    solar_kwh: float = Field(ge=0)
    tariff_bdt_per_kwh: float = Field(ge=0)


class BatteryData(BaseModel):
    capacity_kwh: float = Field(ge=0)
    initial_energy_kwh: float = Field(ge=0)
    minimum_energy_kwh: float = Field(ge=0)
    max_charge_kwh_per_hour: float = Field(ge=0)
    max_discharge_kwh_per_hour: float = Field(ge=0)


class ScenarioRequest(BaseModel):
    scenario_id: str = Field(min_length=1)
    operator_notes: list[str] = Field(min_length=1, max_length=3)
    hours: list[HourData] = Field(min_length=24, max_length=24)
    battery: BatteryData

    @field_validator("operator_notes")
    @classmethod
    def notes_non_empty(cls, notes: list[str]) -> list[str]:
        for note in notes:
            if not note.strip():
                raise ValueError("operator_notes entries must be non-empty")
        return notes

    @field_validator("hours")
    @classmethod
    def hours_cover_0_to_23(cls, hours: list[HourData]) -> list[HourData]:
        seen = sorted(h.hour for h in hours)
        if seen != list(range(24)):
            raise ValueError("hours must contain exactly one entry for each hour 0 through 23")
        return hours


class StructuredAdjustment(BaseModel):
    hours: list[int] | None = None
    factor: float | None = None
    minimum_energy_kwh: float | None = None
    max_grid_kwh: float | None = None

    @field_validator("hours")
    @classmethod
    def hours_unique_ascending_in_range(cls, hours: list[int] | None) -> list[int] | None:
        if hours is None:
            return hours
        if any(h < 0 or h > 23 for h in hours):
            raise ValueError("hours must be within 0 through 23")
        if len(set(hours)) != len(hours):
            raise ValueError("hours must be unique")
        if hours != sorted(hours):
            raise ValueError("hours must be in ascending order")
        return hours

    @model_serializer
    def _dump_only_set_fields(self) -> dict:
        fields = {
            "hours": self.hours,
            "factor": self.factor,
            "minimum_energy_kwh": self.minimum_energy_kwh,
            "max_grid_kwh": self.max_grid_kwh,
        }
        return {k: v for k, v in fields.items() if v is not None}


class DirectiveInterpretation(BaseModel):
    note_index: int = Field(ge=0)
    applies: bool
    directive_type: DirectiveType
    structured_adjustment: StructuredAdjustment | None = None
    explanation: str

    @model_validator(mode="after")
    def no_op_consistency(self) -> "DirectiveInterpretation":
        if self.directive_type == DirectiveType.no_op:
            if self.applies or self.structured_adjustment is not None:
                raise ValueError("no_op requires applies=false and structured_adjustment=null")
        elif not self.applies:
            raise ValueError("applies must be true for every non-no_op directive")
        return self


class HourlyPlanItem(BaseModel):
    hour: int = Field(ge=0, le=23)
    grid_kwh: float = Field(ge=0)
    solar_used_kwh: float = Field(ge=0)
    battery_action: BatteryAction
    battery_kwh: float = Field(ge=0)
    battery_energy_after_kwh: float = Field(ge=0)


class ScenarioResponse(BaseModel):
    scenario_id: str
    directive_interpretation: list[DirectiveInterpretation]
    hourly_plan: list[HourlyPlanItem] = Field(min_length=24, max_length=24)
    total_grid_kwh: float
    total_cost_bdt: float
    peak_grid_kwh: float
    plan_summary: str
