"""Private process-level intervention scheduling for VM-CONTROL.

Interventions here affect the common virtual process (H1). Sensor bias,
post-authentication changes and replay belong to other testbed services.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProcessIntervention:
    start_step: int
    end_step: int
    temperature_disturbance_mC: int = 0
    humidity_disturbance_mPct: int = 0
    cause_label: str = "H1"

    def __post_init__(self) -> None:
        if (type(self.start_step) is not int or type(self.end_step) is not int
                or self.start_step < 1 or self.end_step <= self.start_step
                or self.end_step > (1 << 63) - 1):
            raise ValueError("invalid process intervention step interval")
        for value in (self.temperature_disturbance_mC, self.humidity_disturbance_mPct):
            if type(value) is not int or not -(1 << 31) <= value < 1 << 31:
                raise ValueError("process disturbance must be a signed 32-bit integer")
        if self.cause_label != "H1":
            raise ValueError("only H1 process-level interventions belong to VM-CONTROL")

    def applies_to(self, step: int) -> bool:
        return self.start_step <= step < self.end_step
