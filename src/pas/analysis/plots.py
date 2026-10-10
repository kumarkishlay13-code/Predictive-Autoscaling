"""Figures for the report. Requires the optional extra: pip install -e ".[plots]"."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from pas.config import Config
from pas.data.trace import sustained


def _plt():
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as exc:  # pragma: no cover
        raise ImportError('plots need matplotlib: pip install -e ".[plots]"') from exc
    return plt


def plot_series(cfg: Config, rps: pd.Series, window_paths: list[Path]) -> Path:
    """The whole scaled trace with train/val/test shading and the replay windows marked."""
    plt = _plt()
    fig, ax = plt.subplots(figsize=(14, 4))
    ax.plot(rps.index, rps.to_numpy(), lw=0.6, color="#3b6ea5")
    t0 = pd.Timestamp(cfg.trace.start_date)
    for (lo, hi), label, color in ((cfg.trace.val_days, "validation", "#f2c14e"),
                                   (cfg.trace.test_days, "test", "#e07a5f")):
        ax.axvspan(t0 + pd.Timedelta(days=lo - 1), t0 + pd.Timedelta(days=hi), color=color,
                   alpha=0.15, label=label)
    for path in window_paths:
        w = json.loads(path.read_text())
        start = pd.Timestamp(w["start"])
        ax.axvspan(start, start + pd.Timedelta(minutes=len(w["rps"])), color="black", alpha=0.25)
        ax.annotate(w["name"], (start, ax.get_ylim()[1] * 0.92), fontsize=8)
    ax.set_ylabel("requests per second (scaled)")
    ax.set_title("Load series from the Azure Functions 2019 trace")
    ax.legend(loc="upper left")
    fig.tight_layout()
    out = cfg.reports_dir / "figures" / "series.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return out


def plot_apps(cfg: Config, table: pd.DataFrame, invocations: pd.DataFrame,
              n: int = 6, days: int = 3) -> Path:
    """Small multiples of the top-ranked apps: raw per-minute load and its 15-minute mean."""
    plt = _plt()
    apps = list(table["app"])[:n]
    fig, axes = plt.subplots(len(apps), 1, figsize=(13, 2.1 * len(apps)), sharex=True)
    axes = np.atleast_1d(axes)
    span = slice(0, days * 1440)
    for ax, (_, row) in zip(axes, table.head(len(apps)).iterrows(), strict=True):
        rps = invocations[row["app"]].iloc[span] / 60.0
        ax.plot(rps.index, rps.to_numpy(), lw=0.4, color="#9db4cf")
        ax.plot(rps.index, sustained(rps).to_numpy(), lw=1.2, color="#1f4e79")
        ax.set_title(f"{row['app'][:12]}…  score {row['score']:.2f}  sustained range "
                     f"{row['sustained_range']:.1f}x  burstiness {row['burstiness']:.1f}  "
                     f"daily autocorr {row['daily_autocorr']:.2f}", fontsize=8, loc="left")
        ax.set_ylabel("RPS", fontsize=8)
    axes[-1].set_xlabel(f"first {days} days (light: per minute, dark: 15-minute median)")
    fig.tight_layout()
    out = cfg.reports_dir / "figures" / "apps.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=130)
    plt.close(fig)
    return out


def plot_run(cfg: Config, run_dir: Path, out_dir: Path) -> Path:
    """Offered vs forecast load, replicas vs required, and latency for one run."""
    plt = _plt()
    meta = json.loads((run_dir / "meta.json").read_text())
    window = json.loads((run_dir / "window.json").read_text())
    ts = pd.read_parquet(run_dir / "timeseries.parquet")
    start = pd.Timestamp(((meta.get("k6_summary") or {}).get("started_at")
                          or meta["started"]), unit="s")
    minutes = (ts.index - start) / pd.Timedelta(minutes=1)
    offered = np.asarray(window["rps"], dtype=float)
    grid = np.arange(len(offered))

    fig, axes = plt.subplots(3, 1, figsize=(11, 8), sharex=True)
    ax = axes[0]
    ax.step(grid, offered, where="post", label="offered (trace)", color="black", lw=1)
    ax.plot(minutes, ts["offered_rps"], label="observed", alpha=0.7)
    if ts["predicted_rps"].notna().any():
        ax.plot(minutes, ts["predicted_rps"], label="predicted (lead window)", ls="--")
    ax.set_ylabel("RPS")
    ax.legend(loc="upper left", fontsize=8)

    ax = axes[1]
    required = [cfg.required_replicas(x) for x in offered]
    ax.step(grid, required, where="post", label="required", color="black", lw=1)
    ax.plot(minutes, ts["ready_replicas"], label="ready")
    ax.plot(minutes, ts["desired_replicas"], label="desired", ls=":")
    ax.set_ylabel("replicas")
    ax.legend(loc="upper left", fontsize=8)

    ax = axes[2]
    col = "client_p95_s" if ts["client_p95_s"].notna().any() else "server_p95_s"
    ax.plot(minutes, 1000 * ts[col], label=f"p95 ({col.split('_')[0]})")
    ax.axhline(cfg.slo.p95_latency_ms, color="red", lw=1, ls="--", label="SLO")
    ax.set_ylabel("ms")
    ax.set_xlabel("minutes since start of window")
    ax.legend(loc="upper left", fontsize=8)
    ax.set_xlim(-2, len(offered) + 2)

    fig.suptitle(f"{meta['run_id']}: scaler {meta['scaler']}, window {meta['window']}")
    fig.tight_layout()
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{meta['run_id']}.png"
    fig.savefig(out, dpi=130)
    plt.close(fig)
    return out
