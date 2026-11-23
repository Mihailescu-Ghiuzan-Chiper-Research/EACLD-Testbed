"""Local contract tests for the VM-CONTROL H1 operator runner."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "experiments" / "h1_run.py"
spec = importlib.util.spec_from_file_location("eacld_h1_runner", SCRIPT)
h1 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h1)


class H1RunnerTests(unittest.TestCase):
    def parameters(self, root):
        return SimpleNamespace(
            episode_id="H1-001", block_id="B01", node_id=1, mode="nominal",
            baseline_temperature_mc=22000, high_delta_mc=3000,
            disturbance_temperature_mc=6912, humidity_mpct=50000,
            timeout_seconds=10, poll_seconds=0.01,
            output_root=Path(root), control_dir=Path(root),
            core_url="http://10.60.30.15:8300",
        )

    def test_success_and_private_truth_isolation(self):
        with tempfile.TemporaryDirectory() as root:
            args = self.parameters(root)
            phase = {"reset": False, "injected": False}
            posted = []

            def control(parts, cwd):
                if parts[0] == "agent-health":
                    return {"status": "ok"}
                if parts[0] == "reset":
                    phase["reset"] = True
                    return {"step_id": 0, "temperature_mC": 22000}
                if parts[0] == "inject":
                    return {"scheduled": True}
                if parts[0] == "step":
                    phase["injected"] = True
                    return {"step_id": 1, "temperature_mC": 28912,
                            "humidity_mPct": 50000}
                if parts[0] == "private":
                    return {"private_steps": [{"cause_labels": "H1", "step_id": 1}]}
                raise AssertionError(parts)

            def request(method, url, body=None):
                if url.endswith("/healthz"):
                    return {"status": "ok"}
                if "/v1/telemetry/latest" in url:
                    if phase["injected"]:
                        return {"telemetry_id": 12, "node_id": 1,
                                "temperature_mC": 28912,
                                "mac_status": "valid", "sequence_status": "fresh"}
                    if phase["reset"]:
                        return {"telemetry_id": 11, "node_id": 1,
                                "temperature_mC": 22000,
                                "mac_status": "valid", "sequence_status": "fresh"}
                    return {"telemetry_id": 10, "node_id": 1,
                            "temperature_mC": 22000,
                            "mac_status": "valid", "sequence_status": "fresh"}
                if url.endswith("/v1/diagnose"):
                    posted.append(body)
                    self.assertNotIn("cause", body)
                    self.assertNotIn("ground_truth", body)
                    self.assertEqual(body["expected_telemetry_id"], 12)
                    return {"episode_id": "H1-001", "decision": "H1"}
                if "/v1/diagnosis?" in url:
                    return {"episode_id": "H1-001", "telemetry": {
                        "telemetry_id": 12, "temperature_mC": 28912,
                        "mac_status": "valid", "sequence_status": "fresh"}}
                raise AssertionError((method, url))

            done = h1.run_h1(args, request=request, control=control,
                             command=lambda argv, cwd: "a" * 40,
                             sleeper=lambda _: None)
            self.assertEqual(done["telemetry_id"], 12)
            self.assertEqual(len(posted), 1)
            folder = Path(root) / "H1-001"
            for file in ("manifest.json", "parameters.json", "baseline_telemetry.json",
                         "private_ground_truth.json", "diagnosis_response.json",
                         "core_diagnosis_record.json"):
                self.assertTrue((folder / file).exists(), file)
            with self.assertRaises(FileExistsError):
                h1.run_h1(args)

    def test_invalid_threshold_stops_before_reset(self):
        with tempfile.TemporaryDirectory() as root:
            args = self.parameters(root)
            args.high_delta_mc = 8000
            with self.assertRaises(h1.ProtocolError):
                h1.run_h1(args)
            self.assertFalse((Path(root) / "H1-001").exists())

    def test_failure_is_saved(self):
        with tempfile.TemporaryDirectory() as root:
            args = self.parameters(root)
            with self.assertRaises(h1.ProtocolError):
                h1.run_h1(args, request=lambda *a, **kw: {"status": "unavailable"},
                          command=lambda argv, cwd: "a" * 40)
            self.assertTrue((Path(root) / "H1-001" / "failure.json").exists())


if __name__ == "__main__":
    unittest.main()
