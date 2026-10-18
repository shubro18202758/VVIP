"""Unit tests for the outbound URL safety policy (constraint: scheme
http/https only; external URLs host-validated, rejecting localhost,
loopback, private and reserved addresses; internal endpoints are config
constants)."""

from __future__ import annotations

import pytest

from event_emulator import safe_http
from event_emulator.safe_http import (
    UnsafeUrlError,
    assert_internal_endpoint,
    host_is_public,
    validate_external_url,
)


def _resolve_literally(host: str) -> list[str]:
    """Monkeypatch target: IP literals pass through; 'localhost' resolves to
    loopback (as it does on every real host); other names resolve to a fixed
    PUBLIC address so positive cases need no network."""
    import ipaddress

    try:
        ipaddress.ip_address(host)
        return [host]
    except ValueError:
        pass
    if host == "localhost" or host.endswith(".localhost") or host == "localhost.localdomain":
        return ["127.0.0.1"]
    return ["93.184.216.34"]


@pytest.fixture(autouse=True)
def no_dns(monkeypatch):
    monkeypatch.setattr(safe_http, "resolve_host", _resolve_literally)


class TestSchemePolicy:
    @pytest.mark.parametrize("url", [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "gopher://example.com",
        "javascript:alert(1)",
        "data:text/html,hi",
    ])
    def test_non_http_schemes_rejected(self, url):
        with pytest.raises(UnsafeUrlError, match="[Ss]cheme"):
            validate_external_url(url)

    def test_http_and_https_only(self):
        assert validate_external_url("http://example.com/x").startswith("http://")
        assert validate_external_url("https://example.com/y").startswith("https://")


class TestExternalHostPolicy:
    @pytest.mark.parametrize("url", [
        "http://localhost/api",                       # localhost name
        "http://localhost.localdomain/api",
        "http://127.0.0.1/api",                       # v4 loopback
        "http://127.255.255.254/api",                 # loopback /8
        "http://[::1]/api",                           # v6 loopback
        "http://10.0.0.5/api",                        # private /8
        "http://172.16.1.2/api",                      # private /12
        "http://192.168.1.2/api",                     # private /16
        "http://169.254.169.254/latest/meta-data",    # link-local (cloud metadata)
        "http://0.0.0.0/api",                         # unspecified
        "http://224.0.0.1/api",                       # multicast
        "http://240.0.0.1/api",                       # reserved
        "http://[fc00::1]/api",                       # v6 private
        "http://[fe80::1]/api",                       # v6 link-local
    ])
    def test_forbidden_hosts_rejected(self, url):
        with pytest.raises(UnsafeUrlError):
            validate_external_url(url)

    @pytest.mark.parametrize("url", [
        "http://example.com/api",
        "https://traffic.example.in/api",
        "http://8.8.8.8/api",
        "https://203.0.113.10/api",       # TEST-NET-3 is 'reserved'? no —
        # (documentation ranges: 203.0.113.0/24 IS reserved; kept out below)
    ])
    def test_public_hosts_pass(self, url):
        if url.startswith("https://203"):
            pytest.skip("documentation range is reserved — see reject test")
        assert validate_external_url(url) == url

    def test_documentation_range_is_reserved(self):
        with pytest.raises(UnsafeUrlError):
            validate_external_url("http://203.0.113.10/api")

    def test_credentials_rejected(self):
        with pytest.raises(UnsafeUrlError, match="credentials"):
            validate_external_url("http://user:pass@example.com/api")

    def test_empty_rejected(self):
        with pytest.raises(UnsafeUrlError):
            validate_external_url("")

    def test_dns_rebinding_all_addresses_checked(self, monkeypatch):
        # a hostname resolving to BOTH a public and a private IP is rejected
        monkeypatch.setattr(
            safe_http, "resolve_host", lambda host: ["93.184.216.34", "10.1.2.3"]
        )
        with pytest.raises(UnsafeUrlError, match="private"):
            validate_external_url("http://dual-homed.example.com/api")


class TestInternalEndpointPolicy:
    def test_loopback_constants_allowed(self):
        assert assert_internal_endpoint("http://localhost:8081").endswith(":8081")
        assert assert_internal_endpoint("http://host.docker.internal:8081")

    def test_public_host_not_an_internal_constant(self):
        with pytest.raises(UnsafeUrlError, match="allowlist"):
            assert_internal_endpoint("http://oracle.example.in:8081")

    def test_bad_scheme_not_an_internal_constant(self):
        with pytest.raises(UnsafeUrlError, match="[Ss]cheme"):
            assert_internal_endpoint("ftp://localhost:8081")

    def test_host_is_public_true_for_public(self):
        assert host_is_public("93.184.216.34") is True
