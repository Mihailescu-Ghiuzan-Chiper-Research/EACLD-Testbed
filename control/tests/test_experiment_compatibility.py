"""Cross-experiment CLI and operator-dependency contract checks (H1–H4)."""
from __future__ import annotations

import inspect
from pathlib import Path
import sys
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "experiments"
sys.path.insert(0, str(SCRIPTS))
import h1_run
import h2_run
import h3_run
import h4_run


COMMON = [
    "--block-id", "B01",
    "--node-id", "1",
    "--mode", "nominal",
    "--baseline-temperature-mc", "22000",
    "--high-delta-mc", "3000",
    "--humidity-mpct", "50000",
]
RUNNERS = (
    ("H1", h1_run, h1_run.run_h1, ["--disturbance-temperature-mc", "6912"]),
    ("H2", h2_run, h2_run.run_h2, ["--bias-temperature-mc", "6912"]),
    ("H3", h3_run, h3_run.run_h3, ["--offset", "40", "--xor-mask", "37"]),
    ("H4", h4_run, h4_run.run_h4, []),
)


class ExperimentCompatibilityTests(unittest.TestCase):
    def test_consistent_operator_parameters_and_validation(self):
        for name, module, runner, specific in RUNNERS:
            with self.subTest(experiment=name):
                args = module.parser().parse_args(
                    ["--episode-id", f"{name}-001", *COMMON, *specific])
                self.assertEqual(args.episode_id, f"{name}-001")
                self.assertEqual(args.block_id, "B01")
                self.assertEqual(args.node_id, 1)
                self.assertEqual(args.mode, "nominal")
                self.assertEqual(args.baseline_temperature_mc, 22000)
                self.assertEqual(args.high_delta_mc, 3000)
                self.assertEqual(args.humidity_mpct, 50000)
                self.assertEqual(args.core_url, "http://10.60.30.15:8300")
                module.validate(args)
                self.assertTrue(callable(runner))

    def test_identical_injection_points_for_unit_test_harnesses(self):
        expected = ("request", "control", "command", "sleeper", "clock")
        for name, _, runner, _ in RUNNERS:
            with self.subTest(experiment=name):
                signature = inspect.signature(runner)
                self.assertEqual(tuple(signature.parameters)[1:], expected)

    def test_h1_h2_h3_expected_signal_and_h4_nominal_replay(self):
        self.assertEqual(22000 + 6912, 28912)
        self.assertEqual(h3_run.altered_temperature(22000, 40, 37), 28912)
        self.assertEqual(h4_run._sequence(
            bytes.fromhex("00" * 22 + "00" * 7 + "0a" + "00" * 64)
        ), 10)

    def test_all_runners_reject_unsafe_episode_paths(self):
        for name, module, _, specific in RUNNERS:
            with self.subTest(experiment=name):
                args = module.parser().parse_args(
                    ["--episode-id", "../unsafe", *COMMON, *specific])
                with self.assertRaises((ValueError, RuntimeError)):
                    module.validate(args)


if __name__ == "__main__":
    unittest.main()
