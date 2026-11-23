"""VM-CONTROL operator CLI; no privileged commands are exposed on NET-PLANT.

Agent health requests use the VM-CONTROL source host and NET-CONTROL
addresses. Remote configuration commands await the control-agent API
contract, which is not specified in architecture V1.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time
import urllib.request

from .injector import ProcessIntervention
from .plant import PlantParameters, PlantState
from .private_store import DEFAULT_DB_PATH, PrivateStore

AGENTS = {
    "nodes": "10.60.30.12",
    "reference": "10.60.30.13",
    "edge": "10.60.30.14",
    "core": "10.60.30.15",
}


def check_agent_health(name: str, timeout: float = 3.0) -> dict:
    if name not in AGENTS:
        raise ValueError(f"unknown agent {name!r}")
    url = f"http://{AGENTS[name]}:8300/healthz"
    with urllib.request.urlopen(url, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"agent returned HTTP {response.status}")
        return json.load(response)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Private EACLD control orchestration")
    parser.add_argument("--db", default=os.environ.get("EACLD_PRIVATE_DB", DEFAULT_DB_PATH))
    sub = parser.add_subparsers(dest="operation", required=True)
    reset = sub.add_parser("reset", help="initialize a new deterministic run")
    reset.add_argument("--temperature-mC", type=int, default=22_000)
    reset.add_argument("--humidity-mPct", type=int, default=50_000)
    reset.add_argument("--ambient-temperature-mC", type=int, default=22_000)
    reset.add_argument("--ambient-humidity-mPct", type=int, default=50_000)
    reset.add_argument("--temperature-relaxation-ppm", type=int, default=50_000)
    reset.add_argument("--humidity-relaxation-ppm", type=int, default=30_000)
    reset.add_argument("--heater-gain-mC-per-unit", type=int, default=250)
    heat = sub.add_parser("set-heater", help="change private simulated heater input")
    heat.add_argument("--units", type=int, required=True)
    step = sub.add_parser("step", help="advance the process by an exact step count")
    step.add_argument("--count", type=int, default=1)
    run = sub.add_parser("run", help="advance steps at an operator-selected interval")
    run.add_argument("--count", type=int, required=True)
    run.add_argument("--interval-ms", type=int, required=True)
    inject = sub.add_parser("inject", help="schedule an H1 process-level intervention")
    inject.add_argument("--start-step", type=int, required=True)
    inject.add_argument("--end-step", type=int, required=True)
    inject.add_argument("--temperature-disturbance-mC", type=int, default=0)
    inject.add_argument("--humidity-disturbance-mPct", type=int, default=0)
    sub.add_parser("state", help="print public process observation")
    sub.add_parser("private", help="inspect private run state from VM-CONTROL")
    agent = sub.add_parser("agent-health", help="check NET-CONTROL agent health")
    agent.add_argument("name", choices=tuple(AGENTS))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = PrivateStore(Path(args.db))
    if args.operation == "reset":
        store.reset(
            PlantState(0, args.temperature_mC, args.humidity_mPct),
            PlantParameters(
                ambient_temperature_mC=args.ambient_temperature_mC,
                ambient_humidity_mPct=args.ambient_humidity_mPct,
                temperature_relaxation_ppm=args.temperature_relaxation_ppm,
                humidity_relaxation_ppm=args.humidity_relaxation_ppm,
                heater_gain_mC_per_unit=args.heater_gain_mC_per_unit,
            ),
        )
        result = store.public_state()
    elif args.operation == "set-heater":
        store.set_heater(args.units)
        result = store.public_state()
    elif args.operation == "step":
        result = store.step(args.count)
    elif args.operation == "run":
        if not 1 <= args.count <= 100_000 or args.interval_ms < 0:
            raise ValueError("invalid run count or interval")
        next_deadline = time.monotonic()
        for _ in range(args.count):
            result = store.step()
            next_deadline += args.interval_ms / 1000
            time.sleep(max(0.0, next_deadline - time.monotonic()))
    elif args.operation == "inject":
        store.schedule(ProcessIntervention(
            start_step=args.start_step,
            end_step=args.end_step,
            temperature_disturbance_mC=args.temperature_disturbance_mC,
            humidity_disturbance_mPct=args.humidity_disturbance_mPct,
        ))
        result = {"scheduled": True}
    elif args.operation == "state":
        result = store.public_state()
    elif args.operation == "private":
        result = store.private_snapshot()
    elif args.operation == "agent-health":
        result = check_agent_health(args.name)
    else:
        raise AssertionError("unhandled subcommand")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
