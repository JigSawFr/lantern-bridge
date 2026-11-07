# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""Homebrew from GitHub releases, against a stand-in for GitHub."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from lantern.server.library import MemoryLibrary

from lantern_bridge.config import HomebrewRepo
from lantern_bridge.releases import (
    MAX_ASSET,
    ReleaseError,
    Releases,
    clean_manifest,
    homebrew_id,
    parse_repo,
    pick_asset,
)

NRO = b"NRO0" + b"\x00" * 60 + b"homebrew bytes"


def _zip(folder: str = "switch/com.github.someone.tool") -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(f"{folder}/tool.nro", b"nro")
        z.writestr(f"{folder}/readme.txt", b"hi")
    return out.getvalue()


class Fake:
    """GitHub, as far as the bridge asks it: one repository, its latest release, its assets."""

    def __init__(self) -> None:
        self.tag = "v1.0.0"
        self.etag = '"one"'
        self.assets: dict[str, bytes] = {"tool.nro": NRO}
        self.manifest: dict[str, Any] | None = None
        self.calls: list[str] = []
        self.status = 200
        self.headers: dict[str, str] = {}
        self.about: dict[str, Any] = {
            "description": "A tool for the console",
            "license": {"spdx_id": "MIT"},
            "topics": ["switch", "tool"],
            "owner": {"login": "someone"},
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(url)
        if url.endswith("/releases/latest"):
            if self.status != 200:
                return httpx.Response(self.status, headers=self.headers, json={"message": "no"})
            if request.headers.get("if-none-match") == self.etag:
                return httpx.Response(304)
            assets = dict(self.assets)
            if self.manifest is not None:
                assets["lantern.json"] = json.dumps(self.manifest).encode()
            body = {
                "tag_name": self.tag,
                "draft": False,
                "published_at": "2026-11-01T10:00:00Z",
                "body": "Release notes",
                "assets": [
                    {
                        "name": n,
                        "size": len(d),
                        "state": "uploaded",
                        "browser_download_url": f"https://github.com/someone/tool/releases/download/{self.tag}/{n}",
                    }
                    for n, d in assets.items()
                ],
            }
            return httpx.Response(200, json=body, headers={"etag": self.etag})
        if url.endswith("/repos/someone/tool"):
            return httpx.Response(200, json=self.about)
        if "/releases/download/" in url:
            name = url.rsplit("/", 1)[1]
            data = self.assets.get(name)
            if data is None and name == "lantern.json" and self.manifest is not None:
                data = json.dumps(self.manifest).encode()
            return httpx.Response(200 if data is not None else 404, content=data or b"")
        return httpx.Response(404)


def _releases(tmp_path: Path, fake: Fake, *repos: HomebrewRepo, library: MemoryLibrary | None = None) -> Releases:
    client = httpx.Client(transport=httpx.MockTransport(fake.handler), follow_redirects=False)
    return Releases(tmp_path, library or MemoryLibrary(), list(repos) or [HomebrewRepo("someone/tool")], client=client)


def test_a_repository_is_given_as_a_name_or_a_github_address() -> None:
    assert parse_repo("someone/tool") == "someone/tool"
    assert parse_repo("https://github.com/someone/tool") == "someone/tool"
    assert parse_repo("https://github.com/someone/tool/releases/tag/v1") == "someone/tool"
    assert parse_repo("https://github.com/someone/tool.git") == "someone/tool"
    for bad in ("", "someone", "http://github.com/a/b", "https://gitlab.com/a/b", "a/b/c/../..", "../x/y", "a b/c"):
        with pytest.raises(ReleaseError):
            parse_repo(bad)


def test_the_id_is_reverse_dns_and_safe() -> None:
    assert homebrew_id("SomeOne/My.Tool") == "com.github.someone.my-tool"
    assert homebrew_id("a/_") == "com.github.a.x"


def test_the_asset_is_the_one_nro_else_the_one_zip_else_the_one_named() -> None:
    a = [{"name": "tool.nro"}, {"name": "src.zip"}]
    assert pick_asset(a, None)["name"] == "tool.nro"
    assert pick_asset([{"name": "pack.zip"}], None)["name"] == "pack.zip"
    assert pick_asset(a, "*.zip")["name"] == "src.zip"
    with pytest.raises(ReleaseError):
        pick_asset([{"name": "a.nro"}, {"name": "b.nro"}], None)  # which one?
    with pytest.raises(ReleaseError):
        pick_asset([{"name": "notes.txt"}], None)
    with pytest.raises(ReleaseError):
        pick_asset(a, "*.nsp")


def test_the_manifest_keeps_known_fields_of_their_type_and_refuses_the_rest() -> None:
    ok = clean_manifest({"id": "com.example.tool", "name": " Tool ", "kind": "tool", "tags": ["a"], "unknown": 1})
    assert ok == {"id": "com.example.tool", "name": "Tool", "kind": "tool", "tags": ["a"]}
    bad: list[object] = [{"id": "Not An Id"}, {"kind": "spaceship"}, {"tags": "a"}, {"name": 3}, []]
    for one in bad:
        with pytest.raises(ReleaseError):
            clean_manifest(one)


def test_a_release_is_downloaded_once_described_and_served(tmp_path: Path) -> None:
    fake, library = Fake(), MemoryLibrary()
    releases = _releases(tmp_path, fake, library=library)
    watch = releases.check(HomebrewRepo("someone/tool"))
    assert watch.error == "" and watch.tag == "v1.0.0" and watch.asset == "tool.nro"
    (entry,) = library.homebrew([])
    assert entry.id == "com.github.someone.tool" and entry.version == "1.0.0" and entry.kind == "app"
    assert entry.name == "tool" and entry.author == "someone" and entry.license == "MIT"
    assert entry.summary == "A tool for the console" and entry.tags == ["switch", "tool"]
    assert entry.source == "https://github.com/someone/tool" and entry.build > 0
    assert entry.package.format == "nro" and entry.package.size == len(NRO)
    assert Path(watch.file).read_bytes() == NRO  # kept under the state folder, by its hash


def test_an_unchanged_release_costs_one_conditional_read_and_no_download(tmp_path: Path) -> None:
    fake = Fake()
    releases = _releases(tmp_path, fake)
    repo = HomebrewRepo("someone/tool")
    releases.check(repo)
    before = len(fake.calls)
    releases.check(repo)
    assert fake.calls[before:] == ["https://api.github.com/repos/someone/tool/releases/latest"]


def test_a_new_release_replaces_the_old_one_and_its_file(tmp_path: Path) -> None:
    fake, library = Fake(), MemoryLibrary()
    releases = _releases(tmp_path, fake, library=library)
    repo = HomebrewRepo("someone/tool")
    first = releases.check(repo).file
    fake.tag, fake.etag, fake.assets = "v1.1.0", '"two"', {"tool.nro": NRO + b"more"}
    watch = releases.check(repo)
    assert watch.tag == "v1.1.0" and library.homebrew([])[0].version == "1.1.0"
    assert not Path(first).exists() and Path(watch.file).read_bytes() == NRO + b"more"


def test_what_was_served_is_served_again_after_a_restart_without_a_download(tmp_path: Path) -> None:
    fake = Fake()
    _releases(tmp_path, fake).check(HomebrewRepo("someone/tool"))
    fake.calls.clear()
    library = MemoryLibrary()
    again = _releases(tmp_path, fake, library=library)
    assert [e.id for e in library.homebrew([])] == ["com.github.someone.tool"] and fake.calls == []
    assert again.status()[0]["served"] is True


def test_a_state_of_a_newer_schema_is_not_read_and_not_written_over(tmp_path: Path) -> None:
    (tmp_path / "homebrew").mkdir()
    state = tmp_path / "homebrew" / "state.json"
    state.write_text(json.dumps({"schema": 99, "repos": {"someone/tool": {"tag": "v9"}}, "future": True}))
    fake, library = Fake(), MemoryLibrary()
    releases = _releases(tmp_path, fake, library=library)
    assert releases.watches == {}
    releases.check(HomebrewRepo("someone/tool"))
    assert json.loads(state.read_text())["schema"] == 99  # left as the newer bridge wrote it


def test_the_manifest_of_the_project_names_it(tmp_path: Path) -> None:
    fake, library = Fake(), MemoryLibrary()
    fake.manifest = {"id": "org.example.cooltool", "name": "Cool Tool", "kind": "emulator", "summary": "Does it"}
    _releases(tmp_path, fake, library=library).check(HomebrewRepo("someone/tool"))
    (entry,) = library.homebrew([])
    assert (entry.id, entry.name, entry.kind, entry.summary) == (
        "org.example.cooltool",
        "Cool Tool",
        "emulator",
        "Does it",
    )


def test_a_bad_manifest_stops_that_repository_with_a_sentence(tmp_path: Path) -> None:
    fake = Fake()
    fake.manifest = {"kind": "spaceship"}
    watch = _releases(tmp_path, fake).check(HomebrewRepo("someone/tool"))
    assert "kind is one of" in watch.error and watch.entry is None


def test_a_zip_carries_its_own_manifest_of_files(tmp_path: Path) -> None:
    fake, library = Fake(), MemoryLibrary()
    fake.assets = {"pack.zip": _zip()}
    _releases(tmp_path, fake, library=library).check(HomebrewRepo("someone/tool", kind="tool"))
    (entry,) = library.homebrew([])
    assert entry.kind == "tool" and entry.package.format == "zip"
    paths = sorted(m.path for m in entry.package.manifest or [])
    assert paths == ["/switch/com.github.someone.tool/readme.txt", "/switch/com.github.someone.tool/tool.nro"]


def test_a_zip_that_writes_outside_the_roots_of_the_platform_is_refused_and_said(tmp_path: Path) -> None:
    fake, library = Fake(), MemoryLibrary()
    fake.assets = {"pack.zip": _zip("switch/tool")}  # the specification keeps a package to /switch/<id>/ and the like
    watch = _releases(tmp_path, fake, library=library).check(HomebrewRepo("someone/tool"))
    assert "outside" in watch.error and "roots" in watch.error and library.homebrew([]) == []


def test_what_goes_wrong_is_said_and_the_next_read_is_later(tmp_path: Path) -> None:
    fake = Fake()
    releases = _releases(tmp_path, fake)
    repo = HomebrewRepo("someone/tool")
    fake.status = 404
    assert "No release was found" in releases.check(repo).error
    fake.status, fake.headers = 403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "0"}
    assert "rate limit" in releases.check(repo).error
    fake.status = 200
    assert releases.check(repo).error == ""


def test_an_asset_off_github_or_too_big_is_refused(tmp_path: Path) -> None:
    fake = Fake()
    releases = _releases(tmp_path, fake)
    with pytest.raises(ReleaseError, match="not on GitHub"):
        releases._download("https://evil.example.org/tool.nro", 100)
    with pytest.raises(ReleaseError, match="not on GitHub"):
        releases._download("http://github.com/a/b/releases/download/v1/x", 100)
    with pytest.raises(ReleaseError, match="not on GitHub"):
        releases._download("https://notgithub.com/x", 100)
    fake.assets = {"tool.nro": b"x" * 500}
    with pytest.raises(ReleaseError, match="bigger"):
        releases._download("https://github.com/someone/tool/releases/download/v1.0.0/tool.nro", 100)
    assert MAX_ASSET >= 1 << 20


def test_the_token_goes_to_the_api_and_only_there(tmp_path: Path) -> None:
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("authorization")))
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    releases = Releases(tmp_path, MemoryLibrary(), [HomebrewRepo("someone/tool")], token="secret", client=client)
    releases.check(HomebrewRepo("someone/tool"))
    assert seen and all(auth == "Bearer secret" for url, auth in seen if url.startswith("https://api.github.com"))
