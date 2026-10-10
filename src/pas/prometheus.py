"""Minimal Prometheus HTTP API client (query and query_range)."""

from __future__ import annotations

import math

import pandas as pd
import requests


class PrometheusError(RuntimeError):
    pass


class Prometheus:
    def __init__(self, url: str, timeout: float = 10.0) -> None:
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def _get(self, path: str, params: dict) -> dict:
        try:
            r = self.session.get(f"{self.url}{path}", params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise PrometheusError(f"cannot reach Prometheus at {self.url}: {exc}") from exc
        if r.status_code != 200:
            raise PrometheusError(f"Prometheus returned {r.status_code}: {r.text[:300]}")
        body = r.json()
        if body.get("status") != "success":
            raise PrometheusError(f"Prometheus error: {body.get('error', body)}")
        return body["data"]

    def query_range(self, query: str, start: float, end: float, step: float) -> pd.DataFrame:
        """Return a frame indexed by UTC timestamp, one column per returned series."""
        data = self._get("/api/v1/query_range",
                         {"query": query, "start": start, "end": end, "step": step})
        columns = {}
        for i, series in enumerate(data.get("result", [])):
            label = ",".join(f"{k}={v}" for k, v in sorted(series["metric"].items())) or str(i)
            values = {pd.Timestamp(float(ts), unit="s"): _to_float(v)
                      for ts, v in series["values"]}
            columns[label] = pd.Series(values, dtype=float)
        return pd.DataFrame(columns)

    def query_range_single(self, query: str, start: float, end: float,
                           step: float) -> pd.Series:
        """Like query_range for queries that return at most one series (e.g. sum(...))."""
        frame = self.query_range(query, start, end, step)
        if frame.shape[1] > 1:
            raise PrometheusError(f"query returned {frame.shape[1]} series, expected one: "
                                  f"{query}")
        if frame.empty:
            return pd.Series(dtype=float)
        return frame.iloc[:, 0]

    def query(self, query: str) -> list[dict]:
        return self._get("/api/v1/query", {"query": query}).get("result", [])


def _to_float(value: str) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return math.nan
    return f if math.isfinite(f) else math.nan
