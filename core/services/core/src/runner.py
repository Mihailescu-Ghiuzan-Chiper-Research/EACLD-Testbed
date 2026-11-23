"""Adapter into the preserved E0 Bayesian core; does not reimplement it."""
from __future__ import annotations

import json
from pathlib import Path
import numpy as np

from eacld.core import FittedModel, PolicyEngine

from .live_env import LiveEnvironment


def load_engine(*, config: str | Path, fitted: str | Path) -> tuple[PolicyEngine, dict]:
    cfg = json.loads(Path(config).read_text(encoding="utf-8"))
    values = json.loads(Path(fitted).read_text(encoding="utf-8"))
    if set(values) != {"priors", "emissions"}:
        raise ValueError("fitted E0 model has incompatible fields")
    model = FittedModel(priors=values["priors"], emissions=values["emissions"], cfg=cfg)
    return PolicyEngine(model, cfg), cfg


class DiagnosisRunner:
    def __init__(self, *, engine: PolicyEngine, config: dict, store, verifier):
        self.engine = engine
        self.config = config
        self.store = store
        self.verifier = verifier

    def diagnose(self, *, node_id: int, mode: str, episode_id: str,
                 block_id: str, baseline_temperature_mC: int,
                 high_delta_mC: int,
                 expected_telemetry_id: int | None = None,
                 selected_telemetry_id: int | None = None) -> dict:
        if type(node_id) is not int or not 0 <= node_id <= 65535:
            raise ValueError("invalid node_id")
        if mode not in self.config["modes"]:
            raise ValueError("invalid E0 operating regime")
        if not isinstance(episode_id, str) or not 1 <= len(episode_id) <= 128:
            raise ValueError("invalid episode_id")
        if not isinstance(block_id, str) or not 1 <= len(block_id) <= 128:
            raise ValueError("invalid block_id")
        if selected_telemetry_id is not None:
            reading = self.store.by_id(node_id, selected_telemetry_id)
        else:
            reading = self.store.latest(node_id)
        if reading is None:
            raise LookupError("no accepted telemetry for requested node/identifier")
        if expected_telemetry_id is not None and reading.id != expected_telemetry_id:
            from .experiment_api import TelemetryChanged
            raise TelemetryChanged("telemetry changed before diagnosis")
        live = LiveEnvironment(
            reading=reading, verifier=self.verifier, cfg=self.config,
            episode_id=episode_id, block_id=block_id, mode=mode,
            baseline_temperature_mC=baseline_temperature_mC,
            high_delta_mC=high_delta_mC,
        )
        # B6 is deterministic; the seed does not synthesize probe results.
        result = self.engine.run("B6_EACLD", live, np.random.default_rng(0))
        self.store.record_diagnosis(result, node_id=node_id, telemetry_id=reading.id)
        return result
