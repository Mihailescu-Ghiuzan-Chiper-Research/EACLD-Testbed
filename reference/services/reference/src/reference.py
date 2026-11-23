"""Independent VM-REFERENCE acquisition service (EACLD architecture V1).

The reference channel reads the public virtual process directly on NET-PLANT.
It does not read VM-NODES, pass through the MQTT gateway, or access private
intervention metadata. The NET-PROBE endpoint returns only the three fields
specified in V1 Table 9.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import signal
from typing import Callable, Iterator
import urllib.request

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
import uvicorn

from eacld_contracts.interfaces import PLANT_STATE, REFERENCE_READ

LOGGER = logging.getLogger(__name__)
SERVICE_VERSION = "0.1.0"
MAX_PLANT_RESPONSE_BYTES = 65_536


@dataclass(frozen=True, slots=True)
class PublicPlantReading:
    temperature_mC: int
    humidity_mPct: int
    step_id: int

    @classmethod
    def from_public_json(cls, data: object) -> PublicPlantReading:
        """Validate only public fields; do not retain unforeseen/private fields."""
        if not isinstance(data, dict):
            raise ValueError("public plant response must be an object")
        bounds = {
            "temperature_mC": (-(1 << 31), (1 << 31) - 1),
            "humidity_mPct": (0, 100_000),
            "step_id": (0, (1 << 63) - 1),
        }
        for field, (minimum, maximum) in bounds.items():
            value = data.get(field)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"invalid public plant field: {field}")
        return cls(data["temperature_mC"], data["humidity_mPct"], data["step_id"])


class PlantReferenceClient:
    """Independent, uncached GET of the shared process via NET-PLANT."""

    def __init__(self, url: str = PLANT_STATE.url, timeout: float = 3.0):
        if timeout <= 0:
            raise ValueError("plant request timeout must be positive")
        self.url = url
        self.timeout = timeout
        # Isolated research networks must never be routed through ADMIN proxies.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def read(self) -> PublicPlantReading:
        request = urllib.request.Request(
            self.url, headers={"Accept": "application/json"}, method="GET"
        )
        with self._opener.open(request, timeout=self.timeout) as response:
            if response.status != 200:
                raise RuntimeError("public plant service did not return HTTP 200")
            body = response.read(MAX_PLANT_RESPONSE_BYTES + 1)
        if len(body) > MAX_PLANT_RESPONSE_BYTES:
            raise ValueError("public plant response exceeds size limit")
        return PublicPlantReading.from_public_json(json.loads(body))


class ReferenceReadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    temperature_mC: int
    status: str
    timestamp: str


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    status: str
    service_version: str


class ReferenceAcquisition:
    def __init__(self, plant: PlantReferenceClient, *,
                 clock: Callable[[], datetime] | None = None):
        self.plant = plant
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def read(self) -> ReferenceReadResponse:
        sample = self.plant.read()
        acquired_at = self.clock()
        if acquired_at.tzinfo is None or acquired_at.utcoffset() is None:
            raise ValueError("reference acquisition clock must be timezone-aware")
        return ReferenceReadResponse(
            temperature_mC=sample.temperature_mC,
            status="ok",
            timestamp=acquired_at.astimezone(timezone.utc)
                                 .isoformat(timespec="milliseconds")
                                 .replace("+00:00", "Z"),
        )


def create_probe_app(acquisition: ReferenceAcquisition | None = None) -> FastAPI:
    """NET-PROBE application: only POST /v1/reference/read is exposed."""
    acquisition = acquisition or ReferenceAcquisition(PlantReferenceClient())
    app = FastAPI(title="eacld-reference probe", version=SERVICE_VERSION,
                  docs_url=None, redoc_url=None, openapi_url=None)

    @app.post(REFERENCE_READ.path, response_model=ReferenceReadResponse)
    def reference_read() -> ReferenceReadResponse:
        try:
            return acquisition.read()
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
            # Fail closed, without returning upstream data or exception text.
            LOGGER.warning("reference acquisition failed", exc_info=False)
            raise HTTPException(status_code=503,
                                detail="reference observation unavailable") from None

    return app


def create_control_app(*, allowed_sources: frozenset[str] =
                       frozenset({"10.60.30.10", "127.0.0.1"})) -> FastAPI:
    """NET-CONTROL health-only API, restricted to VM-CONTROL and local checks."""
    app = FastAPI(title="eacld-reference control", version=SERVICE_VERSION,
                  docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def restrict_source(request: Request, call_next):
        source = request.client.host if request.client is not None else ""
        if source not in allowed_sources:
            return JSONResponse(status_code=403, content={"detail": "forbidden"})
        return await call_next(request)

    @app.get("/healthz", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(status="ok", service_version=SERVICE_VERSION)

    return app


class SignalManagedServer(uvicorn.Server):
    """Allow the joint server supervisor to own process signal handlers."""

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


async def serve(acquisition: ReferenceAcquisition | None = None) -> None:
    """Run separate NET-PROBE and NET-CONTROL listeners in one container."""
    probe = SignalManagedServer(uvicorn.Config(
        create_probe_app(acquisition), host="0.0.0.0", port=8201,
        log_level="info", access_log=False))
    control = SignalManagedServer(uvicorn.Config(
        create_control_app(), host="0.0.0.0", port=8300,
        log_level="info", access_log=False))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    servers = [asyncio.create_task(probe.serve()),
               asyncio.create_task(control.serve())]
    stopper = asyncio.create_task(stop.wait())
    try:
        finished, _ = await asyncio.wait(servers + [stopper],
                                         return_when=asyncio.FIRST_COMPLETED)
        if stopper not in finished:
            for task in finished:
                task.result()
            raise RuntimeError("a reference service HTTP listener stopped unexpectedly")
    finally:
        probe.should_exit = True
        control.should_exit = True
        await asyncio.gather(*servers, return_exceptions=True)
        stopper.cancel()
        await asyncio.gather(stopper, return_exceptions=True)


def main() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    main()
