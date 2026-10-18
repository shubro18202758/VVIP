"""URL validation for outbound HTTP (SSRF guard) — pure unit tests.

External-input URLs must reject: non-http(s) schemes, localhost names,
loopback/private/reserved/link-local/multicast literal IPs, hosts that
RESOLVE into those ranges, userinfo, and bad ports. Fixed internal
endpoints (127.0.0.1:8085 etc.) are accepted only under the internal
policy. Redirects are refused and http is IP-pinned.
"""

from __future__ import annotations

import pytest

from embedding_service import config
from embedding_service.security import (
    UnsafeUrlError,
    prepare_validated_request,
    validate_external_url,
    validate_internal_url,
)


class TestScheme:
    @pytest.mark.parametrize(
        "url",
        [
            "ftp://example.com/file",
            "file:///etc/passwd",
            "gopher://example.com:70/x",
            "data:text/html,hello",
            "javascript:alert(1)",
            "example.com/no-scheme",
        ],
    )
    def test_non_http_schemes_rejected(self, url):
        with pytest.raises(UnsafeUrlError, match="scheme"):
            validate_external_url(url)

    def test_https_allowed(self):
        fake_resolver = lambda host: ["93.184.216.34"]  # noqa: E731
        assert (
            validate_external_url("https://example.com/x", fake_resolver)
            == "https://example.com/x"
        )


class TestExternalHosts:
    def _resolver_for(self, mapping: dict):
        def resolve(host):
            if host in mapping:
                return mapping[host]
            raise OSError(f"no such host {host}")

        return resolve

    @pytest.mark.parametrize(
        "host",
        [
            "localhost",
            "127.0.0.1",
            "0.0.0.0",
            "10.1.2.3",
            "192.168.1.10",
            "172.16.0.5",
            "169.254.169.254",  # cloud metadata
            "100.64.0.1",  # CGNAT reserved
            "224.0.0.1",  # multicast
            "255.255.255.255",
            "::1",
            "fd00::1",  # private v6
            "fe80::1",  # link-local v6
        ],
    )
    def test_dangerous_literal_hosts_rejected(self, host):
        url = f"http://{host}/x"
        with pytest.raises(UnsafeUrlError):
            validate_external_url(url)

    @pytest.mark.parametrize("host", ["stats.internal", "db.local", "x.localhost"])
    def test_local_names_rejected(self, host):
        with pytest.raises(UnsafeUrlError):
            validate_external_url(f"http://{host}/x")

    def test_resolves_to_private_rejected(self):
        resolver = self._resolver_for({"rebind.example.com": ["203.0.113.9", "10.0.0.7"]})
        with pytest.raises(UnsafeUrlError, match="non-global"):
            validate_external_url("http://rebind.example.com/x", resolver)

    def test_all_global_addresses_accepted(self):
        resolver = self._resolver_for({"ok.example.com": ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"]})
        assert (
            validate_external_url("http://ok.example.com/x", resolver)
            == "http://ok.example.com/x"
        )

    def test_unresolvable_rejected(self):
        with pytest.raises(UnsafeUrlError, match="resolve"):
            validate_external_url("http://nonexistent.invalid/x", self._resolver_for({}))

    def test_userinfo_rejected(self):
        with pytest.raises(UnsafeUrlError, match="userinfo"):
            validate_external_url("http://user:pass@example.com/x")

    def test_port_zero_rejected(self):
        with pytest.raises(UnsafeUrlError, match="port"):
            validate_external_url("http://example.com:0/x", self._resolver_for({"example.com": ["93.184.216.34"]}))


class TestInternalPolicy:
    def test_fixed_embedding_endpoint_accepted(self):
        assert (
            validate_internal_url(config.EMBEDDING_SERVICE_URL)
            == config.EMBEDDING_SERVICE_URL
        )

    @pytest.mark.parametrize(
        "url",
        [
            "http://169.254.169.254/latest/meta-data",  # not on allowlist
            "http://8.8.8.8:8085/x",  # external host, internal policy
            "ftp://127.0.0.1:8085/x",
        ],
    )
    def test_internal_policy_still_rejects_bad_targets(self, url):
        with pytest.raises(UnsafeUrlError):
            validate_internal_url(url)

    def test_internal_rejects_unlisted_localhost_port_scan_targets(self):
        # host must be on allowlist even for loopback literals
        with pytest.raises(UnsafeUrlError):
            validate_internal_url("http://127.0.0.2:8085/x")


class TestHardening:
    def test_http_url_pinned_to_validated_ip(self):
        resolver = lambda host: ["93.184.216.34"]  # noqa: E731
        prepared = prepare_validated_request(
            "http://example.com:8081/path?q=1",
            resolver=resolver,
        )
        assert prepared.url == "http://93.184.216.34:8081/path?q=1"
        assert prepared.headers["Host"] == "example.com:8081"

    def test_https_url_keeps_hostname_for_tls(self):
        resolver = lambda host: ["93.184.216.34"]  # noqa: E731
        prepared = prepare_validated_request("https://example.com/x", resolver=resolver)
        assert prepared.url == "https://example.com/x"
        assert "Host" not in prepared.headers

    async def test_fetch_refuses_redirect_target(self):
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Redirector(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:9/x")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Redirector)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            from embedding_service.security import fetch_json

            # Loopback redirector must be reachable only via internal policy;
            # the response redirect to another loopback target must REFUSE.
            with pytest.raises(UnsafeUrlError, match="redirect"):
                await fetch_json(f"http://127.0.0.1:{port}/hop", internal=True)
        finally:
            server.shutdown()

    async def test_fetch_internal_end_to_end(self):
        import json as _json
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Echo(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                payload = _json.loads(body) if body else {}
                blob = _json.dumps({"echo": payload}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(blob)))
                self.end_headers()
                self.wfile.write(blob)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Echo)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            from embedding_service.security import fetch_json

            result = await fetch_json(
                f"http://127.0.0.1:{port}/search",
                method="POST",
                payload={"query": "hi"},
                internal=True,
            )
            assert result == {"echo": {"query": "hi"}}
        finally:
            server.shutdown()
