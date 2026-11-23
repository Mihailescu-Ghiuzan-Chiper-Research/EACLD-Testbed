"""Operator-only observable evidence for reproducible EACLD episodes.

No private intervention schedule or ground-truth label is held by VM-CORE.
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from eacld_contracts.frame import Frame


class TelemetryChanged(RuntimeError):
    """The expected message was superseded before diagnosis began."""


def parse_one_argument(request_path: str, expected_path: str, field: str) -> str:
    parsed = urlsplit(request_path)
    if parsed.path != expected_path:
        raise ValueError("invalid observation endpoint")
    args = parse_qs(parsed.query, keep_blank_values=True)
    if set(args) != {field} or len(args[field]) != 1:
        raise ValueError("invalid observation arguments")
    value = args[field][0]
    if not value or len(value) > 128:
        raise ValueError("invalid observation argument length")
    return value


def parse_node(request_path: str) -> int:
    value = parse_one_argument(request_path, "/v1/telemetry/latest", "node_id")
    if not value.isascii() or not value.isdecimal() or str(int(value)) != value:
        raise ValueError("node_id must be canonical decimal")
    node_id = int(value)
    if not 0 <= node_id <= 65535:
        raise ValueError("invalid node_id")
    return node_id


def parse_episode(request_path: str) -> str:
    return parse_one_argument(request_path, "/v1/diagnosis", "episode_id")


def summarize_reading(reading) -> dict:
    output = {
        "telemetry_id": reading.id,
        "node_id": reading.node_id,
        "mac_status": reading.mac_status,
        "sequence_status": reading.sequence_status,
        "frame_hex": reading.payload.hex(),
        "temperature_mC": None,
        "humidity_mPct": None,
        "sequence_no": None,
        "boot_id_hex": None,
    }
    try:
        frame = Frame.from_bytes(reading.payload)
    except (TypeError, ValueError):
        return output
    output.update({
        "temperature_mC": frame.header.temperature_mC,
        "humidity_mPct": frame.header.humidity_mPct,
        "sequence_no": frame.header.sequence_no,
        "boot_id_hex": frame.header.boot_id.hex(),
    })
    return output


def summarize_diagnosis(record: dict) -> dict:
    return {
        "episode_id": record["episode_id"],
        "block_id": record["block_id"],
        "node_id": record["node_id"],
        "recorded_at_utc": record["recorded_at_utc"],
        "telemetry": summarize_reading(record["reading"]),
        "result": record["result"],
    }
