# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""The specification's conformance suite, run against the bridge as a user runs it.

The bridge is started on a folder of fictional packages; a device is paired
with a token bound to its key, so that the suite can go as far as the
levels the bridge offers. The suite lives in the SDK's own submodule
(``git submodule update --init --recursive``); without it, this test skips.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest
from lantern.server import SqliteAccounts
from lantern.server.accounts import SCOPES
from lantern.signatures import DeviceKey

pytestmark = pytest.mark.conformance

ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "sdk" / "spec" / "conformance" / "run.py"
TOKEN = "et_bridge-0123456789abcdef0123456789abcdef"
PACKAGES = {
    "Frost Garden [0100ABCD00010000][v0].nsp": b"frost" * 400,
    "Frost Garden [0100ABCD00010800][v65536].nsp": b"update" * 300,
    "Frost Garden - Winter [0100ABCD00011001][v0].nsp": b"winter" * 200,
    "Moss Walk [0100ABCD00020000][v0].xci": b"moss" * 500,
}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2):
            return True
    except (urllib.error.URLError, OSError):
        return False


@pytest.fixture
def bridge(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    games = tmp_path / "games"
    games.mkdir()
    for name, data in PACKAGES.items():
        (games / name).write_bytes(data)
    config = tmp_path / "config.toml"
    config.write_text(
        f'name = "Conformance shelf"\n\n[[sources]]\nid = "games"\nkind = "folder"\npath = "{games}"\n',
        encoding="utf-8",
    )
    key = DeviceKey.generate()
    (tmp_path / "device.key").write_text(key.seed_b64() + "\n", encoding="ascii")
    with SqliteAccounts(tmp_path / "accounts.sqlite3") as accounts:
        accounts.create_user("mira", SCOPES)
        accounts.add_token(TOKEN, "mira", "Mira's console", SCOPES, key=key.public)
    port = _free_port()
    server = subprocess.Popen(
        [sys.executable, "-m", "lantern_bridge", "serve", "--config", str(config), "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    url = f"http://127.0.0.1:{port}/"
    try:
        deadline = time.monotonic() + 30
        while not _get(url + ".well-known/lantern"):
            assert server.poll() is None, server.stderr.read().decode() if server.stderr else ""
            assert time.monotonic() < deadline, "the bridge did not start"
            time.sleep(0.2)
        # The library lists a file once it is hashed: wait for the four
        while time.monotonic() < deadline:
            status = subprocess.run(
                [sys.executable, "-m", "lantern_bridge", "status", "--config", str(config)],
                capture_output=True,
                text=True,
                check=False,
            ).stdout
            if f"{len(PACKAGES)} hashed\t0 pending" in status:
                break
            time.sleep(0.5)
        else:
            pytest.fail("the packages were not hashed in time")
        time.sleep(1)  # the library follows the index on the same pass
        yield url, tmp_path
    finally:
        server.terminate()
        server.wait(timeout=10)
        if server.stderr:
            server.stderr.close()


@pytest.mark.skipif(not RUNNER.exists(), reason="the SDK's own submodules are not checked out")
def test_the_bridge_reaches_lantern_core(bridge: tuple[str, Path]) -> None:
    url, folder = bridge
    result = subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            url,
            "--token",
            TOKEN,
            "--key",
            str(folder / "device.key"),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    print(result.stdout)
    assert result.returncode == 0, "\n".join(line for line in result.stdout.splitlines() if line.startswith("FAIL"))
    reached = next(line for line in result.stdout.splitlines() if line.startswith("Levels reached"))
    for level in ("Lantern Core", "+titles", "+homebrew"):
        assert level in reached, reached
