"""Paired distributed evaluation metrics and block-stratified resampling."""
from __future__ import annotations

import csv
import json
from pathlib import Path
import random

from distributed_blocks import (
    Episode, HYPOTHESES, POLICIES, ProtocolError, ROOT, _completed, _json, save_json,
)


def _decision_loss(decision: str, cause: str, cfg: dict) -> float:
    d = cfg["decision"]
    if decision == cause:
        return 0.0
    if decision == "unresolved":
        return float(d["unresolved_loss"])
    if decision in ("H1_or_H2", "H3_or_H4"):
        group = ("H1", "H2") if decision == "H1_or_H2" else ("H3", "H4")
        return float(d["class_correct_loss"] if cause in group
                     else d["class_incorrect_loss"])
    if cause in ("H3", "H4") and decision in ("H1", "H2"):
        return float(d["missed_attack_loss"])
    if cause in ("H1", "H2") and decision in ("H3", "H4"):
        return float(d["false_attack_loss"])
    if decision in HYPOTHESES:
        return float(d["wrong_label_loss"])
    raise ProtocolError("unknown policy decision")


def _config(root: Path) -> dict:
    import base64
    import io
    import zipfile
    archive = root.parent / "core/vendor/e0/e0_source.zip.b64"
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(
            archive.read_text(encoding="ascii")))) as z:
        names = [n for n in z.namelist() if n.endswith("/config/e0_config.json")]
        if len(names) != 1:
            raise ProtocolError("E0 configuration unavailable in committed archive")
        return json.loads(z.read(names[0]))


def _records(plan: list[Episode], output_root: Path, cfg: dict) -> list[dict]:
    rows = []
    for episode in plan:
        out = output_root / episode.episode_id
        if not _completed(out, episode):
            raise ProtocolError(f"episode is not complete: {episode.episode_id}")
        evaluation = _json(out / "policy_evaluation.json")
        for result in evaluation["policies"]:
            prob = result["probabilities_h"]
            if (not isinstance(prob, list) or len(prob) != 4
                    or not 0.999 <= sum(prob) <= 1.001):
                raise ProtocolError("invalid posterior probability vector")
            truth_idx = HYPOTHESES.index(episode.cause)
            loss = _decision_loss(result["decision"], episode.cause, cfg)
            brier = sum((float(prob[j]) - (1.0 if j == truth_idx else 0.0)) ** 2
                        for j in range(4))
            rows.append({
                "seed": episode.seed, "block_id": episode.block,
                "episode_id": episode.episode_id, "mode": episode.mode,
                "cause": episode.cause, "baseline_temperature_mC": episode.baseline,
                "reference_unavailable": episode.reference_down,
                "node_unavailable": episode.node_down,
                "probe_delay_ms": episode.delay_ms,
                "telemetry_id": evaluation["telemetry_id"],
                "policy": result["policy"], "decision": result["decision"],
                "probabilities_h": json.dumps(prob, separators=(",", ":")),
                "correct_exact": int(result["decision"] == episode.cause),
                "exact_coverage": int(result["decision"] in HYPOTHESES),
                "realized_loss": loss, "cost_observed": float(result["cost_observed"]),
                "loss_plus_cost": loss + float(result["cost_observed"]),
                "probe_count": int(result["probe_count"]),
                "latency_ms": float(result["latency_ms"]),
                "bytes": int(result["bytes"]), "brier": brier,
                "action_trace": json.dumps(result["trace"], separators=(",", ":")),
                "passive_evidence": json.dumps(evaluation["passive_evidence"], separators=(",", ":")),
                "evidence": json.dumps(evaluation["observations"], separators=(",", ":")),
                "model_sha256": evaluation["model_sha256"],
                "config_sha256": evaluation["config_sha256"],
            })
    return rows


def _macro_f1(rows: list[dict]) -> float:
    result = []
    for cause in HYPOTHESES:
        tp = sum(r["cause"] == cause and r["decision"] == cause for r in rows)
        fp = sum(r["cause"] != cause and r["decision"] == cause for r in rows)
        fn = sum(r["cause"] == cause and r["decision"] != cause for r in rows)
        result.append((2 * tp / (2 * tp + fp + fn)) if (2 * tp + fp + fn) else 0.0)
    return sum(result) / 4


def _metrics(rows: list[dict]) -> dict:
    if not rows:
        raise ProtocolError("cannot summarize empty evaluation")
    n = len(rows)
    fields = ("correct_exact", "exact_coverage", "realized_loss", "loss_plus_cost",
              "probe_count", "cost_observed", "latency_ms", "bytes", "brier")
    output = {k: sum(float(r[k]) for r in rows) / n for k in fields}
    output["macro_f1"] = _macro_f1(rows)
    output["episodes"] = n
    return output


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    v = (len(ordered) - 1) * q
    a = int(v)
    b = min(a + 1, len(ordered) - 1)
    return ordered[a] * (1 - (v - a)) + ordered[b] * (v - a)


def _report(rows: list[dict], replicates: int, bootstrap_seed: int) -> dict:
    grouped = {}
    for r in rows:
        grouped.setdefault((r["mode"], r["block_id"], r["policy"]), []).append(r)
    by_mode = {mode: sorted({key[1] for key in grouped if key[0] == mode})
               for mode in ("nominal", "degraded")}
    for mode, blocks in by_mode.items():
        for block in blocks:
            for policy in POLICIES:
                subset = grouped.get((mode, block, policy), [])
                if len(subset) != 4 or {r["cause"] for r in subset} != set(HYPOTHESES):
                    raise ProtocolError("a paired block lacks one of H1-H4 or a policy")
    rng = random.Random(bootstrap_seed)
    outputs = {}
    for mode, blocks in by_mode.items():
        if not blocks:
            raise ProtocolError("missing operating mode")
        per_policy = {
            policy: _metrics([r for block in blocks
                              for r in grouped[(mode, block, policy)]])
            for policy in POLICIES
        }
        tracks = {policy: {k: [] for k in per_policy[policy] if k != "episodes"}
                  for policy in POLICIES}
        paired = {policy: {k: [] for k in tracks[policy]}
                  for policy in POLICIES if policy != "B6_EACLD"}
        for _ in range(replicates):
            selected = rng.choices(blocks, k=len(blocks))
            replicate = {
                p: _metrics([r for b in selected for r in grouped[(mode, b, p)]])
                for p in POLICIES
            }
            b6 = replicate["B6_EACLD"]
            for policy in POLICIES:
                for metric in tracks[policy]:
                    value = replicate[policy][metric]
                    tracks[policy][metric].append(value)
                    if policy != "B6_EACLD":
                        paired[policy][metric].append(value - b6[metric])
        outputs[mode] = {
            "blocks": len(blocks), "episodes_per_policy": len(blocks) * 4,
            "policies": {
                p: {
                    "point": per_policy[p],
                    "confidence_95": {
                        k: [_percentile(v, .025), _percentile(v, .975)]
                        for k, v in tracks[p].items()
                    },
                    **({"difference_from_B6_confidence_95": {
                        k: [_percentile(v, .025), _percentile(v, .975)]
                        for k, v in paired[p].items()
                    }} if p != "B6_EACLD" else {}),
                } for p in POLICIES
            },
        }
    return {"bootstrap_replicates": replicates,
            "bootstrap_seed": bootstrap_seed,
            "resampling_unit": "matched intervention block stratified by mode",
            "results": outputs}


def finalize(plan, output_root: Path, *, bootstrap_replicates: int,
             bootstrap_seed: int) -> dict:
    cfg = _config(ROOT)
    rows = _records(plan, output_root, cfg)
    result_path = output_root / "distributed_policy_results.csv"
    with result_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    result = _report(rows, bootstrap_replicates, bootstrap_seed)
    result["episodes"] = len(plan)
    result["policy_evaluations"] = len(rows)
    result["seeds"] = sorted(set(e.seed for e in plan))
    result["source_file"] = str(result_path)
    save_json(output_root / "distributed_policy_summary.json", result)
    return {"episodes": len(plan), "policy_evaluations": len(rows),
            "results_csv": str(result_path),
            "summary": str(output_root / "distributed_policy_summary.json")}
