"""`pas`: one command line for the whole study. Run `pas --help` or `pas <command> --help`."""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time

from pas import __version__
from pas.config import DEFAULT_CONFIG, Config, ConfigError, load_config
from pas.io import write_csv, write_text
from pas.kube import CommandError

EPILOG = """\
typical workflow:
  pas doctor                          check tools, config, data and cluster
  pas data download && pas data build trace -> load series + replay windows
  pas train naive arima               fit models on the training days
  pas backtest naive arima            offline accuracy (RQ1)
  pas infra up && pas platform install local kind cluster (or AKS), Prometheus, KEDA
  pas build                           container images (docker + kind load, or ACR)
  pas smoke                           5-minute end-to-end check of the control loop
  pas capacity && pas lag             measure per-pod capacity and scaling lag
  pas experiment plan && pas experiment run
  pas analyze                         tables and figures for the report
docs: README.md, docs/setup-and-testing.md and docs/
"""


# Helpers -----------------------------------------------------------------------------------

def _table(df, floatfmt: str = "{:.3f}") -> str:
    """Render a DataFrame as a GitHub-flavoured Markdown table (no extra dependency)."""
    cols = list(df.columns)

    def cell(v) -> str:
        if isinstance(v, float):
            return "-" if v != v else floatfmt.format(v)  # NaN check
        return str(v)

    lines = ["| " + " | ".join(map(str, cols)) + " |", "|" + "---|" * len(cols)]
    lines += ["| " + " | ".join(cell(v) for v in row) + " |" for row in df.itertuples(index=False)]
    return "\n".join(lines)


def _shard(value: str | None) -> tuple[int, int] | None:
    if not value:
        return None
    i, n = (int(x) for x in value.split("/"))
    if not 0 <= i < n:
        raise argparse.ArgumentTypeError("--shard must be i/n with 0 <= i < n, e.g. 0/2")
    return i, n


# data ----------------------------------------------------------------------------------------

def cmd_data(cfg: Config, args: argparse.Namespace) -> None:
    from pas.data import trace, windows

    if args.action == "download":
        files = trace.download(cfg.trace.archive_url, cfg.trace.raw_dir, force=args.force)
        print(f"{len(files)} daily files in {cfg.trace.raw_dir}")
    elif args.action == "apps":
        table, invocations = trace.rank_apps(cfg.trace.raw_dir, cfg.trace.days,
                                             cfg.trace.start_date, cfg.trace.trigger,
                                             top=args.top)
        cfg.trace.processed_dir.mkdir(parents=True, exist_ok=True)
        invocations.to_parquet(trace.cache_path(cfg.trace.processed_dir, cfg.trace.trigger))
        shown = table.assign(app=table["app"].str[:12] + "...")
        print(_table(shown, "{:.2f}"))
        if args.plot:
            from pas.analysis.plots import plot_apps
            print(f"figure: {plot_apps(cfg, table, invocations)}")
        cfg.reports_dir.mkdir(exist_ok=True)
        write_csv(table, cfg.reports_dir / "apps.csv")
        print("\nFull hashes: reports/apps.csv. To use one, set trace.signal: <hash> in "
              "pas.yaml and run `pas data build` (fast: the candidates are cached).")
    elif args.action in ("build", "windows"):
        if args.action == "build":
            df = windows.build_series(cfg)
            meta = json.loads(cfg.series_path.with_suffix(".json").read_text())
            print(f"series: {len(df)} minutes, signal={meta['signal']}, "
                  f"scale={meta['scale_factor']:.4g}, daily autocorr="
                  f"{meta['stats']['daily_autocorr']:.2f} -> {cfg.series_path}")
        rps = windows.load_series(cfg)
        paths = windows.write_windows(cfg, rps, args.only)
        import pandas as pd
        rows = [{k: w[k] for k in ("name", "start", "minutes", "peak_rps",
                                   "peak_replicas_needed")}
                for w in (windows.load_window(p) for p in paths)]
        print(_table(pd.DataFrame(rows), "{:.1f}"))
        if args.plot:
            from pas.analysis.plots import plot_series
            out = plot_series(cfg, rps, paths)
            print(f"figure: {out}")


# models --------------------------------------------------------------------------------------

def _load_split(cfg: Config):
    from pas.data.windows import load_series, split
    rps = load_series(cfg)
    return rps, split(cfg, rps)


def cmd_train(cfg: Config, args: argparse.Namespace) -> None:
    from pas.models import create_model

    _, parts = _load_split(cfg)
    for name in args.models:
        model = create_model(name, horizon=cfg.forecast.horizon_minutes)
        t0 = time.perf_counter()
        if name.startswith("lstm"):
            model.fit(parts["train"], val=parts["val"])
        else:
            model.fit(parts["train"])
        path = model.save(cfg.models_dir / f"{name}.pkl")
        print(f"{name}: trained in {time.perf_counter() - t0:.1f}s -> {path}")


def cmd_backtest(cfg: Config, args: argparse.Namespace) -> None:
    import pandas as pd

    from pas.backtest import mase_scale, rolling_forecasts, score
    from pas.models import create_model, load_model

    rps, parts = _load_split(cfg)
    target = parts[args.split]
    scale = mase_scale(parts["train"])
    horizons = [h for h in cfg.backtest.horizons if h <= cfg.forecast.horizon_minutes]
    out_dir = cfg.reports_dir / "backtest"
    out_dir.mkdir(parents=True, exist_ok=True)
    tables = []
    for name in args.models:
        path = cfg.models_dir / f"{name}.pkl"
        if path.exists():
            model = load_model(path)
        elif name in ("naive", "last"):
            model = create_model(name).fit(parts["train"])
        else:
            raise ConfigError(f"{path} not found; run `pas train {name}` first")
        quantile = args.quantile if model.trained_quantile is None else None
        label = name + (f"-q{round(args.quantile * 100)}" if quantile else "")
        print(f"backtesting {label} on {args.split} days ...", flush=True)
        fc = rolling_forecasts(model, rps, target.index[0], target.index[-1],
                               cfg.forecast.horizon_minutes, cfg.backtest.stride_minutes,
                               quantile=quantile, context=cfg.forecast.history_minutes)
        write_csv(fc, out_dir / f"{label}-{args.split}.csv")
        table = score(fc, scale, horizons, quantile=quantile or model.trained_quantile)
        table.insert(0, "model", label)
        table["predict_ms"] = table.attrs.get("mean_predict_ms")
        tables.append(table)
    result = pd.concat(tables, ignore_index=True)
    result = result[[c for c in result.columns if c != "predict_ms"] + ["predict_ms"]]
    md = _table(result)
    suffix = f"-q{round(args.quantile * 100)}" if args.quantile else ""
    write_text(out_dir / f"summary-{args.split}{suffix}.md", md + "\n")
    print(md)
    print(f"\nMASE < 1 beats seasonal naive. Details in {out_dir}")


def cmd_serve(cfg: Config | None, args: argparse.Namespace) -> None:
    import uvicorn

    from pas.forecaster.service import Settings, create_app

    env = Settings.from_env()
    overrides = {k: v for k, v in {
        "model": args.model, "lane": args.lane, "prometheus_url": args.prometheus,
        "window_file": args.window, "model_path": args.model_path,
        "interval_seconds": args.interval}.items() if v is not None}
    settings = Settings(**{**env.__dict__, **overrides})
    uvicorn.run(create_app(settings), host=args.host, port=args.port, log_level="info")


# cluster -------------------------------------------------------------------------------------

def cmd_infra(cfg: Config, args: argparse.Namespace) -> None:
    from pas import infra

    if args.action == "up":
        infra.infra_up(cfg, auto_approve=args.yes)
    elif args.action == "down":
        infra.infra_down(cfg, auto_approve=args.yes)
    elif args.action in ("stop", "start"):
        infra.cluster_power(cfg, args.action)
    elif args.action == "outputs":
        print(json.dumps(infra.cluster_info(cfg), indent=2))


def cmd_build(cfg: Config, args: argparse.Namespace) -> None:
    from pas.infra import IMAGES, build_images

    names = args.only or list(IMAGES)
    images = build_images(cfg, names, tag=args.tag, with_lstm=args.with_lstm)
    print("images: " + ", ".join(f"{k}:{v}" for k, v in images.items()))


def cmd_platform(cfg: Config, args: argparse.Namespace) -> None:
    from pas import infra

    if args.action == "install":
        infra.platform_install(cfg)
    else:
        infra.install_dashboards(cfg)


def cmd_forward(cfg: Config, args: argparse.Namespace) -> None:
    from pas.infra import grafana_password
    from pas.kube import port_forward

    if args.command == "grafana":
        service, remote, local = cfg.cluster.grafana_service, 80, args.port or 3000
        print(f"Grafana: user admin, password {grafana_password()}")
    else:
        service, remote, local = cfg.cluster.prometheus_service, 9090, args.port or 9090
    with port_forward(service, remote, local) as url:
        print(f"{args.command} at {url}  (Ctrl+C to stop)", flush=True)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass


def cmd_lane(cfg: Config, args: argparse.Namespace) -> None:
    from pas.experiments.lanes import deploy_lane
    from pas.kube import run

    if args.action == "deploy":
        window = cfg.windows_dir / f"{args.window}.json" if args.window else None
        deploy_lane(cfg, args.lane, args.scaler, window=window, startup_delay=args.startup_delay)
    else:
        run(["kubectl", "-n", args.lane, "get", "deploy,hpa,scaledobject,pods", "-o", "wide"],
            check=False)


def cmd_smoke(cfg: Config, args: argparse.Namespace) -> None:
    from pas.experiments.smoke import smoke_test

    checks = smoke_test(cfg, args.lane, args.scaler, args.window, minutes=args.minutes,
                        step_seconds=args.step_seconds, work_units=args.work_units)
    print(f"\nsmoke test: lane {args.lane}, scaler {args.scaler}, window {args.window}")
    for c in checks:
        print(f"  [{'ok' if c.ok else '!!'}] {c.label}" + ("" if c.ok else f"  -> {c.detail}"))
    failed = [c for c in checks if not c.ok]
    if failed:
        raise CommandError(f"smoke test failed: {len(failed)} of {len(checks)} checks")
    print("all checks passed: the control loop is wired up end to end")


def cmd_capacity(cfg: Config, args: argparse.Namespace) -> None:
    from pas.experiments.measure import capacity_test

    guess = cfg.capacity.per_pod_rps
    steps = args.steps or [round(guess * f, 1) for f in (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2)]
    report = capacity_test(cfg, args.lane, steps, args.step_seconds, args.settle_seconds,
                           cfg.reports_dir)
    import pandas as pd
    print(_table(pd.DataFrame(report["steps"]), "{:.1f}"))
    print(f"\nper-pod capacity within SLO: {report['per_pod_rps']:g} RPS "
          f"(saved to reports/capacity.json)")
    print(f"Update pas.yaml -> capacity.per_pod_rps: {report['per_pod_rps']:g}, then rerun "
          "`pas data build` and `pas train`.")


def cmd_lag(cfg: Config, args: argparse.Namespace) -> None:
    from pas.experiments.measure import measure_lag

    delay = cfg.app.startup_delay_seconds if args.startup_delay is None else args.startup_delay
    report = measure_lag(cfg, args.lane, delay, args.samples, cfg.reports_dir)
    print(f"\nscale-up to ready: mean {report['observed_mean_s']:.1f}s, "
          f"max {report['observed_max_s']:.1f}s (startup delay {delay}s)")
    print(report["note"])


def cmd_experiment(cfg: Config, args: argparse.Namespace) -> None:
    from pas.experiments import runner

    plan_path = cfg.root / args.plan
    results = cfg.root / args.results
    if args.action == "plan":
        plan = runner.make_plan(cfg, cfg.root / args.matrix)
        runner.write_plan(plan, plan_path)
        print(f"{len(plan)} runs -> {plan_path}")
        return
    plan = runner.select(runner.read_plan(plan_path), _shard(args.shard), args.only)
    if args.action == "status":
        done = [r for r in plan if runner.is_done(results, r.run_id)]
        print(f"{len(done)}/{len(plan)} runs complete")
        for r in plan:
            mark = "done" if r in done else "    "
            print(f"  [{mark}] {r.order:3d} {r.run_id}")
        return
    runner.run_plan(cfg, plan, args.lane, results, quiet_seconds=args.quiet_seconds,
                    stop_on_error=args.stop_on_error, dry_run=args.dry_run)


def cmd_analyze(cfg: Config, args: argparse.Namespace) -> None:
    from pas.analysis.metrics import collect, compare, markdown_table

    results = cfg.root / args.results
    runs = collect(cfg, results)
    if runs.empty:
        print(f"no completed runs in {results}")
        return
    out = cfg.reports_dir
    out.mkdir(exist_ok=True)
    write_csv(runs, out / "runs.csv")
    table = compare(runs, args.baseline)
    md = markdown_table(table)
    write_text(out / "summary.md",
               f"# Experiment summary\n\nBaseline for p-values: `{args.baseline}` "
               f"(Mann-Whitney U on SLO-violation minutes). Values are mean ± 95% CI.\n\n{md}\n")
    print(md)
    invalid = runs[~runs["valid"]]
    if not invalid.empty:
        print(f"\nWARNING: {len(invalid)} run(s) excluded (load generator dropped iterations): "
              + ", ".join(invalid["run_id"]))
    if args.plots:
        from pas.analysis.plots import plot_run
        for run_dir in sorted(p.parent for p in results.glob("*/timeseries.parquet")):
            plot_run(cfg, run_dir, out / "figures")
    print(f"\nwritten: {out / 'runs.csv'}, {out / 'summary.md'}")


def cmd_demo(cfg: Config, args: argparse.Namespace) -> None:
    from pas.experiments.runner import demo_start
    from pas.kube import run

    lanes = {args.reactive_lane: args.reactive, args.predictive_lane: args.predictive}
    if args.action == "start":
        demo_start(cfg, args.window, lanes)
        print("\nBoth lanes are replaying the same load. Watch it with `pas grafana` "
              "(dashboard: Predictive autoscaling, lanes: both).")
    else:
        for lane in lanes:
            run(["kubectl", "-n", lane, "delete", "job", "-l", "app.kubernetes.io/name=k6",
                 "--ignore-not-found"], check=False)


def cmd_doctor(cfg_path: str) -> int:
    from pas.kube import INSTALL_HINTS, find_tool, output

    problems = 0

    def check(ok: bool, label: str, hint: str = "", optional: bool = False) -> None:
        nonlocal problems
        mark = "ok" if ok else ("--" if optional else "!!")
        print(f"  [{mark}] {label}" + ("" if ok or not hint else f"  -> {hint}"))
        problems += 0 if ok or optional else 1

    print(f"pas {__version__}, Python {platform.python_version()} on {platform.system()}")
    try:
        cfg = load_config(cfg_path)
    except ConfigError as exc:
        print(f"  [!!] config {cfg_path}  -> {exc}")
        return 1
    local = cfg.cluster.is_local
    print(f"tools (provider: {cfg.cluster.provider})")
    required = ["git", "kubectl", "helm"] + (["docker", "kind"] if local else ["az", "terraform"])
    for tool in required:
        check(find_tool(tool) is not None, tool, INSTALL_HINTS.get(tool, ""))
    for tool, needed_for in (("k6", "load tests outside the cluster"),
                             ("go", "testing demo-api outside Docker")):
        check(find_tool(tool) is not None, f"{tool} (optional: {needed_for})", optional=True)
    if local and find_tool("docker"):
        try:
            cpus, mem = output(["docker", "info", "--format", "{{.NCPU}} {{.MemTotal}}"],
                               timeout=20).split()
            gib = int(mem) / 2**30
            check(int(cpus) >= 8 and gib >= 12, f"Docker engine: {cpus} CPUs, {gib:.1f} GiB",
                  "8+ CPUs and 12+ GiB recommended (docs/setup-and-testing.md)")
        except Exception as exc:  # noqa: BLE001 - any failure means Docker is not usable
            check(False, "Docker engine running", f"{exc}".splitlines()[0])
    print("project")
    check(True, f"config {cfg_path}")
    check(any(cfg.trace.raw_dir.glob("invocations_per_function_md.anon.d*.csv")),
          "trace downloaded", "pas data download")
    check(cfg.series_path.exists(), "load series built", "pas data build")
    check(all((cfg.windows_dir / f"{w.name}.json").exists() for w in cfg.windows),
          "replay windows built", "pas data build")
    models = sorted(p.stem for p in cfg.models_dir.glob("*.pkl"))
    check(bool(models), f"trained models: {', '.join(models) or 'none'}", "pas train naive arima")
    print("cluster")
    if find_tool("kubectl") is None:
        check(False, "kubectl missing; skipping cluster checks")
        return problems
    try:
        from pas.infra import cluster_info
        expected = cluster_info(cfg)["context"]
        context = output(["kubectl", "config", "current-context"], check=False, timeout=10)
        check(context == expected, f"kube context: {context or 'none'} (expected {expected})",
              f"pas infra up, or kubectl config use-context {expected}")
        nodes = output(["kubectl", "get", "nodes", "-o", "name"], timeout=20)
        check(bool(nodes), f"cluster reachable ({len(nodes.split())} nodes)")
        pools = output(["kubectl", "get", "nodes", "-o",
                        "jsonpath={.items[*].metadata.labels.pas\\.dev/pool}"], timeout=20)
        wanted = {"system", "loadgen", *(str(v.get("node_pool")) for v in cfg.lanes.values())}
        check(wanted <= set(pools.split()), f"node pools labelled: {pools or 'none'}",
              f"need {', '.join(sorted(wanted))}: pas infra up")
        if local:
            ms = output(["kubectl", "get", "apiservice", "v1beta1.metrics.k8s.io", "-o", "name"],
                        check=False, timeout=20)
            check(bool(ms), "metrics-server installed", "pas platform install")
        keda = output(["kubectl", "get", "crd", "scaledobjects.keda.sh", "-o", "name"],
                      check=False, timeout=20)
        check(bool(keda), "KEDA installed",
              "pas platform install" if local else "az aks update --enable-keda")
        mon = output(["kubectl", "-n", "monitoring", "get", "svc", "-o", "name"], check=False,
                     timeout=20)
        check("kube-prometheus-stack-prometheus" in mon, "monitoring installed",
              "pas platform install")
    except Exception as exc:  # noqa: BLE001 - report any cluster problem as a failed check
        hint = "pas infra up / pas infra start" if local else "pas infra up / az login"
        check(False, "cluster", f"{exc}".splitlines()[0] + f" ({hint})")
    from pas.infra import load_state
    images = load_state(cfg).get("images", {})
    check(bool(images), f"images built: {images or 'none'}", "pas build")
    print("all good" if problems == 0 else f"{problems} item(s) need attention")
    return 0 if problems == 0 else 1


# Parser ----------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pas", description="Predictive autoscaling study toolkit.",
                                epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=os.environ.get("PAS_CONFIG", DEFAULT_CONFIG),
                   help="path to pas.yaml (default: ./pas.yaml or $PAS_CONFIG)")
    p.add_argument("--version", action="version", version=f"pas {__version__}")
    sub = p.add_subparsers(dest="command", required=True, metavar="command")

    sub.add_parser("doctor", help="check tools, config, data, models and cluster")

    d = sub.add_parser("data", help="download the trace, build the load series and windows")
    d.add_argument("action", choices=["download", "apps", "build", "windows"])
    d.add_argument("--force", action="store_true", help="download: fetch again")
    d.add_argument("--top", type=int, default=20, help="apps: rows to show")
    d.add_argument("--only", nargs="+", help="build/windows: only these windows")
    d.add_argument("--plot", action="store_true", help="apps/build/windows: save a figure")

    from pas.models import MODEL_NAMES
    t = sub.add_parser("train", help="fit forecasting models on the training days",
                       description="Models: " + "; ".join(f"{k}: {v}"
                                                        for k, v in MODEL_NAMES.items()))
    t.add_argument("models", nargs="+", choices=list(MODEL_NAMES))

    b = sub.add_parser("backtest", help="rolling-origin forecast accuracy per horizon")
    b.add_argument("models", nargs="+", choices=list(MODEL_NAMES))
    b.add_argument("--split", choices=["val", "test"], default="test")
    b.add_argument("--quantile", type=float, help="e.g. 0.9 for naive/last/arima")

    s = sub.add_parser("serve", help="run the forecaster service (normally inside the cluster)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--model")
    s.add_argument("--model-path")
    s.add_argument("--lane")
    s.add_argument("--prometheus", help="Prometheus base URL")
    s.add_argument("--window", help="window JSON for experiment mode")
    s.add_argument("--interval", type=float, help="seconds between forecasts")

    i = sub.add_parser("infra", help="create, stop, start or delete the cluster (kind or AKS)")
    i.add_argument("action", choices=["up", "down", "stop", "start", "outputs"])
    i.add_argument("--yes", action="store_true", help="aks: skip Terraform's confirmation")

    bu = sub.add_parser("build", help="build container images (docker + kind load, or ACR)")
    bu.add_argument("--only", nargs="+", choices=["demo-api", "forecaster"])
    bu.add_argument("--tag", help="image tag (default: git commit)")
    bu.add_argument("--with-lstm", action=argparse.BooleanOptionalAction, default=None,
                    help="include PyTorch in the forecaster (default: if an LSTM is trained)")

    pl = sub.add_parser("platform", help="install Prometheus, Grafana and the dashboard "
                        "(locally also KEDA and metrics-server)")
    pl.add_argument("action", choices=["install", "dashboards"])

    for name in ("grafana", "prometheus"):
        f = sub.add_parser(name, help=f"open {name} on localhost via port-forward")
        f.add_argument("--port", type=int)

    la = sub.add_parser("lane", help="deploy a scaler preset to a lane, or show its status")
    la.add_argument("action", choices=["deploy", "status"])
    la.add_argument("lane")
    la.add_argument("--scaler", default="hpa-default", help="preset name from pas.yaml")
    la.add_argument("--window", help="window name (predictive scalers in experiment mode)")
    la.add_argument("--startup-delay", type=int)

    sm = sub.add_parser("smoke", help="few-minute end-to-end check of the control loop")
    sm.add_argument("--lane", default="lane-a")
    sm.add_argument("--scaler", default="naive", help="hpa or predictive preset")
    sm.add_argument("--window", default="WD")
    sm.add_argument("--minutes", type=int, default=10, help="trace minutes to replay")
    sm.add_argument("--step-seconds", type=int, default=10, help="seconds per trace minute")
    sm.add_argument("--work-units", type=int, default=2, help="CPU work per request (light)")

    c = sub.add_parser("capacity", help="step-load test: per-pod RPS within the SLO")
    c.add_argument("--lane", default="lane-a")
    c.add_argument("--steps", type=float, nargs="+", help="RPS levels (default around current)")
    c.add_argument("--step-seconds", type=int, default=120)
    c.add_argument("--settle-seconds", type=int, default=20)

    lg = sub.add_parser("lag", help="measure time from scale-up to a ready pod")
    lg.add_argument("--lane", default="lane-a")
    lg.add_argument("--startup-delay", type=int)
    lg.add_argument("--samples", type=int, default=5)

    e = sub.add_parser("experiment", help="plan and run the comparison experiments")
    e.add_argument("action", choices=["plan", "run", "status"])
    e.add_argument("--matrix", default="experiments/matrix.yaml")
    e.add_argument("--plan", default="experiments/plan.csv")
    e.add_argument("--results", default="experiments/results")
    e.add_argument("--lane", default="lane-a")
    e.add_argument("--shard", help="i/n: run every n-th row starting at i (parallel lanes)")
    e.add_argument("--only", nargs="+", help="run ids")
    e.add_argument("--quiet-seconds", type=int, default=300, help="idle time before each run")
    e.add_argument("--stop-on-error", action="store_true")
    e.add_argument("--dry-run", action="store_true")

    a = sub.add_parser("analyze", help="metrics, comparison table and figures")
    a.add_argument("--results", default="experiments/results")
    a.add_argument("--baseline", default="hpa-tuned")
    a.add_argument("--plots", action="store_true", help="one figure per run")

    dm = sub.add_parser("demo", help="side-by-side reactive vs predictive replay")
    dm.add_argument("action", choices=["start", "stop"])
    dm.add_argument("--window", default="WD")
    dm.add_argument("--reactive", default="hpa-tuned")
    dm.add_argument("--predictive", default="arima")
    dm.add_argument("--reactive-lane", default="lane-a")
    dm.add_argument("--predictive-lane", default="lane-b")
    return p


#: commands that act on the cluster; they first check the kubectl context points at it
CLUSTER_COMMANDS = {"platform", "grafana", "prometheus", "lane", "smoke", "capacity", "lag",
                    "demo"}


def _needs_cluster(cfg: Config, args: argparse.Namespace) -> bool:
    if args.command == "experiment":
        return args.action == "run" and not args.dry_run
    # A local build copies images into the kind nodes; an ACR build needs no kubectl.
    return args.command in CLUSTER_COMMANDS or (args.command == "build" and cfg.cluster.is_local)


HANDLERS = {"data": cmd_data, "train": cmd_train, "backtest": cmd_backtest, "infra": cmd_infra,
            "build": cmd_build, "platform": cmd_platform, "grafana": cmd_forward,
            "prometheus": cmd_forward, "lane": cmd_lane, "smoke": cmd_smoke,
            "capacity": cmd_capacity,
            "lag": cmd_lag, "experiment": cmd_experiment, "analyze": cmd_analyze,
            "demo": cmd_demo}


def main(argv: list[str] | None = None) -> int:
    # Windows consoles and pipes default to a legacy code page; tables contain ± and similar.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower() != "utf-8":
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    try:
        if args.command == "doctor":
            return cmd_doctor(args.config)
        if args.command == "serve":
            cmd_serve(None, args)
            return 0
        cfg = load_config(args.config)
        if _needs_cluster(cfg, args):
            from pas.infra import ensure_context
            ensure_context(cfg)
        HANDLERS[args.command](cfg, args)
        return 0
    except (ConfigError, CommandError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
