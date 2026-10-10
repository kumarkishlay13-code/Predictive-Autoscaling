"""Forecasting models. Use `create_model(name)` to build one by its CLI name."""

from __future__ import annotations

from pas.models.base import Forecaster, load_model
from pas.models.naive import LastValue, Oracle, SeasonalNaive

#: names accepted by `pas train` / `pas backtest`, with a one-line description
MODEL_NAMES = {
    "naive": "seasonal naive: same minute yesterday (baseline)",
    "last": "last observed value (baseline)",
    "arima": "ARIMA(1,1,1) on log load with daily Fourier terms, bias-corrected",
    "lstm": "LSTM, direct 10-minute output, mean (MSE) loss",
    "lstm-p90": "LSTM trained with pinball loss for the 90th percentile",
    "prophet": "Prophet (offline comparison only)",
}


def create_model(name: str, horizon: int = 10) -> Forecaster:
    if name == "naive":
        return SeasonalNaive()
    if name == "last":
        return LastValue()
    if name == "arima":
        from pas.models.arima import FourierArima
        return FourierArima()
    if name in ("lstm", "lstm-p90"):
        from pas.models.lstm import LstmForecaster
        return LstmForecaster(horizon=horizon, quantile=0.9 if name == "lstm-p90" else None)
    if name == "prophet":
        from pas.models.prophet_model import ProphetForecaster
        return ProphetForecaster()
    raise ValueError(f"unknown model {name!r}; choose from {', '.join(MODEL_NAMES)}")


__all__ = ["MODEL_NAMES", "Forecaster", "LastValue", "Oracle", "SeasonalNaive", "create_model",
           "load_model"]
