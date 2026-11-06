from pathlib import Path

import pytest
from fastapi import FastAPI

from lantern_bridge import SPEC_DRAFT, __version__
from lantern_bridge.__main__ import main
from lantern_bridge.config import load


def test_version_is_printed(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stop:
        main(["--version"])
    assert stop.value.code == 0
    assert capsys.readouterr().out.strip() == f"lantern-bridge {__version__} (Lantern draft {SPEC_DRAFT})"


def test_no_argument_prints_the_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "serve" in capsys.readouterr().out


def test_serve_refuses_an_invalid_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = tmp_path / "config.toml"
    config.write_text('uid = "not-a-uuid"\n', encoding="utf-8")
    assert main(["serve", "--config", str(config)]) == 1
    assert "uid" in capsys.readouterr().err


def test_serve_keeps_the_uid_between_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runs: list[FastAPI] = []

    def fake_run(app: FastAPI, **_: object) -> None:
        runs.append(app)

    monkeypatch.setattr("uvicorn.run", fake_run)
    config = tmp_path / "config.toml"
    assert main(["serve", "--config", str(config)]) == 0
    first = load(config).uid
    assert main(["serve", "--config", str(config)]) == 0
    assert load(config).uid == first
    assert len(runs) == 2


def test_unknown_option_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stop:
        main(["--nope"])
    assert stop.value.code == 2
    assert "--nope" in capsys.readouterr().err
