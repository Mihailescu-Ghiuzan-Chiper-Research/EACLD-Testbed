"""Read the common virtual process independently from VM-CONTROL.

Node-specific measurement bias is applied locally in VM-NODES. It is not
included in the public plant API and does not reveal intervention labels.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import urllib.request

from eacld_contracts.interfaces import PLANT_STATE


@dataclass(frozen=True, slots=True)
class PlantObservation:
    temperature_mC: int
    humidity_mPct: int
    step_id: int

    @classmethod
    def parse(cls, payload: object) -> PlantObservation:
        if not isinstance(payload, dict):
            raise ValueError("the public plant response must be a JSON object")
        for key, lowest, highest in (
            ("temperature_mC", -(1 << 31), (1 << 31) - 1),
            ("humidity_mPct", 0, 100_000),
            ("step_id", 0, (1 << 63) - 1),
        ):
            value = payload.get(key)
            if type(value) is not int or not lowest <= value <= highest:
                raise ValueError(f"invalid public plant field: {key}")
        # Other upstream fields, if present, must never enter the node output.
        return cls(payload["temperature_mC"], payload["humidity_mPct"], payload["step_id"])


class PlantClient:
    def __init__(self, url: str = PLANT_STATE.url, timeout: float = 3.0):
        self.url = url
        self.timeout = timeout
        # Host-only VM communication must bypass administrative HTTP proxies.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def read(self) -> PlantObservation:
        request = urllib.request.Request(self.url, method="GET", headers={"Accept": "application/json"})
        with self._opener.open(request, timeout=self.timeout) as response:
            if response.status != 200:
                raise RuntimeError("public plant service returned an unsuccessful response")
            payload = json.load(response)
        return PlantObservation.parse(payload)


@dataclass(frozen=True, slots=True)
class SensorBias:
    temperature_mC: int = 0
    humidity_mPct: int = 0

    def __post_init__(self) -> None:
        for name in ("temperature_mC", "humidity_mPct"):
            value = getattr(self, name)
            if type(value) is not int or not -(1 << 31) <= value < (1 << 31):
                raise ValueError(f"{name} bias must be a signed 32-bit integer")


@dataclass(frozen=True, slots=True)
class SensorMeasurement:
    temperature_mC: int
    humidity_mPct: int


class VirtualSensor:
    def __init__(self, bias: SensorBias | None = None):
        self.bias = bias or SensorBias()

    def measure(self, observation: PlantObservation) -> SensorMeasurement:
        temperature = observation.temperature_mC + self.bias.temperature_mC
        humidity = observation.humidity_mPct + self.bias.humidity_mPct
        if not -(1 << 31) <= temperature < (1 << 31):
            raise ValueError("sensor temperature exceeds the signed 32-bit wire range")
        if not 0 <= humidity <= 100_000:
            raise ValueError("sensor humidity exceeds the physical percentage range")
        return SensorMeasurement(temperature_mC=temperature, humidity_mPct=humidity)
