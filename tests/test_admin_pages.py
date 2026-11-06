"""The administration pages of lot 6: Boréale's look, two languages, the settings, accounts and invitations."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from lantern.uid import new_uid

from lantern_bridge.admin import i18n
from lantern_bridge.app import make_app
from lantern_bridge.config import AerofoilSource, Config, FolderSource, load
from test_admin import PASSWORD, _admin, _client, _csrf

TEMPLATES = Path(__file__).parent.parent / "src" / "lantern_bridge" / "admin" / "templates"
BASE = "http://aerofoil.test:8465/"


def _token(client: TestClient, path: str = "/admin/config") -> str:
    return _csrf(client.get(path).text)


def _settings(tmp_path: Path, **kw: object) -> tuple[TestClient, Path]:
    client = _admin(_client(tmp_path, sources=True))
    return client, tmp_path / "state" / "config.toml"


# -- the look and the languages -----------------------------------------------------------------------


def test_the_pages_wear_boreales_night_and_serve_their_own_fonts(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path))
    page = client.get("/admin/")
    assert page.status_code == 200 and 'href="/admin/static/app.css"' in page.text
    assert "bridge.svg" in page.text and 'content="dark"' in page.text
    assert "font-src 'self'" in page.headers["content-security-policy"]
    css = client.get("/admin/static/app.css")
    assert css.status_code == 200 and "--aurora:#00ffcc" in css.text
    for font in ("sora", "figtree", "jetbrains-mono"):
        reply = client.get(f"/admin/static/{font}.woff2")
        assert (
            reply.status_code == 200 and reply.headers["content-type"] == "font/woff2" and reply.content[:4] == b"wOF2"
        )
    assert client.get("/admin/static/bridge.svg").headers["content-type"] == "image/svg+xml"
    assert client.get("/admin/static/OFL-sora.txt").status_code == 404  # only what a page uses


def test_a_page_is_english_then_french_when_asked(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path))
    assert "Pairings" in client.get("/admin/").text and 'lang="en"' in client.get("/admin/").text
    switched = client.post("/admin/lang", data={"lang": "fr", "next": "/admin/devices"})
    assert switched.status_code == 303 and switched.headers["location"] == "/admin/devices"
    page = client.get("/admin/devices")
    assert 'lang="fr"' in page.text and "Appairés" not in page.text and "Aucun appareil" in page.text
    assert "Vue d&#39;ensemble" in client.get("/admin/").text
    # The browser's wish counts when there is no choice yet.
    fresh = _admin(_client(tmp_path / "other"))
    assert "Vue d&#39;ensemble" in fresh.get("/admin/", headers={"accept-language": "fr-FR,fr;q=0.9"}).text


def test_the_language_switch_goes_nowhere_else(tmp_path: Path) -> None:
    client = _client(tmp_path)
    for target in ("https://evil.example/", "//evil.example", "/other", "/admin//x"):
        assert client.post("/admin/lang", data={"lang": "fr", "next": target}).headers["location"] == "/admin/"
    assert "lb_lang" not in client.post("/admin/lang", data={"lang": "xx"}).headers.get("set-cookie", "")


def test_every_text_of_a_page_has_its_french() -> None:
    texts = set()
    for page in TEMPLATES.glob("*.html"):
        for found in re.findall(r"""_\((?:"([^"]*)"|'([^']*)')\)""", page.read_text()):
            texts.add(found[0] or found[1])
    assert len(texts) > 100
    assert sorted(t for t in texts if t not in i18n.FR) == []
    assert i18n.pick("fr", None) == "fr" and i18n.pick(None, "de, fr;q=0.5") == "fr" and i18n.pick("xx", "de") == "en"


# -- the settings --------------------------------------------------------------------------------------


def test_the_settings_page_lists_the_sources_and_adds_and_removes_one(tmp_path: Path) -> None:
    client, path = _settings(tmp_path)
    page = client.get("/admin/config")
    assert page.status_code == 200 and "games" in page.text and "The settings file changed" not in page.text
    csrf = _csrf(page.text)
    added = client.post(
        "/admin/config/sources",
        data={
            "csrf": csrf,
            "kind": "tinfoil",
            "id": "shop",
            "where": "http://shop.test:8000/",
            "user": "me",
            "password_env": "SHOP_PW",
        },
    )
    assert added.status_code == 303 and added.headers["location"] == "/admin/config?m=added"
    saved = load(path)
    assert [s.id for s in saved.sources] == ["games", "shop"] and saved.uid == client.app.state.runtime.config.uid  # type: ignore[attr-defined]
    after = client.get("/admin/config?m=added")
    assert "Source added." in after.text and "The settings file changed" in after.text
    assert "SHOP_PW" in after.text and "not set" in after.text  # the variable's name, never a value
    assert "The settings file changed" in client.get("/admin/").text
    client.post("/admin/config/sources/shop/up", data={"csrf": csrf})
    assert [s.id for s in load(path).sources] == ["shop", "games"]
    client.post("/admin/config/sources/shop/down", data={"csrf": csrf})
    assert [s.id for s in load(path).sources] == ["games", "shop"]
    client.post("/admin/config/sources/shop/remove", data={"csrf": csrf})
    assert [s.id for s in load(path).sources] == ["games"]
    assert "The settings file changed" not in client.get("/admin/config").text


def test_a_setting_the_reader_would_refuse_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    client, path = _settings(tmp_path)
    csrf = _token(client)
    bad = [
        {"kind": "tinfoil", "id": "x", "where": "ftp://shop/"},
        {"kind": "tinfoil", "id": "games", "where": "http://shop.test/"},
        {"kind": "tinfoil", "id": "x y", "where": "http://shop.test/"},
        {"kind": "tinfoil", "id": "x", "where": "http://shop.test/", "user": "me"},
        {"kind": "tinfoil", "id": "x", "where": "http://shop.test/", "password_env": "not valid!"},
        {"kind": "unknown", "id": "x", "where": "/x"},
    ]
    for form in bad:
        reply = client.post("/admin/config/sources", data={"csrf": csrf, **form})
        assert reply.status_code == 400 and 'role="alert"' in reply.text, form
    assert not path.exists()
    assert client.post("/admin/config/sources/none/remove", data={"csrf": csrf}).status_code == 400
    assert client.post("/admin/config/settings", data={"csrf": csrf, "name": "", "remote_rate": "5"}).status_code == 400
    assert (
        client.post("/admin/config/settings", data={"csrf": csrf, "name": "x", "remote_rate": "-1"}).status_code == 400
    )
    assert (
        client.post("/admin/config/settings", data={"csrf": csrf, "name": "x", "remote_rate": "many"}).status_code
        == 400
    )
    assert client.post("/admin/config/settings", data={"name": "x"}).status_code == 403  # no csrf


def test_the_server_settings_are_saved_and_unknown_keys_survive(tmp_path: Path) -> None:
    client, path = _settings(tmp_path)
    live = client.app.state.runtime.config  # type: ignore[attr-defined]
    path.write_text(live.to_toml() + '\nfuture_key = "kept"\n')
    csrf = _token(client)
    done = client.post(
        "/admin/config/settings", data={"csrf": csrf, "name": "My shelf", "remote_rate": "2.5", "public": "on"}
    )
    assert done.status_code == 303
    saved = load(path)
    assert (saved.name, saved.remote_rate, saved.public, saved.admin_lan_only) == ("My shelf", 2.5, True, False)
    assert 'future_key = "kept"' in path.read_text()
    assert (path.stat().st_mode & 0o777) == 0o600


def test_a_folder_and_a_missing_one_are_tested(tmp_path: Path) -> None:
    client, _ = _settings(tmp_path)
    csrf = _token(client)
    ok = client.post("/admin/config/sources/games/test", data={"csrf": csrf})
    assert "The folder is there and can be read." in ok.text and 'class="good"' in ok.text
    client.post(
        "/admin/config/sources", data={"csrf": csrf, "kind": "folder", "id": "gone", "where": str(tmp_path / "nope")}
    )
    bad = client.post("/admin/config/sources/gone/test", data={"csrf": csrf})
    assert "The folder cannot be read." in bad.text
    assert client.post("/admin/config/sources/none/test", data={"csrf": csrf}).status_code == 404


def test_a_shop_that_does_not_answer_is_reported(tmp_path: Path) -> None:
    client, _ = _settings(tmp_path)
    csrf = _token(client)
    client.post(
        "/admin/config/sources", data={"csrf": csrf, "kind": "tinfoil", "id": "shop", "where": "http://127.0.0.1:9/"}
    )
    reply = client.post("/admin/config/sources/shop/test", data={"csrf": csrf})
    assert reply.status_code == 200 and "The shop did not answer" in reply.text and 'class="note"' in reply.text


# -- accounts and invitations ----------------------------------------------------------------------------


def _with_aerofoil(tmp_path: Path) -> TestClient:
    config = Config(
        uid=new_uid(), name="Shelf", sources=[AerofoilSource("af", BASE, user="svc", password_env="AF_SVC")]
    )
    app = make_app(config, tmp_path / "state", run=False)
    return _admin(TestClient(app, client=("127.0.0.1", 50000), follow_redirects=False))


def test_an_account_is_tied_to_an_aerofoil_user_and_freed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AF_ALICE", "x")
    client = _with_aerofoil(tmp_path)
    csrf = _token(client, "/admin/accounts")
    # An invitation makes the account.
    client.post("/admin/invitations", data={"csrf": csrf, "user": "alice", "days": "7", "devices": "1"})
    page = client.get("/admin/accounts")
    assert "alice" in page.text and "the service account" in page.text and "<code>svc</code>" in page.text
    tied = client.post(
        "/admin/accounts/alice/login", data={"csrf": csrf, "user": "alice-af", "password_env": "AF_ALICE"}
    )
    assert tied.status_code == 303 and tied.headers["location"] == "/admin/accounts?m=tied"
    (source,) = load(tmp_path / "state" / "config.toml").sources
    assert isinstance(source, AerofoilSource) and [(x.account, x.user, x.password_env) for x in source.logins] == [
        ("alice", "alice-af", "AF_ALICE")
    ]
    shown = client.get("/admin/accounts?m=tied")
    assert (
        "alice-af" in shown.text
        and "Login tied." in shown.text
        and "x" not in re.findall(r"AF_ALICE[^<]*", shown.text)[0][8:]
    )
    for form in ({"user": "", "password_env": "A"}, {"user": "u", "password_env": "bad name"}):
        assert client.post("/admin/accounts/alice/login", data={"csrf": csrf, **form}).status_code == 400
    assert (
        client.post("/admin/accounts/ghost/login", data={"csrf": csrf, "user": "u", "password_env": "A"}).status_code
        == 400
    )
    assert client.post("/admin/accounts/alice/login/remove", data={"csrf": csrf}).status_code == 303
    (source,) = load(tmp_path / "state" / "config.toml").sources
    assert isinstance(source, AerofoilSource) and source.logins == ()


def test_an_account_page_without_an_aerofoil_has_no_login_column(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path))
    page = client.get("/admin/accounts")
    assert page.status_code == 200 and "Acts on the AeroFoil as" not in page.text and "No account yet" in page.text
    csrf = _csrf(page.text)
    assert (
        client.post("/admin/accounts/alice/login", data={"csrf": csrf, "user": "u", "password_env": "A"}).status_code
        == 400
    )


def test_an_invitation_is_created_listed_and_revoked(tmp_path: Path) -> None:
    client = _admin(_client(tmp_path))
    csrf = _token(client, "/admin/invitations")
    made = client.post(
        "/admin/invitations", data={"csrf": csrf, "user": "kid", "days": "1", "devices": "2", "note": "tablet"}
    )
    assert made.status_code == 200 and "Invitation created for" in made.text
    listed = client.get("/admin/invitations")
    assert "tablet" in listed.text and "0 / 2" in listed.text
    invitation_id = re.search(r"/admin/invitations/([^/]+)/revoke", listed.text)
    assert invitation_id
    client.post(f"/admin/invitations/{invitation_id.group(1)}/revoke", data={"csrf": csrf})
    assert "No open invitation." in client.get("/admin/invitations").text
    for form in (
        {"user": "bad name!"},
        {"user": "kid", "devices": "0"},
        {"user": "kid", "devices": "99"},
        {"user": "kid", "days": "3"},
    ):
        reply = client.post("/admin/invitations", data={"csrf": csrf, "devices": "1", "days": "7", **form})
        assert reply.status_code == 400 and 'role="alert"' in reply.text, form


# -- the diagnostic -----------------------------------------------------------------------------------------


def test_the_diagnostic_has_no_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AF_SVC", "hunter2-secret")
    config = Config(
        uid=new_uid(),
        sources=[
            FolderSource("games", tmp_path),
            AerofoilSource("af", "http://me:topsecret@aerofoil.test:8465/", user="svc", password_env="AF_SVC"),
        ],
    )
    client = _admin(
        TestClient(make_app(config, tmp_path / "state", run=False), client=("127.0.0.1", 50000), follow_redirects=False)
    )
    reply = client.get("/admin/diagnostic.json")
    assert reply.status_code == 200 and "attachment" in reply.headers["content-disposition"]
    text = reply.text
    for secret in ("topsecret", "hunter2-secret", PASSWORD):
        assert secret not in text
    doc = json.loads(text)
    assert doc["schema"] == 1 and doc["bridge"] and doc["draft"]
    by = {s["id"]: s for s in doc["sources"]}
    assert (
        by["af"]["password_env"] == "AF_SVC"
        and by["af"]["password_set"] is True
        and by["af"]["address"].endswith("aerofoil.test:8465/")
    )
    assert "me@" not in by["af"]["address"] and by["games"]["kind"] == "folder"


def test_the_new_pages_need_a_session(tmp_path: Path) -> None:
    client = _client(tmp_path)
    _admin(client)
    client.cookies.clear()
    for path in ("/admin/config", "/admin/accounts", "/admin/invitations", "/admin/diagnostic.json"):
        assert client.get(path).headers["location"] == "/admin/login"
    for path in ("/admin/config/settings", "/admin/config/sources", "/admin/invitations", "/admin/accounts/a/login"):
        assert client.post(path, data={}).headers["location"] == "/admin/login"
