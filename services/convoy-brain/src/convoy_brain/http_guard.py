"""Outbound HTTP safety guard for convoy-brain.

Two distinct policies:

1. **Platform service endpoints** (model-host :8083, llama-server :8084,
   embedding service :8085, traffic-oracle :8081) are compile-time
   configuration constants of the platform itself — they come from
   ``config.load_service_urls()`` and must be plain http(s) on a loopback or
   host.docker.internal host. :func:`assert_platform_service_url` enforces
   that nothing else (a stray public host, a non-HTTP scheme) sneaks in via
   environment overrides.

2. **Externally supplied URLs** (anything that originates from user input,
   tool output, or another service's payload) are never fetched by this
   service today — but any future outbound client code MUST pass such URLs
   through :func:`validate_external_url`, which rejects non-http(s) schemes
   and every loopback / private / reserved / link-local / multicast address
   *before* the request is made.

Pure stdlib (``urllib.parse`` / ``ipaddress`` / ``socket``) — unit-testable
without network by monkeypatching :func:`resolve_host`.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

__all__ = [
    "UnsafeUrlError",
    "validate_external_url",
    "assert_platform_service_url",
    "host_is_public",
    "resolve_host",
]


class UnsafeUrlError(ValueError):
    """Raised when a URL fails the outbound safety policy."""


_ALLOWED_SCHEMES = ("http", "https")

#: Hosts the platform's own services are allowed to live on (contract C0:
#: localhost + host.docker.internal allowlist for config-fixed endpoints).
_PLATFORM_ALLOWED_HOSTNAMES = frozenset(
    {"localhost", "host.docker.internal"}
)


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
    try:
        addresses = resolve_host(host)
    except UnsafeUrlError:
        raise
    except OSError as exc:
        raise UnsafeUrlError(f"Cannot resolve host {host!r}: {exc}") from exc
    for addr in addresses:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError as exc:
            raise UnsafeUrlError(f"Resolved non-IP address {addr!r} for host {host!r}") from exc
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
    scheme, host = _check_parts(url)
    host_is_public(host)
    return url.strip()


def assert_platform_service_url(url: str) -> str:
    """Validate a config-fixed platform endpoint (model hosts, oracle).

    These endpoints are the platform's own constants, so loopback is the
    *expected* location — but nothing beyond loopback / host.docker.internal
    is allowed, and the scheme must still be http or https.
    """
    scheme, host = _check_parts(url)
    if host in _PLATFORM_ALLOWED_HOSTNAMES:
        return url
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        raise UnsafeUrlError(
            f"Platform service endpoint must be localhost or host.docker.internal: {url!r}"
        ) from None
    if ip.is_loopback:
        return url
    raise UnsafeUrlError(
        f"Platform service endpoint {url!r} must be a loopback host "
        f"(localhost / 127.0.0.1 / ::1 / host.docker.internal)"
    )
