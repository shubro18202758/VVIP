"""Live escort in motion (R9) — GPS, background task, decisions, health feed.

Covers the audit's live-escort findings end to end with fakes:
    - an escort without GPS waits (``waiting_for_gps``) and then FAILS on its
      timeout; it never completes and never marks the movement completed
    - the convoy moving along its route reaches ``completed`` by arrival;
      closures are lifted by route order once passed
    - congestion incidents fire on the RISING EDGE: one decider checkpoint per
      crossing (cooldown, hysteresis), cadence waits instead of rate-limited
      rows, no decider_unavailable alert for cadence
    - the decider only PROPOSES (officer approves every switch): a hold, Plan B
      or re-route — confident or not — is an operator-approval row that a
      named officer approves before it runs (test_escort_authority.py covers
      the rule itself: expiry, rejection, supersede, the HTTP surface)
    - tool failures are isolated; X class never emits closures
    - the HTTP surface (contract C-J): 202 + single flight (409), status,
      stop, REST GPS with C-B mirroring, officer gate, lifecycle (C-F)
    - the health loop feeds /api/v1/adaptive/health for both tiers
    - post-clearance: per-segment baseline, honest "unverified", durations
"""

from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from conftest import FakeContextStore, FakeExecutor, bbox_handler, details_handler, seg
from convoy_brain import config
from convoy_brain.agents.diversion_coordinator import DiversionCoordinatorAgent
from convoy_brain.config import AdaptiveParams
from convoy_brain.decider import DeciderBrain
from convoy_brain.gps_sim import (
    PACE_MAX,
    GpsSimulator,
    RouteGeometry,
    geometry_paces,
    haversine_m,
    segment_line,
    segment_pace,
)
from convoy_brain.llm.decider_client import DeciderAnswer, DeciderUnavailable
from convoy_brain.memory.convoy_context import ConvoyContext, apply_position
from convoy_brain.workflows import live_escort
from convoy_brain.workflows.live_escort import EscortRun, run_live_escort_workflow, utc_iso
from convoy_brain.workflows.post_clearance import run_post_clearance_workflow
from test_agents import StubBridge

EVENT_ID = "11111111-2222-3333-4444-555555555555"

#: A 4 km corridor east along lat 23.02 (3003's geometry is stored
#: reversed on purpose) plus a 6-lane northern bypass 3005-3006 and a
#: 2-lane southern detour 3007-3008 (below the Z lane floor).
NET = {
    3001: seg(3001, (72.550, 23.020), (72.560, 23.020), name="West Arterial"),
    3002: seg(3002, (72.560, 23.020), (72.570, 23.020), name="Central Arterial"),
    3003: seg(3003, (72.580, 23.020), (72.570, 23.020), name="East Arterial"),
    3004: seg(3004, (72.580, 23.020), (72.590, 23.020), name="Terminal Road"),
    3005: seg(3005, (72.570, 23.020), (72.580, 23.030), name="North Bypass"),
    3006: seg(3006, (72.580, 23.030), (72.590, 23.020), name="North Link"),
    3007: seg(3007, (72.570, 23.020), (72.580, 23.010), lanes=2, name="South Lane"),
    3008: seg(3008, (72.580, 23.010), (72.590, 23.020), lanes=2, name="South Link"),
}
ROUTE = [3001, 3002, 3003, 3004]
ALT = [3001, 3002, 3005, 3006]


def _lines(*ids: int) -> dict:
    return {s: segment_line(NET[s]["geometry"]) for s in ids}


def route_geometry(ids=ROUTE) -> RouteGeometry:
    return RouteGeometry(ids, {s: segment_line(NET[s]["geometry"]) for s in ids},
                         speed_limits={s: NET[s]["speed_limit_kmh"] for s in ids},
                         names={s: NET[s]["road_name"] for s in ids})


class ActionDecider:
    """DeciderClient double: the escort's departure question is answered with
    ``departure`` (at ``departure_confidence``), every other choice question
    with ``action``."""

    p95_decide_latency_ms = 100.0

    def __init__(self, action="hold_position", confidence=0.9, error=None,
                 departure="proceed", departure_confidence=0.9):
        self.action = action
        self.confidence = confidence
        self.error = error
        self.departure = departure
        self.departure_confidence = departure_confidence
        self.calls = 0
        self.states: list[str] = []

    async def decide(self, state, questions, request_id=None):
        self.calls += 1
        self.states.append(state)
        if self.error:
            raise self.error
        out = {}
        for name, spec in questions.items():
            if spec["type"] == "choice":
                wanted, confidence = (
                    (self.departure, self.departure_confidence) if name == "departure"
                    else (self.action, self.confidence)
                )
                value = wanted if wanted in spec["options"] else spec["options"][0]
                out[name] = DeciderAnswer(name=name, type="choice", value=value,
                                          probability=0.9, confidence=confidence)
            else:
                out[name] = DeciderAnswer(name=name, type=spec["type"], value=0.1,
                                          probability=0.1, confidence=None)
        return out

    async def health(self, timeout_sec=3.0):
        return {"loaded": True, "p95_decide_latency_ms": 100.0}

    async def warmup(self):
        return True


def live_by_iteration(run: EscortRun, schedule):
    """get_live_traffic double: congestion per segment from ``schedule(iteration)``
    (rows labelled ``data_quality: synthetic`` like the emulated feed's)."""
    def handler(args):
        levels = schedule(run.state.iteration)
        return {"observations": [
            {"segment_id": s, "congestion_idx": levels.get(s, 0.1), "speed_kmh": 30.0,
             "data_quality": "synthetic"}
            for s in args["segment_ids"]
        ]}
    return handler


def after_departure(levels: dict[int, float]):
    """Schedule with a clear corridor at the departure tick (iteration 1)
    and ``levels`` from iteration 2 on — the congestion rises en route."""
    return lambda i: levels if i >= 2 else {}


def network_executor(live=None, routes=None, extra=None) -> FakeExecutor:
    handlers = {
        "query_segment_details": details_handler(NET),
        "query_segments_in_bbox": bbox_handler(NET),
        "get_live_traffic": live or {"observations": []},
        "find_convoy_routes": {"routes": routes or []},
        "plan_diversions": {"diversions": []},
        "get_historical_pattern": {"aggregates": []},
    }
    handlers.update(extra or {})
    return FakeExecutor(routes=handlers)


def seed(store: FakeContextStore, mid="es-1", *, vvip_class="Z", active=(), at_m=None,
         route=ROUTE) -> ConvoyContext:
    ctx = ConvoyContext(movement_id=mid, vvip_class=vvip_class, status="approved")
    ctx.event_id = EVENT_ID
    ctx.decisions_log.append({"phase": "pre_movement", "recommendation": "approved",
                              "primary_segment_ids": list(route), "timestamp": time.time()})
    ctx.active_diversions = [str(s) for s in active]
    if at_m is not None:
        lon, lat, heading = route_geometry(route).point_at(at_m)
        apply_position(ctx, lon, lat, speed_kmh=40.0, heading=heading, t=utc_iso(), source="gps")
    store.contexts[mid] = ctx
    return ctx


class Drive:
    """publish() hook: after every tick the convoy advances ``step_m`` (real GPS)."""

    def __init__(self, store, mid, start_m=0.0, step_m=300.0, stay=False):
        self.store, self.mid, self.d, self.step, self.stay = store, mid, start_m, step_m, stay
        self.hooks = []

    async def __call__(self, run: EscortRun) -> None:
        for hook in self.hooks:
            hook(run)
        if run.route is None or run.finished:
            return
        if not self.stay:
            self.d = min(run.route.length_m, self.d + self.step)
        lon, lat, heading = run.route.point_at(self.d)
        apply_position(self.store.contexts[self.mid], lon, lat, speed_kmh=40.0,
                       heading=heading, t=utc_iso(), source="gps")


class Clock:
    """Monotonic clock double for the loop timings: a Drive hook advances it
    ``step`` seconds per tick, so cooldowns, debounces and hold expiries are
    exercised in exact seconds without real sleeps."""

    def __init__(self, step=1.0, start=1000.0):
        self.now, self.step = start, step

    def __call__(self) -> float:
        return self.now

    def advance(self, _run=None) -> None:
        self.now += self.step


def per_tick(record):
    """A Drive hook that sees each monitoring tick once (publish also runs
    before the first tick and after the last one)."""
    def hook(run: EscortRun) -> None:
        if run.state.iteration and not run.finished:
            record(run)
    return hook


def officer_approves(officer="Duty Officer A", *, approved=None, limit=None, after_iteration=0):
    """A Drive hook: the named officer approves the escort's open proposal
    at each publish (every one, or the first ``limit``) once the loop is
    past ``after_iteration`` — the decider only proposes; this is the
    officer's click (escort authority)."""
    seen = approved if approved is not None else []

    def hook(run: EscortRun) -> None:
        if run.state.iteration < after_iteration:
            return
        for row in list(run.approvals.values()):
            if row["status"] == "pending" and (limit is None or len(seen) < limit):
                seen.append(run.approve(row["approval_id"], "approve", officer))
    return hook


def rows(store, mid="es-1", **match):
    return [r for r in store.contexts[mid].decisions_log
            if all(r.get(k) == v for k, v in match.items())]


async def escort(store, executor, *, run=None, decider=None, max_iterations=40, publish=None,
                 gps_timeout_sec=5.0, cooldown=60.0, hold_max_sec=None, mid="es-1",
                 adaptive=None, lifecycle=None, clear_confirm_sec=0.0, clock=None):
    """Run one escort with zero-wait ticks. ``clear_confirm_sec=0`` makes a
    single clear reading a falling edge (the debounce has its own tests)."""
    run = run or EscortRun(mid)
    adaptive_client = None
    if adaptive is not None:
        class _Adaptive:
            async def fetch(self, **_):
                return adaptive
        adaptive_client = _Adaptive()
    return run, await run_live_escort_workflow(
        movement_id=mid, bridge=StubBridge(fail=True), executor=executor,
        context_store=store, decider_brain=decider, adaptive_client=adaptive_client,
        max_iterations=max_iterations, run=run, publish=publish, poll_interval_sec=0,
        gps_timeout_sec=gps_timeout_sec, incident_cooldown_sec=cooldown,
        hold_max_sec=hold_max_sec, lifecycle=lifecycle, clear_confirm_sec=clear_confirm_sec,
        clock=clock,
    )


# ---------------------------------------------------------------------------
# Route geometry and the emulated walker
# ---------------------------------------------------------------------------


class TestRouteGeometry:
    def test_segments_are_oriented_into_one_travel_ordered_line(self):
        route = route_geometry()
        lons = [p[0] for p in route.points]
        assert lons == sorted(lons)  # 3003 was stored backwards
        assert route.length_m == pytest.approx(4 * 1023.9, rel=0.01)
        assert [r[0] for r in route.ranges] == ROUTE

    def test_locate_projects_a_fix_onto_the_route(self):
        route = route_geometry()
        loc = route.locate(72.565, 23.0201)
        assert loc.segment_id == 3002
        assert loc.distance_m == pytest.approx(1.5 * 1023.9, rel=0.01)
        assert loc.offset_m == pytest.approx(11.1, abs=1.0)
        assert loc.progress == pytest.approx(0.375, abs=0.01)
        lon, lat, heading = route.point_at(loc.distance_m)
        assert (lon, lat) == (pytest.approx(72.565, abs=1e-6), pytest.approx(23.02, abs=1e-6))
        assert heading == pytest.approx(90.0, abs=0.5)

    def test_segments_ahead_and_splice(self):
        route = route_geometry()
        assert route.segments_ahead(1, 1536.0, 1000.0) == [3002, 3003]
        spliced = route.splice(1, [3002, 3005, 3006], source="plan_b",
                               tail_lines=_lines(3005, 3006))
        assert spliced.segment_ids == [3001, 3002, 3005, 3006]
        assert spliced.end == pytest.approx((72.590, 23.020))


class TestGpsSimulator:
    async def test_walks_the_route_to_arrival_with_emulated_fixes(self):
        route = route_geometry()
        fixes = []
        clock = iter(range(100000))

        async def publish(fix):
            fixes.append(fix)

        async def no_sleep(_):
            return None

        sim = GpsSimulator(movement_id="m", get_route=lambda: route, publish=publish,
                           cruise_kmh=45.0, clock=lambda: float(next(clock)), sleep=no_sleep)
        assert await sim.run() == "arrived"
        assert all(f.source == "emulated_gps" for f in fixes)
        assert max(route.locate(f.lon, f.lat).offset_m for f in fixes) < 1.0
        distances = [route.locate(f.lon, f.lat).distance_m for f in fixes]
        assert distances == sorted(distances)
        assert fixes[-1].speed_kmh == 0.0
        assert (fixes[-1].lon, fixes[-1].lat) == pytest.approx(route.end)
        assert {f.speed_kmh for f in fixes[1:-1]} == {45.0}  # 60 km/h limits cap nothing

    async def test_holds_still_while_held_and_stops_for_real_gps(self):
        route = route_geometry()
        fixes = []
        state = {"hold": True, "real": False}

        async def publish(fix):
            fixes.append(fix)
            if len(fixes) == 5:
                state["hold"] = False
            if len(fixes) == 9:
                state["real"] = True

        async def no_sleep(_):
            return None

        clock = iter(range(100000))
        sim = GpsSimulator(movement_id="m", get_route=lambda: route, publish=publish,
                           cruise_kmh=45.0, is_holding=lambda: state["hold"],
                           should_stop=lambda: state["real"],
                           clock=lambda: float(next(clock)), sleep=no_sleep)
        assert await sim.run() == "stopped"
        assert len({(f.lon, f.lat) for f in fixes[:5]}) == 1
        assert all(f.speed_kmh == 0.0 for f in fixes[:5])
        assert fixes[8].lon > fixes[4].lon
        assert len(fixes) == 9  # nothing published after the real fix arrived

    async def test_follows_a_reroute_from_the_spot_reached(self):
        primary = route_geometry()
        bypass = primary.splice(1, [3002, 3005, 3006], source="reroute",
                                tail_lines=_lines(3005, 3006))
        current = {"route": primary}
        fixes = []

        async def publish(fix):
            fixes.append(fix)
            if len(fixes) == 120:  # mid-way along 3002
                current["route"] = bypass

        async def no_sleep(_):
            return None

        clock = iter(range(100000))
        sim = GpsSimulator(movement_id="m", get_route=lambda: current["route"], publish=publish,
                           cruise_kmh=45.0, clock=lambda: float(next(clock)), sleep=no_sleep)
        assert await sim.run() == "arrived"
        assert haversine_m((fixes[119].lon, fixes[119].lat), (fixes[120].lon, fixes[120].lat)) < 20
        assert max(f.lat for f in fixes) == pytest.approx(23.030, abs=1e-4)  # took the bypass


#: 3002 as an OSM-traced road would draw it: the same junctions, bulging
#: ~690 m north of the straight chord the network routes (and the plan
#: prices) it on — drawn ~1.67x its routing length.
BULGE_3002 = [(72.560, 23.020), (72.565, 23.0262), (72.570, 23.020)]


def _bulged_lines(*ids: int) -> dict:
    lines = _lines(*ids)
    if 3002 in lines:
        lines[3002] = list(BULGE_3002)
    return lines


def _polyline_m(points) -> float:
    return sum(haversine_m(a, b) for a, b in zip(points, points[1:], strict=False))


def _epoch_of(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def traced_details(*, paced: bool):
    """query_segment_details double: 3002 on its drawn bulge; ``paced`` adds
    the C-K lengths (schematic ``length_m`` + ``display_length_m``)."""
    plain = details_handler(NET)
    chord = haversine_m((72.560, 23.020), (72.570, 23.020))

    def handler(args):
        doc = plain(args)
        if args["segment_id"] == 3002:
            doc["geometry"] = {"type": "LineString", "coordinates": [list(p) for p in BULGE_3002]}
            doc.pop("length_m", None)
            if paced:
                doc["length_m"] = round(chord, 1)
                doc["display_length_m"] = round(_polyline_m(BULGE_3002), 1)
        return doc
    return handler


class TestRoutingPace:
    """C-K: the escort walks the DRAWN line but keeps the plan's timetable —
    each segment paced by display_length_m / length_m."""

    def _route(self, ids=(3001, 3002, 3003)) -> tuple[RouteGeometry, float]:
        lines = _bulged_lines(*ids)
        pace = _polyline_m(BULGE_3002) / haversine_m((72.560, 23.020), (72.570, 23.020))
        route = RouteGeometry(list(ids), lines,
                              speed_limits={s: NET[s]["speed_limit_kmh"] for s in ids},
                              paces={3002: pace})
        return route, pace

    def test_segment_pace_reads_the_spatial_lengths(self):
        assert segment_pace({"length_m": 5210.0, "display_length_m": 7660.0}) == pytest.approx(
            7660.0 / 5210.0)
        assert segment_pace({"length_m": 5210.0}) == 1.0           # pre-C-K oracle
        assert segment_pace({"length_m": 0, "display_length_m": 10.0}) == 1.0
        assert segment_pace({"length_m": True, "display_length_m": 9.0}) == 1.0
        assert segment_pace({"length_m": 10.0, "display_length_m": 1e6}) == PACE_MAX
        assert geometry_paces({1: {"length_m": 10.0, "display_length_m": 10.0},
                               2: {"length_m": 10.0, "display_length_m": 15.0}}) == {2: 1.5}

    def test_routing_metres_follow_the_plan_lengths(self):
        route, pace = self._route()
        (_s1, a0, a1), (_s2, b0, b1), (_s3, c0, c1) = route.ranges
        assert b1 - b0 == pytest.approx(pace * 1023.9, rel=0.01)  # the drawn bulge
        assert route.routing_length_m == pytest.approx(3 * 1023.9, rel=0.01)
        assert route.routing_m(b0, b1) == pytest.approx(1023.9, rel=0.01)
        assert route.routing_m(a0 + 100.0, b0 + (b1 - b0) / 2) == pytest.approx(
            (a1 - a0 - 100.0) + 1023.9 / 2, rel=0.01)
        assert route.routing_m(c0, b0) == 0.0  # never backwards
        # an advance of R routing metres crosses segment ends at each pace
        at = route.advance(a1 - 100.0, 600.0)
        assert at == pytest.approx(b0 + 500.0 * pace, rel=1e-6)
        assert route.advance(0.0, 1e9) == route.length_m
        # the remaining distance a fix reports is in routing metres
        loc = route.locate(*route.point_at(b0)[:2])
        assert loc.remaining_m == pytest.approx(2 * 1023.9, rel=0.01)
        assert route.summary()["routing_length_m"] == pytest.approx(3 * 1023.9, rel=0.01)

    def test_the_eta_is_the_plan_eta_on_a_traced_segment(self):
        route, pace = self._route()
        plain = RouteGeometry([3001, 3002, 3003], _bulged_lines(3001, 3002, 3003))
        at_36 = route.eta_sec(0.0, lambda _limit: 36.0)  # 10 m/s
        assert at_36 == pytest.approx(3 * 1023.9 / 10.0, rel=0.01)
        assert plain.eta_sec(0.0, lambda _limit: 36.0) == pytest.approx(
            (2 + pace) * 1023.9 / 10.0, rel=0.01)  # unpaced: the drawn metres

    async def test_the_walker_reaches_each_junction_at_the_planned_time(self):
        route, _pace = self._route()
        fixes = []
        clock = iter(range(100000))

        async def publish(fix):
            fixes.append(fix)

        async def no_sleep(_):
            return None

        sim = GpsSimulator(movement_id="m", get_route=lambda: route, publish=publish,
                           cruise_kmh=36.0, clock=lambda: float(next(clock)), sleep=no_sleep)
        assert await sim.run() == "arrived"
        (_s1, _a0, a1), (_s2, _b0, b1), _ = route.ranges
        along = [route.locate(f.lon, f.lat).distance_m for f in fixes]
        # one fix per 1 s tick at 10 m/s of ROUTING distance: J(3001|3002)
        # after ~102 s, J(3002|3003) after ~205 s — the plan's timetable,
        # though the drawn 3002 is ~1.67x longer
        assert next(i for i, d in enumerate(along) if d >= a1 - 0.5) == pytest.approx(102, abs=2)
        assert next(i for i, d in enumerate(along) if d >= b1 - 0.5) == pytest.approx(205, abs=2)
        assert len(fixes) == pytest.approx(3 * 1023.9 / 10.0, abs=3)
        assert max(route.locate(f.lon, f.lat).offset_m for f in fixes) < 1.0  # on the drawn line
        assert {f.speed_kmh for f in fixes[1:-1]} == {36.0}

    async def test_closure_windows_are_priced_on_the_plan_lengths(self, fake_store):
        async def window_3003(paced: bool) -> tuple[float, float]:
            store = FakeContextStore()
            seed(store, at_m=100.0, active=(3003,))
            drive = Drive(store, "es-1", start_m=100.0, stay=True)
            details = traced_details(paced=paced)
            executor = network_executor(extra={"query_segment_details": details})
            run, _ = await escort(store, executor, publish=drive, max_iterations=2)
            (w,) = run.state.to_doc()["active_diversions"]
            return _epoch_of(w["activate_at"]), _epoch_of(w["deactivate_at"])

        drawn_on, drawn_off = await window_3003(paced=False)
        plan_on, plan_off = await window_3003(paced=True)
        # the ~690 m bulge of 3002 is not on the plan's clock: at the fix's
        # 40 km/h it would push 3003's window ~62 s late
        chord = haversine_m((72.560, 23.020), (72.570, 23.020))
        saved = (_polyline_m(BULGE_3002) - chord) / (40.0 / 3.6)
        assert drawn_on - plan_on == pytest.approx(saved, abs=2.0)
        assert drawn_off - plan_off == pytest.approx(saved, abs=2.0)


# ---------------------------------------------------------------------------
# The monitoring loop
# ---------------------------------------------------------------------------


class TestGpsHonesty:
    async def test_escort_without_gps_waits_then_fails_never_completes(self, fake_store):
        seed(fake_store)
        run, result = await escort(fake_store, network_executor(), gps_timeout_sec=0.05,
                                   max_iterations=10_000)
        assert result["status"] == "failed"
        assert result["arrived"] is False and result["escort_complete"] is False
        assert "did NOT complete" in result["detail"]
        assert fake_store.contexts["es-1"].status == "active"  # never "completed"
        assert rows(fake_store, action="waiting_for_gps")
        assert rows(fake_store, type="gps_lost")
        assert not rows(fake_store, action="escort_complete")
        assert run.state.gps == {"source": None, "age_sec": None, "mode": "live"}

    async def test_safety_valve_while_waiting_is_a_stop_not_a_completion(self, fake_store):
        seed(fake_store)
        run, result = await escort(fake_store, network_executor(), max_iterations=3)
        assert result["status"] == "stopped" and result["arrived"] is False
        assert "waiting_for_gps" in result["detail"]
        assert fake_store.contexts["es-1"].status != "completed"

    async def test_stale_fix_reads_as_gps_lost(self, fake_store):
        ctx = seed(fake_store, at_m=100.0)
        ctx.position_t = utc_iso(time.time() - 120)
        run, result = await escort(fake_store, network_executor(), gps_timeout_sec=0.05,
                                   max_iterations=10_000)
        assert result["status"] == "failed"
        assert "120 s old" in rows(fake_store, action="waiting_for_gps")[0]["detail"]
        # The fix age is refreshed while waiting (it matches the detail).
        assert run.state.gps["age_sec"] >= 120.0 and run.state.gps["source"] == "gps"


class TestMotion:
    async def test_convoy_moves_to_arrival_and_closures_lift_in_route_order(self, fake_store):
        seed(fake_store, at_m=0.0, active=(3001, 3003, 3004, 9999))
        drive = Drive(fake_store, "es-1", step_m=350.0)
        seen = []
        drive.hooks.append(lambda run: seen.append(
            (run.state.status, run.state.progress, run.state.current_segment_id,
             [d["segment_id"] for d in run.state.active_diversions])
        ))
        run, result = await escort(fake_store, network_executor(), publish=drive)
        assert result["status"] == "completed" and result["arrived"] is True
        assert result["escort_complete"] is True
        progress = [p for _s, p, _c, _d in seen if p is not None]
        assert progress == sorted(progress) and progress[-1] == 1.0
        assert run.state.eta_sec == 0.0
        # 3001 then 3003 lift once passed, in route order; the destination
        # segment 3004 (the convoy stands on it) and the off-route 9999 stay
        # for post-clearance to restore.
        lifted = [r["segment_ids"] for r in rows(fake_store, action="closures_lifted")]
        assert lifted == [[3001], [3003]]
        assert fake_store.contexts["es-1"].active_diversions == ["3004", "9999"]
        # Arrival does not complete the movement — /clear does.
        assert fake_store.contexts["es-1"].status == "active"
        assert rows(fake_store, action="escort_complete")[0]["arrived"] is True

    async def test_status_document_carries_jit_closure_windows(self, fake_store):
        seed(fake_store, at_m=100.0, active=(9999, 3004, 3003))
        drive = Drive(fake_store, "es-1", start_m=100.0, stay=True)
        run, _ = await escort(fake_store, network_executor(), publish=drive, max_iterations=2)
        doc = run.state.to_doc()
        # Route order (3003 before 3004), the off-route closure last.
        assert [d["segment_id"] for d in doc["active_diversions"]] == [3003, 3004, 9999]
        assert doc["active_diversions"][2]["activate_at"] is None
        window = doc["active_diversions"][0]
        assert window["segment_id"] == 3003 and window["type"] == "partial"
        assert window["state"] == "scheduled"  # ~1.9 km ahead at 40 km/h, Z floor 120 s
        assert window["activate_at"] < window["deactivate_at"]
        assert doc["event_id"] == EVENT_ID
        assert doc["position"]["source"] == "gps" and doc["route"]["segment_ids"] == ROUTE

    async def test_legacy_context_without_a_route_still_monitors(self, fake_store):
        ctx = ConvoyContext(movement_id="es-1", vvip_class="Z", status="active")
        ctx.convoy_position = (72.555, 23.02)
        fake_store.contexts["es-1"] = ctx
        executor = network_executor()
        run, result = await escort(fake_store, executor, max_iterations=2)
        assert result["escort_complete"] is True and result["arrived"] is False
        assert any(name == "query_segments_in_bbox" for name, _ in executor.calls)


def incident_checkpoints(store, mid="es-1"):
    return rows(store, mid, phase="decider_checkpoint", trigger="incident_detected")


class TestIncidents:
    async def test_sustained_congestion_is_one_checkpoint_per_crossing(self, fake_store):
        seed(fake_store, at_m=100.0)
        run = EscortRun("es-1")
        decider = ActionDecider("continue_monitoring")
        executor = network_executor(live=live_by_iteration(run, after_departure({3002: 0.95})))
        drive = Drive(fake_store, "es-1", start_m=100.0, stay=True)
        await escort(fake_store, executor, run=run, publish=drive, max_iterations=6,
                     decider=DeciderBrain(decider, min_interval_sec=0))
        checkpoints = incident_checkpoints(fake_store)
        assert len(checkpoints) == 1 and checkpoints[0]["escort_phase"] == "incident"
        assert decider.calls == 2  # the departure + ONE incident for 5 congested ticks
        crossings = rows(fake_store, action="congestion_crossing")
        assert len(crossings) == 1
        assert crossings[0]["trigger"] == "congestion_threshold_crossed"
        assert crossings[0]["segment_ids"] == [3002]
        # Measured on the synthetic feed: every incident row says so.
        assert crossings[0]["data_quality"] == ["synthetic"] and crossings[0]["simulated"] is True
        assert checkpoints[0]["simulated"] is True
        assert checkpoints[0]["incident_origin"] == "crossing"
        assert not [r for r in fake_store.contexts["es-1"].decisions_log
                    if r.get("type") == "decider_unavailable"]
        assert rows(fake_store, action="continue_monitoring")

    async def test_each_new_rising_edge_after_the_cooldown_is_a_new_checkpoint(self, fake_store):
        seed(fake_store, at_m=100.0)
        run = EscortRun("es-1")
        levels = {2: 0.95, 3: 0.95, 4: 0.2, 5: 0.95, 6: 0.78, 7: 0.95}
        executor = network_executor(
            live=live_by_iteration(run, lambda i: {3002: levels.get(i, 0.1)}))
        decider = ActionDecider("continue_monitoring")
        await escort(fake_store, executor, run=run, max_iterations=7, cooldown=0.0,
                     decider=DeciderBrain(decider, min_interval_sec=0),
                     publish=Drive(fake_store, "es-1", start_m=100.0, stay=True))
        # 2: rise; 4: clears (below 0.8 - 0.05); 5: rise again; 6: 0.78 is inside
        # the hysteresis band, so 7 is NOT a new crossing.
        assert len(incident_checkpoints(fake_store)) == 2
        assert decider.calls == 3  # + the departure
        assert len(rows(fake_store, action="congestion_crossing")) == 2

    async def test_a_crossing_inside_the_cooldown_raises_no_second_checkpoint(self, fake_store):
        seed(fake_store, at_m=100.0)
        run = EscortRun("es-1")
        levels = {2: 0.95, 3: 0.2, 4: 0.95, 5: 0.2, 6: 0.95}
        executor = network_executor(
            live=live_by_iteration(run, lambda i: {3002: levels.get(i, 0.1)}))
        decider = ActionDecider("continue_monitoring")
        await escort(fake_store, executor, run=run, max_iterations=6, cooldown=600.0,
                     decider=DeciderBrain(decider, min_interval_sec=0),
                     publish=Drive(fake_store, "es-1", start_m=100.0, stay=True))
        assert len(incident_checkpoints(fake_store)) == 1

    async def test_silence_neither_raises_nor_clears_an_incident(self, fake_store):
        seed(fake_store, at_m=100.0)
        run = EscortRun("es-1")
        decider = ActionDecider("hold_position")

        def handler(args):
            if run.state.iteration == 1:
                return {"observations": []}  # departure: nothing reporting yet
            if run.state.iteration in (3, 4):
                return {"observations": []}  # silent feed
            return {"observations": [{"segment_id": 3002, "congestion_idx": 0.95,
                                      "speed_kmh": 5.0}]}

        executor = network_executor(live=handler)
        drive = Drive(fake_store, "es-1", start_m=100.0, stay=True)
        drive.hooks.append(officer_approves())
        await escort(fake_store, executor, run=run, max_iterations=6, cooldown=0.0,
                     decider=DeciderBrain(decider, min_interval_sec=0), publish=drive)
        assert len(incident_checkpoints(fake_store)) == 1  # silence at 3-4 was no falling edge
        hold = rows(fake_store, phase="live_escort", action="hold_position")[0]
        assert hold["ordered_by"] == "officer Duty Officer A"  # the decider only proposed it
        assert not rows(fake_store, action="hold_released")  # nor a cleared corridor

    async def test_cadence_wait_instead_of_rate_limited_rows(self, fake_store):
        seed(fake_store, at_m=100.0)
        run = EscortRun("es-1")
        decider = ActionDecider("continue_monitoring")
        brain = DeciderBrain(decider, min_interval_sec=3600)
        brain._mark_checkpoint("es-1")  # e.g. an operator chat turn just used the slot
        executor = network_executor(live=live_by_iteration(run, after_departure({3002: 0.95})))
        drive = Drive(fake_store, "es-1", start_m=100.0, stay=True)
        drive.hooks.append(lambda r: brain._last_checkpoint_at.clear()
                           if r.state.iteration == 3 else None)
        await escort(fake_store, executor, run=run, decider=brain, max_iterations=5,
                     publish=drive)
        checkpoints = rows(fake_store, phase="decider_checkpoint")
        # The departure is a forced single-shot phase transition; the incident
        # waited for its cadence slot, then decided.
        assert [(c["trigger"], c["tier"]) for c in checkpoints] == [
            ("phase_transition", "decider"), ("incident_detected", "decider")]
        assert not [r for r in fake_store.contexts["es-1"].decisions_log
                    if r.get("tier") == "rate_limited" or r.get("type") == "decider_unavailable"]

    async def test_tier0_serves_a_repeat_crossing_without_a_second_decide(self, fake_store):
        from test_novelty_latch import FakeMemoryClient, ScriptedDecider, memory_ok

        seed(fake_store, at_m=100.0)
        run = EscortRun("es-1")
        levels = {2: 0.95, 3: 0.2, 4: 0.95}
        executor = network_executor(
            live=live_by_iteration(run, lambda i: {3002: levels.get(i, 0.1)}))
        scripted = ScriptedDecider(confidence=0.95)
        brain = DeciderBrain(scripted, memory_client=FakeMemoryClient(
            results=[memory_ok(best_score=0.88, doc_id="doc-7")]),
            min_interval_sec=0, tier0_ttl_sec=300.0)
        await escort(fake_store, executor, run=run, decider=brain, max_iterations=4,
                     cooldown=0.0, publish=Drive(fake_store, "es-1", start_m=100.0, stay=True))
        checkpoints = incident_checkpoints(fake_store)
        assert [c["tier"] for c in checkpoints] == ["decider", "tier0_cache_hit"]
        assert checkpoints[1]["tier0_cache"]["status"] == "hit"
        assert scripted.decide_calls == 2  # the departure + the first crossing only
        for row in checkpoints:
            json.dumps(row)


class TestDeparture:
    """The planned -> live phase transition: one decider checkpoint per escort."""

    async def _depart(self, store, departure, *, confidence=0.9, levels=None, alternates=None,
                      max_iterations=3, decider=True, error=None, hook=None):
        seed(store, at_m=100.0)
        run = EscortRun("es-1", officer="Duty Officer A")
        run.alternates = alternates or []
        executor = network_executor(live=live_by_iteration(run, levels or (lambda i: {})))
        double = ActionDecider("continue_monitoring", departure=departure,
                               departure_confidence=confidence, error=error)
        brain = DeciderBrain(double, min_interval_sec=0) if decider else None
        drive = Drive(store, "es-1", start_m=100.0, stay=True)
        if hook:
            drive.hooks.append(hook)
        _, result = await escort(store, executor, run=run, decider=brain,
                                 max_iterations=max_iterations, publish=drive)
        return run, result, double

    async def test_one_departure_checkpoint_on_the_first_fix(self, fake_store):
        run, _, double = await self._depart(fake_store, "proceed")
        departures = rows(fake_store, phase="decider_checkpoint", trigger="phase_transition")
        assert len(departures) == 1 and double.calls == 1
        assert departures[0]["escort_phase"] == "departure" and departures[0]["tier"] == "decider"
        assert "TRIGGER: phase_transition" in double.states[0]
        assert "4-segment primary route" in double.states[0]
        proceed = rows(fake_store, action="departure_proceed")[0]
        assert proceed["ordered_by"] == "decider (decider)"
        assert run.state.last_decision["phase"] == "departure"
        assert run.state.last_decision["action"] == "proceed"
        # The feed the numbers came from is labelled on the status document.
        assert run.state.traffic_feed["data_quality"] == ["synthetic"]
        assert run.state.traffic_feed["simulated"] is True

    async def test_departure_hold_holds_until_the_corridor_clears(self, fake_store):
        run, _, double = await self._depart(
            fake_store, "hold_departure", max_iterations=4,
            levels=lambda i: {3002: 0.95} if i < 3 else {}, hook=officer_approves(),
        )
        # The decider proposed the held departure; the officer approved it.
        proposal = rows(fake_store, type="operator_approval_required")[0]
        assert proposal["trigger"] == "phase_transition" and proposal["kind"] == "hold"
        assert proposal["why"] == (
            "departure: decider (decider) at confidence 0.90 — an officer approves every switch")
        assert proposal["summary"] == (
            "hold the departure until Central Arterial clears (congestion 0.95), up to 180 s")
        hold = rows(fake_store, phase="live_escort", action="hold_position")[0]
        assert hold["ordered_by"] == "officer Duty Officer A"
        assert hold["reason"] == "departure held until congestion 0.95 on Central Arterial clears"
        released = rows(fake_store, action="hold_released")[0]
        assert released["reason"] == "held segment(s) [3002] cleared"
        # The departure decided this crossing: no second checkpoint for it.
        assert not incident_checkpoints(fake_store) and double.calls == 1

    async def test_a_confident_departure_plan_b_is_only_proposed(self, fake_store):
        shown = []
        run, _, _ = await self._depart(fake_store, "activate_plan_b", alternates=[ALT],
                                       levels=lambda i: {3003: 0.95},
                                       hook=per_tick(lambda r: shown.append(
                                           r.state.to_doc()["pending_approval"])))
        assert not rows(fake_store, action="route_switched") and run.route.segment_ids == ROUTE
        proceed = rows(fake_store, action="departure_proceed")[0]
        # The convoy rolls on the officer's launch — the current approved plan.
        assert proceed["ordered_by"] == "officer launch (Duty Officer A)"
        assert proceed["decider_proposes"] == "activate_plan_b"
        pending = shown[0]
        assert pending["kind"] == "plan_b" and pending["tier"] == "decider"
        assert pending["confidence"] == 0.9
        assert pending["proposed"]["segment_ids"] == ALT
        assert pending["summary"].startswith("Plan B via North Bypass, North Link")
        assert pending["expires_at"] > pending["created_at"] and pending["timeout_sec"] == 90.0
        # Nobody answered before the run ended: retired, never executed.
        [row] = run.approvals.values()
        assert row["status"] == "expired" and not run.operator_actions

    async def test_departure_plan_b_switches_spine_once_approved(self, fake_store):
        run, _, _ = await self._depart(fake_store, "activate_plan_b", alternates=[ALT],
                                       levels=lambda i: {3003: 0.95}, hook=officer_approves())
        switched = rows(fake_store, action="route_switched")[0]
        assert switched["source"] == "plan_b" and run.route.segment_ids == ALT
        assert switched["ordered_by"] == "officer Duty Officer A"

    async def test_low_confidence_departure_hold_goes_to_the_operator(self, fake_store):
        run, _, _ = await self._depart(fake_store, "hold_departure", confidence=0.3,
                                       levels=lambda i: {3002: 0.95})
        approval = rows(fake_store, type="operator_approval_required")[0]
        assert approval["trigger"] == "phase_transition"
        assert approval["action"] == "hold_position"
        assert approval["why"].startswith("departure: low confidence (0.30)")
        assert approval["reason"].startswith("AI proposes hold the departure until")
        assert run.hold is None  # not executed without an officer
        proceed = rows(fake_store, action="departure_proceed")[0]
        assert proceed["ordered_by"] == "officer launch (Duty Officer A)"

    async def test_unavailable_decider_proceeds_on_the_officer_launch(self, fake_store):
        await self._depart(fake_store, "proceed",
                           error=DeciderUnavailable("/decide transport error"))
        proceed = rows(fake_store, action="departure_proceed")[0]
        assert proceed["ordered_by"] == "officer launch (Duty Officer A)"
        assert proceed["note"].startswith("strategic tier unavailable")
        assert not rows(fake_store, type="operator_approval_required")  # nothing to approve

    async def test_no_strategic_tier_recommends_a_hold_for_a_congested_route(self, fake_store):
        await self._depart(fake_store, "proceed", decider=False,
                           levels=lambda i: {3004: 0.95})
        approval = rows(fake_store, type="operator_approval_required")[0]
        assert approval["action"] == "hold_position" and approval["tier"] == "none"


class TestDeciderActs:
    async def _incident(self, store, action, *, confidence=0.9, routes=None, alternates=None,
                        error=None, hot=3003, hook=None, max_iterations=4, levels=None):
        seed(store, at_m=600.0, active=(3003, 3004))
        run = EscortRun("es-1")
        run.alternates = alternates or []
        levels = levels or after_departure({hot: 0.95})
        executor = network_executor(live=live_by_iteration(run, levels), routes=routes)
        self.executor = executor
        decider = ActionDecider(action, confidence=confidence, error=error)
        drive = Drive(store, "es-1", start_m=600.0, stay=True)
        if hook:
            drive.hooks.append(hook)
        _, result = await escort(store, executor, run=run, max_iterations=max_iterations,
                                 decider=DeciderBrain(decider, min_interval_sec=0),
                                 publish=drive)
        return run, result, decider

    async def test_hold_position_holds_until_the_corridor_clears(self, fake_store):
        run, _, _ = await self._incident(
            fake_store, "hold_position", hook=officer_approves(),
            levels=lambda i: {3003: 0.95 if 2 <= i < 5 else 0.2}, max_iterations=6,
        )
        hold = rows(fake_store, phase="live_escort", action="hold_position")[0]
        assert hold["ordered_by"] == "officer Duty Officer A" and hold["segment_ids"] == [3003]
        released = rows(fake_store, action="hold_released")
        assert released and released[0]["reason"] == "held segment(s) [3003] cleared"
        assert run.hold is None

    async def test_emergency_reroute_avoids_the_hot_segment_and_passes_the_lane_floor(
        self, fake_store
    ):
        routes = [
            {"route_id": "narrow", "segment_ids": [3002, 3007, 3008], "estimated_time_sec": 150},
            {"route_id": "bypass", "segment_ids": [3002, 3005, 3006], "estimated_time_sec": 160},
        ]
        run, _, _ = await self._incident(fake_store, "emergency_reroute", routes=routes,
                                         hook=officer_approves())
        proposal = rows(fake_store, type="operator_approval_required")[0]
        assert proposal["kind"] == "reroute" and proposal["tier"] == "decider"
        assert proposal["proposed"]["segment_ids"] == [3001, 3002, 3005, 3006]
        assert proposal["proposed"]["closures_lifted"] == [3003, 3004]  # the preview
        assert proposal["proposed"]["avoids"] == [3003]
        switched = rows(fake_store, action="route_switched")[0]
        assert switched["ordered_by"] == "officer Duty Officer A"
        assert switched["source"] == "reroute" and switched["route_id"] == "bypass"
        assert switched["segment_ids"] == [3001, 3002, 3005, 3006]  # 2-lane detour refused for Z
        assert switched["closures_lifted"] == [3003, 3004]           # abandoned segments
        assert run.route.segment_ids == [3001, 3002, 3005, 3006]
        ctx = fake_store.contexts["es-1"]
        assert ctx.selected_route_id == "bypass"
        assert ctx.alerts[-1]["type"] == "emergency_reroute"

    async def test_a_long_detour_goes_to_an_officer_who_can_take_it(self, fake_store):
        # The only path around 3002 loops far north: well over 2x what is left.
        far = {
            3101: seg(3101, (72.550, 23.020), (72.550, 23.060), name="Ring North"),
            3102: seg(3102, (72.550, 23.060), (72.600, 23.060), name="Ring East"),
            3103: seg(3103, (72.600, 23.060), (72.590, 23.020), name="Ring Return"),
        }
        NET.update(far)
        try:
            approvals = []

            def approve(run):
                pending = [a for a in run.approvals.values() if a["status"] == "pending"]
                if pending and not approvals:
                    approvals.append(run.approve(pending[0]["approval_id"], "approve",
                                                 "Duty Officer A"))

            routes = [{"route_id": "ring", "segment_ids": [3001, 3101, 3102, 3103, 3004]}]
            run, _, _ = await self._incident(fake_store, "emergency_reroute", routes=routes,
                                             hot=3002, hook=approve, max_iterations=4)
            approval = rows(fake_store, type="operator_approval_required")[0]
            assert approval["action"] == "emergency_reroute" and approval["tier"] == "decider"
            assert approval["proposed"]["long_detour"] is True
            assert "— a long detour)" in approval["summary"]
            assert approval["detour_m"] > 2 * approval["remaining_m"]
            assert approval["detour_segment_ids"] == [3001, 3101, 3102, 3103, 3004]
            # Approved by the officer, the same detour is taken (no approval loop).
            switched = rows(fake_store, action="route_switched")[0]
            assert switched["ordered_by"] == "officer Duty Officer A"
            assert run.route.segment_ids == [3001, 3101, 3102, 3103, 3004]
            assert switched["route_id"] == "ring"
            assert switched["reason"] == "officer-approved detour avoiding [3002]"
            assert len(rows(fake_store, type="operator_approval_required")) == 1
            solves = [n for n, _ in self.executor.calls if n == "find_convoy_routes"]
            assert len(solves) == 1  # the approved detour is spliced, not solved again
        finally:
            for sid in far:
                NET.pop(sid, None)

    async def test_plan_b_splices_the_alternate_spine(self, fake_store):
        run, _, _ = await self._incident(fake_store, "activate_plan_b", alternates=[ALT],
                                         hook=officer_approves())
        switched = rows(fake_store, action="route_switched")[0]
        assert switched["source"] == "plan_b" and switched["ordered_by"] == "officer Duty Officer A"
        assert run.route.segment_ids == ALT
        assert fake_store.contexts["es-1"].protocol_state["plan_b"]["active"] is True

    async def test_plan_b_through_the_congestion_falls_back_to_a_reroute(self, fake_store):
        through = [3001, 3002, 3003, 3004]
        routes = [{"route_id": "bypass", "segment_ids": [3002, 3005, 3006]}]
        run, _, _ = await self._incident(fake_store, "activate_plan_b", alternates=[through],
                                         routes=routes, hook=officer_approves())
        assert rows(fake_store, type="operator_approval_required")[0]["kind"] == "reroute"
        switched = rows(fake_store, action="route_switched")[0]
        assert switched["source"] == "reroute"
        assert ("every Plan B spine runs through the congested segment(s) [3003]"
                in switched["reason"])

    async def test_low_confidence_raises_an_approval_and_the_officer_executes_it(self, fake_store):
        approvals = []

        def approve(run):
            pending = [a for a in run.approvals.values() if a["status"] == "pending"]
            if pending and not approvals:
                approvals.append(run.approve(pending[0]["approval_id"], "approve",
                                             "Duty Officer A"))

        run, _, _ = await self._incident(fake_store, "hold_position", confidence=0.3,
                                         hook=approve, max_iterations=4)
        row = rows(fake_store, type="operator_approval_required")[0]
        assert row["action"] == "hold_position" and row["status"] == "pending"
        assert "low confidence (0.30)" in row["why"] and "low confidence (0.30)" in row["reason"]
        assert row["phase"] == "live_escort_alert"
        assert row["simulated"] is True and row["data_quality"] == ["synthetic"]
        executed = rows(fake_store, action="operator_approved")[0]
        assert executed["officer"] == "Duty Officer A"
        held = rows(fake_store, phase="live_escort", action="hold_position")[0]
        assert held["ordered_by"] == "officer Duty Officer A"
        assert run.state.last_decision["tier"] == "operator"

    async def test_a_pending_approval_expires_when_the_congestion_clears(self, fake_store):
        run, _, _ = await self._incident(
            fake_store, "hold_position", confidence=0.3, max_iterations=5,
            levels=lambda i: {3003: 0.95 if 2 <= i < 4 else 0.2},
        )
        approval = rows(fake_store, type="operator_approval_required")[0]
        expired = rows(fake_store, action="approval_expired")[0]
        assert expired["approval_id"] == approval["approval_id"]
        assert expired["reason"] == "the congestion ahead cleared before an officer decided"
        assert run.state.pending_approvals == []
        late = run.approve(approval["approval_id"], "approve", "Duty Officer A")
        assert late["status"] == "expired"
        assert not run.operator_actions  # an expired recommendation is never executed

    async def test_failed_plan_b_and_reroute_keep_the_order_in_the_approval(self, fake_store):
        through = [3001, 3002, 3003, 3004]
        await self._incident(fake_store, "activate_plan_b", alternates=[through], routes=[])
        approval = rows(fake_store, type="operator_approval_required")[0]
        assert approval["action"] == "hold_position"
        assert approval["why"].startswith(
            "decider (decider) at confidence 0.90 — an officer approves every switch; the "
            "activate plan b it recommends cannot be carried out — every Plan B spine runs "
            "through the congested segment(s) [3003] — re-routing instead; emergency re-route "
            "found no path")

    async def test_unavailable_decider_is_an_approval_not_an_autonomous_act(self, fake_store):
        run, _, _ = await self._incident(
            fake_store, "emergency_reroute",
            error=DeciderUnavailable("/decide transport error"),
        )
        assert not rows(fake_store, action="route_switched")
        approval = rows(fake_store, type="operator_approval_required")[0]
        assert approval["why"].startswith("strategic tier unavailable")
        # No model answered: the deterministic rule that stood in is named as
        # the proposer (never a model tier), the checkpoint's own tier kept.
        assert approval["tier"] == "deterministic" and approval["confidence"] is None
        assert approval["checkpoint_tier"] not in (None, "deterministic")
        assert approval["reason"].startswith("Rule proposes ")  # never "AI proposes"
        # A real outage IS reported (only cadence never is).
        assert [r for r in fake_store.contexts["es-1"].decisions_log
                if r.get("type") == "decider_unavailable"]

    async def test_escalation_answer_is_routed_to_the_operator(self, fake_store):
        await self._incident(fake_store, "escalate_operator")
        approval = rows(fake_store, type="operator_approval_required")[0]
        assert approval["why"].startswith("the decider escalated this incident to the operator")

    async def test_a_route_order_that_cannot_avoid_the_jam_goes_to_an_officer(self, fake_store):
        # The jam is on the destination segment: neither Plan B (none) nor a
        # re-route can avoid it — the order is referred, never dropped.
        approved = []

        def approve(run):
            pending = [a for a in run.approvals.values() if a["status"] == "pending"]
            if pending and not approved:
                approved.append(run.approve(pending[0]["approval_id"], "approve",
                                            "Duty Officer A"))

        run, _, _ = await self._incident(fake_store, "activate_plan_b", hot=3004, hook=approve,
                                         max_iterations=5)
        assert rows(fake_store, action="reroute_skipped")
        referral = rows(fake_store, type="operator_approval_required")[0]
        assert referral["action"] == "hold_position" and referral["tier"] == "deterministic"
        assert referral["why"] == (
            "decider (decider) at confidence 0.90 — an officer approves every switch; the "
            "activate plan b it recommends cannot be carried out: no route avoids [3004] ahead "
            "of the convoy — approve to hold the convoy until it clears, reject to continue")
        held = rows(fake_store, phase="live_escort", action="hold_position")[0]
        assert held["ordered_by"] == "officer Duty Officer A" and held["segment_ids"] == [3004]
        assert not rows(fake_store, action="route_switched")

    async def test_a_convoy_already_in_the_jam_gets_no_referral(self, fake_store):
        await self._incident(fake_store, "emergency_reroute", hot=3001)
        assert rows(fake_store, action="reroute_skipped")[0]["reason"] == (
            "no congested segment ahead that a re-route could avoid")
        assert not rows(fake_store, type="operator_approval_required")

    async def test_an_approved_detour_the_convoy_has_rolled_past_is_not_taken(self, fake_store):
        # From 3001 the only way around 3003 is the long northern ring; from
        # 3002 there is none. The officer approves only after the convoy has
        # rolled onto 3002, past the detour's branch point.
        far = {
            3101: seg(3101, (72.550, 23.020), (72.550, 23.060), name="Ring North"),
            3102: seg(3102, (72.550, 23.060), (72.600, 23.060), name="Ring East"),
            3103: seg(3103, (72.600, 23.060), (72.590, 23.020), name="Ring Return"),
        }
        NET.update(far)
        try:
            def solve(args):
                if args["origin_segment"] == 3001:
                    return {"routes": [{"route_id": "ring",
                                        "segment_ids": [3001, 3101, 3102, 3103, 3004]}]}
                return {"routes": []}

            seed(fake_store, at_m=600.0)
            run = EscortRun("es-1")
            executor = network_executor(
                live=live_by_iteration(run, after_departure({3003: 0.95})),
                extra={"find_convoy_routes": solve})
            drive = Drive(fake_store, "es-1", start_m=600.0, stay=True)

            def roll_on_then_approve(r):
                if r.state.iteration == 2:
                    drive.d = 1500.0  # onto 3002 before the officer answers
                pending = [a for a in r.approvals.values() if a["status"] == "pending"]
                if r.state.iteration == 3 and pending:
                    r.approve(pending[0]["approval_id"], "approve", "Duty Officer A")

            drive.hooks.append(roll_on_then_approve)
            await escort(fake_store, executor, run=run, max_iterations=5, publish=drive,
                         decider=DeciderBrain(ActionDecider("emergency_reroute"),
                                              min_interval_sec=0))
            first, second = rows(fake_store, type="operator_approval_required")[:2]
            assert first["detour_segment_ids"] == [3001, 3101, 3102, 3103, 3004]
            # Not the stale detour, and not a fresher one on the officer's
            # name: the current plan held, the solver ran again from 3002
            # and the officer is asked again.
            assert not rows(fake_store, action="route_switched") and run.route.segment_ids == ROUTE
            [not_applied] = rows(fake_store, action="approval_not_applied")
            assert "rolled past the branch point" in not_applied["reason"]
            assert run.approvals[first["approval_id"]]["applied"] is False
            assert second["why"].startswith(
                "re-proposed: officer Duty Officer A approved a route the convoy can no longer "
                "take; the emergency reroute it recommends cannot be carried out — emergency "
                "re-route found no path from segment 3002")
            assert [a["origin_segment"] for n, a in executor.calls
                    if n == "find_convoy_routes"] == [3001, 3002]
        finally:
            for sid in far:
                NET.pop(sid, None)


class TestIncidentTiming:
    """Cooldown deferral, the debounced falling edge and hold expiry, on a
    clock double advanced one second per tick."""

    def _drive(self, store, clock, start_m=100.0, record=None):
        drive = Drive(store, "es-1", start_m=start_m, stay=True)
        if record is not None:
            drive.hooks.append(per_tick(record))
        drive.hooks.append(clock.advance)
        return drive

    async def test_a_crossing_deferred_by_the_cooldown_is_decided_when_it_ends(self, fake_store):
        seed(fake_store, at_m=100.0)
        run = EscortRun("es-1")
        clock = Clock()
        # Tick 2 rises (incident 1), tick 3 clears, tick 4 rises again inside
        # the 5 s cooldown and the jam then STAYS above the threshold.
        executor = network_executor(live=live_by_iteration(
            run, lambda i: {3002: 0.1 if i < 2 else 0.2 if i == 3 else 0.95}))
        decider = ActionDecider("continue_monitoring")
        await escort(fake_store, executor, run=run, max_iterations=12, cooldown=5.0, clock=clock,
                     decider=DeciderBrain(decider, min_interval_sec=0),
                     publish=self._drive(fake_store, clock))
        checkpoints = incident_checkpoints(fake_store)
        assert [c["incident_origin"] for c in checkpoints] == ["crossing", "deferred_crossing"]
        crossings = rows(fake_store, action="congestion_crossing")
        assert [bool(c.get("deferred")) for c in crossings] == [False, True]
        assert crossings[1]["decision_due_in_sec"] == 3.0
        raised = rows(fake_store, action="deferred_incident_raised")
        assert len(raised) == 1
        assert raised[0]["crossed_at"] == checkpoints[1]["deferred_from"]
        assert "during the incident cooldown and is still above it" in decider.states[2]
        assert decider.calls == 3  # departure + 2 incidents; nothing more for the sustained jam

    async def test_a_deferred_crossing_that_clears_needs_no_decision(self, fake_store):
        seed(fake_store, at_m=100.0)
        run = EscortRun("es-1")
        clock = Clock()
        levels = {2: 0.95, 3: 0.2, 4: 0.95, 5: 0.2}
        executor = network_executor(
            live=live_by_iteration(run, lambda i: {3002: levels.get(i, 0.1)}))
        decider = ActionDecider("continue_monitoring")
        await escort(fake_store, executor, run=run, max_iterations=10, cooldown=5.0, clock=clock,
                     decider=DeciderBrain(decider, min_interval_sec=0),
                     publish=self._drive(fake_store, clock))
        assert len(incident_checkpoints(fake_store)) == 1
        assert not rows(fake_store, action="deferred_incident_raised")

    async def test_one_low_sample_neither_clears_the_crossing_nor_lifts_the_hold(
        self, fake_store
    ):
        seed(fake_store, at_m=600.0)
        run = EscortRun("es-1")
        clock = Clock()

        def level(i):  # 3003 jammed from tick 2, ONE clear sample at tick 4, clear from 7
            if i < 2:
                return {}
            return {3003: 0.2 if i == 4 or i >= 7 else 0.95}

        executor = network_executor(live=live_by_iteration(run, level))
        holding = []
        drive = self._drive(fake_store, clock, start_m=600.0,
                            record=lambda r: holding.append(r.holding))
        drive.hooks.insert(0, officer_approves())
        await escort(fake_store, executor, run=run, max_iterations=14, clock=clock,
                     clear_confirm_sec=5.0,
                     decider=DeciderBrain(ActionDecider("hold_position"), min_interval_sec=0),
                     publish=drive)
        assert len(incident_checkpoints(fake_store)) == 1  # tick 5 was no new rising edge
        assert len(rows(fake_store, action="congestion_crossing")) == 1
        released = rows(fake_store, action="hold_released")
        assert [r["reason"] for r in released] == ["held segment(s) [3003] cleared"]
        # Proposed at tick 2, approved and held from tick 3 through the dip;
        # lifted after 5 s of clear readings (ticks 7..12).
        assert holding == [False, False] + [True] * 9 + [False] * 3
        assert released[0]["held_sec"] == 9.0

    async def test_a_pending_approval_survives_one_low_sample(self, fake_store):
        seed(fake_store, at_m=600.0)
        run = EscortRun("es-1")
        clock = Clock()

        def level(i):
            if i < 2:
                return {}
            return {3003: 0.2 if i == 4 or i >= 7 else 0.95}

        executor = network_executor(live=live_by_iteration(run, level))
        pending = []
        await escort(fake_store, executor, run=run, max_iterations=14, clock=clock,
                     clear_confirm_sec=5.0,
                     decider=DeciderBrain(ActionDecider("hold_position", confidence=0.3),
                                          min_interval_sec=0),
                     publish=self._drive(fake_store, clock, start_m=600.0,
                                         record=lambda r: pending.append(
                                             len(r.state.pending_approvals))))
        assert pending == [0] + [1] * 10 + [0] * 3
        expired = rows(fake_store, action="approval_expired")
        assert [e["reason"] for e in expired] == [
            "the congestion ahead cleared before an officer decided"]

    async def test_a_hold_that_expires_on_a_jam_is_re_decided(self, fake_store):
        seed(fake_store, at_m=600.0)
        run = EscortRun("es-1")
        clock = Clock()
        executor = network_executor(live=live_by_iteration(run, after_departure({3003: 0.95})))
        decider = ActionDecider("hold_position")
        drive = self._drive(fake_store, clock, start_m=600.0)
        drive.hooks.insert(0, officer_approves())
        await escort(fake_store, executor, run=run, max_iterations=8, clock=clock,
                     hold_max_sec=3.0, decider=DeciderBrain(decider, min_interval_sec=0),
                     publish=drive)
        released = rows(fake_store, action="hold_released")[0]
        assert released["reason"] == (
            "hold expired after 3 s; [3003] still above the threshold — re-deciding")
        assert released["still_congested"] == [3003]
        checkpoints = incident_checkpoints(fake_store)
        assert [c["incident_origin"] for c in checkpoints] == ["crossing", "hold_expired"]
        assert "A hold ran out (held 3 s of a 3 s maximum)" in decider.states[2]
        # The decider proposed holding again and the officer approved it:
        # the convoy never rolled on undecided.
        holds = rows(fake_store, phase="live_escort", action="hold_position")
        assert [h["ordered_by"] for h in holds] == ["officer Duty Officer A"] * 2
        assert run.hold is None  # the run ended (safety valve) — no hold outlives it

    async def test_a_hold_that_expires_on_a_cleared_corridor_is_simply_lifted(self, fake_store):
        seed(fake_store, at_m=600.0)
        run = EscortRun("es-1")
        clock = Clock()
        # 3003 jammed at ticks 2-3, then inside the hysteresis band (0.78):
        # not clear enough to lift the hold, not congested enough to re-decide.
        executor = network_executor(live=live_by_iteration(
            run, lambda i: {} if i < 2 else {3003: 0.95 if i < 4 else 0.78}))
        decider = ActionDecider("hold_position")
        drive = self._drive(fake_store, clock, start_m=600.0)
        drive.hooks.insert(0, officer_approves())
        await escort(fake_store, executor, run=run, max_iterations=8, clock=clock,
                     hold_max_sec=3.0, decider=DeciderBrain(decider, min_interval_sec=0),
                     publish=drive)
        released = rows(fake_store, action="hold_released")
        assert [r["reason"] for r in released] == ["hold expired after 3 s"]
        assert released[0]["still_congested"] == []
        assert len(incident_checkpoints(fake_store)) == 1


class TestHolds:
    """A hold waits on its own segments, not on the lookahead."""

    async def test_a_departure_hold_waits_on_its_own_segment_beyond_the_lookahead(
        self, fake_store
    ):
        seed(fake_store, at_m=0.0)
        run = EscortRun("es-1", officer="Duty Officer A")

        def live(args):
            # 3004 starts ~3.07 km along the route — outside the 3 km
            # lookahead. Jammed until tick 5, silent at tick 3.
            i = run.state.iteration
            return {"observations": [
                {"segment_id": s, "congestion_idx": 0.95 if s == 3004 and i < 5 else 0.1,
                 "speed_kmh": 30.0, "data_quality": "synthetic"}
                for s in args["segment_ids"] if not (s == 3004 and i == 3)
            ]}

        executor = network_executor(live=live)
        double = ActionDecider("continue_monitoring", departure="hold_departure",
                               departure_confidence=0.9)
        drive = Drive(fake_store, "es-1", start_m=0.0, stay=True)
        holding = []
        drive.hooks.append(officer_approves())
        drive.hooks.append(per_tick(lambda r: holding.append(r.holding)))
        await escort(fake_store, executor, run=run, max_iterations=6, publish=drive,
                     decider=DeciderBrain(double, min_interval_sec=0))
        hold = rows(fake_store, phase="live_escort", action="hold_position")[0]
        assert hold["segment_ids"] == [3004] and hold["simulated"] is True
        assert hold["ordered_by"] == "officer Duty Officer A"
        # Proposed at the departure, approved, then held through the jam and
        # the silent tick; lifted on the first clear reading of 3004 itself
        # — not at departure for a clear lookahead.
        assert holding == [False, True, True, True, False, False]
        released = rows(fake_store, action="hold_released")
        assert [r["reason"] for r in released] == ["held segment(s) [3004] cleared"]
        assert released[0]["held_sec"] >= 0.0
        # The held segment was watched although it lies beyond the lookahead.
        watched = [a["segment_ids"] for n, a in executor.calls if n == "get_live_traffic"][1:]
        assert 3004 not in watched[0]  # tick 1: proposed, not yet held
        assert all(3004 in ids for ids in watched[1:5])
        assert watched[5] == [3001, 3002, 3003]
        assert not incident_checkpoints(fake_store) and double.calls == 1

    async def test_a_real_convoy_driving_past_its_held_segment_lifts_the_hold(self, fake_store):
        seed(fake_store, at_m=600.0)
        run = EscortRun("es-1")
        executor = network_executor(live=live_by_iteration(run, after_departure({3002: 0.95})))
        drive = Drive(fake_store, "es-1", start_m=600.0, step_m=300.0)
        drive.hooks.append(officer_approves())
        await escort(fake_store, executor, run=run, max_iterations=6,
                     decider=DeciderBrain(ActionDecider("hold_position"), min_interval_sec=0),
                     publish=drive)
        hold = rows(fake_store, phase="live_escort", action="hold_position")[0]
        assert hold["advisory"] is True  # a real convoy is advised by radio, not halted
        released = rows(fake_store, action="hold_released")
        assert [r["reason"] for r in released] == ["the convoy is past the held segment(s)"]

    async def test_a_route_switch_lifts_a_hold_the_new_route_avoids(self, fake_store):
        seed(fake_store, at_m=600.0)
        run = EscortRun("es-1")
        run.alternates = [ALT]
        executor = network_executor(live=live_by_iteration(run, after_departure({3003: 0.95})))
        drive = Drive(fake_store, "es-1", start_m=600.0, stay=True)

        def officer_orders_plan_b(r):
            if r.state.iteration == 3:  # an approved action queued for the next tick
                r.operator_actions.append({
                    "action": "activate_plan_b", "approval_id": "op-1",
                    "decided_by": "Duty Officer A", "trigger": "incident_detected",
                    "incident": {"peak": 0.95, "hot": [3003], "rows": []},
                })

        drive.hooks.insert(0, officer_approves(limit=1))  # the hold
        drive.hooks.append(officer_orders_plan_b)
        await escort(fake_store, executor, run=run, max_iterations=5, publish=drive,
                     decider=DeciderBrain(ActionDecider("hold_position"), min_interval_sec=0))
        assert rows(fake_store, phase="live_escort", action="hold_position")
        assert run.route.segment_ids == ALT
        released = rows(fake_store, action="hold_released")
        assert [r["reason"] for r in released] == ["the new route avoids the held segment(s)"]
        assert run.hold is None and run.state.hold is None


class TestIsolation:
    async def test_a_failing_tool_degrades_once_and_recovers(self, fake_store):
        seed(fake_store, at_m=100.0)
        run = EscortRun("es-1")

        def flaky(args):
            if run.state.iteration in (1, 2, 3):
                raise RuntimeError("oracle 503")
            return {"observations": []}

        executor = network_executor(live=flaky)
        _, result = await escort(fake_store, executor, run=run, max_iterations=5,
                                 publish=Drive(fake_store, "es-1", start_m=100.0, stay=True))
        assert result["escort_complete"] is True
        assert len(rows(fake_store, action="tool_degraded")) == 1
        assert len(rows(fake_store, action="tool_recovered")) == 1

    async def test_x_class_emits_signal_priority_never_closures(self, fake_store):
        await fake_store.put(ConvoyContext(movement_id="x1", vvip_class="X"))
        bridge = StubBridge()
        executor = FakeExecutor(routes={"plan_diversions": {"diversions": [
            {"segment_id": 3002, "advance_sec": 120, "closure_type": "full"}]},
            "get_live_traffic": {"observations": []}})
        agent = DiversionCoordinatorAgent(bridge, executor, fake_store)  # no adaptive params
        result = await agent.activate_diversions("x1", [3001, 3002])
        assert {d["action"] for d in result["directives"]} == {"signal_priority"}
        assert [d["segment_id"] for d in result["directives"]] == [3001, 3002]
        assert fake_store.contexts["x1"].active_diversions == []
        assert bridge.calls == []  # no tactical narration of a closure plan
        plan_call = next(args for name, args in executor.calls if name == "plan_diversions")
        assert plan_call["advance_closure_sec"] == 0

    async def test_terse_narration_never_leaves_a_planned_segment_open(self, fake_store):
        await fake_store.put(ConvoyContext(movement_id="z1", vvip_class="Z"))
        # Gemma narrates ONE closure and an explicit "monitor" for 3002 out of
        # a three-row oracle plan: 3003 gets the deterministic floor, 3002's
        # explicit directive is kept as written.
        bridge = StubBridge(replies=[json.dumps({
            "directives": [
                {"segment_id": 3001, "action": "activate", "agency": "traffic_police",
                 "timing_sec": 120, "detail": "close 3001"},
                {"segment_id": 3002, "action": "monitor", "agency": "traffic_police",
                 "timing_sec": 0, "detail": "watch 3002"},
            ],
            "queue_alerts": [], "overall_status": "ok", "confidence": "medium",
        })])
        executor = FakeExecutor(routes={
            "plan_diversions": {"diversions": [
                {"segment_id": s, "advance_sec": 120, "closure_type": "partial"}
                for s in (3001, 3002, 3003)]},
            "get_live_traffic": {"observations": []}})
        agent = DiversionCoordinatorAgent(bridge, executor, fake_store)
        result = await agent.activate_diversions("z1", [3001, 3002, 3003])
        assert result["_tier"] == "gemma"
        by_segment = {d["segment_id"]: d for d in result["directives"]}
        assert by_segment[3002]["action"] == "monitor"
        assert by_segment[3003]["action"] == "activate"
        assert by_segment[3003]["source"] == "deterministic_floor"
        assert result["floor_filled_segment_ids"] == [3003]
        assert fake_store.contexts["z1"].active_diversions == ["3001", "3003"]

    async def test_tick_update_keeps_segments_ahead_by_route_order(self, fake_store):
        route = [1001, 1005, 1009, 1013]
        ctx = ConvoyContext(movement_id="t1", vvip_class="Z")
        ctx.active_diversions = ["1001", "1005", "1009", "1013", "7777"]
        await fake_store.put(ctx)
        agent = DiversionCoordinatorAgent(StubBridge(), FakeExecutor(), fake_store)
        # Convoy on 1001: everything ahead stays closed (the audit repro).
        result = await agent.tick_update("t1", (0, 0), [1001, 1005, 1009, 1013],
                                         route_segment_ids=route)
        assert result["deactivated"] == []
        # Convoy on 1009: 1001/1005 are behind it and lift; off-route 7777 stays.
        result = await agent.tick_update("t1", (0, 0), [1009, 1013], route_segment_ids=route)
        assert result["deactivated"] == ["1001", "1005"]
        assert fake_store.contexts["t1"].active_diversions == ["1009", "1013", "7777"]
        # Without route order nothing is lifted.
        result = await agent.tick_update("t1", (0, 0), [1013])
        assert result["status"] == "no_route_order" and result["deactivated"] == []

    async def test_adaptive_threshold_is_applied(self, fake_store):
        seed(fake_store, at_m=100.0)
        run = EscortRun("es-1")
        executor = network_executor(live=live_by_iteration(run, after_departure({3002: 0.5})))
        decider = ActionDecider("continue_monitoring")
        await escort(fake_store, executor, run=run, max_iterations=2,
                     decider=DeciderBrain(decider, min_interval_sec=0),
                     adaptive=AdaptiveParams(congestion_spike_threshold=0.4),
                     publish=Drive(fake_store, "es-1", start_m=100.0, stay=True))
        # 0.5 is below the 0.8 baseline: only the adaptive 0.4 makes it a crossing.
        assert len(incident_checkpoints(fake_store)) == 1
        assert run.state.spike_threshold == 0.4


class TestLifecycle:
    async def test_escort_start_activates_the_event_and_reads_plan_b(self, fake_store):
        class Lifecycle:
            def __init__(self):
                self.calls = []

            async def transition(self, event_id, to_status, officer, note):
                self.calls.append((event_id, to_status, officer))
                return {"outcome": "transitioned", "event_id": event_id,
                        "to_status": to_status, "http_status": 200, "from_status": "planning"}

            async def spines(self, event_id):
                return {"primary": ROUTE, "alt-1": ALT}

        seed(fake_store, at_m=100.0)
        lifecycle = Lifecycle()
        run = EscortRun("es-1", officer="Duty Officer A")
        await escort(fake_store, network_executor(), run=run, max_iterations=1,
                     lifecycle=lifecycle)
        assert lifecycle.calls == [(EVENT_ID, "active", "Duty Officer A")]
        assert run.alternates == [ALT]
        row = rows(fake_store, phase="city_event")[0]
        assert row["action"] == "event_transitioned" and row["officer"] == "Duty Officer A"


# ---------------------------------------------------------------------------
# HTTP surface (contract C-J) — real FastAPI routes, in-memory Valkey
# ---------------------------------------------------------------------------


class _Lifecycle:
    def __init__(self):
        self.calls = []

    async def transition(self, event_id, to_status, officer, note):
        self.calls.append((event_id, to_status, officer))
        return {"outcome": "transitioned", "event_id": event_id, "to_status": to_status,
                "http_status": 200, "from_status": "planning"}

    async def spines(self, event_id):
        return {"primary": ROUTE, "alt-1": ALT}

    async def aclose(self):
        pass


@pytest.fixture
def api(monkeypatch):
    from starlette.testclient import TestClient
    from test_convoy_context_atomic import FakeValkey

    from convoy_brain import __main__ as brain_main
    from convoy_brain.memory.convoy_context import ConvoyContextStore

    server = FakeValkey()
    for name in ("MODEL_HOST_URL", "LLAMA_BASE_URL", "TRAFFIC_ORACLE_URL", "EMBEDDING_SERVICE_URL"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    monkeypatch.delenv("VVIP_REQUIRE_OFFICER", raising=False)
    monkeypatch.delenv("VVIP_ESCORT_SIM_GPS", raising=False)
    monkeypatch.setattr(live_escort, "GPS_POLL_INTERVAL_SEC", 0.02)
    monkeypatch.setattr(
        brain_main, "ConvoyContextStore",
        lambda valkey_url: ConvoyContextStore(valkey_url, client=server),
    )
    app = brain_main.create_app()
    with TestClient(app) as client:
        app.state.executor = network_executor()
        app.state.lifecycle = _Lifecycle()

        def call(fn, *args):
            return client.portal.call(fn, *args)

        yield SimpleNamespace(app=app, client=client, server=server, call=call)


def _planned(api, *, route=ROUTE) -> str:
    mid = api.client.post("/movements", json={
        "origin": [72.55, 23.02], "destination": [72.59, 23.02], "vvip_class": "Z",
        "planned_departure": "2026-10-23T09:00:00+05:30",
    }).json()["movement_id"]

    async def plan():
        def apply(c):
            c.status = "approved"
            c.event_id = EVENT_ID
            c.decisions_log.append({"phase": "pre_movement", "recommendation": "approved",
                                    "primary_segment_ids": list(route), "timestamp": time.time()})
        await api.app.state.context_store.mutate(mid, apply)

    api.call(plan)
    return mid


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition not reached")


class TestEscortApi:
    def test_start_is_202_single_flight_and_status_tracks_motion(self, api):
        mid = _planned(api)
        started = time.monotonic()
        resp = api.client.post(f"/movements/{mid}/escort", json={"officer": "Duty Officer A"})
        assert resp.status_code == 202 and time.monotonic() - started < 1.0
        body = resp.json()
        assert body["status"] == "running" and body["movement_id"] == mid and body["task_id"]
        again = api.client.post(f"/movements/{mid}/escort", json={"officer": "Duty Officer A"})
        assert again.status_code == 409
        assert again.json()["status"] in ("waiting_for_gps", "running")

        status = _wait(lambda: (lambda d: d if d["status"] == "waiting_for_gps" else None)(
            api.client.get(f"/movements/{mid}/escort").json()))
        assert status["position"] is None and status["event_id"] == EVENT_ID

        fix = api.client.post(f"/movements/{mid}/position", json={
            "lon": 72.5552, "lat": 23.0201, "speed_kmh": 38.0, "heading": 91.0})
        assert fix.status_code == 202 and fix.json()["accepted"] is True
        running = _wait(lambda: (lambda d: d if d["status"] == "running" else None)(
            api.client.get(f"/movements/{mid}/escort").json()))
        assert running["current_segment_id"] == 3001
        assert 0.0 < running["progress"] < 0.2
        first = running["iteration"]
        later = _wait(lambda: (lambda d: d if d["iteration"] > first else None)(
            api.client.get(f"/movements/{mid}/escort").json()))
        assert later["position"]["source"] == "gps"

        frame_key = f"convoy:position:{mid}"
        frame = json.loads(api.server.data[frame_key])
        assert api.server.ttl[frame_key] == 30
        assert frame["event_id"] == EVENT_ID and frame["source"] == "gps"
        assert frame["segment_id"] == 3001 and 0 < frame["progress"] < 1
        assert {"movement_id", "lon", "lat", "speed_kmh", "heading", "t"} <= set(frame)

        assert api.app.state.lifecycle.calls[0] == (EVENT_ID, "active", "Duty Officer A")

        stop = api.client.post(f"/movements/{mid}/stop", json={"officer": "Duty Officer A"})
        assert stop.status_code == 200 and stop.json()["status"] == "stopped"
        assert api.client.post(f"/movements/{mid}/stop", json={}).json()["status"] == "stopped"
        final = api.client.get(f"/movements/{mid}/escort").json()
        assert final["status"] == "stopped" and final["detail"] == "stopped by Duty Officer A"
        # The status document survives in Valkey (C-J GET after a restart).
        assert json.loads(api.server.data[f"convoy:escort:{mid}"])["status"] == "stopped"

    def test_a_resumed_escort_keeps_the_route_an_earlier_run_switched_to(self, api):
        mid = _planned(api)
        # An earlier run (after the plan) re-routed onto the northern bypass,
        # then the brain restarted: its status document survives in Valkey.
        api.server.data[f"convoy:escort:{mid}"] = json.dumps({
            "movement_id": mid, "status": "running", "started_at": utc_iso(time.time() + 1),
            "route": {"segment_ids": ALT, "length_m": 4900.0, "source": "reroute",
                      "missing_geometry": []},
        })
        stale = api.client.get(f"/movements/{mid}/escort").json()
        assert stale["status"] == "stopped" and "restarted?" in stale["detail"]
        assert api.client.post(f"/movements/{mid}/escort", json={}).status_code == 202
        status = _wait(lambda: (lambda d: d if d.get("route") else None)(
            api.client.get(f"/movements/{mid}/escort").json()))
        assert status["route"]["segment_ids"] == ALT and status["route"]["source"] == "reroute"
        api.client.post(f"/movements/{mid}/stop")
        # A re-plan after that run wins: the next escort follows the new plan.
        from convoy_brain.workflows.live_escort import resumable_route

        persisted = {"started_at": utc_iso(time.time() - 60),
                     "route": {"segment_ids": ALT, "source": "reroute"}}
        replanned = SimpleNamespace(decisions_log=[{"phase": "pre_movement",
                                                    "primary_segment_ids": ROUTE,
                                                    "timestamp": time.time()}])
        assert resumable_route(persisted, replanned) is None
        assert resumable_route(dict(persisted, route={"segment_ids": ALT, "source": "plan"}),
                               replanned) is None

    def test_position_validation(self, api):
        mid = _planned(api)
        delhi = api.client.post(f"/movements/{mid}/position", json={"lon": 77.21, "lat": 28.61})
        assert delhi.status_code == 422
        nan = api.client.post(f"/movements/{mid}/position",
                              content=b'{"lon": NaN, "lat": 23.02}',
                              headers={"content-type": "application/json"})
        assert nan.status_code == 422
        future = api.client.post(f"/movements/{mid}/position", json={
            "lon": 72.56, "lat": 23.02, "t": "2099-01-01T00:00:00Z"})
        assert future.status_code == 422
        unknown = api.client.post("/movements/nope/position", json={"lon": 72.56, "lat": 23.02})
        assert unknown.status_code == 404
        ok = api.client.post(f"/movements/{mid}/position", json={
            "lon": 72.56, "lat": 23.02, "source": "emulated_gps"})
        assert ok.status_code == 202
        assert json.loads(api.server.data[f"convoy:position:{mid}"])["source"] == "emulated_gps"

    def test_start_preconditions(self, api, monkeypatch):
        assert api.client.post("/movements/nope/escort", json={}).status_code == 404
        unplanned = api.client.post("/movements", json={
            "origin": [72.55, 23.02], "destination": [72.59, 23.02],
            "planned_departure": "2026-10-23T09:00:00+05:30"}).json()["movement_id"]
        assert api.client.post(f"/movements/{unplanned}/escort", json={}).status_code == 422
        mid = _planned(api)
        monkeypatch.setenv("VVIP_REQUIRE_OFFICER", "1")
        assert api.client.post(f"/movements/{mid}/escort", json={}).status_code == 422
        bidi = api.client.post(f"/movements/{mid}/escort", json={"officer": "Officer‮A"})
        assert bidi.status_code == 422
        idle = api.client.get(f"/movements/{mid}/escort").json()
        assert idle["status"] == "idle" and idle["iteration"] == 0
        # Legacy deck body (destination only) still works when officers are optional.
        monkeypatch.delenv("VVIP_REQUIRE_OFFICER")
        legacy = api.client.post(f"/movements/{mid}/escort", json={"destination": [72.59, 23.02]})
        assert legacy.status_code == 202
        api.client.post(f"/movements/{mid}/stop")

    def test_clear_completes_the_corridor_event(self, api):
        mid = _planned(api)
        assert api.client.post(f"/movements/{mid}/escort",
                               json={"officer": "Duty Officer A"}).status_code == 202
        report = api.client.post(f"/movements/{mid}/clear",
                                 json={"officer": "Duty Officer A", "max_recovery_sec": 0}).json()
        assert report["event_transition"]["outcome"] == "transitioned"
        assert api.app.state.lifecycle.calls[-1] == (EVENT_ID, "completed", "Duty Officer A")
        assert api.client.get(f"/movements/{mid}/escort").json()["status"] == "stopped"
        ctx = api.call(api.app.state.context_store.get, mid)
        assert ctx.status == "completed"
        assert api.client.post(f"/movements/{mid}/escort", json={}).status_code == 409
        # A closed movement takes no more fixes: the convoy never reappears
        # on the city picture linked to a completed event.
        late = api.client.post(f"/movements/{mid}/position", json={"lon": 72.56, "lat": 23.02})
        assert late.status_code == 409 and late.json()["status"] == "completed"
        assert f"convoy:position:{mid}" not in api.server.data
        assert api.call(api.app.state.context_store.get, mid).convoy_position is None

    def test_clearing_a_movement_that_never_went_live_cancels_its_event(self, api):
        class PlanningEvent(_Lifecycle):
            async def transition(self, event_id, to_status, officer, note):
                self.calls.append((event_id, to_status, officer))
                if to_status == "completed":  # C-F: planning -> completed is illegal
                    return {"outcome": "conflict", "event_id": event_id,
                            "to_status": to_status, "http_status": 409,
                            "from_status": "planning",
                            "allowed": ["approved", "active", "cancelled"]}
                return {"outcome": "transitioned", "event_id": event_id,
                        "to_status": to_status, "http_status": 200, "from_status": "planning"}

        api.app.state.lifecycle = PlanningEvent()
        mid = _planned(api)
        report = api.client.post(f"/movements/{mid}/clear",
                                 json={"officer": "Duty Officer A", "max_recovery_sec": 0}).json()
        assert report["event_transition"]["to_status"] == "cancelled"
        assert report["event_transition"]["outcome"] == "transitioned"
        assert [c[1] for c in api.app.state.lifecycle.calls] == ["completed", "cancelled"]
        ctx = api.call(api.app.state.context_store.get, mid)
        events = [(r["action"], r["to_status"]) for r in ctx.decisions_log
                  if r.get("phase") == "city_event"]
        assert events == [("event_conflict", "completed"), ("event_transitioned", "cancelled")]

    def test_replanning_under_a_live_escort_is_refused(self, api):
        mid = _planned(api)
        assert api.client.post(f"/movements/{mid}/escort", json={}).status_code == 202
        replan = api.client.post(f"/movements/{mid}/plan", json={
            "origin": [72.55, 23.02], "destination": [72.59, 23.02], "vvip_class": "Z",
            "planned_departure": "2026-10-23T09:00:00+05:30"})
        assert replan.status_code == 409
        assert f"POST /movements/{mid}/stop" in replan.json()["detail"]
        api.client.post(f"/movements/{mid}/stop")

    def test_the_emulated_walker_waits_for_late_route_geometry(self, api, monkeypatch):
        from convoy_brain import __main__ as brain_main

        captured = {}

        class Walker(GpsSimulator):
            def __init__(self, **kwargs):
                captured.update(kwargs)
                super().__init__(**{**kwargs, "tick_sec": 0.05})

        monkeypatch.setattr(brain_main, "GpsSimulator", Walker)
        monkeypatch.setattr(live_escort, "ROUTE_RETRY_SEC", 0.1)
        monkeypatch.setenv("VVIP_ESCORT_SIM_GPS", "1")
        monkeypatch.setenv("VVIP_ESCORT_GPS_TIMEOUT_SEC", "900")
        details = details_handler(NET)
        outage = {"calls_left": len(ROUTE)}

        def flaky_details(args):  # the first route load fails (oracle hiccup)
            if outage["calls_left"] > 0:
                outage["calls_left"] -= 1
                raise RuntimeError("oracle 503")
            return details(args)

        api.app.state.executor = network_executor(extra={"query_segment_details": flaky_details})
        mid = _planned(api)
        assert api.client.post(f"/movements/{mid}/escort", json={}).status_code == 202
        status = _wait(lambda: (lambda d: d if d["status"] == "running" else None)(
            api.client.get(f"/movements/{mid}/escort").json()))
        assert status["gps"]["mode"] == "emulated"
        assert status["position"]["source"] == "emulated_gps"
        assert status["route"]["segment_ids"] == ROUTE and not status["route"]["missing_geometry"]
        # The walker waits for geometry as long as the escort waits for GPS.
        assert captured["route_wait_sec"] == 900.0
        api.client.post(f"/movements/{mid}/stop")


# ---------------------------------------------------------------------------
# Health feed and /health
# ---------------------------------------------------------------------------


class TestHealthFeed:
    async def test_every_poll_feeds_both_roles_to_the_oracle(self, monkeypatch):
        from convoy_brain import __main__ as brain_main
        from convoy_brain.metrics import LLAMA_HEALTH

        posted = []

        def handler(request: httpx.Request) -> httpx.Response:
            posted.append((str(request.url), json.loads(request.content)))
            return httpx.Response(200, json={"degradation_tier": "FULL"})

        class Bridge:
            async def check_health(self):
                return True

        decider = ActionDecider()
        state = SimpleNamespace(
            decider_brain=DeciderBrain(decider),
            decider_client=decider,
            bridge=Bridge(),
            health_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            oracle_url="http://127.0.0.1:8112",
        )
        monkeypatch.setattr(brain_main, "DECIDER_HEALTH_POLL_SEC", 0.01)
        task = asyncio.create_task(brain_main._background_health_loop(SimpleNamespace(state=state)))
        for _ in range(200):
            if len(posted) >= 4:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await state.health_client.aclose()
        assert all(url == "http://127.0.0.1:8112/api/v1/adaptive/health" for url, _ in posted)
        bodies = [body for _, body in posted]
        assert {b["role"] for b in bodies} == {"decider", "gemma"}
        decider_row = next(b for b in bodies if b["role"] == "decider")
        assert decider_row == {"role": "decider", "ok": True, "latency_ms": 100.0}
        assert all(b["ok"] is True and b["latency_ms"] >= 0 for b in bodies)
        assert LLAMA_HEALTH._value.get() == 1.0

    def test_model_host_state_is_loading_not_unreachable(self):
        from convoy_brain.__main__ import _model_host_state

        assert _model_host_state({}) == "unreachable"
        assert _model_host_state({"loaded": False, "loading": True}) == "loading"
        assert _model_host_state({"loaded": False, "load_error": "CUDA OOM"}) == "load_error"
        assert _model_host_state({"loaded": True}) == "connected"


# ---------------------------------------------------------------------------
# Post-clearance
# ---------------------------------------------------------------------------


class TestPostClearance:
    async def test_baseline_per_segment_and_real_escort_duration(self, fake_store):
        ctx = ConvoyContext(movement_id="pc", vvip_class="Z", status="active")
        ctx.active_diversions = ["3003", "3004"]
        ctx.decisions_log += [
            {"phase": "live_escort", "action": "escort_started", "timestamp": 1_000_000.0},
            {"phase": "live_escort", "action": "escort_complete", "timestamp": 1_000_420.0},
        ]
        await fake_store.put(ctx)
        history_calls = []

        def history(args):
            history_calls.append(dict(args))
            # 1_000_000 s epoch = 1970-01-12 13:46 UTC = 19:16 IST: the second
            # row is the 13:00 UTC bucket (18:30-19:30 IST) holding that time.
            return {"segment_id": args["segment_id"], "aggregates": [
                {"hour_utc": "1970-01-12T05:00:00+00:00", "avg_speed_kmh": 46.0,
                 "p95_congestion": 0.25, "observation_cnt": 12},
                {"hour_utc": "1970-01-12T13:00:00+00:00", "avg_speed_kmh": 38.0,
                 "p95_congestion": 0.55, "observation_cnt": 12}]}

        executor = FakeExecutor(routes={
            "get_live_traffic": {"observations": [
                {"segment_id": 3003, "congestion_idx": 0.1}, {"segment_id": 3004,
                                                              "congestion_idx": 0.2}]},
            "get_historical_pattern": history,
        })
        report = await run_post_clearance_workflow(
            movement_id="pc", bridge=StubBridge(fail=True), executor=executor,
            context_store=fake_store, max_recovery_iterations=2)
        assert sorted(c["segment_id"] for c in history_calls) == [3003, 3004]
        assert all({"segment_id", "day_of_week", "hour"} <= set(c) for c in history_calls)
        baseline = report["historical_baseline"]
        # The tool asks for the movement's own IST weekday/hour over 7 days
        # (C-P) and answers only those rows: the report summarises them.
        assert baseline["window_hours"] == 168 and baseline["hour_ist"] == 19
        assert baseline["day_of_week"] == 0  # 1970-01-12 was a Monday
        assert all(c["day_of_week"] == 0 and c["hour"] == 19 for c in history_calls)
        assert baseline["segments"]["3003"] == {
            "hours": 2, "avg_speed_kmh": 42.0, "p95_congestion": 0.55,
            "provenance": "real",
            "label": "real hourly aggregates, same IST weekday and hour (last 7 days)",
        }
        assert baseline["provenance"] == "real" and baseline["segments_with_history"] == 2
        assert report["escort_duration_sec"] == 420.0 and report["escort_ended"] is True
        assert report["recovery_time_sec"] == 0.0  # first check needs no sleep
        assert report["segments_recovered"] == 2

    async def test_baseline_labels_a_synthetic_only_and_an_absent_history(self, fake_store):
        """C-P: no real rows → the synthetic rollup is labelled as such (never
        a calibration input); nothing at all → 'no history'; an older oracle
        without synthetic_aggregates / provenance still answers."""
        from convoy_brain.workflows.post_clearance import (
            NO_BASELINE_LABEL,
            SYNTHETIC_BASELINE_LABEL,
            _historical_baseline,
        )

        def history(args):
            if args["segment_id"] == 3101:
                return {"segment_id": 3101, "aggregates": [], "provenance": "synthetic",
                        "synthetic_aggregates": [
                            {"hour_utc": "2026-10-19T13:00:00+00:00", "avg_speed_kmh": 31.0,
                             "p95_congestion": 0.4, "data_quality": "synthetic"}]}
            return {"segment_id": args["segment_id"], "aggregates": []}  # older oracle

        executor = FakeExecutor(routes={"get_historical_pattern": history})
        from datetime import datetime, timedelta, timezone
        at = datetime(2026, 10, 19, 19, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
        baseline = await _historical_baseline(executor, [3101, 3102], at)
        synthetic = baseline["segments"]["3101"]
        assert synthetic["provenance"] == "synthetic"
        assert synthetic["label"] == SYNTHETIC_BASELINE_LABEL
        assert synthetic["data_quality"] == "synthetic" and synthetic["avg_speed_kmh"] == 31.0
        assert baseline["segments"]["3102"]["label"] == NO_BASELINE_LABEL
        assert baseline["segments_with_history"] == 0 and baseline["segments_synthetic_only"] == 1
        assert baseline["provenance"] == "synthetic"
        assert baseline["baseline_label"] == SYNTHETIC_BASELINE_LABEL

    async def test_low_confidence_posture_is_not_promised_a_draft(self, fake_store):
        """/clear drafts nothing: a low-confidence posture is audited as an
        officer review with the conservative default kept (live finding)."""
        ctx = ConvoyContext(movement_id="pc2", vvip_class="Z", status="active")
        await fake_store.put(ctx)

        class LowDecider:
            async def decide(self, state, questions, request_id=None):
                from convoy_brain.llm.decider_client import DeciderAnswer
                return {name: DeciderAnswer(name=name, type="choice",
                                            value="immediate_full_restoration",
                                            probability=0.3, confidence=0.25)
                        for name in questions}

        from convoy_brain.decider import DeciderBrain

        report = await run_post_clearance_workflow(
            movement_id="pc2", bridge=StubBridge(fail=True),
            executor=FakeExecutor(routes={"get_live_traffic": {"observations": []}}),
            context_store=fake_store,
            decider_brain=DeciderBrain(LowDecider(), min_interval_sec=0),
            max_recovery_iterations=1)
        assert report["restoration_posture"] == "staged_reverse_order_restoration"
        alerts = [d for d in fake_store.contexts["pc2"].decisions_log
                  if d.get("phase") == "post_clearance_alert"]
        assert alerts and all(a["action"] == "officer_review_recommended" for a in alerts)
        assert all(a["outcome"] == "conservative_default_kept" for a in alerts)
        assert not any(a.get("action") == "gemma_draft_required" for a in alerts)

    def test_a_resumed_escort_is_one_window_and_a_replan_starts_a_new_one(self):
        from convoy_brain.workflows.post_clearance import _escort_window

        trail = [
            {"phase": "pre_movement", "timestamp": 900_000.0},
            {"phase": "live_escort", "action": "escort_started", "timestamp": 1_000_000.0},
            # brain killed mid-escort: no end row for the first run
            {"phase": "live_escort", "action": "escort_started", "timestamp": 1_000_500.0},
            {"phase": "live_escort", "action": "escort_complete", "timestamp": 1_000_600.0},
        ]
        assert _escort_window(trail) == (1_000_000.0, 1_000_600.0, 2)
        replanned = trail + [
            {"phase": "pre_movement", "timestamp": 1_001_000.0},
            {"phase": "live_escort", "action": "escort_started", "timestamp": 1_002_000.0},
        ]
        assert _escort_window(replanned) == (1_002_000.0, None, 1)

    async def test_silent_feed_is_unverified_not_recovered(self, fake_store):
        ctx = ConvoyContext(movement_id="pc2", vvip_class="Z", status="active")
        ctx.active_diversions = ["3003", "3004"]
        await fake_store.put(ctx)
        executor = FakeExecutor(routes={"get_live_traffic": {"observations": []},
                                        "get_historical_pattern": {"aggregates": []}})
        report = await run_post_clearance_workflow(
            movement_id="pc2", bridge=StubBridge(fail=True), executor=executor,
            context_store=fake_store, max_recovery_iterations=1)
        assert report["segments_recovered"] == 0
        assert report["segments_unverified"] == 2

    async def test_low_confidence_posture_keeps_the_staged_default(self, fake_store):
        ctx = ConvoyContext(movement_id="pc3", vvip_class="Z", status="active")
        await fake_store.put(ctx)
        brain = DeciderBrain(ActionDecider("immediate_full_restoration", confidence=0.2))
        report = await run_post_clearance_workflow(
            movement_id="pc3", bridge=StubBridge(fail=True), executor=FakeExecutor(),
            context_store=fake_store, decider_brain=brain)
        assert report["restoration_posture"] == "staged_reverse_order_restoration"
        assert "low confidence" in report["restoration_posture_source"]
        confident = DeciderBrain(ActionDecider("immediate_full_restoration", confidence=0.9))
        await fake_store.put(ConvoyContext(movement_id="pc4", vvip_class="Z"))
        report = await run_post_clearance_workflow(
            movement_id="pc4", bridge=StubBridge(fail=True), executor=FakeExecutor(),
            context_store=fake_store, decider_brain=confident)
        assert report["restoration_posture"] == "immediate_full_restoration"
        assert report["restoration_applied"] == {
            "steps": 0, "closures_lifted": 0, "order": "activation", "signal_assist": None}

    async def test_signal_assisted_recovery_goes_to_the_signal_officer(self, fake_store):
        ctx = ConvoyContext(movement_id="pc5", vvip_class="Z", status="active")
        ctx.event_id = EVENT_ID
        ctx.active_diversions = ["3003", "3004"]
        await fake_store.put(ctx)
        executor = FakeExecutor(routes={
            "get_live_traffic": {"observations": [
                {"segment_id": 3003, "congestion_idx": 0.1},
                {"segment_id": 3004, "congestion_idx": 0.1}]},
            "get_historical_pattern": {"aggregates": []},
        })
        report = await run_post_clearance_workflow(
            movement_id="pc5", bridge=StubBridge(fail=True), executor=executor,
            context_store=fake_store, max_recovery_iterations=1,
            decider_brain=DeciderBrain(ActionDecider("signal_assisted_recovery")))
        assert report["restoration_posture"] == "signal_assisted_recovery"
        assert report["restoration_order"] == [3004, 3003]
        # Presented as exactly what happened: one lift step, and the signal
        # part as a recommendation for the officer who owns the signals.
        assert report["restoration_applied"] == {
            "steps": 1, "closures_lifted": 2, "order": "reverse_activation",
            "signal_assist": ("recommended to the signal officer (advisory row; the brain "
                              "has no signal-control path)")}
        advisory = [r for r in fake_store.contexts["pc5"].decisions_log
                    if r.get("type") == "signal_assist_recommended"]
        assert len(advisory) == 1 and advisory[0]["segment_ids"] == [3004, 3003]
        assert advisory[0]["phase"] == "post_clearance_alert"
        assert EVENT_ID in advisory[0]["reason"]


class TestModelsConfigPath:
    def test_reads_models_config_not_the_retired_symlink(self, tmp_path):
        default = config.models_config_path({})
        assert default.parts[-3:] == ("models-config", "configs", "veer_models.yaml")
        assert "models" not in default.parts[:-3][-1:]
        override = tmp_path / "custom.yaml"
        override.write_text("services:\n  model_host: http://localhost:8083\n")
        assert config.models_config_path({"VEER_MODELS_CONFIG": str(override)}) == override
        urls = config.load_service_urls(env={"VEER_MODELS_CONFIG": str(override)})
        assert urls.model_host == "http://localhost:8083" and "veer_models.yaml" in urls.source

    def test_escort_switches(self):
        assert config.escort_sim_gps_enabled({"VVIP_ESCORT_SIM_GPS": "1"}) is True
        assert config.escort_sim_gps_enabled({}) is False
        assert config.officer_required({"VVIP_REQUIRE_OFFICER": "true"}) is True
        assert config.escort_sim_speed_kmh({"VVIP_ESCORT_SIM_SPEED_KMH": "900"}) == 120.0
        assert config.CITY_BBOX == (72.40, 22.90, 72.80, 23.20)
        assert math.isclose(config.escort_gps_timeout_sec({}), 300.0)
        assert Path(config.MODELS_CONFIG_PATH).name == "veer_models.yaml"


async def test_the_registry_keeps_a_bounded_number_of_finished_runs():
    registry = live_escort.EscortRegistry()

    async def finish(_run):
        return None

    for mid in ("m0", "m1", "m2", "m0"):  # m0 runs again last
        registry.start(EscortRun(mid), finish)
        await asyncio.sleep(0)
    gate = asyncio.Event()

    async def linger(_run):
        await gate.wait()

    alive = registry.start(EscortRun("live"), linger)
    await asyncio.sleep(0)
    registry.prune(keep=2)
    assert {m for m in ("m0", "m1", "m2", "live") if registry.get(m)} == {"m2", "m0", "live"}
    assert registry.active("live") is alive  # a running escort is never dropped
    gate.set()
    await alive.task


def test_escort_run_ids_are_unique():
    assert EscortRun("a").task_id != EscortRun("a").task_id
    assert uuid.UUID(hex=EscortRun("a").task_id)
