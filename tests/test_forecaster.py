import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from prometheus_client import generate_latest

from pas.forecaster.engine import Engine
from pas.forecaster.service import ForecasterService, Settings, create_app
from pas.models import LastValue, SeasonalNaive

START = 1_800_000_000.0  # an arbitrary epoch, on a minute boundary
assert START % 60 == 0


def prometheus_like(values_by_minute_end: dict[float, float]):
    """Fake fetch: returns the samples whose timestamps fall in [start, end]."""
    def fetch(start, end, step):
        data = {pd.Timestamp(t, unit="s"): v for t, v in values_by_minute_end.items()
                if start - 1 <= t <= end + 1}
        return pd.Series(data, dtype=float)
    return fetch


def warm_series(minutes=1440, value=10.0):
    idx = pd.date_range("2019-07-27 04:00", periods=minutes, freq="min")
    return pd.Series(np.full(minutes, value), index=idx)


def test_experiment_mode_appends_observations_on_the_trace_clock():
    warm = warm_series()
    observed = {START + 60 * (i + 1): 100.0 + i for i in range(5)}  # minute i ends at +60(i+1)
    engine = Engine(model=LastValue(), fetch=prometheus_like(observed), horizon=5, lead=(1, 2),
                    warm_start=warm, ingest_delay_seconds=0)
    engine.start(START)
    history, n = engine.history(START + 5 * 60 + 1)
    assert n == 5
    assert history.index[-1] == warm.index[-1] + pd.Timedelta(minutes=5)
    assert history.iloc[-5:].tolist() == [100.0, 101.0, 102.0, 103.0, 104.0]
    result = engine.step(START + 5 * 60 + 1)
    assert result.predicted_rps == 104.0
    assert result.last_observed == 104.0


def test_before_start_the_forecast_covers_the_window_start():
    warm = warm_series()
    engine = Engine(model=SeasonalNaive(), fetch=prometheus_like({}), horizon=3, lead=(1, 3),
                    warm_start=warm)
    history, n = engine.history(START)
    assert n == 0 and history.index[-1] == warm.index[-1]
    assert engine.step(START).forecast.tolist() == [10.0, 10.0, 10.0]


def test_ingest_delay_holds_back_the_latest_minute():
    engine = Engine(model=LastValue(), fetch=prometheus_like({}), warm_start=warm_series(),
                    ingest_delay_seconds=20)
    engine.start(START)
    assert engine.history(START + 70)[1] == 0     # minute 0 ended 10 s ago: not yet read
    assert engine.history(START + 81)[1] == 1


def test_free_running_mode_uses_wall_clock():
    now = START + 3600
    observed = {now - 60 * k: float(k) for k in range(0, 30)}
    engine = Engine(model=LastValue(), fetch=prometheus_like(observed), history_minutes=10,
                    ingest_delay_seconds=0)
    history, n = engine.history(now)
    assert len(history) == 10 and n == 10
    # The newest sample (k=0, timestamp `now`) covers the minute that ends at `now`.
    assert history.iloc[-1] == 0.0
    assert history.index[-1] == pd.Timestamp(now - 60, unit="s")


def test_start_requires_a_window():
    engine = Engine(model=LastValue(), fetch=prometheus_like({}))
    with pytest.raises(ValueError, match="window"):
        engine.start(START)


def make_service(fetch, max_stale=1):
    settings = Settings(model="last", lane="lane-a", max_stale_cycles=max_stale, horizon=3,
                        lead=(1, 2))
    engine = Engine(model=LastValue(), fetch=fetch, horizon=3, lead=(1, 2),
                    warm_start=warm_series(), ingest_delay_seconds=0)
    return ForecasterService(settings, engine)


def test_stale_forecast_stops_publishing():
    calls = {"fail": False}

    def fetch(start, end, step):
        if calls["fail"]:
            raise ConnectionError("prometheus down")
        return pd.Series(dtype=float)

    svc = make_service(fetch, max_stale=1)
    svc.engine.start(START)
    svc.run_cycle(START + 30)
    assert b'pas_predicted_rps{lane="lane-a",model="last"} 10.0' in generate_latest(svc.registry)

    calls["fail"] = True
    svc.run_cycle(START + 120)   # 1st failure: keep last value
    assert b"pas_predicted_rps{" in generate_latest(svc.registry)
    svc.run_cycle(START + 150)   # 2nd failure > max_stale: withdraw the metric
    text = generate_latest(svc.registry)
    assert b"pas_predicted_rps{" not in text
    assert b'pas_forecast_errors_total{lane="lane-a"} 2.0' in text

    calls["fail"] = False
    svc.run_cycle(START + 180)
    assert b"pas_predicted_rps{" in generate_latest(svc.registry)


def test_http_endpoints():
    svc = make_service(lambda s, e, st: pd.Series(dtype=float))
    app = create_app(Settings(model="last", lane="lane-a", interval_seconds=3600), service=svc)
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        # The lifespan loop runs one cycle immediately.
        for _ in range(50):
            if client.get("/readyz").status_code == 200:
                break
        assert client.get("/readyz").status_code == 200
        body = client.get("/forecast").json()
        assert body["model"] == "last" and len(body["forecast_rps"]) == 3
        started = client.post("/control/start", json={"at": START}).json()
        assert started["anchor"] == START
        assert "pas_predicted_rps" in client.get("/metrics").text
        assert client.post("/control/reset").json() == {"anchor": None}
