# Changelog

All notable changes to this project. Versions follow [semantic versioning](https://semver.org).

## [0.2.0] - 2026-10-25

The study now runs entirely on one machine, with no cloud account (ADR-013).

### Added
- Local platform, now the default (`cluster.provider: local`): a kind cluster
  (`infra/kind/cluster.yaml`, Kubernetes 1.36 pinned by digest) whose workers stand in for the
  system, app, appb and loadgen pools. `pas infra up/stop/start/down` creates, pauses and
  deletes it.
- `pas platform install` installs metrics-server 3.14.0 and KEDA 2.21.0 with Helm on the local
  cluster, before kube-prometheus-stack.
- `pas build` builds images with the local Docker and loads each into the kind nodes that run
  it. No registry is needed.
- `pas smoke`: a time-compressed replay of a few minutes, then a checklist of every link of the
  control loop (k6, demo-api, Prometheus, metrics-server, HPA, forecaster, KEDA).
- Kube-context guard: commands that act on the cluster refuse to run unless kubectl points at
  the study's cluster.
- CI job `kind`: runs `pas infra up`, `platform install`, `build` and `smoke` on a real kind
  cluster.
- [docs/setup-and-testing.md](docs/setup-and-testing.md): installation and testing on a new
  machine.

### Changed
- The system pool is selected by the label `pas.dev/pool=system` on both providers. The AKS
  default node pool now carries that label.
- kube-prometheus-stack: node-exporter disabled (unused, and it breaks on Docker Desktop);
  Prometheus storage uses the cluster's default storage class.
- k6 Jobs request 500m CPU instead of 1, so two fit on a 2-vCPU loadgen node.
- `pas doctor` checks the tools of the selected provider, Docker's CPUs and memory, the kube
  context, every node pool and metrics-server. k6 and Go are reported as optional.
- The runbook and README are local-first; the AKS procedure moved to the runbook's appendix A.
- ADR-006 (no local Kubernetes) is superseded by ADR-013.

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
