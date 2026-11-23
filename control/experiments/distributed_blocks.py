"""Run S3 V4 matched H1-H4 distributed blocks on the existing five-VM testbed.

Run on VM-CONTROL using the four established scenario runners. Degraded mode
changes actual remote NET-PROBE HTTP response time/availability; it is not a
label-only change in the Bayesian prior. No synthetic probe evidence is used.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import random
import subprocess
import sys

from h1_run import ProtocolError, request_json, save_json

HYPOTHESES = ("H1", "H2", "H3", "H4")
POLICIES = (
    "B0_passive_P0", "B1_passive_P1", "B2_exhaustive", "B3_random",
    "B4_entropy_greedy", "B5_naive_independence", "B6_EACLD",
)
H2_BIAS_MC = 6912
DEFAULT_SEEDS = tuple(range(20261104, 20261114))
NODE_URL = "http://10.60.30.12:8300"
REFERENCE_URL = "http://10.60.30.13:8300"
CORE_URL = "http://10.60.30.15:8300"
ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Episode:
    seed: int
    block: str
    mode: str
    cause: str
    baseline: int
    reference_down: bool
    node_down: bool
    delay_ms: int

    @property
    def episode_id(self) -> str:
        return f"{self.block}-{self.cause}"


def make_plan(seeds: tuple[int, ...], blocks_per_mode: int = 20) -> list[Episode]:
    if len(seeds) < 10 or len(set(seeds)) != len(seeds) or any(
        type(seed) is not int or not 0 <= seed < (1 << 63) for seed in seeds
    ):
        raise ProtocolError("at least ten distinct nonnegative 63-bit seeds required")
    if type(blocks_per_mode) is not int or blocks_per_mode < 20:
        raise ProtocolError("at least 20 blocks per mode and seed required")
    plan = []
    for seed in seeds:
        rng = random.Random(seed)
        blocks = [(mode, index) for mode in ("nominal", "degraded")
                  for index in range(1, blocks_per_mode + 1)]
        rng.shuffle(blocks)
        for mode, index in blocks:
            # byte 40 is 0x55; XOR 0x25 produces a +6912 mC H3 mutation.
            baseline = 0x5500 + rng.randrange(256)
            reference_down = mode == "degraded" and rng.random() < 0.08
            node_down = mode == "degraded" and rng.random() < 0.105
            delay_ms = 120 if mode == "degraded" else 0
            block = f"S{seed}-{mode}-B{index:03d}"
            causes = list(HYPOTHESES)
            rng.shuffle(causes)
            plan.extend(Episode(seed, block, mode, cause, baseline,
                                reference_down, node_down, delay_ms)
                        for cause in causes)
    if len({e.episode_id for e in plan}) != len(plan):
        raise ProtocolError("duplicate episode identifiers in evaluation plan")
    return plan


def profile_of(episode: Episode) -> dict[str, dict]:
    return {
        "reference": {"unavailable": episode.reference_down,
                      "latency_ms": episode.delay_ms},
        "nodes": {"unavailable": episode.node_down,
                  "latency_ms": episode.delay_ms},
    }


def _probe_state(url: str, request=request_json) -> dict:
    value = request("GET", f"{url}/v1/probe-condition")
    if (type(value.get("unavailable")) is not bool
            or type(value.get("latency_ms")) is not int
            or type(value.get("revision")) is not int):
        raise ProtocolError("probe-condition readback has invalid fields")
    return value


def _set_probe(url: str, expected: dict, request=request_json) -> dict:
    request("POST", f"{url}/v1/probe-condition", expected)
    actual = _probe_state(url, request)
    if any(actual[key] != value for key, value in expected.items()):
        raise ProtocolError("remote probe condition differs from requested state")
    return actual


def _normal(state: dict) -> bool:
    return state["unavailable"] is False and state["latency_ms"] == 0


def _runner_args(episode: Episode, *, output_root: Path, timeout: float,
                 node_id: int) -> list[str]:
    argv = [
        sys.executable, str(Path(__file__).with_name(episode.cause.lower() + "_run.py")),
        "--episode-id", episode.episode_id,
        "--block-id", episode.block,
        "--node-id", str(node_id), "--mode", episode.mode,
        "--baseline-temperature-mc", str(episode.baseline),
        "--high-delta-mc", "3000", "--humidity-mpct", "50000",
        "--timeout-seconds", str(timeout), "--output-root", str(output_root),
        "--control-dir", str(ROOT),
    ]
    if episode.cause == "H1":
        argv += ["--disturbance-temperature-mc", "6912"]
    elif episode.cause == "H2":
        argv += ["--bias-temperature-mc", str(H2_BIAS_MC),
                 "--evaluation-random-seed", str(episode.seed)]
    elif episode.cause == "H3":
        argv += ["--offset", "40", "--xor-mask", "37"]
    return argv


def _json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ProtocolError(f"expected object: {path}")
    return value


def _h2_comparison_valid(out: Path) -> bool:
    paths = ("comparison_bias_state.json", "bias_restoration.json")
    if not all((out / name).is_file() for name in paths):
        return False
    checkpoints = _json(out / "comparison_bias_state.json")
    restored = _json(out / "bias_restoration.json")
    return (
        all(isinstance(checkpoints.get(name), dict)
            and checkpoints[name].get("bias_temperature_mC") == H2_BIAS_MC
            and checkpoints[name].get("bias_humidity_mPct") == 0
            for name in ("before", "after"))
        and restored.get("bias_temperature_mC") == 0
        and restored.get("bias_humidity_mPct") == 0
    )


def _completed(out: Path, episode: Episode) -> bool:
    if not out.exists():
        return False
    manifest = _json(out / "manifest.json")
    evaluation = _json(out / "policy_evaluation.json")
    restoration = _json(out / "probe_restoration.json")
    return (manifest.get("status") == "recorded"
            and manifest.get("policy_evaluation_status") == "recorded"
            and manifest.get("probe_restored") is True
            and (episode.cause != "H2" or
                 (manifest.get("policy_comparison_stage") == "active_sensor_bias"
                  and _h2_comparison_valid(out)))
            and manifest.get("episode_id") == episode.episode_id
            and evaluation.get("episode_id") == episode.episode_id
            and evaluation.get("mode") == episode.mode
            and len(evaluation.get("policies", [])) == len(POLICIES)
            and {x["policy"] for x in evaluation["policies"]} == set(POLICIES)
            and restoration.get("reference", {}).get("unavailable") is False
            and restoration.get("nodes", {}).get("unavailable") is False
            and restoration.get("reference", {}).get("latency_ms") == 0
            and restoration.get("nodes", {}).get("latency_ms") == 0)


def run_episode(episode: Episode, *, output_root: Path, timeout: float,
                node_id: int, request=request_json, executor=subprocess.run) -> None:
    out = output_root / episode.episode_id
    if out.exists():
        if _completed(out, episode):
            return
        raise ProtocolError(f"incomplete existing episode directory: {out}")
    baseline_states = {
        "reference": _probe_state(REFERENCE_URL, request),
        "nodes": _probe_state(NODE_URL, request),
    }
    if not all(_normal(state) for state in baseline_states.values()):
        raise ProtocolError("probe services must begin in nominal state")
    desired = profile_of(episode)
    attempted = set()
    failure = None
    restoration = {}
    try:
        for service, url in (("reference", REFERENCE_URL), ("nodes", NODE_URL)):
            attempted.add(service)  # Covers lost acknowledgements.
            changed = _set_probe(url, desired[service], request)
            if changed["revision"] <= baseline_states[service]["revision"]:
                raise ProtocolError("probe-condition revision did not advance")
        for service, url in (("reference", REFERENCE_URL), ("nodes", NODE_URL)):
            state = _probe_state(url, request)
            if any(state[k] != v for k, v in desired[service].items()):
                raise ProtocolError("probe condition changed unexpectedly")

        done = executor(_runner_args(episode, output_root=output_root,
                                    timeout=timeout, node_id=node_id),
                        cwd=ROOT, capture_output=True, text=True, check=False)
        if done.returncode != 0:
            raise ProtocolError(f"{episode.episode_id} runner exited {done.returncode}: "
                                f"{done.stderr[-1800:]}")
        manifest = _json(out / "manifest.json")
        record = _json(out / "core_diagnosis_record.json")
        selected = record.get("telemetry", {}).get("telemetry_id")
        if (manifest.get("status") != "recorded"
                or selected != manifest.get("telemetry_id")
                or type(selected) is not int
                or record.get("episode_id") != episode.episode_id):
            raise ProtocolError("runner evidence does not match selected telemetry")

        evaluation_request = {
            "node_id": node_id, "mode": episode.mode,
            "episode_id": episode.episode_id, "block_id": episode.block,
            "baseline_temperature_mC": episode.baseline,
            "high_delta_mC": 3000,
            "selected_telemetry_id": selected,
            "random_seed": episode.seed,
        }
        if episode.cause == "H2":
            if (manifest.get("policy_comparison_stage") != "active_sensor_bias"
                    or not _h2_comparison_valid(out)):
                raise ProtocolError("H2 comparison evidence is incomplete")
            evaluation = _json(out / "policy_evaluation.json")
        else:
            evaluation = request("POST", f"{CORE_URL}/v1/evaluate", evaluation_request)
        if (evaluation.get("episode_id") != episode.episode_id
                or evaluation.get("block_id") != episode.block
                or evaluation.get("mode") != episode.mode
                or evaluation.get("telemetry_id") != selected
                or evaluation.get("random_seed") != episode.seed
                or {v.get("policy") for v in evaluation.get("policies", [])} != set(POLICIES)
                or len(evaluation.get("policies", [])) != len(POLICIES)):
            raise ProtocolError("VM-CORE policy comparison is not paired to the episode")
        if episode.cause != "H2":
            save_json(out / "policy_evaluation.json", evaluation)
        save_json(out / "probe_profile.json", {
            "seed": episode.seed, "block_id": episode.block,
            "mode": episode.mode, "cause": episode.cause,
            "baseline_temperature_mC": episode.baseline,
            "requested": desired, "initial": baseline_states,
        })
    except BaseException as exc:
        failure = exc
    finally:
        for service, url in (("nodes", NODE_URL), ("reference", REFERENCE_URL)):
            if service in attempted:
                try:
                    original = baseline_states[service]
                    restoration[service] = _set_probe(url, {
                        "unavailable": original["unavailable"],
                        "latency_ms": original["latency_ms"],
                    }, request)
                    if not _normal(restoration[service]):
                        raise ProtocolError("probe reset returned a non-nominal condition")
                except BaseException as exc:
                    failure = ProtocolError(f"probe restoration failed: {service}: {exc}")
    if failure is not None:
        if out.exists():
            save_json(out / "batch_failure.json", {"error": str(failure)})
        raise failure
    if set(restoration) != {"reference", "nodes"}:
        raise ProtocolError("missing probe restoration evidence")
    save_json(out / "probe_restoration.json", restoration)
    manifest = _json(out / "manifest.json")
    manifest["policy_evaluation_status"] = "recorded"
    manifest["probe_restored"] = True
    save_json(out / "manifest.json", manifest)


def run(args, *, request=request_json, executor=subprocess.run) -> dict:
    plan = make_plan(tuple(args.seeds), args.blocks_per_mode)
    if args.plan_only:
        return {"planned_episodes": len(plan), "seeds": list(args.seeds),
                "blocks_per_seed_mode": args.blocks_per_mode,
                "nominal_episodes": sum(e.mode == "nominal" for e in plan),
                "degraded_episodes": sum(e.mode == "degraded" for e in plan)}
    args.output_root.mkdir(parents=True, exist_ok=True)
    if not 0 < args.timeout_seconds <= 3600:
        raise ProtocolError("invalid episode timeout")
    if not 0 <= args.node_id <= 65535:
        raise ProtocolError("invalid node ID")
    for episode in plan:
        run_episode(episode, output_root=args.output_root,
                    timeout=args.timeout_seconds, node_id=args.node_id,
                    request=request, executor=executor)
    from distributed_report import finalize
    return finalize(plan, args.output_root, bootstrap_replicates=args.bootstrap_replicates,
                    bootstrap_seed=args.bootstrap_seed)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="S3 V4 distributed paired-block evaluation")
    p.add_argument("--output-root", type=Path,
                   default=Path.home() / "eacld-experiments")
    p.add_argument("--seeds", nargs="+", type=int, default=list(DEFAULT_SEEDS))
    p.add_argument("--blocks-per-mode", type=int, default=20)
    p.add_argument("--node-id", type=int, default=1)
    p.add_argument("--timeout-seconds", type=float, default=120)
    p.add_argument("--bootstrap-replicates", type=int, default=1500)
    p.add_argument("--bootstrap-seed", type=int, default=20261104)
    p.add_argument("--plan-only", action="store_true")
    return p


def main(argv=None) -> int:
    try:
        args = parser().parse_args(argv)
        if not 1 <= args.bootstrap_replicates <= 100000:
            raise ProtocolError("invalid bootstrap count")
        print(json.dumps(run(args), sort_keys=True))
        return 0
    except (ProtocolError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"distributed evaluation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
