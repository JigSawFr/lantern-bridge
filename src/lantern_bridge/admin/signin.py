# SPDX-FileCopyrightText: 2026 JigSawFr
# SPDX-License-Identifier: MIT
"""Signing in to the pages with something other than an operator's password: a trusted proxy, or OpenID Connect.

Both give a name and groups; the groups become a role through ``[sign_in.roles]`` (the highest role among the groups
that match; none: the person does not get in). Nothing here keeps a token: the OpenID Connect tokens are read once to
learn who signed in, and what stays is a session of the bridge, like any other.

The OpenID Connect flow is the authorization code with PKCE (S256), `state` and `nonce`, the browser held to its own
attempt by a cookie. The identity token comes from the provider's token endpoint over TLS, which OpenID Connect Core
(3.1.3.7) accepts without checking its signature; its issuer, audience, expiry and nonce are checked.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

from lantern_bridge.admin.auth import ROLES, rank
from lantern_bridge.config import Oidc, SignIn

log = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(10.0)
#: How long a sign-in attempt waits for the person to come back from the provider.
ATTEMPT_SECONDS = 600
MAX_ATTEMPTS = 200
DISCOVERY_SECONDS = 3600
SKEW = 60
LOCAL = ("localhost", "127.0.0.1", "::1")

DENIED = "Your single sign-on signed you in, but none of your groups opens these pages. Ask the owner."
FAILED = "The single sign-on did not complete. Try again."


class SignInError(Exception):
    """A sign-in that did not work; ``text`` is one of the fixed texts of the pages (never a token or a reason)."""

    def __init__(self, text: str, why: str = "") -> None:
        super().__init__(why or text)
        self.text = text


@dataclass(frozen=True, slots=True)
class Identity:
    name: str
    groups: tuple[str, ...]


def clean_name(value: object) -> str | None:
    """A name for the pages: 1 to 64 characters, no control character; None when there is none."""
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text if 0 < len(text) <= 64 and text.isprintable() else None


def split_groups(value: object) -> tuple[str, ...]:
    """Groups as a list, or as one text (a proxy sends `a,b`; some providers a list)."""
    if isinstance(value, str):
        items: list[object] = list(value.replace(";", ",").split(","))
    elif isinstance(value, list):
        items = list(value)
    else:
        return ()
    return tuple(dict.fromkeys(g.strip() for g in items if isinstance(g, str) and g.strip()))


def role_for(groups: tuple[str, ...], roles: Mapping[str, str]) -> str | None:
    """The highest role among the groups that are mapped; None when none is."""
    found = [roles[g] for g in groups if roles.get(g) in ROLES]
    return max(found, key=rank) if found else None


def proxy_identity(sign_in: SignIn, headers: Mapping[str, str]) -> Identity | None:
    """Who a trusted proxy says is there. The caller has checked that the request comes from that proxy."""
    if sign_in.proxy_user_header is None:
        return None
    name = clean_name(headers.get(sign_in.proxy_user_header))
    if name is None:
        return None
    return Identity(name, split_groups(headers.get(sign_in.proxy_groups_header)))


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _claims(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise SignInError(FAILED, "the identity token is not a token")
    try:
        document = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except ValueError:
        raise SignInError(FAILED, "the identity token cannot be read") from None
    if not isinstance(document, dict):
        raise SignInError(FAILED, "the identity token holds no claims")
    return document


def _url_ok(value: object) -> bool:
    """An https address, or an http one for the machine itself (a test)."""
    if not isinstance(value, str):
        return False
    parts = urlsplit(value)
    return bool(parts.hostname) and (parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOCAL))


@dataclass(slots=True)
class _Attempt:
    nonce: str
    verifier: str
    binder: str
    expires: float


class OidcSignIn:
    """One provider: sends the browser to it, and turns its answer into an `Identity`."""

    def __init__(self, oidc: Oidc, secret: str | None, client: httpx.Client | None = None) -> None:
        self.oidc = oidc
        self._secret = secret
        self._client = client or httpx.Client(timeout=TIMEOUT, follow_redirects=False)
        self._lock = threading.Lock()
        self._attempts: dict[str, _Attempt] = {}
        self._discovered: tuple[float, dict[str, Any]] | None = None

    @property
    def redirect_uri(self) -> str:
        return f"{self.oidc.public_url}/admin/login/oidc/callback"

    def _discovery(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            if self._discovered is not None and now - self._discovered[0] < DISCOVERY_SECONDS:
                return self._discovered[1]
        try:
            reply = self._client.get(f"{self.oidc.issuer}/.well-known/openid-configuration")
            reply.raise_for_status()
            document = reply.json()
        except (httpx.HTTPError, ValueError) as error:
            raise SignInError(
                FAILED, f"the provider's configuration could not be read: {type(error).__name__}"
            ) from None
        if (
            not isinstance(document, dict)
            or str(document.get("issuer", "")).rstrip("/") != self.oidc.issuer
            or not _url_ok(document.get("authorization_endpoint"))
            or not _url_ok(document.get("token_endpoint"))
        ):
            raise SignInError(FAILED, "the provider's configuration does not match the issuer")
        with self._lock:
            self._discovered = (now, document)
        return document

    def start(self) -> tuple[str, str]:
        """The address to send the browser to, and the value of the cookie that holds it to this attempt."""
        document = self._discovery()
        state, nonce, verifier, binder = (secrets.token_urlsafe(24) for _ in range(4))
        challenge = _b64(hashlib.sha256(verifier.encode()).digest())
        with self._lock:
            now = time.monotonic()
            self._attempts = {s: a for s, a in self._attempts.items() if a.expires > now}
            if len(self._attempts) >= MAX_ATTEMPTS:
                raise SignInError(FAILED, "too many sign-ins at once")
            self._attempts[state] = _Attempt(nonce, verifier, binder, now + ATTEMPT_SECONDS)
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.oidc.client_id,
                "redirect_uri": self.redirect_uri,
                "scope": self.oidc.scopes,
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{document['authorization_endpoint']}?{query}", f"{state}.{binder}"

    def finish(self, code: str, state: str, cookie: str | None) -> Identity:
        """The person who came back with `code`; `cookie` is the value `start` gave. Each attempt serves once."""
        with self._lock:
            attempt = self._attempts.pop(state, None)
        if (
            attempt is None
            or attempt.expires <= time.monotonic()
            or not cookie
            or not secrets.compare_digest(cookie, f"{state}.{attempt.binder}")
        ):
            raise SignInError(FAILED, "the sign-in attempt is unknown, old, or not this browser's")
        document = self._discovery()
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "code_verifier": attempt.verifier,
            "client_id": self.oidc.client_id,
        }
        auth = httpx.BasicAuth(self.oidc.client_id, self._secret) if self._secret else None
        try:
            reply = self._client.post(str(document["token_endpoint"]), data=data, auth=auth or httpx.USE_CLIENT_DEFAULT)
            reply.raise_for_status()
            tokens = reply.json()
        except (httpx.HTTPError, ValueError) as error:
            raise SignInError(FAILED, f"the code was not taken: {type(error).__name__}") from None
        if not isinstance(tokens, dict) or not isinstance(tokens.get("id_token"), str):
            raise SignInError(FAILED, "the provider sent no identity token")
        claims = _claims(tokens["id_token"])
        self._check(claims, attempt.nonce)
        groups = claims.get(self.oidc.groups_claim)
        info: dict[str, Any] = {}
        needs = groups is None or claims.get(self.oidc.name_claim) is None
        if needs and _url_ok(document.get("userinfo_endpoint")) and isinstance(tokens.get("access_token"), str):
            info = self._userinfo(str(document["userinfo_endpoint"]), tokens["access_token"], str(claims["sub"]))
        name = clean_name(
            claims.get(self.oidc.name_claim) or info.get(self.oidc.name_claim) or claims.get("email") or claims["sub"]
        )
        if name is None:
            raise SignInError(FAILED, "the provider gave no usable name")
        return Identity(name, split_groups(groups if groups is not None else info.get(self.oidc.groups_claim)))

    def _check(self, claims: dict[str, Any], nonce: str) -> None:
        audience = claims.get("aud")
        audiences = [audience] if isinstance(audience, str) else audience if isinstance(audience, list) else []
        expires = claims.get("exp")
        problems = (
            str(claims.get("iss", "")).rstrip("/") != self.oidc.issuer,
            self.oidc.client_id not in audiences,
            len(audiences) > 1 and claims.get("azp") != self.oidc.client_id,
            isinstance(expires, bool) or not isinstance(expires, (int, float)) or expires < time.time() - SKEW,
            not isinstance(claims.get("nonce"), str) or not secrets.compare_digest(claims["nonce"], nonce),
            not isinstance(claims.get("sub"), str) or not claims["sub"],
        )
        if any(problems):
            raise SignInError(FAILED, "the identity token is not for this bridge, this attempt, or is out of date")

    def _userinfo(self, endpoint: str, token: str, sub: str) -> dict[str, Any]:
        try:
            reply = self._client.get(endpoint, headers={"Authorization": f"Bearer {token}"})
            reply.raise_for_status()
            info = reply.json()
        except (httpx.HTTPError, ValueError) as error:
            raise SignInError(
                FAILED, f"the provider's user information could not be read: {type(error).__name__}"
            ) from None
        if not isinstance(info, dict) or info.get("sub") != sub:
            raise SignInError(FAILED, "the provider's user information is not about the signed-in person")
        return info
