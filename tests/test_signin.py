# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""Signing in to the pages through a trusted proxy or an OpenID Connect provider."""

from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from lantern.uid import new_uid

from lantern_bridge.admin import signin
from lantern_bridge.admin.auth import AdminStore
from lantern_bridge.app import make_app
from lantern_bridge.config import Config, ConfigError, Oidc, load, parse_sign_in
from test_admin import PASSWORD

ISSUER = "https://id.example.org"
HERE = "https://bridge.example.org"
ROLES: dict[str, str] = {"lantern-owners": "owner", "lantern-family": "manager", "lantern-guests": "observer"}
PROXY: dict[str, Any] = {"proxy": {"enabled": True}, "roles": ROLES, "logout_url": "https://id.example.org/logout"}
OIDC: dict[str, Any] = {
    "oidc": {
        "enabled": True,
        "issuer": ISSUER,
        "client_id": "bridge",
        "public_url": HERE,
        "secret_env": "BRIDGE_SECRET",
    },
    "roles": ROLES,
}


def _app(tmp_path: Path, table: dict[str, Any], *, peer: str = "10.0.0.5") -> TestClient:
    config = Config(
        uid=new_uid(),
        name="Test shelf",
        trusted_proxies=["10.0.0.0/8"],
        sign_in=parse_sign_in(table, ["10.0.0.0/8"]),
    )
    app = make_app(config, tmp_path / "state", run=False)
    return TestClient(app, client=(peer, 50000), follow_redirects=False)


def _owner(client: TestClient) -> None:
    code = client.app.state.admin.setup_code()  # type: ignore[attr-defined]
    assert client.post("/admin/setup", data={"code": code, "name": "boss", "password": PASSWORD}).status_code == 303


def _via_proxy(user: str, groups: str) -> dict[str, str]:
    return {"Remote-User": user, "Remote-Groups": groups}


def _events(client: TestClient) -> list[tuple[str, str]]:
    return [(e.kind, e.subject) for e in client.app.state.runtime.history.events()]  # type: ignore[attr-defined]


# -- the settings ------------------------------------------------------------


def test_the_settings_are_read_checked_and_written_back_whole(tmp_path: Path) -> None:
    sign_in = parse_sign_in({**PROXY, "future_key": 1}, ["10.0.0.0/8"])
    assert sign_in is not None and sign_in.proxy_user_header == "Remote-User" and sign_in.local
    config = Config(uid=new_uid(), trusted_proxies=["10.0.0.0/8"], sign_in=sign_in)
    path = tmp_path / "config.toml"
    path.write_text(config.to_toml(), encoding="utf-8")
    again = load(path)
    assert again.sign_in is not None and again.sign_in.table["future_key"] == 1
    assert again.sign_in.roles == ROLES and again.sign_in.logout_url == "https://id.example.org/logout"
    assert parse_sign_in(None, []) is None


@pytest.mark.parametrize(
    ("table", "proxies", "why"),
    [
        (PROXY, [], "trusted_proxies"),
        ({"proxy": {"enabled": True}}, ["10.0.0.0/8"], "roles"),
        ({"local": False}, [], "nobody could sign in"),
        ({**PROXY, "roles": {"g": "king"}}, ["10.0.0.0/8"], "roles"),
        ({**OIDC, "oidc": {**OIDC["oidc"], "issuer": "http://id.example.org"}}, [], "https"),
        ({**OIDC, "oidc": {**OIDC["oidc"], "public_url": f"{HERE}/path"}}, [], "path"),
        ({**OIDC, "oidc": {**OIDC["oidc"], "secret_env": "not a name"}}, [], "environment variable"),
        ({**OIDC, "oidc": {**OIDC["oidc"], "scopes": "profile"}}, [], "openid"),
        ({**PROXY, "proxy": {"enabled": True, "user_header": "bad header"}}, ["10.0.0.0/8"], "header"),
        ({**PROXY, "logout_url": "javascript:alert(1)"}, ["10.0.0.0/8"], "https"),
    ],
)
def test_what_would_open_the_pages_wrongly_is_refused(table: dict[str, Any], proxies: list[str], why: str) -> None:
    with pytest.raises(ConfigError, match=why):
        parse_sign_in(table, proxies)


def test_a_group_gives_the_highest_role_of_those_it_matches() -> None:
    assert signin.role_for(("lantern-guests", "lantern-family"), ROLES) == "manager"
    assert signin.role_for(("lantern-owners",), ROLES) == "owner"
    assert signin.role_for(("other",), ROLES) is None and signin.role_for((), ROLES) is None
    assert signin.split_groups("a, b;c,,a") == ("a", "b", "c")
    assert signin.clean_name("  Alex \n Smith ") == "Alex Smith" and signin.clean_name("x" * 65) is None


# -- a trusted proxy ---------------------------------------------------------


def test_the_proxy_signs_a_person_in_with_the_role_of_their_group(tmp_path: Path) -> None:
    client = _app(tmp_path, PROXY)
    _owner(client)
    page = client.get("/admin/login")
    assert page.status_code == 200 and 'name="password"' in page.text  # nobody announced: the usual form
    opened = client.get("/admin/login", headers=_via_proxy("lea", "lantern-family,other"))
    assert (
        opened.status_code == 200
        and 'http-equiv="refresh"' in opened.text
        and "lb_session" in opened.headers["set-cookie"]
    )
    overview = client.get("/admin/", headers=_via_proxy("lea", "lantern-family"))
    assert overview.status_code == 200 and "lea" in overview.text
    assert client.get("/admin/devices", headers=_via_proxy("lea", "lantern-family")).status_code == 200
    assert client.get("/admin/operators", headers=_via_proxy("lea", "lantern-family")).status_code == 403  # a manager
    rows = client.app.state.admin.listing()  # type: ignore[attr-defined]
    assert [(r["name"], r["role"], r["source"]) for r in rows] == [("lea", "manager", "proxy")]


def test_the_proxy_is_asked_again_at_every_request(tmp_path: Path) -> None:
    client = _app(tmp_path, PROXY)
    _owner(client)
    client.get("/admin/login", headers=_via_proxy("lea", "lantern-family"))
    assert client.get("/admin/devices", headers=_via_proxy("lea", "lantern-guests")).status_code == 403  # demoted now
    assert client.get("/admin/", headers=_via_proxy("lea", "lantern-guests")).status_code == 200
    assert client.get("/admin/", headers=_via_proxy("zoe", "lantern-guests")).headers["location"] == "/admin/login"
    client.get("/admin/login", headers=_via_proxy("lea", "lantern-family"))
    assert client.get("/admin/").headers["location"] == "/admin/login"  # no header at all: no session
    client.get("/admin/login", headers=_via_proxy("lea", "lantern-family"))
    assert client.get("/admin/", headers=_via_proxy("lea", "nobody")).headers["location"] == "/admin/login"


def test_a_person_none_of_whose_groups_count_does_not_get_in(tmp_path: Path) -> None:
    client = _app(tmp_path, PROXY)
    _owner(client)
    refused = client.get("/admin/login", headers=_via_proxy("eve", "strangers"))
    assert (
        refused.status_code == 403
        and "none of your groups" in refused.text
        and "lb_session" not in refused.headers.get("set-cookie", "")
    )
    assert ("signin_denied", "eve") in _events(client)
    assert client.get("/admin/", headers=_via_proxy("eve", "strangers")).headers["location"] == "/admin/login"


def test_headers_from_anything_but_a_trusted_proxy_are_ignored(tmp_path: Path) -> None:
    client = _app(tmp_path, PROXY, peer="192.168.1.9")  # on the network, not the proxy
    _owner(client)
    forged = client.get("/admin/login", headers=_via_proxy("boss", "lantern-owners"))
    assert forged.status_code == 200 and "lb_session" not in forged.headers.get("set-cookie", "")
    assert client.get("/admin/", headers=_via_proxy("boss", "lantern-owners")).headers["location"] == "/admin/login"


def test_signing_out_goes_to_the_provider_or_does_not_sign_straight_back_in(tmp_path: Path) -> None:
    client = _app(tmp_path, PROXY)
    _owner(client)
    client.get("/admin/login", headers=_via_proxy("lea", "lantern-family"))
    headers = _via_proxy("lea", "lantern-family")
    csrf = re.search(r'name="csrf" value="([^"]+)"', client.get("/admin/me", headers=headers).text)
    assert csrf
    out = client.post("/admin/logout", data={"csrf": csrf.group(1)}, headers=headers)
    assert out.status_code == 303 and out.headers["location"] == "https://id.example.org/logout"
    again = _app(tmp_path / "again", {k: v for k, v in PROXY.items() if k != "logout_url"})
    _owner(again)
    again.get("/admin/login", headers=headers)
    csrf = re.search(r'name="csrf" value="([^"]+)"', again.get("/admin/me", headers=headers).text)
    assert csrf
    out = again.post("/admin/logout", data={"csrf": csrf.group(1)}, headers=headers)
    assert out.headers["location"] == "/admin/login?out=1"
    page = again.get("/admin/login?out=1", headers=headers)
    assert (
        page.status_code == 200 and "signed out" in page.text and "lb_session" not in page.headers.get("set-cookie", "")
    )


def test_a_person_from_outside_has_no_password_here_and_the_local_form_can_be_off(tmp_path: Path) -> None:
    client = _app(tmp_path, {**PROXY, "local": False})
    _owner(client)
    page = client.get("/admin/login")
    assert 'name="password"' not in page.text
    assert client.post("/admin/login", data={"name": "boss", "password": PASSWORD}).status_code in (403,)
    headers = _via_proxy("lea", "lantern-family")
    client.get("/admin/login", headers=headers)
    me = client.get("/admin/me", headers=headers)
    assert me.status_code == 200 and "managed by your single sign-on" in me.text and 'name="current"' not in me.text
    csrf = re.search(r'name="csrf" value="([^"]+)"', me.text)
    assert csrf
    refused = client.post(
        "/admin/me",
        data={"csrf": csrf.group(1), "current": "x", "password": "y" * 12, "again": "y" * 12},
        headers=headers,
    )
    assert refused.status_code == 403


def test_a_session_from_outside_shares_nothing_with_an_operator_of_the_same_name(tmp_path: Path) -> None:
    store = AdminStore(tmp_path / "admin.json")
    store.create(store.setup_code() or "", "boss", PASSWORD)
    assert store.add("lea", "admin", PASSWORD) is None
    local, _ = store.login("lea", "admin")
    outside, session = store.login("lea", "observer", source="proxy")
    assert store.set_role("lea", "manager") is None
    assert store.session(local).role == "manager" and store.session(outside) is session and session.role == "observer"  # type: ignore[union-attr]
    assert store.set_password("lea", "another password") is None
    assert store.session(local) is None and store.session(outside) is session


# -- an OpenID Connect provider ----------------------------------------------


def _token(claims: dict[str, Any]) -> str:
    def part(value: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


class Provider:
    """A fake provider: it answers what the bridge asks, with the claims it was given for the next sign-in."""

    def __init__(self) -> None:
        self.claims: dict[str, Any] = {"sub": "u1", "preferred_username": "lea", "groups": ["lantern-family"]}
        self.userinfo: dict[str, Any] = {}
        self.nonce = ""
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        path = request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/authorize",
                    "token_endpoint": f"{ISSUER}/token",
                    "userinfo_endpoint": f"{ISSUER}/userinfo",
                },
            )
        if path == "/token":
            claims = {"iss": ISSUER, "aud": "bridge", "exp": time.time() + 300, "nonce": self.nonce, **self.claims}
            return httpx.Response(200, json={"id_token": _token(claims), "access_token": "at"})
        if path == "/userinfo":
            return httpx.Response(200, json={"sub": "u1", **self.userinfo})
        return httpx.Response(404)


def _oidc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Provider]:
    monkeypatch.setenv("BRIDGE_SECRET", "s3cret")
    client = _app(tmp_path, OIDC, peer="192.168.1.9")
    _owner(client)
    provider = Provider()
    pages = client.app.state.admin_pages  # type: ignore[attr-defined]
    assert pages.oidc is not None
    pages.oidc = signin.OidcSignIn(
        pages.sign_in.oidc, "s3cret", httpx.Client(transport=httpx.MockTransport(provider), follow_redirects=False)
    )
    return client, provider


def _begin(client: TestClient, provider: Provider) -> str:
    """Starts a sign-in; the `state` the bridge sent the browser away with (the nonce goes to the provider)."""
    sent = client.get("/admin/login/oidc")
    assert sent.status_code == 303
    target = urlsplit(sent.headers["location"])
    query = parse_qs(target.query)
    assert f"{target.scheme}://{target.netloc}{target.path}" == f"{ISSUER}/authorize"
    assert query["client_id"] == ["bridge"] and query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == [f"{HERE}/admin/login/oidc/callback"] and query["response_type"] == ["code"]
    provider.nonce = query["nonce"][0]
    return str(query["state"][0])


def test_a_person_signs_in_with_the_provider_and_gets_the_role_of_their_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, provider = _oidc(tmp_path, monkeypatch)
    assert "Sign in with your single sign-on" in client.get("/admin/login").text
    state = _begin(client, provider)
    done = client.get(f"/admin/login/oidc/callback?code=abc&state={state}")
    assert done.status_code == 200 and "lb_session" in done.headers["set-cookie"]
    token = next(r for r in provider.seen if r.url.path == "/token")
    body = parse_qs(token.content.decode())
    assert body["code"] == ["abc"] and body["grant_type"] == ["authorization_code"] and "code_verifier" in body
    assert token.headers["authorization"].startswith("Basic ")
    assert client.get("/admin/devices").status_code == 200
    rows = client.app.state.admin.listing()  # type: ignore[attr-defined]
    assert [(r["name"], r["role"], r["source"]) for r in rows] == [("lea", "manager", "oidc")]
    assert client.get("/admin/operators").status_code == 403


def test_groups_the_provider_keeps_in_its_user_information_are_read_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, provider = _oidc(tmp_path, monkeypatch)
    provider.claims = {"sub": "u1", "preferred_username": "lea"}
    provider.userinfo = {"groups": ["lantern-owners"]}
    state = _begin(client, provider)
    assert client.get(f"/admin/login/oidc/callback?code=abc&state={state}").status_code == 200
    assert client.app.state.admin.listing()[0]["role"] == "owner"  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "claims",
    [
        {"aud": "someone-else"},
        {"iss": "https://evil.example.org"},
        {"exp": 1},
        {"nonce": "not-the-one"},
        {"sub": ""},
    ],
)
def test_an_identity_token_that_is_not_for_this_attempt_opens_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, claims: dict[str, Any]
) -> None:
    client, provider = _oidc(tmp_path, monkeypatch)
    provider.claims = {"sub": "u1", "preferred_username": "lea", "groups": ["lantern-owners"], **claims}
    state = _begin(client, provider)
    failed = client.get(f"/admin/login/oidc/callback?code=abc&state={state}")
    assert failed.status_code == 400 and "did not complete" in failed.text
    assert client.get("/admin/").headers["location"] == "/admin/login"


def test_an_attempt_serves_once_and_only_the_browser_that_began_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, provider = _oidc(tmp_path, monkeypatch)
    state = _begin(client, provider)
    other = TestClient(client.app, client=("192.168.1.9", 50001), follow_redirects=False)
    assert (
        other.get(f"/admin/login/oidc/callback?code=abc&state={state}").status_code == 400
    )  # no cookie of the attempt
    state = _begin(client, provider)
    assert client.get(f"/admin/login/oidc/callback?code=abc&state={state}").status_code == 200
    client.cookies.set("lb_oidc", f"{state}.x", path="/admin/login/oidc")
    assert client.get(f"/admin/login/oidc/callback?code=abc&state={state}").status_code == 400  # already used
    assert client.get("/admin/login/oidc/callback?error=access_denied&state=x").status_code == 400
    assert client.get("/admin/login/oidc/callback").status_code == 400


def test_a_provider_user_none_of_whose_groups_count_is_refused_and_noted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, provider = _oidc(tmp_path, monkeypatch)
    provider.claims = {"sub": "u1", "preferred_username": "eve", "groups": ["strangers"]}
    state = _begin(client, provider)
    refused = client.get(f"/admin/login/oidc/callback?code=abc&state={state}")
    assert refused.status_code == 403 and "none of your groups" in refused.text
    assert ("signin_denied", "eve") in _events(client)
    assert client.get("/admin/").headers["location"] == "/admin/login"


def test_without_the_provider_there_is_nothing_at_its_address(tmp_path: Path) -> None:
    client = _app(tmp_path, PROXY)
    assert client.get("/admin/login/oidc").status_code == 404
    assert client.get("/admin/login/oidc/callback?code=a&state=b").status_code == 404


def test_the_discovery_must_name_the_issuer_it_was_asked_for() -> None:
    def wrong(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "issuer": "https://other.example.org",
                "authorization_endpoint": f"{ISSUER}/a",
                "token_endpoint": f"{ISSUER}/t",
            },
        )

    oidc = Oidc(ISSUER, "bridge", HERE)
    flow = signin.OidcSignIn(oidc, None, httpx.Client(transport=httpx.MockTransport(wrong)))
    with pytest.raises(signin.SignInError):
        flow.start()
