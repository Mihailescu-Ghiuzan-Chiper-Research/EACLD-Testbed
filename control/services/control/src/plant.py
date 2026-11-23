"""Deterministic discrete-time virtual process for VM-CONTROL.

Model parameters are implementation defaults, not calibrated experiment data.
The experimental protocol determines the values to use in each run.
"""
from __future__ import annotations

from dataclasses import dataclass


PPM = 1_000_000
MIN_TEMPERATURE = -(1 << 31)
MAX_TEMPERATURE = (1 << 31) - 1
MAX_HUMIDITY = 100_000  # 100.000 % in milli-percent units


def _int_range(name: str, value: int, lower: int, upper: int) -> None:
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError(f"{name} must be an integer in [{lower}, {upper}]")


def _round_div_signed(numerator: int, denominator: int) -> int:
    """Round signed rational values to nearest integer, ties away from zero."""
    magnitude = (abs(numerator) + denominator // 2) // denominator
    return magnitude if numerator >= 0 else -magnitude


@dataclass(frozen=True, slots=True)
class PlantParameters:
    ambient_temperature_mC: int = 22_000
    ambient_humidity_mPct: int = 50_000
    temperature_relaxation_ppm: int = 50_000
    humidity_relaxation_ppm: int = 30_000
    heater_gain_mC_per_unit: int = 250

    def __post_init__(self) -> None:
        _int_range("ambient_temperature_mC", self.ambient_temperature_mC,
                   MIN_TEMPERATURE, MAX_TEMPERATURE)
        _int_range("ambient_humidity_mPct", self.ambient_humidity_mPct, 0, MAX_HUMIDITY)
        _int_range("temperature_relaxation_ppm", self.temperature_relaxation_ppm, 0, PPM)
        _int_range("humidity_relaxation_ppm", self.humidity_relaxation_ppm, 0, PPM)
        _int_range("heater_gain_mC_per_unit", self.heater_gain_mC_per_unit, 0, 100_000)


@dataclass(frozen=True, slots=True)
class PlantState:
    step_id: int
    temperature_mC: int
    humidity_mPct: int
    heater_input_units: int = 0

    def __post_init__(self) -> None:
        _int_range("step_id", self.step_id, 0, (1 << 63) - 1)
        _int_range("temperature_mC", self.temperature_mC,
                   MIN_TEMPERATURE, MAX_TEMPERATURE)
        _int_range("humidity_mPct", self.humidity_mPct, 0, MAX_HUMIDITY)
        _int_range("heater_input_units", self.heater_input_units, -100, 100)

    def public(self) -> dict[str, int]:
        return {
            "temperature_mC": self.temperature_mC,
            "humidity_mPct": self.humidity_mPct,
            "step_id": self.step_id,
        }


def advance(
    state: PlantState,
    parameters: PlantParameters,
    *,
    temperature_disturbance_mC: int = 0,
    humidity_disturbance_mPct: int = 0,
) -> PlantState:
    """Advance the common process by one deterministic discrete step."""
    temp_relax = _round_div_signed(
        (parameters.ambient_temperature_mC - state.temperature_mC)
        * parameters.temperature_relaxation_ppm, PPM
    )
    humidity_relax = _round_div_signed(
        (parameters.ambient_humidity_mPct - state.humidity_mPct)
        * parameters.humidity_relaxation_ppm, PPM
    )
    new_temp = (
        state.temperature_mC + temp_relax
        + state.heater_input_units * parameters.heater_gain_mC_per_unit
        + temperature_disturbance_mC
    )
    new_humidity = max(0, min(MAX_HUMIDITY,
        state.humidity_mPct + humidity_relax + humidity_disturbance_mPct
    ))
    return PlantState(
        step_id=state.step_id + 1,
        temperature_mC=new_temp,
        humidity_mPct=new_humidity,
        heater_input_units=state.heater_input_units,
    )
