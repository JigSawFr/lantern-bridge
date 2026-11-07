# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""The pages can be installed as an app: a manifest, icons, a service worker that keeps nothing."""

from __future__ import annotations

import json
from pathlib import Path

from test_admin import _admin, _client


def test_the_manifest_names_the_app_its_scope_and_icons(tmp_path: Path) -> None:
    client = _client(tmp_path, sources=True)
    reply = client.get("/admin/static/manifest.webmanifest")
    assert reply.status_code == 200 and reply.headers["content-type"].startswith("application/manifest+json")
    manifest = json.loads(reply.text)
    assert manifest["scope"] == "/admin/" and manifest["start_url"].startswith("/admin/")
    assert manifest["display"] == "standalone"
    for icon in manifest["icons"]:
        assert client.get(icon["src"]).status_code == 200  # an icon the manifest names is served
    sizes = {i["sizes"] for i in manifest["icons"]}
    assert {"192x192", "512x512"} <= sizes


def test_the_service_worker_is_served_at_the_scope_and_is_never_cached(tmp_path: Path) -> None:
    client = _client(tmp_path, sources=True)
    reply = client.get("/admin/sw.js")
    assert reply.status_code == 200 and reply.headers["content-type"].startswith("text/javascript")
    assert reply.headers["cache-control"] == "no-cache"
    assert "caches." not in reply.text  # it keeps nothing: the pages are private


def test_the_page_links_the_manifest_and_the_policy_lets_it_load(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path, sources=True))
    page = client.get("/admin/")
    assert 'rel="manifest" href="/admin/static/manifest.webmanifest"' in page.text
    policy = page.headers["content-security-policy"]
    assert "manifest-src 'self'" in policy and "worker-src 'self'" in policy
    assert "unsafe" not in policy


def test_the_settings_have_the_button_that_turns_alerts_on(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path, sources=True))
    page = client.get("/admin/config").text
    assert "data-notify-toggle" in page and 'id="alerts"' in page
