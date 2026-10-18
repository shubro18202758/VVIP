"""Outbound HTTP safety guard for the event emulator.

Mirrors the platform guard pattern (services/convoy-brain/src/convoy_brain/
http_guard.py) so both services enforce the same outbound policy:

1. **Fixed internal endpoints** (traffic-oracle :8081 — the twin seam) are
   compile-time configuration constants of the platform itself.  They must be
   plain http(s) on a loopback / host.docker.internal host.
   :func:`assert_internal_endpoint` enforces that nothing else sneaks in via
   environment overrides.

2. **Externally supplied URLs** (anything originating from request bodies,
   CLI arguments, tape files, or another service's payload) must pass
   :func:`validate_external_url` BEFORE any request is made: non-http(s)
   schemes, embedded credentials, and every loopback / private / reserved /
   link-local / multicast / unspecified address are rejected.

The emulator's HTTP client funnels every request through :class:`SafeHttpClient`,
which applies policy (1) to its configured internal constants and policy (2)
to everything else — there is no unguarded outbound path.

Pure stdlib (urllib.parse / ipaddress / socket) — unit-testable without
network by monkeypatching :func:`resolve_host`.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

__all__ = [
    "UnsafeUrlError",
    "validate_external_url",
    "assert_internal_endpoint",
    "host_is_public",
    "resolve_host",
]


class UnsafeUrlError(ValueError):
    """Raised when a URL fails the outbound safety policy."""


_ALLOWED_SCHEMES = ("http", "https")

#: Hosts the platform's own services may live on (loopback demo topology —
#: contract C5/C6; a hardened deployment proxy story is out of scope).
_PLATFORM_ALLOWED_HOSTNAMES = frozenset({"localhost", "host.docker.internal"})


def resolve_host(host: str) -> list[str]:
    """Resolve a hostname to its address strings.

    Single point of indirection so tests can monkeypatch DNS resolution.
    Returns the literal host itself when it already parses as an IP.
    """
    try:
        ipaddress.ip_address(host)
        return [host]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise UnsafeUrlError(f"Cannot resolve host {host!r}: {exc}") from exc
    return [info[4][0] for info in infos]


def _ip_is_forbidden(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Return a reason string when the address must not be fetched."""
    if ip.is_loopback:
        return "loopback address"
    if ip.is_private:
        return "private address"
    if ip.is_link_local:
        return "link-local address"
    if ip.is_reserved:
        return "reserved address"
    if ip.is_multicast:
        return "multicast address"
    if ip.is_unspecified:
        return "unspecified address"
    return None


def _check_parts(url: str) -> tuple[str, str]:
    """Scheme + host validation shared by both policies."""
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise UnsafeUrlError(
            f"Scheme {parsed.scheme!r} is not allowed (http/https only): {url!r}"
        )
    host = parsed.hostname
    if not host:
        raise UnsafeUrlError(f"URL has no host: {url!r}")
    if parsed.username or parsed.password:
        raise UnsafeUrlError(f"URL must not carry credentials: {url!r}")
    return parsed.scheme, host


def host_is_public(host: str) -> bool:
    """True when every resolved address of ``host`` is globally routable."""
    addresses = resolve_host(host)
    for addr in addresses:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError as exc:
            raise UnsafeUrlError(
                f"Resolved non-IP address {addr!r} for host {host!r}"
            ) from exc
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped  # unwrap ::ffff:a.b.c.d before judging
        reason = _ip_is_forbidden(ip)
        if reason:
            raise UnsafeUrlError(f"Host {host!r} resolves to {addr} — a {reason}")
    return True


def validate_external_url(url: str) -> str:
    """Validate a URL that originated from external input, BEFORE any request.

    Rejects:
        - any scheme other than http/https
        - embedded credentials
        - hosts resolving (in whole or part) to loopback, private, reserved,
          link-local, multicast, or unspecified addresses

    Returns the normalized URL on success; raises :class:`UnsafeUrlError`.
    """
    if not isinstance(url, str) or not url.strip():
        raise UnsafeUrlError("URL must be a non-empty string")
    _scheme, host = _check_parts(url)
    host_is_public(host)
    return url.strip()


def assert_internal_endpoint(url: str) -> str:
    """Assert ``url`` is a legal fixed internal endpoint (config constant).

    Policy: http/https scheme, no credentials, and a hostname on the
    platform allowlist (localhost / host.docker.internal).  Internal
    endpoints do not go through :func:`validate_external_url` precisely
    because they are compile-time constants, not external input — this
    assertion is what keeps an environment override from turning a constant
    into an arbitrary fetch target.
    """
    if not isinstance(url, str) or not url.strip():
        raise UnsafeUrlError("internal endpoint must be a non-empty string")
    _scheme, host = _check_parts(url)
    if host.lower() not in _PLATFORM_ALLOWED_HOSTNAMES:
        raise UnsafeUrlError(
            f"Internal endpoint host {host!r} is not on the platform allowlist "
            f"{sorted(_PLATFORM_ALLOWED_HOSTNAMES)}: {url!r}"
        )
    return url.strip()
