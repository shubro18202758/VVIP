"""HTTP guard unit tests — the SSRF policy for outbound URLs."""

from __future__ import annotations

import pytest

from convoy_brain.http_guard import (
    UnsafeUrlError,
    assert_platform_service_url,
    host_is_public,
    validate_external_url,
)


class TestExternalUrlPolicy:
    def test_rejects_non_http_schemes(self):
        for url in ("file:///etc/passwd", "ftp://example.com/x", "gopher://x", "javascript:alert(1)"):
            with pytest.raises(UnsafeUrlError, match="[Ss]cheme"):
                validate_external_url(url)

    def test_rejects_credentials_in_url(self):
        with pytest.raises(UnsafeUrlError, match="credentials"):
            validate_external_url("http://user:pass@example.com/")

    def test_rejects_empty(self):
        with pytest.raises(UnsafeUrlError):
            validate_external_url("")
        with pytest.raises(UnsafeUrlError):
            validate_external_url(None)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "host",
        [
            "127.0.0.1",
            "127.1.2.3",
            "0.0.0.0",
            "10.0.0.5",
            "192.168.1.10",
            "172.16.0.1",
            "172.31.255.255",
            "169.254.169.254",  # cloud metadata
            "::1",
            "fe80::1",
            "fc00::1",
            "224.0.0.1",  # multicast
            "240.0.0.1",   # reserved
        ],
    )
    def test_rejects_forbidden_literal_hosts(self, host):
        with pytest.raises(UnsafeUrlError):
            validate_external_url("http://" + host + "/path")

    def test_rejects_ipv4_mapped_ipv6_loopback(self):
        with pytest.raises(UnsafeUrlError):
            validate_external_url("http://[::ffff:127.0.0.1]/")

    def test_accepts_public_literal_ip(self):
        assert validate_external_url("http://93.184.216.34/") == "http://93.184.216.34/"
        assert validate_external_url("https://93.184.216.34/x?y=1")

    def test_hostname_resolution_is_checked(self, monkeypatch):
        import convoy_brain.http_guard as guard

        monkeypatch.setattr(guard, "resolve_host", lambda host: ["10.1.2.3"])
        with pytest.raises(UnsafeUrlError, match="private"):
            validate_external_url("http://looks-public.example.com/")

    def test_hostname_with_any_private_resolution_rejected(self, monkeypatch):
        import convoy_brain.http_guard as guard

        # DNS rebinding shape: one public + one private answer -> reject.
        monkeypatch.setattr(
            guard, "resolve_host", lambda host: ["93.184.216.34", "192.168.0.4"]
        )
        with pytest.raises(UnsafeUrlError):
            host_is_public("mixed.example.com")

    def test_unresolvable_host_rejected(self, monkeypatch):
        import convoy_brain.http_guard as guard

        def boom(host):
            raise OSError("dns fail")

        monkeypatch.setattr(guard, "resolve_host", boom)
        with pytest.raises(UnsafeUrlError, match="[Cc]annot resolve"):
            validate_external_url("http://nope.invalid/")

    def test_public_hostname_accepted(self, monkeypatch):
        import convoy_brain.http_guard as guard

        monkeypatch.setattr(guard, "resolve_host", lambda host: ["93.184.216.34"])
        assert validate_external_url("https://ok.example.com/api")


class TestPlatformServiceUrlPolicy:
    """Platform endpoints are config constants: loopback only, http(s) only."""

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:8083",
            "http://127.0.0.1:8084/health",
            "https://localhost:8085",
            "http://localhost:8081",
            "http://host.docker.internal:8083",
            "http://[::1]:8084",
        ],
    )
    def test_accepts_loopback_hosts(self, url):
        assert assert_platform_service_url(url) == url

    def test_rejects_public_platform_host(self):
        with pytest.raises(UnsafeUrlError):
            assert_platform_service_url("http://models.example.com:8083")

    def test_rejects_private_platform_host(self):
        # Even private ranges are disallowed for the fixed service config —
        # the allowlist is loopback + host.docker.internal only.
        with pytest.raises(UnsafeUrlError):
            assert_platform_service_url("http://10.0.0.7:8083")

    def test_rejects_non_http_scheme(self):
        with pytest.raises(UnsafeUrlError):
            assert_platform_service_url("ftp://127.0.0.1:8083")
