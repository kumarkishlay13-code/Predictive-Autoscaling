"""End-to-end smoke test of the control loop on a live cluster, in minutes (`pas smoke`).

Replays the first minutes of a window time-compressed and with light requests, then checks
every link of the loop: k6 sent the load and remote-wrote its metrics, demo-api counted it,
Prometheus scraped the app, kube-state-metrics and cAdvisor, metrics-server serves pod CPU, the
HPA computes replica counts and, for predictive presets, the forecaster published
`pas_predicted_rps`, KEDA is ready and replicas went above the minimum.

It verifies wiring only. Time-compressed replays are never study data.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from pas.config import Config, ConfigError
from pas.experiments.lanes import APP, deploy_lane
from pas.experiments.loadgen import finish_k6, start_k6
from pas.experiments.runner import _window_path, replay_env, reset_lane
from pas.kube import CommandError, kubectl_json, port_forward, run
from pas.prometheus import Prometheus, PrometheusError

#: lets Prometheus scrape the last samples and KEDA/HPA act on them before checking
SETTLE_SECONDS = 45


@dataclass(frozen=True)
class Check:
    label: str
    ok: bool
    detail: str = ""


def prometheus_checks(lane: str, run_id: str, predictive: bool,
                      min_replicas: int) -> list[tuple[str, str]]:
    """(label, PromQL) pairs. A check passes when its query returns a non-empty result."""
    def seen(selector: str) -> str:
        return f"count(count_over_time({selector}[15m])) > 0"

    checks = [
        ("demo-api counted the offered load",
         f'sum(increase(demo_requests_received_total{{namespace="{lane}"}}[15m])) > 0'),
        ("k6 remote-wrote client-side metrics",
         seen(f'k6_http_reqs_total{{lane="{lane}",run="{run_id}"}}')),
        ("kube-state-metrics reports replicas",
         seen(f'kube_deployment_status_replicas_ready{{namespace="{lane}",deployment="{APP}"}}')),
        ("cAdvisor reports pod CPU",
         seen(f'container_cpu_usage_seconds_total{{namespace="{lane}",container="{APP}"}}')),
    ]
    if predictive:
        checks += [
            ("forecaster published pas_predicted_rps", seen(f'pas_predicted_rps{{lane="{lane}"}}')),
            (f"scaler raised replicas above the minimum ({min_replicas})",
             f'max_over_time(kube_deployment_spec_replicas{{namespace="{lane}",'
             f'deployment="{APP}"}}[15m]) > {min_replicas}'),
        ]
    return checks


def _condition(obj: dict, kind: str) -> bool:
    return any(c.get("type") == kind and c.get("status") == "True"
               for c in obj.get("status", {}).get("conditions", []))


def cluster_checks(lane: str, predictive: bool) -> list[Check]:
    metrics = run(["kubectl", "get", "--raw",
                   f"/apis/metrics.k8s.io/v1beta1/namespaces/{lane}/pods"],
                  check=False, capture=True, quiet=True)
    checks = [Check("metrics-server serves pod CPU", metrics.returncode == 0,
                    (metrics.stderr or "").strip()[:200])]
    # KEDA creates and owns an HPA named keda-hpa-<scaledobject>.
    hpa_name = f"keda-hpa-{APP}" if predictive else APP
    try:
        hpa = kubectl_json("-n", lane, "get", "hpa", hpa_name)
        checks.append(Check(f"HPA {hpa_name} computes replica counts",
                            _condition(hpa, "ScalingActive"), "condition ScalingActive"))
    except CommandError as exc:
        checks.append(Check(f"HPA {hpa_name} exists", False, str(exc).splitlines()[0]))
    if predictive:
        try:
            so = kubectl_json("-n", lane, "get", "scaledobject", APP)
            checks.append(Check("KEDA ScaledObject is ready", _condition(so, "Ready"),
                                f"kubectl -n {lane} describe scaledobject {APP}"))
        except CommandError as exc:
            checks.append(Check("KEDA ScaledObject exists", False, str(exc).splitlines()[0]))
    return checks


def smoke_test(cfg: Config, lane: str, scaler: str, window: str, *, minutes: int = 10,
               step_seconds: int = 10, work_units: int = 2) -> list[Check]:
    mode = cfg.scaler(scaler)["mode"]
    if mode not in ("hpa", "predictive"):
        raise ConfigError(f"smoke test needs an hpa or predictive preset, {scaler!r} is {mode}")
    predictive = mode == "predictive"
    window_path = _window_path(cfg, window)

    reset_lane(cfg, lane, startup_delay=0, quiet_seconds=0)
    deploy_lane(cfg, lane, scaler, window=window_path, startup_delay=0)
    run_id = time.strftime("smoke-%Y%m%d-%H%M%S")
    env = {**replay_env(cfg, lane, scaler), "MINUTES": str(minutes),
           "STEP_SECONDS": str(step_seconds), "WORK_UNITS": str(work_units)}
    started = time.time()
    job = start_k6(cfg, namespace=lane, run_id=run_id, script="replay.js", env=env,
                   window=window_path)
    result = finish_k6(lane, job, started, timeout_s=minutes * step_seconds + 600)
    checks = [Check("k6 replayed the window and printed its summary",
                    result.ok and result.summary is not None, f"kubectl -n {lane} logs job/{job}")]

    print(f"waiting {SETTLE_SECONDS}s for the last scrapes ...", flush=True)
    time.sleep(SETTLE_SECONDS)
    checks += cluster_checks(lane, predictive)
    with port_forward(cfg.cluster.prometheus_service, 9090) as url:
        prom = Prometheus(url)
        for label, query in prometheus_checks(lane, run_id, predictive,
                                              cfg.capacity.min_replicas):
            try:
                checks.append(Check(label, bool(prom.query(query)), query))
            except PrometheusError as exc:
                checks.append(Check(label, False, str(exc)))
    return checks
