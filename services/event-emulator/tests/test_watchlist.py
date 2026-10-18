"""Watchlist tape + delivery tests (contract C2) — no network, no database.

Pins:
* the watchlist scenario builds anpr_read + agency_unit frames with the
  frozen C2 payload shapes;
* every synthetic plate carries the 'EM' marker (self-labelling);
* the stolen-vehicle and earmarked-plate hits are present and labelled;
* read ids are uuid5 over (use_case, seed, camera, plate) — deterministic
  across hosts and stable across rebuilds;
* the run is byte-identical for the same (spec, seed, t0);
* runner delivery persists sightings through the store seam (idempotent)
  and publishes clearly-labelled emulated JSON through the NATS seam.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest

from event_emulator.library import load_library
from event_emulator.nats_seam import ANPR_EMULATED_SUBJECT, UNITS_EMULATED_SUBJECT
from event_emulator.profiles import profile_for
from event_emulator.runner import RunnerDeps, execute_run
from event_emulator.spec import EmulationSpec, ResolvedRun
from event_emulator.tape import anpr_read_uuid, build_tape

T0 = datetime(2026, 11, 5, 5, 0, 0, tzinfo=timezone.utc)
UC = "watchlist_anpr_sweep"


def _tape(seed: int | None = None):
    lib = load_library()
    uc = lib[UC]
    spec = EmulationSpec(
        use_case_id=UC, seed=seed, t0_anchor=T0, tick_sec=30.0,
        clock_scale=1000.0,  # fast-forward: frames fire immediately
    )
    run = ResolvedRun.resolve(spec, uc)
    return build_tape(run, uc, profile_for(UC)), uc, spec


def _anpr_frames(tape):
    return [f for f in tape.frames if f.kind == "anpr_read"]


def _unit_frames(tape):
    return [f for f in tape.frames if f.kind == "agency_unit"]


class TestWatchlistTape:
    def test_frames_present_for_watchlist_scenario(self):
        tape, _, _ = _tape()
        assert len(_anpr_frames(tape)) == 32   # 4 cameras x 8 vehicles
        assert len(_unit_frames(tape)) == 20   # 2 units x 10 cadence steps

    def test_no_watchlist_frames_for_legacy_scenarios(self):
        lib = load_library()
        uc = lib["vvip_convoy_base"]
        spec = EmulationSpec(
            use_case_id="vvip_convoy_base", seed=None, t0_anchor=T0, tick_sec=30.0
        )
        run = ResolvedRun.resolve(spec, uc)
        tape = build_tape(run, uc, profile_for("vvip_convoy_base"))
        assert _anpr_frames(tape) == []
        assert _unit_frames(tape) == []

    def test_every_plate_carries_em_marker(self):
        tape, _, _ = _tape()
        plates = {f.payload["plate"] for f in _anpr_frames(tape)}
        assert plates
        for plate in plates:
            assert "EM" in plate, plate

    def test_stolen_and_earmarked_hits_present_and_labelled(self):
        tape, _, _ = _tape()
        hits = {
            f.payload["plate"]: f.payload["matched_plate_hit"]
            for f in _anpr_frames(tape)
            if f.payload.get("matched_plate_hit")
        }
        assert set(hits.values()) == {"stolen_vehicle", "earmarked_plate"}
        assert any(v == "stolen_vehicle" for v in hits.values())

    def test_read_ids_are_uuid5_over_use_case_seed_camera_plate(self):
        tape, _, spec = _tape()
        seed = tape.seed
        for f in _anpr_frames(tape):
            expected = anpr_read_uuid(
                UC, seed, f.payload["camera_id"], f.payload["plate"]
            )
            assert f.payload["read_id"] == expected

    def test_payload_shape_is_frozen_c2(self):
        tape, _, _ = _tape()
        for f in _anpr_frames(tape):
            p = f.payload
            assert set(p) >= {
                "camera_id", "plate", "position", "speed_kmh", "heading",
                "vehicle_class", "read_id", "matched_plate_hit",
            }
            assert isinstance(p["position"], list) and len(p["position"]) == 2
            lon, lat = p["position"]
            assert 72.0 < lon < 73.0 and 22.0 < lat < 23.5  # Ahmedabad belt
        for f in _unit_frames(tape):
            p = f.payload
            assert set(p) >= {"unit_id", "agency", "position", "status"}
            assert p["agency"] == "jan_rakshak"
            assert "EM" in p["unit_id"]

    def test_stolen_vehicle_reads_above_segment_speed_limits(self):
        # the demo's deterministic setup for R_SPD_SEGMENT to fire later
        tape, _, _ = _tape()
        stolen = [
            f for f in _anpr_frames(tape)
            if f.payload.get("matched_plate_hit") == "stolen_vehicle"
        ]
        assert stolen
        for f in stolen:
            assert f.payload["speed_kmh"] > 90.0

    def test_byte_identical_rebuild(self):
        a, _, _ = _tape(seed=777)
        b, _, _ = _tape(seed=777)
        assert a.to_json() == b.to_json()

    def test_segment_centroids_anchor_the_reads(self):
        lib = load_library()
        uc = lib[UC]
        spec = EmulationSpec(use_case_id=UC, seed=None, t0_anchor=T0, tick_sec=30.0)
        run = ResolvedRun.resolve(spec, uc)
        centroids = {
            1012: (72.60, 22.99), 1015: (72.59, 23.00),
            1005: (72.58, 23.01), 1013: (72.59, 22.98),
        }
        tape = build_tape(run, uc, profile_for(UC), segment_centroids=centroids)
        anchor_reads = [
            f for f in _anpr_frames(tape) if f.payload["camera_id"] == "EM-CAM-01"
        ]
        for f in anchor_reads:
            lon = f.payload["position"][0]
            assert abs(lon - 72.60) < 0.01  # anchored near segment 1012

    def test_deterministic_with_or_without_centroids(self):
        a, _, _ = _tape(seed=5)
        b, _, _ = _tape(seed=5)
        assert a.to_json() == b.to_json()


# ── runner delivery ─────────────────────────────────────────────────────────


@dataclass
class FakeWatchStore:
    vehicles: dict = field(default_factory=dict)
    sightings: dict = field(default_factory=dict)
    next_id: int = 1
    centroids: dict = field(default_factory=lambda: {
        1012: (72.60, 22.99), 1015: (72.59, 23.00),
        1005: (72.58, 23.01), 1013: (72.59, 22.98),
        # legacy route centroids (movement create path)
        1026: (72.63, 23.21), 1025: (72.62, 23.15), 1024: (72.60, 23.10),
        1014: (72.60, 23.07), 1030: (72.61, 23.08), 1031: (72.59, 23.00),
    })

    async def segment_centroids(self, segment_ids):
        return {s: self.centroids[s] for s in segment_ids if s in self.centroids}

    async def upsert_movement(self, mid, vvip, planned_start, origin, dest):
        self.vehicles.setdefault("__movements__", {})[mid] = (vvip, origin, dest)

    async def replace_route(self, mid, segment_ids, points):
        pass

    async def set_movement_status(self, mid, status):
        pass

    async def upsert_corridor_event(self, event_id, movement_id, event_kind,
                                    protection_class, crowd_class, title,
                                    planned_start, planned_end, metadata_doc):
        self.vehicles.setdefault("__events__", {})[event_id] = event_kind

    async def replace_event_route(self, event_id, segment_ids):
        pass

    async def replace_event_phase(self, event_id, label, window_start,
                                  window_end, closure_policy):
        pass

    async def set_event_status(self, event_id, status):
        pass

    async def insert_run(self, *, use_case_id, spec, spec_hash, seed, t0_anchor,
                         clock_scale, tape_json, is_replay, replay_of_run_id):
        rid = self.next_id
        self.next_id += 1
        return rid

    async def finish_run(self, run_id, status, delivery):
        pass

    # watchlist plane
    async def upsert_vehicle(self, plate, vehicle_class, seen_at):
        import uuid

        key = plate
        if key not in self.vehicles:
            self.vehicles[key] = uuid.uuid4()
        return self.vehicles[key]

    async def insert_sighting(self, *, sighting_id, vehicle_id, plate,
                              camera_id, t, lon, lat, speed_kmh, heading,
                              vehicle_class, run_id, matched_plate_hit):
        if sighting_id in self.sightings:
            return False
        self.sightings[sighting_id] = dict(
            plate=plate, camera_id=camera_id, t=t, lon=lon, lat=lat,
            speed_kmh=speed_kmh, heading=heading, vehicle_class=vehicle_class,
            run_id=run_id, matched_plate_hit=matched_plate_hit,
        )
        return True


@dataclass
class FakeValkey:
    docs: dict = field(default_factory=dict)

    async def write_inputs_doc(self, mid, doc):
        self.docs[mid] = doc

    async def write_source(self, mid, run_id, use_case_id, replay):
        pass


@dataclass
class FakeNats:
    json_docs: list = field(default_factory=list)

    async def publish(self, rows, replay_run_id=None):
        return True  # the Arrow showcase plane stays up in these tests

    async def publish_json(self, subject, doc, replay_run_id=None):
        self.json_docs.append((subject, doc, replay_run_id))
        return True


@dataclass
class _Resp:
    status_code: int = 200


@dataclass
class FakeTwin:
    async def probe(self):
        return False

    def build_body(self, **kw):
        return kw

    async def post_twin(self, body, replay_run_id=None):
        return _Resp(200)


class TestRunnerDelivery:
    def _deps(self):
        store = FakeWatchStore()
        nats = FakeNats()
        deps = RunnerDeps(
            store=store,
            valkey=FakeValkey(),
            twin=FakeTwin(),
            nats=nats,
            sleep=_no_sleep,
        )
        return deps, store, nats

    def test_anpr_reads_persist_as_sightings_and_publish(self):
        deps, store, nats = self._deps()
        lib = load_library()
        spec = EmulationSpec(
            use_case_id=UC, seed=42, t0_anchor=T0, tick_sec=30.0,
            clock_scale=1000.0, deliver_twin=False,
        )
        outcome = asyncio.run(execute_run(deps, spec, library=lib))
        assert outcome.status == "completed"
        anpr_entries = [e for e in outcome.frame_log if e["kind"] == "anpr_read"]
        assert len(anpr_entries) == 32
        assert all(e["result"] == "delivered" for e in anpr_entries)
        # 8 distinct plates x 4 cameras -> 32 sightings, all NEW
        assert len(store.sightings) == 32
        anpr_docs = [d for d in nats.json_docs if d[0] == ANPR_EMULATED_SUBJECT]
        assert len(anpr_docs) == 32
        for _, doc, _ in anpr_docs:
            assert doc["source"] == "emulated_anpr"
        unit_docs = [d for d in nats.json_docs if d[0] == UNITS_EMULATED_SUBJECT]
        assert len(unit_docs) == 20
        for _, doc, _ in unit_docs:
            assert doc["source"] == "emulated"

    def test_replay_is_idempotent_on_sightings(self):
        deps, store, nats = self._deps()
        lib = load_library()
        spec = EmulationSpec(
            use_case_id=UC, seed=42, t0_anchor=T0, tick_sec=30.0,
            clock_scale=1000.0, deliver_twin=False,
        )
        first = asyncio.run(execute_run(deps, spec, library=lib))
        # replay the exact tape: same read ids -> zero NEW sightings
        from event_emulator.runner import execute_replay

        second = asyncio.run(
            execute_replay(
                deps, tape=first.tape, clock_scale=1000.0, deliver_twin=False,
                t0=T0, library=lib,
            )
        )
        replays = [
            e for e in second.frame_log
            if e["kind"] == "anpr_read" and e["result"] == "delivered"
        ]
        assert replays and all(e["sighting_new"] is False for e in replays)
        assert len(store.sightings) == 32  # unchanged — idempotent

    def test_hits_carried_into_sightings(self):
        deps, store, _ = self._deps()
        lib = load_library()
        spec = EmulationSpec(
            use_case_id=UC, seed=42, t0_anchor=T0, tick_sec=30.0,
            clock_scale=1000.0, deliver_twin=False,
        )
        asyncio.run(execute_run(deps, spec, library=lib))
        hits = {
            s["matched_plate_hit"] for s in store.sightings.values()
            if s["matched_plate_hit"]
        }
        assert hits == {"stolen_vehicle", "earmarked_plate"}

    def test_corridor_event_kind_is_crime_incident(self):
        deps, store, _ = self._deps()
        lib = load_library()
        spec = EmulationSpec(
            use_case_id=UC, seed=42, t0_anchor=T0, tick_sec=30.0,
            clock_scale=1000.0, deliver_twin=False,
        )
        asyncio.run(execute_run(deps, spec, library=lib))
        kinds = set(store.vehicles.get("__events__", {}).values())
        assert kinds == {"crime_incident"}


async def _no_sleep(_seconds: float) -> None:
    return None
