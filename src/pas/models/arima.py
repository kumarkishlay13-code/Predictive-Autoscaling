"""Statistical forecaster: ARIMA on log load with daily Fourier terms as regressors.

A seasonal ARIMA with a 1440-minute season is too slow to fit, so the daily cycle is modelled
by Fourier terms (exogenous regressors) and the ARIMA part captures short-term dynamics. The
model is autoregressive, so unlike Prophet it reacts to the latest observations.

Parameters are estimated once in `fit`. `predict` re-runs only the Kalman filter on the most
recent `context_minutes` of history with those fixed parameters, which takes well under a second.

The model works on log1p(load). Transforming a log-scale forecast back with exp gives the
median, which under-predicts the mean; the point forecast therefore applies the log-normal
correction exp(mu + se^2 / 2). Order (1,1,1) without a trend term was selected on the
validation days (docs/decisions.md, ADR-012).
"""

from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
from scipy.stats import norm
from statsmodels.tsa.statespace.sarimax import SARIMAX

from pas.models.base import Forecaster, clip_non_negative
from pas.timeseries import fourier_terms, future_index


class FourierArima(Forecaster):
    name = "arima"

    def __init__(self, order: tuple[int, int, int] = (1, 1, 1), harmonics: int = 4,
                 fit_minutes: int = 4320, context_minutes: int = 1440) -> None:
        self.order = tuple(order)
        # A constant on a differenced series is a drift; only use one for stationary models.
        self.trend = "c" if self.order[1] == 0 else "n"
        self.harmonics = harmonics
        self.fit_minutes = fit_minutes
        self.context_minutes = context_minutes
        self.params: np.ndarray | None = None

    def _model(self, y: pd.Series) -> SARIMAX:
        endog = np.log1p(y.clip(lower=0))
        exog = fourier_terms(y.index, self.harmonics)
        return SARIMAX(endog, exog=exog, order=self.order, trend=self.trend,
                       enforce_stationarity=False, enforce_invertibility=False)

    def fit(self, y: pd.Series) -> FourierArima:
        y = y.iloc[-self.fit_minutes:]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            res = self._model(y).fit(disp=False, maxiter=200)
        self.params = np.asarray(res.params)
        return self

    def predict(self, history: pd.Series, horizon: int,
                quantile: float | None = None) -> np.ndarray:
        if self.params is None:
            raise ValueError("model is not fitted; run `pas train arima` first")
        h = history.iloc[-self.context_minutes:]
        if len(h) < 10:
            last = float(h.dropna().iloc[-1]) if h.notna().any() else 0.0
            return np.full(horizon, max(last, 0.0))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            res = self._model(h).filter(self.params)
            fc = res.get_forecast(horizon,
                                  exog=fourier_terms(future_index(h, horizon), self.harmonics))
        mu, se = np.asarray(fc.predicted_mean), np.asarray(fc.se_mean)
        if quantile is None:
            mu = mu + 0.5 * se ** 2            # log-normal mean, not the median
        else:
            mu = mu + norm.ppf(quantile) * se  # quantiles survive the monotone transform
        return clip_non_negative(np.expm1(mu))

    def describe(self) -> dict:
        return {**super().describe(), "order": list(self.order), "trend": self.trend,
                "harmonics": self.harmonics,
                "fit_minutes": self.fit_minutes, "context_minutes": self.context_minutes}
