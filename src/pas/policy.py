"""Turn a forecast into the number the autoscaler acts on (plan §9).

The forecaster publishes `predicted_rps` = the highest forecast inside the lead window
[t+lead_lo, t+lead_hi]: the demand expected while pods requested now are still starting.
KEDA then computes replicas = ceil(predicted_rps / threshold) with
threshold = per_pod_rps × (1 − headroom), and the HPA takes the max with the CPU trigger.
"""

from __future__ import annotations

import math
from collections.abc import Sequence


def lead_target(forecast: Sequence[float], lead: tuple[int, int]) -> float:
    """Max forecast over minutes lead[0]..lead[1] ahead (1-based, inclusive)."""
    lo, hi = lead
    if not 1 <= lo <= hi <= len(forecast):
        raise ValueError(f"lead window {lead} does not fit a forecast of length {len(forecast)}")
    return float(max(forecast[lo - 1:hi]))


def keda_threshold(per_pod_rps: float, headroom: float) -> float:
    return per_pod_rps * (1.0 - headroom)


def desired_replicas(rps: float, per_pod_rps: float, headroom: float,
                     min_replicas: int, max_replicas: int) -> int:
    """What the HPA will ask for from the predictive trigger alone."""
    raw = math.ceil(max(rps, 0.0) / keda_threshold(per_pod_rps, headroom)) if rps > 0 else 0
    return min(max(raw, min_replicas), max_replicas)
