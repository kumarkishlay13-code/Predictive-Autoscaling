"""Deploy a lane: one namespace with demo-api and the scaler under test (Helm chart `workload`)."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import yaml

from pas.config import Config, ConfigError
from pas.infra import image_ref
from pas.kube import ensure_namespace, run

RELEASE = "workload"
APP = "demo-api"
FORECASTER = "demo-api-forecaster"


def lane_values(cfg: Config, lane: str, scaler: str, *, startup_delay: int | None = None,
                static_replicas: int | None = None,
                images: dict[str, tuple[str, str]] | None = None) -> dict[str, Any]:
    """Helm values for `lane` running scaler preset `scaler` (from pas.yaml)."""
    if lane not in cfg.lanes:
        raise ConfigError(f"lane {lane!r} is not defined in pas.yaml "
                          f"(have: {', '.join(cfg.lanes)})")
    preset = cfg.scaler(scaler)
    mode = preset["mode"]
    images = images or {}
    app_repo, app_tag = images.get("demo-api") or image_ref(cfg, "demo-api")
    values: dict[str, Any] = {
        "app": {
            "image": {"repository": app_repo, "tag": app_tag},
            "startupDelaySeconds": cfg.app.startup_delay_seconds if startup_delay is None
            else startup_delay,
            "workUnits": cfg.app.work_units,
            "nodePool": cfg.lanes[lane].get("node_pool", ""),
        },
        "scaler": {
            "mode": mode,
            "minReplicas": cfg.capacity.min_replicas,
            "maxReplicas": cfg.capacity.max_replicas,
            "staticReplicas": static_replicas or cfg.capacity.max_replicas,
            "cpuTargetPercent": int(preset.get("cpu_target_percent", 70)),
            "behavior": preset.get("behavior", {}),
            "hybrid": bool(preset.get("hybrid", True)),
            "thresholdRps": round(cfg.forecast.keda_threshold(cfg.capacity.per_pod_rps), 3),
        },
    }
    if mode == "predictive":
        fc_repo, fc_tag = images.get("forecaster") or image_ref(cfg, "forecaster")
        quantile = preset.get("quantile")
        values["forecaster"] = {
            "image": {"repository": fc_repo, "tag": fc_tag},
            "model": preset["model"],
            "quantile": "" if quantile is None else str(quantile),
            "horizon": cfg.forecast.horizon_minutes,
            "lead": f"{cfg.forecast.lead_minutes[0]},{cfg.forecast.lead_minutes[1]}",
            "intervalSeconds": cfg.forecast.interval_seconds,
            "historyMinutes": cfg.forecast.history_minutes,
        }
    return values


def deploy_lane(cfg: Config, lane: str, scaler: str, *, window: Path | None = None,
                startup_delay: int | None = None, static_replicas: int | None = None,
                wait: bool = True) -> dict[str, Any]:
    values = lane_values(cfg, lane, scaler, startup_delay=startup_delay,
                         static_replicas=static_replicas)
    ensure_namespace(lane)
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        yaml.safe_dump(values, fh, sort_keys=False)
        values_file = fh.name
    cmd = ["helm", "upgrade", "--install", RELEASE, str(cfg.root / "charts" / "workload"),
           "-n", lane, "-f", values_file]
    if window is not None and values["scaler"]["mode"] == "predictive":
        # Helm parses --set-file values; backslashes in Windows paths must not reach it.
        cmd += ["--set-file", f"forecaster.window={Path(window).resolve().as_posix()}"]
    if wait:
        cmd += ["--wait", "--timeout", "15m"]
    try:
        run(cmd)
    finally:
        Path(values_file).unlink(missing_ok=True)
    return values
