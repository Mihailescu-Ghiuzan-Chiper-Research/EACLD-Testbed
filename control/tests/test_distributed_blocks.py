"""Reproducibility, paired design, remote fault setup and cleanup."""
from __future__ import annotations

from collections import Counter, defaultdict
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments"))
from distributed_blocks import (
    DEFAULT_SEEDS, HYPOTHESES, POLICIES, ProtocolError,
    make_plan, profile_of, run_episode,
)
from distributed_report import _decision_loss, _report


class PlanTests(unittest.TestCase):
    def test_s3_v4_counts_matching_and_determinism(self):
        plan = make_plan(DEFAULT_SEEDS)
        self.assertEqual(len(plan), 1600)
        self.assertEqual(make_plan(DEFAULT_SEEDS), plan)
        self.assertEqual(Counter(x.mode for x in plan),
                         {"nominal": 800, "degraded": 800})
        blocks = defaultdict(list)
        for case in plan:
            blocks[case.block].append(case)
            self.assertLessEqual(0x5500, case.baseline)
            self.assertLessEqual(case.baseline, 0x55FF)
            self.assertEqual(case.episode_id, f"{case.block}-{case.cause}")
        self.assertEqual(len(blocks), 400)
        for cases in blocks.values():
            self.assertEqual(set(x.cause for x in cases), set(HYPOTHESES))
            self.assertEqual(len(set((x.baseline, x.reference_down,
                                      x.node_down, x.delay_ms) for x in cases)), 1)
            for x in cases:
                profile = profile_of(x)
                self.assertEqual(profile["nodes"]["latency_ms"],
                                 120 if x.mode == "degraded" else 0)
                if x.mode == "nominal":
                    self.assertFalse(profile["reference"]["unavailable"])
                    self.assertFalse(profile["nodes"]["unavailable"])

    def test_minimum_required_and_unique_seeds(self):
        for seeds, blocks in ((DEFAULT_SEEDS[:9], 20),
                              (DEFAULT_SEEDS[:-1] + (DEFAULT_SEEDS[0],), 20),
                              (DEFAULT_SEEDS, 19)):
            with self.subTest(seeds=seeds, blocks=blocks), self.assertRaises(ProtocolError):
                make_plan(seeds, blocks)

    def test_loss_matches_e0_matrix_cases(self):
        cfg = {"decision": {
            "unresolved_loss": .46, "class_correct_loss": .22,
            "class_incorrect_loss": 1.65, "wrong_label_loss": 1.0,
            "missed_attack_loss": 2.4, "false_attack_loss": 1.5,
        }}
        examples = [("H1", "H1", 0.0), ("H2", "H1", 1.0),
                    ("H3", "H1", 1.5), ("H1", "H3", 2.4),
                    ("H1_or_H2", "H2", .22),
                    ("H1_or_H2", "H4", 1.65),
                    ("unresolved", "H4", .46)]
        for decision, truth, expected in examples:
            self.assertEqual(_decision_loss(decision, truth, cfg), expected)

    def test_paired_stratified_reporting(self):
        records = []
        for mode in ("nominal", "degraded"):
            for policy in POLICIES:
                for cause in HYPOTHESES:
                    records.append({
                        "mode": mode, "block_id": mode + "-B01",
                        "policy": policy, "cause": cause,
                        "decision": cause, "correct_exact": 1, "exact_coverage": 1,
                        "realized_loss": 0, "loss_plus_cost": .05,
                        "probe_count": 1, "cost_observed": .05,
                        "latency_ms": 10, "bytes": 94, "brier": 0,
                    })
        report = _report(records, 8, 20261104)
        self.assertEqual(report["results"]["nominal"]["blocks"], 1)
        for mode in ("nominal", "degraded"):
            self.assertEqual(report["results"][mode]["policies"]["B6_EACLD"]
                             ["point"]["macro_f1"], 1.0)
            self.assertEqual(report["results"][mode]["policies"]
                             ["B0_passive_P0"]["difference_from_B6_confidence_95"]
                             ["macro_f1"], [0.0, 0.0])


class ExecutionTests(unittest.TestCase):
    def test_h2_comparison_is_recorded_during_active_intervention(self):
        case = next(e for e in make_plan(DEFAULT_SEEDS)
                    if e.mode == "nominal" and e.cause == "H2")
        states = {service: {"unavailable": False, "latency_ms": 0, "revision": 0}
                  for service in ("nodes", "reference")}

        class Adapter:
            def __init__(self, root):
                self.root = root

            def request(self, method, url, body=None):
                if "/v1/probe-condition" in url:
                    service = "nodes" if "10.60.30.12" in url else "reference"
                    if method == "POST":
                        states[service].update(body)
                        states[service]["revision"] += 1
                    return dict(states[service])
                if url.endswith("/v1/evaluate"):
                    raise AssertionError("H2 evaluation is owned by the scenario runner")
                raise AssertionError(url)

            def executor(self, argv, **_):
                assert "--evaluation-random-seed" in argv
                assert argv[argv.index("--evaluation-random-seed") + 1] == str(case.seed)
                out = self.root / case.episode_id
                out.mkdir()
                (out / "manifest.json").write_text(json.dumps({
                    "status": "recorded", "episode_id": case.episode_id,
                    "telemetry_id": 13,
                    "policy_comparison_stage": "active_sensor_bias",
                }))
                (out / "core_diagnosis_record.json").write_text(json.dumps({
                    "episode_id": case.episode_id,
                    "telemetry": {"telemetry_id": 13},
                }))
                (out / "policy_evaluation.json").write_text(json.dumps({
                    "episode_id": case.episode_id, "block_id": case.block,
                    "mode": case.mode, "telemetry_id": 13,
                    "random_seed": case.seed,
                    "policies": [{"policy": p} for p in POLICIES],
                }))
                (out / "comparison_bias_state.json").write_text(json.dumps({
                    "before": {"bias_temperature_mC": 6912, "bias_humidity_mPct": 0},
                    "after": {"bias_temperature_mC": 6912, "bias_humidity_mPct": 0},
                }))
                (out / "bias_restoration.json").write_text(json.dumps({
                    "bias_temperature_mC": 0, "bias_humidity_mPct": 0,
                }))
                return SimpleNamespace(returncode=0, stderr="")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            adapter = Adapter(root)
            run_episode(case, output_root=root, timeout=10, node_id=1,
                        request=adapter.request, executor=adapter.executor)
            self.assertTrue(all(s["unavailable"] is False and s["latency_ms"] == 0
                                for s in states.values()))
            manifest = json.loads((root / case.episode_id / "manifest.json").read_text())
            self.assertEqual(manifest["policy_comparison_stage"], "active_sensor_bias")
            self.assertTrue(manifest["probe_restored"])
            run_episode(case, output_root=root, timeout=10, node_id=1,
                        request=adapter.request, executor=adapter.executor)

    def test_mode_is_real_and_restored_before_acceptance(self):
        case = next(e for e in make_plan(DEFAULT_SEEDS) if
                    e.mode == "degraded" and e.cause == "H3")
        states = {service: {"unavailable": False, "latency_ms": 0, "revision": 0}
                  for service in ("nodes", "reference")}

        class Adapter:
            def __init__(self, root):
                self.root = root
                self.assertions = 0

            def request(self, method, url, body=None):
                if "/v1/probe-condition" in url:
                    service = "nodes" if "10.60.30.12" in url else "reference"
                    if method == "POST":
                        states[service].update(body)
                        states[service]["revision"] += 1
                    return dict(states[service])
                if url.endswith("/v1/evaluate"):
                    self.assertions += 1
                    assert body["mode"] == "degraded"
                    assert states["nodes"]["latency_ms"] > 0
                    return {"episode_id": case.episode_id, "block_id": case.block,
                            "mode": case.mode, "telemetry_id": 13,
                            "random_seed": case.seed,
                            "policies": [{"policy": p} for p in POLICIES]}
                raise AssertionError(url)

            def executor(self, argv, **_):
                assert "--mode" in argv
                assert argv[argv.index("--mode") + 1] == "degraded"
                out = self.root / case.episode_id
                out.mkdir()
                (out / "manifest.json").write_text(json.dumps({
                    "status": "recorded", "episode_id": case.episode_id,
                    "telemetry_id": 13}))
                (out / "core_diagnosis_record.json").write_text(json.dumps({
                    "episode_id": case.episode_id,
                    "telemetry": {"telemetry_id": 13}}))
                return SimpleNamespace(returncode=0, stderr="")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            adapter = Adapter(root)
            run_episode(case, output_root=root, timeout=10, node_id=1,
                        request=adapter.request, executor=adapter.executor)
            self.assertEqual(adapter.assertions, 1)
            self.assertTrue(all(s["unavailable"] is False and s["latency_ms"] == 0
                                for s in states.values()))
            manifest = json.loads((root / case.episode_id / "manifest.json").read_text())
            self.assertTrue(manifest["probe_restored"])
            run_episode(case, output_root=root, timeout=10, node_id=1,
                        request=adapter.request, executor=adapter.executor)
            self.assertEqual(adapter.assertions, 1)


if __name__ == "__main__":
    unittest.main()
