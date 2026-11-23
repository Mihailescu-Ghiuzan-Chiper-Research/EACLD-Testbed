"""H4 operator tests: event provenance, stale frame, diagnosis and cleanup."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "shared"))
from eacld_contracts.frame import FrameHeader, sign_frame

SCRIPTS = Path(__file__).resolve().parents[1] / "experiments"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("h4_operator", SCRIPTS / "h4_run.py")
h4 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h4)


def signed(sequence: int) -> bytes:
    header = FrameHeader(
        version=1, type=1, node_id=1, zone_id=1, key_version=1,
        boot_id=b"b" * 16, sequence_no=sequence, uptime_ms=sequence * 1000,
        temperature_mC=22000, humidity_mPct=50000, nonce=b"n" * 16,
    )
    return sign_frame(header, b"K" * 32).to_bytes()


class Clock:
    def __init__(self):
        self.value = 0

    def now(self):
        self.value += 0.02
        return self.value


class Scenario:
    def __init__(self, *, missing_replay=False, malformed_event=False,
                 diagnosis_error=False):
        self.reference = signed(10)
        self.trigger = signed(11)
        self.reset = False
        self.armed = False
        self.cleared = False
        self.posted = []
        self.visited = []
        self.diagnosis_error = diagnosis_error
        self.missing_replay = missing_replay
        self.event = {
            "event_id": 1,
            "reference_frame_hex": self.reference.hex(),
            "trigger_frame_hex": self.trigger.hex(),
            "replayed_frame_hex": (
                self.trigger.hex() if malformed_event else self.reference.hex()),
        }

    def request(self, method, url, body=None):
        self.visited.append((method, url, body))
        if url.endswith("/healthz"):
            return {"status": "ok"}
        if "/v1/nodes/bias?" in url:
            return {
                "node_id": 1, "bias_temperature_mC": 0,
                "bias_humidity_mPct": 0, "revision": 0,
            }
        if "/v1/g2/intervention/status?" in url:
            return {
                "status": "ok", "node_id": 1, "pending_action": None,
                "last_replay": self.event if self.armed else None,
                "last_mutation": None,
            }
        if url.endswith("/v1/g2/intervention"):
            if body["action"] == "replay":
                self.posted.append(("replay", body))
                self.armed = True
                return {"status": "ok"}
            if body["action"] == "clear":
                self.posted.append(("clear", body))
                self.armed = False
                self.cleared = True
                return {"status": "ok"}
            raise AssertionError(body)
        if "/v1/telemetry/latest?" in url:
            return {
                "telemetry_id": 11 if self.reset else 9,
                "node_id": 1, "frame_hex": self.reference.hex(),
                "temperature_mC": 22000, "humidity_mPct": 50000,
                "mac_status": "valid", "sequence_status": "fresh",
                "sequence_no": 10, "boot_id_hex": (b"b" * 16).hex(),
            }
        if "/v1/telemetry/match?" in url:
            query = parse_qs(urlsplit(url).query)
            assert query["node_id"] == ["1"], query
            if query["frame_hex"] == [self.trigger.hex()]:
                assert query["after_id"] == ["11"], query
                return {
                    "telemetry_id": 12, "node_id": 1,
                    "frame_hex": self.trigger.hex(),
                    "temperature_mC": 22000, "humidity_mPct": 50000,
                    "mac_status": "valid", "sequence_status": "fresh",
                    "sequence_no": 11, "boot_id_hex": (b"b" * 16).hex(),
                }
            if query["frame_hex"] == [self.reference.hex()]:
                assert query["after_id"] == ["12"], query
                if self.missing_replay:
                    raise h4.ApiError(404, "replay not yet ingested")
                return {
                    "telemetry_id": 13, "node_id": 1,
                    "frame_hex": self.reference.hex(),
                    "temperature_mC": 22000, "humidity_mPct": 50000,
                    "mac_status": "valid", "sequence_status": "stale",
                    "sequence_no": 10, "boot_id_hex": (b"b" * 16).hex(),
                }
            raise AssertionError(query)
        if url.endswith("/v1/diagnose"):
            if self.diagnosis_error:
                raise h4.ApiError(503, "B6 unavailable")
            self.posted.append(("diagnose", body))
            return {"episode_id": "H4-001", "decision": "unresolved"}
        if "/v1/diagnosis?" in url:
            return {
                "episode_id": "H4-001", "block_id": "B01", "node_id": 1,
                "telemetry": {
                    "telemetry_id": 13, "frame_hex": self.reference.hex(),
                    "mac_status": "valid", "sequence_status": "stale",
                },
                "result": {"decision": "unresolved"},
            }
        raise AssertionError((method, url, body))

    def control(self, argv, _cwd):
        if argv[0] == "agent-health":
            return {"status": "ok"}
        if argv[0] == "reset":
            self.reset = True
            return {"step_id": 0, "temperature_mC": 22000, "humidity_mPct": 50000}
        if argv[0] == "state":
            return {"step_id": 0, "temperature_mC": 22000, "humidity_mPct": 50000}
        if argv[0] == "private":
            return {"interventions": [], "private_steps": [],
                    "plant": {"step_id": 0}}
        raise AssertionError(argv)


class H4RunnerTests(unittest.TestCase):
    def args(self, root):
        return SimpleNamespace(
            episode_id="H4-001", block_id="B01", node_id=1,
            mode="nominal", baseline_temperature_mc=22000,
            high_delta_mc=3000, humidity_mpct=50000,
            timeout_seconds=2, poll_seconds=0.01,
            control_dir=Path(root), output_root=Path(root),
            core_url="http://10.60.30.15:8300",
            edge_url="http://10.60.30.14:8300",
            nodes_control_url="http://10.60.30.12:8300",
        )

    def run_scenario(self, args, scenario):
        clock = Clock()
        return h4.run_h4(
            args, request=scenario.request, control=scenario.control,
            command=lambda *_: "f" * 40, sleeper=lambda _: None,
            clock=clock.now,
        )

    def test_valid_replay_marked_stale_and_diagnosed_by_exact_record(self):
        with tempfile.TemporaryDirectory() as folder:
            scenario = Scenario()
            result = self.run_scenario(self.args(folder), scenario)
            self.assertEqual(result["telemetry_id"], 13)
            self.assertTrue(scenario.cleared)
            self.assertIn(("replay", {
                "action": "replay", "node_id": 1,
                "frame_hex": scenario.reference.hex(),
            }), scenario.posted)
            self.assertIn(("diagnose", {
                "node_id": 1, "mode": "nominal",
                "episode_id": "H4-001", "block_id": "B01",
                "baseline_temperature_mC": 22000,
                "high_delta_mC": 3000, "selected_telemetry_id": 13,
            }), scenario.posted)
            root = Path(folder) / "H4-001"
            for filename in (
                "parameters.json", "git_commit.txt", "initial_bias.json",
                "edge_initial_status.json", "reference_telemetry.json",
                "edge_replay_evidence.json", "core_trigger_telemetry.json",
                "core_replayed_telemetry.json", "diagnosis_response.json",
                "core_diagnosis_record.json", "private_ground_truth.json",
                "edge_cleanup.json", "manifest.json",
            ):
                self.assertTrue((root / filename).is_file(), filename)
            evidence = json.loads((root / "edge_replay_evidence.json").read_text())
            self.assertEqual(evidence["reference_sequence_no"], 10)
            self.assertEqual(evidence["trigger_sequence_no"], 11)
            self.assertTrue(evidence["frame_bytes_preserved"])
            self.assertEqual(json.loads((root / "private_ground_truth.json").read_text())["cause"], "H4")
            self.assertNotIn("cause", scenario.posted[1][1])
            self.assertFalse((root / "failure.json").exists())

    def test_missing_stale_frame_reports_failure_and_clears_g2(self):
        with tempfile.TemporaryDirectory() as folder:
            scenario = Scenario(missing_replay=True)
            with self.assertRaises(h4.ProtocolError):
                self.run_scenario(self.args(folder), scenario)
            self.assertTrue(scenario.cleared)
            self.assertTrue((Path(folder) / "H4-001" / "failure.json").is_file())
            self.assertFalse((Path(folder) / "H4-001" / "manifest.json").exists())

    def test_invalid_gateway_evidence_aborts_diagnosis_and_cleans(self):
        with tempfile.TemporaryDirectory() as folder:
            scenario = Scenario(malformed_event=True)
            with self.assertRaises(h4.ProtocolError):
                self.run_scenario(self.args(folder), scenario)
            self.assertTrue(scenario.cleared)
            self.assertFalse(any(op == "diagnose" for op, _ in scenario.posted))

    def test_diagnosis_failure_leaves_evidence_and_clears(self):
        with tempfile.TemporaryDirectory() as folder:
            scenario = Scenario(diagnosis_error=True)
            with self.assertRaises(h4.ApiError):
                self.run_scenario(self.args(folder), scenario)
            self.assertTrue(scenario.cleared)
            self.assertTrue((Path(folder) / "H4-001" / "failure.json").is_file())
            self.assertTrue((Path(folder) / "H4-001" / "core_replayed_telemetry.json").is_file())

    def test_parameter_validation_prevents_any_remote_action(self):
        with tempfile.TemporaryDirectory() as folder:
            args = self.args(folder)
            args.high_delta_mc = 0
            with self.assertRaises(h4.ProtocolError):
                h4.run_h4(args)
            self.assertFalse((Path(folder) / "H4-001").exists())


if __name__ == "__main__":
    unittest.main()
