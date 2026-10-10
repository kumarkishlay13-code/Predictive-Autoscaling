"""Rolling-origin backtest and forecast-accuracy metrics (plan §8.6).

At each origin t (every `stride` minutes across the test period) the model sees only data up
to t and forecasts t+1..t+H. Errors are reported per horizon. MASE is scaled by the in-sample
seasonal-naive MAE of the training data (Hyndman & Koehler, 2006), so MASE < 1 means "better
than same-minute-yesterday".
"""

from __future__ import annotations

import time
from collections.abc import Iterable

import numpy as np
import pandas as pd

from pas.models.base import Forecaster
from pas.timeseries import MINUTES_PER_DAY, fill_gaps


def mase_scale(train: pd.Series, season: int = MINUTES_PER_DAY) -> float:
    v = fill_gaps(train).to_numpy()
    if len(v) <= season:
        raise ValueError("training series must be longer than one season to compute MASE")
    scale = float(np.mean(np.abs(v[season:] - v[:-season])))
    if scale == 0:
        raise ValueError("training series repeats exactly every season; MASE is undefined")
    return scale


def rolling_forecasts(model: Forecaster, series: pd.Series, start: pd.Timestamp,
                      end: pd.Timestamp, horizon: int, stride: int,
                      quantile: float | None = None,
                      context: int | None = None) -> pd.DataFrame:
    """Forecast from every `stride`-th origin in [start, end - horizon]. Returns long format."""
    series = series.astype(float)
    positions = np.flatnonzero((series.index >= start) & (series.index <= end))
    if len(positions) == 0:
        raise ValueError("no data between start and end")
    rows: list[dict] = []
    last_origin = positions[-1] - horizon
    for pos in range(positions[0], last_origin + 1, stride):
        lo = 0 if context is None else max(0, pos + 1 - context)
        history = series.iloc[lo:pos + 1]
        truth = series.iloc[pos + 1:pos + 1 + horizon].to_numpy()
        t0 = time.perf_counter()
        pred = model.predict(history, horizon, quantile=quantile)
        elapsed = time.perf_counter() - t0
        origin = series.index[pos]
        for h in range(horizon):
            rows.append({"origin": origin, "h": h + 1, "y_true": truth[h], "y_pred": pred[h],
                         "predict_seconds": elapsed})
    return pd.DataFrame(rows)


def score(forecasts: pd.DataFrame, scale: float, horizons: Iterable[int],
          quantile: float | None = None) -> pd.DataFrame:
    """Per-horizon accuracy table."""
    out = []
    for h in horizons:
        d = forecasts[forecasts["h"] == h]
        if d.empty:
            continue
        y, p = d["y_true"].to_numpy(), d["y_pred"].to_numpy()
        err = y - p
        row = {
            "h": h,
            "n": len(d),
            "MAE": np.mean(np.abs(err)),
            "RMSE": np.sqrt(np.mean(err ** 2)),
            "MASE": np.mean(np.abs(err)) / scale,
            "sMAPE_%": 100 * np.mean(2 * np.abs(err) / (np.abs(y) + np.abs(p) + 1e-9)),
            "under_pred_%": 100 * np.mean(p < y),
        }
        if quantile is not None:
            row["pinball"] = np.mean(np.maximum(quantile * err, (quantile - 1) * err))
        out.append(row)
    table = pd.DataFrame(out)
    if not forecasts.empty:
        table.attrs["mean_predict_ms"] = 1000 * forecasts.groupby("origin")[
            "predict_seconds"].first().mean()
    return table
