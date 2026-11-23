"""Actual probe capture is shared fairly across all seven E0 policies."""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.comparison import POLICIES, RecordedEnvironment, evaluate_policies
from src.runner import DiagnosisRunner


class ComparisonTests(unittest.TestCase):
    def test_policies_use_one_real_observation_per_action(self):
        calls = []
        cfg = {"modes": ["nominal", "degraded"],
               "actions": {a: {"cost": .05} for a in
                           ("reference_read", "node_challenge", "message_verify",
                            "sequence_check")}}

        class Live:
            def __init__(self, **kwargs):
                self.public = {"episode_id": kwargs["episode_id"],
                               "block_id": kwargs["block_id"],
                               "mode": kwargs["mode"],
                               "passive": {"passive_mac": None, "passive_sequence": None}}
            def execute(self, action):
                calls.append(action)
                return ("unavailable" if action == "reference_read" else "valid",
                        {"cost": .05, "latency_ms": 10., "bytes": 94})

        class Engine:
            model = SimpleNamespace(priors={"nominal": .06, "degraded": .16},
                                    emissions={"nominal": {}, "degraded": {}})
            def run(self, policy, env, rng):
                sample = None
                if policy not in POLICIES[:2]:
                    sample, _ = env.execute("reference_read")
                return {"policy": policy, "decision": "H1",
                        "episode_id": env.public["episode_id"],
                        "observed": sample,
                        "random_value": int(rng.integers(0, 100000))}

        reading = SimpleNamespace(node_id=1, id=51,
                                  mac_status="valid", sequence_status="fresh")
        with patch("src.comparison.LiveEnvironment", Live):
            first = evaluate_policies(
                engine=Engine(), cfg=cfg, reading=reading, verifier=object(),
                episode_id="S1-degraded-B001-H1", block_id="S1-degraded-B001",
                mode="degraded", baseline_temperature_mC=22000,
                high_delta_mC=3000, random_seed=20261104)
            second = evaluate_policies(
                engine=Engine(), cfg=cfg, reading=reading, verifier=object(),
                episode_id="S1-degraded-B001-H1", block_id="S1-degraded-B001",
                mode="degraded", baseline_temperature_mC=22000,
                high_delta_mC=3000, random_seed=20261104)
        self.assertEqual(len(calls), 8)
        self.assertEqual(calls[:4], list(cfg["actions"]))
        self.assertEqual([x["policy"] for x in first["policies"]], list(POLICIES))
        self.assertEqual(first["policies"], second["policies"])
        self.assertEqual(first["telemetry_id"], 51)
        self.assertEqual(first["observations"]["reference_read"]["outcome"], "unavailable")
        self.assertEqual(first["passive_evidence"],
                         {"passive_mac": "valid", "passive_sequence": "fresh"})
        self.assertNotIn("cause", first)
        self.assertEqual(len(first["model_sha256"]), 64)
        self.assertEqual(len(first["config_sha256"]), 64)

    def test_environment_does_not_allow_action_reuse(self):
        env = RecordedEnvironment({"mode": "degraded"}, {
            "message_verify": {"outcome": "invalid",
                               "metadata": {"cost": .01}}})
        self.assertEqual(env.execute("message_verify")[0], "invalid")
        with self.assertRaises(ValueError):
            env.execute("message_verify")

    def test_comparison_is_pinned_to_original_record(self):
        reading = SimpleNamespace(id=51, node_id=1)
        class Store:
            def get_diagnosis(self, episode_id):
                return {"block_id": "B01", "node_id": 1, "reading": reading}
        runner = DiagnosisRunner(engine=object(), config={"modes": ["nominal"]},
                                 store=Store(), verifier=object())
        base = {
            "node_id": 1, "mode": "nominal", "episode_id": "H1-001",
            "block_id": "B01", "baseline_temperature_mC": 22000,
            "high_delta_mC": 3000, "random_seed": 100,
        }
        with self.assertRaises(ValueError):
            runner.evaluate_policies(**base, selected_telemetry_id=52)
        with patch("src.comparison.evaluate_policies", return_value={"policies": []}) as compute:
            result = runner.evaluate_policies(**base, selected_telemetry_id=51)
        self.assertEqual(result, {"policies": []})
        self.assertEqual(compute.call_args.kwargs["reading"].id, 51)


if __name__ == "__main__":
    unittest.main()
