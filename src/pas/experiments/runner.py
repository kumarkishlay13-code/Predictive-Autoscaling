"""Experiment plan and the per-run protocol (plan §10.4).

A plan is a CSV of runs in randomised order. `run_plan` executes the runs that have no result
yet, so an interrupted session resumes where it stopped. Each run directory holds:

  meta.json            config, images, timings, k6 summary, status
  timeseries.parquet   Prometheus export at 15-s resolution
  window.json          the exact load that was replayed
  k6.log               load-generator output
"""

from __future__ import annotations

import csv
import json
import random
import shutil
import time
import traceback
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from pas.config import Config, ConfigError
from pas.experiments.export import export_run
from pas.experiments.lanes import APP, deploy_lane, lane_values
from pas.experiments.loadgen import finish_k6, start_k6
from pas.infra import default_tag, load_state
from pas.io import write_text
from pas.kube import port_forward, run, wait_ready_replicas
from pas.prometheus import Prometheus


@dataclass(frozen=True)
class RunSpec:
    order: int
    run_id: str
    scaler: str
    window: str
    rep: int
    startup_delay: int


# Planning -----------------------------------------------------------------------------------

def make_plan(cfg: Config, matrix_path: Path) -> list[RunSpec]:
    m = yaml.safe_load(matrix_path.read_text(encoding="utf-8"))
    scalers, windows = list(m["scalers"]), list(m["windows"])
    for s in scalers:
        cfg.scaler(s)
    for w in windows:
        cfg.window(w)
    delays = m.get("startup_delay_seconds", cfg.app.startup_delay_seconds)
    delays = delays if isinstance(delays, list) else [delays]
    reps = int(m.get("repetitions", 3))
    cells = [(s, w, r, d) for s in scalers for w in windows for d in delays
             for r in range(1, reps + 1)]
    for s in m.get("once", []):  # e.g. the static reference needs one run per window
        cfg.scaler(s)
        cells += [(s, w, 1, d) for w in windows for d in delays]
    random.Random(int(m.get("seed", 2026))).shuffle(cells)
    return [RunSpec(order=i + 1, run_id=f"{w}-{s}-d{d}-r{r}", scaler=s, window=w, rep=r,
                    startup_delay=int(d))
            for i, (s, w, r, d) in enumerate(cells)]


def write_plan(plan: list[RunSpec], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(asdict(plan[0])))
        writer.writeheader()
        writer.writerows(asdict(r) for r in plan)


def read_plan(path: Path) -> list[RunSpec]:
    if not path.exists():
        raise ConfigError(f"{path} not found; create it with `pas experiment plan`")
    with path.open(newline="") as fh:
        return [RunSpec(order=int(r["order"]), run_id=r["run_id"], scaler=r["scaler"],
                        window=r["window"], rep=int(r["rep"]),
                        startup_delay=int(r["startup_delay"]))
                for r in csv.DictReader(fh)]


def is_done(results_dir: Path, run_id: str) -> bool:
    meta = results_dir / run_id / "meta.json"
    return meta.exists() and json.loads(meta.read_text()).get("status") == "ok"


def select(plan: list[RunSpec], shard: tuple[int, int] | None = None,
           only: list[str] | None = None) -> list[RunSpec]:
    runs = [r for r in plan if not only or r.run_id in only]
    if shard:
        index, count = shard
        runs = [r for r in runs if (r.order - 1) % count == index]
    return runs


# Running --------------------------------------------------------------------------------------

def _window_path(cfg: Config, name: str) -> Path:
    path = cfg.windows_dir / f"{name}.json"
    if not path.exists():
        raise ConfigError(f"{path} not found; run `pas data build`")
    return path


def reset_lane(cfg: Config, lane: str, startup_delay: int, quiet_seconds: int) -> None:
    """Remove the scaler, return to min replicas and let the lane sit idle."""
    deploy_lane(cfg, lane, "off", startup_delay=startup_delay)
    run(["kubectl", "-n", lane, "scale", f"deployment/{APP}",
         f"--replicas={cfg.capacity.min_replicas}"])
    wait_ready_replicas(lane, APP, cfg.capacity.min_replicas, timeout_s=900)
    if quiet_seconds > 0:
        print(f"quiet period {quiet_seconds}s ...", flush=True)
        time.sleep(quiet_seconds)


def replay_env(cfg: Config, lane: str, scaler: str) -> dict[str, str]:
    predictive = cfg.scaler(scaler)["mode"] == "predictive"
    return {"TARGET": f"http://{APP}.{lane}.svc",
            "FORECASTER": f"http://{APP}-forecaster.{lane}.svc" if predictive else "",
            "WINDOW_FILE": "/windows/window.json"}


def run_one(cfg: Config, spec: RunSpec, lane: str, results_dir: Path,
            quiet_seconds: int = 300, cooldown_seconds: int = 60) -> dict[str, Any]:
    window_path = _window_path(cfg, spec.window)
    window = json.loads(window_path.read_text())
    run_dir = results_dir / spec.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    meta: dict[str, Any] = {
        **asdict(spec), "lane": lane, "status": "running", "git": default_tag(cfg),
        "images": load_state(cfg).get("images", {}),
        "config": {"capacity": cfg.raw.get("capacity"), "slo": cfg.raw.get("slo"),
                   "forecast": cfg.raw.get("forecast"), "app": cfg.raw.get("app")},
        "helm_values": lane_values(cfg, lane, spec.scaler, startup_delay=spec.startup_delay),
    }
    meta_path = run_dir / "meta.json"
    write_text(meta_path, json.dumps(meta, indent=2, default=str))
    shutil.copyfile(window_path, run_dir / "window.json")

    try:
        print(f"\n=== {spec.run_id} (order {spec.order}) on {lane}", flush=True)
        reset_lane(cfg, lane, spec.startup_delay, quiet_seconds)
        deploy_lane(cfg, lane, spec.scaler, window=window_path, startup_delay=spec.startup_delay)

        started = time.time()
        job = start_k6(cfg, namespace=lane, run_id=spec.run_id, script="replay.js",
                       env=replay_env(cfg, lane, spec.scaler), window=window_path)
        result = finish_k6(lane, job, started, timeout_s=len(window["rps"]) * 60 + 900)
        write_text(run_dir / "k6.log", result.log)
        meta.update(started=started, finished=result.finished, k6_summary=result.summary)

        time.sleep(cooldown_seconds)
        load_start = (result.summary or {}).get("started_at") or started
        with port_forward(cfg.cluster.prometheus_service, 9090) as url:
            export_run(Prometheus(url), lane, spec.run_id, load_start - 120,
                       result.finished + 60, run_dir / "timeseries.parquet")
        meta["status"] = "ok" if result.ok and result.summary else "k6-failed"
    except Exception as exc:
        meta.update(status="error", error=str(exc), traceback=traceback.format_exc())
        raise
    finally:
        write_text(meta_path, json.dumps(meta, indent=2, default=str))
    return meta


def run_plan(cfg: Config, plan: list[RunSpec], lane: str, results_dir: Path, *,
             quiet_seconds: int = 300, stop_on_error: bool = False,
             dry_run: bool = False) -> None:
    pending = [r for r in plan if not is_done(results_dir, r.run_id)]
    print(f"{len(pending)} of {len(plan)} selected runs pending on lane {lane}")
    for spec in pending:
        if dry_run:
            print(f"  would run {spec.order:3d} {spec.run_id}")
            continue
        try:
            meta = run_one(cfg, spec, lane, results_dir, quiet_seconds=quiet_seconds)
            print(f"=== {spec.run_id}: {meta['status']}")
        except Exception as exc:  # noqa: BLE001 - keep going unless asked to stop
            print(f"=== {spec.run_id}: ERROR {exc}")
            if stop_on_error:
                raise


# Demo ---------------------------------------------------------------------------------------

def demo_start(cfg: Config, window: str, lanes: dict[str, str]) -> list[str]:
    """Deploy each lane with its scaler and start the same window on all of them at once."""
    window_path = _window_path(cfg, window)
    for lane, scaler in lanes.items():
        reset_lane(cfg, lane, cfg.app.startup_delay_seconds, quiet_seconds=0)
        deploy_lane(cfg, lane, scaler, window=window_path)
    jobs = []
    stamp = time.strftime("%H%M%S")
    for lane, scaler in lanes.items():
        jobs.append(start_k6(cfg, namespace=lane, run_id=f"demo-{scaler}-{stamp}",
                             script="replay.js", env=replay_env(cfg, lane, scaler),
                             window=window_path))
    return jobs
