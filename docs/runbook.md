# Runbook

Step-by-step procedures for the parts that touch Azure. Every `pas` command prints the
underlying `kubectl`/`helm`/`az`/`terraform` command before running it.

## 0. Once per machine

```powershell
winget install GoLang.Go Kubernetes.kubectl Helm.Helm Hashicorp.Terraform GrafanaLabs.k6 Microsoft.AzureCLI
# open a new terminal so PATH is refreshed, then from the repository root:
python -m pip install -e ".[dev,plots]"          # add ,lstm for the LSTM models
az login
pas doctor
```

If `pas` is "not recognized", pip installed it in a user Scripts folder that is not on PATH. Use
`python -m pas.cli ...` instead, or add `%APPDATA%\Python\Python312\Scripts` to PATH.

## 1. Check quota before creating anything

```powershell
az vm list-usage -l centralindia -o table | findstr /i "DSv5 Total"
```

The lean cluster needs 10 vCPU of DSv5 (5 × D2s_v5); parallel lanes need 16. Student
subscriptions are often below that. Request an increase (Portal → Quotas), pick another region
or VM family in `terraform.tfvars`, or shrink `app_node_count`.

## 2. Create the environment

```powershell
copy infra\terraform\terraform.tfvars.example infra\terraform\terraform.tfvars
# edit: subscription_id, location, budget_amount, budget_emails
pas infra up                 # terraform apply (asks for confirmation) + kubeconfig
pas platform install         # Prometheus, Grafana, dashboard; checks KEDA
pas build                    # demo-api + forecaster images in ACR
pas doctor                   # everything should be [ok]
```

## 3. Calibrate (week 3)

```powershell
pas lane deploy lane-a --scaler static --startup-delay 0
pas capacity                 # step load on one pod -> reports/capacity.json
```

Put the reported value in `pas.yaml` (`capacity.per_pod_rps`), then rebuild data and models,
because the trace is scaled to capacity:

```powershell
pas data build
pas train naive arima
pas build --only forecaster  # bake the new models into the image
pas lag --startup-delay 60   # reports/lag-d60.json; set forecast.lead_minutes to cover it
```

Lead window rule of thumb: pod lag (from `pas lag`) + about 90 s of control loop (scrape 15 s,
rate window ~30 s, forecaster 30 s, KEDA 15 s), rounded up to whole minutes, is the start of the
lead window.

## 4. Experiment day

```powershell
pas infra start              # if the cluster was stopped
pas doctor
pas experiment plan          # experiments/matrix.yaml -> experiments/plan.csv
pas experiment run --dry-run # see what will run
pas experiment run           # resumable: re-run the same command after any interruption
pas experiment status
```

Parallel lanes (needs `second_lane_node_count = 3` in Terraform): run two terminals,

```powershell
pas experiment run --lane lane-a --shard 0/2
pas experiment run --lane lane-b --shard 1/2
```

Each run takes the window length + about 10 minutes (reset, quiet period, cool-down, export).
While it runs, watch `pas grafana` → dashboard *Predictive autoscaling*.

**Validity checks after each session**

- `pas analyze` warns about runs where k6 dropped iterations (the load generator could not
  keep up). Re-run those: delete `experiments/results/<run_id>/` and run again.
- On the very first run, confirm in Prometheus that `k6_http_req_duration_p95` values are in
  seconds (they are for k6 2.2) and that `demo_requests_received_total` matches the window file.

**Stop paying when you are done for the day**

```powershell
pas infra stop               # deallocates nodes; disks and IPs remain (small cost)
```

## 5. Analyse

```powershell
pas analyze --plots          # reports/runs.csv, reports/summary.md, reports/figures/*.png
pas backtest naive arima lstm lstm-p90 --split test
```

## 6. Viva demo

Day before:

```powershell
pas infra start
pas doctor
pas demo start               # lane-a: hpa-tuned, lane-b: arima, window WD (30 min)
pas grafana                  # check both lanes appear; then
pas demo stop
```

Record the screen during this rehearsal; the recording is the fallback if Azure misbehaves on
the day.

On the day (start at least one hour before, so the cluster and Prometheus are warm):

```powershell
pas infra start
pas demo start
pas grafana                  # dashboard: Predictive autoscaling, lane: All
```

Talking points: the forecast line rises before offered load; lane-b adds pods first; lane-a's
p95 crosses the SLO line at the ramp, lane-b's does not. To show the safety net, delete the
forecaster pod (`kubectl -n lane-b delete pod -l app.kubernetes.io/name=demo-api-forecaster`):
the forecast panel goes stale and lane-b keeps scaling on CPU.

## 7. Tear down

```powershell
pas infra down               # terraform destroy: deletes everything, including ACR images
```

Results in `experiments/results/` are local and unaffected; back them up (OneDrive, Blob) before
deleting anything.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `'kubectl' was not found` | terminal opened before winget install | open a new terminal |
| `no Terraform outputs found` | cluster not created, or created elsewhere | `pas infra up`, or set `cluster.acr_login_server` in `pas.yaml` |
| pods `Pending` on the app pool | quota or pool size too small for `maxReplicas` | lower `capacity.max_replicas` or add nodes |
| ScaledObject `Ready=False` | KEDA cannot reach Prometheus | `kubectl -n <lane> describe scaledobject demo-api`; check `forecaster.prometheusUrl` |
| forecaster `/readyz` 503 | no successful forecast yet | `kubectl -n <lane> logs deploy/demo-api-forecaster` |
| `predicted_rps` flat at warm-start values | k6 could not call `/control/start` | check the k6 log for `forecaster /control/start returned` |
| `helm --set-file` cannot find a file | backslashes in a Windows path | `pas` passes forward slashes; do the same by hand |
