"""Execute H2 sensor-bias episodes using existing restricted EACLD interfaces.

All intervention parameters and ground truth stay on VM-CONTROL.
The original process is not modified after resetting its initial conditions.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlencode

from h1_run import (
    ApiError, ProtocolError, control_command, request_json, run_command,
    save_json, utc_now,
)

CORE_URL = "http://10.60.30.15:8300"
NODES_CONTROL_URL = "http://10.60.30.12:8300"
CONTROL_DIR = Path(__file__).resolve().parents[1]


def validate(args: argparse.Namespace) -> None:
    for key in ("episode_id", "block_id"):
        value = getattr(args, key)
        if not isinstance(value, str) or not 1 <= len(value) <= 128:
            raise ProtocolError(f"invalid {key}")
        if value in (".", "..") or any(ch in value for ch in ("/", "\\", "\x00")):
            raise ProtocolError(f"unsafe {key}")
    if args.mode != "nominal":
        raise ProtocolError("H2 runner implements nominal conditions only")
    if type(args.node_id) is not int or not 0 <= args.node_id <= 65535:
        raise ProtocolError("invalid node ID")
    if type(args.baseline_temperature_mc) is not int or not -(1 << 31) <= args.baseline_temperature_mc < (1 << 31):
        raise ProtocolError("invalid temperature baseline")
    if type(args.bias_temperature_mc) is not int or not -(1 << 31) <= args.bias_temperature_mc < (1 << 31):
        raise ProtocolError("invalid sensor bias")
    if type(args.high_delta_mc) is not int or not 1 <= args.high_delta_mc <= 100000000:
        raise ProtocolError("invalid diagnostic threshold")
    if type(args.humidity_mpct) is not int or not 0 <= args.humidity_mpct <= 100000:
        raise ProtocolError("invalid humidity baseline")
    if not 0 < args.timeout_seconds <= 3600 or not 0 < args.poll_seconds <= 60:
        raise ProtocolError("invalid polling duration")
    measured = args.baseline_temperature_mc + args.bias_temperature_mc
    if not -(1 << 31) <= measured < (1 << 31):
        raise ProtocolError("biased temperature is outside wire range")
    if measured < args.baseline_temperature_mc + args.high_delta_mc:
        raise ProtocolError("bias does not cross the diagnostic temperature threshold")


def _assert_bias(payload: dict, node_id: int, temperature: int, humidity: int) -> None:
    if (payload.get("node_id") != node_id
            or payload.get("bias_temperature_mC") != temperature
            or payload.get("bias_humidity_mPct") != humidity
            or type(payload.get("revision")) is not int):
        raise ProtocolError("VM-NODES bias state differs from the requested value")


def _assert_process(state: dict, temperature: int, humidity: int) -> None:
    if (state.get("step_id") != 0
            or state.get("temperature_mC") != temperature
            or state.get("humidity_mPct") != humidity):
        raise ProtocolError("the shared process changed during H2")


def _find_fresh_telemetry(request, url: str, *, previous_id: int,
                          node_id: int, temperature: int, humidity: int,
                          deadline: float, clock, sleeper, poll_seconds: float):
    while clock() < deadline:
        try:
            reading = request("GET", url)
        except ApiError as exc:
            if exc.status != 404:
                raise
        else:
            if (type(reading.get("telemetry_id")) is int
                    and reading["telemetry_id"] > previous_id
                    and reading.get("node_id") == node_id
                    and reading.get("temperature_mC") == temperature
                    and reading.get("humidity_mPct") == humidity
                    and reading.get("mac_status") == "valid"
                    and reading.get("sequence_status") == "fresh"):
                return reading
        sleeper(poll_seconds)
    raise ProtocolError("matching fresh telemetry was not received before timeout")


def run_h2(args: argparse.Namespace, *, request=request_json,
           control=control_command, command=run_command,
           sleeper=time.sleep, clock=time.monotonic) -> dict:
    validate(args)
    out = args.output_root.expanduser().resolve() / args.episode_id
    out.mkdir(parents=True, exist_ok=False)

    core_url = args.core_url.rstrip("/")
    nodes_url = args.nodes_control_url.rstrip("/")
    latest_url = f"{core_url}/v1/telemetry/latest?{urlencode({'node_id': args.node_id})}"
    record_url = f"{core_url}/v1/diagnosis?{urlencode({'episode_id': args.episode_id})}"
    bias_url = f"{nodes_url}/v1/nodes/bias"
    bias_query = f"{bias_url}?{urlencode({'node_id': args.node_id})}"
    expected_temperature = args.baseline_temperature_mc + args.bias_temperature_mc
    initial_bias: dict | None = None
    bias_may_have_changed = False
    failure: BaseException | None = None
    selected = None
    commit = ""

    save_json(out / "parameters.json", {
        "episode_id": args.episode_id, "block_id": args.block_id,
        "node_id": args.node_id, "mode": args.mode,
        "baseline_temperature_mC": args.baseline_temperature_mc,
        "high_delta_mC": args.high_delta_mc,
        "bias_temperature_mC": args.bias_temperature_mc,
        "bias_humidity_mPct": 0, "humidity_mPct": args.humidity_mpct,
        "expected_temperature_mC": expected_temperature,
        "started_at_utc": utc_now(),
    })
    try:
        commit = command(["git", "rev-parse", "HEAD"], args.control_dir)
        (out / "git_commit.txt").write_text(commit + "\n", encoding="utf-8")
        if request("GET", f"{core_url}/healthz").get("status") != "ok":
            raise ProtocolError("VM-CORE is not ready")
        for name in ("nodes", "reference", "edge", "core"):
            if control(["agent-health", name], args.control_dir).get("status") != "ok":
                raise ProtocolError(f"{name} control agent is unavailable")

        initial_bias = request("GET", bias_query)
        _assert_bias(initial_bias, args.node_id, 0, 0)
        save_json(out / "initial_bias.json", initial_bias)

        try:
            latest = request("GET", latest_url)
            previous_id = latest.get("telemetry_id")
            if type(previous_id) is not int or previous_id < 1:
                raise ProtocolError("invalid last telemetry identifier")
        except ApiError as exc:
            if exc.status != 404:
                raise
            previous_id = 0

        reset_state = control([
            "reset", "--temperature-mC", str(args.baseline_temperature_mc),
            "--humidity-mPct", str(args.humidity_mpct),
            "--ambient-temperature-mC", str(args.baseline_temperature_mc),
            "--ambient-humidity-mPct", str(args.humidity_mpct),
        ], args.control_dir)
        _assert_process(reset_state, args.baseline_temperature_mc, args.humidity_mpct)

        initial_truth = control(["private"], args.control_dir)
        if initial_truth.get("interventions") or initial_truth.get("private_steps"):
            raise ProtocolError("private process history was not cleared by reset")

        baseline = _find_fresh_telemetry(
            request, latest_url, previous_id=previous_id,
            node_id=args.node_id,
            temperature=args.baseline_temperature_mc,
            humidity=args.humidity_mpct,
            deadline=clock() + args.timeout_seconds,
            clock=clock, sleeper=sleeper, poll_seconds=args.poll_seconds,
        )
        save_json(out / "baseline_telemetry.json", baseline)
        previous_id = baseline["telemetry_id"]

        # Restoration is attempted even if the modification succeeded but
        # the HTTP acknowledgement was lost.
        bias_may_have_changed = True
        set_bias = {
            "node_id": args.node_id,
            "bias_temperature_mC": args.bias_temperature_mc,
            "bias_humidity_mPct": 0,
        }
        updated = request("POST", bias_url, set_bias)
        _assert_bias(updated, args.node_id, args.bias_temperature_mc, 0)
        confirmed = request("GET", bias_query)
        _assert_bias(confirmed, args.node_id, args.bias_temperature_mc, 0)
        if confirmed["revision"] <= initial_bias["revision"]:
            raise ProtocolError("sensor bias revision did not advance")
        save_json(out / "bias_activation.json", confirmed)

        _assert_process(
            control(["state"], args.control_dir),
            args.baseline_temperature_mc, args.humidity_mpct,
        )

        diagnosis_body = {
            "node_id": args.node_id, "mode": args.mode,
            "episode_id": args.episode_id, "block_id": args.block_id,
            "baseline_temperature_mC": args.baseline_temperature_mc,
            "high_delta_mC": args.high_delta_mc,
        }
        deadline = clock() + args.timeout_seconds
        while clock() < deadline:
            candidate = _find_fresh_telemetry(
                request, latest_url, previous_id=previous_id,
                node_id=args.node_id, temperature=expected_temperature,
                humidity=args.humidity_mpct,
                deadline=deadline, clock=clock, sleeper=sleeper,
                poll_seconds=args.poll_seconds,
            )
            try:
                diagnosis = request(
                    "POST", f"{core_url}/v1/diagnose",
                    {**diagnosis_body, "expected_telemetry_id": candidate["telemetry_id"]},
                )
            except ApiError as exc:
                if exc.status == 409 and "telemetry changed" in exc.detail:
                    previous_id = candidate["telemetry_id"]
                    sleeper(args.poll_seconds)
                    continue
                raise
            selected = candidate
            save_json(out / "diagnosis_response.json", diagnosis)
            break
        if selected is None:
            raise ProtocolError("H2 observation could not be diagnosed before timeout")

        record = request("GET", record_url)
        observation = record.get("telemetry")
        if (not isinstance(observation, dict)
                or record.get("episode_id") != args.episode_id
                or record.get("block_id") != args.block_id
                or record.get("node_id") != args.node_id
                or observation.get("telemetry_id") != selected["telemetry_id"]
                or observation.get("temperature_mC") != expected_temperature
                or observation.get("humidity_mPct") != args.humidity_mpct
                or observation.get("mac_status") != "valid"
                or observation.get("sequence_status") != "fresh"):
            raise ProtocolError("persisted diagnosis does not match H2 input telemetry")
        save_json(out / "core_diagnosis_record.json", record)

        state = control(["state"], args.control_dir)
        _assert_process(state, args.baseline_temperature_mc, args.humidity_mpct)
        save_json(out / "process_state.json", state)
        private = control(["private"], args.control_dir)
        if private.get("interventions") or private.get("private_steps"):
            raise ProtocolError("a process-level intervention occurred during H2")
        save_json(out / "private_ground_truth.json", {
            "cause": "H2", "episode_id": args.episode_id,
            "block_id": args.block_id, "node_id": args.node_id,
            "bias_temperature_mC": args.bias_temperature_mc,
            "bias_humidity_mPct": 0,
            "private_process": private,
        })
    except BaseException as exc:
        failure = exc
    finally:
        if bias_may_have_changed and initial_bias is not None:
            try:
                restored = request("POST", bias_url, {
                    "node_id": args.node_id,
                    "bias_temperature_mC": initial_bias["bias_temperature_mC"],
                    "bias_humidity_mPct": initial_bias["bias_humidity_mPct"],
                })
                _assert_bias(
                    restored, args.node_id, initial_bias["bias_temperature_mC"],
                    initial_bias["bias_humidity_mPct"],
                )
                confirmed = request("GET", bias_query)
                _assert_bias(
                    confirmed, args.node_id,
                    initial_bias["bias_temperature_mC"],
                    initial_bias["bias_humidity_mPct"],
                )
                save_json(out / "bias_restoration.json", confirmed)
            except BaseException as restore_error:
                original = f"{type(failure).__name__}: {failure}" if failure else "none"
                failure = ProtocolError(
                    f"experiment failure: {original}; bias restoration failed: {restore_error}"
                )
    if failure is not None:
        save_json(out / "failure.json", {
            "at_utc": utc_now(),
            "error": f"{type(failure).__name__}: {failure}",
        })
        raise failure
    summary = {
        "status": "recorded", "episode_id": args.episode_id,
        "telemetry_id": selected["telemetry_id"],
        "git_commit": commit, "completed_at_utc": utc_now(),
        "output_directory": str(out),
    }
    save_json(out / "manifest.json", summary)
    return summary


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run one distributed H2 sensor-bias episode")
    p.add_argument("--episode-id", required=True)
    p.add_argument("--block-id", required=True)
    p.add_argument("--node-id", type=int, required=True)
    p.add_argument("--mode", choices=("nominal",), required=True)
    p.add_argument("--baseline-temperature-mc", type=int, required=True)
    p.add_argument("--high-delta-mc", type=int, required=True)
    p.add_argument("--bias-temperature-mc", type=int, required=True)
    p.add_argument("--humidity-mpct", type=int, default=50000)
    p.add_argument("--timeout-seconds", type=float, default=60)
    p.add_argument("--poll-seconds", type=float, default=1)
    p.add_argument("--output-root", type=Path, default=Path.home() / "eacld-experiments")
    p.add_argument("--control-dir", type=Path, default=CONTROL_DIR)
    p.add_argument("--core-url", default=CORE_URL)
    p.add_argument("--nodes-control-url", default=NODES_CONTROL_URL)
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        print(json.dumps(run_h2(parser().parse_args(argv)), sort_keys=True))
        return 0
    except (OSError, ProtocolError, subprocess.SubprocessError) as exc:
        print(f"H2 execution failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
