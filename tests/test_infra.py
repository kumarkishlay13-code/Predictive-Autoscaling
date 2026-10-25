"""Cluster, image and platform commands per provider, with every external call recorded."""

import dataclasses
import subprocess
from pathlib import Path

import pytest
import yaml

from pas import infra
from pas.config import load_config
from pas.kube import CommandError

REPO = Path(__file__).resolve().parents[1]

#: a kind cluster as infra/kind/cluster.yaml creates it: (node name, pas.dev/pool label)
NODES = [("pas-control-plane", None), ("pas-worker", "system"), ("pas-worker2", "app"),
         ("pas-worker3", "appb"), ("pas-worker4", "loadgen")]


@pytest.fixture
def calls(monkeypatch):
    """Record commands instead of running them. Set `calls.outputs[prefix] = stdout`."""

    class Recorder(list):
        outputs: dict[str, str]

        def commands(self, prefix: str) -> list[str]:
            return [c for c in self if c.startswith(prefix)]

    recorded = Recorder()
    recorded.outputs = {}

    def fake_run(cmd, **_):
        recorded.append(" ".join(str(c) for c in cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def fake_output(cmd, **_):
        line = " ".join(str(c) for c in cmd)
        recorded.append(line)
        return next((out for prefix, out in recorded.outputs.items() if line.startswith(prefix)),
                    "")

    def fake_kubectl_json(*args):
        nodes = NODES
        if "-l" in args:  # "pas.dev/pool in (a,b)"
            pools = args[args.index("-l") + 1].split("(")[1].rstrip(")").split(",")
            nodes = [n for n in NODES if n[1] in pools]
        return {"items": [{"metadata": {"name": name,
                                        "labels": {infra.POOL_LABEL: pool} if pool else {}}}
                          for name, pool in nodes]}

    monkeypatch.setattr(infra, "run", fake_run)
    monkeypatch.setattr(infra, "output", fake_output)
    monkeypatch.setattr(infra, "kubectl_json", fake_kubectl_json)
    monkeypatch.setattr(infra, "apply", lambda *a, **k: None)
    return recorded


def aks(cfg):
    return dataclasses.replace(cfg, cluster=dataclasses.replace(cfg.cluster, provider="aks"))


def test_kind_config_provides_every_pool_the_study_uses():
    cfg = load_config(REPO / "pas.yaml")
    kind = yaml.safe_load((REPO / cfg.cluster.kind_config).read_text(encoding="utf-8"))
    pools = {n.get("labels", {}).get(infra.POOL_LABEL) for n in kind["nodes"]}
    lanes = {lane["node_pool"] for lane in cfg.lanes.values()}
    assert {"system", "loadgen"} | lanes <= pools
    # One pinned node image for every node.
    assert len({n["image"] for n in kind["nodes"]}) == 1 and "@sha256:" in kind["nodes"][0]["image"]


def test_local_up_creates_the_cluster_and_taints_user_pools(project, calls):
    infra.infra_up(project)
    create = calls.commands("kind create cluster")
    assert len(create) == 1 and "--name pas" in create[0]
    assert create[0].replace("\\", "/").endswith("infra/kind/cluster.yaml --wait 5m")
    taints = calls.commands("kubectl taint")
    assert sorted(t.split()[3] for t in taints) == ["pas-worker2", "pas-worker3", "pas-worker4"]
    assert "pas.dev/pool=app:NoSchedule --overwrite" in taints[0]


def test_local_up_reuses_an_existing_cluster(project, calls):
    calls.outputs["kind get clusters"] = "other\npas\n"
    infra.infra_up(project)
    assert not calls.commands("kind create") and calls.commands("kind export kubeconfig")


def test_local_build_loads_each_image_into_the_nodes_that_run_it(project, calls):
    infra.build_images(project, ["demo-api", "forecaster"], tag="t1", with_lstm=True)
    builds = calls.commands("docker build")
    assert builds[0] == "docker build -t pas/demo-api:t1 -f app/Dockerfile app"
    assert "--build-arg WITH_LSTM=true" in builds[1]
    loads = calls.commands("kind load")
    assert loads[0].endswith("pas/demo-api:t1 --name pas --nodes pas-worker2,pas-worker3")
    assert loads[1].endswith("pas/forecaster:t1 --name pas --nodes pas-worker")
    assert not calls.commands("az ")
    assert infra.image_ref(project, "forecaster") == ("pas/forecaster", "t1")


def test_local_platform_installs_metrics_server_and_keda_first(project, calls):
    infra.platform_install(project)
    releases = [c.split()[3] for c in calls.commands("helm upgrade --install")]
    assert releases == ["metrics-server", "keda", "kube-prometheus-stack"]


def test_aks_platform_relies_on_the_add_ons(project, calls):
    infra.platform_install(aks(project))
    releases = [c.split()[3] for c in calls.commands("helm upgrade --install")]
    assert releases == ["kube-prometheus-stack"]


def test_local_stop_and_start_act_on_the_node_containers(project, calls):
    calls.outputs["kind get nodes"] = "pas-control-plane\npas-worker\n"
    infra.cluster_power(project, "stop")
    assert calls.commands("docker stop") == ["docker stop pas-control-plane pas-worker"]


def test_commands_refuse_a_foreign_kube_context(project, calls):
    calls.outputs["kubectl config current-context"] = "work-prod"
    with pytest.raises(CommandError, match="kind-pas"):
        infra.ensure_context(project)
    calls.outputs["kubectl config current-context"] = "kind-pas"
    infra.ensure_context(project)
