# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""The page at the root: for a browser, not for a console's app."""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

from lantern_bridge.admin.i18n import FR
from test_admin import _client

BROWSER = {"accept": "text/html,application/xhtml+xml,*/*;q=0.8"}


def test_a_browser_gets_a_page(tmp_path: Path) -> None:
    client = _client(tmp_path, sources=True)
    response = client.get("/", headers=BROWSER)
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/html")
    assert "This is a Lantern server." in response.text and "Running" in response.text
    assert 'href="/admin/"' in response.text  # the test client is on the local network


def test_the_page_says_nothing_a_stranger_could_use(tmp_path: Path) -> None:
    text = _client(tmp_path, sources=True).get("/", headers=BROWSER).text
    assert "version" not in text.lower() and "/admin/static" not in text and "<script" not in text


def test_the_style_is_allowed_by_its_hash_and_nothing_else(tmp_path: Path) -> None:
    response = _client(tmp_path, sources=True).get("/", headers=BROWSER)
    style = re.search(r"<style>(.*?)</style>", response.text, re.S)
    assert style is not None
    digest = base64.b64encode(hashlib.sha256(style.group(1).encode()).digest()).decode()
    policy = response.headers["content-security-policy"]
    assert f"style-src 'sha256-{digest}'" in policy and "script-src" not in policy and "default-src 'none'" in policy


def test_another_client_keeps_the_plain_404(tmp_path: Path) -> None:
    client = _client(tmp_path, sources=True)
    for headers in ({}, {"accept": "application/json"}, {"accept": "*/*"}):
        assert client.get("/", headers=headers).status_code == 404


def test_the_page_speaks_french_to_a_french_browser(tmp_path: Path) -> None:
    text = _client(tmp_path, sources=True).get("/", headers={**BROWSER, "accept-language": "fr-FR,fr;q=0.9"}).text
    assert "Ceci est un serveur Lantern." in text and 'lang="fr"' in text
    for line in ("This is a Lantern server.", "Running", "Open the administration"):
        assert line in FR
