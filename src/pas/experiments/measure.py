"""Capacity test and provisioning-lag measurement (plan week 3)."""

from __future__ import annotations

import json
import statistics
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from pas.config import Config
from pas.experiments.lanes import APP, deploy_lane
from pas.experiments.loadgen import finish_k6, start_k6
from pas.io import write_text
from pas.kube import CommandError, kubectl_json, run, wait_ready_replicas


def choose_capacity(steps: list[dict[str, Any]], p95_ms: float, error_rate: float) -> float:
    """Highest step rate, reached through consecutive passing steps, that meets the SLO."""
    best = 0.0
    for step in steps:
        ok = (step.get("p95_ms") is not None and step["p95_ms"] <= p95_ms
              and (step.get("error_rate") or 0.0) <= error_rate
              and step.get("achieved_rps", 0) >= 0.95 * step["rps"])
        if not ok:
            break
        best = float(step["rps"])
    return best


def capacity_test(cfg: Config, lane: str, steps: list[float], step_seconds: int,
                  settle_seconds: int, out_dir: Path) -> dict[str, Any]:
    deploy_lane(cfg, lane, "static", static_replicas=1, startup_delay=0)
    env = {"TARGET": f"http://{APP}.{lane}.svc", "STEPS": ",".join(f"{s:g}" for s in steps),
           "STEP_SECONDS": str(step_seconds), "SETTLE_SECONDS": str(settle_seconds)}
    run_id = time.strftime("capacity-%Y%m%d-%H%M%S")
    started = time.time()
    job = start_k6(cfg, namespace=lane, run_id=run_id, script="capacity.js", env=env)
    res = finish_k6(lane, job, started, timeout_s=len(steps) * (step_seconds + 10) + 600)
    if not res.summary:
        raise CommandError(f"capacity test produced no summary; see `kubectl -n {lane} logs "
                           f"job/{job}`")
    capacity = choose_capacity(res.summary["steps"], cfg.slo.p95_latency_ms, cfg.slo.error_rate)
    report = {"run_id": run_id, "lane": lane, "work_units": cfg.app.work_units,
              "slo": {"p95_latency_ms": cfg.slo.p95_latency_ms, "error_rate": cfg.slo.error_rate},
              "per_pod_rps": capacity, **res.summary}
    out_dir.mkdir(parents=True, exist_ok=True)
    write_text(out_dir / "capacity.json", json.dumps(report, indent=2))
    return report


def _ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _newest_pod(lane: str) -> dict[str, Any]:
    pods = kubectl_json("-n", lane, "get", "pods", "-l", f"app.kubernetes.io/name={APP}")
    return max(pods["items"], key=lambda p: p["metadata"]["creationTimestamp"])


def _breakdown(pod: dict[str, Any]) -> dict[str, float | None]:
    created = _ts(pod["metadata"]["creationTimestamp"])
    conds = {c["type"]: _ts(c.get("lastTransitionTime"))
             for c in pod.get("status", {}).get("conditions", [])}
    started = None
    for cs in pod.get("status", {}).get("containerStatuses", []):
        started = _ts(cs.get("state", {}).get("running", {}).get("startedAt")) or started

    def span(a: datetime | None, b: datetime | None) -> float | None:
        return (b - a).total_seconds() if a and b else None

    return {"schedule_s": span(created, conds.get("PodScheduled")),
            "container_start_s": span(conds.get("PodScheduled"), started),
            "readiness_s": span(started, conds.get("Ready")),
            "create_to_ready_s": span(created, conds.get("Ready"))}


def measure_lag(cfg: Config, lane: str, startup_delay: int, samples: int,
                out_dir: Path) -> dict[str, Any]:
    """Scale +1 replica `samples` times and time each step until the new pod is Ready."""
    deploy_lane(cfg, lane, "off", startup_delay=startup_delay)
    base = cfg.capacity.min_replicas
    scale = ["kubectl", "-n", lane, "scale", f"deployment/{APP}"]
    run([*scale, f"--replicas={base}"])
    wait_ready_replicas(lane, APP, base, timeout_s=900)
    rows = []
    for i in range(samples):
        run([*scale, f"--replicas={base + 1}"], quiet=True)
        observed = wait_ready_replicas(lane, APP, base + 1, timeout_s=startup_delay + 600)
        rows.append({"sample": i + 1, "observed_s": round(observed, 1),
                     **_breakdown(_newest_pod(lane))})
        print(f"sample {i + 1}/{samples}: {observed:.1f}s to ready", flush=True)
        run([*scale, f"--replicas={base}"], quiet=True)
        wait_ready_replicas(lane, APP, base, timeout_s=300)
        time.sleep(5)
    observed = [r["observed_s"] for r in rows]
    report = {"lane": lane, "startup_delay_seconds": startup_delay, "samples": rows,
              "observed_mean_s": statistics.mean(observed),
              "observed_max_s": max(observed),
              "note": "Pod-level lag only. Add metric scrape (15 s), rate window (~30 s), "
                      "forecaster cycle (30 s) and KEDA/HPA sync (15 s) for the full loop."}
    out_dir.mkdir(parents=True, exist_ok=True)
    write_text(out_dir / f"lag-d{startup_delay}.json", json.dumps(report, indent=2))
    return report
