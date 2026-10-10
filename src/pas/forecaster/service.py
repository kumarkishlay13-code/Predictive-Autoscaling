"""HTTP service that runs the forecaster every N seconds and exposes the result to Prometheus.

Endpoints
  GET  /healthz         liveness
  GET  /readyz          200 once the first forecast succeeded
  GET  /metrics         Prometheus metrics (KEDA reads pas_predicted_rps through Prometheus)
  GET  /forecast        latest forecast as JSON (debugging, demo)
  POST /control/start   start of the replay window; body {"at": <epoch seconds>} optional
  POST /control/reset   forget the start time

Configuration comes from environment variables (see `Settings.from_env`).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from pas.data.windows import load_window, window_series
from pas.forecaster.engine import Engine, ForecastResult
from pas.models import Oracle, SeasonalNaive, load_model
from pas.models.base import Forecaster
from pas.prometheus import Prometheus

log = logging.getLogger("pas.forecaster")


@dataclass(frozen=True)
class Settings:
    model: str = "naive"
    model_path: str = ""
    lane: str = "default"
    prometheus_url: str = "http://localhost:9090"
    query: str = ""
    window_file: str = ""
    interval_seconds: float = 30.0
    horizon: int = 10
    lead: tuple[int, int] = (2, 4)
    quantile: float | None = None
    history_minutes: int = 4320
    max_stale_cycles: int = 2

    @property
    def observed_query(self) -> str:
        return self.query or (
            f'sum(rate(demo_requests_received_total{{namespace="{self.lane}"}}[1m]))')

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ.get
        lead = env("PAS_LEAD", "2,4").split(",")
        quantile = env("PAS_QUANTILE", "")
        return cls(
            model=env("PAS_MODEL", "naive"),
            model_path=env("PAS_MODEL_PATH", ""),
            lane=env("PAS_LANE", "default"),
            prometheus_url=env("PAS_PROMETHEUS_URL", "http://localhost:9090"),
            query=env("PAS_QUERY", ""),
            window_file=env("PAS_WINDOW_FILE", ""),
            interval_seconds=float(env("PAS_INTERVAL_SECONDS", "30")),
            horizon=int(env("PAS_HORIZON", "10")),
            lead=(int(lead[0]), int(lead[1])),
            quantile=float(quantile) if quantile else None,
            history_minutes=int(env("PAS_HISTORY_MINUTES", "4320")),
            max_stale_cycles=int(env("PAS_MAX_STALE_CYCLES", "2")),
        )


def build_model(settings: Settings, window: dict | None) -> Forecaster:
    if settings.model == "oracle":
        if window is None:
            raise ValueError("the oracle model needs PAS_WINDOW_FILE")
        return Oracle(window_series(window))
    path = settings.model_path or f"artifacts/models/{settings.model}.pkl"
    if Path(path).exists():
        return load_model(path)
    if settings.model == "naive" and settings.quantile is None:
        log.warning("no artifact at %s; using an unfitted seasonal-naive model", path)
        return SeasonalNaive()
    raise FileNotFoundError(f"model artifact not found: {path} (run `pas train {settings.model}`)")


class Metrics:
    def __init__(self, registry: CollectorRegistry) -> None:
        self.predicted = Gauge("pas_predicted_rps",
                               "Peak forecast RPS inside the lead window (drives KEDA)",
                               ["lane", "model"], registry=registry)
        self.forecast = Gauge("pas_forecast_rps", "Forecast RPS per minute ahead",
                              ["lane", "model", "horizon"], registry=registry)
        self.observed = Gauge("pas_observed_rps", "Last observed RPS used as model input",
                              ["lane"], registry=registry)
        self.last_success = Gauge("pas_forecast_last_success_timestamp_seconds",
                                  "Unix time of the last successful forecast", ["lane"],
                                  registry=registry)
        self.errors = Counter("pas_forecast_errors", "Failed forecast cycles", ["lane"],
                              registry=registry)
        self.duration = Histogram("pas_forecast_duration_seconds",
                                  "Time to build history and predict", ["lane"],
                                  buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
                                  registry=registry)


class ForecasterService:
    """Owns the engine, the metrics and the staleness rule."""

    def __init__(self, settings: Settings, engine: Engine,
                 registry: CollectorRegistry | None = None) -> None:
        self.settings = settings
        self.engine = engine
        self.registry = registry or CollectorRegistry()
        self.metrics = Metrics(self.registry)
        self.latest: ForecastResult | None = None
        self.failures = 0
        self._published = False

    @property
    def model_label(self) -> str:
        return self.settings.model

    def run_cycle(self, now: float | None = None) -> ForecastResult | None:
        now = time.time() if now is None else now
        lane = self.settings.lane
        t0 = time.perf_counter()
        try:
            result = self.engine.step(now)
        except Exception as exc:  # noqa: BLE001 - any failure must not kill the loop
            self.failures += 1
            self.metrics.errors.labels(lane).inc()
            log.warning("forecast cycle failed (%d in a row): %s", self.failures, exc)
            if self.failures > self.settings.max_stale_cycles and self._published:
                # Stop publishing so KEDA falls back to the CPU trigger instead of acting on
                # a stale forecast.
                self.metrics.predicted.remove(lane, self.model_label)
                self._published = False
                log.error("forecast is stale; stopped publishing pas_predicted_rps")
            return None
        self.metrics.duration.labels(lane).observe(time.perf_counter() - t0)
        self.failures = 0
        self.latest = result
        self.metrics.predicted.labels(lane, self.model_label).set(result.predicted_rps)
        self._published = True
        for h, value in enumerate(result.forecast, start=1):
            self.metrics.forecast.labels(lane, self.model_label, str(h)).set(float(value))
        if result.last_observed is not None:
            self.metrics.observed.labels(lane).set(result.last_observed)
        self.metrics.last_success.labels(lane).set(now)
        return result


def create_app(settings: Settings | None = None,
               service: ForecasterService | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    if service is None:
        window = load_window(settings.window_file) if settings.window_file else None
        prom = Prometheus(settings.prometheus_url)
        query = settings.observed_query
        engine = Engine(
            model=build_model(settings, window),
            fetch=lambda start, end, step: prom.query_range_single(query, start, end, step),
            horizon=settings.horizon,
            lead=settings.lead,
            quantile=settings.quantile,
            history_minutes=settings.history_minutes,
            warm_start=window_series(window).iloc[:len(window["warm_start_rps"])]
            if window else None,
        )
        service = ForecasterService(settings, engine)

    async def loop() -> None:
        while True:
            await asyncio.to_thread(service.run_cycle)
            await asyncio.sleep(settings.interval_seconds)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        task = asyncio.create_task(loop())
        log.info("forecaster started: model=%s lane=%s interval=%ss", settings.model,
                 settings.lane, settings.interval_seconds)
        yield
        task.cancel()

    app = FastAPI(title="pas forecaster", lifespan=lifespan)
    app.state.service = service

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> dict:
        if service.latest is None:
            raise HTTPException(503, "no successful forecast yet")
        return {"status": "ready"}

    @app.get("/metrics")
    def metrics() -> Response:
        return Response(generate_latest(service.registry), media_type=CONTENT_TYPE_LATEST)

    @app.get("/forecast")
    def forecast() -> dict:
        if service.latest is None:
            raise HTTPException(503, "no successful forecast yet")
        return {"model": settings.model, "lane": settings.lane, **service.latest.as_dict()}

    @app.post("/control/start")
    async def control_start(body: dict | None = Body(default=None)) -> dict:
        at = float((body or {}).get("at") or time.time())
        try:
            service.engine.start(at)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        await asyncio.to_thread(service.run_cycle)
        return {"anchor": at, "window_start": str(service.engine.window_start)}

    @app.post("/control/reset")
    def control_reset() -> dict:
        service.engine.reset()
        return {"anchor": None}

    return app
