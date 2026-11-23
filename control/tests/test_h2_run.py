"""H2 operator regression tests: sequencing, telemetry identity and cleanup."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "experiments"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("h2_operator", SCRIPTS / "h2_run.py")
h2 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h2)


class Scenario:
    def __init__(self, *, failed_diagnosis=False, mismatch_once=False,
                 failed_evaluation=False):
        self.reset = False
        self.bias = 0
        self.revision = 0
        self.history = []
        self.diagnosis_requests = []
        self.failed_diagnosis = failed_diagnosis
        self.failed_evaluation = failed_evaluation
        self.mismatch_once = mismatch_once
        self.used_id = None
        self.latest_id = 12

    def request(self, method, url, body=None):
        self.history.append((method, url, body))
        if url.endswith("/healthz"):
            return {"status": "ok"}
        if "/v1/nodes/bias?" in url and method == "GET":
            return {
                "node_id": 1, "bias_temperature_mC": self.bias,
                "bias_humidity_mPct": 0, "revision": self.revision,
            }
        if url.endswith("/v1/nodes/bias") and method == "POST":
            self.bias = body["bias_temperature_mC"]
            self.revision += 1
            return {
                "node_id": 1, "bias_temperature_mC": self.bias,
                "bias_humidity_mPct": 0, "revision": self.revision,
            }
        if "/v1/telemetry/latest?" in url:
            latest = 10 if not self.reset else 11 if not self.bias else self.latest_id
            return {
                "telemetry_id": latest, "node_id": 1,
                "temperature_mC": 22000 + self.bias,
                "humidity_mPct": 50000,
                "mac_status": "valid", "sequence_status": "fresh",
            }
        if url.endswith("/v1/diagnose"):
            self.diagnosis_requests.append(body)
            if self.failed_diagnosis:
                raise h2.ApiError(503, "core unavailable")
            if self.mismatch_once:
                self.mismatch_once = False
                self.latest_id = 13
                raise h2.ApiError(409, "telemetry changed before diagnosis")
            self.used_id = body["expected_telemetry_id"]
            return {"episode_id": "H2-001", "decision": "unresolved"}
        if url.endswith("/v1/evaluate"):
            if self.bias != 6912:
                raise AssertionError("H2 comparison requires active sensor bias")
            if self.failed_evaluation:
                raise h2.ApiError(503, "comparison unavailable")
            return {
                "episode_id": "H2-001", "block_id": "B01", "mode": "nominal",
                "telemetry_id": self.used_id, "random_seed": body["random_seed"],
                "policies": [{"policy": p} for p in (
                    "B0_passive_P0", "B1_passive_P1", "B2_exhaustive",
                    "B3_random", "B4_entropy_greedy",
                    "B5_naive_independence", "B6_EACLD",
                )],
            }
        if "/v1/diagnosis?" in url:
            return {
                "episode_id": "H2-001", "block_id": "B01", "node_id": 1,
                "telemetry": {
                    "telemetry_id": self.used_id, "temperature_mC": 28912,
                    "humidity_mPct": 50000, "mac_status": "valid",
                    "sequence_status": "fresh",
                },
                "result": {"decision": "unresolved"},
            }
        raise AssertionError((method, url))

    def control(self, argv, cwd):
        self.history.append(("control", argv))
        if argv[0] == "agent-health":
            return {"status": "ok"}
        if argv[0] == "reset":
            self.reset = True
            return {"step_id": 0, "temperature_mC": 22000, "humidity_mPct": 50000}
        if argv[0] == "state":
            return {"step_id": 0, "temperature_mC": 22000, "humidity_mPct": 50000}
        if argv[0] == "private":
            return {"plant": {"step_id": 0, "temperature_mC": 22000},
                    "interventions": [], "private_steps": []}
        raise AssertionError(argv)


class H2RunnerTests(unittest.TestCase):
    def args(self, root):
        return SimpleNamespace(
            episode_id="H2-001", block_id="B01", node_id=1,
            mode="nominal", baseline_temperature_mc=22000,
            high_delta_mc=3000, bias_temperature_mc=6912,
            humidity_mpct=50000, timeout_seconds=1, poll_seconds=0.01,
            output_root=Path(root), control_dir=Path(root),
            core_url="http://10.60.30.15:8300",
            nodes_control_url="http://10.60.30.12:8300",
            evaluation_random_seed=None,
        )

    def test_records_measured_bias_without_private_truth_leaking_to_core(self):
        with tempfile.TemporaryDirectory() as tmp:
            scenario = Scenario()
            result = h2.run_h2(
                self.args(tmp), request=scenario.request,
                control=scenario.control, command=lambda *_: "a" * 40,
                sleeper=lambda _: None,
            )
            self.assertEqual(result["telemetry_id"], 12)
            self.assertEqual(scenario.bias, 0)
            self.assertEqual(scenario.revision, 2)
            self.assertEqual(len(scenario.diagnosis_requests), 1)
            self.assertEqual(scenario.diagnosis_requests[0]["expected_telemetry_id"], 12)
            self.assertNotIn("cause", scenario.diagnosis_requests[0])
            self.assertNotIn("bias_temperature_mC", scenario.diagnosis_requests[0])
            folder = Path(tmp) / "H2-001"
            for path in (
                "manifest.json", "parameters.json", "git_commit.txt",
                "initial_bias.json", "baseline_telemetry.json", "bias_activation.json",
                "bias_restoration.json", "core_diagnosis_record.json",
                "private_ground_truth.json", "process_state.json",
            ):
                self.assertTrue((folder / path).exists(), path)
            truth = json.loads((folder / "private_ground_truth.json").read_text())
            self.assertEqual(truth["cause"], "H2")
            self.assertNotIn("failure.json", {p.name for p in folder.iterdir()})

    def test_core_conflict_retry_selects_the_newer_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            scenario = Scenario(mismatch_once=True)
            result = h2.run_h2(
                self.args(tmp), request=scenario.request,
                control=scenario.control, command=lambda *_: "a" * 40,
                sleeper=lambda _: None,
            )
            self.assertEqual(result["telemetry_id"], 13)
            self.assertEqual([x["expected_telemetry_id"]
                              for x in scenario.diagnosis_requests], [12, 13])
            self.assertEqual(scenario.bias, 0)

    def test_restores_bias_and_preserves_failure_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            scenario = Scenario(failed_diagnosis=True)
            with self.assertRaises(h2.ApiError):
                h2.run_h2(self.args(tmp), request=scenario.request,
                          control=scenario.control, command=lambda *_: "a" * 40,
                          sleeper=lambda _: None)
            self.assertEqual(scenario.bias, 0)
            self.assertTrue((Path(tmp) / "H2-001" / "failure.json").exists())
            self.assertFalse((Path(tmp) / "H2-001" / "manifest.json").exists())

    def test_comparison_uses_active_bias_and_selected_telemetry(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self.args(tmp)
            args.evaluation_random_seed = 20261104
            scenario = Scenario()
            summary = h2.run_h2(
                args, request=scenario.request, control=scenario.control,
                command=lambda *_: "a" * 40, sleeper=lambda _: None,
            )
            requests = scenario.history
            evaluated = next(i for i, item in enumerate(requests)
                             if item[0] == "POST" and item[1].endswith("/v1/evaluate"))
            restored = next(i for i, item in enumerate(requests)
                            if item[0] == "POST"
                            and item[1].endswith("/v1/nodes/bias")
                            and item[2]["bias_temperature_mC"] == 0)
            self.assertLess(evaluated, restored)
            body = requests[evaluated][2]
            self.assertEqual(body["selected_telemetry_id"], 12)
            self.assertEqual(body["random_seed"], args.evaluation_random_seed)
            self.assertNotIn("cause", body)
            self.assertEqual(summary["policy_comparison_stage"], "active_sensor_bias")
            folder = Path(tmp) / "H2-001"
            states = json.loads((folder / "comparison_bias_state.json").read_text())
            self.assertEqual(states["before"]["bias_temperature_mC"], 6912)
            self.assertEqual(states["after"]["bias_temperature_mC"], 6912)
            result = json.loads((folder / "policy_evaluation.json").read_text())
            self.assertEqual(result["telemetry_id"], 12)
            self.assertEqual(scenario.bias, 0)

    def test_comparison_failure_still_restores_sensor(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self.args(tmp)
            args.evaluation_random_seed = 20261104
            scenario = Scenario(failed_evaluation=True)
            with self.assertRaises(h2.ApiError):
                h2.run_h2(
                    args, request=scenario.request, control=scenario.control,
                    command=lambda *_: "a" * 40, sleeper=lambda _: None,
                )
            self.assertEqual(scenario.bias, 0)
            folder = Path(tmp) / "H2-001"
            self.assertTrue((folder / "failure.json").exists())
            self.assertFalse((folder / "manifest.json").exists())

    def test_invalid_parameters_cannot_change_process_or_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self.args(tmp)
            args.high_delta_mc = 8000
            with self.assertRaises(h2.ProtocolError):
                h2.run_h2(args)
            self.assertFalse((Path(tmp) / "H2-001").exists())


if __name__ == "__main__":
    unittest.main()
