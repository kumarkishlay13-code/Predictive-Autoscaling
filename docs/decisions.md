# Decision log

Short architecture decision records (ADRs). Each one states what was decided, why, and what it
costs. Add a new entry whenever a choice would need explaining in the methodology chapter;
never rewrite an old one, supersede it.

| # | Decision | Status |
|---|----------|--------|
| 001 | Replay the Azure Functions 2019 trace as load, instead of training on VM CPU traces | accepted |
| 002 | Forecast demand (requests per second), not CPU | accepted |
| 003 | Hybrid scaler: KEDA with a CPU trigger and a forecast trigger, HPA takes the max | accepted |
| 004 | Configurable start-up delay in the demo app | accepted |
| 005 | No ingress controller; offered load counted on arrival in the app | accepted |
| 006 | No local Kubernetes; images built in Azure (ACR Tasks) | superseded by 013 |
| 007 | Prophet used offline only | accepted |
| 008 | Go for the workload, Python for everything else | accepted |
| 009 | All configuration in one file, `pas.yaml` | accepted |
| 010 | Load signal chosen by exploratory analysis, not the trace total | accepted |
| 011 | Flash-crowd spikes capped at 95 % of cluster capacity | accepted |
| 012 | ARIMA(1,1,1) with log-normal bias correction, chosen on validation days | accepted |
| 013 | Run the study on a local kind cluster; AKS becomes optional | accepted |

---

## ADR-001 Replay a real trace as load

**Context.** The outline trained models on Azure VM CPU traces (5-minute resolution) but
evaluated them on the app's own metrics under k6 load. Those are different signals, so accuracy
on one says nothing about the other.

**Decision.** Scale the Azure Functions 2019 invocation trace (per-minute) to the cluster and
replay it with k6 (`ramping-arrival-rate`, open model). Train on days 1–10, validate on 11–12,
and take every live window from days 13–14.

**Consequences.** The offered load is known exactly (the window file), which gives ground truth
for "required replicas". The "real-world data" claim stays valid. Live runs use 2-hour windows,
not whole days, to keep Azure cost manageable.

## ADR-002 Forecast RPS

**Context.** Per-pod CPU falls when the scaler adds pods, so a CPU forecaster would learn from a
signal its own decisions change.

**Decision.** Forecast offered RPS and convert with `replicas = ceil(rps / (per_pod_rps × (1 −
headroom)))`, where `per_pod_rps` is measured by `pas capacity`.

**Consequences.** Needs a capacity test, and capacity must be re-measured if the app or node
size changes.

## ADR-003 Hybrid scaler

**Decision.** Predictive presets deploy a KEDA `ScaledObject` with two triggers: CPU (same target
as the tuned HPA) and `max(pas_predicted_rps)` from Prometheus. The HPA KEDA creates uses the
larger of the two. If the forecaster stops publishing (stale or crashed), the Prometheus query
returns nothing, KEDA treats it as 0, and the CPU trigger takes over.

**Consequences.** Prediction can only add capacity early, never remove the reactive safety net.
Predictive presets share the tuned HPA's behaviour and CPU target (YAML anchors in `pas.yaml`),
so "predictive vs hpa-tuned" isolates the effect of the forecast. A forecast-only ablation
(`arima-only`) is kept for comparison.

## ADR-004 Start-up delay

**Context.** A small Go service is ready in about a second, so reactive scaling looks almost as
good as prediction. Real services (JVM, caches, model loading) often take 30–120 s.

**Decision.** `STARTUP_DELAY_SECONDS` delays readiness. The main study uses 60 s; a sensitivity
study uses 0/30/60/120 s.

**Consequences.** The delay must be justified in the report (cite typical warm-up times) and the
0 s case reported as well.

## ADR-005 No ingress controller

**Context.** The plan measured offered load at ingress-nginx. The ingress-nginx project was
retired in 2026, and an ingress adds a component that can itself saturate.

**Decision.** k6 runs inside the cluster and calls the `demo-api` Service directly.
`demo_requests_received_total` is incremented on arrival, before any work, so it counts offered
load even when a pod is saturated. k6 opens a new connection per request (`noConnectionReuse`),
so kube-proxy spreads load over newly added pods.

**Consequences.** Client-side latency and errors come from k6 (Prometheus remote write), which is
the primary SLO measure. Server-side latency is kept as a cross-check.

## ADR-006 No local Kubernetes

*Superseded by ADR-013.*

**Context.** The development laptop has 3.9 GB RAM. Docker Desktop with a kind cluster running
Prometheus, Grafana and KEDA does not fit.

**Decision.** Everything that does not need a cluster (data, models, backtests, forecaster, the
Go app, k6 scripts) runs and is tested locally. Images are built in Azure with `az acr build`.
All cluster work happens on AKS.

**Consequences.** Iterating on manifests costs a little Azure time; `helm template` and the CI
job catch most mistakes before that.

## ADR-007 Prophet offline only

Prophet's forecast depends only on time features, not on the latest observations, so it cannot
react to a burst in progress, and refitting at minute resolution is slow. It appears in the
offline accuracy comparison and as the seasonal part of the optional hybrid Prophet-LSTM, not as
a live scaler.

## ADR-008 Languages

The workload is Go: predictable CPU per request, tiny `scratch` image, fast start. Everything
else (data, models, service, orchestration, analysis) is one Python package with one CLI, so
there is a single toolchain to learn and test.

## ADR-009 One configuration file

Capacity, SLO, forecast settings, windows, scaler presets and lanes all live in `pas.yaml`.
Helm values are generated from it, and every run's `meta.json` stores a copy, so a result can
always be traced back to its exact settings.

## ADR-010 Choose the load signal by exploratory analysis

**Context.** The first build used the sum of all functions in the trace. It is very regular
(daily autocorrelation 0.62) but almost flat: the daily swing is only about 1.5×, so scaled to
the cluster the scaler would move between roughly 11 and 15 replicas. That is too little
dynamic range to compare scalers.

**Decision.** Rank the busiest applications with `pas data apps` (daily regularity × variation ×
share of non-idle minutes) and use one application's load as the signal (`trace.signal` in
`pas.yaml`). The chosen app and its statistics are recorded in `data/processed/series.json`.

**Consequences.** A single application is also a more realistic per-service load. The choice is
a parameter, so the study can be repeated on other applications.

## ADR-011 Cap flash-crowd spikes

A 3× spike on top of the busiest period asked for about 41 replicas with a cap of 20, which
tests `maxReplicas`, not the scaler. Spikes are capped at 95 % of full cluster capacity
(`SPIKE_CAP`), and the window file records whether the cap applied (`spike_capped`).

## ADR-012 ARIMA specification

**Context.** The first ARIMA(2,0,1) under-predicted 58–67 % of the time on held-out days.
Fitting on log1p(load) and transforming back with exp yields the median, not the mean.

**Decision.** Apply the log-normal correction exp(μ + σ²/2) to point forecasts; quantile
forecasts use μ + z·σ (quantiles are preserved by monotone transforms). The order was chosen on
the **validation days only** (stride 15 min, horizons 1–10). The test days stay untouched for
reporting:

| Order, trend | MASE h1 | h3 | h5 | h10 | under-predicted h1 / h10 |
|---|---|---|---|---|---|
| (2,0,1), const, no correction | 0.415 | 0.711 | 0.921 | 1.161 | 59 % / 60 % |
| (2,0,1), const, corrected | 0.383 | 0.666 | 0.934 | 1.167 | 50 % / 45 % |
| **(1,1,1), none, corrected** | **0.285** | **0.505** | **0.711** | 1.013 | 40 % / 17 % |
| (2,1,2), none, corrected | 0.390 | 0.710 | 1.024 | 1.355 | 47 % / 34 % |

**Consequences.** At long horizons the corrected forecast leans high, the safe side for
autoscaling. The P90 interval is conservative (3–4 % of observations above it rather than 10 %),
so `arima-p90` will over-provision somewhat. Report this calibration with the results.

## ADR-013 Run the study on a local kind cluster

**Context.** There is no Azure subscription available for the study, and the experiments should
not depend on a cloud account. ADR-006 assumed the 3.9 GB development laptop was the only
machine; the experiments will now run on a separate workstation with enough CPU and memory.

**Decision.** The default platform is a [kind](https://kind.sigs.k8s.io) cluster in Docker
(`cluster.provider: local`):

- Four worker nodes stand in for the AKS pools, with the same `pas.dev/pool` labels and taints:
  system, app, appb and loadgen (`infra/kind/cluster.yaml`). The node image is pinned by digest
  to Kubernetes 1.36.
- AKS provides KEDA and metrics-server; locally, `pas platform install` installs them with Helm,
  with pinned chart versions.
- Images are built with `docker build` and copied into the nodes with `kind load`. No registry
  is needed.
- AKS stays available unchanged as `cluster.provider: aks`. The system pool now carries the
  same `pas.dev/pool=system` label on both providers, so the chart and monitoring values are
  shared.
- `pas smoke` and a CI job on a real kind cluster check the whole control loop end to end.

**Consequences.**

1. **Lanes are not isolated.** Every kind node reports and shares the host's CPUs. Pod CPU
   limits (CFS quota) still fix per-pod capacity, which is what the scalers act on. But two
   lanes at once compete for the host, so measured runs use one lane at a time. Parallel lanes
   are only for the demo.
2. **Pod start-up is faster than on AKS.** Images are already on the nodes and no VM is ever
   provisioned, so the provisioning lag is almost entirely the configured warm-up (ADR-004).
   This makes the start-up-delay sensitivity study the main lever for lag. The report must say
   that node scale-out (Cluster Autoscaler) is out of scope.
3. **Capacity and lag belong to the workstation.** `pas capacity` and `pas lag` must run on the
   machine that runs the experiments, and the results must be reported with its CPU model.
4. **Generality.** HPA, KEDA, metrics-server, kube-state-metrics and Prometheus are the same
   upstream components AKS uses, so the comparison of scaling policies carries over. Claims
   specific to AKS (managed control-plane latency, VM start-up) cannot be made from these runs.
5. **The study costs nothing to run**, so repetitions are limited only by time. The full matrix
   is about 136 hours.
