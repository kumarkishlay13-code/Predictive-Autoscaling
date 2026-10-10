# demo-api

The workload under test: a small Go HTTP service with a **fixed CPU cost per request**, a
**configurable warm-up**, and Prometheus metrics.

| Endpoint | Purpose |
|---|---|
| `GET /work?units=N` | Burn N units of CPU work (default `DEFAULT_WORK_UNITS`) and return a digest |
| `GET /healthz` | Liveness: 200 while the process runs |
| `GET /readyz` | Readiness: 503 while warming up or draining, 200 otherwise |
| `GET /metrics` | Prometheus metrics |

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `8080` | Listen port |
| `STARTUP_DELAY_SECONDS` | `0` | Simulated warm-up before the pod reports ready (ADR-004) |
| `DEFAULT_WORK_UNITS` | `20` | Work per request when `?units=` is absent |
| `MAX_WORK_UNITS` | `1000` | Upper bound accepted from `?units=` |
| `DRAIN_DELAY_SECONDS` | `5` | Time between SIGTERM and closing the listener |

## Metrics

| Metric | Type | Notes |
|---|---|---|
| `demo_requests_received_total{handler}` | counter | Counted **on arrival**: offered load, used by the forecaster |
| `demo_http_requests_total{handler,code}` | counter | Completed requests |
| `demo_http_request_duration_seconds{handler}` | histogram | Server-side latency |
| `demo_http_in_flight_requests` | gauge | Concurrency |
| `demo_ready` | gauge | 1 when ready |

## Work units

One unit is a fixed amount of hashing (2000 SHA-256 rounds), not a fixed duration. A request
therefore costs the same CPU whether or not the pod is throttled; under overload, latency grows
instead, as it would in a real service. Measure the cost on your machine:

```bash
go test -run xxx -bench BurnUnit .
```

On the development laptop one unit took about 0.77 ms, so 20 units on a 250m pod give roughly
20 RPS per pod. The real figure on AKS comes from `pas capacity`.

## Develop

```bash
go test ./...                      # unit tests
go run .                           # http://localhost:8080/work
STARTUP_DELAY_SECONDS=5 go run .   # watch /readyz flip after 5 s
```

The image is built in Azure by `pas build` (multi-stage, `scratch` final image, non-root).
