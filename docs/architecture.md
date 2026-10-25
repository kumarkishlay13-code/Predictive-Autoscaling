# Architecture

## Components

```
                ┌──────────── Kubernetes: local kind cluster (default) or AKS ─────────────┐
                │ loadgen pool             app pool (fixed size)          system pool       │
 window JSON ───┼─► k6 Job ──── HTTP ────► demo-api pods ◄─── scale ───┐  Prometheus        │
 (trace replay) │   │  (open model,         (Go, 250m CPU,             │  Grafana           │
                │   │   new conn/request)    warm-up delay)            │  kube-state-metrics│
                │   │ remote write              │ /metrics            │  KEDA, metrics-srv │
                │   ▼                           ▼                     │                    │
                │  Prometheus ◄──────── scrape every 15 s ────────────┤                    │
                │   │  sum(rate(demo_requests_received_total[1m]))    │                    │
                │   ▼                                                 │                    │
                │  forecaster (Python, every 30 s) ── pas_predicted_rps ─► Prometheus      │
                │                                                     │                    │
                │  KEDA ScaledObject: triggers = CPU, max(pas_predicted_rps) ─► HPA ──┘    │
                └──────────────────────────────────────────────────────────────────────────┘

 workstation: pas CLI ── data, models, backtests, kind/docker/Helm, experiment runner, analysis
```

| Component | Code | Runs in |
|---|---|---|
| Workload | `app/` (Go) | app pool, one Deployment per lane |
| Load generator | `loadgen/replay.js`, `loadgen/capacity.js` (k6) | loadgen pool, one Job per run |
| Forecaster service | `src/pas/forecaster/` | system pool, predictive lanes only |
| Models | `src/pas/models/` | workstation (training), forecaster (inference) |
| Lane chart | `charts/workload/` | Helm release `workload` in each lane namespace |
| Monitoring | `k8s/monitoring/` | kube-prometheus-stack on the system pool |
| Local cluster | `infra/kind/cluster.yaml`, `src/pas/infra.py` | kind nodes in Docker (default) |
| Infrastructure (optional) | `infra/terraform/` | AKS, ACR, node pools, budget |
| Orchestration and analysis | `src/pas/cli.py`, `src/pas/experiments/`, `src/pas/analysis/` | workstation |

## Platforms

`cluster.provider` in `pas.yaml` selects where the cluster runs (ADR-013):

| | local (default) | aks |
|---|---|---|
| Cluster | kind in Docker, from `infra/kind/cluster.yaml` | AKS, from `infra/terraform/` |
| Node pools | kind workers labelled and tainted `pas.dev/pool=system/app/appb/loadgen` | separate VM pools with the same labels and taints |
| Images | `docker build`, then `kind load` into the nodes that run them | ACR Tasks (`az acr build`), pulled with AcrPull |
| KEDA, metrics-server | Helm charts (pinned versions in `src/pas/infra.py`) | AKS add-on, built in |
| Isolation of lanes | none: nodes share the host CPUs, so one lane at a time | per pool |
| Stop/start | `docker stop/start` of the node containers | `az aks stop/start` |

Commands that touch the cluster refuse to run unless the kubectl context is the study's
(`kind-pas`, or the AKS cluster name), so a work cluster in the same kubeconfig is never touched.

## Lanes and scaler modes

A **lane** is a namespace pinned to its own node pool (`pas.dev/pool` label and taint). The
`workload` chart deploys demo-api plus one scaler mode:

| Mode | Objects | Used by presets |
|---|---|---|
| `off` | none (replica count left as is) | experiment reset, lag measurement |
| `static` | HPA with min = max | `static` |
| `hpa` | autoscaling/v2 HPA on CPU | `hpa-default`, `hpa-tuned` |
| `predictive` | forecaster + KEDA ScaledObject (CPU + forecast triggers) | `naive`, `arima`, `lstm`, `oracle`, … |

The Deployment has no `replicas` field, so Helm upgrades never fight the autoscaler. Mode
changes always pass through `off`, because KEDA refuses a ScaledObject while another HPA owns the
Deployment.

## The control loop

1. demo-api counts each request on arrival (`demo_requests_received_total`).
2. Prometheus scrapes it every 15 s.
3. Every 30 s the forecaster reads the last minutes of `sum(rate(...[1m]))`, appends them to its
   history, and predicts the next `horizon_minutes`.
4. It publishes `pas_predicted_rps` = the highest forecast inside the lead window
   (`forecast.lead_minutes`), the demand expected while new pods are still starting.
5. KEDA reads `max(pas_predicted_rps{lane=...})` and the HPA asks for
   `ceil(predicted / threshold)` replicas, with threshold = `per_pod_rps × (1 − headroom)`, or
   for what CPU asks, whichever is larger.

**Lag budget.** Scrape (≤15 s) + rate window (~30 s) + forecaster cycle (≤30 s) + scrape (≤15 s)
+ KEDA poll (≤15 s) + pod start and warm-up. `pas lag` measures the pod part; the lead window
must cover the total.

## Forecaster clock

Models see one continuous per-minute series on the **trace clock**.

- **Experiment mode** (`PAS_WINDOW_FILE` set): history = the trace minutes before the window
  (warm start) followed by observed minutes since `POST /control/start`. k6 calls
  `/control/start` in `setup()` at the instant load begins, so observed minute *i* lands at
  `window_start + i`. Before the start, the forecast covers the window's first minutes, so the
  predictive scaler can pre-scale for the opening load.
- **Free-running mode** (no window): history = the last `history_minutes` on wall-clock time.

A minute is read only after a 20-s grace period, so Prometheus has scraped it. If forecasting
fails more than `PAS_MAX_STALE_CYCLES` times in a row, the service withdraws
`pas_predicted_rps`; the CPU trigger then drives scaling alone.

## Data flow of one experiment run

```
pas.yaml + window W*.json ──► helm values ──► lane (off → scaler) ──► k6 Job
                                                                       │
experiments/results/<run_id>/  ◄── Prometheus export (15 s) ◄──────────┘
  meta.json  timeseries.parquet  window.json  k6.log
          │
          ▼
pas analyze ──► reports/runs.csv, reports/summary.md, reports/figures/
```

## Measurement definitions

All per-run metrics use a 1-minute grid aligned to the k6 start time (`started_at` in the k6
summary). Implemented in `src/pas/analysis/metrics.py`.

| Metric | Definition |
|---|---|
| Required replicas | `max(min_replicas, ceil(offered_rps / per_pod_rps))`, offered from the window file |
| SLO-violation minutes | minutes with p95 latency > `slo.p95_latency_ms` **or** error rate > `slo.error_rate` (headline metric) |
| p95 latency | client side (k6, successful requests) when available, else server side; worst 15-s value in the minute |
| Error rate | non-2xx or failed k6 requests ÷ all k6 requests |
| Under-provisioned % | share of minutes with ready < required (ready = fewest ready replicas in the minute) |
| Deficit episodes | runs of consecutive under-provisioned minutes, and their mean length |
| Over-provisioning | Σ max(0, ready − required), in replica-minutes |
| Replica-hours | Σ ready ÷ 60 (multiply by the per-pod price for cost) |
| Scaling actions | changes of the Deployment's desired replica count |
| Valid | k6 dropped ≤ 0.1 % of iterations (otherwise the load generator, not the scaler, was the limit) |

Comparison (`pas analyze`): mean ± 95 % t-interval per (window, scaler) over repetitions, and a
two-sided Mann–Whitney U test of SLO-violation minutes against the baseline (default
`hpa-tuned`).

## Security and hygiene

- Containers run as non-root with a read-only root filesystem, no capabilities, and the
  RuntimeDefault seccomp profile; no service-account tokens are mounted.
- The demo-api image is `scratch` plus one static binary.
- No secrets in the repository. Locally, images never leave the machine (no registry). On AKS,
  Terraform state and `terraform.tfvars` are git-ignored, and ACR is reached through the
  cluster's managed identity (AcrPull) with the admin user disabled.
- Model files are Python pickles: only load artifacts this project produced.
