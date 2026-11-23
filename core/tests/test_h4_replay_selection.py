"""VM-CORE H4: preserved HMAC, stale sequence, selected-frame inference."""
from __future__ import annotations

from types import SimpleNamespace
import unittest

from eacld_contracts.frame import FrameHeader, sign_frame, Frame, verify_frame
from src.runner import DiagnosisRunner
from src.sequence_registry import classify_and_advance


def signed(sequence: int) -> bytes:
    return sign_frame(FrameHeader(
        version=1, type=1, node_id=1, zone_id=1, key_version=1,
        boot_id=b"b" * 16, sequence_no=sequence, uptime_ms=sequence * 1000,
        temperature_mC=22000, humidity_mPct=50000, nonce=b"n" * 16,
    ), b"K" * 32).to_bytes()


class WatermarkCursor:
    def __init__(self):
        self.maximum = {}

    def execute(self, query, args):
        assert "sequence_watermarks" in query
        node, boot, sequence = args
        key = (node, boot)
        existing = self.maximum.get(key, -1)
        if sequence > existing:
            self.maximum[key] = sequence
            self.inserted = True
        else:
            self.inserted = False

    def fetchone(self):
        return (1,) if self.inserted else None


class Engine:
    def run(self, policy, env, _rng):
        mac, _ = env.execute("message_verify")
        sequence, _ = env.execute("sequence_check")
        return {
            "episode_id": env.public["episode_id"],
            "block_id": env.public["block_id"],
            "policy": policy, "mac_status": mac, "sequence_status": sequence,
        }


class Store:
    def __init__(self):
        self.frame = signed(20)
        self.replay = SimpleNamespace(
            id=51, node_id=1, payload=self.frame,
            mac_status="valid", sequence_status="stale",
        )
        self.newest = SimpleNamespace(
            id=60, node_id=1, payload=signed(22),
            mac_status="valid", sequence_status="fresh",
        )
        self.saved = None

    def by_id(self, node_id, telemetry_id):
        if node_id == 1 and telemetry_id == 51:
            return self.replay
        return None

    def latest(self, node_id):
        return self.newest if node_id == 1 else None

    def record_diagnosis(self, result, *, node_id, telemetry_id):
        self.saved = (result, node_id, telemetry_id)


class H4CoreTests(unittest.TestCase):
    def test_authentic_retransmission_does_not_advance_watermark(self):
        frame = signed(20)
        self.assertTrue(verify_frame(Frame.from_bytes(frame), b"K" * 32))
        cursor = WatermarkCursor()
        boot = b"b" * 16
        self.assertEqual(classify_and_advance(
            cursor, node_id=1, boot_id=boot, sequence_no=20
        ), "fresh")
        self.assertEqual(classify_and_advance(
            cursor, node_id=1, boot_id=boot, sequence_no=22
        ), "fresh")
        self.assertEqual(classify_and_advance(
            cursor, node_id=1, boot_id=boot, sequence_no=20
        ), "stale")
        self.assertEqual(cursor.maximum[(1, boot)], 22)

    def test_b6_receives_stale_frame_with_valid_mac_even_after_newer_data(self):
        store = Store()
        runner = DiagnosisRunner(
            engine=Engine(),
            config={
                "modes": ["nominal"],
                "actions": {
                    "message_verify": {"cost": 1},
                    "sequence_check": {"cost": 1},
                },
            },
            store=store,
            verifier=object(),
        )
        result = runner.diagnose(
            node_id=1, mode="nominal", episode_id="H4-001", block_id="B01",
            baseline_temperature_mC=22000, high_delta_mC=3000,
            selected_telemetry_id=51,
        )
        self.assertEqual(result["policy"], "B6_EACLD")
        self.assertEqual(result["mac_status"], "valid")
        self.assertEqual(result["sequence_status"], "stale")
        self.assertEqual(store.saved[2], 51)
        self.assertEqual(store.latest(1).id, 60)


if __name__ == "__main__":
    unittest.main()
