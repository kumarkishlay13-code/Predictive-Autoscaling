"""Thin wrappers around kubectl, helm, az and terraform.

Every command is echoed before it runs, so `pas` never does anything you cannot see or repeat
by hand.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import yaml

INSTALL_HINTS = {
    "kubectl": "winget install Kubernetes.kubectl",
    "helm": "winget install Helm.Helm",
    "az": "winget install Microsoft.AzureCLI",
    "terraform": "winget install Hashicorp.Terraform",
    "k6": "winget install GrafanaLabs.k6",
    "go": "winget install GoLang.Go",
    "git": "winget install Git.Git",
}


class CommandError(RuntimeError):
    pass


def find_tool(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    # winget puts command aliases here; a terminal opened before the install lacks it on PATH.
    links = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links"
    for candidate in (links / f"{name}.exe", links / name):
        if candidate.exists():
            return str(candidate)
    return None


def require_tool(name: str) -> str:
    path = find_tool(name)
    if path is None:
        hint = INSTALL_HINTS.get(name, "")
        raise CommandError(f"'{name}' was not found on PATH. Install it ({hint}) and open a new "
                           "terminal, or run `pas doctor` for a full check.")
    return path


def _display(cmd: Sequence[str]) -> str:
    return " ".join(shlex.quote(str(c)) for c in cmd)


def run(cmd: Sequence[str], *, check: bool = True, capture: bool = False,
        input_text: str | None = None, quiet: bool = False,
        cwd: str | Path | None = None, timeout: float | None = None,
        ) -> subprocess.CompletedProcess[str]:
    cmd = [str(c) for c in cmd]
    exe = require_tool(cmd[0])
    if not quiet:
        print(f"$ {_display(cmd)}", file=sys.stderr, flush=True)
    proc = subprocess.run([exe, *cmd[1:]], capture_output=capture, text=True, input=input_text,
                          cwd=cwd, timeout=timeout, encoding="utf-8", errors="replace")
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() if capture else ""
        raise CommandError(f"command failed ({proc.returncode}): {_display(cmd)}"
                           + (f"\n{detail}" if detail else ""))
    return proc


def output(cmd: Sequence[str], **kwargs: Any) -> str:
    return run(cmd, capture=True, quiet=kwargs.pop("quiet", True), **kwargs).stdout.strip()


# kubectl helpers ---------------------------------------------------------------------------

def kubectl_json(*args: str) -> dict:
    return json.loads(output(["kubectl", *args, "-o", "json"]))


def apply(manifests: dict | list[dict], quiet: bool = False) -> None:
    docs = manifests if isinstance(manifests, list) else [manifests]
    text = yaml.safe_dump_all(docs, sort_keys=False)
    run(["kubectl", "apply", "-f", "-"], input_text=text, capture=True, quiet=quiet)


def ensure_namespace(name: str) -> None:
    apply({"apiVersion": "v1", "kind": "Namespace",
           "metadata": {"name": name, "labels": {"app.kubernetes.io/part-of": "pas"}}},
          quiet=True)


def configmap(name: str, namespace: str, files: dict[str, str | Path],
              labels: dict[str, str] | None = None) -> dict:
    data = {key: Path(path).read_text(encoding="utf-8") for key, path in files.items()}
    return {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": name, "namespace": namespace, "labels": labels or {}},
            "data": data}


def ready_replicas(namespace: str, deployment: str) -> int:
    status = kubectl_json("-n", namespace, "get", "deployment", deployment).get("status", {})
    return int(status.get("readyReplicas") or 0)


def wait_rollout(namespace: str, deployment: str, timeout: str = "10m") -> None:
    run(["kubectl", "-n", namespace, "rollout", "status", f"deployment/{deployment}",
         f"--timeout={timeout}"])


def wait_ready_replicas(namespace: str, deployment: str, want: int, timeout_s: float) -> float:
    """Poll until exactly `want` replicas are ready; return seconds waited."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        if ready_replicas(namespace, deployment) == want:
            return time.monotonic() - t0
        time.sleep(0.5)
    raise CommandError(f"{namespace}/{deployment}: {want} ready replicas not reached in "
                       f"{timeout_s:.0f}s")


def job_logs(namespace: str, job: str) -> str:
    return output(["kubectl", "-n", namespace, "logs", f"job/{job}", "--tail=-1"])


def wait_job(namespace: str, job: str, timeout_s: float) -> bool:
    """Wait for a Job to finish. Returns True on success, False on failure."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        status = kubectl_json("-n", namespace, "get", "job", job).get("status", {})
        if status.get("succeeded"):
            return True
        if status.get("failed"):
            return False
        time.sleep(10)
    raise CommandError(f"job {namespace}/{job} did not finish in {timeout_s:.0f}s")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def port_forward(service: str, remote_port: int, local_port: int | None = None,
                 timeout_s: float = 30) -> Iterator[str]:
    """Forward `namespace/service` to localhost and yield its base URL."""
    namespace, name = service.split("/", 1)
    local_port = local_port or _free_port()
    exe = require_tool("kubectl")
    proc = subprocess.Popen([exe, "-n", namespace, "port-forward", f"svc/{name}",
                             f"{local_port}:{remote_port}"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        t0 = time.monotonic()
        while True:
            if proc.poll() is not None:
                raise CommandError(f"port-forward to {service} failed: "
                                   f"{proc.stderr.read() if proc.stderr else ''}")
            with contextlib.suppress(OSError), socket.create_connection(
                    ("127.0.0.1", local_port), timeout=1):
                break
            if time.monotonic() - t0 > timeout_s:
                raise CommandError(f"port-forward to {service} did not open in {timeout_s}s")
            time.sleep(0.3)
        yield f"http://127.0.0.1:{local_port}"
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)
