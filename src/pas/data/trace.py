"""Azure Functions Trace 2019: download, extract and turn into a per-minute load series.

Files: invocations_per_function_md.anon.dXX.csv, one per day, columns
HashOwner, HashApp, HashFunction, Trigger, 1..1440 (invocations per minute).
CSVs are read in chunks so the pipeline fits in a few hundred MB of RAM.
"""

from __future__ import annotations

import sys
import tarfile
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from pas.timeseries import MINUTES_PER_DAY

FILE_PATTERN = "invocations_per_function_md.anon.d{day:02d}.csv"
MINUTE_COLS = [str(i) for i in range(1, MINUTES_PER_DAY + 1)]
_CHUNK_ROWS = 4000


def day_file(raw_dir: Path, day: int) -> Path:
    return raw_dir / FILE_PATTERN.format(day=day)


def download(url: str, raw_dir: Path, force: bool = False) -> list[Path]:
    """Download the archive (≈143 MB) and extract only the invocation-count files."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    wanted = [day_file(raw_dir, d) for d in range(1, 15)]
    if not force and all(p.exists() for p in wanted):
        print(f"trace already present in {raw_dir}")
        return wanted

    archive = raw_dir / url.rsplit("/", 1)[-1]
    if force or not archive.exists():
        tmp = archive.with_suffix(archive.suffix + ".part")
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            done = 0
            with tmp.open("wb") as fh:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    fh.write(chunk)
                    done += len(chunk)
                    if total:
                        sys.stderr.write(f"\rdownloading {done / 1e6:6.1f}/{total / 1e6:.1f} MB")
        sys.stderr.write("\n")
        tmp.replace(archive)

    print(f"extracting invocation files from {archive.name} ...")
    with tarfile.open(archive, mode="r:xz") as tar:
        for member in tar:
            name = Path(member.name).name
            if member.isfile() and name.startswith("invocations_per_function_md"):
                src = tar.extractfile(member)
                if src is None:
                    continue
                with (raw_dir / name).open("wb") as dst:
                    while block := src.read(1 << 20):
                        dst.write(block)
    missing = [p.name for p in wanted if not p.exists()]
    if missing:
        raise RuntimeError(f"archive did not contain: {', '.join(missing)}")
    return wanted


def _chunks(raw_dir: Path, day: int, trigger: str | None):
    path = day_file(raw_dir, day)
    if not path.exists():
        raise FileNotFoundError(f"{path} not found; run `pas data download` first")
    for chunk in pd.read_csv(path, chunksize=_CHUNK_ROWS):
        if trigger:
            chunk = chunk[chunk["Trigger"] == trigger]
        if not chunk.empty:
            yield chunk


def load_invocations(raw_dir: Path, days: int, start_date: str, signal: str = "total",
                     trigger: str | None = None) -> pd.Series:
    """Invocations per minute for the chosen signal ("total" or one HashApp)."""
    t0 = pd.Timestamp(start_date)
    parts = []
    for day in range(1, days + 1):
        acc = np.zeros(MINUTES_PER_DAY)
        for chunk in _chunks(raw_dir, day, trigger):
            if signal != "total":
                chunk = chunk[chunk["HashApp"] == signal]
            acc += chunk[MINUTE_COLS].to_numpy(dtype=float).sum(axis=0)
        index = pd.date_range(t0 + pd.Timedelta(days=day - 1), periods=MINUTES_PER_DAY,
                              freq="min")
        parts.append(pd.Series(acc, index=index))
    series = pd.concat(parts)
    series.name = "invocations"
    if series.sum() == 0:
        raise ValueError(f"signal {signal!r} (trigger={trigger}) has no invocations")
    return series


def rank_apps(raw_dir: Path, days: int, start_date: str, trigger: str | None = None,
              top: int = 20, candidates: int = 60) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Rank the busiest apps as load signals.

    Returns (ranking table, per-minute invocations of every candidate). The second frame is
    cached by the CLI so `pas data build` with an app signal does not re-read the trace.
    """
    totals: dict[str, float] = {}
    for day in range(1, days + 1):
        for chunk in _chunks(raw_dir, day, trigger):
            sums = chunk[MINUTE_COLS].sum(axis=1).groupby(chunk["HashApp"]).sum()
            for app, value in sums.items():
                totals[app] = totals.get(app, 0.0) + float(value)
    busiest = sorted(totals, key=totals.get, reverse=True)[:candidates]

    per_app = {app: np.zeros(days * MINUTES_PER_DAY) for app in busiest}
    for day in range(1, days + 1):
        sl = slice((day - 1) * MINUTES_PER_DAY, day * MINUTES_PER_DAY)
        for chunk in _chunks(raw_dir, day, trigger):
            chunk = chunk[chunk["HashApp"].isin(per_app)]
            if chunk.empty:
                continue
            grouped = chunk.groupby("HashApp")[MINUTE_COLS].sum()
            for app, values in grouped.iterrows():
                per_app[app][sl] += values.to_numpy(dtype=float)
    index = pd.date_range(pd.Timestamp(start_date), periods=days * MINUTES_PER_DAY, freq="min")
    invocations = pd.DataFrame(per_app, index=index)
    rows = [{"app": app, **signal_stats(invocations[app] / 60.0)} for app in busiest]
    table = pd.DataFrame(rows).sort_values("score", ascending=False).head(top)
    return table.reset_index(drop=True), invocations


def cache_path(processed_dir: Path, trigger: str | None) -> Path:
    return processed_dir / f"apps-{trigger or 'all'}.parquet"


def load_cached(processed_dir: Path, signal: str, trigger: str | None) -> pd.Series | None:
    """The app's invocations from the `pas data apps` cache, or None if not cached."""
    path = cache_path(processed_dir, trigger)
    if signal == "total" or not path.exists():
        return None
    cached = pd.read_parquet(path)
    if signal not in cached.columns:
        return None
    series = cached[signal].astype(float)
    series.index = pd.DatetimeIndex(series.index, freq="min")
    series.name = "invocations"
    return series


def sustained(rps: pd.Series, minutes: int = 15) -> pd.Series:
    """Burst-robust level of the load: rolling median over `minutes`."""
    return rps.rolling(minutes, min_periods=1).median()


def signal_stats(rps: pd.Series) -> dict[str, float]:
    """Summary used to choose a signal.

    What matters for comparing scalers is the *sustained* daily swing (it drives scaling) and
    its regularity (it makes prediction possible), not one-minute bursts. Sustained load is the
    15-minute rolling median, which a burst of a few minutes cannot move.
    """
    v = rps.to_numpy(dtype=float)
    mean = float(v.mean())
    smooth = sustained(rps)
    sv = smooth.to_numpy()
    lo, hi = np.percentile(sv, [5, 95])
    # Floors keep the ratios finite for apps that go idle at night.
    sustained_range = float(min(hi / max(lo, 0.01 * hi), 100.0)) if hi > 0 else 0.0
    burstiness = float(np.percentile(v / np.maximum(sv, 0.01 * max(mean, 1e-9)), 99.9))
    long = len(v) > 2 * MINUTES_PER_DAY
    daily_acf = float(smooth.autocorr(lag=MINUTES_PER_DAY)) if long else 0.0
    zero_pct = float(100 * np.mean(v == 0))
    cv = float(v.std() / mean) if mean > 0 else float("inf")
    # Regular (daily autocorrelation) x wide sustained range (log scale, saturating at 16x)
    # x rarely idle.
    range_term = min(np.log2(max(sustained_range, 1.0)), 4.0) / 4.0
    score = max(daily_acf, 0.0) * range_term * (1 - zero_pct / 100)
    return {"mean_rps": mean, "peak_rps": float(v.max()), "sustained_range": sustained_range,
            "burstiness": burstiness, "cv": cv, "zero_minutes_%": zero_pct,
            "daily_autocorr": daily_acf, "score": score}
