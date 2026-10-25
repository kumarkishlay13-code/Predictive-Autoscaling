# Runbook

Step-by-step procedures for the parts that touch the cluster. First-time installation and the
smoke test are in [setup-and-testing.md](setup-and-testing.md). Every `pas` command prints the
underlying `kind`/`docker`/`kubectl`/`helm` command before running it. Commands that act on the
cluster first check that the kubectl context is the study's (`kind-pas`).

The default cluster is local (`cluster.provider: local`, ADR-013). Appendix A covers AKS.

## 1. Start of every session

```bash
pas infra start              # only if the cluster was stopped
pas doctor                   # everything [ok]
pas smoke                    # optional: 5-minute check of the control loop
```

## 2. Calibrate (once per machine, before measured runs)

```bash
pas lane deploy lane-a --scaler static --startup-delay 0
pas capacity                 # step load on one pod -> reports/capacity.json
```

Put the reported value in `pas.yaml` (`capacity.per_pod_rps`), then rebuild data and models,
because the trace is scaled to capacity:

```bash
pas data build
pas train naive last arima lstm lstm-p90
pas build --only forecaster  # bake the new models into the image
pas lag --startup-delay 60   # reports/lag-d60.json; set forecast.lead_minutes to cover it
```

Lead window rule of thumb: pod lag (from `pas lag`) plus about 90 s of control loop, rounded up
to whole minutes, is the start of the lead window. The 90 s is scrape 15 s, rate window ~30 s,
forecaster 30 s and KEDA 15 s. On kind, images are already on the nodes and no node is ever
added, so the pod lag is mostly the configured start-up delay.

Capacity and lag belong to the machine. Re-measure them if the experiments move to another one.

## 3. Experiment day

```bash
pas experiment plan          # experiments/matrix.yaml -> experiments/plan.csv
pas experiment run --dry-run # see what will run
pas experiment run           # resumable: re-run the same command after any interruption
pas experiment status
```

Each run takes the window length plus about 10 minutes (reset, quiet period, cool-down, export).
While it runs, watch `pas grafana` → dashboard *Predictive autoscaling*.

**One lane at a time on a local cluster.** All kind nodes share the host CPUs, so a second lane
would compete with the first and distort its latency. Parallel lanes with `--shard` are for AKS,
where each pool is a separate VM:

```bash
pas experiment run --lane lane-a --shard 0/2   # AKS only, terminal 1
pas experiment run --lane lane-b --shard 1/2   # AKS only, terminal 2
```

**Validity checks after each session**

- `pas analyze` warns about runs where k6 dropped iterations, meaning the load generator could
  not keep up. Re-run those: delete `experiments/results/<run_id>/` and run again.
- On the very first run, confirm in Prometheus (`pas prometheus`) that
  `k6_http_req_duration_p95` values are in seconds (they are for k6 2.2) and that
  `demo_requests_received_total` matches the window file.
- Check that the host was not busy with other work during the session (CPU contention shows up
  as latency that is not the scaler's).

**Pause between sessions**

```bash
pas infra stop               # stops the node containers; images and Prometheus data are kept
```

## 4. Analyse

```bash
pas analyze --plots          # reports/runs.csv, reports/summary.md, reports/figures/*.png
pas backtest naive last arima lstm lstm-p90 --split test
```

## 5. Viva demo

Day before:

```bash
pas infra start
pas doctor
pas demo start               # lane-a: hpa-tuned, lane-b: arima, window WD (30 min)
pas grafana                  # check that both lanes appear; then
pas demo stop
```

Record the screen during this rehearsal. The recording is the fallback if the machine
misbehaves on the day. Two lanes share the host here, which is fine for a demo but not for
measurements.

On the day (start at least 30 minutes before, so Prometheus has fresh data):

```bash
pas infra start
pas demo start
pas grafana                  # dashboard: Predictive autoscaling, lane: All
```

Talking points: the forecast line rises before offered load; lane-b adds pods first; lane-a's
p95 crosses the SLO line at the ramp, lane-b's does not. To show the safety net, delete the
forecaster pod (`kubectl -n lane-b delete pod -l app.kubernetes.io/name=demo-api-forecaster`):
the forecast panel goes stale and lane-b keeps scaling on CPU.

## 6. Tear down

```bash
pas infra down               # kind delete cluster: removes all nodes, images and Prometheus data
```

Results in `experiments/results/` are local and unaffected. Back them up before deleting
anything.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `kubectl context is '…', expected 'kind-pas'` | another cluster is selected | `kubectl config use-context kind-pas` |
| `'kind' was not found` | tool missing or terminal opened before install | install it, open a new terminal |
| pods `Pending` on the app node | Docker has too few CPUs or too little memory for `maxReplicas` | raise Docker's resources or lower `capacity.max_replicas` |
| HPA targets `<unknown>` | metrics-server not ready | `kubectl -n kube-system logs deploy/metrics-server` |
| ScaledObject `Ready=False` | KEDA cannot reach Prometheus | `kubectl -n <lane> describe scaledobject demo-api`; check `forecaster.prometheusUrl` |
| forecaster `/readyz` 503 | no successful forecast yet | `kubectl -n <lane> logs deploy/demo-api-forecaster` |
| `predicted_rps` flat at warm-start values | k6 could not call `/control/start` | check the k6 log for `forecaster /control/start returned` |
| `ErrImagePull` for `pas/...` | image not loaded into that node (e.g. after `infra down`) | `pas build` |
| `helm --set-file` cannot find a file | backslashes in a Windows path | `pas` passes forward slashes; do the same by hand |

---

## Appendix A: running on AKS instead

Set `cluster.provider: aks` in `pas.yaml`. Then:

1. Install `az` and `terraform` and run `az login`.
2. Check vCPU quota: `az vm list-usage -l centralindia -o table | findstr /i "DSv5 Total"`. The
   lean cluster needs 10 vCPU of DSv5; parallel lanes need 16.
3. Copy `infra/terraform/terraform.tfvars.example` to `terraform.tfvars` and set
   `subscription_id`, `location` and the budget. For parallel lanes, also set
   `second_lane_node_count = 3`.
4. `pas infra up` (terraform apply plus kubeconfig), `pas platform install` (KEDA and
   metrics-server come with AKS), `pas build` (ACR Tasks, no local Docker needed).
5. Everything from §2 onward is the same. `pas infra stop`/`start` deallocates the nodes;
   `pas infra down` runs `terraform destroy`.

The lean cluster is 5 × Standard_D2s_v5, about US$0.50 per hour while running.
