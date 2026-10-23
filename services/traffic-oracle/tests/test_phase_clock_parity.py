"""Display-vs-rules phase parity: the canvas ring and the e-challan engine
read ONE clock.

GET /api/v1/signals/state renders each junction's ring from
``signals.plans.active_phase``; R_SIGNAL_VIOLATION reads
``enforcement.rules.phase_state_at``.  They used to be two models (one
never wrapped modulo the cycle, the other anchored on 2026-01-01) and
disagreed for up to 105 of 110 s.  Both are now views over
``signals.plans.phase_at``; these tests sweep every seeded program shape,
every 006 offset, the live store's programs and the writer's calibrated
variants second by second and require ZERO disagreements — including
through the real /signals/state router with a pinned clock.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from traffic_oracle.enforcement import engine
from traffic_oracle.enforcement.rules import SignalContext, phase_state_at
from traffic_oracle.signals import plans as P

REPO = Path(__file__).resolve().parents[3]
M006 = REPO / "services" / "corridor-store" / "migrations" / "006_simulation_runs.sql"

#: Snapshot of the live store's corridor.signal_programs (2026-10-22) —
#: the programs the canvas actually shows.
LIVE_PROGRAMS = [
    (1, 100, '[{"green_sec": 47, "movements": ["through"]}, {"green_sec": 38, "movements": ["cross"]}]', 17),
    (3, 100, '[{"green_sec": 47, "movements": ["through"]}, {"green_sec": 38, "movements": ["cross"]}]', 51),
    (5, 110, '[{"green_sec": 50, "movements": ["through"]}, {"green_sec": 42, "movements": ["cross"]}]', 85),
    (6, 100, '[{"green_sec": 47, "movements": ["through"]}, {"green_sec": 38, "movements": ["cross"]}]', 2),
    (7, 100, '[{"green_sec": 47, "movements": ["through"]}, {"green_sec": 38, "movements": ["cross"]}]', 19),
    (8, 90, '[{"green_sec": 44, "movements": ["through"]}, {"green_sec": 34, "movements": ["ramp", "cross"]}]', 46),
    (9, 90, '[{"green_sec": 44, "movements": ["through"]}, {"green_sec": 34, "movements": ["ramp", "cross"]}]', 63),
    (10, 90, '[{"green_sec": 44, "movements": ["through"]}, {"green_sec": 34, "movements": ["ramp", "cross"]}]', 80),
    (11, 90, '[{"green_sec": 44, "movements": ["through"]}, {"green_sec": 34, "movements": ["ramp", "cross"]}]', 7),
    (13, 90, '[{"green_sec": 44, "movements": ["through"]}, {"green_sec": 34, "movements": ["ramp", "cross"]}]', 41),
    (14, 90, '[{"green_sec": 44, "movements": ["through"]}, {"green_sec": 34, "movements": ["ramp", "cross"]}]', 58),
]

EPOCH0 = 1_793_854_800  # 2026-11-05T05:00:00Z


def _006_templates() -> list[dict]:
    text = M006.read_text()
    found = re.findall(r"'(\{\"cycle_sec\".*?\})'::jsonb", text)
    assert len(found) == 5, "006 must carry its five class templates"
    return [json.loads(f) for f in found]


def _programs() -> list[tuple[int, int, list[dict], int]]:
    """(junction_id, cycle, phases, offset) for every program shape."""
    out = []
    for jid in range(1, 61):  # every 006 offset (j * 17) % cycle
        for tpl in _006_templates():
            cycle = int(tpl["cycle_sec"])
            out.append((jid, cycle, tpl["phases"], (jid * 17) % cycle))
    for jid, cycle, phases, offset in LIVE_PROGRAMS:
        out.append((jid, cycle, json.loads(phases), offset))
    # the city-plan writer's calibrated variants keep cycle + offset and
    # move green between phases: the clock must agree on those too
    for jid, cycle, phases, offset in LIVE_PROGRAMS:
        base = P.JunctionProgram(jid, cycle, json.loads(phases), offset, "osm_assumed")
        for band in ("AMBER", "RED", "BLACK"):
            planned = P.plan_for_junction(base, band)
            if planned is not None:
                out.append((jid, cycle, planned.phases, offset))
    return out


def _disagreements(display: dict, signal: SignalContext, epoch: float) -> list:
    t = datetime.fromtimestamp(epoch, tz=timezone.utc)
    bad = []
    labels = {m for _g, mv in signal.phases for m in mv}
    for m in sorted(labels):
        state, into = phase_state_at(signal, m, t)
        shows_green = (not display["lost"]) and m in display["movements"]
        if (state == "green") != shows_green:
            bad.append((signal.junction_id, epoch, m, state, display))
        if display["movement_states"].get(m) != state:
            bad.append((signal.junction_id, epoch, m, state, display))
        if into != display["sec_into_cycle"]:
            bad.append((signal.junction_id, epoch, m, "position", into, display))
    return bad


def test_display_and_rules_agree_on_every_program_every_second():
    disagreements = []
    checked = 0
    for jid, cycle, phases, offset in _programs():
        signal = SignalContext(
            junction_id=jid, cycle_sec=cycle,
            phases=engine.parse_phases(phases), offset_sec=offset,
        )
        for k in range(2 * cycle):
            for frac in (0.0, 0.5):
                epoch = EPOCH0 + k + frac
                display = P.active_phase(phases, cycle, offset, epoch)
                disagreements.extend(_disagreements(display, signal, epoch))
                checked += 1
    assert checked > 60_000
    assert disagreements == [], disagreements[:5]


def test_lost_time_is_exactly_the_unallocated_cycle():
    """The audit's symptom: junction 5 showed 'lost' 85 of 110 s and
    junction 10 80 of 90 s.  The shared clock shows the programmed lost
    time (18 s and 12 s) — and rules never call a movement green there."""
    for jid, cycle, phases, offset in LIVE_PROGRAMS:
        ph = json.loads(phases)
        lost = sum(
            1 for k in range(cycle)
            if P.active_phase(ph, cycle, offset, EPOCH0 + k)["lost"]
        )
        assert lost == cycle - sum(p["green_sec"] for p in ph), (jid, lost)


class _FakePool:
    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, sql, *args):
        return self._rows


def test_signals_state_router_matches_the_rules(monkeypatch):
    """End to end through GET /api/v1/signals/state (the canvas feed) with
    a pinned clock: every junction's ring equals phase_state_at."""
    from traffic_oracle.signals import router as signals_router_mod

    rows = [
        {
            "junction_id": jid, "cycle_sec": cycle, "phases": phases,
            "offset_sec": offset, "source": "osm_assumed",
            "lon": 72.58, "lat": 23.02,
        }
        for jid, cycle, phases, offset in LIVE_PROGRAMS
    ]
    app = FastAPI()
    app.state.pool = _FakePool(rows)
    app.include_router(signals_router_mod.router)
    client = TestClient(app)
    for k in range(0, 220, 7):
        epoch = EPOCH0 + k + 0.25
        monkeypatch.setattr(
            signals_router_mod, "time", SimpleNamespace(time=lambda e=epoch: e)
        )
        body = client.get("/api/v1/signals/state").json()
        assert body["count"] == len(LIVE_PROGRAMS)
        for j in body["junctions"]:
            jid, cycle, phases, offset = next(
                p for p in LIVE_PROGRAMS if p[0] == j["junction_id"]
            )
            signal = SignalContext(
                junction_id=jid, cycle_sec=cycle,
                phases=engine.parse_phases(phases), offset_sec=offset,
            )
            assert _disagreements(j["active_phase"], signal, epoch) == []
