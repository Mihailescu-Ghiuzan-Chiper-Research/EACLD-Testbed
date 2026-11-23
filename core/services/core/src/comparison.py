"""Paired policy evaluation using one acquisition of actual distributed evidence.

The cache contains observations returned by real NET-PROBE requests and
VM-CORE's actual message/sequence verifier. The causal ground truth is absent.
"""
from __future__ import annotations

import hashlib
import json
import time

import numpy as np

from .live_env import LiveEnvironment

POLICIES = (
    "B0_passive_P0", "B1_passive_P1", "B2_exhaustive", "B3_random",
    "B4_entropy_greedy", "B5_naive_independence", "B6_EACLD",
)


class RecordedEnvironment:
    def __init__(self, public: dict, observations: dict):
        self.public = public
        self._observations = observations
        self._used: set[str] = set()

    def execute(self, action: str) -> tuple[str, dict]:
        if action in self._used:
            raise ValueError("duplicate policy action")
        self._used.add(action)
        result = self._observations[action]
        return result["outcome"], dict(result["metadata"])


def evaluate_policies(*, engine, cfg: dict, reading, verifier,
                      episode_id: str, block_id: str, mode: str,
                      baseline_temperature_mC: int, high_delta_mC: int,
                      random_seed: int) -> dict:
    if type(random_seed) is not int or not 0 <= random_seed < 1 << 63:
        raise ValueError("random seed must be a nonnegative 63-bit integer")
    live = LiveEnvironment(
        reading=reading, verifier=verifier, cfg=cfg,
        episode_id=episode_id, block_id=block_id, mode=mode,
        baseline_temperature_mC=baseline_temperature_mC,
        high_delta_mC=high_delta_mC,
    )
    # VM-CORE has already verified MAC and sequence status at ingestion.
    # Use these actual P1 observations consistently for every policy.
    passive = {
        "passive_mac": (reading.mac_status if reading.mac_status in ("valid", "invalid") else None),
        "passive_sequence": (reading.sequence_status if reading.sequence_status in ("fresh", "stale") else None),
    }
    live.public["passive"] = passive
    start = time.monotonic_ns()
    observations = {}
    for action in cfg["actions"]:
        outcome, metadata = live.execute(action)
        observations[action] = {"outcome": outcome, "metadata": metadata}
    acquisition_ms = (time.monotonic_ns() - start) / 1_000_000

    evaluated = []
    for policy in POLICIES:
        hash_bytes = hashlib.sha256(f"{episode_id}|{policy}".encode()).digest()[:8]
        state = (random_seed + int.from_bytes(hash_bytes, "little")) % (1 << 63)
        env = RecordedEnvironment(dict(live.public), observations)
        evaluated.append(engine.run(policy, env, np.random.default_rng(state)))

    def digest(value):
        raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    model = engine.model
    return {
        "model_sha256": digest({"priors": model.priors, "emissions": model.emissions}),
        "config_sha256": digest(cfg),
        "episode_id": episode_id,
        "block_id": block_id,
        "node_id": reading.node_id,
        "telemetry_id": reading.id,
        "mode": mode,
        "random_seed": random_seed,
        "evidence_acquisition_latency_ms": acquisition_ms,
        "passive_evidence": passive,
        "observations": observations,
        "policies": evaluated,
    }
