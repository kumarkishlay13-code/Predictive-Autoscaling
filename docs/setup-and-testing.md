# Setup and testing on a new machine

This guide sets up and runs the whole study on one machine. It uses a local Kubernetes cluster
([kind](https://kind.sigs.k8s.io)) in Docker, with Prometheus, Grafana, KEDA and metrics-server.
No cloud account is needed. Getting to a passing smoke test takes about an hour.

## 1. Machine

| | Minimum | Recommended |
|---|---|---|
| CPU | 8 cores | 12 or more |
| RAM | 16 GB | 32 GB |
| Free disk | 40 GB | 60 GB |
| OS | Ubuntu 22.04+ (or another Linux), macOS, Windows 10/11 with WSL 2 | Linux |

At the peak of a window, 20 demo-api pods × 250m CPU need 5 cores. k6, Prometheus and the
forecaster need about 2 more.

- **Docker Desktop (Windows, macOS).** Under Settings → Resources, give Docker at least 8 CPUs
  and 12 GB of memory. On Windows with the WSL 2 backend these limits are set in
  `%UserProfile%\.wslconfig` instead:
  ```ini
  [wsl2]
  processors=8
  memory=12GB
  ```
- **Linux.** Five kind nodes need higher inotify limits:
  ```bash
  echo -e "fs.inotify.max_user_watches=524288\nfs.inotify.max_user_instances=512" \
    | sudo tee /etc/sysctl.d/99-kind.conf && sudo sysctl --system
  ```
- Turn off sleep and hibernation while experiments run.

## 2. Install the tools

| Tool | Version | Windows (`winget install …`) | macOS (`brew install …`) | Linux |
|---|---|---|---|---|
| Docker | Desktop 4.x or Engine 27+ | `Docker.DockerDesktop` | `--cask docker` | [Docker Engine](https://docs.docker.com/engine/install/), then add yourself to the `docker` group |
| kind | **0.33.0 or newer** | `Kubernetes.kind` | `kind` | `curl -Lo kind https://kind.sigs.k8s.io/dl/v0.33.0/kind-linux-amd64 && sudo install kind /usr/local/bin/` |
| kubectl | 1.35–1.37 | `Kubernetes.kubectl` | `kubectl` | [install guide](https://kubernetes.io/docs/tasks/tools/install-kubectl-linux/) |
| Helm | 3.14+ or 4.x | `Helm.Helm` | `helm` | [install script](https://helm.sh/docs/intro/install/) |
| Python | 3.11+ | `Python.Python.3.12` | `python@3.12` | `sudo apt install python3 python3-venv` |
| Git | any | `Git.Git` | `git` | `sudo apt install git` |
| *optional:* k6, Go 1.27 | | `GrafanaLabs.k6`, `GoLang.Go` | `k6`, `go` | for tests outside the cluster |

Open a new terminal afterwards so PATH is up to date. Start Docker before continuing.

## 3. Get the code

```bash
git clone https://github.com/kumarkishlay13-code/Predictive-Autoscaling.git predictive-autoscaling
cd predictive-autoscaling
python -m venv .venv
source .venv/bin/activate          # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,plots,lstm]"
```

If `pas` is not recognised, use `python -m pas.cli <command>` instead.

## 4. Test without a cluster (about 5 minutes)

```bash
pytest          # every test must pass (about 2 minutes)
pas doctor      # tools and project checks
```

At this stage, `doctor` shows `!!` for the trace, the load series and every cluster item. That
is expected. The replay windows and trained models are in the repository; the trace is only
needed for step 7.

## 5. Start the local platform (15–25 minutes the first time)

```bash
pas infra up            # kind cluster "pas": control plane + system, app, appb, loadgen nodes
pas platform install    # metrics-server, KEDA, Prometheus + Grafana, dashboard
pas build               # builds demo-api and forecaster images, loads them into the nodes
pas doctor              # tools and cluster checks: all [ok]
```

`pas build` includes PyTorch in the forecaster image when LSTM models are present (about 1.5 GB).
If you won't run the `lstm*` presets, `pas build --no-with-lstm` is much faster.

## 6. Smoke test (5–10 minutes)

```bash
pas smoke                       # predictive scaler (seasonal naive) on lane-a
pas smoke --scaler hpa-tuned    # reactive HPA
```

The smoke test replays 10 trace minutes in 100 seconds with light requests, then checks every
link of the control loop. A passing run ends like this:

```
  [ok] k6 replayed the window and printed its summary
  [ok] metrics-server serves pod CPU
  [ok] HPA keda-hpa-demo-api computes replica counts
  [ok] KEDA ScaledObject is ready
  [ok] demo-api counted the offered load
  [ok] k6 remote-wrote client-side metrics
  [ok] kube-state-metrics reports replicas
  [ok] cAdvisor reports pod CPU
  [ok] forecaster published pas_predicted_rps
  [ok] scaler raised replicas above the minimum (2)
all checks passed: the control loop is wired up end to end
```

To watch it, run `pas grafana`, open <http://localhost:3000> (user `admin`, password printed)
and pick the dashboard **Predictive autoscaling**.

## 7. Calibrate on this machine (before any measured run)

Per-pod capacity and scaling lag depend on the CPU, so measure them on the machine that runs the
experiments:

```bash
pas data download && pas data build   # trace: 143 MB download, about 2 GB unpacked
pas capacity                          # step-load test -> reports/capacity.json
```

Set `capacity.per_pod_rps` in `pas.yaml` to the printed value, then rebuild what depends on it:

```bash
pas data build                        # load series re-scaled to the new capacity
pas train naive last arima lstm lstm-p90
pas build --only forecaster           # bake the new models into the image
pas lag                               # -> reports/lag-d60.json
```

Set `forecast.lead_minutes` from the lag result ([runbook](runbook.md) §2), and record both
values in [findings.md](findings.md).

## 8. Run the experiments

```bash
pas experiment plan
pas experiment run --dry-run
pas experiment run          # resumable: run the same command again after any interruption
pas experiment status
pas analyze --plots         # reports/summary.md, reports/runs.csv, reports/figures/
```

The full matrix is 76 runs, about 136 hours (5–6 days) of continuous running. Run one lane at a
time: kind nodes share the host CPUs, so parallel lanes would disturb each other (ADR-013). Keep
other heavy work off the machine while runs are measured.

## 9. Demo, stop, clean up

```bash
pas demo start / pas demo stop  # lane-a reactive vs lane-b predictive, side by side
pas infra stop                  # stop the node containers; everything is kept
pas infra start                 # resume; wait about a minute, then `pas doctor`
pas infra down                  # delete the cluster (experiments/results/ is kept)
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Docker engine running [!!]`, "cannot connect to the Docker daemon" | Start Docker Desktop, or `sudo systemctl start docker` |
| `kind create` fails with "too many open files", or nodes stay NotReady (Linux) | inotify limits, §1 |
| Pods `Pending` with `Insufficient cpu` or `memory` | Give Docker more resources, or lower `capacity.max_replicas` |
| HPA shows `<unknown>` targets | metrics-server is not ready: `kubectl -n kube-system logs deploy/metrics-server` |
| ScaledObject `Ready=False` | `kubectl -n lane-a describe scaledobject demo-api`; check that Prometheus pods are running |
| `kubectl context is '…', expected 'kind-pas'` | `kubectl config use-context kind-pas` (the guard stops pas from touching other clusters) |
| `ErrImagePull` or `ImagePullBackOff` for `pas/...` | Images are loaded into the nodes, not pulled: run `pas build` again |
| Cluster unreachable after a reboot | `pas infra start`; if that fails: `pas infra down`, `pas infra up`, `pas platform install`, `pas build` |
| A smoke check fails | The `->` hint names the command to inspect; the [runbook](runbook.md) troubleshooting table has more |
