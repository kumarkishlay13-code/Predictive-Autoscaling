"""Cluster lifecycle (Terraform), image builds (ACR Tasks) and the monitoring platform."""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any

from pas.config import Config
from pas.io import write_text
from pas.kube import CommandError, apply, configmap, output, run

KPS_CHART = "prometheus-community/kube-prometheus-stack"
KPS_VERSION = "92.3.0"
KPS_REPO = "https://prometheus-community.github.io/helm-charts"
IMAGES = {"demo-api": ("app/Dockerfile", "app"),
          "forecaster": ("docker/forecaster.Dockerfile", ".")}


# Local state (.pas/state.json): what `pas` learned from Terraform and image builds ----------

def state_path(cfg: Config) -> Path:
    return cfg.root / ".pas" / "state.json"


def load_state(cfg: Config) -> dict[str, Any]:
    path = state_path(cfg)
    return json.loads(path.read_text()) if path.exists() else {}


def save_state(cfg: Config, **updates: Any) -> dict[str, Any]:
    state = {**load_state(cfg), **updates}
    path = state_path(cfg)
    path.parent.mkdir(exist_ok=True)
    write_text(path, json.dumps(state, indent=2))
    return state


def tf_dir(cfg: Config) -> Path:
    return cfg.root / "infra" / "terraform"


def terraform_outputs(cfg: Config) -> dict[str, str]:
    raw = output(["terraform", f"-chdir={tf_dir(cfg)}", "output", "-json"])
    return {k: v["value"] for k, v in json.loads(raw or "{}").items()}


def cluster_info(cfg: Config) -> dict[str, str]:
    """Resource group, cluster and ACR names, from state or Terraform outputs."""
    state = load_state(cfg)
    if not state.get("cluster_name"):
        outs = terraform_outputs(cfg)
        if not outs:
            raise CommandError("no Terraform outputs found; run `pas infra up` first")
        state = save_state(cfg, resource_group=outs["resource_group"],
                           cluster_name=outs["cluster_name"], acr_name=outs["acr_name"],
                           acr_login_server=outs["acr_login_server"])
    if cfg.cluster.acr_login_server:
        state["acr_login_server"] = cfg.cluster.acr_login_server
    return state


# Terraform ----------------------------------------------------------------------------------

def infra_up(cfg: Config, auto_approve: bool = False) -> None:
    d = tf_dir(cfg)
    if not (d / "terraform.tfvars").exists():
        raise CommandError(f"{d / 'terraform.tfvars'} is missing. Copy terraform.tfvars.example "
                           "to terraform.tfvars and set subscription_id.")
    run(["terraform", f"-chdir={d}", "init", "-input=false"])
    run(["terraform", f"-chdir={d}", "apply", "-input=false"]
        + (["-auto-approve"] if auto_approve else []))
    save_state(cfg, cluster_name="")  # force re-read of outputs
    info = cluster_info(cfg)
    run(["az", "aks", "get-credentials", "-g", info["resource_group"], "-n",
         info["cluster_name"], "--overwrite-existing"])
    print(f"\nCluster {info['cluster_name']} is up. Next: pas platform install")


def infra_down(cfg: Config, auto_approve: bool = False) -> None:
    run(["terraform", f"-chdir={tf_dir(cfg)}", "destroy", "-input=false"]
        + (["-auto-approve"] if auto_approve else []))
    save_state(cfg, cluster_name="", resource_group="", acr_name="", acr_login_server="")


def aks_power(cfg: Config, action: str) -> None:
    info = cluster_info(cfg)
    run(["az", "aks", action, "-g", info["resource_group"], "-n", info["cluster_name"]])


# Images -------------------------------------------------------------------------------------

def default_tag(cfg: Config) -> str:
    try:
        sha = output(["git", "-C", str(cfg.root), "rev-parse", "--short", "HEAD"])
        dirty = output(["git", "-C", str(cfg.root), "status", "--porcelain"])
    except CommandError:
        return time.strftime("dev-%Y%m%d%H%M%S")
    return f"{sha}-dirty{time.strftime('%m%d%H%M')}" if dirty else sha


def build_images(cfg: Config, names: list[str], tag: str | None = None,
                 with_lstm: bool | None = None) -> dict[str, str]:
    info = cluster_info(cfg)
    tag = tag or default_tag(cfg)
    if with_lstm is None:
        with_lstm = any(cfg.models_dir.glob("lstm*.pkl"))
    images = dict(load_state(cfg).get("images", {}))
    for name in names:
        dockerfile, context = IMAGES[name]
        cmd = ["az", "acr", "build", "-r", info["acr_name"], "-t", f"pas/{name}:{tag}",
               "-f", dockerfile]
        if name == "forecaster" and with_lstm:
            cmd += ["--build-arg", "WITH_LSTM=true"]
        run([*cmd, context], cwd=cfg.root)
        images[name] = tag
    save_state(cfg, images=images)
    return images


def image_ref(cfg: Config, name: str) -> tuple[str, str]:
    info = cluster_info(cfg)
    tag = load_state(cfg).get("images", {}).get(name)
    if not tag:
        raise CommandError(f"no {name} image recorded; run `pas build` first")
    return f"{info['acr_login_server']}/pas/{name}", tag


# Monitoring platform ------------------------------------------------------------------------

def platform_install(cfg: Config) -> None:
    run(["helm", "repo", "add", "prometheus-community", KPS_REPO, "--force-update"])
    run(["helm", "repo", "update", "prometheus-community"])
    run(["helm", "upgrade", "--install", "kube-prometheus-stack", KPS_CHART,
         "--version", KPS_VERSION, "-n", "monitoring", "--create-namespace",
         "-f", str(cfg.root / "k8s" / "monitoring" / "kube-prometheus-stack.values.yaml"),
         "--wait", "--timeout", "15m"])
    install_dashboards(cfg)
    keda = run(["kubectl", "get", "crd", "scaledobjects.keda.sh"], check=False, capture=True,
               quiet=True)
    if keda.returncode != 0:
        print("WARNING: KEDA CRDs not found. Enable the add-on: "
              "az aks update -g <rg> -n <cluster> --enable-keda")
    print("\nMonitoring is installed. Open Grafana with: pas grafana")


def install_dashboards(cfg: Config) -> None:
    files = {p.name: p for p in sorted((cfg.root / "k8s" / "monitoring" / "dashboards")
                                        .glob("*.json"))}
    cm = configmap("pas-dashboards", "monitoring", files, labels={"grafana_dashboard": "1"})
    apply(cm)


def grafana_password() -> str:
    raw = output(["kubectl", "-n", "monitoring", "get", "secret", "kube-prometheus-stack-grafana",
                  "-o", "jsonpath={.data.admin-password}"])
    return base64.b64decode(raw).decode() if raw else "(not found)"
