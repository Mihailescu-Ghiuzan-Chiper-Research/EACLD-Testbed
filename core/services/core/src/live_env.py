"""LiveEnvironment adapter: execute four real distributed diagnostic actions.

Only permitted observations reach the unchanged E0 PolicyEngine. No synthetic
probe outcome, private cause label, or experimental intervention metadata is
consulted.
"""
from __future__ import annotations

import json
import secrets
import time
import urllib.request

from eacld_contracts.frame import CHALLENGE_RESPONSE_TYPE, Frame
from eacld_contracts.interfaces import NODE_CHALLENGE, REFERENCE_READ


class LiveEnvironment:
    """One fixed accepted telemetry frame and explicitly configured experiment context."""

    def __init__(self, *, reading, verifier, cfg: dict, episode_id: str,
                 block_id: str, mode: str, baseline_temperature_mC: int,
                 high_delta_mC: int, http_timeout: float = 3.0,
                 opener=None):
        if mode not in cfg["modes"]:
            raise ValueError("mode is absent from E0 calibrated model")
        if type(baseline_temperature_mC) is not int or type(high_delta_mC) is not int:
            raise ValueError("temperature baseline and delta must be integers")
        if not 1 <= high_delta_mC <= 100000000:
            raise ValueError("high_delta_mC must be positive")
        self.public = {
            "episode_id": episode_id, "block_id": block_id, "mode": mode,
            "passive": {"passive_mac": None, "passive_sequence": None},
        }
        self.reading = reading
        self.verifier = verifier
        self.cfg = cfg
        self.baseline = baseline_temperature_mC
        self.delta = high_delta_mC
        self.timeout = http_timeout
        self._opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._used: set[str] = set()

    def _classify_temperature(self, value: object) -> str:
        if type(value) is not int:
            raise ValueError("temperature must be an integer")
        return "high" if value >= self.baseline + self.delta else "normal"

    def _post(self, url: str, body: bytes) -> tuple[dict, int]:
        request = urllib.request.Request(url, data=body,
                    headers={"Content-Type": "application/json"}, method="POST")
        with self._opener.open(request, timeout=self.timeout) as response:
            if response.status != 200:
                raise OSError("remote active probe failed")
            data = response.read(4097)
            if len(data) > 4096:
                raise ValueError("active probe response too large")
        answer = json.loads(data)
        if not isinstance(answer, dict):
            raise ValueError("active probe must return an object")
        return answer, len(body) + len(data)

    def execute(self, action: str) -> tuple[str, dict]:
        if action not in self.cfg["actions"]:
            raise ValueError("unsupported E0 action")
        if action in self._used:
            raise ValueError("duplicate E0 active check")
        self._used.add(action)
        start_ns = time.monotonic_ns()
        bytes_transferred = 0
        try:
            if action == "message_verify":
                outcome = self.reading.mac_status
            elif action == "sequence_check":
                outcome = self.reading.sequence_status
            elif action == "reference_read":
                response, bytes_transferred = self._post(REFERENCE_READ.url, b"{}")
                if response.get("status") != "ok":
                    outcome = "unavailable"
                else:
                    outcome = self._classify_temperature(response.get("temperature_mC"))
            elif action == "node_challenge":
                nonce = secrets.token_bytes(16)
                body = json.dumps({"node_id": self.reading.node_id,
                                   "nonce_hex": nonce.hex()}).encode("utf-8")
                response, bytes_transferred = self._post(NODE_CHALLENGE.url, body)
                frame = Frame.from_hex(response["frame_hex"])
                if (frame.header.type != CHALLENGE_RESPONSE_TYPE
                    or frame.header.node_id != self.reading.node_id
                    or frame.header.nonce != nonce
                    or self.verifier.verify(self.reading.node_id, frame.to_bytes(),
                                            expected_type=CHALLENGE_RESPONSE_TYPE) != "valid"):
                    outcome = "unavailable"
                else:
                    outcome = self._classify_temperature(frame.header.temperature_mC)
            else:
                raise ValueError("unsupported E0 action")
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            outcome = "unavailable"
        allowed = {
            "reference_read": ("normal", "high", "unavailable"),
            "node_challenge": ("normal", "high", "unavailable"),
            "message_verify": ("valid", "invalid", "unavailable"),
            "sequence_check": ("fresh", "stale", "unavailable"),
        }
        if outcome not in allowed[action]:
            outcome = "unavailable"
        latency_ms = (time.monotonic_ns() - start_ns) / 1_000_000
        metadata = {
            "cost": float(self.cfg["actions"][action]["cost"]),
            "latency_ms": latency_ms,
            "bytes": bytes_transferred if bytes_transferred else
                     (len(self.reading.payload) if action in ("message_verify", "sequence_check") else 0),
        }
        return outcome, metadata
