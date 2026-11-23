"""Node-specific producer for the shared 94-byte EACLD wire frame.

Sequence numbers are unique per node/boot across telemetry and challenges.
All authentication is delegated to the shared protocol implementation.
"""
from __future__ import annotations

import secrets
import threading
import time

from eacld_contracts.frame import (
    CHALLENGE_RESPONSE_TYPE,
    TELEMETRY_TYPE,
    Frame,
    FrameHeader,
    sign_frame,
)

from .sensor import SensorMeasurement


class NodeFrameProducer:
    def __init__(self, *, node_id: int, zone_id: int, key_version: int,
                 key: bytes, protocol_version: int, boot_id: bytes | None = None):
        if not isinstance(key, bytes) or len(key) < 32:
            raise ValueError("node HMAC key must contain at least 32 bytes")
        self.node_id = node_id
        self.zone_id = zone_id
        self.key_version = key_version
        self.protocol_version = protocol_version
        self._key = key
        self.boot_id = secrets.token_bytes(16) if boot_id is None else boot_id
        self._start_ns = time.monotonic_ns()
        self._sequence = 0
        self._lock = threading.Lock()
        # Validate immutable header fields before accepting the configuration.
        FrameHeader(protocol_version, TELEMETRY_TYPE, node_id, zone_id,
                    key_version, self.boot_id, 0, 0, 0, 0, bytes(16))

    def emit(self, message_type: int, measurement: SensorMeasurement,
             *, nonce: bytes | None = None) -> Frame:
        if message_type not in (TELEMETRY_TYPE, CHALLENGE_RESPONSE_TYPE):
            raise ValueError("invalid EACLD message type")
        selected_nonce = secrets.token_bytes(16) if nonce is None else nonce
        with self._lock:
            if self._sequence == (1 << 64) - 1:
                raise OverflowError("node sequence number exhausted")
            next_sequence = self._sequence + 1
            uptime_ms = (time.monotonic_ns() - self._start_ns) // 1_000_000
            header = FrameHeader(
                version=self.protocol_version, type=message_type,
                node_id=self.node_id, zone_id=self.zone_id,
                key_version=self.key_version, boot_id=self.boot_id,
                sequence_no=next_sequence, uptime_ms=uptime_ms,
                temperature_mC=measurement.temperature_mC,
                humidity_mPct=measurement.humidity_mPct, nonce=selected_nonce,
            )
            frame = sign_frame(header, self._key)
            self._sequence = next_sequence
            return frame
