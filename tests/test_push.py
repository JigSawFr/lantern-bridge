# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""Push: the message a browser can read, the key that signs it, who it may be sent to, and the file that keeps it."""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path

import httpx
import pytest
from Crypto.Hash import SHA256
from Crypto.PublicKey import ECC
from Crypto.Signature import DSS
from fastapi.testclient import TestClient

from lantern_bridge.history import Event
from lantern_bridge.push import KINDS, SCHEMA, Push, _raw, allowed, b64d, b64e, decrypt, encrypt, vapid_header
from test_admin import _admin, _client, _csrf

ENDPOINT = "https://fcm.googleapis.com/fcm/send/abc123"


def _unspace(text: str) -> bytes:
    return b64d("".join(text.split()))


def _browser() -> tuple[ECC.EccKey, bytes, bytes]:
    """What a browser holds for a subscription: its key pair and its authentication secret."""
    key = ECC.generate(curve="P-256")
    return key, _raw(key), os.urandom(16)


def test_the_message_is_the_one_of_the_example_of_rfc_8291() -> None:
    ua_private = ECC.construct(
        curve="P-256", d=int.from_bytes(_unspace("q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94"), "big")
    )
    as_private = ECC.construct(
        curve="P-256", d=int.from_bytes(_unspace("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big")
    )
    ua_public = _unspace("BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4")
    auth = _unspace("BTBZMqHH6r4Tts7J_aSIgg")
    expected = _unspace(
        """DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27ml
        mlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPT
        pK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN"""
    )
    assert _raw(ua_private) == ua_public
    plaintext = b"When I grow up, I want to be a watermelon"
    sent = encrypt(plaintext, ua_public, auth, salt=_unspace("DGv6ra1nlYgDCS1FRnbzlw"), as_private=as_private)
    assert sent == expected  # byte for byte: what a real browser would decrypt
    assert decrypt(expected, ua_private, auth) == plaintext


def test_a_message_is_for_one_browser_only_and_a_long_one_is_refused() -> None:
    key, public, auth = _browser()
    message = encrypt(b"hello", public, auth)
    assert decrypt(message, key, auth) == b"hello"
    assert encrypt(b"hello", public, auth) != message  # a new salt and a new key each time
    other, _, _ = _browser()
    with pytest.raises(ValueError):
        decrypt(message, other, auth)  # another browser cannot read it
    with pytest.raises(ValueError):
        decrypt(message, key, os.urandom(16))  # nor with another authentication secret
    with pytest.raises(ValueError):
        encrypt(b"x" * 3994, public, auth)  # the most a push service takes in one record
    with pytest.raises(ValueError):
        encrypt(b"hello", b"\x04" + bytes(64), auth)  # a point that is not on the curve


def test_the_header_of_the_bridge_is_signed_for_the_push_service_it_calls() -> None:
    key = ECC.generate(curve="P-256")
    header = vapid_header(key, "https://updates.push.services.mozilla.com/wpush/v2/xyz", now=1_000_000)
    assert header.startswith("vapid t=") and f", k={b64e(_raw(key))}" in header
    token = header.removeprefix("vapid t=").split(",")[0]
    head, claims, signature = token.split(".")
    assert json.loads(b64d(head)) == {"typ": "JWT", "alg": "ES256"}
    body = json.loads(b64d(claims))
    assert body["aud"] == "https://updates.push.services.mozilla.com" and body["exp"] == 1_000_000 + 12 * 3600
    assert body["sub"].startswith("https://")  # a contact, and no e-mail address
    DSS.new(key, "fips-186-3").verify(SHA256.new(f"{head}.{claims}".encode()), b64d(signature))  # raises if wrong
    assert len(b64d(signature)) == 64


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://fcm.googleapis.com/fcm/send/x",
        "https://updates.push.services.mozilla.com/wpush/v2/x",
        "https://wns2-par02p.notify.windows.com/w/?token=x",
        "https://web.push.apple.com/x",
    ],
)
def test_the_push_services_of_the_browsers_are_the_only_ones_called(endpoint: str) -> None:
    assert allowed(endpoint)


@pytest.mark.parametrize(
    "endpoint",
    [
        "",
        "http://fcm.googleapis.com/fcm/send/x",  # not https
        "https://127.0.0.1/x",
        "https://localhost/x",
        "https://192.168.1.10/x",
        "https://169.254.169.254/latest/meta-data",
        "https://evilfcm.googleapis.com/x",  # not a subdomain
        "https://fcm.googleapis.com.evil.example/x",
        "https://fcm.googleapis.com@evil.example/x",  # the host is what follows the @
        "https://user:pw@fcm.googleapis.com/x",
        "https://fcm.googleapis.com:8443/x",
        "https://fcm.googleapis.com/" + "a" * 1100,
        "ftp://fcm.googleapis.com/x",
    ],
)
def test_no_other_address_is_reached(endpoint: str) -> None:
    assert not allowed(endpoint)


def _push(tmp_path: Path, calls: list[httpx.Request], status: int = 201, **kw: object) -> Push:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status)

    return Push(tmp_path / "push.json", "Test shelf", client=httpx.Client(transport=httpx.MockTransport(handler)), **kw)  # type: ignore[arg-type]


def _subscribe(push: Push, endpoint: str = ENDPOINT, lang: str = "en") -> tuple[ECC.EccKey, bytes]:
    key, public, auth = _browser()
    assert push.subscribe(endpoint, b64e(public), b64e(auth), lang)
    return key, auth


def test_the_key_is_made_once_and_kept_in_a_versioned_file(tmp_path: Path) -> None:
    push = _push(tmp_path, [])
    path = tmp_path / "push.json"
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["schema"] == SCHEMA == 1 and data["subscriptions"] == []
    key = push.public_key()
    assert len(b64d(key)) == 65
    assert _push(tmp_path, []).public_key() == key  # the same after a restart
    _subscribe(push)
    assert _push(tmp_path, []).count() == 1  # and so are the subscriptions


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_the_file_is_private_when_it_is_made_and_when_it_is_written_again(tmp_path: Path) -> None:
    push = _push(tmp_path, [])
    path = tmp_path / "push.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    _subscribe(push)  # written again
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_file_of_a_newer_schema_is_left_alone_and_push_stays_off(tmp_path: Path) -> None:
    path = tmp_path / "push.json"
    newer = json.dumps({"schema": 2, "vapid": {"private": "x"}, "subscriptions": [], "future": True})
    path.write_text(newer)
    push = _push(tmp_path, [])
    assert push.newer and not push.on
    assert not push.subscribe(ENDPOINT, "x", "y")
    with pytest.raises(RuntimeError):
        push.public_key()
    push.on_event(Event(1.0, "source_down", "games", ""))  # nothing is sent, nothing fails
    assert path.read_text() == newer  # never written over


def test_what_a_file_holds_beside_what_is_known_is_kept_when_it_is_written(tmp_path: Path) -> None:
    _push(tmp_path, [])
    path = tmp_path / "push.json"
    data = json.loads(path.read_text())
    data["note"] = {"from": "a later version"}
    path.write_text(json.dumps(data))
    push = _push(tmp_path, [])
    _subscribe(push)
    assert json.loads(path.read_text())["note"] == {"from": "a later version"}


def test_a_file_that_cannot_be_read_is_not_written_over(tmp_path: Path) -> None:
    path = tmp_path / "push.json"
    path.write_text("{ not json")
    push = _push(tmp_path, [])
    assert not push.on
    assert path.read_text() == "{ not json"


def test_a_subscription_must_be_a_real_one(tmp_path: Path) -> None:
    push = _push(tmp_path, [])
    _, public, auth = _browser()
    assert not push.subscribe("https://evil.example/x", b64e(public), b64e(auth))
    assert not push.subscribe(ENDPOINT, b64e(b"\x04" + bytes(64)), b64e(auth))  # not on the curve
    assert not push.subscribe(ENDPOINT, b64e(public), b64e(b"short"))
    assert not push.subscribe(ENDPOINT, "!!!", b64e(auth))
    assert push.count() == 0
    assert push.subscribe(ENDPOINT, b64e(public), b64e(auth))
    assert push.subscribe(ENDPOINT, b64e(public), b64e(auth))  # the same address is renewed, not doubled
    assert push.count() == 1
    for n in range(40):  # the table does not grow without end: the oldest make room
        _subscribe(push, f"https://fcm.googleapis.com/fcm/send/{n}")
    assert push.count() == 20
    assert push.unsubscribe(f"https://fcm.googleapis.com/fcm/send/{39}") and not push.unsubscribe(ENDPOINT)


def test_a_line_of_the_journal_reaches_each_device_in_its_language_and_nothing_else_does(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[httpx.Request] = []
    words = {"fr": {"Source down": "Source en panne"}}
    push = _push(tmp_path, calls, translate=lambda lang, text: words.get(lang, {}).get(text, text))
    english = _subscribe(push, "https://fcm.googleapis.com/fcm/send/en", "en")
    french = _subscribe(push, "https://updates.push.services.mozilla.com/wpush/v2/fr", "fr")
    with caplog.at_level(logging.DEBUG):
        push.on_event(Event(1.0, "scan", "games", "+1 ~0 -0"))  # not worth a buzz
        push.on_event(Event(2.0, "source_down", "games", "a path that must not travel"))
        push.close()
    assert len(calls) == 2
    by_host = {request.url.host: request for request in calls}
    for host, (key, auth), body in (
        ("fcm.googleapis.com", english, "Source down: games"),
        ("updates.push.services.mozilla.com", french, "Source en panne: games"),
    ):
        request = by_host[host]
        assert request.headers["content-encoding"] == "aes128gcm" and request.headers["urgency"] == "high"
        assert request.headers["authorization"].startswith("vapid t=") and int(request.headers["ttl"]) > 0
        message = json.loads(decrypt(request.content, key, auth))
        assert message == {"title": "Test shelf", "body": body, "tag": "lb-sources", "url": "/admin/events"}
        assert "path that must not travel" not in json.dumps(message)  # the detail of a line stays home
    assert "fcm/send/en" not in caplog.text and "wpush/v2/fr" not in caplog.text  # an address is a capability


def test_a_subscription_the_browser_dropped_is_forgotten_and_a_failure_is_only_noted(tmp_path: Path) -> None:
    gone: list[httpx.Request] = []
    push = _push(tmp_path, gone, status=410)
    _subscribe(push)
    push.on_event(Event(1.0, "update", "", "9.9.9"))
    push.close()
    assert push.count() == 0  # a 410 says it is over

    broken: list[httpx.Request] = []
    push = _push(tmp_path, broken, status=503)
    _subscribe(push)
    push.on_event(Event(1.0, "update", "", "9.9.9"))
    push.close()
    assert push.count() == 1 and push.error == "503"  # a push service that fails is not a reason to forget


def test_every_kind_that_speaks_has_a_text_the_pages_already_have() -> None:
    from lantern_bridge.admin.i18n import FR
    from lantern_bridge.push import TAGS

    assert set(KINDS) == set(TAGS)
    for words, _urgency in KINDS.values():
        assert words in FR  # no text of its own to translate


def _signed_in(tmp_path: Path) -> TestClient:
    return _admin(_client(tmp_path, sources=True))


def test_the_pages_give_the_key_and_keep_or_drop_a_subscription(tmp_path: Path) -> None:
    client = _signed_in(tmp_path)
    push = client.app.state.runtime.push  # type: ignore[attr-defined]
    assert client.get("/admin/push/key").json() == {"key": push.public_key()}
    csrf = _csrf(client.get("/admin/config").text)
    _, public, auth = _browser()
    form = {"csrf": csrf, "endpoint": ENDPOINT, "p256dh": b64e(public), "auth": b64e(auth)}
    assert client.post("/admin/push/subscribe", data={**form, "csrf": "wrong"}).status_code == 403
    assert client.post("/admin/push/subscribe", data={**form, "endpoint": "https://127.0.0.1/x"}).status_code == 400
    assert push.count() == 0
    client.cookies.set("lb_lang", "fr", path="/admin")
    assert client.post("/admin/push/subscribe", data=form).status_code == 204 and push.count() == 1
    assert push._subs[0]["lang"] == "fr"  # the device is told in the language of its page
    assert client.post("/admin/push/unsubscribe", data={"csrf": csrf, "endpoint": ENDPOINT}).status_code == 204
    assert push.count() == 0
    assert "endpoint" not in client.get("/admin/config").text  # an address is never shown in a page


def test_nobody_signed_out_can_use_the_routes(tmp_path: Path) -> None:
    client = _client(tmp_path, sources=True)
    assert client.get("/admin/push/key").status_code in (303, 307, 401, 403)
    assert client.post("/admin/push/subscribe", data={"endpoint": ENDPOINT}).status_code in (303, 307, 401, 403)


def test_the_bridge_listens_to_its_journal_with_it(tmp_path: Path) -> None:
    client = _signed_in(tmp_path)
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    assert runtime.push.on_event in runtime.history.listeners
    assert (tmp_path / "state" / "push.json").exists() or runtime.push.path.exists()


def test_a_folder_that_cannot_be_written_leaves_push_off_and_the_bridge_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_: object) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr("lantern_bridge.push.os.replace", refuse)  # the same as a folder nobody can write in
    push = Push(tmp_path / "push.json", "Test shelf")
    assert not push.on and not list(tmp_path.glob(".push-*"))  # off, and no leftover file
    push.on_event(Event(1.0, "source_down", "games", ""))  # nothing to do, nothing fails


def test_a_subscription_that_cannot_be_kept_is_refused_and_not_half_made(tmp_path: Path) -> None:
    push = _push(tmp_path, [])
    _subscribe(push)
    (tmp_path / "push.json").unlink()
    (tmp_path / "push.json").mkdir()  # a folder where the file goes: it cannot be replaced
    _, public, auth = _browser()
    assert not push.subscribe("https://fcm.googleapis.com/fcm/send/other", b64e(public), b64e(auth))
    assert push.count() == 1  # what was kept is still what is kept
    assert not push.unsubscribe(ENDPOINT) and push.count() == 1


def test_an_event_after_the_bridge_was_swapped_is_not_an_error(tmp_path: Path) -> None:
    push = _push(tmp_path, [])
    _subscribe(push)
    push.close()
    push.on_event(Event(1.0, "source_down", "games", ""))  # the pool is shut: told nothing, raises nothing
