import numpy as np
import pandas as pd
import pytest

from pas.backtest import mase_scale, rolling_forecasts, score
from pas.models import LastValue, Oracle, SeasonalNaive, create_model, load_model
from pas.models.arima import FourierArima
from tests.conftest import diurnal


def test_seasonal_naive_repeats_yesterday(series):
    model = SeasonalNaive()
    history = series.iloc[:3 * 1440]
    pred = model.predict(history, 5)
    np.testing.assert_allclose(pred, series.iloc[2 * 1440: 2 * 1440 + 5].to_numpy())


def test_seasonal_naive_falls_back_to_last_value_on_short_history(series):
    pred = SeasonalNaive().predict(series.iloc[:100], 3)
    np.testing.assert_allclose(pred, [series.iloc[99]] * 3)


def test_quantile_forecast_is_above_mean(series):
    train = series.iloc[:7 * 1440]
    for model in (SeasonalNaive().fit(train), LastValue().fit(train)):
        mean = model.predict(train, 5)
        p90 = model.predict(train, 5, quantile=0.9)
        assert np.all(p90 >= mean)


def test_quantile_needs_fit(series):
    with pytest.raises(ValueError, match="fit"):
        SeasonalNaive().predict(series.iloc[:2000], 3, quantile=0.9)


def test_forecasts_are_never_negative():
    idx = pd.date_range("2019-07-15", periods=3000, freq="min")
    falling = pd.Series(np.linspace(50, 0, 3000), index=idx)
    model = LastValue().fit(falling)
    assert np.all(model.predict(falling, 10, quantile=0.1) >= 0)


def test_oracle_reads_the_future(series):
    oracle = Oracle(series)
    pred = oracle.predict(series.iloc[:500], 4)
    np.testing.assert_allclose(pred, series.iloc[500:504].to_numpy())
    # Past the end of known data it holds the last value.
    assert oracle.predict(series, 2).tolist() == [series.iloc[-1]] * 2


def test_arima_beats_naive_on_short_horizons():
    series = diurnal(days=6, noise=0.05)
    train, test_start = series.iloc[:4 * 1440], series.index[4 * 1440]
    model = FourierArima(fit_minutes=3 * 1440, context_minutes=720).fit(train)
    fc = rolling_forecasts(model, series, test_start, series.index[-1], horizon=5, stride=60,
                           context=1440)
    table = score(fc, mase_scale(train), horizons=[1, 5])
    assert len(table) == 2
    assert table.loc[table["h"] == 1, "MASE"].item() < 1.0
    # Quantile forecasts sit above the mean.
    history = series.iloc[:4 * 1440]
    assert np.all(model.predict(history, 5, quantile=0.9) > model.predict(history, 5))


def test_save_and_load_round_trip(tmp_path, series):
    model = SeasonalNaive().fit(series.iloc[:3 * 1440])
    path = model.save(tmp_path / "naive.pkl")
    loaded = load_model(path)
    assert path.with_suffix(".json").exists()
    np.testing.assert_allclose(loaded.predict(series.iloc[:3000], 3),
                               model.predict(series.iloc[:3000], 3))


def test_create_model_rejects_unknown_names():
    with pytest.raises(ValueError, match="unknown model"):
        create_model("magic")


def test_lstm_learns_a_daily_pattern():
    pytest.importorskip("torch")
    from pas.models.lstm import LstmForecaster

    series = diurnal(days=4, noise=0.02)
    model = LstmForecaster(window=30, horizon=5, hidden=16, layers=1, epochs=8, batch_size=128,
                           lr=5e-3)
    model.fit(series.iloc[:3 * 1440])
    pred = model.predict(series.iloc[:3 * 1440], 5)
    truth = series.iloc[3 * 1440: 3 * 1440 + 5].to_numpy()
    assert pred.shape == (5,)
    assert np.mean(np.abs(pred - truth) / truth) < 0.1
    with pytest.raises(ValueError, match="quantile"):
        model.predict(series, 5, quantile=0.9)
