"""H3 operator tests with simulated G2, VM-CORE and VM-CONTROL responses."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1] / "experiments"
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("h3_operator", ROOT / "h3_run.py")
h3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h3)


class Clock:
    def __init__(self):
        self.t = 0

    def now(self):
        self.t += 0.03
        return self.t


class Scenario:
    def __init__(self, *, no_frame=False, bad_event=False):
        self.reset = False
        self.armed = False
        self.cleared = False
        self.no_frame = no_frame
        self.bad_event = bad_event
        self.posted = []
        self.requests = []
        header = bytearray(62)
        header[1] = 1
        header[2:4] = (1).to_bytes(2, "big")
        header[38:42] = (22000).to_bytes(4, "big", signed=True)
        header[42:46] = (50000).to_bytes(4, "big")
        self.original = bytes(header) + b"z" * 32
        frame = bytearray(self.original)
        frame[40] ^= 37
        self.modified = bytes(frame)
        self.event = {
            "event_id": 1, "offset": 40 if not bad_event else 39,
            "xor_mask": 37,
            "original_frame_hex": self.original.hex(),
            "mutated_frame_hex": self.modified.hex(),
        }

    def request(self, method, url, body=None):
        self.requests.append((method, url, body))
        if url.endswith("/healthz"):
            return {"status": "ok"}
        if "/v1/nodes/bias?" in url:
            return {
                "node_id": 1, "bias_temperature_mC": 0,
                "bias_humidity_mPct": 0, "revision": 0,
            }
        if "/v1/g2/intervention/status?" in url:
            return {
                "status": "ok", "node_id": 1,
                "pending_action": None,
                "last_mutation": self.event if self.armed else None,
            }
        if url.endswith("/v1/g2/intervention"):
            if body["action"] == "mutate":
                self.armed = True
                return {"status": "ok"}
            if body["action"] == "clear":
                self.cleared = True
                self.armed = False
                return {"status": "ok"}
            raise AssertionError(body)
        if "/v1/telemetry/latest?" in url:
            return {
                "telemetry_id": 11 if self.reset else 10,
                "node_id": 1, "temperature_mC": 22000,
                "humidity_mPct": 50000,
                "mac_status": "valid", "sequence_status": "fresh",
            }
        if "/v1/telemetry/match?" in url:
            params = parse_qs(urlsplit(url).query)
            self.posted.append(("match", params))
            self.expect(params["after_id"], ["11"])
            self.expect(params["frame_hex"], [self.modified.hex()])
            if self.no_frame:
                raise h3.ApiError(404, "no matching telemetry")
            return {
                "telemetry_id": 12, "node_id": 1,
                "frame_hex": self.modified.hex(),
                "temperature_mC": 28912, "humidity_mPct": 50000,
                "mac_status": "invalid", "sequence_status": "unavailable",
            }
        if url.endswith("/v1/diagnose"):
            self.posted.append(("diagnose", body))
            self.expect(body["selected_telemetry_id"], 12)
            self.assert_no_private_fields(body)
            return {"episode_id": "H3-001", "decision": "unresolved"}
        if "/v1/diagnosis?" in url:
            return {
                "episode_id": "H3-001", "block_id": "B01", "node_id": 1,
                "telemetry": {
                    "telemetry_id": 12, "frame_hex": self.modified.hex(),
                    "mac_status": "invalid", "sequence_status": "unavailable",
                },
                "result": {"decision": "unresolved"},
            }
        raise AssertionError((method, url, body))

    @staticmethod
    def expect(actual, expected):
        if actual != expected:
            raise AssertionError(f"expected {expected}, got {actual}")

    @staticmethod
    def assert_no_private_fields(body):
        for key in ("cause", "intervention", "xor_mask", "offset"):
            if key in body:
                raise AssertionError(f"private field leaked to VM-CORE: {key}")

    def control(self, args, _cwd):
        if args[0] == "agent-health":
            return {"status": "ok"}
        if args[0] == "reset":
            self.reset = True
            return {"step_id": 0, "temperature_mC": 22000, "humidity_mPct": 50000}
        if args[0] == "state":
            return {"step_id": 0, "temperature_mC": 22000, "humidity_mPct": 50000}
        if args[0] == "private":
            return {"interventions": [], "private_steps": [],
                    "plant": {"step_id": 0}}
        raise AssertionError(args)


class H3RunnerTests(unittest.TestCase):
    def args(self, output):
        return SimpleNamespace(
            episode_id="H3-001", block_id="B01", node_id=1, mode="nominal",
            baseline_temperature_mc=22000, high_delta_mc=3000,
            humidity_mpct=50000, offset=40, xor_mask=37,
            timeout_seconds=2, poll_seconds=0.01,
            control_dir=Path(output), output_root=Path(output),
            core_url="http://10.60.30.15:8300",
            edge_url="http://10.60.30.14:8300",
            nodes_control_url="http://10.60.30.12:8300",
        )

    def run_scenario(self, args, scenario):
        clock = Clock()
        return h3.run_h3(
            args, request=scenario.request, control=scenario.control,
            command=lambda *_: "f" * 40,
            sleeper=lambda _: None, clock=clock.now,
        )

    def test_complete_record_with_original_and_modified_frames(self):
        with tempfile.TemporaryDirectory() as tmp:
            scenario = Scenario()
            result = self.run_scenario(self.args(tmp), scenario)
            self.assertEqual(result["telemetry_id"], 12)
            self.assertTrue(scenario.cleared)
            self.assertIn(("diagnose", {
                "node_id": 1, "mode": "nominal", "episode_id": "H3-001",
                "block_id": "B01", "baseline_temperature_mC": 22000,
                "high_delta_mC": 3000, "selected_telemetry_id": 12,
            }), scenario.posted)
            folder = Path(tmp) / "H3-001"
            for filename in (
                "parameters.json", "git_commit.txt", "initial_bias.json",
                "edge_initial_status.json", "baseline_telemetry.json",
                "edge_mutation_evidence.json", "core_mutated_telemetry.json",
                "diagnosis_response.json", "core_diagnosis_record.json",
                "private_ground_truth.json", "edge_cleanup.json", "manifest.json",
            ):
                self.assertTrue((folder / filename).is_file(), filename)
            evidence = json.loads((folder / "edge_mutation_evidence.json").read_text())
            self.assertTrue(evidence["mac_unchanged"])
            self.assertEqual(evidence["mutated_temperature_mC"], 28912)
            self.assertFalse((folder / "failure.json").exists())

    def test_no_corrupt_frame_times_out_and_clears_intervention(self):
        with tempfile.TemporaryDirectory() as tmp:
            scenario = Scenario(no_frame=True)
            with self.assertRaises(h3.ProtocolError):
                self.run_scenario(self.args(tmp), scenario)
            self.assertTrue(scenario.cleared)
            self.assertTrue((Path(tmp) / "H3-001" / "failure.json").exists())
            self.assertFalse((Path(tmp) / "H3-001" / "manifest.json").exists())

    def test_wrong_mutation_event_is_rejected_and_cleared(self):
        with tempfile.TemporaryDirectory() as tmp:
            scenario = Scenario(bad_event=True)
            with self.assertRaises(h3.ProtocolError):
                self.run_scenario(self.args(tmp), scenario)
            self.assertTrue(scenario.cleared)
            self.assertFalse(any(op == "diagnose" for op, _ in scenario.posted))

    def test_invalid_mutation_not_armed(self):
        with tempfile.TemporaryDirectory() as tmp:
            params = self.args(tmp)
            params.offset = 37
            with self.assertRaises(h3.ProtocolError):
                h3.run_h3(params)
            self.assertFalse((Path(tmp) / "H3-001").exists())
            params.offset = 40
            params.xor_mask = 1
            with self.assertRaises(h3.ProtocolError):
                h3.run_h3(params)
            self.assertFalse((Path(tmp) / "H3-001").exists())


if __name__ == "__main__":
    unittest.main()
