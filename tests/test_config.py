# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from lantern_bridge.config import (
    SCHEMA,
    Config,
    ConfigError,
    ConfigSchemaError,
    FolderSource,
    TinfoilSource,
    load,
    load_or_create,
    save,
)

UID = "0190f5a0-7b1c-7c3a-8d2e-4f6a1b2c3d4e"


def test_absent_file_gives_defaults(tmp_path: Path) -> None:
    config = load(tmp_path / "config.toml")
    assert config.remote_rate == 10.0
    assert config.sources == []


def test_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    config = Config(uid=UID, name="Home", sources=[FolderSource("games", Path("/data/games"))])
    save(path, config)
    again = load(path)
    assert again.uid == UID
    assert again.name == "Home"
    assert again.sources == [FolderSource("games", Path("/data/games"))]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_file_is_private(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    save(path, Config(uid=UID))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_load_or_create_keeps_the_uid(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "config.toml"
    first = load_or_create(path)
    assert load_or_create(path).uid == first.uid


def test_hand_written_file_without_uid_gets_one_once(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('name = "Hand"\n', encoding="utf-8")
    first = load_or_create(path)
    assert load_or_create(path).uid == first.uid


def test_unknown_keys_survive_a_rewrite(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        f'schema = 1\nuid = "{UID}"\nfuture = {{ a = 1 }}\n\n'
        '[[sources]]\nid = "g"\nkind = "folder"\npath = "/g"\nweight = 3\n',
        encoding="utf-8",
    )
    config = load(path)
    config.name = "Renamed"
    save(path, config)
    text = path.read_text(encoding="utf-8")
    assert "future" in text
    assert "weight = 3" in text
    assert load(path).name == "Renamed"


def test_newer_schema_is_refused_and_never_rewritten(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    original = f'schema = {SCHEMA + 1}\nuid = "{UID}"\nnew_thing = true\n'
    path.write_text(original, encoding="utf-8")
    with pytest.raises(ConfigSchemaError):
        load(path)
    with pytest.raises(ConfigSchemaError):
        save(path, Config(uid=UID))
    assert path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    "text",
    [
        "not toml [",
        "schema = 0\n",
        "remote_rate = -1\n",
        'name = ""\n',
        'uid = "nope"\n',
        '[[sources]]\nid = "a b"\nkind = "folder"\npath = "/x"\n',
        '[[sources]]\nid = "a"\nkind = "tinfoil"\npath = "/x"\n',
        '[[sources]]\nid = "a"\nkind = "folder"\n',
        '[[sources]]\nid = "a"\nkind = "folder"\npath = "/x"\n[[sources]]\nid = "a"\nkind = "folder"\npath = "/y"\n',
    ],
)
def test_invalid_files_are_refused_with_a_clear_error(tmp_path: Path, text: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError):
        load(path)


def test_public_is_off_by_default_and_kept(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    assert load(path).public is False
    config = Config(uid=UID, public=True)
    save(path, config)
    assert load(path).public is True


def test_public_must_be_a_boolean(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('public = "yes"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load(path)


def test_a_tinfoil_source_round_trips_and_keeps_what_it_does_not_know(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        'schema = 1\nuid = "0c9c2a8e-5d3a-4c7e-8b0a-6f1d2e3c4b5a"\nremote_rate = 2.5\n'
        '[[sources]]\nid = "shop"\nkind = "tinfoil"\nurl = "http://nas.lan:8465/"\nuser = "u"\n'
        'password_env = "SHOP_PASSWORD"\nfuture = "kept"\n',
        encoding="utf-8",
    )
    config = load(path)
    assert config.sources == [TinfoilSource("shop", "http://nas.lan:8465/", "u", "SHOP_PASSWORD")]
    assert config.remote_rate == 2.5
    save(path, config)
    again = path.read_text(encoding="utf-8")
    assert 'future = "kept"' in again and "SHOP_PASSWORD" in again and "hunter2" not in again


@pytest.mark.parametrize(
    "source",
    [
        'kind = "tinfoil"\n',
        'kind = "tinfoil"\nurl = "ftp://x/"\n',
        'kind = "tinfoil"\nurl = "http://u:p@x/"\n',
        'kind = "tinfoil"\nurl = "http://x/"\nuser = "u"\n',
        'kind = "tinfoil"\nurl = "http://x/"\npassword_env = "P"\n',
        'kind = "tinfoil"\nurl = "http://x/"\nuser = ""\npassword_env = "P"\n',
    ],
)
def test_a_tinfoil_source_needs_an_address_and_a_complete_login(tmp_path: Path, source: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(f'[[sources]]\nid = "shop"\n{source}', encoding="utf-8")
    with pytest.raises(ConfigError):
        load(path)


def test_a_shop_may_name_the_variable_that_holds_its_host_verification(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        'schema = 1\nuid = "0c9c2a8e-5d3a-4c7e-8b0a-6f1d2e3c4b5a"\n'
        '[[sources]]\nid = "aero"\nkind = "aerofoil"\nurl = "https://aero.example.org/"\nhauth_env = "AERO_HAUTH"\n',
        encoding="utf-8",
    )
    config = load(path)
    assert config.sources[0].hauth_env == "AERO_HAUTH"  # type: ignore[union-attr]
    save(path, config)
    assert 'hauth_env = "AERO_HAUTH"' in path.read_text(encoding="utf-8")
    path.write_text(path.read_text(encoding="utf-8").replace('"AERO_HAUTH"', '""'), encoding="utf-8")
    with pytest.raises(ConfigError):
        load(path)
