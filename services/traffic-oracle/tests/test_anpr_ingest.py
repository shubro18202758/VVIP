"""ANPR ingest seam — POST /api/v1/vehicles/sightings (contract C-E).

Two layers:

* Pure / fake-pool tests (always run): plate normalization, body
  validation, the idempotency key, hit derivation from ACTIVE earmarks
  (never from a label in the body), time integrity (future -> 409, past
  retention -> 422, nothing written), duplicate handling, the PENDING
  recommendations the e-challan engine creates, an unknown vehicle class
  that never drops a read, and the live-source gate — through the real
  router.  Plus the reaper's visible health (GET /vehicles/retention).

* DB-backed tests (run when VEER_TEST_PG_DSN points at a SANDBOX store
  with migrations 001-012 + seed + post_seed_derivations): the whole
  pipeline against PostGIS/pgRouting inside a transaction that is always
  rolled back — idempotency, the computed hit, the created recommendation,
  the retention reap sparing a decided recommendation's evidence, and an
  earmark that only ever extends retention.
  They refuse to run against the live store's port.
"""

from __future__ import annotations

import json

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from traffic_oracle.city.common import SQL_LAWFUL_READ
from traffic_oracle.enforcement import engine
from traffic_oracle.signals.plans import phase_at
from traffic_oracle.vehicles import sighting_processor as sp
from traffic_oracle.vehicles import store as vstore
from traffic_oracle.vehicles.plates import InvalidPlate, normalize_plate


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _body(**over) -> dict:
    body = {
        "read_id": str(uuid.uuid4()),
        "plate": "GJ27EM9001",
        "camera_id": "EM-CAM-02",
        "t": (_now() - timedelta(seconds=5)).isoformat(),
        "lon": 72.57302,
        "lat": 23.02453,
        "speed_kmh": 96.0,
        "heading": 78.0,
        "vehicle_class": "car",
        "source": "emulated_anpr",
    }
    body.update(over)
    return body


# ── plates ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, canonical",
    [
        ("GJ27EM9001", "GJ27EM9001"),
        ("GJ 27 EM 9001", "GJ27EM9001"),
        ("gj-27-em-9001", "GJ27EM9001"),
        (" gj.27.em.9001 ", "GJ27EM9001"),
        ("DL3CAB1234", "DL3CAB1234"),
        ("22 BH 1234 AA", "22BH1234AA"),
    ],
)
def test_plate_normalization(raw, canonical):
    assert normalize_plate(raw) == canonical


@pytest.mark.parametrize("raw", ["", "   ", "HELLO WORLD", "GJ27EM90011", "1234",
                                 "GJ27EM9001; DROP", "X" * 40])
def test_plate_rejects_non_plates(raw):
    with pytest.raises(InvalidPlate):
        normalize_plate(raw)


# ── body validation (contract C-E) ─────────────────────────────────────────


def test_body_normalizes_and_validates():
    read = sp.AnprReadIn(**_body(plate="gj 27 em-9001", heading=438.0,
                                 vehicle_class="Car"))
    assert read.plate == "GJ27EM9001"
    assert read.heading == 78.0
    assert read.vehicle_class == "car"
    naive = sp.AnprReadIn(**_body(t="2026-10-22T12:00:00"))
    assert naive.t.tzinfo is not None  # contract: UTC
    # an unknown class is normalized here and nulled by the ingest (with a
    # warning) — it never invalidates the read
    assert sp.AnprReadIn(**_body(vehicle_class=" SUV ")).vehicle_class == "suv"
    for bad in (
        {"plate": "NOT A PLATE"},
        {"camera_id": "cam 1; drop"},
        {"source": "live_feed"},
        {"lat": 123.0},
        {"vehicle_class": "x" * 40},
        {"speed_kmh": -3.0},
    ):
        with pytest.raises(ValidationError):
            sp.AnprReadIn(**_body(**bad))


def test_idempotency_key():
    rid = str(uuid.uuid4())
    assert sp.sighting_uuid_for(rid, "emulated_anpr", "EM-CAM-02") == uuid.UUID(rid)
    a = sp.sighting_uuid_for("VENDOR-000123", "live_anpr", "CAM-7")
    b = sp.sighting_uuid_for("VENDOR-000123", "live_anpr", "CAM-7")
    c = sp.sighting_uuid_for("VENDOR-000123", "live_anpr", "CAM-8")
    assert a == b and a != c and a.version == 5


def test_hit_comes_from_active_earmarks_only():
    assert sp.hit_for_earmarks([]) is None
    assert sp.hit_for_earmarks([{"lawful_basis": "mva_1988_s207"}]) == "watchlist"
    assert sp.hit_for_earmarks([
        {"lawful_basis": "mva_1988_s207"},
        {"lawful_basis": "stolen_vehicle_fir"},
    ]) == "stolen_vehicle"


# ── fake pool ───────────────────────────────────────────────────────────────


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, pool: "FakePool") -> None:
        self.pool = pool

    async def fetchrow(self, sql, *args):
        self.pool.calls.append(("fetchrow", sql, args))
        rows = self.pool.rows_for(sql)
        return rows[0] if rows else None

    async def fetch(self, sql, *args):
        self.pool.calls.append(("fetch", sql, args))
        return self.pool.rows_for(sql)

    def transaction(self):
        return _Tx()


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class FakePool:
    """Routes SQL by substring to canned rows (first match wins)."""

    def __init__(self, matches=()):
        self.matches = list(matches)
        self.calls: list = []

    def rows_for(self, sql):
        for needle, rows in self.matches:
            if needle in sql:
                return rows if isinstance(rows, list) else [rows]
        return []

    def acquire(self):
        return _Acquire(FakeConn(self))

    def sql_calls(self, needle):
        return [(sql, args) for _k, sql, args in self.calls if needle in sql]


def _client(pool: FakePool) -> TestClient:
    app = FastAPI()
    app.state.pool = pool
    from traffic_oracle.vehicles import router

    app.include_router(router)
    return TestClient(app)


VID = uuid.uuid4()


def _pipeline_pool(body: dict, *, earmarks, existing=None) -> FakePool:
    """A pool scripted for one fresh read: the sighting does not exist yet,
    the plate has ``earmarks``, the read lands on segment 1015 (60 km/h),
    no signalized junction within 40 m, no active closure."""
    sid = sp.sighting_uuid_for(body["read_id"], "emulated_anpr", body["camera_id"])
    t = datetime.fromisoformat(body["t"])
    return FakePool([
        ("SELECT sighting_id, vehicle_id, plate, camera_id, t, source", existing or []),
        ("SELECT e.earmark_id, e.state, e.lawful_basis", earmarks),
        ("INSERT INTO corridor.vehicles (plate", {"vehicle_id": VID}),
        ("INSERT INTO corridor.vehicle_sightings", {"sighting_id": sid}),
        ("SELECT s.sighting_id, s.vehicle_id, s.plate", {
            "sighting_id": sid, "vehicle_id": VID, "plate": "GJ27EM9001",
            "camera_id": body["camera_id"], "t": t, "lon": body["lon"],
            "lat": body["lat"], "speed_kmh": body["speed_kmh"],
            "heading": body["heading"], "vehicle_class": "car",
            "source": "emulated_anpr", "matched_plate_hit": None,
        }),
        ("SELECT state FROM corridor.vehicle_earmarks", []),
        ("SELECT segment_id, road_name, speed_limit_kmh", {
            "segment_id": 1015, "road_name": "Nehru Bridge Road",
            "speed_limit_kmh": 60, "dist_m": 12.0,
        }),
        ("INSERT INTO events.enforcement_recommendations", {
            "recommendation_id": uuid.uuid4(), "created_at": _now(),
        }),
    ])


def test_ingest_computes_hit_and_creates_pending_recommendation():
    body = _body(speed_kmh=96.0, matched_plate_hit="earmarked_plate")
    pool = _pipeline_pool(body, earmarks=[
        {"earmark_id": uuid.uuid4(), "state": "flagged",
         "lawful_basis": "stolen_vehicle_fir"},
    ])
    r = _client(pool).post("/api/v1/vehicles/sightings", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["hit"] == "stolen_vehicle"           # from the earmark ...
    assert out["duplicate"] is False
    assert out["sighting_id"] == body["read_id"]
    assert out["vehicle_id"] == str(VID)
    assert [x["rule_id"] for x in out["recommendations"]] == ["R_SPD_SEGMENT"]
    assert all(x["status"] == "pending" for x in out["recommendations"])
    # ... persisted with the schema's vocabulary, never the body's label
    (sql, args), = pool.sql_calls("INSERT INTO corridor.vehicle_sightings")
    assert args[-1] == "stolen_vehicle"
    assert args[2] == "GJ27EM9001" and args[9] == "emulated_anpr"


def test_ingest_without_earmark_has_no_hit():
    body = _body(speed_kmh=40.0, matched_plate_hit="stolen_vehicle")
    pool = _pipeline_pool(body, earmarks=[])
    out = _client(pool).post("/api/v1/vehicles/sightings", json=body).json()
    assert out["hit"] is None and out["recommendations"] == []
    (sql, args), = pool.sql_calls("INSERT INTO corridor.vehicle_sightings")
    assert args[-1] is None  # the tape label in the body is ignored


# ── C-E stays byte-compatible under the read-side C6 rule (WP-16) ────────────

#: Contract C-E's response keys, plus the additive provenance WP-03 shipped
#: with them: the exact body WP-13's emulator posts against.
C_E_KEYS = {"sighting_id", "vehicle_id", "duplicate", "hit", "recommendations"}
C_E_ADDITIVE_KEYS = {
    "plate", "camera_id", "t", "source", "vehicle_class", "rules_fired", "warnings",
    # WP-19 (C-N): a stolen vehicle's read proposes no e-challan
    "challan_withheld", "withheld_rules",
}


def test_c_e_response_is_unchanged_and_never_consults_the_disclosure_rule():
    """WP-16 made the raw READ paths lawful-only; the ingest seam must not
    notice: same status, same keys, same recommendation shape, and the C6
    disclosure predicate is never evaluated on the way IN."""
    body = _body(speed_kmh=96.0)  # fires R_SPD_SEGMENT on 1015 (60 km/h)
    pool = _pipeline_pool(body, earmarks=[])
    r = _client(pool).post("/api/v1/vehicles/sightings", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert set(out) == C_E_KEYS | C_E_ADDITIVE_KEYS
    assert (out["hit"], out["duplicate"]) == (None, False)
    assert (out["challan_withheld"], out["withheld_rules"]) == (False, [])
    assert [set(x) for x in out["recommendations"]] == [
        {"recommendation_id", "rule_id", "status"}
    ]
    assert pool.sql_calls(SQL_LAWFUL_READ) == []
    assert pool.sql_calls(vstore.SQL_DISCLOSURE_BASIS) == []


def test_a_read_accepted_for_an_unearmarked_plate_is_withheld_on_the_way_out():
    """C-E accepts every well-formed read (the platform must see the
    camera); disclosing one individually is a different question — with no
    active earmark and no recommendation citing it, /sightings and /trace
    answer the count-free 404."""
    body = _body(speed_kmh=40.0)  # under the limit: no rule fires, no evidence
    pool = _pipeline_pool(body, earmarks=[])
    client = _client(pool)
    out = client.post("/api/v1/vehicles/sightings", json=body).json()
    assert out["recommendations"] == [] and out["hit"] is None
    for path in ("sightings", "trace?window_sec=86400"):
        withheld = client.get(f"/api/v1/vehicles/{out['vehicle_id']}/{path}")
        assert withheld.status_code == 404
        assert withheld.json() == {
            "detail": "no lawful basis to disclose individual reads for this vehicle"
        }
    assert pool.sql_calls(vstore.SQL_DISCLOSURE_BASIS)  # decided by the rule itself


def test_ingest_duplicate_returns_the_same_sighting():
    body = _body()
    sid = uuid.UUID(body["read_id"])
    rec_id = uuid.uuid4()
    pool = FakePool([
        ("SELECT sighting_id, vehicle_id, plate, camera_id, t, source", {
            "sighting_id": sid, "vehicle_id": VID, "plate": "GJ27EM9001",
            "camera_id": "EM-CAM-02", "t": datetime.fromisoformat(body["t"]),
            "source": "emulated_anpr", "vehicle_class": "car",
            "matched_plate_hit": "stolen_vehicle",
        }),
        ("SELECT recommendation_id, rule_id, status", [
            {"recommendation_id": rec_id, "rule_id": "R_SPD_SEGMENT", "status": "pending"},
        ]),
    ])
    r = _client(pool).post("/api/v1/vehicles/sightings", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["duplicate"] is True and out["sighting_id"] == str(sid)
    assert out["hit"] == "stolen_vehicle"
    assert out["recommendations"] == [
        {"recommendation_id": str(rec_id), "rule_id": "R_SPD_SEGMENT", "status": "pending"}
    ]
    assert pool.sql_calls("INSERT INTO") == []  # nothing re-written / re-run


def test_ingest_read_id_reuse_for_another_plate_is_422():
    body = _body()
    pool = FakePool([
        ("SELECT sighting_id, vehicle_id, plate, camera_id, t, source", {
            "sighting_id": uuid.UUID(body["read_id"]), "vehicle_id": VID,
            "plate": "GJ01EM2174", "camera_id": "EM-CAM-02", "t": _now(),
            "source": "emulated_anpr", "matched_plate_hit": None,
        }),
    ])
    assert _client(pool).post("/api/v1/vehicles/sightings", json=body).status_code == 422


def test_ingest_refuses_future_reads_with_409_and_writes_nothing():
    pool = FakePool()
    body = _body(t=(_now() + timedelta(minutes=10)).isoformat())
    r = _client(pool).post("/api/v1/vehicles/sightings", json=body)
    assert r.status_code == 409, r.text
    assert pool.sql_calls("INSERT INTO") == []
    # inside the 30 s clock-skew allowance is fine
    ok = _body(t=(_now() + timedelta(seconds=10)).isoformat())
    ok_pool = _pipeline_pool(ok, earmarks=[])
    assert _client(ok_pool).post("/api/v1/vehicles/sightings", json=ok).status_code == 200


def test_ingest_refuses_reads_past_retention_with_422():
    pool = FakePool()
    body = _body(t=(_now() - timedelta(days=31)).isoformat())
    r = _client(pool).post("/api/v1/vehicles/sightings", json=body)
    assert r.status_code == 422
    assert pool.sql_calls("INSERT INTO") == []


def test_ingest_unknown_vehicle_class_keeps_the_read_and_its_hit():
    """A vendor class outside the schema vocabulary must not drop a
    stolen-vehicle read: the class is stored as NULL and named."""
    body = _body(vehicle_class="SUV", speed_kmh=40.0)
    pool = _pipeline_pool(body, earmarks=[
        {"earmark_id": uuid.uuid4(), "state": "flagged",
         "lawful_basis": "stolen_vehicle_fir"},
    ])
    r = _client(pool).post("/api/v1/vehicles/sightings", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["hit"] == "stolen_vehicle"
    assert out["vehicle_class"] is None
    assert out["warnings"] and "'suv'" in out["warnings"][0]
    (sql, args), = pool.sql_calls("INSERT INTO corridor.vehicle_sightings")
    assert args[8] is None  # vehicle_class column
    # a known class is stored as sent, with no warning
    ok = _body(vehicle_class="Two_Wheeler", speed_kmh=40.0)
    ok_out = _client(_pipeline_pool(ok, earmarks=[])).post(
        "/api/v1/vehicles/sightings", json=ok
    ).json()
    assert ok_out["vehicle_class"] == "two_wheeler" and ok_out["warnings"] == []


def test_live_source_label_is_refused_unless_the_deployment_enables_it(monkeypatch):
    """The source label is the caller's claim: without a registered live
    feed (VVIP_ACCEPT_LIVE_ANPR=1) a read may not call itself live."""
    monkeypatch.delenv(sp.LIVE_ANPR_ENV, raising=False)
    for label in ("anpr", "live_anpr"):
        pool = FakePool()
        body = _body(source=label)
        r = _client(pool).post("/api/v1/vehicles/sightings", json=body)
        assert r.status_code == 422, r.text
        assert "emulated_anpr" in r.json()["detail"]
        # refused before the ingest touches a table (only the throttled
        # reap may have run on this request)
        assert pool.sql_calls("corridor.vehicle_sightings WHERE sighting_id") == []
        assert pool.sql_calls("INSERT INTO") == []
    monkeypatch.setenv(sp.LIVE_ANPR_ENV, "1")
    body = _body(source="anpr", speed_kmh=40.0)
    pool = _pipeline_pool(body, earmarks=[])
    sid = sp.sighting_uuid_for(body["read_id"], "live_anpr", body["camera_id"])
    pool.matches.insert(0, ("INSERT INTO corridor.vehicle_sightings", {"sighting_id": sid}))
    r = _client(pool).post("/api/v1/vehicles/sightings", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["source"] == "live_anpr"
    (sql, args), = pool.sql_calls("INSERT INTO corridor.vehicle_sightings")
    assert args[9] == "live_anpr"


def test_retention_status_reports_a_failing_reaper():
    vstore.REAPER.reset()
    try:
        client = _client(FakePool())
        body = client.get("/api/v1/vehicles/retention").json()
        assert body["default_retention_days"] == 30
        assert body["earmark_retention_cap_days"] == 90
        assert body["reaper"]["healthy"] is True
        assert body["reaper"]["last_attempt_at"] is None

        class Boom:
            def transaction(self):
                raise RuntimeError("db down")

        assert _run(vstore.REAPER.maybe_reap(Boom(), _now())) is None
        body = client.get("/api/v1/vehicles/retention").json()
        assert body["reaper"]["healthy"] is False
        assert body["reaper"]["consecutive_failures"] == 1
        assert body["reaper"]["last_result"]["failed"] is True
        assert "db down" in body["reaper"]["last_result"]["reason"]
        assert body["reaper"]["last_attempt_at"] is not None
    finally:
        vstore.REAPER.reset()


def test_ingest_invalid_plate_is_422():
    r = _client(FakePool()).post(
        "/api/v1/vehicles/sightings", json=_body(plate="NOT A PLATE")
    )
    assert r.status_code == 422


def test_evaluate_refuses_future_sightings_with_409():
    from traffic_oracle.enforcement import router as enforcement_router

    sid = uuid.uuid4()
    pool = FakePool([
        ("SELECT s.sighting_id, s.vehicle_id, s.plate", {
            "sighting_id": sid, "vehicle_id": VID, "plate": "GJ27EM9001",
            "camera_id": "EM-CAM-02", "t": _now() + timedelta(minutes=10),
            "lon": 72.573, "lat": 23.024, "speed_kmh": 96.0, "heading": 78.0,
            "vehicle_class": "car", "source": "emulated_anpr",
            "matched_plate_hit": None,
        }),
    ])
    app = FastAPI()
    app.state.pool = pool
    app.include_router(enforcement_router)
    r = TestClient(app).post("/api/v1/enforcement/evaluate", json={"sighting_id": str(sid)})
    assert r.status_code == 409, r.text
    assert "future" in r.json()["detail"]
    assert pool.sql_calls("INSERT INTO events.enforcement_recommendations") == []


# ── DB-backed (sandbox store only, always rolled back) ─────────────────────

_DSN = os.environ.get("VEER_TEST_PG_DSN", "")
_LIVE_PORT = ":56434/"  # the shared live store — never written by tests

db = pytest.mark.skipif(
    not _DSN or _LIVE_PORT in _DSN,
    reason="set VEER_TEST_PG_DSN to a SANDBOX store (001-012 + seed + "
           "post_seed_derivations); never the live :56434 store",
)


def _run(coro):
    return asyncio.run(coro)


async def _in_rolled_back_tx(fn):
    import asyncpg

    conn = await asyncpg.connect(_DSN)
    tr = conn.transaction()
    await tr.start()
    try:
        return await fn(conn)
    finally:
        await tr.rollback()
        await conn.close()


async def _red_time_for(conn, junction_id: int, movement: str) -> datetime:
    """The latest past second (>= 5 s ago) at which ``movement`` is RED at
    the junction, by the same shared clock the rules read."""
    prog = await conn.fetchrow(
        "SELECT cycle_sec, phases, offset_sec FROM corridor.signal_programs "
        "WHERE junction_id = $1",
        junction_id,
    )
    program = {"cycle_sec": prog["cycle_sec"], "offset_sec": prog["offset_sec"],
               "phases": prog["phases"]}
    t = int(_now().timestamp()) - 5
    for _ in range(int(prog["cycle_sec"]) + 5):
        if phase_at(program, t).state_of(movement) == "red":
            return datetime.fromtimestamp(t, tz=timezone.utc)
        t -= 1
    raise AssertionError("program never shows red")


@db
def test_db_ingest_pipeline_end_to_end():
    async def scenario(conn):
        now = _now()
        plate = "GJ99ZZ0001"  # a test plate, inside the rolled-back tx
        veh = await vstore.upsert_vehicle_by_plate(conn, plate, "car", now)
        await vstore.insert_earmark(
            conn, veh["vehicle_id"], "flagged", "test FIR", "stolen_vehicle_fir",
            30, "pytest", now, now + timedelta(days=30),
        )
        cam = await conn.fetchrow(
            "SELECT ST_X(geom) AS lon, ST_Y(geom) AS lat, heading_deg, junction_id "
            "FROM corridor.cameras WHERE camera_id = 'EM-J03'"
        )
        assert cam is not None, "post_seed_derivations not applied"
        t = await _red_time_for(conn, int(cam["junction_id"]), "through")
        read = sp.AnprReadIn(**_body(
            plate="GJ 99 ZZ 0001", camera_id="EM-J03", t=t.isoformat(),
            lon=float(cam["lon"]), lat=float(cam["lat"]),
            heading=float(cam["heading_deg"]), speed_kmh=95.0,
        ))
        first = await sp.ingest_read(conn, read, now=_now())
        again = await sp.ingest_read(conn, read, now=_now())
        rows = await conn.fetch(
            "SELECT rule_id, status, evidence FROM events.enforcement_recommendations "
            "WHERE sighting_id = $1 ORDER BY rule_id",
            uuid.UUID(first.sighting_id),
        )
        stored = await conn.fetchrow(
            "SELECT plate, matched_plate_hit FROM corridor.vehicle_sightings "
            "WHERE sighting_id = $1",
            uuid.UUID(first.sighting_id),
        )
        return first, again, rows, stored, veh

    first, again, rows, stored, veh = _run(_in_rolled_back_tx(scenario))
    assert first.hit == "stolen_vehicle" and first.duplicate is False
    assert first.vehicle_id == str(veh["vehicle_id"])
    assert stored["plate"] == "GJ99ZZ0001"
    assert stored["matched_plate_hit"] == "stolen_vehicle"
    rules_fired = {r["rule_id"] for r in rows}
    # 95 km/h on 60 km/h Nehru Bridge Road + crossing Lal Darwaja on red —
    # but the vehicle is reported STOLEN (C-N, WP-19): no e-challan for the
    # victim's car, ONE R_STOLEN_SIGHTING carrying both rules as withheld
    assert rules_fired == {"R_STOLEN_SIGHTING"}
    evidence = rows[0]["evidence"]
    evidence = json.loads(evidence) if isinstance(evidence, str) else evidence
    assert {w["rule_id"] for w in evidence["withheld_rules"]} == {
        "R_SPD_SEGMENT", "R_SIGNAL_VIOLATION"
    }
    assert first.challan_withheld is True
    assert set(first.withheld_rules) == {"R_SPD_SEGMENT", "R_SIGNAL_VIOLATION"}
    assert all(r["status"] == "pending" for r in rows)
    assert again.duplicate is True and again.sighting_id == first.sighting_id
    assert sorted(x["recommendation_id"] for x in again.recommendations) == sorted(
        x["recommendation_id"] for x in first.recommendations
    )


@db
def test_db_reap_never_deletes_a_decided_recommendations_evidence():
    async def scenario(conn):
        now = _now()
        old = now - timedelta(days=45)
        veh = await vstore.upsert_vehicle_by_plate(conn, "GJ99ZZ0002", "car", old)
        vid = veh["vehicle_id"]
        ids = {}
        for label in ("approved", "rejected", "pending", "none"):
            sid = uuid.uuid4()
            ids[label] = sid
            await conn.fetchrow(
                "INSERT INTO corridor.vehicle_sightings (sighting_id, vehicle_id, "
                "plate, camera_id, t, position, source) VALUES ($1, $2, "
                "'GJ99ZZ0002', 'EM-J03', $3, ST_SetSRID(ST_MakePoint(72.58, 23.02), "
                "4326), 'emulated_anpr') RETURNING sighting_id",
                sid, vid, old,
            )
            if label != "none":
                await conn.fetchrow(
                    "INSERT INTO events.enforcement_recommendations (sighting_id, "
                    "vehicle_id, rule_id, recommendation, status) VALUES ($1, $2, "
                    "'R_SPD_SEGMENT', 'test', $3) RETURNING recommendation_id",
                    sid, vid, label,
                )
        removed = await vstore.reap_expired_sightings(conn, now - timedelta(days=30))
        left = {
            r["sighting_id"]
            for r in await conn.fetch(
                "SELECT sighting_id FROM corridor.vehicle_sightings "
                "WHERE vehicle_id = $1", vid,
            )
        }
        recs = await conn.fetch(
            "SELECT status, sighting_id FROM events.enforcement_recommendations "
            "WHERE vehicle_id = $1", vid,
        )
        return ids, left, recs, removed

    ids, left, recs, removed = _run(_in_rolled_back_tx(scenario))
    # decided evidence survives the reap; undecided old reads go
    assert ids["approved"] in left and ids["rejected"] in left
    assert ids["pending"] not in left and ids["none"] not in left
    assert removed >= 2
    by_status = {r["status"]: r["sighting_id"] for r in recs}
    assert set(by_status) == {"approved", "rejected", "pending"}
    # migration 012: the pending recommendation SURVIVES its sighting's
    # reap (FK ON DELETE SET NULL) — the audit trail is never cascaded away
    assert by_status["pending"] is None
    assert by_status["approved"] == ids["approved"]


@db
def test_db_an_earmark_only_ever_extends_retention():
    """Default horizon 30 days.  A 7-day earmark must not cut a vehicle's
    20-day-old sightings (an un-earmarked vehicle keeps them); a 60-day
    earmark keeps 45-day-old sightings and lets 61-day-old ones go."""
    async def scenario(conn):
        now = _now()
        out = {}
        for plate, days in (("GJ99ZZ0004", 7), ("GJ99ZZ0005", 60)):
            veh = await vstore.upsert_vehicle_by_plate(conn, plate, "car", now)
            vid = veh["vehicle_id"]
            await vstore.insert_earmark(
                conn, vid, "watch", "pytest", "mva_1988_s207", days, "pytest",
                now, now + timedelta(days=days),
            )
            ages = {}
            for age in (20, 45, 61):
                sid = uuid.uuid4()
                ages[age] = sid
                await conn.fetchrow(
                    "INSERT INTO corridor.vehicle_sightings (sighting_id, "
                    "vehicle_id, plate, camera_id, t, position, source) VALUES "
                    "($1, $2, $3, 'EM-J03', $4, ST_SetSRID(ST_MakePoint(72.58, "
                    "23.02), 4326), 'emulated_anpr') RETURNING sighting_id",
                    sid, vid, plate, now - timedelta(days=age),
                )
            out[days] = (vid, ages)
        await vstore.reap_expired_sightings(conn, now - timedelta(days=30))
        left = {}
        for days, (vid, ages) in out.items():
            rows = await conn.fetch(
                "SELECT sighting_id FROM corridor.vehicle_sightings "
                "WHERE vehicle_id = $1", vid,
            )
            kept = {r["sighting_id"] for r in rows}
            left[days] = {age for age, sid in ages.items() if sid in kept}
        return left

    left = _run(_in_rolled_back_tx(scenario))
    assert left[7] == {20}        # the 30-day default still protects day 20
    assert left[60] == {20, 45}   # the earmark extends to 60 days


@db
def test_db_reaper_is_throttled_and_lock_serialized():
    async def scenario(conn):
        reaper = vstore.RetentionReaper(min_interval_sec=600.0)
        cutoff = _now() - timedelta(days=30)
        first = await reaper.maybe_reap(conn, cutoff)
        second = await reaper.maybe_reap(conn, cutoff)
        return first, second, reaper.last_result

    first, second, last = _run(_in_rolled_back_tx(scenario))
    assert first is not None and second is None
    assert last["ran"] is True


def test_reaper_backs_off_after_failure():
    class Boom:
        def transaction(self):
            raise RuntimeError("db down")

    clock = [1000.0]
    reaper = vstore.RetentionReaper(min_interval_sec=600.0, clock=lambda: clock[0])
    assert _run(reaper.maybe_reap(Boom(), _now())) is None
    assert reaper.last_result["ran"] is False
    assert reaper.due() is False          # a failing reap is not retried per request
    clock[0] += 601.0
    assert reaper.due() is True


def test_engine_future_check_tolerance():
    now = _now()
    engine.check_not_future(now + timedelta(seconds=29), now)
    with pytest.raises(engine.FutureSightingError):
        engine.check_not_future(now + timedelta(seconds=31), now)
    # a human decision gets no skew allowance at all
    engine.check_not_future(now, now, tolerance_sec=0.0)
    with pytest.raises(engine.FutureSightingError, match="retry after"):
        engine.check_not_future(now + timedelta(seconds=1), now, tolerance_sec=0.0)
