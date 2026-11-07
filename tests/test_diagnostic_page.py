# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""The diagnostic page: the bridge's state as text for a support request, with nothing private unless asked."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from fastapi.testclient import TestClient
from lantern.uid import new_uid

from lantern_bridge.app import make_app
from lantern_bridge.config import Config, FolderSource, Mqtt, OwnfoilSource
from test_admin import _admin

HOOK = "https://hooks.example.org/very-secret-path-123"
NTFY = "https://ntfy.example.org/topic-secret-456"


def _shelf(tmp_path: Path) -> TestClient:
    games = tmp_path / "private-folder" / "games"
    games.mkdir(parents=True)
    (games / "Aurora Quest [0100AAAA00000000][v0].nsp").write_bytes(b"base")
    config = Config(
        uid=new_uid(),
        name="Test shelf",
        atlas=False,
        update_check=False,
        webhook=HOOK,
        notify=NTFY,
        mqtt=Mqtt(host="broker.example.org", user="me", password_env="MQTT_PW"),
        sources=[
            FolderSource("games", games),
            OwnfoilSource("shop", "http://user:hunter2@192.168.1.50:8465/"),
        ],
    )
    client = _admin(
        TestClient(make_app(config, tmp_path / "state", run=False), client=("127.0.0.1", 50000), follow_redirects=False)
    )
    client.app.state.runtime.cycle(batch=5)  # type: ignore[attr-defined]
    return client


def test_the_report_says_what_a_support_request_needs(tmp_path: Path) -> None:
    text = _shelf(tmp_path).app.state.admin_pages.report()  # type: ignore[attr-defined]
    assert text.startswith("lantern-bridge ") and "Python " in text and "Lantern draft" in text
    assert "Sources (2):" in text and "- games: folder" in text and "- shop: ownfoil" in text
    assert "1 of 1 files hashed" in text or "files hashed" in text
    assert "Health:" in text and "- ok:" in text and "Settings: applied" in text
    assert "MQTT: on" in text and "webhook: set" in text and "notifications: set" in text


def test_the_short_report_holds_nothing_private(tmp_path: Path) -> None:
    client = _shelf(tmp_path)
    logging.getLogger("lantern_bridge.test").warning("cannot read /home/ana/secret games/File.nsp")
    for text in (
        client.app.state.admin_pages.report(),  # type: ignore[attr-defined]
        client.get("/admin/diagnostic").text,
    ):
        for private in (
            "hunter2",
            "192.168.1.50",
            "private-folder",
            "very-secret-path",
            "topic-secret",
            "broker.example.org",
        ):
            assert private not in text, private
        assert "/home/ana" not in text
    assert re.search(r"Warnings and errors since the start: [1-9]", client.app.state.admin_pages.report())  # type: ignore[attr-defined]


def test_the_full_report_has_the_details_but_never_a_password_or_a_secret_path(tmp_path: Path) -> None:
    client = _shelf(tmp_path)
    logging.getLogger("lantern_bridge.test").warning("cannot read /home/ana/secret games/File.nsp")
    text = client.app.state.admin_pages.report(full=True)  # type: ignore[attr-defined]
    assert "private-folder" in text
    assert re.search(r"- shop: ownfoil, .* · http://192\.168\.1\.50:8465/\n", text)  # the address, without its login
    assert re.search(r"- MQTT: on \(broker\.example\.org:1883\)\n", text)
    assert "/home/ana/secret games/File.nsp" in text  # the messages of the log, when asked for
    for never in ("hunter2", "very-secret-path", "topic-secret"):
        assert never not in text, never  # a login in an address, the webhook's and ntfy's addresses: never


def test_the_page_copies_toggles_and_is_for_the_administrator(tmp_path: Path) -> None:
    client = _shelf(tmp_path)
    page = client.get("/admin/diagnostic").text
    assert 'data-copy-text="lantern-bridge' in page and "/admin/diagnostic?full=1" in page
    assert "/admin/diagnostic.json" in page and "issues/new" in page
    assert 'href="/admin/diagnostic"' in client.get("/admin/diagnostic?full=1").text  # and back to the short one
    assert 'href="/admin/diagnostic"' in client.get("/admin/").text  # it is in the Manage menu
    client.post("/admin/lang", data={"lang": "fr", "next": "/admin/diagnostic"})
    assert "état de ce pont en texte" in client.get("/admin/diagnostic").text
