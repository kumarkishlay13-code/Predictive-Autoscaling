import numpy as np
import pandas as pd
import pytest

from pas.backtest import mase_scale, rolling_forecasts, score
from pas.models import SeasonalNaive
from pas.policy import desired_replicas, keda_threshold, lead_target


def test_lead_target_takes_max_inside_window():
    forecast = [10, 50, 20, 30, 99]
    assert lead_target(forecast, (2, 4)) == 50
    assert lead_target(forecast, (3, 4)) == 30
    with pytest.raises(ValueError):
        lead_target(forecast, (4, 6))


def test_replicas_follow_keda_formula():
    assert keda_threshold(20, 0.2) == pytest.approx(16)
    assert desired_replicas(160, 20, 0.2, 2, 20) == 10   # ceil(160 / 16)
    assert desired_replicas(161, 20, 0.2, 2, 20) == 11
    assert desired_replicas(0, 20, 0.2, 2, 20) == 2      # floor at minReplicas
    assert desired_replicas(10_000, 20, 0.2, 2, 20) == 20  # cap at maxReplicas


def test_naive_is_perfect_on_a_perfectly_periodic_series():
    idx = pd.date_range("2019-07-15", periods=4 * 1440, freq="min")
    t = np.arange(len(idx)) % 1440
    y = pd.Series(50 + 40 * np.sin(2 * np.pi * t / 1440) + (t % 13), index=idx)
    fc = rolling_forecasts(SeasonalNaive(), y, idx[3 * 1440], idx[-1], horizon=5, stride=30)
    table = score(fc, scale=1.0, horizons=[1, 5])
    assert table["MAE"].max() == pytest.approx(0, abs=1e-9)
    assert set(table.columns) >= {"MAE", "RMSE", "MASE", "sMAPE_%", "under_pred_%"}


def test_mase_undefined_for_exactly_periodic_training_data():
    idx = pd.date_range("2019-07-15", periods=3 * 1440, freq="min")
    with pytest.raises(ValueError, match="repeats exactly"):
        mase_scale(pd.Series(np.tile(np.arange(1440.0), 3), index=idx))


def test_backtest_uses_only_past_data():
    """A model that peeks at the future would see values beyond the origin."""
    idx = pd.date_range("2019-07-15", periods=3000, freq="min")
    y = pd.Series(np.arange(3000, dtype=float), index=idx)

    class Spy(SeasonalNaive):
        def predict(self, history, horizon, quantile=None):
            assert history.index[-1] < idx[-1]
            return np.full(horizon, history.iloc[-1])

    fc = rolling_forecasts(Spy(), y, idx[2000], idx[-1], horizon=3, stride=100)
    # Origins step by `stride`; truth is the next value, the forecast the current one.
    first = fc[fc["h"] == 1].iloc[0]
    assert first["y_true"] - first["y_pred"] == 1


def test_pinball_reported_for_quantiles(series):
    train = series.iloc[:7 * 1440]
    model = SeasonalNaive().fit(train)
    fc = rolling_forecasts(model, series, series.index[8 * 1440], series.index[9 * 1440],
                           horizon=3, stride=60, quantile=0.9)
    table = score(fc, mase_scale(train), horizons=[1], quantile=0.9)
    assert "pinball" in table.columns
    assert table["under_pred_%"].item() < 50
