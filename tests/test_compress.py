# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""Compressing and decompressing a title's files, with a stand-in for the nsz tool."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from lantern_bridge import compress
from lantern_bridge.compress import WORK, Compressor
from lantern_bridge.hashing import scan_packages
from test_admin import _csrf
from test_writable import BASE, OTHER, UPDATE, _shelf

FAKE = """#!{python}
import json, os, pathlib, sys, time
args = sys.argv[1:]
log = os.environ.get("FAKE_NSZ_LOG")
if log:
    pathlib.Path(log).write_text(json.dumps(args))
mode = os.environ.get("FAKE_NSZ", "")
if mode == "slow":
    time.sleep(30)
out = pathlib.Path(args[args.index("-o") + 1])
src = pathlib.Path(args[-1])
if mode == "fail":
    print(json.dumps({{"type": "summary", "success": False, "errorCount": 1, "errors": ["keys are wrong"]}}))
    sys.exit(1)
new = {{".nsp": ".nsz", ".xci": ".xcz", ".nsz": ".nsp", ".xcz": ".xci"}}[src.suffix]
(out / (src.stem + new)).write_bytes(b"result of the tool")
print(json.dumps({{"type": "summary", "success": True, "errorCount": 0, "errors": []}}))
"""


@pytest.fixture
def tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "bin" / "nsz.py"  # run with this Python, so it works where a shebang does not (Windows)
    path.parent.mkdir()
    path.write_text(FAKE.format(python=sys.executable))
    monkeypatch.setattr(compress, "find_tool", lambda: str(path))
    monkeypatch.setenv("FAKE_NSZ_LOG", str(tmp_path / "args.json"))
    return path


def _ready(tmp_path: Path) -> None:
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "prod.keys").write_text("master_key_00 = 00\n")


def _wait(compressor: Compressor, state: str = "done") -> compress.Job:
    for _ in range(100):
        job = compressor.jobs()[0]
        if job.state == state:
            return job
        time.sleep(0.1)
    raise AssertionError(f"the job is {compressor.jobs()[0].state}, not {state}")


def _convert(client: TestClient, path: str, **extra: str) -> object:
    page = client.get("/admin/library/0100AAAA00000000").text
    return client.post(
        "/admin/library/0100AAAA00000000/convert",
        data={"csrf": _csrf(page), "source": "games", "path": path, **extra},
    )


def _args(tmp_path: Path) -> list[str]:
    return json.loads((tmp_path / "args.json").read_text())  # type: ignore[no-any-return]


def test_the_tool_and_the_keys_are_what_makes_it_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    compressor = Compressor(tmp_path)
    monkeypatch.setattr(compress, "find_tool", lambda: None)
    assert compressor.status() == "no_tool"
    monkeypatch.setattr(compress, "find_tool", lambda: "/usr/bin/nsz")
    assert compressor.status() == "no_keys"
    (tmp_path / "prod.keys").write_text("x")
    assert compressor.status() == "ready"
    with pytest.raises(ValueError, match="cannot be converted"):
        compressor.submit("games", "notes.txt", "0100AAAA00000000")


def test_the_title_page_offers_a_conversion_only_when_it_can_be_done(tmp_path: Path, tool: Path) -> None:
    client, _ = _shelf(tmp_path)
    page = client.get("/admin/library/0100AAAA00000000").text
    assert "Convert…" not in page and "No prod.keys in the configuration folder." in page  # the tool, but no keys
    _ready(tmp_path)
    page = client.get("/admin/library/0100AAAA00000000").text
    assert page.count("Convert…") == 2 and "Compress to NSZ" in page and "compress-note" not in page
    closed, _ = _shelf(tmp_path / "closed", writable=False)
    assert "Convert…" not in closed.get("/admin/library/0100AAAA00000000").text  # a closed folder has no buttons


def test_a_file_is_compressed_next_to_its_original(tmp_path: Path, tool: Path) -> None:
    client, games = _shelf(tmp_path)
    _ready(tmp_path)
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    reply = _convert(client, UPDATE, level="10", mode="block")
    assert reply.status_code == 303 and reply.headers["location"] == "/admin/tasks?m=converting"  # type: ignore[attr-defined]
    job = _wait(runtime.compressor)
    new = games / UPDATE.replace(".nsp", ".nsz")
    assert new.read_bytes() == b"result of the tool" and (games / UPDATE).exists()  # the original stays
    assert not list(games.glob(f"{WORK}*")) and job.size_out == len(b"result of the tool")
    args = _args(tmp_path)
    assert "-C" in args and "-B" in args and args[args.index("-l") + 1] == "10" and "-Q" not in args
    assert args[args.index("--keys") + 1] == str(tmp_path / "state") and args[-1] == str(games / UPDATE)
    assert [e.detail for e in runtime.history.events() if e.kind == "converted"] == ["nsp→nsz"]
    runtime.cycle(batch=50)  # the next pass lists the new file beside the old one
    formats = {f.format for f in runtime.browser.catalog().titles["0100AAAA00000000"].files if f.type == "update"}
    assert formats == {"nsp", "nsz"}
    page = client.get("/admin/tasks").text
    assert "done" in page and UPDATE in page and "Compress to NSZ" in page


def test_the_original_goes_only_after_the_result_is_there_and_checked(tmp_path: Path, tool: Path) -> None:
    client, games = _shelf(tmp_path)
    _ready(tmp_path)
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    _convert(client, UPDATE, delete="on")
    _wait(runtime.compressor)
    assert not (games / UPDATE).exists() and (games / UPDATE.replace(".nsp", ".nsz")).exists()
    assert "-Q" in _args(tmp_path)  # the tool verified the NCAs first
    assert UPDATE not in {f.name for f in runtime.browser.catalog().titles["0100AAAA00000000"].files}  # dropped at once


def test_a_failed_conversion_leaves_the_folder_as_it_was(
    tmp_path: Path, tool: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, games = _shelf(tmp_path)
    _ready(tmp_path)
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    monkeypatch.setenv("FAKE_NSZ", "fail")
    _convert(client, UPDATE, delete="on")
    job = _wait(runtime.compressor, "failed")
    assert job.message == "keys are wrong" and (games / UPDATE).exists()
    assert sorted(p.name for p in games.iterdir()) == sorted([BASE, UPDATE, OTHER])  # nothing new, no work folder
    monkeypatch.delenv("FAKE_NSZ")
    (games / UPDATE.replace(".nsp", ".nsz")).write_bytes(b"already")
    _convert(client, UPDATE)
    assert _wait(runtime.compressor, "failed").message.startswith("A file of that name is already there")
    assert (games / UPDATE.replace(".nsp", ".nsz")).read_bytes() == b"already"  # never replaced


def test_a_conversion_can_be_cancelled_and_leaves_nothing_behind(
    tmp_path: Path, tool: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, games = _shelf(tmp_path)
    _ready(tmp_path)
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    monkeypatch.setenv("FAKE_NSZ", "slow")
    _convert(client, UPDATE)
    for _ in range(50):
        if runtime.compressor.jobs()[0].state == "running":
            break
        time.sleep(0.1)
    csrf = _csrf(client.get("/admin/tasks").text)
    assert client.post(f"/admin/tasks/{runtime.compressor.jobs()[0].id}/cancel", data={"csrf": csrf}).status_code == 303
    _wait(runtime.compressor, "cancelled")
    assert not list(games.glob(f"{WORK}*")) and (games / UPDATE).exists()
    assert client.post("/admin/tasks/99/cancel", data={"csrf": csrf}).status_code == 404


def test_a_decompression_goes_the_other_way(tmp_path: Path, tool: Path) -> None:
    client, games = _shelf(tmp_path)
    _ready(tmp_path)
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    packed = "Aurora Quest [0100AAAA00000000][v0].nsz"
    (games / packed).write_bytes(b"packed")
    (games / BASE).unlink()
    runtime.cycle(batch=50)
    _convert(client, packed)
    _wait(runtime.compressor)
    assert "-D" in _args(tmp_path) and "-l" not in _args(tmp_path)  # no level for a decompression
    assert (games / BASE).read_bytes() == b"result of the tool"


def test_nothing_is_converted_in_a_closed_folder_or_without_the_tool(tmp_path: Path, tool: Path) -> None:
    client, games = _shelf(tmp_path / "closed", writable=False)
    _ready(tmp_path / "closed")
    assert _convert(client, UPDATE).status_code == 403  # type: ignore[attr-defined]
    open_, _ = _shelf(tmp_path / "open")
    assert _convert(open_, UPDATE).status_code == 409  # type: ignore[attr-defined]  # no keys yet
    _ready(tmp_path / "open")
    assert _convert(open_, "../x.nsp").status_code == 403  # type: ignore[attr-defined]
    assert _convert(open_, OTHER).status_code == 403  # type: ignore[attr-defined]  # not this title's
    assert not list(games.glob("*.nsz"))


def test_a_half_written_result_is_never_taken_for_a_package(tmp_path: Path) -> None:
    work = tmp_path / f"{WORK}3"
    work.mkdir()
    (work / "Aurora Quest [0100AAAA00000000][v0].nsz").write_bytes(b"half")
    (tmp_path / "Zebra Run [0100BBBB00000000][v0].nsp").write_bytes(b"whole")
    assert [e.path for e in scan_packages(tmp_path)] == ["Zebra Run [0100BBBB00000000][v0].nsp"]


def test_a_job_goes_on_through_a_settings_reload(tmp_path: Path, tool: Path) -> None:
    client, _ = _shelf(tmp_path)
    shell = client.app
    before = shell.state.compressor  # type: ignore[attr-defined]
    config = shell.state.runtime.config  # type: ignore[attr-defined]
    from lantern_bridge.config import save

    config.name = "Renamed"
    save(tmp_path / "state" / "config.toml", config)
    assert asyncio.run(shell.reload()) is None  # type: ignore[attr-defined]
    assert shell.state.compressor is before and shell.state.runtime.compressor is before  # type: ignore[attr-defined]


def test_the_tasks_page_is_for_the_administrator_and_in_french(tmp_path: Path, tool: Path) -> None:
    client, _ = _shelf(tmp_path)
    _ready(tmp_path)
    assert "No task yet" in client.get("/admin/tasks").text
    client.post("/admin/lang", data={"lang": "fr", "next": "/admin/tasks"})
    page = client.get("/admin/tasks").text
    assert "Tâches" in page and "Aucune tâche pour l" in page
    assert 'href="/admin/tasks"' in client.get("/admin/").text  # in the Manage menu
