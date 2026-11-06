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


def _config_with_source(tmp_path: Path) -> Path:
    config = tmp_path / "config.toml"
    config.write_text(
        '[[sources]]\nid = "games"\nkind = "folder"\npath = "/nowhere"\n',
        encoding="utf-8",
    )
    return config


def test_status_needs_an_index(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["status", "--config", str(_config_with_source(tmp_path))]) == 1
    assert "start the server first" in capsys.readouterr().err


def test_status_and_rehash_follow_the_index(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from lantern_bridge.index import Entry, Index

    config = _config_with_source(tmp_path)
    with Index(tmp_path / "index.sqlite3") as index:
        index.sync("games", [Entry("a", 1, 1), Entry("b", 1, 1)])
        [first, *_] = index.pending()
        index.set_hash(first, "a" * 64)
    assert main(["status", "--config", str(config)]) == 0
    assert "1 hashed\t1 pending" in capsys.readouterr().out
    assert main(["rehash", "games", "--config", str(config)]) == 0
    assert "1 hashes forgotten" in capsys.readouterr().out
    assert main(["status", "--config", str(config)]) == 0
    assert "0 hashed\t2 pending" in capsys.readouterr().out


def test_rehash_refuses_an_unknown_source(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from lantern_bridge.index import Index

    config = _config_with_source(tmp_path)
    Index(tmp_path / "index.sqlite3").close()
    assert main(["rehash", "nope", "--config", str(config)]) == 1
    assert "no source 'nope'" in capsys.readouterr().err


def test_invite_prints_a_code_the_accounts_know(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from lantern.server import SqliteAccounts

    config = tmp_path / "config.toml"
    assert main(["invite", "--user", "noor", "--devices", "2", "--config", str(config)]) == 0
    out = capsys.readouterr().out
    with SqliteAccounts(tmp_path / "accounts.sqlite3") as accounts:
        [invitation] = accounts.invitations()
        assert invitation.code in out
        assert invitation.user == "noor"
        assert invitation.devices == 2
        assert "admin" not in invitation.scopes


def test_invite_refuses_nonsense(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["invite", "--devices", "0", "--config", str(tmp_path / "config.toml")]) == 1
    assert "at least 1" in capsys.readouterr().err
