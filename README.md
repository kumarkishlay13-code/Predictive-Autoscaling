# Predictive autoscaling on AKS

Forecast-driven (proactive) pod autoscaling for Kubernetes, evaluated head-to-head against the
standard reactive Horizontal Pod Autoscaler on Azure Kubernetes Service.

A real workload trace (Azure Functions 2019) is replayed against a small service. A forecaster
predicts the next minutes of demand and KEDA scales the service *before* the load arrives, with
CPU-based scaling kept as a safety net. Everything needed for the study is here: data
preparation, forecasting models and backtests, the forecaster service, Helm chart, Terraform,
load generation, the experiment runner and the analysis. It is all driven by one command, `pas`.

> BITS Pilani SSWT ZG628T dissertation project, *Predictive Autoscaling for Kubernetes
> Workloads: A Machine Learning Approach to Proactive Pod Scaling on AKS*.
> Design rationale: [docs/decisions.md](docs/decisions.md).

## Status

| Part | State |
|---|---|
| Data pipeline, models, backtests | Working on the real trace; results in [docs/findings.md](docs/findings.md) |
| Forecaster service | Tested (unit tests and an HTTP run against a stub Prometheus) |
| demo-api, k6 scripts | Tested locally (Go tests; k6 replay, capacity and remote-write output verified) |
| Helm chart, Terraform, monitoring values | Lint/validate/render clean; **not yet applied to AKS** |
| Experiment runner, analysis | Unit-tested with synthetic runs; first real run happens on AKS |

---

## Contents

- [How it works](#how-it-works)
- [Quick start (laptop, no Azure)](#quick-start-laptop-no-azure)
- [Running on AKS](#running-on-aks)
- [Command reference](#command-reference)
- [Configuration: pas.yaml](#configuration-pasyaml)
- [Repository layout](#repository-layout)
- [How this maps to the dissertation](#how-this-maps-to-the-dissertation)
- [Development](#development)
- [Data licence and citation](#data-licence-and-citation)

## How it works

```
trace window ─► k6 (open-model load) ─► demo-api pods ─► Prometheus
                                              ▲               │ offered RPS
                                              │               ▼
                         HPA ◄── KEDA ◄── pas_predicted_rps ◄── forecaster (every 30 s)
                       (max of CPU trigger and forecast trigger)
```

- **Workload** ([app/](app/)): Go service with a fixed CPU cost per request and a configurable
  warm-up, so provisioning lag is realistic.
- **Forecaster** ([src/pas/forecaster/](src/pas/forecaster/)): reads offered load from
  Prometheus and publishes the peak demand expected while new pods would still be starting.
- **Scalers compared**: default HPA, tuned HPA, static over-provisioning, and predictive scalers
  driven by seasonal-naive, ARIMA, LSTM (mean and P90) and a perfect-foresight oracle.
- **Measurement**: client-side latency and errors from k6, replicas from kube-state-metrics,
  exported per run and reduced to SLO-violation minutes, under/over-provisioning and
  replica-hours, with confidence intervals and significance tests.

Details: [docs/architecture.md](docs/architecture.md).

## Quick start (laptop, no Azure)

Everything except the live experiments runs locally on Windows, macOS or Linux, and needs no
Docker.

**Prerequisites:** Python 3.11+, Git. Optional: Go 1.27 (to test the app) and k6 (to load-test
it locally).

```powershell
git clone <this repository> predictive-autoscaling
cd predictive-autoscaling
python -m pip install -e ".[dev,plots]"     # add ,lstm for PyTorch (LSTM models)
pas doctor                                  # what is installed, what is missing
```

> If `pas` is not recognised, pip put it in a Scripts folder that is not on PATH.
> `python -m pas.cli <command>` always works.

Then build the data and evaluate the forecasters offline:

```powershell
pas data download            # Azure Functions 2019 trace, 143 MB download, ~2 GB unpacked
pas data apps                # rank applications as candidate load signals
pas data build --plot        # scaled load series + replay windows + reports/figures/series.png
pas train naive arima        # fit on the training days -> artifacts/models/
pas backtest naive arima     # accuracy per horizon on held-out days -> reports/backtest/
```

Try the workload and the load generator locally (optional):

```powershell
cd app; go test ./...; $env:PORT=8080; go run .            # terminal 1
k6 run -e TARGET=http://localhost:8080 -e WINDOW_FILE=windows/WD.json `
       -e STEP_SECONDS=5 -e WORK_UNITS=1 loadgen/replay.js  # terminal 2, from the repo root
```

`WINDOW_FILE` is resolved relative to the script's folder (`loadgen/`). `STEP_SECONDS=5`
replays one trace minute every 5 seconds and `WORK_UNITS=1` keeps a laptop from saturating. Use
these only for smoke tests; real experiments replay in real time.

## Running on AKS

Follow [docs/runbook.md](docs/runbook.md). In short:

```powershell
az login
copy infra\terraform\terraform.tfvars.example infra\terraform\terraform.tfvars   # edit it
pas infra up                 # AKS (system, app, loadgen pools), ACR, KEDA add-on, budget alert
pas platform install         # Prometheus + Grafana + dashboard
pas build                    # images built in Azure, no local Docker needed
pas capacity                 # per-pod capacity -> set capacity.per_pod_rps in pas.yaml
pas lag                      # scaling lag -> set forecast.lead_minutes
pas experiment plan
pas experiment run           # resumable; run it again after any interruption
pas analyze --plots          # reports/summary.md, reports/runs.csv, reports/figures/
pas infra stop               # stop paying for nodes between sessions
```

Lean cluster: 5 × Standard_D2s_v5 (10 vCPU), about US$0.50 per hour while running. **Check your
vCPU quota first** (runbook §1).

## Command reference

| Command | What it does | Writes |
|---|---|---|
| `pas doctor` | Checks tools, config, data, models, cluster, KEDA, monitoring, images | – |
| `pas data download` | Downloads and unpacks the invocation files of the trace | `data/raw/` |
| `pas data apps` | Ranks the busiest apps by daily regularity and variation | – |
| `pas data build [--plot]` | Builds the scaled load series and all replay windows | `data/processed/`, `loadgen/windows/` |
| `pas data windows [--only W1]` | Rebuilds windows only | `loadgen/windows/` |
| `pas train MODEL…` | Fits `naive`, `last`, `arima`, `lstm`, `lstm-p90`, `prophet` | `artifacts/models/` |
| `pas backtest MODEL… [--quantile 0.9] [--split val\|test]` | Rolling-origin accuracy per horizon (MAE, RMSE, MASE, sMAPE, under-prediction, pinball) | `reports/backtest/` |
| `pas serve` | Runs the forecaster service (normally in the cluster) | – |
| `pas infra up\|stop\|start\|down\|outputs` | Terraform apply/destroy, AKS stop/start | `.pas/state.json` |
| `pas build [--only …] [--with-lstm]` | Builds images with ACR Tasks, tagged with the git commit | `.pas/state.json` |
| `pas platform install\|dashboards` | kube-prometheus-stack and the Grafana dashboard | – |
| `pas grafana`, `pas prometheus` | Port-forward and print the URL (and Grafana password) | – |
| `pas lane deploy LANE --scaler PRESET [--window W] [--startup-delay N]` | Deploys one scaler preset to a lane | – |
| `pas lane status LANE` | Deployments, HPAs, ScaledObjects, pods | – |
| `pas capacity [--steps …]` | Step-load test on one pod; capacity within the SLO | `reports/capacity.json` |
| `pas lag [--startup-delay N] [--samples 5]` | Time from scale-up to a ready pod, with breakdown | `reports/lag-d*.json` |
| `pas experiment plan` | Expands `experiments/matrix.yaml` into a shuffled plan | `experiments/plan.csv` |
| `pas experiment run [--lane] [--shard i/n] [--only …] [--dry-run]` | Runs pending runs: reset, scaler, k6, export | `experiments/results/<run>/` |
| `pas experiment status` | Which runs are done | – |
| `pas analyze [--baseline hpa-tuned] [--plots]` | Per-run metrics, comparison with CIs and p-values | `reports/` |
| `pas demo start\|stop` | Side-by-side reactive vs predictive replay for the viva | – |

`pas <command> --help` shows every option.

## Configuration: pas.yaml

One file holds every study parameter ([pas.yaml](pas.yaml), ADR-009). Each run stores a copy of
the relevant parts in its `meta.json`.

| Section | Holds | When to change it |
|---|---|---|
| `trace` | Dataset URL, load signal (`total` or an app hash), train/val/test days | after `pas data apps` |
| `capacity` | `per_pod_rps`, min/max replicas, peak utilisation of the scaled trace | after `pas capacity` |
| `slo` | p95 latency and error-rate targets | after the capacity test |
| `app` | Start-up delay, CPU work per request | sensitivity studies |
| `forecast` | Horizon, lead window, headroom, history length, cycle time | after `pas lag` |
| `windows` | Replay windows (fixed time or `auto:ramp` / `auto:peak`), spikes | rarely |
| `scalers` | Named presets: mode, model, quantile, HPA behaviour | to add a variant |
| `lanes` | Namespace → node pool | when adding lanes |

After changing `trace` or `capacity`, rerun `pas data build`, `pas train …` and
`pas build --only forecaster`, because the load series is scaled to capacity and the models are
baked into the image.

## Repository layout

```
app/                    Go workload (demo-api) + Dockerfile
charts/workload/        Helm chart for one lane (app + scaler + forecaster), with values schema
docker/                 forecaster image
docs/                   architecture, decision log (ADRs), runbook
experiments/            matrix.yaml; plan.csv and results/ are generated (git-ignored)
infra/terraform/        AKS, node pools, ACR, budget alert
k8s/monitoring/         kube-prometheus-stack values, Grafana dashboard
loadgen/                k6 scripts; windows/ generated by `pas data build`
src/pas/                Python package and CLI
  data/                 trace download, load series, windows
  models/               naive, ARIMA, LSTM, Prophet, oracle
  forecaster/           live service (engine + HTTP)
  experiments/          lanes, k6 jobs, runner, capacity and lag, export
  analysis/             metrics, comparison, figures
tests/                  pytest suite (synthetic data, no cluster needed)
pas.yaml                study configuration
```

## How this maps to the dissertation

| Plan item | Commands | Evidence produced |
|---|---|---|
| Wk 3: environment, capacity, lag | `pas infra up`, `pas platform install`, `pas capacity`, `pas lag` | `reports/capacity.json`, `reports/lag-d*.json` |
| Wk 4: dataset and replay | `pas data download/apps/build --plot` | `data/processed/series.json`, `reports/figures/series.png`, `loadgen/windows/` |
| Wk 5–6: reactive baseline, end-to-end loop | `pas experiment run --only …` with `hpa-default`, `hpa-tuned`, `naive`, `oracle` | `experiments/results/`, `pas analyze` |
| Wk 7–8: statistical models (RQ1) | `pas train arima`, `pas backtest naive last arima` | `reports/backtest/summary-test.md` |
| Wk 9–11: LSTM and quantile scaling | `pas train lstm lstm-p90`, `pas build --with-lstm` | backtests, live runs |
| Wk 12–13: comparison (RQ2–RQ4) | `pas experiment plan/run`, `pas analyze --plots` | `reports/summary.md`, `reports/runs.csv`, figures |
| Wk 14: demo | `pas demo start`, `pas grafana` | live dashboard |

## Development

```powershell
python -m pip install -e ".[dev,lstm,plots]"
pytest                       # unit and end-to-end tests on synthetic data, no cluster
ruff check src tests         # lint
cd app; go vet ./...; go test ./...
helm lint charts/workload --set app.image.repository=x --set app.image.tag=y
terraform -chdir=infra/terraform validate
```

CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs all of the above on every push.

Conventions: one configuration file; every cluster action is a printed, reproducible command;
every design choice that needs a reason goes into [docs/decisions.md](docs/decisions.md).

## Data licence and citation

The Azure Functions Trace 2019 is published by Microsoft under the
[CC-BY 4.0 licence](https://github.com/Azure/AzurePublicDataset/blob/master/LICENSE). If you
use it, cite:

> M. Shahrad, R. Fonseca, I. Goiri, G. Chaudhry, P. Batum, J. Cooke, E. Laureano, C. Tresness,
> M. Russinovich, R. Bianchini. "Serverless in the Wild: Characterizing and Optimizing the
> Serverless Workload at a Large Cloud Provider." USENIX ATC 2020.

The trace is downloaded on demand and is not stored in this repository.
