"""Scale the trace to the cluster and cut the live-experiment windows (plan §7.2).

Outputs
  data/processed/series.parquet   per-minute `invocations` and scaled `rps` for all 14 days
  data/processed/series.json      how the series was built (signal, scale factor, ...)
  loadgen/windows/<NAME>.json     the replay window and the history that precedes it
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from pas.config import Config, ConfigError, WindowConfig
from pas.data.trace import load_cached, load_invocations, signal_stats, sustained
from pas.io import write_text
from pas.timeseries import day_slice

PEAK_PERCENTILE = 99.5
SPIKE_CAP = 0.95
_EDGE_MINUTES = 15


def build_series(cfg: Config) -> pd.DataFrame:
    t = cfg.trace
    inv = load_cached(t.processed_dir, t.signal, t.trigger)
    if inv is None:
        inv = load_invocations(t.raw_dir, t.days, t.start_date, t.signal, t.trigger)
    raw_rps = inv / 60.0
    peak = float(np.percentile(raw_rps, PEAK_PERCENTILE))
    scale = cfg.capacity.target_peak_rps / peak
    df = pd.DataFrame({"invocations": inv, "rps": raw_rps * scale})
    t.processed_dir.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cfg.series_path)
    meta = {
        "signal": t.signal,
        "trigger": t.trigger,
        "scale_factor": scale,
        "target_peak_rps": cfg.capacity.target_peak_rps,
        "peak_percentile": PEAK_PERCENTILE,
        "stats": signal_stats(df["rps"]),
    }
    write_text(cfg.series_path.with_suffix(".json"), json.dumps(meta, indent=2))
    return df


def load_series(cfg: Config) -> pd.Series:
    if not cfg.series_path.exists():
        raise FileNotFoundError(f"{cfg.series_path} not found; run `pas data build` first")
    s = pd.read_parquet(cfg.series_path)["rps"]
    s.index = pd.DatetimeIndex(s.index, freq="min")
    return s


def split(cfg: Config, rps: pd.Series) -> dict[str, pd.Series]:
    t = cfg.trace
    return {name: day_slice(rps, lo, hi, t.start_date)
            for name, (lo, hi) in (("train", t.train_days), ("val", t.val_days),
                                   ("test", t.test_days))}


def _resolve_start(cfg: Config, w: WindowConfig, rps: pd.Series) -> pd.Timestamp:
    if w.day is None or w.start is None or w.minutes is None:
        raise ConfigError(f"window {w.name}: needs day, start and minutes (or base)")
    day = day_slice(rps, w.day, w.day, cfg.trace.start_date)
    if w.start.startswith("auto:"):
        mode = w.start.split(":", 1)[1]
        # Judge windows on sustained load (15-min median) so short bursts do not decide.
        v = sustained(day).to_numpy()
        starts = range(0, len(v) - w.minutes + 1, 5)
        if mode == "ramp":
            scores = [v[s + w.minutes - _EDGE_MINUTES:s + w.minutes].mean()
                      - v[s:s + _EDGE_MINUTES].mean() for s in starts]
        elif mode == "peak":
            scores = [v[s:s + w.minutes].mean() for s in starts]
        else:
            raise ConfigError(f"window {w.name}: unknown start mode {w.start!r}")
        return day.index[list(starts)[int(np.argmax(scores))]]
    hh, mm = (int(x) for x in w.start.split(":"))
    return day.index[0] + pd.Timedelta(hours=hh, minutes=mm)


def build_window(cfg: Config, name: str, rps: pd.Series) -> dict[str, Any]:
    w = cfg.window(name)
    if w.base:
        base = cfg.window(w.base)
        start, minutes = _resolve_start(cfg, base, rps), int(base.minutes)
    else:
        start, minutes = _resolve_start(cfg, w, rps), int(w.minutes)

    end = start + pd.Timedelta(minutes=minutes - 1)
    test = day_slice(rps, *cfg.trace.test_days, cfg.trace.start_date)
    if start < test.index[0] or end > test.index[-1]:
        raise ConfigError(f"window {name} ({start}..{end}) must lie inside test_days "
                          f"{cfg.trace.test_days}; models are trained on the other days")

    values = rps.loc[start:end].to_numpy().copy()
    spike_capped = False
    if w.spike:
        at, length = int(w.spike["at_minute"]), int(w.spike["minutes"])
        values[at:at + length] *= float(w.spike["factor"])
        # A spike the cluster could never serve tests maxReplicas, not the scaler: cap it at
        # SPIKE_CAP of full capacity.
        cap = SPIKE_CAP * cfg.capacity.max_replicas * cfg.capacity.per_pod_rps
        spike_capped = bool((values[at:at + length] > cap).any())
        values[at:at + length] = np.minimum(values[at:at + length], cap)

    warm = rps.loc[:start - pd.Timedelta(minutes=1)].iloc[-cfg.forecast.history_minutes:]
    return {
        "name": name,
        "description": w.description,
        "start": start.isoformat(),
        "step_seconds": 60,
        "minutes": minutes,
        "rps": [round(float(x), 3) for x in values],
        "warm_start_start": warm.index[0].isoformat() if len(warm) else start.isoformat(),
        "warm_start_rps": [round(float(x), 3) for x in warm.to_numpy()],
        "spike": w.spike,
        "spike_capped": spike_capped,
        "peak_rps": round(float(values.max()), 3),
        "peak_replicas_needed": cfg.required_replicas(float(values.max())),
    }


def write_windows(cfg: Config, rps: pd.Series, names: list[str] | None = None) -> list[Path]:
    cfg.windows_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for w in cfg.windows:
        if names and w.name not in names:
            continue
        data = build_window(cfg, w.name, rps)
        path = cfg.windows_dir / f"{w.name}.json"
        write_text(path, json.dumps(data, separators=(",", ":")))
        paths.append(path)
    return paths


def load_window(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text())
    for key in ("name", "start", "rps", "warm_start_rps"):
        if key not in data:
            raise ValueError(f"{path}: not a pas window file (missing {key!r})")
    return data


def window_series(window: dict[str, Any]) -> pd.Series:
    """Warm-start history followed by the window itself, on the trace clock."""
    warm = pd.Series(window["warm_start_rps"], dtype=float,
                     index=pd.date_range(window["warm_start_start"],
                                         periods=len(window["warm_start_rps"]), freq="min"))
    body = pd.Series(window["rps"], dtype=float,
                     index=pd.date_range(window["start"], periods=len(window["rps"]), freq="min"))
    return pd.concat([warm, body])
