# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""The page of the GitHub repositories served as homebrew: add by link, remove, read again, settings kept."""

from __future__ import annotations

from pathlib import Path

from lantern_bridge.config import HomebrewRepo, load, parse_text
from test_admin import _admin, _client, _csrf
from test_observer import _with_observer


def _add(client, csrf: str, repo: str, **extra: str):  # type: ignore[no-untyped-def]
    return client.post("/admin/homebrew/add", data={"csrf": csrf, "back": "homebrew", "repo": repo, **extra})


def test_a_repository_is_added_by_its_link_and_kept_in_the_settings(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path, sources=True))
    page = client.get("/admin/homebrew")
    assert page.status_code == 200 and "No repository yet" in page.text
    csrf = _csrf(page.text)
    reply = _add(client, csrf, "https://github.com/someone/tool", asset="*.nro", kind="tool", prerelease="on")
    assert reply.status_code == 303 and reply.headers["location"] == "/admin/homebrew?m=homebrew_added"
    saved = load(tmp_path / "state" / "config.toml").homebrew
    assert saved == [HomebrewRepo("someone/tool", "*.nro", "tool", True)]
    listed = client.get("/admin/homebrew").text
    assert "someone/tool" in listed and "https://github.com/someone/tool" in listed


def test_a_bad_or_double_repository_is_refused_on_the_same_page(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path, sources=True))
    csrf = _csrf(client.get("/admin/homebrew").text)
    bad = _add(client, csrf, "https://gitlab.com/a/b")
    assert (
        bad.status_code == 400
        and "Only a repository of github.com can be added." in bad.text
        and "Add a repository" in bad.text
    )
    assert _add(client, csrf, "someone/tool").status_code == 303
    twice = _add(client, csrf, "Someone/Tool")
    assert twice.status_code == 400 and "That repository is already there." in twice.text
    assert _add(client, csrf, "a/b", kind="spaceship").status_code == 400


def test_a_repository_is_removed_and_read_again_on_demand(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path, sources=True))
    csrf = _csrf(client.get("/admin/homebrew").text)
    _add(client, csrf, "someone/tool")
    again = client.post("/admin/homebrew/check", data={"csrf": csrf, "repo": "someone/tool"})
    assert again.status_code == 303 and again.headers["location"].endswith("?m=homebrew_checking")
    gone = client.post("/admin/homebrew/remove", data={"csrf": csrf, "back": "homebrew", "repo": "someone/tool"})
    assert gone.status_code == 303 and load(tmp_path / "state" / "config.toml").homebrew == []
    missing = client.post("/admin/homebrew/remove", data={"csrf": csrf, "back": "homebrew", "repo": "someone/tool"})
    assert missing.status_code == 400 and "That repository is not there." in missing.text


def test_the_journal_says_the_repository_changed_without_a_value(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path, sources=True))
    _add(client, _csrf(client.get("/admin/homebrew").text), "someone/tool")
    journal = client.get("/admin/events?kind=settings").text
    assert "homebrew" in journal


def test_the_observer_cannot_open_the_page_or_change_it(tmp_path: Path) -> None:
    boss, eye = _with_observer(tmp_path)
    assert eye.get("/admin/homebrew").status_code == 403
    assert eye.post("/admin/homebrew/add", data={"csrf": "x", "repo": "a/b"}).status_code == 403
    assert "/admin/homebrew" not in eye.get("/admin/").text
    assert boss.get("/admin/homebrew").status_code == 200


def test_the_settings_file_keeps_the_repositories_and_refuses_nonsense() -> None:
    head = 'uid = "6df832d0-7d7a-4b5e-9f2a-d8f775ed93f1"\nschema = 1\n'
    config = parse_text(head + 'github_token_env = "GH"\n[[homebrew]]\nrepo = "a/b"\nkind = "tool"\n')
    assert config.homebrew == [HomebrewRepo("a/b", None, "tool", False)] and config.github_token_env == "GH"
    again = parse_text(config.to_toml())
    assert again.homebrew == config.homebrew
    for bad in (
        '[[homebrew]]\nrepo = "https://gitlab.com/a/b"\n',
        '[[homebrew]]\nrepo = "a/b"\nkind = "boat"\n',
        '[[homebrew]]\nrepo = "a/b"\n[[homebrew]]\nrepo = "A/B"\n',
        '[[homebrew]]\nrepo = "a/b"\nprerelease = "yes"\n',
        'github_token_env = ""\n',
        "homebrew = 3\n",
    ):
        try:
            parse_text(head + bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_a_restart_serves_what_was_kept_without_reading_github(tmp_path: Path) -> None:
    from lantern.server.library import MemoryLibrary

    from lantern_bridge.runtime import Runtime
    from test_releases import Fake, _releases

    state = tmp_path / "state"
    repo = HomebrewRepo("someone/tool")
    _releases(state, Fake()).check(repo)
    config = parse_text(
        f'uid = "6df832d0-7d7a-4b5e-9f2a-d8f775ed93f1"\nschema = 1\n[[homebrew]]\nrepo = "{repo.repo}"\n'
    )
    library = MemoryLibrary()
    runtime = Runtime(config, state, library)
    try:
        assert [e.id for e in library.homebrew([])] == ["com.github.someone.tool"]
        assert runtime.releases is not None and runtime.releases.status()[0]["served"] is True
    finally:
        runtime.stop()
