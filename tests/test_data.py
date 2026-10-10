import json

import numpy as np
import pandas as pd
import pytest

from pas.config import ConfigError
from pas.data.trace import cache_path, load_invocations, rank_apps, signal_stats
from pas.data.windows import build_series, build_window, load_series, window_series, write_windows


def test_load_invocations_total_and_filters(project, fake_trace):
    t = project.trace
    total = load_invocations(fake_trace, 14, t.start_date)
    assert len(total) == 14 * 1440
    assert total.index[0] == pd.Timestamp(t.start_date)
    http_only = load_invocations(fake_trace, 14, t.start_date, trigger="http")
    np.testing.assert_allclose(total - http_only, 60)   # the timer function adds 60/min
    app_b = load_invocations(fake_trace, 14, t.start_date, signal="appB")
    assert app_b.max() == 1


def test_unknown_signal_is_an_error(project, fake_trace):
    with pytest.raises(ValueError, match="no invocations"):
        load_invocations(fake_trace, 2, project.trace.start_date, signal="missing")


def test_missing_trace_points_to_download(project):
    with pytest.raises(FileNotFoundError, match="pas data download"):
        load_invocations(project.trace.raw_dir, 1, project.trace.start_date)


def test_rank_apps_prefers_the_regular_busy_app(project, fake_trace):
    table, invocations = rank_apps(fake_trace, 14, project.trace.start_date, top=5)
    assert table.iloc[0]["app"] == "appA"
    assert table.iloc[0]["daily_autocorr"] > 0.5
    assert table.iloc[0]["sustained_range"] > 2
    assert list(invocations.columns) == ["appA", "appB"] and len(invocations) == 14 * 1440


def test_build_uses_the_app_cache(project, fake_trace):
    import dataclasses
    _, invocations = rank_apps(fake_trace, 14, project.trace.start_date)
    project.trace.processed_dir.mkdir(parents=True)
    invocations.to_parquet(cache_path(project.trace.processed_dir, None))
    for f in fake_trace.glob("*.csv"):   # prove the raw trace is not read again
        f.unlink()
    cfg = dataclasses.replace(project, trace=dataclasses.replace(project.trace, signal="appA"))
    df = build_series(cfg)
    np.testing.assert_allclose(df["invocations"].to_numpy(), invocations["appA"].to_numpy())


def test_bursts_do_not_count_as_sustained_range():
    idx = pd.date_range("2019-07-15", periods=3 * 1440, freq="min")
    flat = np.full(len(idx), 10.0)
    flat[::60] = 300.0                       # one-minute burst every hour
    stats = signal_stats(pd.Series(flat, index=idx))
    assert stats["sustained_range"] < 1.5
    assert stats["burstiness"] > 5


def test_series_is_scaled_to_cluster_capacity(project, fake_trace):
    df = build_series(project)
    peak = np.percentile(df["rps"], 99.5)
    assert peak == pytest.approx(project.capacity.target_peak_rps)
    meta = json.loads(project.series_path.with_suffix(".json").read_text())
    assert meta["scale_factor"] > 0 and meta["signal"] == "total"


def test_windows_come_from_test_days_with_history(project, fake_trace):
    build_series(project)
    rps = load_series(project)
    w1 = build_window(project, "W1", rps)
    w3 = build_window(project, "W3", rps)
    start = pd.Timestamp(w1["start"])
    test_start = pd.Timestamp(project.trace.start_date) + pd.Timedelta(days=12)
    assert start >= test_start
    assert len(w1["rps"]) == 120
    assert len(w1["warm_start_rps"]) == project.forecast.history_minutes
    # The ramp window rises; W3 is W2 with a 3x spike at minute 60.
    assert np.mean(w1["rps"][-15:]) > np.mean(w1["rps"][:15])
    w2 = build_window(project, "W2", rps)
    cap = 0.95 * project.capacity.max_replicas * project.capacity.per_pod_rps
    assert w3["rps"][60] == pytest.approx(min(3 * w2["rps"][60], cap), rel=1e-3)
    assert max(w3["rps"]) <= cap + 1e-6
    assert w3["rps"][:60] == w2["rps"][:60]
    # Warm start ends exactly one minute before the window.
    s = window_series(w1)
    assert (s.index[1:] - s.index[:-1] == pd.Timedelta(minutes=1)).all()
    assert s.index[len(w1["warm_start_rps"])] == start


def test_windows_outside_test_days_are_rejected(project, fake_trace):
    import dataclasses
    build_series(project)
    rps = load_series(project)
    bad = dataclasses.replace(project, windows=(dataclasses.replace(project.window("W1"),
                                                                     day=3),))
    with pytest.raises(ConfigError, match="test_days"):
        build_window(bad, "W1", rps)


def test_write_windows_creates_files(project, fake_trace):
    build_series(project)
    paths = write_windows(project, load_series(project), ["W1", "WD"])
    assert [p.name for p in paths] == ["W1.json", "WD.json"]
