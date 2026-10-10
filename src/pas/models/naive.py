"""Reference forecasters: seasonal naive ("same minute yesterday") and last value.

Every ML model must beat these to be worth its complexity (MASE < 1). Quantile forecasts use
empirical quantiles of the training residuals, so the P90 variants are also available here.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from pas.models.base import Forecaster, clip_non_negative
from pas.timeseries import MINUTES_PER_DAY, fill_gaps

_MAX_HORIZON = 60
#: residual quantiles are stored on this grid (0, 0.005, ..., 1) instead of raw residuals
_GRID = np.linspace(0.0, 1.0, 201)


def _quantile_table(residuals: np.ndarray) -> np.ndarray:
    residuals = residuals[np.isfinite(residuals)]
    return np.quantile(residuals, _GRID).astype(np.float32)


def _lookup(table: np.ndarray, q: float) -> float:
    return float(np.interp(q, _GRID, table))


class SeasonalNaive(Forecaster):
    name = "naive"

    def __init__(self, season: int = MINUTES_PER_DAY) -> None:
        self.season = season
        self.residual_quantiles: np.ndarray | None = None

    def fit(self, y: pd.Series) -> SeasonalNaive:
        v = fill_gaps(y).to_numpy()
        if len(v) > self.season:
            self.residual_quantiles = _quantile_table(v[self.season:] - v[:-self.season])
        return self

    def predict(self, history: pd.Series, horizon: int,
                quantile: float | None = None) -> np.ndarray:
        if horizon > self.season:
            raise ValueError("horizon cannot exceed the season length")
        v = fill_gaps(history).to_numpy()
        n = len(v)
        if n == 0:
            return np.zeros(horizon)
        if n >= self.season:
            point = v[n - self.season: n - self.season + horizon]
        else:  # not enough history yet: fall back to the last value
            point = np.full(horizon, v[-1])
        if quantile is not None:
            if self.residual_quantiles is None:
                raise ValueError("quantile forecasts need a fitted model (call fit first)")
            point = point + _lookup(self.residual_quantiles, quantile)
        return clip_non_negative(point)

    def describe(self) -> dict:
        return {**super().describe(), "season": self.season}


class LastValue(Forecaster):
    name = "last"

    def __init__(self) -> None:
        self.residual_quantiles: dict[int, np.ndarray] = {}

    def fit(self, y: pd.Series) -> LastValue:
        v = fill_gaps(y).to_numpy()
        for h in range(1, min(_MAX_HORIZON, len(v) - 1) + 1):
            self.residual_quantiles[h] = _quantile_table(v[h:] - v[:-h])
        return self

    def predict(self, history: pd.Series, horizon: int,
                quantile: float | None = None) -> np.ndarray:
        v = fill_gaps(history).to_numpy()
        last = v[-1] if len(v) else 0.0
        point = np.full(horizon, last, dtype=float)
        if quantile is not None:
            if not self.residual_quantiles:
                raise ValueError("quantile forecasts need a fitted model (call fit first)")
            longest = max(self.residual_quantiles)
            for i in range(horizon):
                table = self.residual_quantiles.get(i + 1, self.residual_quantiles[longest])
                point[i] += _lookup(table, quantile)
        return clip_non_negative(point)


class Oracle(Forecaster):
    """Perfect foresight: reads the future from the known load series (upper bound)."""

    name = "oracle"

    def __init__(self, truth: pd.Series) -> None:
        self.truth = truth.astype(float)

    def predict(self, history: pd.Series, horizon: int,
                quantile: float | None = None) -> np.ndarray:
        if len(history) == 0:
            return np.zeros(horizon)
        future = pd.date_range(history.index[-1] + pd.Timedelta(minutes=1),
                               periods=horizon, freq="min")
        values = self.truth.reindex(future).to_numpy()
        # Past the end of the known series, hold the last known value.
        if np.isnan(values).any():
            fallback = float(self.truth.iloc[-1]) if len(self.truth) else 0.0
            values = np.where(np.isnan(values), fallback, values)
        return clip_non_negative(values)
