"""Who may reach the administration pages, from where, under which name: the address of the client (behind a
proxy it is the proxy that is seen), the `Host` header, and whether the browser speaks HTTPS."""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Sequence

Network = ipaddress.IPv4Network | ipaddress.IPv6Network

#: Names that cannot be reached from outside: a DNS rebinding attack needs a name of the attacker's own.
_PRIVATE_SUFFIXES = (".local", ".lan", ".home", ".home.arpa", ".internal", ".localdomain", ".localhost")


def parse_networks(values: Iterable[str]) -> list[Network]:
    """Addresses and networks (`192.168.1.0/24`, `fd00::/8`, `10.0.0.5`); raises ValueError on a bad one."""
    return [ipaddress.ip_network(value.strip(), strict=False) for value in values]


def _address(text: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address((text or "").strip().split("%")[0])
    except ValueError:
        return None


def within(text: str | None, networks: Sequence[Network]) -> bool:
    address = _address(text)
    return address is not None and any(address in network for network in networks)


def client_address(peer: str | None, forwarded: str | None, trusted: Sequence[Network]) -> str:
    """The address of the client. `X-Forwarded-For` is believed only when the peer is a proxy we trust, and read
    from the right: the first address that is not one of ours is the client; a client cannot write past it."""
    if not within(peer, trusted) or not forwarded:
        return peer or "?"
    chain = [part.strip() for part in forwarded.split(",") if part.strip()]
    for part in reversed(chain):
        if _address(part) is None:
            return peer or "?"  # not an address: believe nothing of it
        if not within(part, trusted):
            return part
    return chain[0]


def is_https(scheme: str, peer: str | None, forwarded_proto: str | None, trusted: Sequence[Network]) -> bool:
    if scheme == "https":
        return True
    return within(peer, trusted) and (forwarded_proto or "").split(",")[0].strip().lower() == "https"


def host_ok(header: str | None, allowed: Sequence[str]) -> bool:
    """Whether the `Host` header names this bridge. With a list, only those names. Without one, an address, a
    one-word name or a local suffix (`bridge.lan`): what a rebinding page on the internet cannot be called."""
    host = (header or "").strip().lower()
    if host.startswith("["):  # [::1]:8080
        host = host[1:].partition("]")[0]
    elif host.count(":") == 1:
        host = host.partition(":")[0]
    if not host:
        return False
    host = host.rstrip(".")
    if allowed:
        return host in {name.strip().lower().rstrip(".") for name in allowed}
    return _address(host) is not None or "." not in host or host.endswith(_PRIVATE_SUFFIXES)


def same_site_request(fetch_site: str | None, origin: str | None, host: str | None, forwarded_host: str | None) -> bool:
    """Whether a form was sent by one of our own pages (a page of another site cannot sign in, or set a preference).

    A modern browser says it in `Sec-Fetch-Site`; an older one gives an `Origin` to compare with the `Host` (or the
    one a trusted proxy forwards). A request with neither (a script, a test) is not a browser form and passes.
    """
    if fetch_site:
        return fetch_site.strip().lower() in ("same-origin", "none")
    if not origin:
        return True
    if origin == "null":
        return False
    name = origin.split("://", 1)[-1].lower()
    return name in {(host or "").lower(), (forwarded_host or "").lower()} - {""}
