"""Runner + replay tests with fake seams — no network, no database.

These pin the cause-tape delivery contract: which seams receive which
frames, that live injections carry NO emulator header (indistinguishable
at the API boundary), and that replays re-post the stored tape VERBATIM
with the X-Emulation-Replay header — the single tell.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest

from event_emulator.library import load_library
from event_emulator.runner import RunnerDeps, execute_replay, execute_run
from event_emulator.spec import EmulationSpec
from event_emulator.tape import CauseTape


@dataclass
class FakeStore:
    movements: dict = field(default_factory=dict)
    routes: dict = field(default_factory=dict)
    runs: dict = field(default_factory=dict)
    events: dict = field(default_factory=dict)
    event_routes: dict = field(default_factory=dict)
    event_phases: dict = field(default_factory=dict)
    next_id: int = 1
    centroids: dict = field(default_factory=lambda: {
        1001: (72.57, 23.02), 1006: (72.57, 23.03), 1010: (72.56, 23.04),
        1007: (72.58, 23.03), 1032: (72.59, 23.09), 1002: (72.58, 23.05),
        1040: (72.51, 23.00), 1021: (72.52, 23.01), 1022: (72.53, 23.02),
        1008: (72.54, 23.03), 1012: (72.59, 22.99), 1015: (72.58, 23.00),
        1005: (72.57, 23.00), 1013: (72.58, 22.98), 1026: (72.63, 23.21),
        1025: (72.62, 23.15), 1024: (72.60, 23.10), 1014: (72.60, 23.07),
        1030: (72.61, 23.08), 1028: (72.62, 23.20), 1027: (72.62, 23.18),
        1031: (72.59, 23.00), 1038: (72.60, 22.99),
    })

    async def segment_centroids(self, segment_ids):
        return {s: self.centroids[s] for s in segment_ids if s in self.centroids}

    async def upsert_movement(self, mid, vvip, planned_start, origin, dest):
        self.movements[mid] = dict(
            vvip_class=vvip, status="planning",
            planned_start=planned_start, origin=origin, destination=dest,
        )

    async def replace_route(self, mid, segment_ids, points):
        self.routes[mid] = (list(segment_ids), list(points))

    async def set_movement_status(self, mid, status):
        self.movements[mid]["status"] = status

    async def upsert_corridor_event(self, event_id, movement_id, event_kind,
                                    protection_class, crowd_class, title,
                                    planned_start, planned_end, metadata_doc):
        self.events[event_id] = dict(
            event_kind=event_kind, protection_class=protection_class,
            crowd_class=crowd_class, title=title, status="planning",
            movement_id=movement_id, planned_start=planned_start,
            planned_end=planned_end, metadata=metadata_doc,
        )

    async def replace_event_route(self, event_id, segment_ids):
        self.event_routes[event_id] = list(segment_ids)

    async def replace_event_phase(self, event_id, label, window_start,
                                  window_end, closure_policy):
        self.event_phases[event_id] = (label, window_start, window_end,
                                       closure_policy)

    async def set_event_status(self, event_id, status):
        self.events[event_id]["status"] = status

    async def insert_run(self, *, use_case_id, spec, spec_hash, seed, t0_anchor,
                         clock_scale, tape_json, is_replay, replay_of_run_id):
        rid = self.next_id
        self.next_id += 1
        self.runs[rid] = dict(
            use_case_id=use_case_id, spec=spec, spec_hash=spec_hash, seed=seed,
            t0_anchor=t0_anchor, clock_scale=clock_scale, tape_json=tape_json,
            status="running", is_replay=is_replay, replay_of=replay_of_run_id,
        )
        return rid

    async def finish_run(self, run_id, status, delivery):
        self.runs[run_id]["status"] = status
        self.runs[run_id]["delivery"] = delivery

    async def fetch_run(self, run_id):
        return self.runs.get(run_id)


@dataclass
class FakeValkey:
    docs: dict = field(default_factory=dict)
    sources: dict = field(default_factory=dict)

    async def write_inputs_doc(self, mid, doc):
        self.docs[mid] = doc

    async def write_source(self, mid, run_id, use_case_id, replay):
        self.sources[mid] = (run_id, use_case_id, replay)


@dataclass
class FakeTwin:
    posts: list = field(default_factory=list)
    responses: list = field(default_factory=list)

    def build_body(self, **kw):
        return kw

    async def post_twin(self, body, replay_run_id=None):
        self.posts.append({"body": body, "replay_run_id": replay_run_id})
        return self.responses.pop(0) if self.responses else _Resp(200)

    async def probe(self):
        return True


@dataclass
class _Resp:
    status_code: int


@dataclass
class FakeNats:
    batches: list = field(default_factory=list)
    subject: str = "corridor.traffic.emulated"

    async def publish(self, rows, replay_run_id=None):
        self.batches.append({"rows": list(rows), "replay_run_id": replay_run_id,
                             "subject": self.subject})
        return True


class NoSleep:
    """Clock replacement: execute frames as fast as possible."""

    async def __call__(self, delay: float) -> None:
        assert delay >= 0


def _deps(**kw) -> RunnerDeps:
    d = RunnerDeps(
        store=kw.get("store", FakeStore()),
        valkey=kw.get("valkey", FakeValkey()),
        twin=kw.get("twin", FakeTwin()),
        nats=kw.get("nats", FakeNats()),
        sleep=NoSleep(),
    )
    return d


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
        # movement row created with the config's class, completed at horizon
        mv = store.movements[outcome.movement_id]
        assert mv["vvip_class"] == "X"
        assert mv["status"] == "completed"
        # route spine written with real centroids
        segs, pts = store.routes[outcome.movement_id]
        assert segs == [1032, 1002, 1001]
        assert len(pts) == 3
        # inputs docs written for every inputs_doc frame
        vk: FakeValkey = deps.valkey
        n_docs = sum(1 for f in outcome.tape.frames if f.kind == "inputs_doc")
        assert len(vk.docs) == 1  # keyed per movement; last doc wins in fake
        assert vk.sources[outcome.movement_id][0] == outcome.run_id

    async def test_corridor_event_row_written(self):
        deps = _deps()
        outcome = await execute_run(deps, _spec("motera_stadium_egress"),
                                    library=load_library())
        store: FakeStore = deps.store
        ev = store.events[outcome.movement_id]
        assert ev["event_kind"] == "egress_surge"
        assert ev["protection_class"] == "none"   # public event (007 rule)
        assert ev["crowd_class"] == "EXTREME"
        assert ev["status"] == "completed"
        assert ev["movement_id"] == outcome.movement_id  # soft link
        assert store.event_routes[outcome.movement_id] == [1032, 1002, 1001]
        label, _ws, _we, policy = store.event_phases[outcome.movement_id]
        assert label == "egress_surge"
        assert policy == {"advance_closure_sec": 60, "closure_type": "signal_hold"}

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
        await execute_run(deps, _spec("flood_evacuation_corridor"), library=lib)
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

    async def test_twin_failure_is_recorded_not_fatal(self):
        twin = FakeTwin(responses=[_Resp(503)])
        deps = _deps(twin=twin)
        outcome = await execute_run(deps, _spec("vvip_convoy_base"),
                                    library=load_library())
        assert outcome.status == "completed"  # DB/valkey seams still drove
        twin_entry = [e for e in outcome.frame_log if e["kind"] == "twin_post"]
        assert twin_entry[0]["result"] == "failed"
        assert twin_entry[0]["http_status"] == 503

    async def test_missing_seams_skip_honestly(self):
        deps = RunnerDeps(store=None, valkey=None, twin=None, nats=None, sleep=NoSleep())
        outcome = await execute_run(deps, _spec("metro_work_zone"),
                                    library=load_library())
        assert outcome.run_id is None
        assert outcome.delivery["skipped"] > 0
        assert outcome.status == "completed"  # nothing failed; store absent

    async def test_unknown_use_case_raises(self):
        deps = _deps()
        with pytest.raises(KeyError):
            await execute_run(deps, _spec("nope"), library=load_library())


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
        # VERBATIM: identical frame sequence and payloads
        assert [f.to_dict() for f in second.tape.frames] == \
            [f.to_dict() for f in first.tape.frames]
        assert second.tape.seed == first.tape.seed
        assert second.tape.movement_id == first.tape.movement_id
        # re-anchored t0 (later wall clock)
        assert second.tape.t0_anchor_iso >= first.tape.t0_anchor_iso
        # the ONE tell: replay header on twin POST and NATS batches
        assert twin.posts[0]["replay_run_id"] == second.run_id
        assert all(b["replay_run_id"] == second.run_id for b in nats.batches)
        # ledger marks the lineage
        store2: FakeStore = deps2.store
        assert store2.runs[second.run_id]["is_replay"] is True
        assert store2.runs[second.run_id]["replay_of"] == first.run_id

    async def test_replay_reanimates_same_movement_row(self):
        lib = load_library()
        deps = _deps()
        first = await execute_run(deps, _spec("national_day_parade"), library=lib)
        assert deps.store.movements[first.movement_id]["status"] == "completed"

        deps2 = _deps()
        second = await execute_replay(
            deps2, tape=first.tape, clock_scale=1000.0, replay_of=first.run_id,
            library=lib,
        )
        assert second.movement_id == first.movement_id
        assert deps2.store.movements[first.movement_id]["status"] == "completed"

    async def test_replay_from_ledger_json(self):
        lib = load_library()
        deps = _deps()
        first = await execute_run(deps, _spec("metro_work_zone"), library=lib)
        raw = json.loads(first.tape.to_json())
        tape = CauseTape.from_dict(raw)
        assert tape.to_json() == first.tape.to_json()
