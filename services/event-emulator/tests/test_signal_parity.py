"""Parity of the emulator's signal-clock replica with the oracle's own.

The living city and the sweep's fleeing vehicle time stop-line crossings
with tape.movement_states_at / tape.approach_movement.  The e-challan
engine judges those crossings with traffic_oracle.signals.plans.phase_at and
enforcement.rules.read_approach.  When the oracle package is importable
(it is in the repo's gate venv) the two are compared over whole cycles and
over every approach of a set of junction geometries; the test skips
cleanly otherwise.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from event_emulator.tape import approach_movement, movement_states_at

_ORACLE_SRC = Path(__file__).resolve().parents[2] / "traffic-oracle" / "src"
if _ORACLE_SRC.is_dir() and str(_ORACLE_SRC) not in sys.path:
    sys.path.append(str(_ORACLE_SRC))

plans = pytest.importorskip("traffic_oracle.signals.plans")
rules = pytest.importorskip("traffic_oracle.enforcement.rules")

PROGRAMS = [
    (100, 17, [{"green_sec": 47, "movements": ["through"]},
               {"green_sec": 38, "movements": ["cross"]}]),
    (90, 46, [{"green_sec": 44, "movements": ["through"]},
              {"green_sec": 34, "movements": ["ramp", "cross"]}]),
    (110, 85, [{"green_sec": 50, "movements": ["through"]},
               {"green_sec": 42, "movements": ["cross"]}]),
    (60, 5, [{"green_sec": 25, "movements": ["NS"]},
             {"green_sec": 25, "movements": ["EW"]}]),
    (40, 0, [{"green_sec": 30, "movements": ["through"]},
             {"green_sec": 30, "movements": ["cross"]}]),   # over-allocated
]

LEG_SETS = [
    [("primary", 138.0, 1005), ("trunk", 60.0, 1012), ("trunk", 265.0, 1015)],
    [("trunk", 0.0, 1), ("trunk", 180.0, 2), ("primary", 90.0, 3), ("secondary", 270.0, 4)],
    [("motorway", 10.0, 11), ("motorway", 200.0, 12), ("trunk", 100.0, 13)],
    [("secondary", 45.0, 21), ("secondary", 135.0, 22)],
    [("primary", 0.0, 31), ("primary", 95.0, 32), ("primary", 185.0, 33), ("primary", 275.0, 34)],
]


@pytest.mark.parametrize("cycle, offset, phases", PROGRAMS)
def test_phase_clock_matches_the_oracle(cycle, offset, phases):
    base = 1_793_000_000.0
    for k in range(cycle * 2):
        epoch = base + k + 0.37
        ours = movement_states_at(cycle, offset, phases, epoch)
        theirs = plans.phase_at(
            {"cycle_sec": cycle, "offset_sec": offset, "phases": phases}, epoch
        ).movement_states
        assert ours == dict(theirs), (cycle, offset, k)


@pytest.mark.parametrize("legs", LEG_SETS)
@pytest.mark.parametrize("cycle, offset, phases", PROGRAMS[:4])
def test_approach_mapping_matches_read_approach(legs, cycle, offset, phases):
    jlegs = tuple(rules.JunctionLeg(segment_id=sid, road_class=rc, bearing_deg=b)
                  for rc, b, sid in legs)
    signal = rules.SignalContext(
        junction_id=1, cycle_sec=cycle,
        phases=tuple((p["green_sec"], tuple(p["movements"])) for p in phases),
        offset_sec=offset, legs=jlegs,
    )
    ours_legs = [{"segment_id": sid, "road_class": rc, "bearing_deg": b} for rc, b, sid in legs]
    for heading in range(0, 360, 3):
        for _rc, _b, sid in legs:
            theirs = rules.read_approach(float(heading), sid, signal)
            ours = approach_movement(float(heading), ours_legs, phases, segment_id=sid)
            assert ours == (theirs.movement if theirs else None), (heading, sid)
