"""H3 VM-CORE verification: exact mutated-frame lookup and B6 input selection."""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace
import unittest
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

from eacld_contracts.frame import FrameHeader, sign_frame
from src.core import _valid_request, create_control_server
from src.experiment_api import parse_frame_match
from src.runner import DiagnosisRunner


class FakeEngine:
    def run(self, policy, live, _rng):
        mac, _ = live.execute("message_verify")
        freshness, _ = live.execute("sequence_check")
        return {
            "episode_id": live.public["episode_id"],
            "block_id": live.public["block_id"],
            "policy": policy,
            "mac_observation": mac,
            "sequence_observation": freshness,
        }


class FakeStore:
    def __init__(self):
        header = FrameHeader(
            version=1, type=1, node_id=1, zone_id=1, key_version=1,
            boot_id=b"b" * 16, sequence_no=21, uptime_ms=1000,
            temperature_mC=22000, humidity_mPct=50000, nonce=b"n" * 16,
        )
        self.original = sign_frame(header, b"k" * 32).to_bytes()
        changed = bytearray(self.original)
        changed[40] ^= 37
        self.modified = bytes(changed)
        self.target = SimpleNamespace(
            id=51, node_id=1, payload=self.modified,
            mac_status="invalid", sequence_status="unavailable")
        self.latest_reading = SimpleNamespace(
            id=99, node_id=1, payload=self.original,
            mac_status="valid", sequence_status="fresh")
        self.saved = None

    def latest(self, node_id):
        return self.latest_reading if node_id == 1 else None

    def matching_frame(self, node_id, after_id, frame):
        if node_id == 1 and after_id < self.target.id and frame == self.modified:
            return self.target
        return None

    def by_id(self, node_id, frame_id):
        if node_id == 1 and frame_id == self.target.id:
            return self.target
        return None

    def record_diagnosis(self, result, *, node_id, telemetry_id):
        self.saved = (result, node_id, telemetry_id)

    def get_diagnosis(self, episode_id):
        if not self.saved or self.saved[0]["episode_id"] != episode_id:
            return None
        return {
            "episode_id": episode_id, "block_id": self.saved[0]["block_id"],
            "node_id": self.saved[1], "recorded_at_utc": "2026-11-11T12:00:00Z",
            "result": self.saved[0], "reading": self.target,
        }


class H3CoreTests(unittest.TestCase):
    def test_match_query_accepts_exact_signed_frame(self):
        store = FakeStore()
        url = "/v1/telemetry/match?" + urlencode({
            "node_id": 1, "after_id": 30, "frame_hex": store.modified.hex()
        })
        self.assertEqual(parse_frame_match(url), (1, 30, store.modified))
        for bad_url in (
            url.replace("node_id=1", "node_id=01"),
            url.replace("after_id=30", "after_id=-1"),
            url.replace("after_id=30", "after_id=51"),
            url.replace("frame_hex=" + store.modified.hex(), "frame_hex=zz"),
            url + "&extra=1",
        ):
            if "after_id=51" in bad_url:
                # A syntactically valid watermark may simply produce no match.
                continue
            with self.subTest(bad_url=bad_url), self.assertRaises(ValueError):
                parse_frame_match(bad_url)

    def test_stored_mutation_selected_even_after_newer_valid_frame(self):
        store = FakeStore()
        config = {"modes": ["nominal"],
                  "actions": {
                      "message_verify": {"cost": 1},
                      "sequence_check": {"cost": 1},
                  }}
        engine = FakeEngine()
        runner = DiagnosisRunner(engine=engine, config=config,
                                 store=store, verifier=object())
        mqtt = SimpleNamespace(ready=threading.Event())
        mqtt.ready.set()
        server = create_control_server(runner, mqtt, host="127.0.0.1",
                                       port=0, allowed_source="127.0.0.1")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        opener = build_opener(ProxyHandler({}))
        try:
            match_url = base + "/v1/telemetry/match?" + urlencode({
                "node_id": 1, "after_id": 30, "frame_hex": store.modified.hex(),
            })
            with opener.open(match_url, timeout=2) as response:
                frame = json.load(response)
            self.assertEqual(frame["telemetry_id"], 51)
            self.assertEqual(frame["temperature_mC"], 28912)
            self.assertEqual(frame["mac_status"], "invalid")
            self.assertEqual(frame["sequence_status"], "unavailable")
            self.assertEqual(store.latest(1).id, 99)

            body = {
                "node_id": 1, "mode": "nominal", "episode_id": "H3-001",
                "block_id": "B01", "baseline_temperature_mC": 22000,
                "high_delta_mC": 3000, "selected_telemetry_id": frame["telemetry_id"],
            }
            with opener.open(Request(base + "/v1/diagnose",
                                     data=json.dumps(body).encode(), method="POST"),
                             timeout=2) as response:
                result = json.load(response)
            self.assertEqual(result["mac_observation"], "invalid")
            self.assertEqual(result["sequence_observation"], "unavailable")
            self.assertEqual(store.saved[2], 51)
            with opener.open(base + "/v1/diagnosis?episode_id=H3-001", timeout=2) as response:
                record = json.load(response)
            self.assertEqual(record["telemetry"]["telemetry_id"], 51)
            self.assertEqual(record["telemetry"]["frame_hex"], store.modified.hex())
            self.assertEqual(record["result"]["policy"], "B6_EACLD")

            with self.assertRaises(HTTPError) as err:
                opener.open(base + "/v1/telemetry/match?" + urlencode({
                    "node_id": 1, "after_id": 51, "frame_hex": store.modified.hex()
                }), timeout=2)
            self.assertEqual(err.exception.code, 404)

            bad = dict(body, selected_telemetry_id=999)
            with self.assertRaises(HTTPError) as err:
                opener.open(Request(base + "/v1/diagnose",
                                    data=json.dumps(bad).encode(), method="POST"),
                            timeout=2)
            self.assertEqual(err.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_request_does_not_allow_two_selection_modes(self):
        valid = {
            "node_id": 1, "mode": "nominal", "episode_id": "H3-001",
            "block_id": "B01", "baseline_temperature_mC": 22000,
            "high_delta_mC": 3000, "selected_telemetry_id": 5,
        }
        self.assertEqual(_valid_request(valid, ["nominal"]), valid)
        for variant in (
            dict(valid, expected_telemetry_id=5),
            dict(valid, selected_telemetry_id=0),
            dict(valid, selected_telemetry_id=True),
        ):
            with self.assertRaises(ValueError):
                _valid_request(variant, ["nominal"])


if __name__ == "__main__":
    unittest.main()
