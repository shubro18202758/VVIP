#!/usr/bin/env python3
"""export_protocol_params.py — protocol-params codegen (contract C1).

The canonical definition of the corridor closure protocol parameters is
``VVIP/shared/schemas/protocol_params.json``.  That file is a GENERATOR
INPUT ONLY — it sits outside both service build contexts and is never read
at runtime.  This script writes one checked-in Python module per service:

    services/traffic-oracle/src/traffic_oracle/adaptive/protocol_params_gen.py
    services/convoy-brain/src/convoy_brain/protocol_params_gen.py

Runtime code imports the generated module (already inside each Docker build
context: Dockerfile.traffic-oracle / Dockerfile.convoy-brain COPY src/).
A per-service pytest regenerates the module text in memory from the JSON and
asserts the checked-in copy matches, so the two services can never silently
fork again.

Usage (from anywhere; paths resolve from the repo root):
    python scripts/export_protocol_params.py [--check]

``--check`` regenerates in memory and exits nonzero on drift without writing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_JSON = REPO_ROOT / "shared" / "schemas" / "protocol_params.json"

TARGETS: tuple[tuple[Path, str], ...] = (
    (
        REPO_ROOT / "services" / "traffic-oracle" / "src" / "traffic_oracle"
        / "adaptive" / "protocol_params_gen.py",
        "traffic_oracle.adaptive",
    ),
    (
        REPO_ROOT / "services" / "convoy-brain" / "src" / "convoy_brain"
        / "protocol_params_gen.py",
        "convoy_brain",
    ),
)

_HEADER = '''\
"""GENERATED — DO NOT EDIT BY HAND (contract C1).

Regenerate with:  python scripts/export_protocol_params.py
Canonical source: VVIP/shared/schemas/protocol_params.json (generator input
only; never read at runtime).  A pytest in this service regenerates the
module text and asserts it matches this file — hand edits fail the tier.
"""

from __future__ import annotations

{payload}


__all__ = [
    "ADVANCE_CLOSURE_FLOOR_SEC",
    "BASELINE_ADVANCE_CLOSURE_SEC",
    "BASELINE_CONGESTION_SPIKE_THRESHOLD",
    "BASELINE_GPS_POLL_INTERVAL_SEC",
    "BASELINE_ROBUST_ROUTING",
    "BASELINE_SCORING_WEIGHTS",
    "CONGESTION_THRESHOLD_MAX",
    "CONGESTION_THRESHOLD_MIN",
    "GPS_POLL_MAX_SEC",
    "GPS_POLL_MIN_SEC",
    "MIN_LANES",
    "PROTECTION_CLASS_ORDER",
    "ROBUST_QUANTILE_MAX",
    "ROBUST_QUANTILE_MIN",
    "SECURITY_POINTS",
]
'''


def _fmt_float(value: float) -> str:
    text = repr(float(value))
    return text


def _fmt_scalar(value: float | bool) -> str:
    """Render a JSON scalar (bool or number) as a Python literal."""
    if isinstance(value, bool):
        return repr(value)
    return _fmt_float(value)


def render_module(doc: dict) -> str:
    """Deterministic module text from the canonical JSON document."""
    baseline = doc["baseline"]
    bounds = doc["engineering_bounds"]
    classes = doc["protection_classes"]

    order = list(classes.keys())
    floors: list[str] = []
    points: list[str] = []
    lanes: list[str] = []
    for code in order:
        entry = classes[code]
        floor = entry["advance_closure_floor_sec"]
        floors.append(f'    "{code}": {floor if floor is not None else "None"},')
        points.append(f'    "{code}": {int(entry["security_points"])},')
        lanes.append(f'    "{code}": {int(entry["min_lanes"])},')

    weights = baseline["scoring_weights"]
    weight_lines = "\n".join(
        f'        "{key}": {_fmt_float(value)},' for key, value in weights.items()
    )

    robust = baseline["robust_routing"]
    robust_lines = "\n".join(
        f'        "{key}": {_fmt_scalar(value)},' for key, value in robust.items()
    )

    payload = f'''# ---------------------------------------------------------------------------
# Baseline constants — identical across every service (fail-closed floor)
# ---------------------------------------------------------------------------

BASELINE_ADVANCE_CLOSURE_SEC: int = {int(baseline["advance_closure_sec"])}
BASELINE_CONGESTION_SPIKE_THRESHOLD: float = {_fmt_float(baseline["congestion_spike_threshold"])}
BASELINE_GPS_POLL_INTERVAL_SEC: float = {_fmt_float(baseline["gps_poll_interval_sec"])}
BASELINE_SCORING_WEIGHTS: dict[str, float] = {{
{weight_lines}
}}

#: Robust-routing objective knob (P90 tail costs): ``quantile`` selects the
#: tail used for the time/disruption cost tables (clamped to
#: [ROBUST_QUANTILE_MIN, ROBUST_QUANTILE_MAX] — never a sub-median or a
#: degenerate >0.99 tail); the two weights are the robust objective's
#: time/disruption coefficients; ``enabled=false`` reverts the router to
#: today's point objective exactly (auditable one-key disable).
BASELINE_ROBUST_ROUTING: dict[str, float | bool] = {{
{robust_lines}
}}

# ---------------------------------------------------------------------------
# Hard engineering bounds applied to every published parameter set
# ---------------------------------------------------------------------------

GPS_POLL_MIN_SEC: float = {_fmt_float(bounds["gps_poll_min_sec"])}
GPS_POLL_MAX_SEC: float = {_fmt_float(bounds["gps_poll_max_sec"])}
CONGESTION_THRESHOLD_MIN: float = {_fmt_float(bounds["congestion_threshold_min"])}
CONGESTION_THRESHOLD_MAX: float = {_fmt_float(bounds["congestion_threshold_max"])}
ROBUST_QUANTILE_MIN: float = {_fmt_float(bounds["robust_quantile_min"])}
ROBUST_QUANTILE_MAX: float = {_fmt_float(bounds["robust_quantile_max"])}

# ---------------------------------------------------------------------------
# Per-protection-class protocol tables (protection classes generalize the
# legacy VVIP tier axis: 'none' is a public/civic event with no protectee).
# ---------------------------------------------------------------------------

#: Order from mildest to most severe (rank index).
PROTECTION_CLASS_ORDER: tuple[str, ...] = {tuple(order)!r}

#: Protocol floor on advance_closure_sec by protection class (None = no floor).
ADVANCE_CLOSURE_FLOOR_SEC: dict[str, int | None] = {{
{chr(10).join(floors)}
}}

#: Severity points contributed to the situation score by protection class.
SECURITY_POINTS: dict[str, int] = {{
{chr(10).join(points)}
}}

#: Minimum escortable lanes by protection class (route validation floor).
MIN_LANES: dict[str, int] = {{
{chr(10).join(lanes)}
}}
'''
    return _HEADER.format(payload=payload)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="verify targets match the canonical JSON; no writes")
    args = parser.parse_args()

    if not CANONICAL_JSON.is_file():
        print(f"ERROR: canonical protocol params missing: {CANONICAL_JSON}",
              file=sys.stderr)
        return 2
    doc = json.loads(CANONICAL_JSON.read_text(encoding="utf-8"))
    expected = render_module(doc)

    drift = False
    for target, _package in TARGETS:
        if args.check:
            actual = target.read_text(encoding="utf-8") if target.is_file() else None
            if actual != expected:
                print(f"DRIFT: {target.relative_to(REPO_ROOT)} does not match "
                      f"{CANONICAL_JSON.relative_to(REPO_ROOT)} — regenerate with "
                      "python scripts/export_protocol_params.py", file=sys.stderr)
                drift = True
            else:
                print(f"OK: {target.relative_to(REPO_ROOT)} matches canonical JSON")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(expected, encoding="utf-8")
            print(f"wrote {target.relative_to(REPO_ROOT)}")
    if drift:
        return 1

    if not args.check:
        # Fail loudly if a generated module does not even compile.
        for target, _package in TARGETS:
            compile(expected, str(target), "exec")
        print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
