"""Shared fixtures: a project config in a temp dir, a synthetic trace and a synthetic series."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from pas.config import load_config
from pas.data.trace import MINUTE_COLS

REPO = Path(__file__).resolve().parents[1]


def diurnal(days: int = 14, seed: int = 0, noise: float = 0.05,
            start: str = "2019-07-15") -> pd.Series:
    """Daily-periodic load (low at night, peak in the afternoon) with multiplicative noise."""
    idx = pd.date_range(start, periods=days * 1440, freq="min")
    t = (idx.hour * 60 + idx.minute).to_numpy() / 1440
    base = 100 + 80 * np.sin(2 * np.pi * (t - 0.3)) + 20 * np.sin(4 * np.pi * t)
    rng = np.random.default_rng(seed)
    return pd.Series(base * (1 + noise * rng.standard_normal(len(idx))), index=idx).clip(lower=1)


@pytest.fixture
def series() -> pd.Series:
    return diurnal()


@pytest.fixture
def project(tmp_path: Path):
    """A pas.yaml in tmp_path that points data/windows/models into tmp_path."""
    raw = yaml.safe_load((REPO / "pas.yaml").read_text(encoding="utf-8"))
    raw["trace"]["raw_dir"] = "data/raw"
    raw["trace"]["processed_dir"] = "data/processed"
    raw["trace"]["signal"] = "total"
    path = tmp_path / "pas.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    for sub in ("charts", "loadgen"):
        (tmp_path / sub).mkdir()
    return load_config(path)


@pytest.fixture
def fake_trace(project):
    """14 daily CSVs in the real schema: two apps (http, timer) plus a tiny noisy one."""
    raw_dir = project.trace.raw_dir
    raw_dir.mkdir(parents=True)
    s = diurnal(seed=1).to_numpy()
    for day in range(1, 15):
        day_values = s[(day - 1) * 1440: day * 1440]
        rows = [
            {"HashOwner": "o1", "HashApp": "appA", "HashFunction": "f1", "Trigger": "http",
             **dict(zip(MINUTE_COLS, np.round(day_values * 30).astype(int), strict=True))},
            {"HashOwner": "o1", "HashApp": "appA", "HashFunction": "f2", "Trigger": "timer",
             **dict(zip(MINUTE_COLS, np.full(1440, 60), strict=True))},
            {"HashOwner": "o2", "HashApp": "appB", "HashFunction": "f3", "Trigger": "http",
             **dict(zip(MINUTE_COLS, (np.arange(1440) % 7 == 0).astype(int), strict=True))},
        ]
        pd.DataFrame(rows).to_csv(raw_dir / f"invocations_per_function_md.anon.d{day:02d}.csv",
                                  index=False)
    return raw_dir
