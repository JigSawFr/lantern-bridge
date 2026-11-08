# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""Web Push: a notification on a device while no page of the bridge is open.

A browser that allowed alerts also subscribes to its push service (the one of its maker: Google, Mozilla, Apple,
Microsoft) and gives the bridge the address of the subscription and two keys. When the journal gets a line worth a
buzz, the bridge sends the push service a message that only that browser can read: encrypted with the browser's
own keys (RFC 8291, `aes128gcm`) and signed with the bridge's VAPID key (RFC 8292), so that nobody else can push
to the subscription. The push service sees that a message goes by, never what it says.

`push.json` (0600, `schema` 1) holds the VAPID key and the subscriptions. The address of a subscription is a
capability: with the keys it lets anyone push to that browser. It is never logged, never shown in a page and never
put in a backup. A file of a newer schema is left alone and push stays off.

Only the push services of the browsers are called (a subscription for another host is refused): the address comes
from a browser, and the bridge must not become a way to reach any address of its network.
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from Crypto.Cipher import AES
from Crypto.Hash import SHA256
from Crypto.Protocol.DH import key_agreement
from Crypto.Protocol.KDF import HKDF
from Crypto.PublicKey import ECC
from Crypto.Random import get_random_bytes
from Crypto.Signature import DSS

from lantern_bridge.history import Event

log = logging.getLogger("lantern_bridge.push")

SCHEMA = 1
TIMEOUT = 8.0
#: The push services of the browsers; a host is allowed when it is one of these or a subdomain of one.
SERVICES = ("fcm.googleapis.com", "push.services.mozilla.com", "notify.windows.com", "push.apple.com")
MAX_SUBSCRIPTIONS = 20
MAX_ENDPOINT = 1024
#: Who is told, in the words the pages already have (the journal's own labels), and how urgent it is.
KINDS = {
    "source_down": ("Source down", "high"),
    "source_up": ("Source back", "normal"),
    "update": ("Newer version out", "low"),
    "converted": ("File converted", "low"),
}
#: The tag of the alert, shared with the ones the open pages show, so that one replaces the other.
TAGS = {"source_down": "lb-sources", "source_up": "lb-sources", "update": "lb-update", "converted": "lb-tasks"}
VAPID_SUBJECT = "https://github.com/JigSawFr/lantern-bridge"  # RFC 8292: a contact the push service can look at
TTL = 3600  # seconds a push service keeps a message for a device that is off


def b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _point(public: bytes) -> ECC.EccKey:
    """A P-256 public key from its 65 bytes (uncompressed form). ValueError when it is not a point of the curve
    (RFC 8291 section 7: a key that is not checked can give away a private key), the point at infinity included."""
    if len(public) != 65 or public[0] != 4:
        raise ValueError("not an uncompressed P-256 point")
    key = ECC.construct(
        curve="P-256", point_x=int.from_bytes(public[1:33], "big"), point_y=int.from_bytes(public[33:], "big")
    )
    if key.pointQ.is_point_at_infinity():  # (0, 0) is how the library spells it
        raise ValueError("the point at infinity is not a key")
    return key


def _raw(key: ECC.EccKey) -> bytes:
    return b"\x04" + int(key.pointQ.x).to_bytes(32, "big") + int(key.pointQ.y).to_bytes(32, "big")


def encrypt(
    plaintext: bytes,
    ua_public: bytes,
    auth_secret: bytes,
    *,
    salt: bytes | None = None,
    as_private: ECC.EccKey | None = None,
) -> bytes:
    """The body of a push message (RFC 8291, a single `aes128gcm` record) for the browser with this public key and
    authentication secret. `salt` and `as_private` are only given by the test of the RFC's example."""
    if len(plaintext) > 3993:
        raise ValueError("a push message holds at most 3993 bytes")
    ua = _point(ua_public)
    sender = as_private or ECC.generate(curve="P-256")
    as_public = _raw(sender)
    salt = salt or get_random_bytes(16)

    shared = key_agreement(static_priv=sender, eph_pub=ua, kdf=lambda z: z)
    # RFC 8291 3.3: the shared secret and the authentication secret make the input keying material
    secret = HKDF(shared, 32, auth_secret, SHA256, 1, context=b"WebPush: info\x00" + ua_public + as_public)
    cek = HKDF(secret, 16, salt, SHA256, 1, context=b"Content-Encoding: aes128gcm\x00")
    nonce = HKDF(secret, 12, salt, SHA256, 1, context=b"Content-Encoding: nonce\x00")
    cipher = AES.new(cek, AES.MODE_GCM, nonce=nonce)
    body, tag = cipher.encrypt_and_digest(plaintext + b"\x02")  # the delimiter of the last (only) record
    header = salt + (4096).to_bytes(4, "big") + bytes([len(as_public)]) + as_public
    return header + body + tag


def decrypt(message: bytes, ua_private: ECC.EccKey, auth_secret: bytes) -> bytes:
    """What a browser does with a message (for the tests): the plaintext, or ValueError."""
    salt, size, idlen = message[:16], int.from_bytes(message[16:20], "big"), message[20]
    as_public = message[21 : 21 + idlen]
    body = message[21 + idlen :]
    if size < len(body) or not body:
        raise ValueError("a single record is expected")
    ua_public = _raw(ua_private)
    shared = key_agreement(static_priv=ua_private, eph_pub=_point(as_public), kdf=lambda z: z)
    secret = HKDF(shared, 32, auth_secret, SHA256, 1, context=b"WebPush: info\x00" + ua_public + as_public)
    cek = HKDF(secret, 16, salt, SHA256, 1, context=b"Content-Encoding: aes128gcm\x00")
    nonce = HKDF(secret, 12, salt, SHA256, 1, context=b"Content-Encoding: nonce\x00")
    plain = AES.new(cek, AES.MODE_GCM, nonce=nonce).decrypt_and_verify(body[:-16], body[-16:])
    if not plain.endswith(b"\x02"):
        raise ValueError("the padding delimiter is not there")
    return plain[:-1]


def vapid_header(key: ECC.EccKey, endpoint: str, now: float | None = None) -> str:
    """The `Authorization` value of RFC 8292 for a push service: a JWT (ES256) for the origin of the endpoint,
    valid for twelve hours, with the public key beside it."""
    parts = urlsplit(endpoint)
    claims = {
        "aud": f"{parts.scheme}://{parts.netloc}",
        "exp": int((time.time() if now is None else now) + 12 * 3600),
        "sub": VAPID_SUBJECT,
    }
    head = b64e(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    text = head + "." + b64e(json.dumps(claims, separators=(",", ":")).encode())
    signature = DSS.new(key, "fips-186-3").sign(SHA256.new(text.encode()))  # r || s, 64 bytes
    return f"vapid t={text}.{b64e(signature)}, k={b64e(_raw(key))}"


def allowed(endpoint: str, services: tuple[str, ...] = SERVICES) -> bool:
    """Whether this is the address of the push service of a browser: https, no credentials, no odd port."""
    if not endpoint or len(endpoint) > MAX_ENDPOINT:
        return False
    try:
        parts = urlsplit(endpoint)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return False
    if parts.scheme != "https" or parts.username or parts.password or port not in (None, 443):
        return False
    return any(host == s or host.endswith("." + s) for s in services)


class Push:
    def __init__(
        self,
        path: Path,
        name: str,
        *,
        client: httpx.Client | None = None,
        services: tuple[str, ...] = SERVICES,
        translate: Callable[[str, str], str] | None = None,
    ) -> None:
        # httpx logs the address of every request at INFO; the address of a subscription is a capability (see above)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        self.path = path
        self.name = name
        self.services = services
        self._client = client or httpx.Client(timeout=TIMEOUT, follow_redirects=False)
        self.translate: Callable[[str, str], str] = translate or (lambda _lang, text: text)
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="push")
        self._extra: dict[str, Any] = {}  # what a newer version of this file wrote beside what is known is kept
        self._subs: list[dict[str, Any]] = []
        self._key: ECC.EccKey | None = None
        self.newer = False  # a file of a newer schema: left alone, push stays off
        self.error: str | None = None
        self._load()

    # -- the file ----------------------------------------------------------------------------------------------

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            data = None
        except (OSError, ValueError) as error:
            log.warning("push.json could not be read, push stays off: %s", type(error).__name__)
            self.newer = True  # unreadable is treated like newer: it is not written over
            return
        if data is None:
            key = ECC.generate(curve="P-256")
            try:
                self._save(key, [])
            except OSError as error:
                log.warning("push.json could not be written, push stays off: %s", type(error).__name__)
                self.newer = True  # nowhere to keep a key or a subscription: off, and the bridge goes on
                return
            self._key = key
            return
        if not isinstance(data, dict) or not isinstance(data.get("schema"), int) or data["schema"] > SCHEMA:
            self.newer = True
            return
        try:
            self._key = ECC.construct(curve="P-256", d=int.from_bytes(b64d(data["vapid"]["private"]), "big"))
        except (KeyError, TypeError, ValueError):
            log.warning("push.json holds no usable key, push stays off")
            self.newer = True
            return
        self._extra = {k: v for k, v in data.items() if k not in ("schema", "vapid", "subscriptions")}
        self._subs = [s for s in data.get("subscriptions", []) if isinstance(s, dict) and "endpoint" in s]

    def _save(self, key: ECC.EccKey, subs: list[dict[str, Any]]) -> None:
        """Writes the file whole (a new one beside it, then in its place). OSError when it cannot."""
        body = {
            **self._extra,
            "schema": SCHEMA,
            "vapid": {"private": b64e(int(key.d).to_bytes(32, "big"))},
            "subscriptions": subs,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".push-")
        try:
            with os.fdopen(fd, "w") as out:
                json.dump(body, out, indent=1)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    # -- the subscriptions -------------------------------------------------------------------------------------

    @property
    def on(self) -> bool:
        return self._key is not None and not self.newer

    def public_key(self) -> str:
        """The key a browser subscribes with (`applicationServerKey`), in base64url."""
        if self._key is None or self.newer:
            raise RuntimeError("push is off")
        return b64e(_raw(self._key))

    def count(self) -> int:
        with self._lock:
            return len(self._subs)

    def subscribe(self, endpoint: str, p256dh: str, auth: str, lang: str = "en", agent: str = "") -> bool:
        """Keeps a subscription (or renews the one of the same address). False when it is not acceptable."""
        if not self.on or not allowed(endpoint, self.services):
            return False
        try:
            _point(b64d(p256dh))
            if len(b64d(auth)) != 16:
                return False
        except (ValueError, TypeError):
            return False
        with self._lock:
            subs = [s for s in self._subs if s["endpoint"] != endpoint]
            if len(subs) >= MAX_SUBSCRIPTIONS:
                subs = subs[-(MAX_SUBSCRIPTIONS - 1) :]  # the oldest make room
            subs.append(
                {
                    "endpoint": endpoint,
                    "p256dh": p256dh,
                    "auth": auth,
                    "lang": lang[:12],
                    "agent": agent[:80],
                    "added": int(time.time()),
                }
            )
            return self._keep(subs)

    def unsubscribe(self, endpoint: str) -> bool:
        with self._lock:
            kept = [s for s in self._subs if s["endpoint"] != endpoint]
            if len(kept) == len(self._subs):
                return False
            return self._keep(kept)

    def _keep(self, subs: list[dict[str, Any]]) -> bool:
        """Writes `subs` and, only when that worked, takes them as the subscriptions (the lock is held)."""
        if self._key is None:
            return False
        try:
            self._save(self._key, subs)
        except OSError as error:
            log.warning("push.json could not be written: %s", type(error).__name__)
            return False
        self._subs = subs
        return True

    # -- sending -----------------------------------------------------------------------------------------------

    def text(self, event: Event, lang: str) -> dict[str, str]:
        what = self.translate(lang, KINDS[event.kind][0])
        body = f"{what}: {event.subject}" if event.subject else what
        return {"title": self.name, "body": body, "tag": TAGS[event.kind], "url": "/admin/events"}

    def on_event(self, event: Event) -> None:
        """A line of the journal: told to every subscription, without holding the one that wrote it."""
        if event.kind not in KINDS or not self.on:
            return
        with self._lock:
            subs = [dict(s) for s in self._subs]
        for sub in subs:
            try:
                self._pool.submit(self._send, sub, event)
            except RuntimeError:  # the bridge is being swapped for a new one: this one tells nothing more
                return

    def _send(self, sub: dict[str, Any], event: Event) -> None:
        if self._key is None:
            return
        try:
            message = encrypt(
                json.dumps(self.text(event, sub.get("lang", "en")), separators=(",", ":")).encode(),
                b64d(sub["p256dh"]),
                b64d(sub["auth"]),
            )
            reply = self._client.post(
                sub["endpoint"],
                content=message,
                headers={
                    "Authorization": vapid_header(self._key, sub["endpoint"]),
                    "Content-Encoding": "aes128gcm",
                    "Content-Type": "application/octet-stream",
                    "TTL": str(TTL),
                    "Urgency": KINDS[event.kind][1],
                },
            )
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as error:
            self.error = type(error).__name__
            log.warning("a push was not sent: %s", self.error)  # the address is a capability: it is not logged
            return
        if reply.status_code in (404, 410):  # the browser dropped the subscription
            self.unsubscribe(sub["endpoint"])
            return
        if reply.status_code >= 300:
            self.error = str(reply.status_code)
            log.warning("a push service refused a message: %s", reply.status_code)
            return
        self.error = None

    def close(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=False)
        self._client.close()
