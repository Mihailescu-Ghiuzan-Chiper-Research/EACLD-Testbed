"""Application-level API definitions stated in architecture V1, Tables 7 and 9.

This module declares interfaces; it does not expose HTTP listeners, choose
undocumented request schemas, or implement distributed services.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, TypedDict


@dataclass(frozen=True, slots=True)
class Endpoint:
    method: str
    host: str
    port: int
    path: str

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}{self.path}"


PLANT_STATE = Endpoint("GET", "10.60.40.10", 8100, "/v1/state")
REFERENCE_READ = Endpoint("POST", "10.60.20.13", 8201, "/v1/reference/read")
NODE_CHALLENGE = Endpoint("POST", "10.60.20.12", 8202, "/v1/challenge")
CONTROL_AGENT_PORT = 8300
CONTROL_HEALTH_PATH = "/healthz"
MQTT_BROKER_HOST = "10.60.10.14"
MQTT_BROKER_PORT = 1883


class PlantState(TypedDict):
    temperature_mC: int
    humidity_mPct: int
    step_id: Any  # V1 does not fix the JSON scalar type.


class ReferenceReadResponse(TypedDict):
    temperature_mC: int
    status: Any  # Exact value set is defined by the implementing service.
    timestamp: Any  # Exact timestamp encoding is not specified in V1.


class ChallengeRequest(TypedDict):
    nonce_hex: str
    node_id: int


class ChallengeResponse(TypedDict):
    frame_hex: str  # 94-byte type-2 authenticated frame, hex encoded.


class HealthResponse(TypedDict):
    status: Any
    service_version: str


class AcquisitionMetadata(TypedDict):
    cost: Any
    latency_ms: Any
    bytes: int


class LivePublicState(TypedDict):
    episode_id: Any
    block_id: Any
    mode: Any
    passive: Any


class LiveEnvironment(Protocol):
    public: LivePublicState

    def execute(self, action: Any) -> tuple[Any, AcquisitionMetadata]: ...


def parse_challenge_request(value: ChallengeRequest) -> tuple[int, bytes]:
    """Validate the defined challenge inputs without assigning policy semantics."""
    if not isinstance(value, dict):
        raise TypeError("challenge request must be a JSON object")
    node_id = value.get("node_id")
    if type(node_id) is not int or not 0 <= node_id <= 65535:
        raise ValueError("node_id must be an unsigned 16-bit integer")
    nonce_hex = value.get("nonce_hex")
    if not isinstance(nonce_hex, str):
        raise ValueError("nonce_hex must be a hexadecimal string")
    try:
        nonce = bytes.fromhex(nonce_hex)
    except ValueError as exc:
        raise ValueError("nonce_hex is not valid hexadecimal data") from exc
    if len(nonce) != 16:
        raise ValueError("nonce_hex must encode exactly 16 bytes")
    return node_id, nonce
