"""Runner + replay tests with fake seams — no network, no database.

These pin the cause-tape delivery contract: which seams receive which
frames; that the corridor event goes 'active' at t0 and 'completed' at the
horizon THROUGH the oracle's lifecycle seam (C-F) with the system officer;
that an activation the oracle could not take at t0 is retried every few
wall seconds until it lands, and a run whose event never went live is
'failed', never 'completed'; that windows and stamps are wall time; that
live injections carry NO emulator header while replays re-post the stored
tape VERBATIM with the X-Emulation-Replay header — the single tell; that a
stop closes the event and the row; and that a run delivering nothing is
never 'completed'.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from event_emulator.library import load_library
from event_emulator.runner import (
    ACTIVATE_RETRY_SEC,
    RunControl,
    RunnerDeps,
    cap_frame_log,
    execute_replay,
    execute_run,
)
from event_emulator.spec import EmulationSpec
from event_emulator.tape import CauseTape, chain_polyline, movement_uuid
from fakes import FakeNats, FakeOracle, FakeStore, FakeTwin, FakeValkey, NoSleep


def _deps(**kw) -> RunnerDeps:
    store = kw.get("store", FakeStore())
    return RunnerDeps(
        store=store,
        valkey=kw.get("valkey", FakeValkey()),
        twin=kw.get("twin", FakeTwin()),
        nats=kw.get("nats", FakeNats()),
        oracle=kw.get("oracle", FakeOracle(store=store)),
        sleep=kw.get("sleep", NoSleep()),
    )


def _spec(uid: str, **kw) -> EmulationSpec:
    params = dict(use_case_id=uid, clock_scale=1000.0, tick_sec=30.0)
    params.update(kw)
    return EmulationSpec(**params)


class TestExecuteRun:
    async def test_full_delivery(self):
        deps = _deps()
        outcome = await execute_run(deps, _spec("motera_stadium_egress"),
                                    library=load_library())
        assert outcome.status == "completed"
        assert outcome.delivery["failed"] == 0
        store: FakeStore = deps.store
        mv = store.movements[outcome.movement_id]
        assert mv["vvip_class"] == "X"
        assert mv["status"] == "completed"
        segs, pts = store.routes[outcome.movement_id]
        assert segs == [1032, 1041, 1009, 1002, 1001]
        # ONE line: every segment's road line chained in route order (the
        # fake lines do not touch: 2 points each), from the movement's
        # origin to its destination
        assert len(pts) == 2 * len(segs)
        assert (mv["origin"], mv["destination"]) == (pts[0], pts[-1])
        vk: FakeValkey = deps.valkey
        assert len(vk.docs) == 1  # keyed per movement; last doc wins in fake
        assert vk.sources[outcome.movement_id][0] == outcome.run_id

    async def test_movement_starts_and_ends_where_its_route_does(self):
        # the motera route on a chained network whose segments are stored in
        # both directions: 1041 and 1002 run against the drive
        j = [(72.594, 23.092), (72.586, 23.076), (72.578, 23.064), (72.574, 23.050),
             (72.572, 23.036), (72.571, 23.022)]
        bend = (72.591, 23.085)
        store = FakeStore(
            paths={1032: [j[0], bend, j[1]], 1041: [j[2], j[1]], 1009: [j[2], j[3]],
                   1002: [j[4], j[3]], 1001: [j[4], j[5]]},
            vias={(1032, 1041): [j[1]], (1041, 1009): [j[2]], (1009, 1002): [j[3]],
                  (1002, 1001): [j[4]]})
        deps = _deps(store=store)
        outcome = await execute_run(deps, _spec("motera_stadium_egress"),
                                    library=load_library())
        mv = store.movements[outcome.movement_id]
        segs, line = store.routes[outcome.movement_id]
        assert segs == [1032, 1041, 1009, 1002, 1001]
        # origin: where the first segment is entered; destination: where the
        # last is left — the route's ends, not a segment's middle
        assert (mv["origin"], mv["destination"]) == (j[0], j[5])
        # one LineString, oriented, each shared junction once
        assert line == [j[0], bend, j[1], j[2], j[3], j[4], j[5]]

    async def test_movement_keeps_the_on_road_anchors_without_route_lines(self):
        class NoLines(FakeStore):
            async def route_geometry(self, segment_ids):
                return {"lines": {}, "oneway": {}, "vias": []}

        store = NoLines()
        deps = _deps(store=store)
        outcome = await execute_run(deps, _spec("kumbh_scale_gathering"),
                                    library=load_library())
        segs, line = store.routes[outcome.movement_id]
        assert line == [store.anchors[s] for s in segs]
        mv = store.movements[outcome.movement_id]
        assert (mv["origin"], mv["destination"]) == (line[0], line[-1])

    async def test_event_lifecycle_goes_through_c_f(self):
        deps = _deps()
        outcome = await execute_run(deps, _spec("motera_stadium_egress"),
                                    library=load_library())
        oracle: FakeOracle = deps.oracle
        moves = [(t[1], t[2]) for t in oracle.transitions if t[0] == outcome.movement_id]
        assert moves == [("planning", "active"), ("active", "completed")]
        assert all(t[3] == "event-emulator (system)" for t in oracle.transitions)
        store: FakeStore = deps.store
        ev = store.events[outcome.movement_id]
        assert ev["status"] == "completed"
        # creation row of the status ledger written with the event
        assert store.status_log[0][1:4] == (None, "planning", "event-emulator (system)")

    async def test_corridor_event_row_written(self):
        deps = _deps()
        outcome = await execute_run(deps, _spec("motera_stadium_egress"),
                                    library=load_library())
        store: FakeStore = deps.store
        ev = store.events[outcome.movement_id]
        assert ev["event_kind"] == "egress_surge"
        assert ev["protection_class"] == "none"   # public event (007 rule)
        assert ev["crowd_class"] == "EXTREME"
        assert ev["movement_id"] == outcome.movement_id  # soft link
        meta = ev["metadata"]
        assert meta["source"] == "event-emulator"
        assert meta["mode"] == "use_case"
        assert meta["run_id"] == outcome.run_id
        assert store.event_routes[outcome.movement_id] == [1032, 1041, 1009, 1002, 1001]
        label, _ws, _we, policy = store.event_phases[outcome.movement_id]
        assert label == "egress_surge"
        assert policy == {"advance_closure_sec": 60, "closure_type": "signal_hold"}

    async def test_event_window_is_wall_time(self):
        deps = _deps()
        t0 = datetime(2026, 10, 22, 6, 0, tzinfo=timezone.utc)
        outcome = await execute_run(deps, _spec("rath_yatra_monsoon_ambulance",
                                                clock_scale=10.0, t0_anchor=t0),
                                    library=load_library())
        ev = deps.store.events[outcome.movement_id]
        assert ev["planned_start"] == t0
        # a 60-minute scenario at x10 holds the road for 6 wall minutes
        assert ev["planned_end"] == t0 + timedelta(minutes=6)
        _label, ws, we, _p = deps.store.event_phases[outcome.movement_id]
        assert (ws, we) == (t0, t0 + timedelta(minutes=6))

    async def test_live_runs_get_run_scoped_identity(self):
        lib = load_library()
        deps = _deps()
        a = await execute_run(deps, _spec("riverfront_marathon"), library=lib)
        b = await execute_run(deps, _spec("riverfront_marathon"), library=lib)
        assert a.run_id != b.run_id
        assert a.movement_id != b.movement_id
        seed = lib["riverfront_marathon"].config["seed"]
        assert a.movement_id == movement_uuid("riverfront_marathon", seed, a.run_id)

    async def test_protected_movement_keeps_vvip_class(self):
        deps = _deps()
        outcome = await execute_run(deps, _spec("vvip_convoy_base"),
                                    library=load_library())
        ev = deps.store.events[outcome.movement_id]
        assert ev["protection_class"] == "Z+"
        assert ev["event_kind"] == "protected_movement"

    async def test_twin_body_carries_causes_verbatim(self):
        twin = FakeTwin()
        deps = _deps(twin=twin)
        lib = load_library()
        cfg = lib["flood_evacuation_corridor"].config
        outcome = await execute_run(deps, _spec("flood_evacuation_corridor"), library=lib)
        assert len(twin.posts) == 1
        body = twin.posts[0]["body"]
        assert body["route_segment_ids"] == cfg["route_segment_ids"]
        assert body["departure_time"] == cfg["departure_time"]
        assert body["closure_policy"] == cfg["closure_policy"]
        assert body["horizon_min"] == cfg["horizon_min"]
        assert body["n_replications"] == cfg["n_replications"]
        assert body["weather"] == cfg["weather"]
        assert body["event_overrides"]["waterlogging"] == 3
        assert body["event_overrides"]["breakdown"] == 1.8
        assert "ambulance_rate_multiplier" in body["event_overrides"]
        kinds = {e["type"] for e in body["forced_events"]}
        assert "waterlogging" in kinds
        assert "emergency_vehicle" in kinds
        assert "emergency_request" not in kinds
        assert body["scenario_id"] == "flood_evacuation_corridor"
        assert outcome.twin["delivery_level"] == "twin_scenario_library"
        assert outcome.twin["summary"]["replications"] == 1

    async def test_twin_library_miss_resends_without_scenario(self):
        twin = FakeTwin(library={"flood_evacuation_corridor"})
        deps = _deps(twin=twin)
        outcome = await execute_run(deps, _spec("watchlist_anpr_sweep"),
                                    library=load_library())
        assert [("scenario_id" in p["body"]) for p in twin.posts] == [True, False]
        assert outcome.twin["delivery_level"] == "twin_route_only"
        assert outcome.twin["library_miss"] is True
        assert outcome.twin["scenario_id_sent"] is None
        # remembered: the next run of the use case skips the 404 round trip
        await execute_run(deps, _spec("watchlist_anpr_sweep"), library=load_library())
        assert "scenario_id" not in twin.posts[-1]["body"]
        assert len(twin.posts) == 3

    async def test_twin_summary_lands_in_the_ledger(self):
        deps = _deps()
        outcome = await execute_run(deps, _spec("kumbh_scale_gathering"),
                                    library=load_library())
        spec = deps.store.runs[outcome.run_id]["spec"]
        assert spec["twin"]["delivery_level"] == "twin_scenario_library"
        assert spec["twin"]["http_status"] == 200
        assert spec["final_status"] == "completed"
        assert spec["frame_log"] and len(spec["frame_log"]) <= 300

    async def test_live_injection_carries_no_replay_header(self):
        twin = FakeTwin()
        nats = FakeNats()
        deps = _deps(twin=twin, nats=nats)
        await execute_run(deps, _spec("riverfront_marathon"), library=load_library())
        assert twin.posts[0]["replay_run_id"] is None
        assert all(b["replay_run_id"] is None for b in nats.batches)

    async def test_nats_batches_only_on_emulated_subject(self):
        nats = FakeNats()
        deps = _deps(nats=nats)
        await execute_run(deps, _spec("kumbh_scale_gathering"), library=load_library())
        assert nats.batches
        assert all(b["subject"] == "corridor.traffic.emulated" for b in nats.batches)
        assert all(len(b["rows"]) == 3 for b in nats.batches)  # route size

    async def test_probe_rows_sit_on_their_segments_anchor(self):
        # each per-segment row is placed at the store's on-road anchor of
        # the segment it claims (the midpoint along the line), never
        # anywhere else
        nats = FakeNats()
        deps = _deps(nats=nats)
        await execute_run(deps, _spec("kumbh_scale_gathering"), library=load_library())
        anchors = deps.store.anchors
        for batch in nats.batches:
            for sid, lon, lat, _speed, _cong in batch["rows"]:
                assert (lon, lat) == anchors[sid]

    async def test_twin_failure_is_recorded_not_fatal(self):
        twin = FakeTwin(status_codes=[503])
        deps = _deps(twin=twin)
        outcome = await execute_run(deps, _spec("vvip_convoy_base"),
                                    library=load_library())
        assert outcome.status == "completed"  # DB/valkey seams still drove
        twin_entry = [e for e in outcome.frame_log if e["kind"] == "twin_post"]
        assert twin_entry[0]["result"] == "failed"
        assert twin_entry[0]["http_status"] == 503
        assert twin_entry[0]["delivery_level"] == "twin_rejected"

    async def test_missing_seams_skip_honestly_and_fail(self):
        deps = RunnerDeps(store=None, valkey=None, twin=None, nats=None,
                          oracle=None, sleep=NoSleep())
        outcome = await execute_run(deps, _spec("metro_work_zone"),
                                    library=load_library())
        assert outcome.run_id is None
        assert outcome.delivery["skipped"] > 0
        assert outcome.delivery["delivered"] == 0
        # nothing reached anything: never reported 'completed'
        assert outcome.status == "failed"

    async def test_oracle_down_leaves_event_open_and_says_so(self):
        store = FakeStore()
        oracle = FakeOracle(store=store, down=True)
        deps = _deps(store=store, oracle=oracle)
        outcome = await execute_run(deps, _spec("metro_work_zone"), library=load_library())
        lifecycle = [e for e in outcome.frame_log if e["kind"] == "event_lifecycle"]
        assert lifecycle and all(e["result"] == "failed" for e in lifecycle)
        assert store.events[outcome.movement_id]["status"] == "planning"
        # frames around an event that never went live are not a completed run
        assert outcome.status == "failed"
        assert outcome.reason.startswith("the corridor event never became active (C-F)")

    async def test_unknown_use_case_raises(self):
        deps = _deps()
        with pytest.raises(KeyError):
            await execute_run(deps, _spec("nope"), library=load_library())


class _OutageClock:
    """A virtual wall clock the injected sleep advances (frames fire at
    t0 + t_sec / clock_scale on it), with the oracle's outage window: down
    from the start, back ``down_for`` virtual seconds later (None: never).
    Every lifecycle call is timed on it."""

    def __init__(self, oracle: FakeOracle, down_for: float | None) -> None:
        self.now = datetime.now(timezone.utc).timestamp()
        self.t0 = self.now
        self.oracle = oracle
        self.down_for = down_for
        self.calls: list[tuple[float, str]] = []
        oracle.down = True
        real = oracle.event_status

        async def timed(event_id, to_status, note=None, replay_run_id=None):
            self.calls.append((self.now - self.t0, to_status))
            return await real(event_id, to_status, note, replay_run_id)

        oracle.event_status = timed   # type: ignore[method-assign]

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.now += delay
        if self.down_for is not None and self.now - self.t0 >= self.down_for:
            self.oracle.down = False


def _outage_deps(down_for: float | None) -> tuple[RunnerDeps, _OutageClock]:
    store = FakeStore()
    oracle = FakeOracle(store=store)
    clock = _OutageClock(oracle, down_for)
    deps = _deps(store=store, oracle=oracle, sleep=clock.sleep)
    deps.clock = clock
    return deps, clock


class TestLateActivation:
    """The oracle cannot take the t0 'active' (an outage): the create frame
    counts failed, the activation is retried every ACTIVATE_RETRY_SEC of
    wall time, and the run is completed only if the event went live."""

    async def test_activation_lands_once_the_oracle_is_back(self):
        deps, clock = _outage_deps(down_for=14.0)
        control = RunControl()
        outcome = await execute_run(deps, _spec("metro_work_zone", clock_scale=10.0),
                                    library=load_library(), control=control)
        assert outcome.status == "completed", outcome.reason
        mid = outcome.movement_id
        moves = [(t[1], t[2]) for t in deps.oracle.transitions if t[0] == mid]
        assert moves == [("planning", "active"), ("active", "completed")]
        # attempts: t0, then one per 5-10 wall seconds until it landed
        attempts = [t for t, to in clock.calls if to == "active"]
        assert attempts[0] < 1.0 and len(attempts) >= 3
        gaps = [b - a for a, b in zip(attempts, attempts[1:])]
        assert all(ACTIVATE_RETRY_SEC <= g <= 2 * ACTIVATE_RETRY_SEC for g in gaps), gaps
        assert attempts[-1] >= 14.0
        # the create frame was re-counted delivered when the event went live
        create = next(e for e in outcome.frame_log if e["kind"] == "event_row")
        assert create["result"] == "delivered" and create["activated_late"] is True
        assert create["activation_attempts"] == len(attempts) and "reason" not in create
        late = [e for e in outcome.frame_log
                if e["kind"] == "event_lifecycle" and e.get("late")]
        assert len(late) == 1 and late[0]["result"] == "delivered"
        assert control.progress.failed == 0
        assert control.progress.delivered == control.progress.frame == control.progress.total
        assert deps.store.movements[mid]["status"] == "completed"
        spec = deps.store.runs[outcome.run_id]["spec"]
        assert spec["final_status"] == "completed" and spec["event_status"] == "completed"
        assert spec["event_activation"] == {"active": True, "attempts": len(attempts),
                                            "late": True, "error": None}

    async def test_no_lifecycle_seam_is_not_retried(self):
        deps, clock = _outage_deps(down_for=None)
        deps.oracle = None
        outcome = await execute_run(deps, _spec("metro_work_zone", clock_scale=10.0),
                                    library=load_library())
        assert outcome.status == "failed"
        assert "oracle seam not configured" in outcome.reason
        create = next(e for e in outcome.frame_log if e["kind"] == "event_row")
        assert create["result"] == "failed" and create["activation_attempts"] == 1

    async def test_progress_counts_the_pending_event_as_failed(self):
        deps, clock = _outage_deps(down_for=None)
        control = RunControl()
        seen: list[tuple[str | None, int]] = []

        async def sleep(delay: float) -> None:
            await clock.sleep(delay)
            seen.append((control.event_status, control.progress.failed))

        deps.sleep = sleep
        await execute_run(deps, _spec("metro_work_zone", clock_scale=10.0),
                          library=load_library(), control=control)
        # mid-run the poll says: event still 'planning', its create frame failed
        assert seen and all(s == ("planning", 1) for s in seen[1:-1])

    async def test_an_event_that_never_went_live_fails_the_run(self):
        deps, clock = _outage_deps(down_for=None)
        control = RunControl()
        outcome = await execute_run(deps, _spec("metro_work_zone", clock_scale=10.0),
                                    library=load_library(), control=control)
        assert outcome.status == "failed"
        assert outcome.reason.startswith("the corridor event never became active (C-F)")
        assert "ConnectError" in outcome.reason
        attempts = [t for t, to in clock.calls if to == "active"]
        assert len(attempts) > 30                       # retried for the whole run
        create = next(e for e in outcome.frame_log if e["kind"] == "event_row")
        assert create["result"] == "failed" and "not active" in create["reason"]
        assert create["activation_attempts"] == len(attempts)
        # the failed frames: the create (never live) and the horizon close
        # (the oracle is still down) — every other frame was delivered
        failed = [e for e in outcome.frame_log
                  if e["kind"] == "event_row" and e["result"] == "failed"]
        assert len(failed) == 2 and control.progress.failed == 2
        assert control.progress.delivered == control.progress.frame - 2
        assert control.event_status == "planning"
        row = deps.store.runs[outcome.run_id]
        assert row["status"] == "failed" and row["spec"]["reason"] == outcome.reason
        # every inputs doc still reached Valkey: the frames were not the fault
        assert deps.valkey.docs

    async def test_an_event_closed_by_an_officer_is_not_retried(self):
        deps, clock = _outage_deps(down_for=8.0)
        oracle: FakeOracle = deps.oracle
        cancelled: list[str] = []
        real_sleep = clock.sleep

        async def sleep(delay: float) -> None:
            await real_sleep(delay)
            # back up — and a duty officer cancels the still-planning event
            for eid, ev in deps.store.events.items():
                if not oracle.down and ev["status"] == "planning" and not cancelled:
                    cancelled.append(eid)
                    await oracle.event_status(eid, "cancelled", "duty officer")

        deps.sleep = sleep
        outcome = await execute_run(deps, _spec("metro_work_zone", clock_scale=10.0),
                                    library=load_library())
        assert cancelled == [outcome.movement_id]
        assert outcome.status == "failed"
        assert "cancelled before it became active" in outcome.reason
        actives = [t for t, to in clock.calls if to == "active"]
        assert all(t <= 8.0 + 2 * ACTIVATE_RETRY_SEC for t in actives)   # then it stopped
        # nothing left for the run to close: no completion attempt either
        assert deps.store.events[outcome.movement_id]["status"] == "cancelled"
        complete = [e for e in outcome.frame_log
                    if e["kind"] == "event_row" and e.get("t_sec", 0) > 0]
        assert complete and complete[-1]["result"] == "skipped"


class TestStop:
    async def test_stop_mid_run_closes_event_and_row(self):
        control = RunControl()
        sleep = NoSleep(on_call=lambda n: control.request_stop("stopped") if n == 40 else None)
        deps = _deps(sleep=sleep)
        outcome = await execute_run(deps, _spec("rath_yatra_monsoon_ambulance"),
                                    library=load_library(), control=control)
        assert outcome.status == "stopped"
        store: FakeStore = deps.store
        assert store.events[outcome.movement_id]["status"] == "completed"
        assert store.movements[outcome.movement_id]["status"] == "completed"
        assert store.runs[outcome.run_id]["status"] == "stopped"
        progress = store.runs[outcome.run_id]["spec"]["progress"]
        assert 0 < progress["frame"] < progress["total"]
        last = deps.oracle.transitions[-1]
        assert last[2] == "completed" and "stopped by operator" in last[4]

    async def test_interrupted_falls_back_to_stopped_column(self):
        control = RunControl()
        control.request_stop("interrupted")
        deps = _deps()
        outcome = await execute_run(deps, _spec("metro_work_zone"),
                                    library=load_library(), control=control)
        assert outcome.status == "interrupted"
        row = deps.store.runs[outcome.run_id]
        assert row["status"] == "stopped"           # 009 CHECK predates 'interrupted'
        assert row["spec"]["final_status"] == "interrupted"



# ── scripted unit tracks ride the store's DRAWN paths (contract C-K) ────────

#: The flagship's track segments as the store draws them once OSM-traced:
#: the seed endpoints, a bend the library's embedded route_geometry lacks.
DRAWN = {
    1005: [(72.5798, 23.0254), (72.5850, 23.0300), (72.5900, 23.0180)],
    1012: [(72.5798, 23.0254), (72.5760, 23.0290), (72.5830, 23.0310)],
    1015: [(72.5714, 23.0225), (72.5750, 23.0300), (72.5798, 23.0254)],
}


def _off_line_m(point, line) -> float:
    """Metres from a point to a polyline (local equirectangular frame)."""
    import math

    lon0, lat0 = point
    kx = 111_320.0 * math.cos(math.radians(lat0))
    xy = [((lon - lon0) * kx, (lat - lat0) * 110_574.0) for lon, lat in line]
    best = math.inf
    for (x1, y1), (x2, y2) in zip(xy, xy[1:]):
        dx, dy = x2 - x1, y2 - y1
        t = max(0.0, min(1.0, -(x1 * dx + y1 * dy) / ((dx * dx + dy * dy) or 1.0)))
        best = min(best, math.hypot(x1 + t * dx, y1 + t * dy))
    return best


class _TrackStore(FakeStore):
    """FakeStore that records segment_paths reads (or fails them)."""

    def __init__(self, *, fail: bool = False, **kw) -> None:
        super().__init__(**kw)
        self.fail = fail
        self.path_calls: list[list[int]] = []

    async def segment_paths(self, segment_ids):
        self.path_calls.append(list(segment_ids))
        if self.fail:
            raise RuntimeError("store down")
        return await super().segment_paths(segment_ids)


def _track(tape: CauseTape, unit_id: str) -> list[tuple[float, float]]:
    return [tuple(f.payload["position"]) for f in tape.frames
            if f.kind == "agency_unit" and f.payload["unit_id"] == unit_id]


class TestUnitTrackGeometry:
    async def test_recorded_units_follow_the_stores_drawn_paths(self):
        store = _TrackStore(paths={s: list(line) for s, line in DRAWN.items()})
        deps = _deps(store=store)
        outcome = await execute_run(deps, _spec("rath_yatra_monsoon_ambulance"),
                                    library=load_library())
        # the flagship has no watchlist knobs: ONE read, for its track segments
        assert store.path_calls == [[1012, 1005, 1015]]
        lib = load_library()["rath_yatra_monsoon_ambulance"].config["unit_tracks"]
        library_line = [tuple(p) for p in lib["route_geometry"]["1005"]]
        beat = chain_polyline([1012, 1005], DRAWN)
        run = chain_polyline([1005, 1015], DRAWN)
        for unit, line in (("JR-EM-21", beat), ("JR-EM-22", beat), ("AMB-EM-108", run)):
            points = _track(outcome.tape, unit)
            assert points, unit
            assert max(_off_line_m(p, line) for p in points) < 1.0, unit
        # …and off the library's straighter line where the drawn road bends
        assert max(_off_line_m(p, library_line) for p in _track(outcome.tape, "AMB-EM-108")
                   if p[0] > 72.5798) > 300.0

    async def test_a_store_that_cannot_answer_keeps_the_library_geometry(self):
        store = _TrackStore(fail=True)
        deps = _deps(store=store)
        outcome = await execute_run(deps, _spec("rath_yatra_monsoon_ambulance"),
                                    library=load_library())
        assert outcome.status == "completed"
        lib = load_library()["rath_yatra_monsoon_ambulance"].config["unit_tracks"]
        lines = {int(s): [tuple(p) for p in pts] for s, pts in lib["route_geometry"].items()}
        beat = chain_polyline([1012, 1005], lines)
        points = _track(outcome.tape, "JR-EM-21")
        assert points and max(_off_line_m(p, beat) for p in points) < 1.0


class TestFrameLog:
    def test_cap_keeps_failures_first(self):
        log = [{"frame": i, "kind": "inputs_doc", "result": "delivered"} for i in range(1000)]
        log[10]["result"] = "failed"
        kept, dropped = cap_frame_log(log, cap=100)
        assert len(kept) == 100 and dropped == 900
        assert log[10] in kept
        assert kept[-1] is log[-1]


class TestReplay:
    async def test_replay_reposts_tape_verbatim_with_header(self):
        lib = load_library()
        deps = _deps()
        first = await execute_run(deps, _spec("rath_yatra_ahmedabad"), library=lib)

        twin = FakeTwin()
        nats = FakeNats()
        deps2 = _deps(twin=twin, nats=nats)
        second = await execute_replay(
            deps2, tape=first.tape, clock_scale=1000.0, replay_of=first.run_id,
            library=lib,
        )
        assert [f.to_dict() for f in second.tape.frames] == \
            [f.to_dict() for f in first.tape.frames]
        assert second.tape.seed == first.tape.seed
        assert second.tape.movement_id == first.tape.movement_id
        assert second.tape.t0_anchor_iso >= first.tape.t0_anchor_iso
        assert twin.posts[0]["replay_run_id"] == second.run_id
        assert all(b["replay_run_id"] == second.run_id for b in nats.batches)
        store2: FakeStore = deps2.store
        assert store2.runs[second.run_id]["is_replay"] is True
        assert store2.runs[second.run_id]["replay_of"] == first.run_id

    async def test_replay_reanimates_same_event_through_c_f(self):
        lib = load_library()
        deps = _deps()
        first = await execute_run(deps, _spec("national_day_parade"), library=lib)
        assert deps.store.events[first.movement_id]["status"] == "completed"
        second = await execute_replay(
            deps, tape=first.tape, clock_scale=1000.0, replay_of=first.run_id,
            library=lib,
        )
        assert second.movement_id == first.movement_id
        store: FakeStore = deps.store
        assert store.events[first.movement_id]["status"] == "completed"
        # the re-animation is its own ledger row (completed → planning)
        assert any(r[1] == "completed" and r[2] == "planning" for r in store.status_log)
        meta = store.events[first.movement_id]["metadata"]
        assert meta["is_replay"] is True and meta["run_id"] == second.run_id

    async def test_replay_from_ledger_json(self):
        lib = load_library()
        deps = _deps()
        first = await execute_run(deps, _spec("metro_work_zone"), library=lib)
        raw = json.loads(first.tape.to_json())
        tape = CauseTape.from_dict(raw)
        assert tape.to_json() == first.tape.to_json()
        stored = deps.store.runs[first.run_id]["tape"]
        assert CauseTape.from_dict(stored).to_json() == first.tape.to_json()
