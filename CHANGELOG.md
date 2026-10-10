# Changelog

All notable changes to this project. Versions follow [semantic versioning](https://semver.org).

## [0.1.0] - 2026-10-10

First complete version, covering plan weeks 1–6 plus the model and experiment tooling for
weeks 7–14.

### Added
- `demo-api`: Go workload with fixed CPU cost per request, configurable warm-up, graceful
  drain and Prometheus metrics (offered load counted on arrival).
- `pas` CLI: one entry point for data, training, backtests, infrastructure, image builds,
  lanes, capacity and lag measurement, experiments, analysis and the demo.
- Data pipeline for the Azure Functions 2019 trace: chunked loading, app ranking, scaling to
  cluster capacity, automatic ramp/peak windows, capped flash-crowd spikes.
- Forecasters: seasonal naive, last value, ARIMA with daily Fourier terms, LSTM (mean and P90
  pinball loss), Prophet (offline), oracle. Empirical or model-based quantiles.
- Rolling-origin backtests with MAE, RMSE, MASE, sMAPE, under-prediction rate and pinball loss.
- Forecaster service: trace-clock alignment, staleness withdrawal, Prometheus metrics, control
  endpoints.
- Helm chart `workload` with off/static/hpa/predictive modes, values schema and restricted pod
  security.
- Terraform for AKS (system/app/loadgen pools, KEDA add-on), ACR with AcrPull, budget alerts.
- k6 replay (open model) and capacity scripts, k6 remote write to Prometheus.
- Experiment runner: shuffled, resumable plans, parallel lanes, per-run export to parquet.
- Analysis: per-run SLO and provisioning metrics, 95 % CIs, Mann–Whitney U, figures.
- Grafana dashboard, CI workflow, architecture docs, decision log, runbook.
