"""Run k6 as a Kubernetes Job on the dedicated loadgen pool and collect its summary."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pas.config import Config
from pas.kube import CommandError, apply, configmap, job_logs, run, wait_job

MARKER = re.compile(r"(PAS_SUMMARY|PAS_CAPACITY) (\{.*\})")


@dataclass
class K6Result:
    job: str
    ok: bool
    started: float
    finished: float
    summary: dict[str, Any] | None
    log: str


def prometheus_url(cfg: Config) -> str:
    namespace, name = cfg.cluster.prometheus_service.split("/", 1)
    return f"http://{name}.{namespace}.svc:9090"


def k6_job(cfg: Config, *, name: str, namespace: str, script: str, run_id: str,
           env: dict[str, str], with_window: bool) -> dict:
    env = {
        "K6_PROMETHEUS_RW_SERVER_URL": f"{prometheus_url(cfg)}/api/v1/write",
        "K6_PROMETHEUS_RW_TREND_STATS": "p(95),p(99),avg",
        "K6_PROMETHEUS_RW_PUSH_INTERVAL": "5s",
        **env,
    }
    labels = {"app.kubernetes.io/part-of": "pas", "app.kubernetes.io/name": "k6",
              "pas.dev/run": run_id}
    volumes = [{"name": "scripts", "configMap": {"name": "pas-k6-scripts"}}]
    mounts = [{"name": "scripts", "mountPath": "/scripts", "readOnly": True}]
    if with_window:
        volumes.append({"name": "window", "configMap": {"name": f"{name}-window"}})
        mounts.append({"name": "window", "mountPath": "/windows", "readOnly": True})
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
        "spec": {
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": 86400,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    "nodeSelector": {"pas.dev/pool": "loadgen"},
                    "tolerations": [{"key": "pas.dev/pool", "operator": "Equal",
                                     "value": "loadgen", "effect": "NoSchedule"}],
                    "containers": [{
                        "name": "k6",
                        "image": cfg.cluster.k6_image,
                        "args": ["run", "--no-usage-report", "-o", "experimental-prometheus-rw",
                                 "--tag", f"lane={namespace}", "--tag", f"run={run_id}",
                                 f"/scripts/{script}"],
                        "env": [{"name": k, "value": str(v)} for k, v in env.items()],
                        # 500m leaves room for two Jobs (demo, parallel lanes) on one
                        # 2-vCPU loadgen node; k6 may burst above it.
                        "resources": {"requests": {"cpu": "500m", "memory": "1Gi"},
                                      "limits": {"memory": "3Gi"}},
                        "volumeMounts": mounts,
                    }],
                    "volumes": volumes,
                },
            },
        },
    }


def start_k6(cfg: Config, *, namespace: str, run_id: str, script: str, env: dict[str, str],
             window: Path | None = None) -> str:
    """Create the script/window ConfigMaps and the Job; return the Job name."""
    name = f"k6-{run_id}".lower()[:52].rstrip("-")
    loadgen = cfg.root / "loadgen"
    manifests = [configmap("pas-k6-scripts", namespace,
                           {"replay.js": loadgen / "replay.js",
                            "capacity.js": loadgen / "capacity.js"})]
    if window is not None:
        manifests.append(configmap(f"{name}-window", namespace, {"window.json": window}))
    run(["kubectl", "-n", namespace, "delete", "job", name, "--ignore-not-found"],
        capture=True, quiet=True)
    apply(manifests, quiet=True)
    apply(k6_job(cfg, name=name, namespace=namespace, script=script, run_id=run_id, env=env,
                 with_window=window is not None))
    print(f"k6 job {namespace}/{name} started")
    return name


def finish_k6(namespace: str, job: str, started: float, timeout_s: float) -> K6Result:
    ok = wait_job(namespace, job, timeout_s)
    finished = time.time()
    try:
        log = job_logs(namespace, job)
    except CommandError as exc:
        log = f"(could not read logs: {exc})"
    return K6Result(job=job, ok=ok, started=started, finished=finished,
                    summary=parse_summary(log), log=log)


def parse_summary(log: str) -> dict[str, Any] | None:
    found = None
    for line in log.splitlines():
        m = MARKER.search(line)
        if m:
            found = json.loads(m.group(2))
    return found
