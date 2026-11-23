"""Run a traceable H1 episode from VM-CONTROL using deployed EACLD services.

The VM-CORE API receives no private cause label, intervention schedule or truth.
All research parameters are supplied by the operator.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

CORE_URL = "http://10.60.30.15:8300"
CONTROL_DIR = Path(__file__).resolve().parents[1]


class ProtocolError(RuntimeError):
    pass


class ApiError(ProtocolError):
    def __init__(self, status: int, detail: str):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def request_json(method: str, url: str, body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body, separators=(",", ":")).encode()
    req = Request(url, data=data, method=method, headers={
        "Accept": "application/json",
        **({"Content-Type": "application/json"} if data is not None else {}),
    })
    try:
        with build_opener(ProxyHandler({})).open(req, timeout=30) as response:
            value = json.load(response)
            if not isinstance(value, dict):
                raise ProtocolError(f"non-object JSON response from {url}")
            return value
    except HTTPError as exc:
        raise ApiError(exc.code, exc.read(1024).decode("utf-8", "replace")) from exc
    except (URLError, TimeoutError, ValueError) as exc:
        raise ProtocolError(f"request failed: {method} {url}: {exc}") from exc


def run_command(argv: list[str], cwd: Path) -> str:
    done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                          check=False, timeout=30)
    if done.returncode:
        raise ProtocolError(f"command failed ({done.returncode}): "
                            f"{' '.join(argv)}: {done.stderr.strip()}")
    return done.stdout.strip()


def control_command(operation: list[str], cwd: Path) -> dict:
    value = run_command(["docker", "compose", "exec", "-T", "eacld-control",
                         "python", "-m", "src.orchestrator", *operation], cwd)
    try:
        result = json.loads(value)
    except ValueError as exc:
        raise ProtocolError("orchestrator returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise ProtocolError("orchestrator returned non-object JSON")
    return result


def save_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")


def validate(args: argparse.Namespace) -> None:
    for field in ("episode_id", "block_id"):
        value = getattr(args, field)
        if not 1 <= len(value) <= 128 or any(c in value for c in ("/", "\\", "\x00")):
            raise ProtocolError(f"invalid {field}")
    if args.mode not in ("nominal", "degraded"):
        raise ProtocolError("unsupported operating mode")
    if not 0 <= args.node_id <= 65535:
        raise ProtocolError("node_id outside uint16 range")
    if not -(2**31) <= args.baseline_temperature_mc < 2**31:
        raise ProtocolError("invalid baseline temperature")
    if not -(2**31) <= args.disturbance_temperature_mc < 2**31:
        raise ProtocolError("invalid disturbance")
    if not 0 <= args.humidity_mpct <= 100000:
        raise ProtocolError("invalid humidity")
    if not 1 <= args.high_delta_mc <= 100000000:
        raise ProtocolError("invalid diagnostic threshold")
    if not 0 < args.timeout_seconds <= 3600 or not 0 < args.poll_seconds <= 60:
        raise ProtocolError("invalid polling parameters")
    expected = args.baseline_temperature_mc + args.disturbance_temperature_mc
    if not -(2**31) <= expected < 2**31:
        raise ProtocolError("expected temperature outside signed 32-bit range")
    if expected < args.baseline_temperature_mc + args.high_delta_mc:
        raise ProtocolError("H1 intervention does not cross the diagnostic threshold")


def run_h1(args: argparse.Namespace, *, request=request_json,
           control=control_command, command=run_command,
           sleeper=time.sleep, clock=time.monotonic) -> dict:
    validate(args)
    out = args.output_root.expanduser().resolve() / args.episode_id
    out.mkdir(parents=True, exist_ok=False)
    core = args.core_url.rstrip("/")
    latest_url = f"{core}/v1/telemetry/latest?{urlencode({'node_id': args.node_id})}"
    diagnosis_url = f"{core}/v1/diagnosis?{urlencode({'episode_id': args.episode_id})}"
    expected_temp = args.baseline_temperature_mc + args.disturbance_temperature_mc
    params = {
        "episode_id": args.episode_id, "block_id": args.block_id,
        "node_id": args.node_id, "mode": args.mode,
        "baseline_temperature_mC": args.baseline_temperature_mc,
        "high_delta_mC": args.high_delta_mc,
        "humidity_mPct": args.humidity_mpct,
        "temperature_disturbance_mC": args.disturbance_temperature_mc,
        "expected_temperature_mC": expected_temp,
        "started_at_utc": utc_now(),
    }
    save_json(out / "parameters.json", params)
    try:
        commit = command(["git", "rev-parse", "HEAD"], args.control_dir)
        (out / "git_commit.txt").write_text(commit + "\n", encoding="utf-8")
        if request("GET", f"{core}/healthz").get("status") != "ok":
            raise ProtocolError("VM-CORE MQTT is not ready")
        for name in ("nodes", "reference", "edge", "core"):
            if control(["agent-health", name], args.control_dir).get("status") != "ok":
                raise ProtocolError(f"control agent {name} is unavailable")
        try:
            reading = request("GET", latest_url)
            previous_id = reading["telemetry_id"]
            if type(previous_id) is not int or previous_id < 1:
                raise ProtocolError("invalid telemetry record identifier")
        except ApiError as exc:
            if exc.status != 404:
                raise
            previous_id = 0

        initial = control([
            "reset", "--temperature-mC", str(args.baseline_temperature_mc),
            "--humidity-mPct", str(args.humidity_mpct),
            "--ambient-temperature-mC", str(args.baseline_temperature_mc),
        ], args.control_dir)
        if (initial.get("step_id") != 0 or
                initial.get("temperature_mC") != args.baseline_temperature_mc):
            raise ProtocolError("process reset did not establish the baseline")

        deadline = clock() + args.timeout_seconds
        baseline = None
        while clock() < deadline:
            try:
                candidate = request("GET", latest_url)
                if (candidate.get("telemetry_id", 0) > previous_id
                        and candidate.get("node_id") == args.node_id
                        and candidate.get("temperature_mC") == args.baseline_temperature_mc
                        and candidate.get("mac_status") == "valid"
                        and candidate.get("sequence_status") == "fresh"):
                    baseline = candidate
                    break
            except ApiError as exc:
                if exc.status != 404:
                    raise
            sleeper(args.poll_seconds)
        if baseline is None:
            raise ProtocolError("no fresh baseline telemetry after reset")
        save_json(out / "baseline_telemetry.json", baseline)
        previous_id = baseline["telemetry_id"]

        control([
            "inject", "--start-step", "1", "--end-step", "2",
            "--temperature-disturbance-mC", str(args.disturbance_temperature_mc),
        ], args.control_dir)
        state = control(["step", "--count", "1"], args.control_dir)
        if state.get("step_id") != 1 or state.get("temperature_mC") != expected_temp:
            raise ProtocolError(f"unexpected injected process state: {state}")
        save_json(out / "process_state.json", state)
        truth = control(["private"], args.control_dir)
        private_steps = truth.get("private_steps", [])
        if len(private_steps) != 1 or private_steps[0].get("cause_labels") != "H1":
            raise ProtocolError("private audit does not confirm H1 at step 1")
        save_json(out / "private_ground_truth.json", truth)

        diagnosis_body = {
            "node_id": args.node_id, "mode": args.mode,
            "episode_id": args.episode_id, "block_id": args.block_id,
            "baseline_temperature_mC": args.baseline_temperature_mc,
            "high_delta_mC": args.high_delta_mc,
        }
        selected = None
        deadline = clock() + args.timeout_seconds
        while clock() < deadline:
            try:
                candidate = request("GET", latest_url)
                if (candidate.get("telemetry_id", 0) > previous_id
                        and candidate.get("node_id") == args.node_id
                        and candidate.get("temperature_mC") == expected_temp
                        and candidate.get("mac_status") == "valid"
                        and candidate.get("sequence_status") == "fresh"):
                    body = {**diagnosis_body,
                            "expected_telemetry_id": candidate["telemetry_id"]}
                    try:
                        result = request("POST", f"{core}/v1/diagnose", body)
                    except ApiError as exc:
                        if exc.status == 409 and "telemetry changed" in exc.detail:
                            sleeper(args.poll_seconds)
                            continue
                        raise
                    selected = candidate
                    save_json(out / "diagnosis_response.json", result)
                    break
            except ApiError as exc:
                if exc.status != 404:
                    raise
            sleeper(args.poll_seconds)
        if selected is None:
            raise ProtocolError("no matching fresh H1 telemetry diagnosed before timeout")
        record = request("GET", diagnosis_url)
        observation = record.get("telemetry", {})
        if not isinstance(observation, dict) or (
            observation.get("telemetry_id") != selected["telemetry_id"]
            or observation.get("temperature_mC") != expected_temp
            or observation.get("mac_status") != "valid"
            or observation.get("sequence_status") != "fresh"
            or record.get("episode_id") != args.episode_id
        ):
            raise ProtocolError("stored diagnosis did not use selected H1 telemetry")
        save_json(out / "core_diagnosis_record.json", record)
        summary = {
            "status": "recorded", "episode_id": args.episode_id,
            "telemetry_id": selected["telemetry_id"], "git_commit": commit,
            "completed_at_utc": utc_now(), "output_directory": str(out),
        }
        save_json(out / "manifest.json", summary)
        return summary
    except BaseException as exc:
        save_json(out / "failure.json", {
            "at_utc": utc_now(), "error": f"{type(exc).__name__}: {exc}"})
        raise


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run one distributed H1 experiment")
    p.add_argument("--episode-id", required=True)
    p.add_argument("--block-id", required=True)
    p.add_argument("--node-id", type=int, required=True)
    p.add_argument("--mode", choices=("nominal", "degraded"), required=True)
    p.add_argument("--baseline-temperature-mc", type=int, required=True)
    p.add_argument("--high-delta-mc", type=int, required=True)
    p.add_argument("--disturbance-temperature-mc", type=int, required=True)
    p.add_argument("--humidity-mpct", type=int, default=50000)
    p.add_argument("--timeout-seconds", type=float, default=60)
    p.add_argument("--poll-seconds", type=float, default=1)
    p.add_argument("--output-root", type=Path, default=Path.home() / "eacld-experiments")
    p.add_argument("--control-dir", type=Path, default=CONTROL_DIR)
    p.add_argument("--core-url", default=CORE_URL)
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        print(json.dumps(run_h1(parser().parse_args(argv)), sort_keys=True))
        return 0
    except (OSError, ProtocolError, subprocess.SubprocessError) as exc:
        print(f"H1 execution failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
