"""Export one run's time series from Prometheus to parquet (plan §10.4 step 5)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from pas.prometheus import Prometheus

STEP_SECONDS = 15

#: column name -> PromQL; {ns} is the lane namespace and {run} the run id
QUERIES = {
    "offered_rps": 'sum(rate(demo_requests_received_total{{namespace="{ns}"}}[1m]))',
    "served_rps": 'sum(rate(demo_http_requests_total{{namespace="{ns}",handler="work"}}[1m]))',
    "server_p95_s": 'histogram_quantile(0.95, sum by (le) (rate('
                    'demo_http_request_duration_seconds_bucket{{namespace="{ns}"}}[1m])))',
    "server_p99_s": 'histogram_quantile(0.99, sum by (le) (rate('
                    'demo_http_request_duration_seconds_bucket{{namespace="{ns}"}}[1m])))',
    "ready_replicas": 'max(kube_deployment_status_replicas_ready{{namespace="{ns}",'
                      'deployment="demo-api"}})',
    "desired_replicas": 'max(kube_deployment_spec_replicas{{namespace="{ns}",'
                        'deployment="demo-api"}})',
    "cpu_utilisation": 'sum(rate(container_cpu_usage_seconds_total{{namespace="{ns}",'
                       'container="demo-api"}}[1m])) / sum(kube_pod_container_resource_requests'
                       '{{namespace="{ns}",container="demo-api",resource="cpu"}})',
    "predicted_rps": 'max(pas_predicted_rps{{lane="{ns}"}})',
    # k6 remote-write series are split by status and report durations in seconds (verified
    # against k6 2.2). Latency uses successful requests; failures count against the error rate.
    "client_p95_s": 'max(k6_http_req_duration_p95{{lane="{ns}",run="{run}",status="200"}})',
    "client_p99_s": 'max(k6_http_req_duration_p99{{lane="{ns}",run="{run}",status="200"}})',
    "client_rps": 'sum(rate(k6_http_reqs_total{{lane="{ns}",run="{run}"}}[1m]))',
    "client_error_rate": '(sum(rate(k6_http_reqs_total{{lane="{ns}",run="{run}",'
                         'status!~"2.."}}[1m])) or vector(0))'
                         ' / sum(rate(k6_http_reqs_total{{lane="{ns}",run="{run}"}}[1m]))',
    "dropped_iterations": 'sum(increase(k6_dropped_iterations_total{{lane="{ns}",'
                          'run="{run}"}}[1m]))',
}


def export_run(prom: Prometheus, namespace: str, run_id: str, start: float, end: float,
               path: Path) -> pd.DataFrame:
    """Query every series over [start, end] at 15 s resolution and write a wide parquet."""
    columns = {}
    for name, template in QUERIES.items():
        query = template.format(ns=namespace, run=run_id)
        series = prom.query_range_single(query, start, end, STEP_SECONDS)
        columns[name] = series
    frame = pd.DataFrame(columns)
    frame.index.name = "time"
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path)
    return frame
