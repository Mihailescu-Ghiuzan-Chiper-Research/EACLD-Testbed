"""Unit tests for restricted experiment evidence helpers."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "shared"))
from eacld_contracts.frame import FrameHeader, sign_frame

SOURCE = Path(__file__).resolve().parents[1] / "services/core/src/experiment_api.py"
spec = importlib.util.spec_from_file_location("eacld_experiment_api", SOURCE)
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)


class ExperimentEvidenceTests(unittest.TestCase):
    def test_valid_frame_and_no_private_ground_truth(self):
        frame = sign_frame(FrameHeader(
            version=1, type=1, node_id=1, zone_id=1, key_version=1,
            boot_id=b"a" * 16, sequence_no=3, uptime_ms=1000,
            temperature_mC=28912, humidity_mPct=50000, nonce=b"0" * 16,
        ), b"z" * 32).to_bytes()
        reading = SimpleNamespace(id=10, node_id=1, payload=frame,
                                  mac_status="valid", sequence_status="fresh")
        summary = api.summarize_reading(reading)
        self.assertEqual(summary["temperature_mC"], 28912)
        self.assertEqual(summary["telemetry_id"], 10)
        self.assertEqual(summary["frame_hex"], frame.hex())
        self.assertNotIn("cause", summary)
        result = api.summarize_diagnosis({
            "episode_id": "H1-001", "block_id": "B01", "node_id": 1,
            "recorded_at_utc": "2026-11-11T12:00:00Z",
            "reading": reading, "result": {"decision": "H1"},
        })
        self.assertEqual(result["telemetry"]["telemetry_id"], 10)
        self.assertNotIn("ground_truth", result)

    def test_invalid_frame_does_not_invent_measurement(self):
        reading = SimpleNamespace(id=2, node_id=1, payload=b"bad",
                                  mac_status="invalid", sequence_status="unavailable")
        self.assertIsNone(api.summarize_reading(reading)["temperature_mC"])

    def test_strict_query_validation(self):
        self.assertEqual(api.parse_node("/v1/telemetry/latest?node_id=1"), 1)
        self.assertEqual(api.parse_episode("/v1/diagnosis?episode_id=H1-001"), "H1-001")
        for value in ("01", "-1", "65536", "a", "", "1&node_id=2"):
            with self.assertRaises(ValueError):
                api.parse_node("/v1/telemetry/latest?node_id=" + value)
        with self.assertRaises(ValueError):
            api.parse_episode("/v1/diagnosis?episode_id=H1&extra=1")


if __name__ == "__main__":
    unittest.main()
