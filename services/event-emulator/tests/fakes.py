"""In-memory fakes of the emulator's seams (no network, no database).

FakeOracle enforces the real C-F lifecycle table (planning → approved |
active | cancelled; approved → active | cancelled; active → completed |
cancelled), releases a closing event's open dispatches the way the
oracle's auto-close does (answering with ``closed_dispatches``), and
answers C-E / dispatch POSTs with the oracle's shapes, so the tests pin
what the emulator ASKS the oracle to do — never an outcome the emulator
decides itself.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

TRANSITIONS = {
    "planning": ("approved", "active", "cancelled"),
    "approved": ("active", "cancelled"),
    "active": ("completed", "cancelled"),
    "completed": (),
    "cancelled": (),
}

#: Each segment's on-road anchor (store.segment_midpoints) in the fake
#: network; a segment's fake line runs 0.004 deg either side of it.
ANCHORS = {
    1001: (72.57, 23.02), 1006: (72.57, 23.03), 1010: (72.56, 23.04),
    1007: (72.58, 23.03), 1032: (72.59, 23.09), 1002: (72.58, 23.05),
    1040: (72.51, 23.00), 1021: (72.52, 23.01), 1022: (72.53, 23.02),
    1008: (72.54, 23.03), 1012: (72.59, 22.99), 1015: (72.58, 23.00),
    1005: (72.57, 23.00), 1013: (72.58, 22.98), 1026: (72.63, 23.21),
    1025: (72.62, 23.15), 1024: (72.60, 23.10), 1014: (72.60, 23.07),
    1030: (72.61, 23.08), 1028: (72.62, 23.20), 1027: (72.62, 23.18),
    1031: (72.59, 23.00), 1038: (72.60, 22.99),
    # the use-case routes on the real network (WP-20) add these
    1009: (72.572, 23.051), 1029: (72.620, 23.077), 1041: (72.582, 23.067),
    1043: (72.589, 23.045), 1048: (72.630, 23.096), 1049: (72.598, 23.021),
}


def _resp(status: int, body: Any) -> httpx.Response:
    return httpx.Response(status, json=body)


@dataclass
class FakeStore:
    """Every CorridorStore method the runner / living city / reaper call."""

    movements: dict = field(default_factory=dict)
    routes: dict = field(default_factory=dict)
    runs: dict = field(default_factory=dict)
    events: dict = field(default_factory=dict)
    event_routes: dict = field(default_factory=dict)
    event_phases: dict = field(default_factory=dict)
    status_log: list = field(default_factory=list)
    heartbeats: list = field(default_factory=list)
    next_id: int = 1
    anchors: dict = field(default_factory=lambda: dict(ANCHORS))
    paths: dict = field(default_factory=dict)
    oneway: dict = field(default_factory=dict)          # segment -> one-way
    vias: dict = field(default_factory=dict)            # (from, to) -> [junction point]
    network: Any = "seed"                               # store.network_key()
    cameras: list = field(default_factory=list)
    programs: dict = field(default_factory=dict)
    legs: dict = field(default_factory=dict)
    dispatched: set = field(default_factory=set)
    adjacency: dict | None = None        # segment -> junctions (None: seed network)
    #: (from, to) moves the CURRENT network does not allow (route_problems);
    #: empty: every recorded route is a legal path
    illegal_moves: set = field(default_factory=set)
    route_check_error: Exception | None = None
    ledger_accepts_interrupted: bool = False
    # this process's identity, stamped into every run row (store.runner)
    runner: dict = field(default_factory=lambda: {
        "host": "test-host", "port": 8090, "pid": 1, "boot": "boot-now"})

    async def probe(self, timeout: float = 2.0) -> bool:
        return True

    async def network_key(self):
        return self.network

    async def segment_midpoints(self, segment_ids):
        return {s: self.anchors[s] for s in segment_ids if s in self.anchors}

    def _lines(self, segment_ids):
        out = {}
        for s in segment_ids:
            if s in self.paths:
                out[s] = self.paths[s]
            elif s in self.anchors:
                lon, lat = self.anchors[s]
                out[s] = [(lon - 0.004, lat), (lon + 0.004, lat)]
        return out

    async def segment_paths(self, segment_ids):
        return self._lines(segment_ids)

    async def route_geometry(self, segment_ids):
        ids = list(segment_ids)
        lines = self._lines(ids)
        return {
            "lines": lines,
            "oneway": {s: bool(self.oneway.get(s)) for s in lines},
            "vias": [list(self.vias.get((a, b), [])) for a, b in zip(ids, ids[1:])],
        }

    async def camera_sites(self):
        return [dict(c) for c in self.cameras]

    async def signal_programs(self):
        return dict(self.programs)

    async def junction_legs(self, junction_ids):
        return {j: self.legs.get(j, []) for j in junction_ids}

    async def dispatched_units(self, unit_ids):
        return {u for u in unit_ids if u in self.dispatched}

    async def segment_junction_ids(self):
        if self.adjacency is not None:
            return {s: set(js) for s, js in self.adjacency.items()}
        # default: the seed network the living city names (every site's
        # roads meet its junctions; every beat's roads exist)
        from event_emulator.ambient import PATROLS, SITES

        out: dict = {}
        for site in SITES:
            for s in site.segments:
                out.setdefault(s, set()).update(site.junctions)
        for _unit, _beat, route in PATROLS:
            for s in route:
                out.setdefault(s, set())
        return out

    async def route_problems(self, segment_ids):
        if self.route_check_error is not None:
            raise self.route_check_error
        ids = [int(s) for s in segment_ids]
        if not ids:
            return ["no route recorded"]
        known = set(self.anchors) | set(self.paths)
        unknown = [s for s in ids if s not in known]
        out = [f"unknown segments {unknown}"] if unknown else []
        return out + [f"{a} -> {b} is not a legal move" for a, b in zip(ids, ids[1:])
                      if a in known and b in known and (a, b) in self.illegal_moves]

    async def upsert_movement(self, mid, vvip, planned_start, origin, dest):
        # create-or-reanimate: a re-animation moves the ends too (store.py)
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
                                    planned_start, planned_end, metadata_doc,
                                    description="", note="event created"):
        prior = self.events.get(event_id, {}).get("status")
        self.events[event_id] = dict(
            event_kind=event_kind, protection_class=protection_class,
            crowd_class=crowd_class, title=title, description=description,
            status="planning", movement_id=movement_id,
            planned_start=planned_start, planned_end=planned_end,
            metadata=dict(metadata_doc),
        )
        self.status_log.append((event_id, prior, "planning", "event-emulator (system)", note))
        return prior

    async def replace_event_route(self, event_id, segment_ids):
        self.event_routes[event_id] = list(segment_ids)

    async def replace_event_phase(self, event_id, label, window_start,
                                  window_end, closure_policy):
        self.event_phases[event_id] = (label, window_start, window_end, closure_policy)

    async def live_emulator_events(self):
        out = []
        for eid, ev in self.events.items():
            meta = ev.get("metadata") or {}
            if meta.get("source") == "event-emulator" and ev["status"] in (
                    "planning", "approved", "active"):
                rid = meta.get("run_id")
                out.append({"event_id": eid, "status": ev["status"],
                            "run_id": None if rid is None else str(rid),
                            "mode": meta.get("mode"), "planned_end": ev.get("planned_end"),
                            "updated_at": None})
        return out

    async def insert_run(self, *, use_case_id, spec, spec_hash, seed, t0_anchor,
                         clock_scale, tape_json, is_replay, replay_of_run_id):
        rid = self.next_id
        self.next_id += 1
        doc = dict(spec)
        doc.update(use_case_id=use_case_id, is_replay=is_replay,
                   replay_of_run_id=replay_of_run_id)
        doc.setdefault("heartbeat_at", datetime.now(timezone.utc).isoformat())
        doc.setdefault("runner", dict(self.runner))
        self.runs[rid] = dict(
            run_id=rid, use_case_id=use_case_id, spec=doc, spec_hash=spec_hash,
            seed=seed, t0_anchor=t0_anchor, clock_scale=clock_scale,
            tape=json.loads(tape_json), status="running", is_replay=is_replay,
            replay_of=replay_of_run_id, created_at=datetime.now(timezone.utc),
            ended_at=None,
        )
        return rid

    async def update_run_tape(self, run_id, tape_json):
        self.runs[run_id]["tape"] = json.loads(tape_json)

    async def heartbeat_run(self, run_id, doc):
        self.heartbeats.append((run_id, doc))
        if self.runs.get(run_id, {}).get("status") == "running":
            self.runs[run_id]["spec"].update(doc)
            self.runs[run_id]["spec"]["heartbeat_at"] = datetime.now(timezone.utc).isoformat()

    async def finish_run(self, run_id, status, doc):
        column = status
        if status == "interrupted" and not self.ledger_accepts_interrupted:
            column = "stopped"
        row = self.runs[run_id]
        row["status"] = column
        row["spec"].update(doc)
        row["spec"]["final_status"] = status
        row["ended_at"] = datetime.now(timezone.utc)
        row["delivery"] = doc.get("delivery")
        return column

    async def fetch_run(self, run_id):
        row = self.runs.get(run_id)
        if row is None:
            return None
        return {k: row[k] for k in ("run_id", "spec_hash", "spec", "seed", "t0_anchor",
                                    "clock_scale", "status", "tape", "created_at", "ended_at")}

    async def fetch_runs(self, limit=50):
        rows = []
        for rid in sorted(self.runs, reverse=True)[:limit]:
            r = await self.fetch_run(rid)
            r.pop("tape")
            r["spec"] = {k: v for k, v in r["spec"].items() if k != "frame_log"}
            rows.append(r)
        return rows

    async def running_runs(self):
        return [
            {"run_id": rid, "use_case_id": r["spec"].get("use_case_id"),
             "heartbeat_at": r["spec"].get("heartbeat_at"),
             "progress": r["spec"].get("progress"), "mode": r["spec"].get("mode"),
             "runner": r["spec"].get("runner"), "created_at": r["created_at"]}
            for rid, r in sorted(self.runs.items()) if r["status"] == "running"
        ]

    async def finished_run_ids(self, run_ids):
        return {r for r in run_ids
                if r not in self.runs or self.runs[r]["status"] != "running"}


@dataclass
class FakeValkey:
    docs: dict = field(default_factory=dict)
    sources: dict = field(default_factory=dict)
    up: bool = True

    async def probe(self):
        if not self.up:
            raise ConnectionError("valkey down")
        return True

    async def write_inputs_doc(self, mid, doc):
        self.docs[mid] = doc

    async def write_source(self, mid, run_id, use_case_id, replay):
        self.sources[mid] = (run_id, use_case_id, replay)


@dataclass
class FakeTwin:
    posts: list = field(default_factory=list)
    status_codes: list = field(default_factory=list)
    library: set | None = None          # None: every scenario is known
    library_misses: set = field(default_factory=set)
    delay: float = 0.0

    def build_body(self, **kw):
        body = {k: v for k, v in kw.items() if k != "scenario_id"}
        if kw.get("scenario_id") is not None:
            body["scenario_id"] = kw["scenario_id"]
        return body

    async def post_twin(self, body, replay_run_id=None):
        if self.delay:
            await asyncio.sleep(self.delay)
        self.posts.append({"body": dict(body), "replay_run_id": replay_run_id})
        if self.status_codes:
            return _resp(self.status_codes.pop(0), {"detail": "scripted"})
        sid = body.get("scenario_id")
        if sid is not None and self.library is not None and sid not in self.library:
            return _resp(404, {"detail": f"unknown scenario {sid!r}"})
        return _resp(200, {"runs": [{"replication": 0, "total_disruption_vehicle_hours": 1.5,
                                     "max_queue_length_m": 120.0,
                                     "convoy_transit_time_sec": 300.0,
                                     "ambulance_delay_sec": 20.0, "recovery_sec": 60.0}],
                           "calibrated": False, "forecast_coupling": "internal",
                           "seed": body.get("seed")})

    async def deliver(self, body, replay_run_id=None):
        from event_emulator.twin_seam import TwinSeam

        seam = TwinSeam.__new__(TwinSeam)
        seam.library_misses = self.library_misses
        seam.post_twin = self.post_twin  # type: ignore[method-assign]
        return await TwinSeam.deliver(seam, body, replay_run_id)

    async def probe(self):
        return True


@dataclass
class FakeNats:
    batches: list = field(default_factory=list)
    json_docs: list = field(default_factory=list)
    subject: str = "corridor.traffic.emulated"
    up: bool = True

    async def probe(self, timeout: float = 1.0):
        return self.up

    async def publish(self, rows, replay_run_id=None):
        if not self.up:
            return False
        self.batches.append({"rows": list(rows), "replay_run_id": replay_run_id,
                             "subject": self.subject})
        return True

    async def publish_json(self, subject, doc, replay_run_id=None):
        if not self.up:
            return False
        self.json_docs.append((subject, doc, replay_run_id))
        return True


@dataclass
class FakeOracle:
    """C-F lifecycle + C-E sightings + dispatch requests, in memory."""

    store: FakeStore | None = None
    officer: str = "event-emulator (system)"
    transitions: list = field(default_factory=list)
    sightings: dict = field(default_factory=dict)
    sighting_bodies: list = field(default_factory=list)
    dispatches: list = field(default_factory=list)
    earmarked: dict = field(default_factory=dict)    # plate -> 'stolen_vehicle'|'watchlist'
    rules: Any = None                                # fn(body) -> list[rule ids]
    down: bool = False
    # dispatch_id -> {event_id, unit_id, agency, status, body}
    dispatch_rows: dict = field(default_factory=dict)

    def _status(self, event_id):
        if self.store is None or event_id not in self.store.events:
            return None
        return self.store.events[event_id]["status"]

    def _release(self, event_id, to_status):
        """The oracle's close_event_dispatches: open dispatches of a closing
        event are cancelled (on scene + completed event → completed)."""
        closed = []
        for did, row in self.dispatch_rows.items():
            if row["event_id"] != event_id or row["status"] not in (
                    "requested", "approved", "en_route", "on_scene"):
                continue
            to = ("completed" if to_status == "completed" and row["status"] == "on_scene"
                  else "cancelled")
            closed.append({"dispatch_id": did, "unit_id": row["unit_id"],
                           "agency": row["agency"], "from_status": row["status"],
                           "to_status": to})
            row["status"] = to
        return closed

    def decide(self, dispatch_id, to_status="approved"):
        """A duty officer's decision on a requested dispatch."""
        assert self.dispatch_rows[dispatch_id]["status"] == "requested"
        self.dispatch_rows[dispatch_id]["status"] = to_status

    async def event_status(self, event_id, to_status, note=None, replay_run_id=None):
        if self.down:
            raise httpx.ConnectError("oracle down")
        current = self._status(event_id)
        if current is None:
            return _resp(404, {"detail": "not found"})
        allowed = TRANSITIONS[current]
        if to_status not in allowed:
            return _resp(409, {"detail": "illegal", "from_status": current,
                               "allowed": list(allowed)})
        self.store.events[event_id]["status"] = to_status
        self.transitions.append((event_id, current, to_status, self.officer, note))
        closed = self._release(event_id, to_status) if to_status in (
            "completed", "cancelled") else []
        return _resp(200, {"event_id": event_id, "from_status": current,
                           "to_status": to_status, "actor": self.officer,
                           "closed_dispatches": closed})

    async def post_sighting(self, read, replay_run_id=None):
        if self.down:
            raise httpx.ConnectError("oracle down")
        self.sighting_bodies.append((dict(read), replay_run_id))
        t = datetime.fromisoformat(read["t"])
        if (t - datetime.now(timezone.utc)).total_seconds() > 30:
            return _resp(409, {"detail": "future read"})
        rid = read["read_id"]
        if rid in self.sightings:
            return _resp(200, {"sighting_id": rid, "vehicle_id": "v", "duplicate": True,
                               "hit": self.sightings[rid]["hit"], "recommendations": []})
        hit = self.earmarked.get(read["plate"])
        fired = list(self.rules(read)) if self.rules else []
        withheld: list = []
        if hit == "stolen_vehicle":
            # C-N, as the oracle does it: the penal rules still run, are
            # WITHHELD, and fold into ONE R_STOLEN_SIGHTING (no fine to the
            # owner of a stolen vehicle)
            withheld, fired = fired, ["R_STOLEN_SIGHTING"]
        self.sightings[rid] = {"body": dict(read), "hit": hit, "fired": fired}
        return _resp(200, {
            "sighting_id": rid, "vehicle_id": str(uuid.uuid4()), "duplicate": False,
            "hit": hit,
            "recommendations": [{"recommendation_id": str(uuid.uuid4()), "rule_id": r,
                                 "status": "pending"} for r in fired],
            "rules_fired": fired,
            "challan_withheld": hit == "stolen_vehicle",
            "withheld_rules": withheld,
        })

    async def request_dispatch(self, event_id, body):
        if self.down:
            raise httpx.ConnectError("oracle down")
        status = self._status(event_id)
        if status is None:
            return _resp(404, {"detail": "event not found"})
        if status not in ("planning", "approved", "active"):
            return _resp(409, {"detail": "event is not active"})
        self.dispatches.append((event_id, dict(body)))
        did = str(uuid.uuid4())
        self.dispatch_rows[did] = {"event_id": event_id, "unit_id": body.get("unit_id"),
                                   "agency": body.get("agency"), "status": "requested",
                                   "body": dict(body)}
        return _resp(200, {"dispatch_id": did, "event_id": event_id,
                           "status": "requested", **body})


class NoSleep:
    """Clock replacement: execute frames as fast as possible."""

    def __init__(self, on_call=None) -> None:
        self.calls = 0
        self.on_call = on_call

    async def __call__(self, delay: float) -> None:
        assert delay >= 0
        self.calls += 1
        if self.on_call is not None:
            self.on_call(self.calls)
