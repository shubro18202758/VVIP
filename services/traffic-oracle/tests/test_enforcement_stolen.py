"""Stolen-vehicle e-challan suppression — contract C-N (WP-19).

The registered owner of a car reported stolen is the VICTIM: no e-challan
is ever proposed or approved for that car.  Covered here:

    * the pure rules: under a StolenVehicleContext evaluate_rules returns
      ONE R_STOLEN_SIGHTING (the read + the penal rules it would have
      fired, withheld) — the penal evaluation itself is untouched, and the
      penal vocabulary (RULE_IDS) stays exactly the 010 tuple;
    * the FIR reference: only the reference leaves the officer's reason;
    * the engine: an active stolen-vehicle earmark (which outranks any
      other active earmark) persists exactly one R_STOLEN_SIGHTING with
      ``context.challan_withheld``; any other earmark keeps the penal rules;
    * the decision endpoint: approving a PENAL recommendation of a stolen
      vehicle is 409 {detail: 'e-challan withheld — vehicle reported
      stolen', reason: 'stolen_vehicle_fir'} and writes nothing; rejecting
      it, and approving the R_STOLEN_SIGHTING, are 200;
    * the ingest seam answers ``challan_withheld`` / ``withheld_rules``;
    * a pursuit opens from an approved R_STOLEN_SIGHTING, and no pursuit
      422 calls the authorisation a fine;
    * the city entity and feed say 'e-challan withheld' (and never relabel
      a penal row decided before the vehicle was reported stolen);
    * migration 017's CHECK is exactly ALL_RULE_IDS.

An optional DB tier (VEER_TEST_PG_DSN = a SANDBOX store with 001-017 +
seed; never the live :56434 store) runs the engine against real tables
inside a transaction that is rolled back.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from traffic_oracle.city import common
from traffic_oracle.city.entities import recommendation_entity
from traffic_oracle.city.feed import enforcement_items
from traffic_oracle.enforcement import engine, rules
from traffic_oracle.enforcement.rules import (
    ALL_RULE_IDS,
    CHALLAN_WITHHELD_TEXT,
    EVIDENCE_RULE_IDS,
    RULE_IDS,
    ClosureContext,
    SegmentContext,
    SightingContext,
    StolenVehicleContext,
    evaluate_rules,
    fir_reference,
)

REPO = Path(__file__).resolve().parents[3]
M017 = REPO / "services" / "corridor-store" / "migrations" / "017_enforcement_stolen_sighting.sql"

T0 = datetime(2026, 10, 18, 15, 0, 4, tzinfo=timezone.utc)
VEHICLE = uuid.UUID("1b94e9d2-7e11-4174-a735-7dd3be3189cf")


def _sighting(speed_kmh: float | None = 112.0, earmark_state: str | None = "flagged",
              t: datetime = T0) -> SightingContext:
    return SightingContext(
        sighting_id=str(uuid.uuid4()),
        plate="GJ27EM9001",
        camera_id="EM-CAM-02",
        t=t,
        lon=72.573251,
        lat=23.02397,
        source="emulated_anpr",
        speed_kmh=speed_kmh,
        heading=78.0,
        matched_plate_hit="stolen_vehicle",
        earmark_state=earmark_state,
    )


SEGMENT = SegmentContext(1015, 60.0, "Nehru Bridge Road")
STOLEN = StolenVehicleContext(fir_ref="4401/2026", earmark_state="flagged")


# ─── vocabulary ──────────────────────────────────────────────────────────────


class TestVocabulary:
    def test_penal_tuple_is_unchanged_and_the_evidence_rule_is_separate(self):
        assert RULE_IDS == ("R_SPD_SEGMENT", "R_SIGNAL_VIOLATION", "R_CLOSED_CORRIDOR_ENTRY")
        assert EVIDENCE_RULE_IDS == ("R_STOLEN_SIGHTING",)
        assert ALL_RULE_IDS == RULE_IDS + EVIDENCE_RULE_IDS
        assert all(rules.is_penal(r) for r in RULE_IDS)
        assert not rules.is_penal("R_STOLEN_SIGHTING") and not rules.is_penal(None)

    def test_migration_017_check_is_exactly_all_rule_ids(self):
        text = M017.read_text()
        m = re.search(
            r"ADD CONSTRAINT enforcement_recommendations_rule_id_check "
            r"CHECK \(rule_id IN \((.*?)\)\)",
            text,
            re.S,
        )
        assert m, "017 must re-add the rule_id CHECK"
        assert re.findall(r"'([A-Z_]+)'", m.group(1)) == list(ALL_RULE_IDS)
        assert "DROP CONSTRAINT IF EXISTS enforcement_recommendations_rule_id_check" in text

    def test_the_one_withheld_wording(self):
        assert CHALLAN_WITHHELD_TEXT == "e-challan withheld — vehicle reported stolen"


# ─── pure rules ──────────────────────────────────────────────────────────────


class TestPureRules:
    def test_without_a_stolen_context_nothing_changes(self):
        s = _sighting()
        assert evaluate_rules(s, SEGMENT, None) == evaluate_rules(s, SEGMENT, None, stolen=None)
        (rec,) = evaluate_rules(s, SEGMENT, None)
        assert rec.rule_id == "R_SPD_SEGMENT"

    def test_a_stolen_vehicle_gets_one_sighting_and_no_penal_rule(self):
        s = _sighting(speed_kmh=112.0)
        recs = evaluate_rules(s, SEGMENT, None, stolen=STOLEN)
        assert [r.rule_id for r in recs] == ["R_STOLEN_SIGHTING"]
        rec = recs[0]
        assert rec.recommendation == (
            "stolen vehicle (FIR 4401/2026) sighted at EM-CAM-02 — "
            "e-challan withheld, vehicle reported stolen"
        )
        ev = rec.evidence
        # the read itself ...
        assert (ev["sighting_id"], ev["plate"], ev["camera_id"]) == (
            s.sighting_id, "GJ27EM9001", "EM-CAM-02"
        )
        assert (ev["speed_kmh"], ev["heading"], ev["segment_id"]) == (112.0, 78.0, 1015)
        assert ev["t"] == T0.isoformat() and ev["source"] == "emulated_anpr"
        # ... and the penal rule it WOULD have fired, withheld
        assert ev["challan_withheld"] is True
        assert ev["withheld_reason"] == "vehicle reported stolen"
        assert ev["lawful_basis"] == "stolen_vehicle_fir" and ev["fir_ref"] == "4401/2026"
        (withheld,) = ev["withheld_rules"]
        assert withheld["rule_id"] == "R_SPD_SEGMENT"
        assert withheld["evidence"]["excess_kmh"] == 52.0
        # the evidence is reproducible JSON (deterministic, persisted as-is)
        json.dumps(ev, sort_keys=True)

    def test_a_sighting_is_recorded_even_without_a_violation(self):
        recs = evaluate_rules(_sighting(speed_kmh=40.0), SEGMENT, None, stolen=STOLEN)
        assert [r.rule_id for r in recs] == ["R_STOLEN_SIGHTING"]
        assert recs[0].evidence["withheld_rules"] == []

    def test_every_penal_rule_is_folded_closure_entry_included(self):
        closure = ClosureContext(
            event_id=str(uuid.uuid4()), title="Z+ convoy", event_kind="protected_movement",
            window_start=T0 - timedelta(minutes=5), window_end=T0 + timedelta(minutes=5),
            covers_position=True,
        )
        penal = evaluate_rules(_sighting(), SEGMENT, None, (closure,))
        assert {r.rule_id for r in penal} == {"R_SPD_SEGMENT", "R_CLOSED_CORRIDOR_ENTRY"}
        (rec,) = evaluate_rules(_sighting(), SEGMENT, None, (closure,), stolen=STOLEN)
        assert [w["rule_id"] for w in rec.evidence["withheld_rules"]] == [
            r.rule_id for r in penal
        ]

    def test_text_without_a_fir_reference(self):
        (rec,) = evaluate_rules(_sighting(), SEGMENT, None, stolen=StolenVehicleContext())
        assert rec.recommendation == (
            "stolen vehicle (FIR) sighted at EM-CAM-02 — "
            "e-challan withheld, vehicle reported stolen"
        )
        assert rec.evidence["fir_ref"] is None

    @pytest.mark.parametrize(
        "reason, ref",
        [
            ("STOLEN VEHICLE reported — FIR 4401/2026, Kalupur PS; God's Eye Morning", "4401/2026"),
            ("FIR No. 12/2025 at Navrangpura", "12/2025"),
            ("fir no 33", "33"),
            ("FIR: 4401/2026/", "4401/2026"),
            ("FIR registered at Kalupur", None),  # a reference carries a digit
            ("FIREFIGHTERS 2026", None),
            ("no reference at all", None),
            (None, None),
        ],
    )
    def test_fir_reference_extracts_only_the_reference(self, reason, ref):
        assert fir_reference(reason) == ref


# ─── engine ──────────────────────────────────────────────────────────────────


@dataclass
class EngineConn:
    """Answers evaluate_sighting's context queries (all parameter-bound)."""

    earmark: dict | None
    speed_limit: float = 60.0
    inserted: list[tuple] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)

    async def fetchrow(self, sql: str, *args: Any):
        self.queries.append(sql)
        if "FROM corridor.vehicle_earmarks" in sql:
            return self.earmark
        if "FROM corridor.road_segments" in sql:
            return {"segment_id": 1015, "road_name": "Nehru Bridge Road",
                    "speed_limit_kmh": self.speed_limit, "dist_m": 8.0}
        if "FROM corridor.junctions j" in sql:
            return None  # no signalized junction within 40 m
        if sql.lstrip().startswith("INSERT INTO events.enforcement_recommendations"):
            self.inserted.append(args)
            return {"recommendation_id": uuid.uuid4(), "created_at": T0}
        raise AssertionError("unexpected fetchrow: " + sql[:80])

    async def fetch(self, sql: str, *args: Any):
        self.queries.append(sql)
        if "FROM events.corridor_events e JOIN events.event_routes r" in sql:
            return []  # no active closure
        raise AssertionError("unexpected fetch: " + sql[:80])

    async def fetchval(self, sql: str, *args: Any):
        if sql == common._SQL_COLUMN:
            return True  # a 018 store
        raise AssertionError("unexpected fetchval: " + sql[:80])


def _srow(speed: float = 112.0) -> dict:
    return {
        "sighting_id": uuid.uuid4(), "vehicle_id": VEHICLE, "plate": "GJ27EM9001",
        "camera_id": "EM-CAM-02", "t": datetime.now(timezone.utc) - timedelta(minutes=1),
        "lon": 72.573251, "lat": 23.02397, "speed_kmh": speed, "heading": 78.0,
        "vehicle_class": "car", "source": "emulated_anpr",
        "matched_plate_hit": "stolen_vehicle",
    }


STOLEN_EARMARK = {
    "state": "flagged",
    "lawful_basis": "stolen_vehicle_fir",
    "reason": "STOLEN VEHICLE reported — FIR 4401/2026, Kalupur PS",
}


class TestEngine:
    async def test_a_stolen_earmark_persists_one_sighting_and_withholds_the_challan(self):
        common.reset_relation_cache()
        conn = EngineConn(earmark=STOLEN_EARMARK)
        srow = _srow()
        out = await engine.evaluate_sighting(conn, srow["sighting_id"], srow=srow)
        assert [r.rule_id for r in out.fired] == ["R_STOLEN_SIGHTING"]
        (args,) = conn.inserted
        assert args[2] == "R_STOLEN_SIGHTING"  # rule_id
        assert "e-challan withheld" in args[3]
        evidence = json.loads(args[4])
        assert evidence["withheld_rules"][0]["rule_id"] == "R_SPD_SEGMENT"
        audit = json.loads(args[5])
        assert audit[0]["action"] == "created:pending"
        assert "e-challan withheld" in audit[0]["note"]
        assert out.context["challan_withheld"] is True
        assert out.context["withheld_rules"] == ["R_SPD_SEGMENT"]
        assert out.context["lawful_basis"] == "stolen_vehicle_fir"
        assert out.context["earmark_state"] == "flagged"

    async def test_any_other_earmark_keeps_the_penal_rules(self):
        common.reset_relation_cache()
        conn = EngineConn(earmark={"state": "watch", "lawful_basis": "mva_1988_s183",
                                   "reason": "speeding complaints"})
        srow = _srow()
        out = await engine.evaluate_sighting(conn, srow["sighting_id"], srow=srow)
        assert [r.rule_id for r in out.fired] == ["R_SPD_SEGMENT"]
        assert out.context["challan_withheld"] is False
        assert out.context["withheld_rules"] == []

    async def test_the_stolen_earmark_outranks_a_newer_earmark(self):
        """The earmark query prefers a stolen-vehicle FIR earmark over any
        newer one — a later 'watch' never switches the challans back on."""
        common.reset_relation_cache()
        conn = EngineConn(earmark=STOLEN_EARMARK)
        srow = _srow()
        await engine.evaluate_sighting(conn, srow["sighting_id"], srow=srow)
        earmark_sql = next(q for q in conn.queries if "FROM corridor.vehicle_earmarks" in q)
        flat = " ".join(earmark_sql.split())
        assert "ORDER BY (lawful_basis = 'stolen_vehicle_fir') DESC, earmarked_at DESC" in flat
        assert "cleared_at IS NULL" in flat and "retention_until > NOW()" in flat

    async def test_the_fir_reference_comes_from_the_reason_only(self):
        common.reset_relation_cache()
        state = await engine._active_earmark_state(EngineConn(earmark=STOLEN_EARMARK), VEHICLE)
        assert (state.state, state.lawful_basis, state.fir_ref) == (
            "flagged", "stolen_vehicle_fir", "4401/2026"
        )
        assert state.stolen
        none = await engine._active_earmark_state(EngineConn(earmark=None), VEHICLE)
        assert none == engine.ActiveEarmark() and not none.stolen


# ─── the decision endpoint ───────────────────────────────────────────────────


class DecisionConn:
    def __init__(self, row: dict) -> None:
        self.row = row
        self.updates: list[tuple] = []
        self.sql: list[str] = []

    async def fetchrow(self, sql: str, *args: Any):
        self.sql.append(sql)
        if sql.startswith("SELECT r.recommendation_id, r.status, r.audit_tail"):
            return self.row
        if "UPDATE events.enforcement_recommendations" in sql:
            self.updates.append(args)
            return {"recommendation_id": self.row["recommendation_id"], "status": args[0],
                    "decided_by": args[1], "decided_at": args[2]}
        raise AssertionError("unexpected fetchrow: " + sql[:80])

    def transaction(self):
        return _Tx()


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Pool:
    def __init__(self, conn) -> None:
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Acq:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Acq()


def _decision_app(rule_id: str, stolen: bool) -> tuple[TestClient, DecisionConn, uuid.UUID]:
    rid = uuid.uuid4()
    conn = DecisionConn({
        "recommendation_id": rid, "status": "pending", "audit_tail": [],
        "rule_id": rule_id, "vehicle_id": VEHICLE, "stolen_vehicle": stolen,
        "sighting_t": datetime.now(timezone.utc) - timedelta(minutes=5),
    })
    from traffic_oracle.enforcement import router

    app = FastAPI()
    app.state.pool = _Pool(conn)
    app.include_router(router)
    return TestClient(app), conn, rid


class TestDecision:
    @pytest.mark.parametrize("rule_id", RULE_IDS)
    def test_approving_an_e_challan_of_a_stolen_vehicle_is_409(self, rule_id):
        client, conn, rid = _decision_app(rule_id, stolen=True)
        r = client.post(f"/api/v1/enforcement/recommendations/{rid}/decision",
                        json={"decision": "approve", "officer": "Duty Officer A"})
        assert r.status_code == 409, r.text
        body = r.json()
        assert body["detail"] == "e-challan withheld — vehicle reported stolen"
        assert body["reason"] == "stolen_vehicle_fir"
        assert body["rule_id"] == rule_id
        assert conn.updates == []  # nothing decided — the row stays pending

    def test_rejecting_it_stays_allowed(self):
        client, conn, rid = _decision_app("R_SPD_SEGMENT", stolen=True)
        r = client.post(f"/api/v1/enforcement/recommendations/{rid}/decision",
                        json={"decision": "reject", "officer": "Duty Officer A",
                              "note": "vehicle reported stolen"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "rejected"
        assert conn.updates[0][0] == "rejected"

    def test_the_sighting_itself_is_approved_normally(self):
        client, conn, rid = _decision_app("R_STOLEN_SIGHTING", stolen=True)
        r = client.post(f"/api/v1/enforcement/recommendations/{rid}/decision",
                        json={"decision": "approve", "officer": "Duty Officer A",
                              "note": "authorise pursuit"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "approved"

    def test_an_e_challan_of_any_other_vehicle_is_approved_as_before(self):
        client, conn, rid = _decision_app("R_SPD_SEGMENT", stolen=False)
        r = client.post(f"/api/v1/enforcement/recommendations/{rid}/decision",
                        json={"decision": "approve", "officer": "Duty Officer A"})
        assert r.status_code == 200, r.text

    def test_the_decision_read_composes_the_stolen_predicate(self):
        from traffic_oracle.enforcement import _SQL_DECISION_ROW

        assert engine.SQL_STOLEN_EARMARK in _SQL_DECISION_ROW
        assert _SQL_DECISION_ROW.rstrip().endswith("FOR UPDATE OF r")
        flat = engine.SQL_STOLEN_EARMARK
        for clause in ("ve.cleared_at IS NULL", "ve.retention_until > NOW()",
                       "ve.lawful_basis = 'stolen_vehicle_fir'"):
            assert clause in flat


# ─── the ingest seam (contract C-E, additive fields) ────────────────────────


def test_ingest_answers_challan_withheld_for_a_stolen_vehicle():
    from test_anpr_ingest import VID, FakePool, _body
    from test_anpr_ingest import _client as anpr_client

    from traffic_oracle.vehicles import sighting_processor as sp

    common.reset_relation_cache()
    body = _body(speed_kmh=112.0)
    sid = sp.sighting_uuid_for(body["read_id"], "emulated_anpr", body["camera_id"])
    t = datetime.fromisoformat(body["t"])
    pool = FakePool([
        ("SELECT sighting_id, vehicle_id, plate, camera_id, t, source", []),
        ("SELECT e.earmark_id, e.state, e.lawful_basis",
         [{"earmark_id": uuid.uuid4(), "state": "flagged", "lawful_basis": "stolen_vehicle_fir"}]),
        ("INSERT INTO corridor.vehicles (plate", {"vehicle_id": VID}),
        ("INSERT INTO corridor.vehicle_sightings", {"sighting_id": sid}),
        ("SELECT s.sighting_id, s.vehicle_id, s.plate", {
            "sighting_id": sid, "vehicle_id": VID, "plate": "GJ27EM9001",
            "camera_id": body["camera_id"], "t": t, "lon": body["lon"], "lat": body["lat"],
            "speed_kmh": body["speed_kmh"], "heading": body["heading"],
            "vehicle_class": "car", "source": "emulated_anpr",
            "matched_plate_hit": "stolen_vehicle",
        }),
        ("SELECT state, lawful_basis, reason FROM corridor.vehicle_earmarks", STOLEN_EARMARK),
        ("SELECT segment_id, road_name, speed_limit_kmh", {
            "segment_id": 1015, "road_name": "Nehru Bridge Road",
            "speed_limit_kmh": 60, "dist_m": 12.0,
        }),
        ("INSERT INTO events.enforcement_recommendations", {
            "recommendation_id": uuid.uuid4(), "created_at": T0,
        }),
    ])
    r = anpr_client(pool).post("/api/v1/vehicles/sightings", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["hit"] == "stolen_vehicle"
    assert [x["rule_id"] for x in out["recommendations"]] == ["R_STOLEN_SIGHTING"]
    assert out["rules_fired"] == ["R_STOLEN_SIGHTING"]
    assert out["challan_withheld"] is True and out["withheld_rules"] == ["R_SPD_SEGMENT"]


# ─── pursuits authorised by a sighting ───────────────────────────────────────


class PursuitConn:
    def __init__(self, rec: dict | None, earmarked: bool = True, prior: str | None = None) -> None:
        self.rec = rec
        self.earmarked = earmarked
        self.prior = prior

    async def fetchval(self, sql: str, *args: Any):
        from traffic_oracle import events as events_mod

        if sql == events_mod._SQL_PURSUIT_OF_RECOMMENDATION:
            return self.prior
        assert sql == events_mod._SQL_ACTIVE_EARMARKS
        return 1 if self.earmarked else 0

    async def fetchrow(self, sql: str, *args: Any):
        return self.rec


class TestPursuit:
    async def test_an_approved_stolen_sighting_opens_a_pursuit(self):
        from traffic_oracle.events import _validate_pursuit

        rid = uuid.uuid4()
        conn = PursuitConn({"recommendation_id": rid, "vehicle_id": VEHICLE,
                            "status": "approved", "decided_by": "Duty Officer A"})
        out = await _validate_pursuit(
            conn,
            {"vehicle_id": str(VEHICLE), "recommendation_id": str(rid), "fir": "FIR 4401/2026"},
        )
        assert out["recommendation_id"] == str(rid) and out["fir"] == "FIR 4401/2026"

    async def test_no_pursuit_refusal_calls_the_authorisation_a_fine(self):
        from fastapi import HTTPException

        from traffic_oracle.events import _validate_pursuit

        rid = uuid.uuid4()
        cases = [
            (PursuitConn(None), {}),
            (PursuitConn(None), {"vehicle_id": str(VEHICLE)}),
            (PursuitConn(None, earmarked=False),
             {"vehicle_id": str(VEHICLE), "recommendation_id": str(rid)}),
            (PursuitConn(None), {"vehicle_id": str(VEHICLE), "recommendation_id": str(rid)}),
            (PursuitConn({"recommendation_id": rid, "vehicle_id": VEHICLE,
                          "status": "pending", "decided_by": None}),
             {"vehicle_id": str(VEHICLE), "recommendation_id": str(rid)}),
        ]
        texts = []
        for conn, meta in cases:
            with pytest.raises(HTTPException) as exc:
                await _validate_pursuit(conn, meta)
            assert exc.value.status_code == 422
            texts.append(str(exc.value.detail))
        for text in texts:
            low = text.lower()
            assert "fine" not in low and "e-challan" not in low and "enforcement" not in low, text
        assert "R_STOLEN_SIGHTING" in texts[1]

    async def test_an_approved_challan_never_authorises_pursuing_a_stolen_car(self):
        from fastapi import HTTPException

        from traffic_oracle.events import _validate_pursuit

        rid = uuid.uuid4()
        meta = {"vehicle_id": str(VEHICLE), "recommendation_id": str(rid)}
        # an e-challan approved before the theft report (the legacy fixture):
        # the officer must authorise the R_STOLEN_SIGHTING instead (C-N)
        for rule in ("R_SPD_SEGMENT", "R_SIGNAL_VIOLATION", "R_CLOSED_CORRIDOR_ENTRY"):
            conn = PursuitConn({"recommendation_id": rid, "vehicle_id": VEHICLE, "status": "approved",
                                "decided_by": "Duty Officer A", "rule_id": rule, "stolen_vehicle": True})
            with pytest.raises(HTTPException) as exc:
                await _validate_pursuit(conn, meta)
            assert exc.value.status_code == 422
            text = str(exc.value.detail)
            assert "R_STOLEN_SIGHTING" in text and rule in text
            low = text.lower()
            assert "fine" not in low and "e-challan" not in low and "enforcement" not in low, text
        # the sighting itself authorises it
        conn = PursuitConn({"recommendation_id": rid, "vehicle_id": VEHICLE, "status": "approved",
                            "decided_by": "Duty Officer A", "rule_id": "R_STOLEN_SIGHTING",
                            "stolen_vehicle": True})
        assert (await _validate_pursuit(conn, meta))["recommendation_id"] == str(rid)
        # a vehicle earmarked on another lawful basis keeps the C6 chain as before
        conn = PursuitConn({"recommendation_id": rid, "vehicle_id": VEHICLE, "status": "approved",
                            "decided_by": "Duty Officer A", "rule_id": "R_SPD_SEGMENT",
                            "stolen_vehicle": False})
        assert (await _validate_pursuit(conn, meta))["recommendation_id"] == str(rid)

    async def test_one_approval_authorises_one_pursuit(self):
        from fastapi import HTTPException

        from traffic_oracle.events import _validate_pursuit

        rid = uuid.uuid4()
        meta = {"vehicle_id": str(VEHICLE), "recommendation_id": str(rid)}
        rec = {"recommendation_id": rid, "vehicle_id": VEHICLE, "status": "approved",
               "decided_by": "Duty Officer A", "rule_id": "R_STOLEN_SIGHTING", "stolen_vehicle": True}
        # the scripted showcase's approval, spent on its own (closed) pursuit,
        # never pre-empts the next human authorisation
        with pytest.raises(HTTPException) as exc:
            await _validate_pursuit(PursuitConn(rec, prior="0b5e0000-0000-4000-8000-000000000001"), meta)
        assert exc.value.status_code == 422
        text = str(exc.value.detail)
        assert "already authorised pursuit 0b5e0000-0000-4000-8000-000000000001" in text
        low = text.lower()
        assert "fine" not in low and "e-challan" not in low and "enforcement" not in low, text
        assert (await _validate_pursuit(PursuitConn(rec), meta))["recommendation_id"] == str(rid)

    def test_the_pursuit_query_reads_the_rule_and_the_live_theft_report(self):
        from traffic_oracle import events as events_mod
        from traffic_oracle.enforcement.engine import SQL_STOLEN_EARMARK

        sql = events_mod._SQL_PURSUIT_RECOMMENDATION
        assert "r.rule_id" in sql and SQL_STOLEN_EARMARK + " AS stolen_vehicle" in sql
        assert sql.rstrip().endswith("FOR SHARE")


# ─── city surfaces ───────────────────────────────────────────────────────────


def _rec_row(rule_id: str, status: str, stolen: bool, evidence: dict | None = None) -> dict:
    return {
        "recommendation_id": uuid.uuid4(), "sighting_id": uuid.uuid4(),
        "vehicle_id": VEHICLE, "rule_id": rule_id,
        "recommendation": "stolen vehicle (FIR 4401/2026) sighted at EM-CAM-02 — e-challan "
                          "withheld, vehicle reported stolen",
        "evidence": evidence or {}, "status": status,
        "decided_by": None if status == "pending" else "Duty Officer A",
        "decided_at": None if status == "pending" else T0 + timedelta(minutes=2),
        "created_at": T0, "camera_id": "EM-CAM-02", "sighting_t": T0,
        "sighting_source": "emulated_anpr", "plate": "GJ27EM9001",
        "lon": 72.57, "lat": 23.02, "stolen_vehicle": stolen,
    }


class TestCitySurfaces:
    def test_the_sighting_entity_says_e_challan_withheld(self):
        (rec,) = evaluate_rules(_sighting(), SEGMENT, None, stolen=STOLEN)
        ent = recommendation_entity(
            _rec_row("R_STOLEN_SIGHTING", "pending", True, rec.evidence), T0
        )
        assert ent["label"] == "R_STOLEN_SIGHTING · PENDING · GJ27EM•••1 · e-challan withheld"
        props = ent["props"]
        assert props["challan_withheld"] is True and props["rule_kind"] == "evidence"
        assert props["withheld_text"] == CHALLAN_WITHHELD_TEXT
        assert props["withheld_rules"] == ["R_SPD_SEGMENT"]
        assert props["approvable"] is True and props["evidence"]["fir_ref"] == "4401/2026"

    def test_a_pending_penal_row_of_a_stolen_vehicle_is_withheld_and_not_approvable(self):
        ent = recommendation_entity(_rec_row("R_SPD_SEGMENT", "pending", True), T0)
        assert ent["label"].endswith("· e-challan withheld")
        assert ent["props"]["challan_withheld"] is True
        assert ent["props"]["approvable"] is False

    def test_a_penal_row_decided_before_the_fir_keeps_its_decision(self):
        ent = recommendation_entity(_rec_row("R_SPD_SEGMENT", "approved", True), T0)
        assert ent["label"].endswith("· vehicle reported stolen")
        assert ent["props"]["challan_withheld"] is False
        assert ent["props"]["vehicle_reported_stolen"] is True

    def test_any_other_vehicle_is_unchanged(self):
        ent = recommendation_entity(_rec_row("R_SPD_SEGMENT", "pending", False), T0)
        assert ent["label"] == "R_SPD_SEGMENT · PENDING · GJ27EM•••1"
        assert ent["props"]["challan_withheld"] is False and ent["props"]["approvable"] is True

    def test_the_feed_narrates_a_sighting_never_a_challan(self):
        (rec,) = evaluate_rules(_sighting(), SEGMENT, None, stolen=STOLEN)
        row = _rec_row("R_STOLEN_SIGHTING", "approved", True, rec.evidence)
        row["audit_tail"] = [
            {"actor": "traffic-oracle", "action": "created:pending",
             "at": T0.isoformat(), "note": "deterministic rule evaluation"},
            {"actor": "Duty Officer A", "action": "decision:approved",
             "at": (T0 + timedelta(minutes=2)).isoformat(), "note": ""},
        ]
        created, decided = enforcement_items(row, common.EPOCH)
        assert created["text"].startswith(
            "Stolen vehicle GJ27EM•••1 (FIR 4401/2026) sighted at EM-CAM-02"
        )
        assert "e-challan withheld" in created["text"]
        assert "recommended" not in created["text"]
        assert decided["text"] == (
            "Stolen-vehicle sighting of GJ27EM•••1 confirmed by Duty Officer A — "
            "pursuit authorised; e-challan withheld"
        )
        assert created["props"]["challan_withheld"] is True
        assert decided["actor_kind"] == "human"

    def test_a_pending_penal_row_of_a_stolen_vehicle_reads_withheld_in_the_feed(self):
        row = _rec_row("R_SPD_SEGMENT", "pending", True,
                       {"speed_kmh": 92, "speed_limit_kmh": 40, "road_name": "Relief Road"})
        row["audit_tail"] = [{"actor": "traffic-oracle", "action": "created:pending",
                              "at": T0.isoformat(), "note": ""}]
        (item,) = enforcement_items(row, common.EPOCH)
        assert item["text"].endswith("; now e-challan withheld — vehicle reported stolen")
        assert item["props"]["challan_withheld"] is True


# ─── DB tier (sandbox store with 017; everything rolled back) ───────────────

_DSN = os.environ.get("VEER_TEST_PG_DSN", "")
_LIVE_PORT = ":56434"
db = pytest.mark.skipif(
    not _DSN or _LIVE_PORT in _DSN,
    reason="set VEER_TEST_PG_DSN to a SANDBOX store (001-017 + seed); never the live :56434 store",
)


@db
async def test_db_a_stolen_vehicle_read_lands_one_r_stolen_sighting():
    import asyncpg

    conn = await asyncpg.connect(_DSN)
    tx = conn.transaction()
    await tx.start()
    try:
        common.reset_relation_cache()
        check = await conn.fetchval(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'enforcement_recommendations_rule_id_check'"
        )
        if "R_STOLEN_SIGHTING" not in (check or ""):
            pytest.skip("migration 017 not applied on this store")
        plate = "GJ99ZZ" + str(uuid.uuid4().int % 9000 + 1000)
        vid = await conn.fetchval(
            "INSERT INTO corridor.vehicles (plate, vehicle_class, first_seen, last_seen) "
            "VALUES ($1, 'car', NOW(), NOW()) RETURNING vehicle_id", plate
        )
        await conn.execute(
            "INSERT INTO corridor.vehicle_earmarks (vehicle_id, state, reason, lawful_basis, "
            "retention_days, earmarked_by, earmarked_at, retention_until) VALUES "
            "($1, 'flagged', 'test FIR 77/2026', 'stolen_vehicle_fir', 1, 'WP-19 test', "
            "NOW(), NOW() + interval '1 day')", vid
        )
        sid = uuid.uuid4()
        await conn.execute(
            "INSERT INTO corridor.vehicle_sightings (sighting_id, vehicle_id, plate, camera_id, "
            "t, position, speed_kmh, heading, vehicle_class, source, matched_plate_hit) VALUES "
            "($1, $2, $3, 'EM-CAM-02', NOW() - interval '1 minute', "
            "ST_SetSRID(ST_MakePoint(72.573251, 23.02397), 4326), 112, 78, 'car', "
            "'emulated_anpr', 'stolen_vehicle')", sid, vid, plate
        )
        out = await engine.evaluate_sighting(conn, sid)
        assert [r.rule_id for r in out.fired] == ["R_STOLEN_SIGHTING"]
        assert out.context["challan_withheld"] is True
        rows = await conn.fetch(
            "SELECT rule_id, status, recommendation, evidence FROM "
            "events.enforcement_recommendations WHERE sighting_id = $1", sid
        )
        assert [(r["rule_id"], r["status"]) for r in rows] == [("R_STOLEN_SIGHTING", "pending")]
        assert "FIR 77/2026" in rows[0]["recommendation"]
        assert json.loads(rows[0]["evidence"])["challan_withheld"] is True
    finally:
        await tx.rollback()
        await conn.close()
