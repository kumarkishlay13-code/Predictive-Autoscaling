"""Helpers shared by every model: the per-minute series convention and calendar features.

Convention used throughout the project: a load series is a ``pd.Series`` of requests per second
(float), indexed by a timezone-naive ``DatetimeIndex`` at one-minute frequency. The last element
of a *history* is the most recent complete minute; a forecast of horizon H returns the values for
the next H minutes.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

MINUTES_PER_DAY = 1440
FREQ = "min"


def as_minutely(y: pd.Series) -> pd.Series:
    """Return `y` as float on a regular 1-minute grid (gaps become NaN)."""
    if not isinstance(y.index, pd.DatetimeIndex):
        raise TypeError("series must have a DatetimeIndex")
    y = y.astype(float).sort_index()
    y = y[~y.index.duplicated(keep="last")]
    return y.asfreq(FREQ)


def future_index(history: pd.Series, horizon: int) -> pd.DatetimeIndex:
    start = history.index[-1] + pd.Timedelta(minutes=1)
    return pd.date_range(start, periods=horizon, freq=FREQ)


def fill_gaps(y: pd.Series) -> pd.Series:
    """Forward-fill then back-fill short gaps; remaining NaNs (empty series) become 0."""
    return y.ffill().bfill().fillna(0.0)


def minute_of_day(index: pd.DatetimeIndex) -> np.ndarray:
    return (index.hour * 60 + index.minute).to_numpy(dtype=float)


def fourier_terms(index: pd.DatetimeIndex, harmonics: int) -> pd.DataFrame:
    """Daily Fourier terms sin/cos(2πk·t/1440), k=1..harmonics."""
    t = minute_of_day(index) / MINUTES_PER_DAY
    cols = {}
    for k in range(1, harmonics + 1):
        cols[f"sin{k}"] = np.sin(2 * np.pi * k * t)
        cols[f"cos{k}"] = np.cos(2 * np.pi * k * t)
    return pd.DataFrame(cols, index=index)


def day_slice(y: pd.Series, first_day: int, last_day: int, start_date: str) -> pd.Series:
    """Select trace days [first_day, last_day] (1-based, inclusive)."""
    t0 = pd.Timestamp(start_date)
    lo = t0 + pd.Timedelta(days=first_day - 1)
    hi = t0 + pd.Timedelta(days=last_day) - pd.Timedelta(minutes=1)
    return y.loc[lo:hi]
