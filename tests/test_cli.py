"""End-to-end through the CLI on the synthetic trace: build data, train, backtest."""

import pytest

from pas.cli import build_parser, main


def test_help_lists_every_command(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--help"])
    out = capsys.readouterr().out
    for command in ("doctor", "data", "train", "backtest", "experiment", "analyze", "demo"):
        assert command in out


def test_offline_pipeline(project, fake_trace, capsys):
    cfg = str(project.root / "pas.yaml")
    assert main(["--config", cfg, "data", "build"]) == 0
    assert (project.windows_dir / "W1.json").exists()
    assert main(["--config", cfg, "train", "naive", "arima"]) == 0
    assert (project.models_dir / "arima.pkl").exists()
    assert main(["--config", cfg, "backtest", "naive", "arima", "--split", "val"]) == 0
    out = capsys.readouterr().out
    assert "| naive |" in out and "| arima |" in out
    assert (project.reports_dir / "backtest" / "summary-val.md").exists()


def test_errors_are_reported_without_traceback(project, capsys):
    cfg = str(project.root / "pas.yaml")
    assert main(["--config", cfg, "train", "naive"]) == 1
    assert "pas data build" in capsys.readouterr().err
