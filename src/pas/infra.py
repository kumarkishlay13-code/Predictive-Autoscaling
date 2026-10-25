"""Cluster lifecycle, image builds and the monitoring platform, for both providers.

`cluster.provider` in pas.yaml picks the backend (ADR-013):

* local: a kind cluster in Docker. Images are built with `docker build` and copied into the
  nodes with `kind load`; metrics-server and KEDA are installed with Helm.
* aks:   AKS from infra/terraform. Images are built in Azure with ACR Tasks; KEDA is the
  managed add-on.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any

from pas.config import Config
from pas.io import write_text
from pas.kube import CommandError, apply, configmap, kubectl_json, output, run

POOL_LABEL = "pas.dev/pool"
SYSTEM_POOL = "system"

KPS_CHART = "prometheus-community/kube-prometheus-stack"
KPS_VERSION = "92.3.0"
KPS_REPO = "https://prometheus-community.github.io/helm-charts"
# Local cluster only (AKS provides both). Pinned for reproducibility; KEDA 2.21 is tested on
# Kubernetes 1.34-1.36, matching the kind node image in infra/kind/cluster.yaml.
KEDA_CHART, KEDA_VERSION, KEDA_REPO = "kedacore/keda", "2.21.0", "https://kedacore.github.io/charts"
METRICS_CHART, METRICS_VERSION, METRICS_REPO = (
    "metrics-server/metrics-server", "3.14.0", "https://kubernetes-sigs.github.io/metrics-server/")

IMAGES = {"demo-api": ("app/Dockerfile", "app"),
          "forecaster": ("docker/forecaster.Dockerfile", ".")}
#: repository prefix of locally built images (kind resolves it as docker.io/pas/...)
LOCAL_REPO = "pas"


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
    """What identifies the cluster: kind name and context, or AKS resource names."""
    if cfg.cluster.is_local:
        name = cfg.cluster.kind_name
        return {"provider": "local", "cluster_name": name, "context": f"kind-{name}"}
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
    # `az aks get-credentials` names the kubectl context after the cluster.
    return {"provider": "aks", "context": state["cluster_name"], **state}


def ensure_context(cfg: Config) -> None:
    """Refuse to touch a cluster other than the study's (e.g. a work cluster in kubeconfig)."""
    expected = cluster_info(cfg)["context"]
    current = output(["kubectl", "config", "current-context"], check=False)
    if current != expected:
        raise CommandError(f"kubectl context is {current or '(none)'!r}, expected {expected!r}. "
                           f"Run `kubectl config use-context {expected}` (or `pas infra up`).")


# Cluster lifecycle --------------------------------------------------------------------------

def infra_up(cfg: Config, auto_approve: bool = False) -> None:
    if cfg.cluster.is_local:
        kind_up(cfg)
        return
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
    if cfg.cluster.is_local:
        run(["kind", "delete", "cluster", "--name", cfg.cluster.kind_name])
        save_state(cfg, images={})
        return
    run(["terraform", f"-chdir={tf_dir(cfg)}", "destroy", "-input=false"]
        + (["-auto-approve"] if auto_approve else []))
    save_state(cfg, cluster_name="", resource_group="", acr_name="", acr_login_server="")


def cluster_power(cfg: Config, action: str) -> None:
    """Stop or start the cluster without deleting it (`action` is "stop" or "start")."""
    if cfg.cluster.is_local:
        # A kind node is a Docker container; stopping it keeps its disk (images, Prometheus PV).
        run(["docker", action, *kind_nodes(cfg)])
        if action == "start":
            print("Nodes restarted. Give the API server a minute, then run `pas doctor`.")
        return
    info = cluster_info(cfg)
    run(["az", "aks", action, "-g", info["resource_group"], "-n", info["cluster_name"]])


def kind_clusters() -> list[str]:
    return output(["kind", "get", "clusters"]).split()


def kind_nodes(cfg: Config) -> list[str]:
    nodes = output(["kind", "get", "nodes", "--name", cfg.cluster.kind_name]).split()
    if not nodes:
        raise CommandError(f"kind cluster {cfg.cluster.kind_name!r} not found; run "
                           "`pas infra up`")
    return nodes


def kind_up(cfg: Config) -> None:
    name = cfg.cluster.kind_name
    if name in kind_clusters():
        print(f"kind cluster {name!r} already exists; updating the kubeconfig entry")
        run(["kind", "export", "kubeconfig", "--name", name])
    else:
        run(["kind", "create", "cluster", "--name", name,
             "--config", str(cfg.root / cfg.cluster.kind_config), "--wait", "5m"])
    taint_pools()
    save_state(cfg, provider="local", cluster_name=name)
    print(f"\nkind cluster {name!r} is up (context kind-{name}). Next: pas platform install")


def taint_pools() -> None:
    """Taint every labelled node except the system pool, as the AKS user pools are tainted.

    kind can label nodes from its config file but not taint them, so this runs after creation
    (idempotent thanks to --overwrite).
    """
    for node in kubectl_json("get", "nodes")["items"]:
        pool = node["metadata"].get("labels", {}).get(POOL_LABEL)
        if pool and pool != SYSTEM_POOL:
            run(["kubectl", "taint", "nodes", node["metadata"]["name"],
                 f"{POOL_LABEL}={pool}:NoSchedule", "--overwrite"])


# Images -------------------------------------------------------------------------------------

def default_tag(cfg: Config) -> str:
    try:
        sha = output(["git", "-C", str(cfg.root), "rev-parse", "--short", "HEAD"])
        dirty = output(["git", "-C", str(cfg.root), "status", "--porcelain"])
    except CommandError:
        return time.strftime("dev-%Y%m%d%H%M%S")
    return f"{sha}-dirty{time.strftime('%m%d%H%M')}" if dirty else sha


def _image_pools(cfg: Config, name: str) -> list[str]:
    """Pools whose nodes run an image: demo-api on the lanes' pools, forecaster on system."""
    if name == "forecaster":
        return [SYSTEM_POOL]
    return sorted({str(lane.get("node_pool")) for lane in cfg.lanes.values()
                   if lane.get("node_pool")})


def _pool_nodes(pools: list[str]) -> list[str]:
    selector = f"{POOL_LABEL} in ({','.join(pools)})"
    return [n["metadata"]["name"] for n in kubectl_json("get", "nodes", "-l", selector)["items"]]


def build_images(cfg: Config, names: list[str], tag: str | None = None,
                 with_lstm: bool | None = None) -> dict[str, str]:
    tag = tag or default_tag(cfg)
    if with_lstm is None:
        with_lstm = any(cfg.models_dir.glob("lstm*.pkl"))
    images = dict(load_state(cfg).get("images", {}))
    acr = None if cfg.cluster.is_local else cluster_info(cfg)["acr_name"]
    for name in names:
        dockerfile, context = IMAGES[name]
        lstm_arg = (["--build-arg", "WITH_LSTM=true"] if name == "forecaster" and with_lstm
                    else [])
        if acr:
            run(["az", "acr", "build", "-r", acr, "-t", f"pas/{name}:{tag}", "-f", dockerfile,
                 *lstm_arg, context], cwd=cfg.root)
        else:
            ref = f"{LOCAL_REPO}/{name}:{tag}"
            run(["docker", "build", "-t", ref, "-f", dockerfile, *lstm_arg, context],
                cwd=cfg.root)
            # Copy the image only to the nodes that run it (the forecaster image is large).
            nodes = _pool_nodes(_image_pools(cfg, name))
            run(["kind", "load", "docker-image", ref, "--name", cfg.cluster.kind_name]
                + (["--nodes", ",".join(nodes)] if nodes else []))
        images[name] = tag
    save_state(cfg, images=images)
    return images


def image_ref(cfg: Config, name: str) -> tuple[str, str]:
    tag = load_state(cfg).get("images", {}).get(name)
    if not tag:
        raise CommandError(f"no {name} image recorded; run `pas build` first")
    if cfg.cluster.is_local:
        return f"{LOCAL_REPO}/{name}", tag
    return f"{cluster_info(cfg)['acr_login_server']}/pas/{name}", tag


# Monitoring platform ------------------------------------------------------------------------

def _helm_install(release: str, chart: str, version: str, namespace: str,
                  values: Path | None = None) -> None:
    run(["helm", "upgrade", "--install", release, chart, "--version", version,
         "-n", namespace, "--create-namespace"]
        + (["-f", str(values)] if values else []) + ["--wait", "--timeout", "15m"])


def platform_install(cfg: Config) -> None:
    mon = cfg.root / "k8s" / "monitoring"
    repos = [("prometheus-community", KPS_REPO)]
    if cfg.cluster.is_local:
        repos += [("kedacore", KEDA_REPO), ("metrics-server", METRICS_REPO)]
    for repo, url in repos:
        run(["helm", "repo", "add", repo, url, "--force-update"])
    run(["helm", "repo", "update", *(r for r, _ in repos)])
    if cfg.cluster.is_local:
        # AKS ships these two; kind does not. The HPA and KEDA's CPU trigger need
        # metrics-server, the predictive scaler needs KEDA.
        _helm_install("metrics-server", METRICS_CHART, METRICS_VERSION, "kube-system",
                      mon / "metrics-server.values.yaml")
        _helm_install("keda", KEDA_CHART, KEDA_VERSION, "keda", mon / "keda.values.yaml")
    _helm_install("kube-prometheus-stack", KPS_CHART, KPS_VERSION, "monitoring",
                  mon / "kube-prometheus-stack.values.yaml")
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
