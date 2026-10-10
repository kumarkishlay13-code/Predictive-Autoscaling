"""Per-run metrics and the cross-run comparison (plan §11).

Every run is reduced to one row of numbers on a common 1-minute grid aligned to the start of
the replay (minute i = [start + 60·i, start + 60·(i+1))). Ground-truth demand comes from the
window file, so it is exactly what k6 was asked to send.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats

from pas.config import Config

HEADLINE = "slo_violation_minutes"
COMPARE = ["slo_violation_minutes", "under_provisioned_%", "over_provisioning_replica_min",
           "replica_hours", "p95_ms", "error_rate_%", "scaling_actions"]


def per_minute(ts: pd.DataFrame, start: float, minutes: int) -> pd.DataFrame:
    """Resample 15-s samples onto the window's minute grid."""
    t0 = pd.Timestamp(start, unit="s")
    rel = (ts.index - t0) / pd.Timedelta(minutes=1)
    minute = np.floor(rel).astype(int)
    df = ts.assign(minute=minute)
    df = df[(df["minute"] >= 0) & (df["minute"] < minutes)]
    agg = {c: "mean" for c in ts.columns}
    # Conservative choices: worst latency within the minute, fewest ready replicas.
    for col in ("client_p95_s", "server_p95_s", "client_p99_s", "server_p99_s"):
        if col in agg:
            agg[col] = "max"
    if "ready_replicas" in agg:
        agg["ready_replicas"] = "min"
    return df.groupby("minute").agg(agg).reindex(range(minutes))


def run_metrics(cfg: Config, ts: pd.DataFrame, window: dict[str, Any], start: float,
                k6_summary: dict[str, Any] | None) -> dict[str, Any]:
    minutes = len(window["rps"]) if not k6_summary else int(k6_summary.get("minutes")
                                                              or len(window["rps"]))
    pm = per_minute(ts, start, minutes)
    offered = np.asarray(window["rps"][:minutes], dtype=float)
    required = np.array([cfg.required_replicas(x) for x in offered])
    ready = pm["ready_replicas"].ffill().fillna(0).to_numpy()

    latency_col = "client_p95_s" if pm.get("client_p95_s", pd.Series(dtype=float)).notna().any() \
        else "server_p95_s"
    latency_ms = 1000 * pm[latency_col].to_numpy()
    errors = pm.get("client_error_rate", pd.Series(0.0, index=pm.index)).fillna(0).to_numpy()
    violation = (np.nan_to_num(latency_ms, nan=0) > cfg.slo.p95_latency_ms) | \
                (errors > cfg.slo.error_rate)

    deficit = ready < required
    episodes = _episode_lengths(deficit)
    desired = ts.get("desired_replicas", pd.Series(dtype=float)).dropna()

    summary_p95 = _summary(k6_summary, "http_req_duration", "p(95)")
    summary_err = _summary(k6_summary, "http_req_failed", "rate")
    dropped = _summary(k6_summary, "dropped_iterations", "count") or 0.0
    return {
        "minutes": minutes,
        "slo_violation_minutes": int(violation.sum()),
        "under_provisioned_%": 100 * float(deficit.mean()),
        "deficit_episodes": len(episodes),
        "mean_deficit_episode_min": float(np.mean(episodes)) if episodes else 0.0,
        "over_provisioning_replica_min": float(np.clip(ready - required, 0, None).sum()),
        "replica_hours": float(ready.sum() / 60),
        "scaling_actions": int((desired.diff().fillna(0) != 0).sum()),
        "p95_ms": summary_p95 if summary_p95 is not None else float(np.nanmax(latency_ms)),
        "error_rate_%": 100 * (summary_err or 0.0),
        "dropped_iterations": float(dropped),
        "latency_source": latency_col,
        # A run whose load generator could not keep up does not measure the scaler.
        "valid": bool(dropped <= 0.001 * max(_summary(k6_summary, "http_reqs", "count") or 1, 1)),
    }


def _summary(summary: dict | None, metric: str, key: str) -> float | None:
    if not summary or not summary.get(metric):
        return None
    value = summary[metric].get(key)
    return None if value is None else float(value)


def _episode_lengths(flags: np.ndarray) -> list[int]:
    lengths, current = [], 0
    for f in flags:
        if f:
            current += 1
        elif current:
            lengths.append(current)
            current = 0
    if current:
        lengths.append(current)
    return lengths


# Across runs ----------------------------------------------------------------------------------

def collect(cfg: Config, results_dir: Path) -> pd.DataFrame:
    """One row per completed run in results_dir."""
    rows = []
    for meta_path in sorted(results_dir.glob("*/meta.json")):
        meta = json.loads(meta_path.read_text())
        if meta.get("status") != "ok":
            continue
        run_dir = meta_path.parent
        ts = pd.read_parquet(run_dir / "timeseries.parquet")
        window = json.loads((run_dir / "window.json").read_text())
        summary = meta.get("k6_summary")
        start = (summary or {}).get("started_at") or meta["started"]
        rows.append({"run_id": meta["run_id"], "scaler": meta["scaler"],
                     "window": meta["window"], "rep": meta["rep"],
                     "startup_delay": meta["startup_delay"],
                     **run_metrics(cfg, ts, window, float(start), summary)})
    return pd.DataFrame(rows)


def ci95(values: pd.Series) -> tuple[float, float]:
    v = values.dropna().to_numpy(dtype=float)
    if len(v) < 2:
        return float(v.mean()) if len(v) else math.nan, math.nan
    half = stats.t.ppf(0.975, len(v) - 1) * v.std(ddof=1) / math.sqrt(len(v))
    return float(v.mean()), float(half)


def compare(runs: pd.DataFrame, baseline: str) -> pd.DataFrame:
    """Mean ± 95% CI per (window, scaler) and a Mann-Whitney U test against `baseline`."""
    runs = runs[runs["valid"]]
    out = []
    for (window, scaler), group in runs.groupby(["window", "scaler"]):
        row: dict[str, Any] = {"window": window, "scaler": scaler, "n": len(group)}
        for metric in COMPARE:
            mean, half = ci95(group[metric])
            row[metric] = mean
            row[f"{metric}_ci"] = half
        base = runs[(runs["window"] == window) & (runs["scaler"] == baseline)][HEADLINE]
        if scaler != baseline and len(base) >= 2 and len(group) >= 2:
            res = stats.mannwhitneyu(group[HEADLINE], base, alternative="two-sided")
            row["p_vs_baseline"] = float(res.pvalue)
        out.append(row)
    return pd.DataFrame(out)


def markdown_table(table: pd.DataFrame) -> str:
    def fmt(row: pd.Series, metric: str) -> str:
        mean, half = row.get(metric), row.get(f"{metric}_ci")
        if mean is None or pd.isna(mean):
            return "–"
        return f"{mean:.1f}" if pd.isna(half) else f"{mean:.1f} ± {half:.1f}"

    cols = ["window", "scaler", "n", *COMPARE, "p_vs_baseline"]
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for _, row in table.iterrows():
        cells = [str(row["window"]), str(row["scaler"]), str(row["n"])]
        cells += [fmt(row, m) for m in COMPARE]
        p = row.get("p_vs_baseline")
        cells.append("–" if p is None or pd.isna(p) else f"{p:.3f}")
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)
