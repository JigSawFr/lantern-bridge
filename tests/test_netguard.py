"""Who the administration answers to: the client behind a proxy, the Host header, the allowed networks, HTTPS."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from lantern.uid import new_uid

from lantern_bridge.admin.netguard import client_address, host_ok, is_https, parse_networks
from lantern_bridge.app import make_app
from lantern_bridge.config import Config, ConfigError, FolderSource, load, parse_list, save
from test_admin import PASSWORD

PROXY = parse_networks(["172.18.0.0/16"])


def test_the_forwarded_address_is_believed_only_from_a_trusted_proxy_and_read_from_the_right() -> None:
    assert client_address("172.18.0.2", "192.168.1.9", PROXY) == "192.168.1.9"
    assert client_address("203.0.113.5", "192.168.1.9", PROXY) == "203.0.113.5"  # not a proxy of ours: ignored
    # a client that writes its own first hop cannot pass for another: the rightmost untrusted one counts
    assert client_address("172.18.0.2", "10.0.0.1, 198.51.100.7", PROXY) == "198.51.100.7"
    assert client_address("172.18.0.2", "198.51.100.7, 172.18.0.3", PROXY) == "198.51.100.7"
    assert client_address("172.18.0.2", "172.18.0.9", PROXY) == "172.18.0.9"
    assert client_address("172.18.0.2", "not an address", PROXY) == "172.18.0.2"
    assert client_address("172.18.0.2", None, PROXY) == "172.18.0.2" and client_address(None, None, PROXY) == "?"


def test_https_is_taken_from_the_proxy_only_when_it_is_trusted() -> None:
    assert is_https("https", "203.0.113.5", None, PROXY)
    assert is_https("http", "172.18.0.2", "https", PROXY)
    assert not is_https("http", "203.0.113.5", "https", PROXY)
    assert not is_https("http", "172.18.0.2", "http", PROXY)


@pytest.mark.parametrize(
    ("header", "allowed", "ok"),
    [
        ("192.168.1.20:8080", [], True),
        ("[::1]:8080", [], True),
        ("localhost", [], True),
        ("bridge", [], True),
        ("bridge.lan:8080", [], True),
        ("nas.home.arpa", [], True),
        ("evil.example.org", [], False),
        ("evil.example.org:8080", [], False),
        ("", [], False),
        (None, [], False),
        ("bridge.example.org", ["bridge.example.org"], True),
        ("BRIDGE.example.org.", ["bridge.example.org"], True),
        ("192.168.1.20", ["bridge.example.org"], False),  # a list means only those names
    ],
)
def test_the_host_header_names_this_bridge(header: str | None, allowed: list[str], ok: bool) -> None:
    assert host_ok(header, allowed) is ok


def _app(tmp_path: Path, **kw: object) -> TestClient:
    games = tmp_path / "games"
    games.mkdir(parents=True, exist_ok=True)
    config = Config(uid=new_uid(), name="Shelf", sources=[FolderSource("games", games)], **kw)  # type: ignore[arg-type]
    return TestClient(
        make_app(config, tmp_path / "state", run=False), client=("172.18.0.2", 50000), follow_redirects=False
    )


def test_the_pages_refuse_a_name_that_is_not_ours_and_say_so_before_anything_else(tmp_path: Path) -> None:
    client = _app(tmp_path)
    assert client.get("/admin/login", headers={"host": "evil.example.org"}).status_code in (400, 303)
    assert client.get("/admin/", headers={"host": "evil.example.org"}).status_code == 400
    assert client.get("/admin/", headers={"host": "bridge.lan"}).status_code in (200, 303)
    listed = _app(tmp_path / "b", admin_hosts=["bridge.example.org"])
    assert listed.get("/admin/", headers={"host": "bridge.example.org"}).status_code in (200, 303)
    assert listed.get("/admin/", headers={"host": "192.168.1.20"}).status_code == 400


def test_behind_a_trusted_proxy_the_real_client_is_checked_counted_and_the_cookie_is_secure(tmp_path: Path) -> None:
    client = _app(tmp_path, trusted_proxies=["172.18.0.0/16"], admin_allow=["192.168.1.0/24"])
    code = client.app.state.admin.setup_code()  # type: ignore[attr-defined]
    inside = {"x-forwarded-for": "192.168.1.9", "x-forwarded-proto": "https"}
    assert (
        client.post(
            "/admin/setup", data={"code": code, "name": "boss", "password": PASSWORD}, headers=inside
        ).status_code
        == 303
    )
    assert (
        client.get("/admin/login", headers={"x-forwarded-for": "10.9.9.9"}).status_code == 403
    )  # a private one, not on the list
    assert client.get("/admin/login", headers={"x-forwarded-for": "8.8.8.8"}).status_code == 403
    signed = client.post("/admin/login", data={"name": "boss", "password": PASSWORD}, headers=inside)
    assert signed.status_code == 303 and "Secure" in signed.headers["set-cookie"]
    # a failure is counted against the client behind the proxy, not the proxy
    other = {"x-forwarded-for": "192.168.1.77"}
    client.post("/admin/login", data={"name": "nobody", "password": "wrong password"}, headers=other)
    pages = client.app.state.admin_pages  # type: ignore[attr-defined]
    assert "192.168.1.77" in pages.attempts._hits and "172.18.0.2" not in pages.attempts._hits


def test_without_a_trusted_proxy_the_forwarded_headers_are_ignored(tmp_path: Path) -> None:
    client = _app(tmp_path)  # the peer 172.18.0.2 is private: allowed, whatever it claims
    assert client.get("/admin/login", headers={"x-forwarded-for": "8.8.8.8"}).status_code in (200, 303)
    code = client.app.state.admin.setup_code()  # type: ignore[attr-defined]
    client.post("/admin/setup", data={"code": code, "name": "boss", "password": PASSWORD})
    signed = client.post(
        "/admin/login", data={"name": "boss", "password": PASSWORD}, headers={"x-forwarded-proto": "https"}
    )
    assert signed.status_code == 303 and "Secure" not in signed.headers["set-cookie"]


def test_the_lists_are_checked_kept_and_written_back(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    save(
        path,
        Config(
            uid=new_uid(),
            admin_allow=["192.168.1.0/24"],
            admin_hosts=["bridge.example.org"],
            trusted_proxies=["172.18.0.2"],
        ),
    )
    again = load(path)
    assert (again.admin_allow, again.admin_hosts, again.trusted_proxies) == (
        ["192.168.1.0/24"],
        ["bridge.example.org"],
        ["172.18.0.2"],
    )
    save(path, Config(uid=new_uid()))
    text = path.read_text()
    assert "admin_allow" not in text and "trusted_proxies" not in text
    for bad in ("x", [1], [""], ["not-a-network"]):
        with pytest.raises(ConfigError):
            parse_list(bad, "admin_allow", networks=True)
    assert parse_list(None, "k") == [] and parse_list([" a "], "k") == ["a"]
