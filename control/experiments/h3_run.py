"""Run one H3 gateway-header mutation episode from VM-CONTROL.

G2 retains the original and modified wire frames. VM-CORE receives only
telemetry (with the modified header and original HMAC) and ordinary research
parameters; private intervention metadata stays on VM-CONTROL.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import struct
import subprocess
import sys
import time
from urllib.parse import urlencode

from h1_run import (
    ApiError, ProtocolError, control_command, request_json,
    run_command, save_json, utc_now,
)
from h2_run import _assert_bias, _assert_process, _find_fresh_telemetry

CORE_URL = "http://10.60.30.15:8300"
EDGE_URL = "http://10.60.30.14:8300"
NODES_URL = "http://10.60.30.12:8300"
CONTROL_DIR = Path(__file__).resolve().parents[1]
FRAME_BYTES = 94
HEADER_BYTES = 62
TEMPERATURE_OFFSETS = range(38, 42)


def altered_temperature(baseline_mC: int, offset: int, mask: int) -> int:
    """Compute the decoded temperature without having the node signing key."""
    frame_field = bytearray(struct.pack("!i", baseline_mC))
    frame_field[offset - 38] ^= mask
    return struct.unpack("!i", frame_field)[0]


def validate(args: argparse.Namespace) -> int:
    for field in ("episode_id", "block_id"):
        value = getattr(args, field)
        if not isinstance(value, str) or not 1 <= len(value) <= 128:
            raise ProtocolError(f"invalid {field}")
        if value in (".", "..") or any(ch in value for ch in ("/", "\\", "\x00")):
            raise ProtocolError(f"unsafe {field}")
    if args.mode not in ("nominal", "degraded"):
        raise ProtocolError("unsupported operating mode")
    if type(args.node_id) is not int or not 0 <= args.node_id <= 65535:
        raise ProtocolError("invalid node_id")
    if type(args.baseline_temperature_mc) is not int or not -(1 << 31) <= args.baseline_temperature_mc < (1 << 31):
        raise ProtocolError("invalid baseline temperature")
    if type(args.humidity_mpct) is not int or not 0 <= args.humidity_mpct <= 100000:
        raise ProtocolError("invalid humidity")
    if type(args.high_delta_mc) is not int or not 1 <= args.high_delta_mc <= 100000000:
        raise ProtocolError("invalid diagnostic threshold")
    if type(args.offset) is not int or args.offset not in TEMPERATURE_OFFSETS:
        raise ProtocolError("H3 mutation offset must be one of the temperature header bytes 38..41")
    if type(args.xor_mask) is not int or not 1 <= args.xor_mask <= 255:
        raise ProtocolError("xor_mask must be an integer from 1 through 255")
    if not 0 < args.timeout_seconds <= 3600 or not 0 < args.poll_seconds <= 60:
        raise ProtocolError("invalid polling options")
    observed = altered_temperature(
        args.baseline_temperature_mc, args.offset, args.xor_mask)
    if observed < args.baseline_temperature_mc + args.high_delta_mc:
        raise ProtocolError("the header mutation does not cross the diagnostic threshold")
    return observed


def verify_mutation(event: dict, args: argparse.Namespace, expected: int) -> dict:
    if (type(event.get("event_id")) is not int or event["event_id"] < 1
            or event.get("offset") != args.offset
            or event.get("xor_mask") != args.xor_mask):
        raise ProtocolError("G2 mutation event does not match the staged operation")
    try:
        original = bytes.fromhex(event["original_frame_hex"])
        mutated = bytes.fromhex(event["mutated_frame_hex"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ProtocolError("G2 did not return valid frame evidence") from exc
    if len(original) != FRAME_BYTES or len(mutated) != FRAME_BYTES:
        raise ProtocolError("G2 returned frames with an unexpected length")
    if original[1] != 1 or int.from_bytes(original[2:4], "big") != args.node_id:
        raise ProtocolError("G2 original was not target-node telemetry")
    if (int.from_bytes(original[38:42], "big", signed=True)
            != args.baseline_temperature_mc
            or int.from_bytes(original[42:46], "big") != args.humidity_mpct):
        raise ProtocolError("G2 original telemetry differs from the controlled baseline")
    expected_frame = bytearray(original)
    expected_frame[args.offset] ^= args.xor_mask
    if mutated != bytes(expected_frame) or original[HEADER_BYTES:] != mutated[HEADER_BYTES:]:
        raise ProtocolError("G2 modified bytes other than the selected header byte")
    if int.from_bytes(mutated[38:42], "big", signed=True) != expected:
        raise ProtocolError("G2 mutated temperature does not match the predicted value")
    return {
        "event_id": event["event_id"], "offset": args.offset,
        "xor_mask": args.xor_mask,
        "original_frame_hex": original.hex(), "mutated_frame_hex": mutated.hex(),
        "original_temperature_mC": args.baseline_temperature_mc,
        "mutated_temperature_mC": expected,
        "mac_unchanged": True,
    }


def run_h3(args: argparse.Namespace, *, request=request_json,
           control=control_command, command=run_command,
           sleeper=time.sleep, clock=time.monotonic) -> dict:
    expected = validate(args)
    out = args.output_root.expanduser().resolve() / args.episode_id
    out.mkdir(parents=True, exist_ok=False)

    core = args.core_url.rstrip("/")
    edge = args.edge_url.rstrip("/")
    nodes = args.nodes_control_url.rstrip("/")
    latest_url = f"{core}/v1/telemetry/latest?{urlencode({'node_id': args.node_id})}"
    edge_action = f"{edge}/v1/g2/intervention"
    edge_status = f"{edge_action}/status?{urlencode({'node_id': args.node_id})}"
    bias_status = f"{nodes}/v1/nodes/bias?{urlencode({'node_id': args.node_id})}"
    diagnosis_record = f"{core}/v1/diagnosis?{urlencode({'episode_id': args.episode_id})}"

    save_json(out / "parameters.json", {
        "episode_id": args.episode_id, "block_id": args.block_id,
        "node_id": args.node_id, "mode": args.mode,
        "baseline_temperature_mC": args.baseline_temperature_mc,
        "humidity_mPct": args.humidity_mpct,
        "high_delta_mC": args.high_delta_mc,
        "mutated_temperature_mC": expected,
        "mutation_offset": args.offset,
        "xor_mask": args.xor_mask,
        "started_at_utc": utc_now(),
    })
    action_may_be_pending = False
    failure: BaseException | None = None
    selected: dict | None = None
    commit = ""

    try:
        commit = command(["git", "rev-parse", "HEAD"], args.control_dir)
        (out / "git_commit.txt").write_text(commit + "\n", encoding="utf-8")
        if request("GET", f"{core}/healthz").get("status") != "ok":
            raise ProtocolError("VM-CORE is unavailable")
        for name in ("nodes", "reference", "edge", "core"):
            if control(["agent-health", name], args.control_dir).get("status") != "ok":
                raise ProtocolError(f"{name} control agent is unavailable")

        bias = request("GET", bias_status)
        _assert_bias(bias, args.node_id, 0, 0)
        save_json(out / "initial_bias.json", bias)

        prior = request("GET", edge_status)
        if prior.get("status") != "ok" or prior.get("node_id") != args.node_id:
            raise ProtocolError("G2 status is unavailable")
        if prior.get("pending_action") is not None:
            raise ProtocolError("G2 already has a pending intervention for the node")
        prior_event = (prior.get("last_mutation") or {}).get("event_id", 0)
        if type(prior_event) is not int or prior_event < 0:
            raise ProtocolError("invalid G2 mutation event counter")
        save_json(out / "edge_initial_status.json", prior)

        try:
            latest = request("GET", latest_url)
            previous_id = latest.get("telemetry_id")
            if type(previous_id) is not int or previous_id < 1:
                raise ProtocolError("invalid last accepted telemetry ID")
        except ApiError as exc:
            if exc.status != 404:
                raise
            previous_id = 0

        reset = control([
            "reset", "--temperature-mC", str(args.baseline_temperature_mc),
            "--humidity-mPct", str(args.humidity_mpct),
            "--ambient-temperature-mC", str(args.baseline_temperature_mc),
            "--ambient-humidity-mPct", str(args.humidity_mpct),
        ], args.control_dir)
        _assert_process(reset, args.baseline_temperature_mc, args.humidity_mpct)
        private = control(["private"], args.control_dir)
        if private.get("interventions") or private.get("private_steps"):
            raise ProtocolError("the controlled process was not reset")
        baseline = _find_fresh_telemetry(
            request, latest_url, previous_id=previous_id, node_id=args.node_id,
            temperature=args.baseline_temperature_mc, humidity=args.humidity_mpct,
            deadline=clock() + args.timeout_seconds,
            clock=clock, sleeper=sleeper, poll_seconds=args.poll_seconds,
        )
        save_json(out / "baseline_telemetry.json", baseline)

        # Mark as possibly armed before sending: a lost HTTP reply must
        # still trigger best-effort cleanup on VM-EDGE.
        action_may_be_pending = True
        armed = request("POST", edge_action, {
            "action": "mutate", "node_id": args.node_id,
            "offset": args.offset, "xor_mask": args.xor_mask,
        })
        if armed.get("status") != "ok":
            raise ProtocolError("G2 did not acknowledge H3 mutation staging")

        event = None
        deadline = clock() + args.timeout_seconds
        while clock() < deadline:
            state = request("GET", edge_status)
            observation = state.get("last_mutation")
            if (state.get("status") == "ok"
                    and isinstance(observation, dict)
                    and type(observation.get("event_id")) is int
                    and observation["event_id"] > prior_event):
                event = verify_mutation(observation, args, expected)
                if state.get("pending_action") is not None:
                    raise ProtocolError("G2 mutation was not consumed")
                break
            sleeper(args.poll_seconds)
        if event is None:
            raise ProtocolError("G2 mutation event was not observed")
        save_json(out / "edge_mutation_evidence.json", event)

        match_query = urlencode({
            "node_id": args.node_id,
            "after_id": baseline["telemetry_id"],
            "frame_hex": event["mutated_frame_hex"],
        })
        match_url = f"{core}/v1/telemetry/match?{match_query}"
        deadline = clock() + args.timeout_seconds
        while clock() < deadline:
            try:
                candidate = request("GET", match_url)
            except ApiError as exc:
                if exc.status != 404:
                    raise
                sleeper(args.poll_seconds)
                continue
            if (candidate.get("node_id") != args.node_id
                    or candidate.get("frame_hex") != event["mutated_frame_hex"]
                    or candidate.get("temperature_mC") != expected
                    or candidate.get("mac_status") != "invalid"
                    or candidate.get("sequence_status") != "unavailable"
                    or type(candidate.get("telemetry_id")) is not int
                    or candidate["telemetry_id"] <= baseline["telemetry_id"]):
                raise ProtocolError("VM-CORE did not persist the expected invalid H3 frame")
            selected = candidate
            break
        if selected is None:
            raise ProtocolError("VM-CORE did not ingest the mutated H3 frame")
        save_json(out / "core_mutated_telemetry.json", selected)

        _assert_process(control(["state"], args.control_dir),
                        args.baseline_temperature_mc, args.humidity_mpct)
        diagnosis = request("POST", f"{core}/v1/diagnose", {
            "node_id": args.node_id, "mode": args.mode,
            "episode_id": args.episode_id, "block_id": args.block_id,
            "baseline_temperature_mC": args.baseline_temperature_mc,
            "high_delta_mC": args.high_delta_mc,
            "selected_telemetry_id": selected["telemetry_id"],
        })
        save_json(out / "diagnosis_response.json", diagnosis)

        record = request("GET", diagnosis_record)
        reading = record.get("telemetry")
        if (not isinstance(reading, dict)
                or record.get("episode_id") != args.episode_id
                or record.get("block_id") != args.block_id
                or record.get("node_id") != args.node_id
                or reading.get("telemetry_id") != selected["telemetry_id"]
                or reading.get("frame_hex") != event["mutated_frame_hex"]
                or reading.get("mac_status") != "invalid"
                or reading.get("sequence_status") != "unavailable"):
            raise ProtocolError("the recorded diagnosis did not use the selected H3 frame")
        save_json(out / "core_diagnosis_record.json", record)

        _assert_process(control(["state"], args.control_dir),
                        args.baseline_temperature_mc, args.humidity_mpct)
        truth = control(["private"], args.control_dir)
        if truth.get("interventions") or truth.get("private_steps"):
            raise ProtocolError("unexpected process intervention occurred during H3")
        save_json(out / "private_ground_truth.json", {
            "cause": "H3", "node_id": args.node_id,
            "episode_id": args.episode_id, "block_id": args.block_id,
            "mutation_event": event,
            "private_process": truth,
        })
    except BaseException as exc:
        failure = exc
    finally:
        if action_may_be_pending:
            try:
                response = request("POST", edge_action, {
                    "action": "clear", "node_id": args.node_id,
                })
                if response.get("status") != "ok":
                    raise ProtocolError("G2 rejected intervention cleanup")
                cleared = request("GET", edge_status)
                if cleared.get("pending_action") is not None:
                    raise ProtocolError("G2 still has a pending intervention")
                save_json(out / "edge_cleanup.json", cleared)
            except BaseException as cleanup_error:
                previous = f"{type(failure).__name__}: {failure}" if failure else "none"
                failure = ProtocolError(
                    f"experiment failure: {previous}; G2 cleanup failed: {cleanup_error}"
                )
    if failure is not None:
        save_json(out / "failure.json", {
            "at_utc": utc_now(), "error": f"{type(failure).__name__}: {failure}",
        })
        raise failure

    summary = {
        "status": "recorded", "episode_id": args.episode_id,
        "telemetry_id": selected["telemetry_id"],
        "git_commit": commit,
        "completed_at_utc": utc_now(), "output_directory": str(out),
    }
    save_json(out / "manifest.json", summary)
    return summary


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Execute one authenticated H3 header mutation")
    p.add_argument("--episode-id", required=True)
    p.add_argument("--block-id", required=True)
    p.add_argument("--node-id", type=int, required=True)
    p.add_argument("--mode", choices=("nominal", "degraded"), required=True)
    p.add_argument("--baseline-temperature-mc", type=int, required=True)
    p.add_argument("--high-delta-mc", type=int, required=True)
    p.add_argument("--humidity-mpct", type=int, default=50000)
    p.add_argument("--offset", type=int, required=True)
    p.add_argument("--xor-mask", type=int, required=True)
    p.add_argument("--timeout-seconds", type=float, default=60)
    p.add_argument("--poll-seconds", type=float, default=1)
    p.add_argument("--output-root", type=Path, default=Path.home() / "eacld-experiments")
    p.add_argument("--control-dir", type=Path, default=CONTROL_DIR)
    p.add_argument("--core-url", default=CORE_URL)
    p.add_argument("--edge-url", default=EDGE_URL)
    p.add_argument("--nodes-control-url", default=NODES_URL)
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        print(json.dumps(run_h3(parser().parse_args(argv)), sort_keys=True))
        return 0
    except (OSError, ProtocolError, subprocess.SubprocessError) as exc:
        print(f"H3 execution failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
