"""Deck-proxy authority-pinning regression (gate-venv pure python).

The proxy guard itself is TypeScript (src/hooks.server.ts): the SvelteKit
hook builds the upstream URL from the untrusted request path and MUST
refuse any path that would override the rule's fixed loopback authority
(``//host/...`` is protocol-relative under WHATWG URL parsing — the
live-confirmed open proxy: /api/convoy//127.0.0.1:8091/health once fetched
the traffic-oracle through the brain rule).

This module is the pure-python SOURCE-CONTRACT rung (no node, no network):
it pins that the origin check exists in hooks.server.ts and that it runs
BEFORE the upstream fetch. The BEHAVIOURAL rung lives in the node suite —
tests/ops-guard.entry.ts, extended with refuse cases (protocol-relative
and backslash authority overrides, GET+POST) and pinned-origin forward
cases. Run it from the deck root (services/command-deck):

    node_modules/.bin/esbuild tests/ops-guard.entry.ts \\
        --bundle --platform=node --format=esm \\
        --outfile tests/artifacts/ops-guard-bundle.mjs \\
        && node tests/artifacts/ops-guard-bundle.mjs

(a python-side subprocess wrapper for that command is intentionally NOT
used here — the host's security scanner blocks subprocess-executing test
code, so the ladder gets the source contract and the entry keeps the
behaviour.)
"""

from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).resolve().parent
DECK_ROOT = HERE.parent
HOOKS_TS = DECK_ROOT / "src" / "hooks.server.ts"


def test_guard_present_in_source() -> None:
    source = HOOKS_TS.read_text(encoding="utf-8")

    # The authority-pinning refusal, verbatim contract from the fix.
    assert "upstream.origin !== base.origin" in source, (
        "hooks.server.ts lost the upstream-origin pinning guard"
    )
    assert "refused authority-override path" in source, (
        "hooks.server.ts refusal response lost its detail marker"
    )

    # The guard must run BEFORE the upstream fetch — a check placed after
    # the fetch would refuse the response but the request would already
    # have left for the attacker-chosen host.
    guard_at = source.index("upstream.origin !== base.origin")
    fetch_at = source.index("await fetch(upstream, init)")
    assert guard_at < fetch_at, "authority pinning must precede the upstream fetch"


def test_proxy_rules_still_pinned_to_loopback() -> None:
    """The four proxy targets must stay loopback literals.

    The origin check compares against ``rule.target`` — if a target ever
    stops being a loopback constant the pin itself becomes the hole.
    """
    source = HOOKS_TS.read_text(encoding="utf-8")

    for env_name in ("ORACLE_PORT", "BRAIN_PORT", "EMULATOR_PORT"):
        assert f"http://127.0.0.1:${{process.env.{env_name}" in source, (
            f"proxy target for {env_name} is no longer a pinned 127.0.0.1 constant"
        )
