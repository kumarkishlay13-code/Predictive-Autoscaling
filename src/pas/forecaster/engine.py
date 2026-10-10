"""Forecasting logic of the live service, independent of HTTP and Prometheus.

Two clock modes:

* Free-running (no window file): history = the last `history_minutes` of observed load, on
  wall-clock time.
* Experiment (window file given): history = the trace minutes that precede the window
  (warm start) followed by what has been observed since the run was started with
  `POST /control/start`. Observed minute i is placed at trace time window_start + i, so models
  see one continuous series on the trace clock. Before the start, the forecast covers the first
  minutes of the window, which lets the predictive scaler pre-scale before load arrives.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from pas.models.base import Forecaster
from pas.policy import lead_target

#: fetch(start_epoch, end_epoch, step_seconds) -> Series indexed by UTC timestamp. Each value
#: must be the average RPS over the minute that ends at its timestamp.
Fetch = Callable[[float, float, float], pd.Series]


@dataclass
class ForecastResult:
    wall_time: float
    trace_time: pd.Timestamp
    forecast: np.ndarray
    predicted_rps: float
    last_observed: float | None
    history_minutes: int
    observed_minutes: int

    def as_dict(self) -> dict:
        return {
            "wall_time": self.wall_time,
            "trace_time": self.trace_time.isoformat(),
            "forecast_rps": [round(float(x), 3) for x in self.forecast],
            "predicted_rps": round(self.predicted_rps, 3),
            "last_observed_rps": None if self.last_observed is None
            else round(self.last_observed, 3),
            "history_minutes": self.history_minutes,
            "observed_minutes": self.observed_minutes,
        }


@dataclass
class Engine:
    model: Forecaster
    fetch: Fetch
    horizon: int = 10
    lead: tuple[int, int] = (2, 4)
    quantile: float | None = None
    history_minutes: int = 4320
    #: wait this long after a minute ends before reading it, so Prometheus has scraped it
    ingest_delay_seconds: float = 20.0
    warm_start: pd.Series | None = None
    anchor: float | None = field(default=None)

    @property
    def window_start(self) -> pd.Timestamp | None:
        if self.warm_start is None or self.warm_start.empty:
            return None
        return self.warm_start.index[-1] + pd.Timedelta(minutes=1)

    def start(self, at: float) -> None:
        """Mark wall-clock time `at` as the first second of the replay window."""
        if self.warm_start is None:
            raise ValueError("control/start needs a window file (PAS_WINDOW_FILE)")
        self.anchor = at

    def reset(self) -> None:
        self.anchor = None

    # History -----------------------------------------------------------------------------
    def history(self, now: float) -> tuple[pd.Series, int]:
        """Return (history on the model's clock, number of observed minutes in it)."""
        if self.warm_start is None:
            end = math.floor((now - self.ingest_delay_seconds) / 60) * 60
            start = end - 60 * (self.history_minutes - 1)
            obs = self.fetch(start, end, 60)
            index = pd.date_range(pd.Timestamp(start, unit="s") - pd.Timedelta(minutes=1),
                                  periods=self.history_minutes, freq="min")
            values = _align(obs, pd.Timestamp(start, unit="s"), self.history_minutes)
            return pd.Series(values, index=index), int(np.isfinite(values).sum())

        warm = self.warm_start
        if self.anchor is None:
            return warm.iloc[-self.history_minutes:], 0
        n = int((now - self.anchor - self.ingest_delay_seconds) // 60)
        if n <= 0:
            return warm.iloc[-self.history_minutes:], 0
        obs = self.fetch(self.anchor + 60, self.anchor + 60 * n, 60)
        values = _align(obs, pd.Timestamp(self.anchor + 60, unit="s"), n)
        observed = pd.Series(values, index=pd.date_range(self.window_start, periods=n,
                                                         freq="min"))
        history = pd.concat([warm, observed]).iloc[-self.history_minutes:]
        return history, n

    # Forecast ----------------------------------------------------------------------------
    def step(self, now: float) -> ForecastResult:
        history, observed = self.history(now)
        if history.empty:
            raise ValueError("no history available yet")
        forecast = self.model.predict(history, self.horizon, quantile=self.quantile)
        finite = history.dropna()
        return ForecastResult(
            wall_time=now,
            trace_time=history.index[-1],
            forecast=forecast,
            predicted_rps=lead_target(forecast, self.lead),
            last_observed=float(finite.iloc[-1]) if observed and len(finite) else None,
            history_minutes=len(history),
            observed_minutes=observed,
        )


def _align(obs: pd.Series, first: pd.Timestamp, n: int) -> np.ndarray:
    """Place fetched samples on the expected 1-minute grid starting at `first` (NaN if absent)."""
    grid = pd.date_range(first, periods=n, freq="min")
    if obs is None or obs.empty:
        return np.full(n, np.nan)
    obs = obs.copy()
    obs.index = pd.DatetimeIndex(obs.index).round("s")
    return obs.reindex(grid, method="nearest", tolerance=pd.Timedelta(seconds=5)).to_numpy(
        dtype=float)
