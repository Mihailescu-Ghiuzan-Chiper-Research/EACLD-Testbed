"""Execute H4 authentic-frame replay from VM-CONTROL.

A genuine, previously accepted VM-NODES telemetry frame is selected by its
exact bytes from G2's forwarding history. G2 forwards the next live frame and
then retransmits that earlier signed frame. VM-CORE receives neither the
H4 intervention label nor private process state.
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
    ApiError, ProtocolError, control_command, request_json,
    run_command, save_json, utc_now,
)
from h2_run import _assert_bias, _assert_process, _find_fresh_telemetry

CORE_URL = "http://10.60.30.15:8300"
EDGE_URL = "http://10.60.30.14:8300"
NODES_URL = "http://10.60.30.12:8300"
CONTROL_DIR = Path(__file__).resolve().parents[1]
FRAME_BYTES = 94


def validate(args: argparse.Namespace) -> None:
    for field in ("episode_id", "block_id"):
        value = getattr(args, field)
        if not isinstance(value, str) or not 1 <= len(value) <= 128:
            raise ProtocolError(f"invalid {field}")
        if value in (".", "..") or any(c in value for c in ("/", "\\", "\x00")):
            raise ProtocolError(f"unsafe {field}")
    if args.mode != "nominal":
        raise ProtocolError("H4 runner supports nominal conditions only")
    if type(args.node_id) is not int or not 0 <= args.node_id <= 65535:
        raise ProtocolError("invalid node_id")
    if (type(args.baseline_temperature_mc) is not int
            or not -(1 << 31) <= args.baseline_temperature_mc < (1 << 31)):
        raise ProtocolError("invalid temperature baseline")
    if type(args.humidity_mpct) is not int or not 0 <= args.humidity_mpct <= 100000:
        raise ProtocolError("invalid baseline humidity")
    if type(args.high_delta_mc) is not int or not 1 <= args.high_delta_mc <= 100000000:
        raise ProtocolError("invalid diagnostic threshold")
    if (not 0 < args.timeout_seconds <= 3600
            or not 0 < args.poll_seconds <= 60):
        raise ProtocolError("invalid polling parameters")


def _frame(value: object, *, node_id: int) -> bytes:
    if (not isinstance(value, str) or len(value) != FRAME_BYTES * 2
            or any(ch not in "0123456789abcdef" for ch in value)):
        raise ProtocolError("H4 evidence is not a canonical 94-byte frame")
    payload = bytes.fromhex(value)
    if payload[1] != 1 or int.from_bytes(payload[2:4], "big") != node_id:
        raise ProtocolError("H4 evidence is not target-node telemetry")
    return payload


def _sequence(payload: bytes) -> int:
    return int.from_bytes(payload[22:30], "big")


def verify_replay(event: dict, reference: dict,
                  args: argparse.Namespace) -> dict:
    if type(event.get("event_id")) is not int or event["event_id"] < 1:
        raise ProtocolError("invalid G2 replay event identifier")
    original = _frame(reference.get("frame_hex"), node_id=args.node_id)
    captured = _frame(event.get("reference_frame_hex"), node_id=args.node_id)
    trigger = _frame(event.get("trigger_frame_hex"), node_id=args.node_id)
    replayed = _frame(event.get("replayed_frame_hex"), node_id=args.node_id)

    if captured != original or replayed != original:
        raise ProtocolError("G2 replayed bytes differ from the selected signed frame")
    if trigger == original:
        raise ProtocolError("triggering frame is the same as the earlier frame")
    if trigger[6:22] != original[6:22]:
        raise ProtocolError("H4 triggering frame uses a different node boot identifier")
    if _sequence(trigger) <= _sequence(original):
        raise ProtocolError("H4 replay was not triggered by newer telemetry")
    for name, frame in (("reference", original), ("trigger", trigger)):
        temperature = int.from_bytes(frame[38:42], "big", signed=True)
        humidity = int.from_bytes(frame[42:46], "big")
        if (temperature != args.baseline_temperature_mc
                or humidity != args.humidity_mpct):
            raise ProtocolError(f"{name} frame differs from the nominal process")

    return {
        "event_id": event["event_id"],
        "reference_frame_hex": original.hex(),
        "trigger_frame_hex": trigger.hex(),
        "replayed_frame_hex": replayed.hex(),
        "reference_sequence_no": _sequence(original),
        "trigger_sequence_no": _sequence(trigger),
        "frame_bytes_preserved": True,
    }


def _matching_url(core: str, node_id: int, after_id: int, frame_hex: str) -> str:
    query = urlencode({
        "node_id": node_id, "after_id": after_id,
        "frame_hex": frame_hex,
    })
    return f"{core}/v1/telemetry/match?{query}"


def _wait_for_frame(request, url: str, *, node_id: int,
                    after_id: int, expected_frame: str,
                    mac_status: str, sequence_status: str,
                    temperature: int, humidity: int,
                    deadline: float, clock, sleeper,
                    poll_seconds: float) -> dict:
    while clock() < deadline:
        try:
            candidate = request("GET", url)
        except ApiError as exc:
            if exc.status != 404:
                raise
        else:
            if (type(candidate.get("telemetry_id")) is not int
                    or candidate["telemetry_id"] <= after_id
                    or candidate.get("node_id") != node_id
                    or candidate.get("frame_hex") != expected_frame
                    or candidate.get("mac_status") != mac_status
                    or candidate.get("sequence_status") != sequence_status
                    or candidate.get("temperature_mC") != temperature
                    or candidate.get("humidity_mPct") != humidity):
                raise ProtocolError("VM-CORE persisted unexpected H4 frame evidence")
            return candidate
        sleeper(poll_seconds)
    raise ProtocolError(f"VM-CORE frame with {sequence_status} status not observed before timeout")


def run_h4(args: argparse.Namespace, *, request=request_json,
           control=control_command, command=run_command,
           sleeper=time.sleep, clock=time.monotonic) -> dict:
    validate(args)
    out = args.output_root.expanduser().resolve() / args.episode_id
    out.mkdir(parents=True, exist_ok=False)

    core = args.core_url.rstrip("/")
    edge = args.edge_url.rstrip("/")
    nodes = args.nodes_control_url.rstrip("/")
    latest_url = f"{core}/v1/telemetry/latest?{urlencode({'node_id': args.node_id})}"
    edge_action = f"{edge}/v1/g2/intervention"
    edge_status = f"{edge_action}/status?{urlencode({'node_id': args.node_id})}"
    bias_status = f"{nodes}/v1/nodes/bias?{urlencode({'node_id': args.node_id})}"
    record_url = f"{core}/v1/diagnosis?{urlencode({'episode_id': args.episode_id})}"
    save_json(out / "parameters.json", {
        "episode_id": args.episode_id, "block_id": args.block_id,
        "node_id": args.node_id, "mode": args.mode,
        "baseline_temperature_mC": args.baseline_temperature_mc,
        "humidity_mPct": args.humidity_mpct,
        "high_delta_mC": args.high_delta_mc,
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

        initial_bias = request("GET", bias_status)
        _assert_bias(initial_bias, args.node_id, 0, 0)
        save_json(out / "initial_bias.json", initial_bias)

        prior_status = request("GET", edge_status)
        if prior_status.get("status") != "ok" or prior_status.get("node_id") != args.node_id:
            raise ProtocolError("G2 status is unavailable")
        if prior_status.get("pending_action") is not None:
            raise ProtocolError("G2 already has a pending action")
        old_event = (prior_status.get("last_replay") or {}).get("event_id", 0)
        if type(old_event) is not int or old_event < 0:
            raise ProtocolError("invalid prior replay event ID")
        save_json(out / "edge_initial_status.json", prior_status)

        try:
            latest = request("GET", latest_url)
            previous_id = latest.get("telemetry_id")
            if type(previous_id) is not int or previous_id < 1:
                raise ProtocolError("invalid initial telemetry ID")
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
        initial_truth = control(["private"], args.control_dir)
        if initial_truth.get("interventions") or initial_truth.get("private_steps"):
            raise ProtocolError("shared process was not reset")

        reference = _find_fresh_telemetry(
            request, latest_url, previous_id=previous_id, node_id=args.node_id,
            temperature=args.baseline_temperature_mc, humidity=args.humidity_mpct,
            deadline=clock() + args.timeout_seconds,
            clock=clock, sleeper=sleeper, poll_seconds=args.poll_seconds,
        )
        reference_bytes = _frame(reference.get("frame_hex"), node_id=args.node_id)
        if (reference.get("mac_status") != "valid"
                or reference.get("sequence_status") != "fresh"):
            raise ProtocolError("H4 source frame is not authenticated, fresh telemetry")
        if reference.get("sequence_no") != _sequence(reference_bytes):
            raise ProtocolError("source sequence does not match its raw frame")
        save_json(out / "reference_telemetry.json", reference)

        # G2 selects this already-forwarded exact frame from its bounded
        # history, eliminating a race with later arriving telemetry.
        action_may_be_pending = True
        armed = request("POST", edge_action, {
            "action": "replay", "node_id": args.node_id,
            "frame_hex": reference_bytes.hex(),
        })
        if armed.get("status") != "ok":
            raise ProtocolError("G2 did not acknowledge H4 replay staging")

        event = None
        deadline = clock() + args.timeout_seconds
        while clock() < deadline:
            state = request("GET", edge_status)
            pending_event = state.get("last_replay")
            if (state.get("status") == "ok"
                    and isinstance(pending_event, dict)
                    and type(pending_event.get("event_id")) is int
                    and pending_event["event_id"] > old_event):
                event = verify_replay(pending_event, reference, args)
                if state.get("pending_action") is not None:
                    raise ProtocolError("the G2 replay intervention was not consumed")
                break
            sleeper(args.poll_seconds)
        if event is None:
            raise ProtocolError("G2 H4 replay event was not observed")
        save_json(out / "edge_replay_evidence.json", event)

        trigger_url = _matching_url(
            core, args.node_id, reference["telemetry_id"],
            event["trigger_frame_hex"],
        )
        trigger = _wait_for_frame(
            request, trigger_url, node_id=args.node_id,
            after_id=reference["telemetry_id"],
            expected_frame=event["trigger_frame_hex"],
            mac_status="valid", sequence_status="fresh",
            temperature=args.baseline_temperature_mc,
            humidity=args.humidity_mpct,
            deadline=clock() + args.timeout_seconds,
            clock=clock, sleeper=sleeper, poll_seconds=args.poll_seconds,
        )
        if (trigger.get("sequence_no") != event["trigger_sequence_no"]
                or trigger.get("boot_id_hex") != reference.get("boot_id_hex")):
            raise ProtocolError("VM-CORE triggering frame identity does not match G2")
        save_json(out / "core_trigger_telemetry.json", trigger)

        replay_url = _matching_url(
            core, args.node_id, trigger["telemetry_id"],
            event["replayed_frame_hex"],
        )
        replayed = _wait_for_frame(
            request, replay_url, node_id=args.node_id,
            after_id=trigger["telemetry_id"],
            expected_frame=event["replayed_frame_hex"],
            mac_status="valid", sequence_status="stale",
            temperature=args.baseline_temperature_mc,
            humidity=args.humidity_mpct,
            deadline=clock() + args.timeout_seconds,
            clock=clock, sleeper=sleeper, poll_seconds=args.poll_seconds,
        )
        if (replayed.get("sequence_no") != event["reference_sequence_no"]
                or replayed.get("boot_id_hex") != reference.get("boot_id_hex")):
            raise ProtocolError("VM-CORE replayed frame identity differs from the reference")
        selected = replayed
        save_json(out / "core_replayed_telemetry.json", selected)

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
        record = request("GET", record_url)
        observed = record.get("telemetry")
        if (not isinstance(observed, dict)
                or record.get("episode_id") != args.episode_id
                or record.get("block_id") != args.block_id
                or record.get("node_id") != args.node_id
                or observed.get("telemetry_id") != selected["telemetry_id"]
                or observed.get("frame_hex") != event["replayed_frame_hex"]
                or observed.get("mac_status") != "valid"
                or observed.get("sequence_status") != "stale"):
            raise ProtocolError("persisted H4 diagnosis uses unexpected telemetry")
        save_json(out / "core_diagnosis_record.json", record)

        _assert_process(control(["state"], args.control_dir),
                        args.baseline_temperature_mc, args.humidity_mpct)
        truth = control(["private"], args.control_dir)
        if truth.get("interventions") or truth.get("private_steps"):
            raise ProtocolError("unexpected process intervention occurred during H4")
        save_json(out / "private_ground_truth.json", {
            "cause": "H4", "episode_id": args.episode_id,
            "block_id": args.block_id, "node_id": args.node_id,
            "reference_telemetry_id": reference["telemetry_id"],
            "trigger_telemetry_id": trigger["telemetry_id"],
            "replayed_telemetry_id": selected["telemetry_id"],
            "g2_replay_event": event, "private_process": truth,
        })
    except BaseException as exc:
        failure = exc
    finally:
        if action_may_be_pending:
            try:
                cleared = request("POST", edge_action, {
                    "action": "clear", "node_id": args.node_id,
                })
                if cleared.get("status") != "ok":
                    raise ProtocolError("G2 rejected H4 cleanup")
                status = request("GET", edge_status)
                if status.get("status") != "ok" or status.get("pending_action") is not None:
                    raise ProtocolError("G2 remains armed after H4 cleanup")
                save_json(out / "edge_cleanup.json", status)
            except BaseException as cleanup_error:
                old = f"{type(failure).__name__}: {failure}" if failure else "none"
                failure = ProtocolError(
                    f"experiment failure: {old}; G2 cleanup failed: {cleanup_error}"
                )
    if failure is not None:
        save_json(out / "failure.json", {
            "at_utc": utc_now(), "error": f"{type(failure).__name__}: {failure}",
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
    p = argparse.ArgumentParser(description="Run an H4 authenticated telemetry replay")
    p.add_argument("--episode-id", required=True)
    p.add_argument("--block-id", required=True)
    p.add_argument("--node-id", type=int, required=True)
    p.add_argument("--mode", choices=("nominal",), required=True)
    p.add_argument("--baseline-temperature-mc", type=int, required=True)
    p.add_argument("--high-delta-mc", type=int, required=True)
    p.add_argument("--humidity-mpct", type=int, default=50000)
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
        print(json.dumps(run_h4(parser().parse_args(argv)), sort_keys=True))
        return 0
    except (OSError, ProtocolError, subprocess.SubprocessError) as exc:
        print(f"H4 execution failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
