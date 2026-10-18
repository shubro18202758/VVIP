"""Contract C1 guard — the checked-in protocol_params_gen must match the
canonical shared/schemas/protocol_params.json exactly.

The canonical JSON is a GENERATOR INPUT ONLY (it lives outside this
service's build context on purpose).  This test regenerates the module
text in memory — via the same renderer scripts/export_protocol_params.py
uses — and asserts byte equality with the checked-in copy, so a hand edit
(or a stale regeneration) fails the tier instead of silently forking the
oracle and the brain.

The test fails CLOSED when the canonical file or the generator script is
missing: an absent source of truth is drift, not a pass.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# Repo root: <repo>/VVIP/services/traffic-oracle/tests/<this file>
TESTS_DIR = Path(__file__).resolve().parent
SERVICE_DIR = TESTS_DIR.parent
REPO_ROOT = SERVICE_DIR.parents[1]  # .../VVIP

CANONICAL_JSON = REPO_ROOT / "shared" / "schemas" / "protocol_params.json"
EXPORT_SCRIPT = REPO_ROOT / "scripts" / "export_protocol_params.py"
GEN_MODULE = (
    SERVICE_DIR / "src" / "traffic_oracle" / "adaptive" / "protocol_params_gen.py"
)


def _load_export_script():
    if not CANONICAL_JSON.is_file():
        pytest.fail(
            f"canonical protocol params missing: {CANONICAL_JSON} "
            "(fail-closed: generator input must exist)"
        )
    if not EXPORT_SCRIPT.is_file():
        pytest.fail(
            f"generator script missing: {EXPORT_SCRIPT} (fail-closed)"
        )
    spec = importlib.util.spec_from_file_location("export_protocol_params", EXPORT_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_generated_module_matches_canonical_json() -> None:
    exporter = _load_export_script()
    import json

    doc = json.loads(CANONICAL_JSON.read_text(encoding="utf-8"))
    expected = exporter.render_module(doc)
    actual = GEN_MODULE.read_text(encoding="utf-8")
    assert actual == expected, (
        "protocol_params_gen.py is stale or hand-edited — regenerate with "
        "'python scripts/export_protocol_params.py' from the repo root"
    )


def test_floor_table_is_complete_over_declared_classes() -> None:
    """Every declared protection class has a floor entry (KeyError guard).

    clamp_advance_closure / candidate_params index the floor table directly;
    a class declared in the enum but missing here would KeyError at runtime.
    """
    from traffic_oracle.adaptive.enums import SecurityTier
    from traffic_oracle.adaptive.policy import ADVANCE_CLOSURE_FLOOR_SEC

    for tier in SecurityTier:
        assert tier in ADVANCE_CLOSURE_FLOOR_SEC, tier
    # The event-agnostic public class exists and has no floor.
    assert ADVANCE_CLOSURE_FLOOR_SEC[SecurityTier.NONE] is None


def test_security_points_cover_every_tier() -> None:
    from traffic_oracle.adaptive.enums import SecurityTier
    from traffic_oracle.adaptive.situation import SECURITY_POINTS

    for tier in SecurityTier:
        assert tier in SECURITY_POINTS, tier
    assert SECURITY_POINTS[SecurityTier.NONE] == 0


def test_canonical_values_are_the_shipping_ones() -> None:
    """The canonical JSON pins today's shipping constants (no retuning)."""
    import json

    doc = json.loads(CANONICAL_JSON.read_text(encoding="utf-8"))
    assert doc["baseline"]["advance_closure_sec"] == 120
    assert doc["baseline"]["congestion_spike_threshold"] == 0.8
    assert doc["baseline"]["gps_poll_interval_sec"] == 1.0
    assert doc["protection_classes"]["Z+"]["advance_closure_floor_sec"] == 180
    assert doc["protection_classes"]["Z"]["advance_closure_floor_sec"] == 120
    assert doc["protection_classes"]["Y"]["advance_closure_floor_sec"] == 60
    assert doc["protection_classes"]["X"]["advance_closure_floor_sec"] is None
    assert doc["protection_classes"]["none"]["advance_closure_floor_sec"] is None


def test_canonical_robust_routing_knob_and_bounds() -> None:
    """The robust-routing (P90) knob is pinned in the canonical JSON and in
    the generated module with the same values — floored, auditable,
    disablable to today-behavior via ``enabled=false``."""
    import json

    from traffic_oracle.adaptive.protocol_params_gen import (
        BASELINE_ROBUST_ROUTING,
        ROBUST_QUANTILE_MAX,
        ROBUST_QUANTILE_MIN,
    )

    doc = json.loads(CANONICAL_JSON.read_text(encoding="utf-8"))
    assert doc["baseline"]["robust_routing"] == {
        "enabled": True, "quantile": 0.9,
        "time_weight": 0.3, "disruption_weight": 0.4,
    }
    assert doc["engineering_bounds"]["robust_quantile_min"] == 0.5
    assert doc["engineering_bounds"]["robust_quantile_max"] == 0.99

    # The generated module materializes the same values (the drift test
    # above already pins byte equality; this pins semantics).
    assert BASELINE_ROBUST_ROUTING == {
        "enabled": True, "quantile": 0.9,
        "time_weight": 0.3, "disruption_weight": 0.4,
    }
    assert ROBUST_QUANTILE_MIN == 0.5
    assert ROBUST_QUANTILE_MAX == 0.99
    # Floor semantics: the clamp can never produce a sub-median objective.
    assert ROBUST_QUANTILE_MIN >= 0.5
    assert ROBUST_QUANTILE_MAX <= 0.99
