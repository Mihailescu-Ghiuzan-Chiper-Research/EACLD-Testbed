"""Authentication of source-signed frames with VM-CORE-only node keys."""
from __future__ import annotations

import json
from pathlib import Path

from eacld_contracts.frame import Frame, FRAME_SIZE, TELEMETRY_TYPE, verify_frame


def load_keyring(path: str | Path) -> dict[int, dict[int, bytes]]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("keyring must be a JSON object")
    result = {}
    for node, versions in raw.items():
        if not isinstance(versions, dict):
            raise ValueError("node key versions must be a JSON object")
        node_id = int(node)
        if not 0 <= node_id <= 65535 or str(node_id) != node:
            raise ValueError("invalid keyring node identifier")
        keys = {}
        for version, encoded in versions.items():
            key_version = int(version)
            if not 0 <= key_version <= 255 or str(key_version) != version:
                raise ValueError("invalid key version")
            if not isinstance(encoded, str):
                raise ValueError("key must be hexadecimal text")
            key = bytes.fromhex(encoded)
            if len(key) < 32:
                raise ValueError("node HMAC key must have at least 32 bytes")
            keys[key_version] = key
        result[node_id] = keys
    return result


class FrameVerifier:
    def __init__(self, keys: dict[int, dict[int, bytes]]):
        self._keys = keys

    def key(self, node_id: int, version: int) -> bytes | None:
        return self._keys.get(node_id, {}).get(version)

    def verify(self, node_id: int, payload: bytes, *, expected_type: int = TELEMETRY_TYPE) -> str:
        """Return valid, invalid or unavailable, never signing a received frame."""
        if not isinstance(payload, bytes) or len(payload) != FRAME_SIZE:
            return "invalid"
        try:
            frame = Frame.from_bytes(payload)
        except (TypeError, ValueError):
            return "invalid"
        if frame.header.node_id != node_id or frame.header.type != expected_type:
            return "invalid"
        key = self.key(node_id, frame.header.key_version)
        if key is None:
            return "unavailable"
        return "valid" if verify_frame(frame, key) else "invalid"
