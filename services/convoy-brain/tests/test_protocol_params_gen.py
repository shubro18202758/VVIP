"""Contract C1 guard — the checked-in protocol_params_gen must match the
canonical shared/schemas/protocol_params.json exactly.

Mirrors services/traffic-oracle/tests/test_protocol_params_gen.py: the
canonical JSON is a GENERATOR INPUT ONLY (outside this build context);
this test regenerates the module text in memory via the same renderer the
export script uses and asserts byte equality.  It fails CLOSED when the
canonical file or generator is missing.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

# Repo root: <repo>/VVIP/services/convoy-brain/tests/<this file>
TESTS_DIR = Path(__file__).resolve().parent
SERVICE_DIR = TESTS_DIR.parent
REPO_ROOT = SERVICE_DIR.parents[1]  # .../VVIP

CANONICAL_JSON = REPO_ROOT / "shared" / "schemas" / "protocol_params.json"
EXPORT_SCRIPT = REPO_ROOT / "scripts" / "export_protocol_params.py"
GEN_MODULE = SERVICE_DIR / "src" / "convoy_brain" / "protocol_params_gen.py"


def _load_export_script():
    if not CANONICAL_JSON.is_file():
        pytest.fail(
            f"canonical protocol params missing: {CANONICAL_JSON} "
            "(fail-closed: generator input must exist)"
        )
    if not EXPORT_SCRIPT.is_file():
        pytest.fail(f"generator script missing: {EXPORT_SCRIPT} (fail-closed)")
    spec = importlib.util.spec_from_file_location("export_protocol_params", EXPORT_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_generated_module_matches_canonical_json() -> None:
    exporter = _load_export_script()
    doc = json.loads(CANONICAL_JSON.read_text(encoding="utf-8"))
    expected = exporter.render_module(doc)
    actual = GEN_MODULE.read_text(encoding="utf-8")
    assert actual == expected, (
        "protocol_params_gen.py is stale or hand-edited — regenerate with "
        "'python scripts/export_protocol_params.py' from the repo root"
    )


def test_brain_floor_table_includes_public_class() -> None:
    from convoy_brain.config import ADVANCE_CLOSURE_FLOOR_SEC

    assert set(ADVANCE_CLOSURE_FLOOR_SEC) == {"Z+", "Z", "Y", "X", "none"}
    assert ADVANCE_CLOSURE_FLOOR_SEC["none"] is None


def test_brain_baselines_are_the_shipping_constants() -> None:
    from convoy_brain.config import (
        BASE_ADVANCE_CLOSURE_SEC,
        BASE_CONGESTION_SPIKE_THRESHOLD,
        BASE_GPS_POLL_INTERVAL_SEC,
        BASE_SCORING_WEIGHTS,
    )

    assert BASE_ADVANCE_CLOSURE_SEC == 120
    assert BASE_CONGESTION_SPIKE_THRESHOLD == 0.8
    assert BASE_GPS_POLL_INTERVAL_SEC == 1.0
    assert BASE_SCORING_WEIGHTS == {
        "time": 0.3, "disruption": 0.4, "security": 0.2, "complexity": 0.1,
    }


def test_canonical_robust_routing_knob_is_pinned() -> None:
    """The robust-routing (P90 objective) knob lives in the canonical JSON
    and materializes identically into this service's generated module —
    the same floored/auditable/disablable knob the oracle consumes."""
    doc = json.loads(CANONICAL_JSON.read_text(encoding="utf-8"))
    assert doc["baseline"]["robust_routing"] == {
        "enabled": True, "quantile": 0.9,
        "time_weight": 0.3, "disruption_weight": 0.4,
    }
    assert doc["engineering_bounds"]["robust_quantile_min"] == 0.5
    assert doc["engineering_bounds"]["robust_quantile_max"] == 0.99

    from convoy_brain.protocol_params_gen import (
        BASELINE_ROBUST_ROUTING,
        ROBUST_QUANTILE_MAX,
        ROBUST_QUANTILE_MIN,
    )

    assert BASELINE_ROBUST_ROUTING == {
        "enabled": True, "quantile": 0.9,
        "time_weight": 0.3, "disruption_weight": 0.4,
    }
    assert ROBUST_QUANTILE_MIN == 0.5
    assert ROBUST_QUANTILE_MAX == 0.99


def test_floored_advance_closure_none_class_is_signal_priority() -> None:
    from convoy_brain.config import AdaptiveParams

    params = AdaptiveParams(advance_closure_sec=90)
    assert params.floored_advance_closure_sec("none") == 0
    assert params.floored_advance_closure_sec("X") == 0
    assert params.floored_advance_closure_sec("Z+") == 180
