"""Load and validate pas.yaml, the single source of truth for the study."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG = "pas.yaml"


class ConfigError(ValueError):
    """Raised when pas.yaml is missing a value or holds an invalid one."""


@dataclass(frozen=True)
class TraceConfig:
    archive_url: str
    raw_dir: Path
    processed_dir: Path
    signal: str = "total"
    trigger: str | None = None
    start_date: str = "2019-07-15"
    days: int = 14
    train_days: tuple[int, int] = (1, 10)
    val_days: tuple[int, int] = (11, 12)
    test_days: tuple[int, int] = (13, 14)


@dataclass(frozen=True)
class CapacityConfig:
    per_pod_rps: float
    min_replicas: int
    max_replicas: int
    peak_utilisation: float

    @property
    def target_peak_rps(self) -> float:
        return self.peak_utilisation * self.max_replicas * self.per_pod_rps


@dataclass(frozen=True)
class SloConfig:
    p95_latency_ms: float
    error_rate: float


@dataclass(frozen=True)
class AppConfig:
    startup_delay_seconds: int
    work_units: int


@dataclass(frozen=True)
class ForecastConfig:
    horizon_minutes: int
    lead_minutes: tuple[int, int]
    headroom: float
    history_minutes: int
    interval_seconds: int

    def keda_threshold(self, per_pod_rps: float) -> float:
        return per_pod_rps * (1.0 - self.headroom)


@dataclass(frozen=True)
class WindowConfig:
    name: str
    description: str = ""
    day: int | None = None
    start: str | None = None
    minutes: int | None = None
    base: str | None = None
    spike: dict[str, float] | None = None


@dataclass(frozen=True)
class BacktestConfig:
    stride_minutes: int
    horizons: tuple[int, ...]


@dataclass(frozen=True)
class ClusterConfig:
    prometheus_service: str
    grafana_service: str
    k6_image: str
    acr_login_server: str = ""


@dataclass(frozen=True)
class Config:
    root: Path
    trace: TraceConfig
    capacity: CapacityConfig
    slo: SloConfig
    app: AppConfig
    forecast: ForecastConfig
    windows: tuple[WindowConfig, ...]
    scalers: dict[str, dict[str, Any]]
    lanes: dict[str, dict[str, Any]]
    backtest: BacktestConfig
    cluster: ClusterConfig
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    # Convenience paths -------------------------------------------------------------------
    @property
    def series_path(self) -> Path:
        return self.trace.processed_dir / "series.parquet"

    @property
    def windows_dir(self) -> Path:
        return self.root / "loadgen" / "windows"

    @property
    def models_dir(self) -> Path:
        return self.root / "artifacts" / "models"

    @property
    def reports_dir(self) -> Path:
        return self.root / "reports"

    def window(self, name: str) -> WindowConfig:
        for w in self.windows:
            if w.name == name:
                return w
        raise ConfigError(f"window {name!r} is not defined in pas.yaml (have: "
                          f"{', '.join(w.name for w in self.windows)})")

    def scaler(self, name: str) -> dict[str, Any]:
        if name not in self.scalers:
            raise ConfigError(f"scaler preset {name!r} is not defined in pas.yaml (have: "
                              f"{', '.join(self.scalers)})")
        return self.scalers[name]

    def required_replicas(self, rps: float) -> int:
        """Replicas needed to serve `rps` at measured per-pod capacity (no headroom)."""
        return max(self.capacity.min_replicas, math.ceil(rps / self.capacity.per_pod_rps))


def _pair(value: Any, name: str) -> tuple[int, int]:
    if not (isinstance(value, list | tuple) and len(value) == 2):
        raise ConfigError(f"{name} must be a two-element list, got {value!r}")
    lo, hi = int(value[0]), int(value[1])
    if lo > hi:
        raise ConfigError(f"{name}: first value must be <= second, got {value!r}")
    return lo, hi


def _require(d: dict[str, Any], key: str, section: str) -> Any:
    if key not in d:
        raise ConfigError(f"pas.yaml: missing '{section}.{key}'")
    return d[key]


def load_config(path: str | Path = DEFAULT_CONFIG) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"config file not found: {path.resolve()} "
                          "(run from the repository root or pass --config)")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    root = path.resolve().parent

    t = raw.get("trace", {})
    trace = TraceConfig(
        archive_url=_require(t, "archive_url", "trace"),
        raw_dir=root / t.get("raw_dir", "data/raw"),
        processed_dir=root / t.get("processed_dir", "data/processed"),
        signal=str(t.get("signal", "total")),
        trigger=t.get("trigger"),
        start_date=str(t.get("start_date", "2019-07-15")),
        days=int(t.get("days", 14)),
        train_days=_pair(t.get("train_days", [1, 10]), "trace.train_days"),
        val_days=_pair(t.get("val_days", [11, 12]), "trace.val_days"),
        test_days=_pair(t.get("test_days", [13, 14]), "trace.test_days"),
    )
    if not (trace.train_days[1] < trace.val_days[0] <= trace.val_days[1] < trace.test_days[0]):
        raise ConfigError("trace: train_days < val_days < test_days must not overlap")

    c = raw.get("capacity", {})
    capacity = CapacityConfig(
        per_pod_rps=float(_require(c, "per_pod_rps", "capacity")),
        min_replicas=int(c.get("min_replicas", 2)),
        max_replicas=int(c.get("max_replicas", 20)),
        peak_utilisation=float(c.get("peak_utilisation", 0.75)),
    )
    if capacity.per_pod_rps <= 0 or not 0 < capacity.peak_utilisation <= 1:
        raise ConfigError("capacity: per_pod_rps must be > 0 and peak_utilisation in (0, 1]")
    if not 1 <= capacity.min_replicas <= capacity.max_replicas:
        raise ConfigError("capacity: need 1 <= min_replicas <= max_replicas")

    s = raw.get("slo", {})
    slo = SloConfig(p95_latency_ms=float(s.get("p95_latency_ms", 300)),
                    error_rate=float(s.get("error_rate", 0.01)))

    a = raw.get("app", {})
    app = AppConfig(startup_delay_seconds=int(a.get("startup_delay_seconds", 60)),
                    work_units=int(a.get("work_units", 20)))

    f = raw.get("forecast", {})
    forecast = ForecastConfig(
        horizon_minutes=int(f.get("horizon_minutes", 10)),
        lead_minutes=_pair(f.get("lead_minutes", [2, 4]), "forecast.lead_minutes"),
        headroom=float(f.get("headroom", 0.2)),
        history_minutes=int(f.get("history_minutes", 4320)),
        interval_seconds=int(f.get("interval_seconds", 30)),
    )
    if not 1 <= forecast.lead_minutes[0] <= forecast.lead_minutes[1] <= forecast.horizon_minutes:
        raise ConfigError("forecast: need 1 <= lead_minutes[0] <= lead_minutes[1] "
                          "<= horizon_minutes")
    if not 0 <= forecast.headroom < 1:
        raise ConfigError("forecast.headroom must be in [0, 1)")

    windows = tuple(
        WindowConfig(
            name=str(w["name"]),
            description=str(w.get("description", "")),
            day=w.get("day"),
            start=w.get("start"),
            minutes=w.get("minutes"),
            base=w.get("base"),
            spike=w.get("spike"),
        )
        for w in raw.get("windows", [])
    )
    names = [w.name for w in windows]
    if len(names) != len(set(names)):
        raise ConfigError("windows: names must be unique")

    b = raw.get("backtest", {})
    backtest = BacktestConfig(stride_minutes=int(b.get("stride_minutes", 5)),
                              horizons=tuple(int(h) for h in b.get("horizons", [1, 3, 5, 10])))

    k = raw.get("cluster", {})
    cluster = ClusterConfig(
        prometheus_service=k.get("prometheus_service",
                                 "monitoring/kube-prometheus-stack-prometheus"),
        grafana_service=k.get("grafana_service", "monitoring/kube-prometheus-stack-grafana"),
        k6_image=k.get("k6_image", "grafana/k6:2.2.0"),
        acr_login_server=k.get("acr_login_server", "") or "",
    )

    scalers = dict(raw.get("scalers", {}))
    for name, preset in scalers.items():
        if not isinstance(name, str):
            raise ConfigError(f"scalers: preset name {name!r} is not a string; quote it in "
                              'pas.yaml (YAML reads bare off/on/yes/no as booleans)')
        if preset.get("mode") not in ("off", "static", "hpa", "predictive"):
            raise ConfigError(f"scalers.{name}.mode must be off, static, hpa or predictive")
        if preset["mode"] == "predictive" and "model" not in preset:
            raise ConfigError(f"scalers.{name}: predictive presets need a model")

    return Config(
        root=root,
        trace=trace,
        capacity=capacity,
        slo=slo,
        app=app,
        forecast=forecast,
        windows=windows,
        scalers=scalers,
        lanes=dict(raw.get("lanes", {})),
        backtest=backtest,
        cluster=cluster,
        raw=raw,
    )
