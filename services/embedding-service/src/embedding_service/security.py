"""Outbound-URL validation for every HTTP client code path in this package.

Constraint (stream mandate): any outbound HTTP request must

  1. use scheme http or https only, and
  2. if the URL came from external input, host-validate it BEFORE the request
     is made and reject localhost, loopback, private, and reserved addresses.

Fixed internal endpoints (the platform's own services on 127.0.0.1 /
host.docker.internal per contract C0) are compile-time config constants, not
user-supplied URLs; they are accepted only when the caller explicitly marks
the URL as internal AND its host is on the config allowlist.

Hardening beyond the minimum constraint (mirrors the SSRF review checklist):

* Redirects are NEVER followed — a 3xx raises instead of being chased, so an
  external host cannot bounce the client onto an internal address.
* DNS rebinding: for ``http://`` the connection is pinned to a literal IP
  that was validated at prepare time (the original hostname travels in the
  ``Host`` header), so the address resolved is the address connected to. For
  ``https://`` the connection stays on the hostname: TLS certificate
  hostname verification then fails closed if DNS later resolves to an
  impostor, which closes the rebinding window for TLS traffic.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

from . import config


class UnsafeUrlError(ValueError):
    """Raised when a URL fails outbound validation."""


# Hook for tests: resolves a hostname to a list of IP strings.
Resolver = Callable[[str], list[str]]


def _default_resolver(hostname: str) -> list[str]:
    infos = socket.getaddrinfo(hostname, None)
    return [info[4][0] for info in infos]


@dataclass(frozen=True)
class UrlPolicy:
    """Which class of URL is permitted for this call site."""

    internal_fixed: bool = False
    """True → host must be on the FIXED_INTERNAL_HOSTS allowlist.

    Use ONLY for URLs built from the compile-time constants in config.py
    (EMBEDDING_SERVICE_URL / TRAFFIC_ORACLE_URL). Never for external input.
    """


@dataclass(frozen=True)
class PreparedRequest:
    """A validated, ready-to-fetch URL plus required extra headers."""

    url: str
    headers: dict[str, str] = field(default_factory=dict)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: return None → urllib raises HTTPError(3xx)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _validate_scheme(url: str) -> None:
    scheme = url.split("://", 1)[0].lower() if "://" in url else ""
    if scheme not in ("http", "https"):
        raise UnsafeUrlError(f"scheme {scheme!r} not allowed (http/https only)")


def _authority(url: str) -> str:
    rest = url.split("://", 1)[1] if "://" in url else url
    return rest.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]


def _split_host_port(authority: str) -> tuple[str, str | None]:
    """Split authority into (host, port_text), rejecting userinfo."""
    if "@" in authority:
        raise UnsafeUrlError("userinfo in URL is not allowed")
    if authority.startswith("["):  # IPv6 literal
        end = authority.find("]")
        if end == -1:
            raise UnsafeUrlError("malformed IPv6 authority")
        host = authority[1:end]
        remainder = authority[end + 1 :]
        port = remainder[1:] if remainder.startswith(":") else None
        return host, port
    if authority.count(":") == 1:
        host, port = authority.rsplit(":", 1)
        return host, (port or None)
    if ":" in authority:
        raise UnsafeUrlError("malformed authority (bare IPv6 must be bracketed)")
    return authority, None


def _reject_non_global_ip(ip_text: str, host: str) -> None:
    try:
        addr = ipaddress.ip_address(ip_text.split("%", 1)[0])
    except ValueError as exc:
        raise UnsafeUrlError(f"bad IP {ip_text!r} for host {host!r}") from exc
    # Explicit reject set — is_global alone is NOT sufficient: e.g. Python
    # reports multicast 224.0.0.1 as global.
    if (
        addr.is_loopback
        or addr.is_private
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
        or not addr.is_global
    ):
        raise UnsafeUrlError(
            f"host {host!r} involves non-global address {ip_text} "
            "(loopback/private/reserved/link-local/multicast/unspecified)"
        )


def _validate_external_host(host: str, resolver: Resolver) -> list[str]:
    """External-input host: must resolve to global unicast addresses only."""
    hostname = host.rstrip(".").lower()
    if (
        hostname == "localhost"
        or hostname.endswith(".localhost")
        or hostname.endswith(".local")
        or hostname.endswith(".internal")
    ):
        raise UnsafeUrlError(f"host {host!r} is a local name")
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        _reject_non_global_ip(str(addr), host)
        return [str(addr)]

    try:
        ips = resolver(host)
    except (socket.gaierror, OSError) as exc:
        raise UnsafeUrlError(f"cannot resolve host {host!r}: {exc}") from exc
    if not ips:
        raise UnsafeUrlError(f"host {host!r} resolved to no addresses")
    for ip in ips:
        _reject_non_global_ip(ip, host)
    return list(ips)


def validate_outbound_url(
    url: str,
    policy: UrlPolicy | None = None,
    resolver: Resolver = _default_resolver,
) -> str:
    """Validate an outbound URL and return it unchanged.

    Raises UnsafeUrlError before any request is made. ``resolver`` is
    injectable for tests; production uses socket.getaddrinfo.
    """
    prepared = prepare_validated_request(url, policy, resolver)
    return prepared.url if policy and policy.internal_fixed else url


def validate_external_url(url: str, resolver: Resolver = _default_resolver) -> str:
    """Validate a URL that came from external input (strictest policy)."""
    prepare_validated_request(url, UrlPolicy(internal_fixed=False), resolver)
    return url


def validate_internal_url(url: str) -> str:
    """Validate a URL built from the platform's fixed endpoint constants."""
    prepare_validated_request(url, UrlPolicy(internal_fixed=True))
    return url


def prepare_validated_request(
    url: str,
    policy: UrlPolicy | None = None,
    resolver: Resolver = _default_resolver,
) -> PreparedRequest:
    """Validate a URL and produce the request that will actually be sent.

    Scheme must be http/https; host and port are validated per policy. For
    plain ``http`` URLs under the external policy the returned URL is pinned
    to a validated literal IP (rebinding-proof) and the original host moves
    into the Host header. Validation and pinning happen here — before any
    connection is opened.
    """
    if not isinstance(url, str) or not url:
        raise UnsafeUrlError("URL must be a non-empty string")
    policy = policy or UrlPolicy(internal_fixed=False)
    _validate_scheme(url)

    authority = _authority(url)
    host, port_text = _split_host_port(authority)
    if not host:
        raise UnsafeUrlError("URL has no host")
    if port_text:
        try:
            port = int(port_text)
        except ValueError as exc:
            raise UnsafeUrlError(f"invalid port {port_text!r}") from exc
        if not 1 <= port <= 65535:
            raise UnsafeUrlError(f"port {port} out of range")

    scheme = url.split("://", 1)[0].lower()
    if policy.internal_fixed:
        if host not in config.FIXED_INTERNAL_HOSTS:
            raise UnsafeUrlError(
                f"internal endpoint host {host!r} is not on the fixed "
                "internal allowlist"
            )
        return PreparedRequest(url=url)

    ips = _validate_external_host(host, resolver)

    # Rebinding-proof connection for plaintext http: swap in a validated IP
    # (port preserved; original host[:port] travels in the Host header).
    if scheme == "http":
        pinned = ips[0]
        if ":" in pinned:  # IPv6 literal needs brackets in a URL
            pinned = f"[{pinned}]"
        port_suffix = f":{port_text}" if port_text else ""
        authority_out = f"{pinned}{port_suffix}"
        host_header = authority  # original host[:port]
        tail = url.split("://", 1)[1]
        path = tail[len(authority):]
        return PreparedRequest(
            url=f"http://{authority_out}{path}", headers={"Host": host_header}
        )
    # https: keep hostname for SNI + certificate verification; the validated
    # resolution above already rejected internal targets, and a later rebind
    # cannot present a valid certificate for this hostname.
    return PreparedRequest(url=url)


# ─── Minimal async HTTP client over urllib (no third-party dep) ───────────────
# Used by embedding_service.client for brain→embedding calls and by the
# traffic-oracle importer. Every request is prepared (validated + pinned)
# immediately before the connection is opened, and redirects are refused.


async def fetch_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict | None = None,
    timeout_sec: float = 10.0,
    internal: bool = False,
    resolver: Resolver = _default_resolver,
) -> dict:
    """Perform a validated HTTP request and return the decoded JSON body.

    ``internal=True`` marks the URL as a fixed platform endpoint (must be on
    the FIXED_INTERNAL_HOSTS allowlist). External URLs (the default) are
    host-resolved and must land on global addresses only; redirects raise.
    """
    policy = UrlPolicy(internal_fixed=internal)
    prepared = prepare_validated_request(url, policy, resolver)

    def _do() -> dict:
        data = None
        headers = {"Accept": "application/json", **prepared.headers}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            prepared.url, data=data, headers=headers, method=method.upper()
        )
        opener = urllib.request.build_opener(_NoRedirectHandler)
        try:
            with opener.open(request, timeout=timeout_sec) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400:
                raise UnsafeUrlError(
                    f"redirect to {exc.headers.get('Location')!r} refused"
                ) from exc
            raise
        return json.loads(body.decode("utf-8")) if body else {}

    return await asyncio.to_thread(_do)


__all__ = [
    "PreparedRequest",
    "UnsafeUrlError",
    "UrlPolicy",
    "fetch_json",
    "prepare_validated_request",
    "validate_external_url",
    "validate_internal_url",
    "validate_outbound_url",
]
