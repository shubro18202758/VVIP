"""Deck edge regression (gate-venv python): proxy guard, hardening, operator gate.

The edge itself is TypeScript (src/hooks.server.ts + the operator-session
and public-projection modules), so this module has two rungs:

* SOURCE CONTRACT (pure python, no node): the authority-pinning refusal
  exists and runs BEFORE the upstream fetch (the live-confirmed open proxy:
  /api/convoy//127.0.0.1:8091/health once fetched the traffic-oracle through
  the brain rule), the upstream targets stay pinned loopback literals, and
  the WebSocket 501 answer precedes any proxying.
* BEHAVIOUR (node): bundles tests/ops-guard.entry.ts with the vendored
  esbuild and runs it — authority pinning, 501-on-upgrade, header hygiene,
  the nonce CSP, the opt-in VEER_OPERATOR_PIN gate (default-deny on the
  canonical, once-decoded path: %-encoded operator feeds, the interception
  plan and the raw events feed refused; the anonymous ambulance request
  capped per client) and its /api/session sign-in, the public projection's
  allowlist and hold provenance, the doctrine window timing, and the C-D
  chat parser. The suite stubs fetch, so nothing leaves the process.
  Equivalent manual run from services/command-deck:

      node_modules/.bin/esbuild tests/ops-guard.entry.ts --bundle \\
          --platform=node --format=esm --alias:$lib=./src/lib \\
          --outfile=tests/artifacts/ops-guard-bundle.mjs \\
          && node tests/artifacts/ops-guard-bundle.mjs

The behaviour rung skips (with the reason) only when node or the vendored
esbuild is absent; it never passes vacuously.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
DECK_ROOT = HERE.parent
HOOKS_TS = DECK_ROOT / "src" / "hooks.server.ts"
ENTRY = HERE / "ops-guard.entry.ts"
ESBUILD = DECK_ROOT / "node_modules" / ".bin" / "esbuild"


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
    """The proxy targets must stay loopback literals on the live ports.

    The origin check compares against ``rule.target`` — if a target ever
    stops being a loopback constant the pin itself becomes the hole.
    """
    source = HOOKS_TS.read_text(encoding="utf-8")

    for env_name, default in (
        ("ORACLE_PORT", "8091"),
        ("BRAIN_PORT", "8092"),
        ("EMULATOR_PORT", "8090"),
    ):
        assert f"http://127.0.0.1:${{process.env.{env_name} ?? '{default}'}}" in source, (
            f"proxy target for {env_name} is no longer a pinned 127.0.0.1 constant "
            f"defaulting to the live port {default}"
        )


def test_upgrade_answered_before_proxying() -> None:
    """WebSocket upgrades get 501 at the top of handle(), before any proxy."""
    source = HOOKS_TS.read_text(encoding="utf-8")
    handle_at = source.index("export const handle")
    upgrade_at = source.index("isUpgrade(event.request)", handle_at)
    proxy_at = source.index("await proxy(event, rule", handle_at)
    assert upgrade_at < proxy_at, "the 501 upgrade answer must precede proxying"


def test_guard_bundle_behaviour(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None or not ESBUILD.exists():
        pytest.skip("node or the vendored esbuild (node_modules/.bin/esbuild) is not installed")

    bundle = tmp_path / "ops-guard-bundle.mjs"
    build = subprocess.run(
        [
            str(ESBUILD),
            str(ENTRY),
            "--bundle",
            "--platform=node",
            "--format=esm",
            "--alias:$lib=./src/lib",
            "--log-level=warning",
            f"--outfile={bundle}",
        ],
        cwd=DECK_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert build.returncode == 0, f"esbuild failed:\n{build.stdout}\n{build.stderr}"

    run = subprocess.run(
        [node, str(bundle)],
        cwd=DECK_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    output = run.stdout + run.stderr
    assert run.returncode == 0, f"guard suite failed:\n{output[-4000:]}"
    assert "all guard assertions passed" in run.stdout
    # The suite must actually exercise every rung, not pass vacuously.
    for marker in (
        "ok proxy-refuse",
        "ok ws-upgrade answered 501",
        "ok page: CSP present",
        "ok gate on, anonymous POST /api/v1/enforcement",
        "ok gate on, anonymous GET /api/v1/%76ehicles → 403",
        "ok gate on, anonymous GET /api/v1/events/4508d86d-566b-438f-8414-b3d67fd7e6e4/interception-plan → 403",
        "ok gate on: anonymous ambulance requests capped per client",
        "ok gate on: malformed session cookie → 403, never 500",
        "ok session: right PIN",
        "ok projection: vehicle_pursuit never projected",
        'ok feed: oracle "baseline" params stay labelled baseline',
        "ok window: unit beyond one window",
        "ok writer: no citizen all-clear for a pursuit",
        "ok C-D: tool_call",
    ):
        assert marker in run.stdout, f"guard suite never reached: {marker}"
