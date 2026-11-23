"""Exercise VM-CORE's restricted observation and guarded diagnosis over HTTP."""
from __future__ import annotations

import json
import threading
import unittest
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, ProxyHandler, build_opener

from eacld_contracts.frame import FrameHeader, sign_frame
from src.core import _valid_request, create_control_server
from src.experiment_api import TelemetryChanged


class FakeStore:
    def __init__(self):
        self.reading = SimpleNamespace(
            id=51, node_id=1, mac_status="valid", sequence_status="fresh",
            payload=sign_frame(FrameHeader(
                version=1, type=1, node_id=1, zone_id=1, key_version=1,
                boot_id=b"a" * 16, sequence_no=55, uptime_ms=1000,
                temperature_mC=28912, humidity_mPct=50000, nonce=b"0" * 16,
            ), b"z" * 32).to_bytes(),
        )

    def latest(self, node_id):
        return self.reading if node_id == 1 else None

    def get_diagnosis(self, episode_id):
        if episode_id != "H1-001":
            return None
        return {
            "episode_id": episode_id, "block_id": "B01", "node_id": 1,
            "recorded_at_utc": "2026-11-11T12:00:00Z",
            "reading": self.reading, "result": {"decision": "H1"},
        }


class FakeRunner:
    def __init__(self):
        self.store = FakeStore()
        self.config = {"modes": ["nominal", "degraded"]}

    def diagnose(self, **params):
        if params.get("expected_telemetry_id") != self.store.reading.id:
            raise TelemetryChanged("telemetry changed before diagnosis")
        return {"episode_id": params["episode_id"], "decision": "H1"}


class OperatorApiTests(unittest.TestCase):
    def test_evidence_and_diagnosis_use_exact_message(self):
        runner = FakeRunner()
        mqtt = SimpleNamespace(ready=threading.Event())
        mqtt.ready.set()
        server = create_control_server(runner, mqtt, host="127.0.0.1",
                                       port=0, allowed_source="127.0.0.1")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        opener = build_opener(ProxyHandler({}))
        try:
            with opener.open(base + "/v1/telemetry/latest?node_id=1", timeout=2) as response:
                observation = json.load(response)
                self.assertEqual(observation["temperature_mC"], 28912)
                self.assertEqual(observation["telemetry_id"], 51)
            with opener.open(base + "/v1/diagnosis?episode_id=H1-001", timeout=2) as response:
                evidence = json.load(response)
                self.assertEqual(evidence["telemetry"]["telemetry_id"], 51)
            params = {
                "node_id": 1, "mode": "nominal", "episode_id": "H1-001",
                "block_id": "B01", "baseline_temperature_mC": 22000,
                "high_delta_mC": 3000, "expected_telemetry_id": 51,
            }
            with opener.open(Request(base + "/v1/diagnose",
                                     data=json.dumps(params).encode(), method="POST"),
                             timeout=2) as response:
                self.assertEqual(json.load(response)["decision"], "H1")
            params["expected_telemetry_id"] = 50
            with self.assertRaises(HTTPError) as error:
                opener.open(Request(base + "/v1/diagnose",
                                    data=json.dumps(params).encode(), method="POST"),
                            timeout=2)
            self.assertEqual(error.exception.code, 409)
            for endpoint in ("/v1/telemetry/latest?node_id=01",
                             "/v1/telemetry/latest?node_id=999",
                             "/v1/diagnosis?episode_id=unknown"):
                with self.assertRaises(HTTPError):
                    opener.open(base + endpoint, timeout=2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_optional_telemetry_pin_is_strictly_validated(self):
        payload = {"node_id": 1, "mode": "nominal", "episode_id": "H1-001",
                   "block_id": "B01", "baseline_temperature_mC": 22000,
                   "high_delta_mC": 3000}
        self.assertEqual(_valid_request(payload, ["nominal"]), payload)
        pinned = {**payload, "expected_telemetry_id": 1}
        self.assertEqual(_valid_request(pinned, ["nominal"]), pinned)
        for bad in (0, -1, True, "1"):
            with self.assertRaises(ValueError):
                _valid_request({**pinned, "expected_telemetry_id": bad}, ["nominal"])


if __name__ == "__main__":
    unittest.main()
