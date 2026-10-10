"""Prophet forecaster, used for the offline accuracy comparison only (ADR-007).

Prophet's forecast depends only on time (trend + seasonality), not on the latest observations,
so it cannot follow a burst already in progress. That limitation is part of what the
comparison shows. Requires: pip install -e ".[prophet]"
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from pas.models.base import Forecaster, clip_non_negative
from pas.timeseries import future_index


class ProphetForecaster(Forecaster):
    name = "prophet"

    def __init__(self, fit_minutes: int = 7 * 1440, interval_width: float = 0.8) -> None:
        self.fit_minutes = fit_minutes
        self.interval_width = interval_width
        self.model = None

    def fit(self, y: pd.Series) -> ProphetForecaster:
        try:
            from prophet import Prophet
        except ImportError as exc:  # pragma: no cover
            raise ImportError('Prophet is optional: pip install -e ".[prophet]"') from exc
        logging.getLogger("cmdstanpy").setLevel(logging.WARNING)
        y = y.iloc[-self.fit_minutes:]
        df = pd.DataFrame({"ds": y.index, "y": np.log1p(y.clip(lower=0).to_numpy())})
        self.model = Prophet(daily_seasonality=True, weekly_seasonality=len(y) >= 14 * 1440,
                             yearly_seasonality=False, interval_width=self.interval_width)
        self.model.fit(df)
        return self

    def predict(self, history: pd.Series, horizon: int,
                quantile: float | None = None) -> np.ndarray:
        if self.model is None:
            raise ValueError("model is not fitted; run `pas train prophet` first")
        fc = self.model.predict(pd.DataFrame({"ds": future_index(history, horizon)}))
        if quantile is None:
            values = fc["yhat"].to_numpy()
        else:
            # Prophet's interval is symmetric: upper bound of width w is the (1+w)/2 quantile.
            expected = (1 + self.interval_width) / 2
            if abs(quantile - expected) > 1e-9:
                raise ValueError(f"this Prophet model gives quantile {expected:.2f} only")
            values = fc["yhat_upper"].to_numpy()
        return clip_non_negative(np.expm1(values))
