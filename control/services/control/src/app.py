"""Read-only NET-PLANT HTTP API for the shared virtual process.

The service deliberately exposes no endpoint for orchestration, injection,
run labels, private truth, or mutating process state.
"""
from __future__ import annotations

import os

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict

from .private_store import DEFAULT_DB_PATH, PrivateStore

SERVICE_VERSION = "0.1.0"


class PublicPlantState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    temperature_mC: int
    humidity_mPct: int
    step_id: int


class Health(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str
    service_version: str


def create_app(db_path: str | None = None) -> FastAPI:
    store = PrivateStore(db_path or os.environ.get("EACLD_PRIVATE_DB", DEFAULT_DB_PATH))
    api = FastAPI(title="eacld-control", version=SERVICE_VERSION,
                  docs_url=None, redoc_url=None, openapi_url=None)

    @api.get("/v1/state", response_model=PublicPlantState)
    def read_state() -> dict[str, int]:
        return store.public_state()

    @api.get("/healthz", response_model=Health)
    def health() -> dict[str, str]:
        store.public_state()
        return {"status": "ok", "service_version": SERVICE_VERSION}

    return api


app = create_app()
