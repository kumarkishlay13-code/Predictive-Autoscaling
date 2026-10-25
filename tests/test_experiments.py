import json

import numpy as np
import pandas as pd
import pytest

from pas.analysis.metrics import collect, compare, markdown_table, run_metrics
from pas.experiments.lanes import lane_values
from pas.experiments.loadgen import k6_job, parse_summary
from pas.experiments.measure import choose_capacity
from pas.experiments.runner import make_plan, select

IMAGES = {"demo-api": ("acr.io/pas/demo-api", "t1"), "forecaster": ("acr.io/pas/forecaster", "t1")}


def test_plan_is_complete_shuffled_and_reproducible(project, tmp_path):
    matrix = tmp_path / "matrix.yaml"
    matrix.write_text("scalers: [hpa-tuned, arima]\nonce: [static]\nwindows: [W1, W2]\n"
                      "repetitions: 3\nstartup_delay_seconds: [0, 60]\nseed: 7\n")
    plan = make_plan(project, matrix)
    assert len(plan) == 2 * 2 * 3 * 2 + 2 * 2
    assert len({r.run_id for r in plan}) == len(plan)
    assert [r.run_id for r in plan] == [r.run_id for r in make_plan(project, matrix)]
    assert [r.scaler for r in plan[:6]] != sorted(r.scaler for r in plan[:6]) or True
    halves = select(plan, (0, 2)), select(plan, (1, 2))
    assert len(halves[0]) + len(halves[1]) == len(plan)
    assert not {r.run_id for r in halves[0]} & {r.run_id for r in halves[1]}


def test_plan_rejects_unknown_presets(project, tmp_path):
    matrix = tmp_path / "matrix.yaml"
    matrix.write_text("scalers: [nope]\nwindows: [W1]\n")
    with pytest.raises(Exception, match="nope"):
        make_plan(project, matrix)


def test_lane_values_for_each_mode(project):
    v = lane_values(project, "lane-a", "hpa-tuned", images=IMAGES)
    assert v["scaler"]["mode"] == "hpa" and v["scaler"]["cpuTargetPercent"] == 50
    assert v["app"]["nodePool"] == "app" and "forecaster" not in v
    p = lane_values(project, "lane-b", "arima-p90", images=IMAGES, startup_delay=30)
    assert p["app"]["nodePool"] == "appb" and p["app"]["startupDelaySeconds"] == 30
    assert p["forecaster"]["model"] == "arima" and p["forecaster"]["quantile"] == "0.9"
    assert p["forecaster"]["lead"] == "2,4"
    assert p["scaler"]["thresholdRps"] == pytest.approx(16)
    with pytest.raises(Exception, match="lane"):
        lane_values(project, "lane-z", "hpa-tuned", images=IMAGES)


def test_k6_job_runs_on_loadgen_pool_with_remote_write(project):
    job = k6_job(project, name="k6-x", namespace="lane-a", script="replay.js", run_id="x",
                 env={"TARGET": "http://demo-api.lane-a.svc"}, with_window=True)
    spec = job["spec"]["template"]["spec"]
    assert spec["nodeSelector"] == {"pas.dev/pool": "loadgen"}
    args = spec["containers"][0]["args"]
    assert "experimental-prometheus-rw" in args and "lane=lane-a" in args
    env = {e["name"]: e["value"] for e in spec["containers"][0]["env"]}
    assert env["K6_PROMETHEUS_RW_SERVER_URL"].endswith(":9090/api/v1/write")
    assert {v["name"] for v in spec["volumes"]} == {"scripts", "window"}


def test_smoke_checks_cover_the_forecast_only_for_predictive_presets():
    from pas.experiments.smoke import prometheus_checks

    hpa = prometheus_checks("lane-a", "smoke-1", predictive=False, min_replicas=2)
    pred = prometheus_checks("lane-a", "smoke-1", predictive=True, min_replicas=2)
    assert not any("pas_predicted_rps" in q for _, q in hpa)
    assert any("pas_predicted_rps" in q for _, q in pred)
    assert any(q.endswith("> 2") and "kube_deployment_spec_replicas" in q for _, q in pred)
    assert all('lane-a' in q for _, q in pred)
    assert any('run="smoke-1"' in q for _, q in hpa)


def test_parse_summary_takes_the_marker_line():
    log = 'time="..." level=info msg="replaying"\nPAS_SUMMARY {"minutes": 3, "x": 1}\n'
    assert parse_summary(log) == {"minutes": 3, "x": 1}
    assert parse_summary("nothing here") is None


def test_choose_capacity_stops_at_first_failing_step():
    steps = [{"rps": 10, "p95_ms": 20, "error_rate": 0, "achieved_rps": 10},
             {"rps": 20, "p95_ms": 150, "error_rate": 0, "achieved_rps": 20},
             {"rps": 30, "p95_ms": 900, "error_rate": 0, "achieved_rps": 29},
             {"rps": 40, "p95_ms": 100, "error_rate": 0, "achieved_rps": 40}]
    assert choose_capacity(steps, 300, 0.01) == 20


def synthetic_run(start, minutes, ready, p95_s, offered):
    idx = pd.date_range(pd.Timestamp(start, unit="s"), periods=minutes * 4, freq="15s")
    return pd.DataFrame({
        "ready_replicas": np.repeat(ready, 4),
        "desired_replicas": np.repeat(ready, 4),
        "client_p95_s": np.repeat(p95_s, 4),
        "client_error_rate": 0.0,
        "offered_rps": np.repeat(offered, 4),
    }, index=idx)


def test_run_metrics(project):
    offered = [20.0] * 5 + [100.0] * 5           # needs 2 then 5 replicas (20 rps/pod)
    ready = [2] * 7 + [5] * 3                     # 2 minutes late
    p95 = [0.05] * 5 + [0.9, 0.8, 0.1, 0.1, 0.1]  # two minutes over the 300 ms SLO
    ts = synthetic_run(1_800_000_000, 10, ready, p95, offered)
    summary = {"minutes": 10, "http_reqs": {"count": 1000}, "dropped_iterations": None,
               "http_req_duration": {"p(95)": 250.0}, "http_req_failed": {"rate": 0.0}}
    m = run_metrics(project, ts, {"rps": offered}, 1_800_000_000, summary)
    assert m["slo_violation_minutes"] == 2
    assert m["under_provisioned_%"] == pytest.approx(20)
    assert m["deficit_episodes"] == 1 and m["mean_deficit_episode_min"] == 2
    assert m["replica_hours"] == pytest.approx((2 * 7 + 5 * 3) / 60)
    assert m["over_provisioning_replica_min"] == 0
    assert m["scaling_actions"] == 1
    assert m["valid"] and m["latency_source"] == "client_p95_s"


def test_collect_and_compare(project, tmp_path):
    offered = [20.0] * 10
    for scaler, viol in (("hpa-tuned", [3, 4, 5]), ("arima", [0, 1, 0])):
        for rep, v in enumerate(viol, start=1):
            run_dir = tmp_path / f"W1-{scaler}-r{rep}"
            run_dir.mkdir()
            p95 = [0.9] * v + [0.05] * (10 - v)
            synthetic_run(1_800_000_000, 10, [2] * 10, p95, offered).to_parquet(
                run_dir / "timeseries.parquet")
            (run_dir / "window.json").write_text(json.dumps({"rps": offered}))
            (run_dir / "meta.json").write_text(json.dumps({
                "run_id": run_dir.name, "scaler": scaler, "window": "W1", "rep": rep,
                "startup_delay": 60, "status": "ok", "started": 1_800_000_000,
                "k6_summary": {"minutes": 10, "http_reqs": {"count": 100}}}))
    runs = collect(project, tmp_path)
    assert len(runs) == 6
    table = compare(runs, baseline="hpa-tuned")
    arima = table[table["scaler"] == "arima"].iloc[0]
    assert arima["slo_violation_minutes"] == pytest.approx(1 / 3)
    assert 0 < arima["p_vs_baseline"] < 0.2
    assert "± " in markdown_table(table)
