from pathlib import Path

import pytest
import yaml

from pas.config import ConfigError, load_config

REPO = Path(__file__).resolve().parents[1]


def test_repository_config_loads():
    cfg = load_config(REPO / "pas.yaml")
    assert cfg.capacity.target_peak_rps == pytest.approx(
        cfg.capacity.peak_utilisation * cfg.capacity.max_replicas * cfg.capacity.per_pod_rps)
    assert {"hpa-default", "hpa-tuned", "naive", "arima", "oracle"} <= set(cfg.scalers)
    # Predictive presets share the tuned HPA's behaviour (YAML anchors) for a fair ablation.
    assert cfg.scaler("arima")["behavior"] == cfg.scaler("hpa-tuned")["behavior"]
    # The study runs locally unless pas.yaml asks for AKS (ADR-013).
    assert cfg.cluster.is_local and cfg.cluster.kind_name == "pas"


def test_required_replicas_respects_minimum():
    cfg = load_config(REPO / "pas.yaml")
    assert cfg.required_replicas(0) == cfg.capacity.min_replicas
    assert cfg.required_replicas(cfg.capacity.per_pod_rps * 5 + 0.1) == 6


@pytest.mark.parametrize("path, value, message", [
    (("trace", "test_days"), [5, 6], "must not overlap"),
    (("forecast", "lead_minutes"), [3, 20], "lead_minutes"),
    (("forecast", "headroom"), 1.5, "headroom"),
    (("capacity", "min_replicas"), 50, "min_replicas"),
    (("cluster", "provider"), "gke", "provider"),
])
def test_invalid_values_are_rejected(tmp_path, path, value, message):
    raw = yaml.safe_load((REPO / "pas.yaml").read_text(encoding="utf-8"))
    raw[path[0]][path[1]] = value
    cfg_path = tmp_path / "pas.yaml"
    cfg_path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match=message):
        load_config(cfg_path)


def test_unknown_names_give_helpful_errors():
    cfg = load_config(REPO / "pas.yaml")
    with pytest.raises(ConfigError, match="have:"):
        cfg.window("nope")
    with pytest.raises(ConfigError, match="have:"):
        cfg.scaler("nope")


def test_missing_file_is_explained(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.yaml")
