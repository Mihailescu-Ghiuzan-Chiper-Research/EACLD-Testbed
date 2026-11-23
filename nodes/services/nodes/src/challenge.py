"""Direct active node challenge service on NET-PROBE.

This API is separate from the NET-CONTROL listener. The challenge response
is an authenticated type-2 binary frame encoded as hexadecimal text.
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict

from eacld_contracts.interfaces import parse_challenge_request
from eacld_contracts.probe_condition import ProbeCondition


class ChallengeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    nonce_hex: str
    node_id: int


class ChallengeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    frame_hex: str


def create_probe_app(service, condition: ProbeCondition | None = None) -> FastAPI:
    app = FastAPI(title="eacld-nodes challenge", docs_url=None,
                  redoc_url=None, openapi_url=None)
    condition = condition or ProbeCondition()

    @app.post("/v1/challenge", response_model=ChallengeResponse)
    def challenge(request: ChallengeRequest) -> ChallengeResponse:
        try:
            node_id, nonce = parse_challenge_request(request.model_dump())
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if node_id not in service.producers:
            raise HTTPException(status_code=404, detail="unknown node_id")
        try:
            condition.apply()
            frame = service.challenge(node_id, nonce)
        except (OSError, RuntimeError, ValueError):
            raise HTTPException(status_code=503, detail="plant observation unavailable") from None
        return ChallengeResponse(frame_hex=frame.to_hex())

    return app
