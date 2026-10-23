"""C6 privacy parity for the raw vehicle read paths (WP-16).

GET /api/v1/vehicles/{id}/sightings and /trace used to return every read
of any vehicle (and the pursuit interception plan anchored on any read);
they now apply the city surfaces' own rule —
``traffic_oracle.city.common.SQL_LAWFUL_READ``, COMPOSED into every
statement that returns an individual read (vehicles/store.py), never
re-stated: a read leaves the oracle only while its vehicle holds an ACTIVE
earmark, or when it is the evidence of an enforcement recommendation.

Two layers:

* Fake-pool tests (always run): the 404 for a vehicle with no lawful basis
  (count-free, identical for an unknown id, no read statement executed),
  the whole trail under an earmark, the evidence reads alone without one,
  the 'quiet' trace label that never carries a withheld read's time, and
  the statements themselves composing the one predicate.

* DB-backed tests (VEER_TEST_PG_DSN pointing at a SANDBOX store with
  001-013 + seed + post_seed_derivations; never the live :56434 store):
  the predicate on the real schema inside a transaction that is always
  rolled back — earmark placed, cleared, evidence cited — through the
  store functions, the real router and the pursuit plan on the real graph,
  plus the sandbox's own earmarked GJ27EM9001 (its trail keeps working).
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI

pytest.importorskip("httpx")

from httpx import ASGITransport, AsyncClient  # noqa: E402

from traffic_oracle.city.common import SQL_LAWFUL_READ  # noqa: E402
from traffic_oracle.vehicles import NO_LAWFUL_BASIS_DETAIL, pursuit  # noqa: E402
from traffic_oracle.vehicles import router as vehicles_router  # noqa: E402
from traffic_oracle.vehicles import store as vstore  # noqa: E402

T0 = datetime(2026, 10, 18, 15, 0, 0, tzinfo=timezone.utc)

#: Every statement that returns an individual read (or decides whether
#: any may be returned).
INDIVIDUAL_READ_SQL = {
    "SQL_DISCLOSURE_BASIS": vstore.SQL_DISCLOSURE_BASIS,
    "SQL_LAWFUL_SIGHTINGS": vstore.SQL_LAWFUL_SIGHTINGS,
    "SQL_LAWFUL_TRACE": vstore.SQL_LAWFUL_TRACE,
    "SQL_LAWFUL_LATEST_T": vstore.SQL_LAWFUL_LATEST_T,
    "SQL_LAWFUL_LAST_SIGHTINGS": vstore.SQL_LAWFUL_LAST_SIGHTINGS,
    # the pursuit interception plan's anchoring read
    "pursuit._SQL_LATEST_LAWFUL_SIGHTING": pursuit._SQL_LATEST_LAWFUL_SIGHTING,
}


# ── fake pool ─────────────────────────────────────────────────────────────────


class _Ctx:
    """``async with`` yielding a fixed value (an acquired conn, a tx)."""

    def __init__(self, value=None) -> None:
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *exc):
        return False


class Recorded:
    """An asyncpg-style connection answering by exact statement."""

    def __init__(self, basis: tuple[bool, bool] | None, sightings=(), trace=(), latest_t=None):
        self.basis = basis
        self.sightings = list(sightings)
        self.trace = list(trace)
        self.latest_t = latest_t
        self.calls: list[str] = []

    async def fetchrow(self, sql, *args):
        self.calls.append(sql)
        if sql == vstore.SQL_DISCLOSURE_BASIS:
            if self.basis is None:  # an id the fake store has never seen
                return {"earmarked": False, "lawful_reads": False}
            return {"earmarked": self.basis[0], "lawful_reads": self.basis[1]}
        if sql == vstore.SQL_LAWFUL_LATEST_T:
            return {"latest_t": self.latest_t}
        if sql == vstore.REAP_LOCK_SQL:
            return {"got": False}  # a peer holds the reap: no DELETE here
        raise AssertionError("unexpected fetchrow: " + sql[:80])

    async def fetch(self, sql, *args):
        self.calls.append(sql)
        if sql == vstore.SQL_LAWFUL_SIGHTINGS:
            return self.sightings[: args[1]]
        if sql == vstore.SQL_LAWFUL_TRACE:
            return [r for r in self.trace if r["t"] >= args[1]]
        raise AssertionError("unexpected fetch: " + sql[:80])

    def transaction(self):
        return _Ctx()

    def acquire(self):
        return _Ctx(self)


def _app(pool) -> FastAPI:
    app = FastAPI()
    app.state.pool = pool
    app.include_router(vehicles_router)
    return app


def _get(app, path: str):
    async def go():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as c:
            return await c.get(path)

    return asyncio.run(go())


def _sighting(i: int, t: datetime) -> dict:
    return {
        "sighting_id": uuid.UUID(int=i), "plate": "GJ27EM9001", "camera_id": f"EM-CAM-0{i}",
        "t": t, "lon": 72.57 + i / 1000, "lat": 23.02, "speed_kmh": 40.0 + i,
        "heading": 90.0, "vehicle_class": "car", "source": "emulated_anpr",
        "run_id": None, "matched_plate_hit": "stolen_vehicle",
    }


@pytest.fixture(autouse=True)
def _quiet_reaper():
    """The process-wide reaper is throttled; start each test 'just ran'."""
    vstore.REAPER.reset()
    vstore.REAPER._last_attempt = vstore.REAPER._clock()
    yield
    vstore.REAPER.reset()


# ── the rule, composed ────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", sorted(INDIVIDUAL_READ_SQL))
def test_every_individual_read_statement_composes_the_city_rule(name):
    sql = INDIVIDUAL_READ_SQL[name]
    assert SQL_LAWFUL_READ in sql, f"{name} must compose SQL_LAWFUL_READ"
    assert "corridor.vehicle_sightings s" in sql  # the alias the predicate binds
    assert "s.t <= NOW()" in sql or name == "SQL_DISCLOSURE_BASIS"


def test_the_basis_reuses_the_rule_instead_of_restating_an_earmark_test():
    """``earmarked`` is SQL_LAWFUL_READ evaluated for a read that is no
    recommendation's evidence — so it can never drift from the read rule."""
    basis = vstore.SQL_DISCLOSURE_BASIS
    assert basis.count(SQL_LAWFUL_READ) == 2
    assert "NULL::uuid AS sighting_id" in basis
    # nothing outside the composed predicate tests an earmark on its own
    assert basis.replace(SQL_LAWFUL_READ, "").count("vehicle_earmarks") == 0


# ── no lawful basis: 404, count-free ──────────────────────────────────────────


@pytest.mark.parametrize("path", ["sightings", "trace?window_sec=86400"])
def test_unearmarked_vehicle_reads_are_a_count_free_404(path):
    conn = Recorded(basis=(False, False))
    r = _get(_app(conn), f"/api/v1/vehicles/{uuid.uuid4()}/{path}")
    assert r.status_code == 404
    assert r.json() == {"detail": NO_LAWFUL_BASIS_DETAIL}
    assert NO_LAWFUL_BASIS_DETAIL == "no lawful basis to disclose individual reads for this vehicle"
    # nothing beyond the basis was even asked of the store
    assert [c for c in conn.calls if c != vstore.REAP_LOCK_SQL] == [vstore.SQL_DISCLOSURE_BASIS]


def test_unknown_vehicle_answers_exactly_like_an_unearmarked_one():
    unknown = _get(_app(Recorded(basis=None)), f"/api/v1/vehicles/{uuid.uuid4()}/sightings")
    unearmarked = _get(_app(Recorded(basis=(False, False))),
                       f"/api/v1/vehicles/{uuid.uuid4()}/sightings")
    assert (unknown.status_code, unknown.json()) == (unearmarked.status_code, unearmarked.json())


def test_malformed_vehicle_id_is_still_422():
    r = _get(_app(Recorded(basis=(True, True))), "/api/v1/vehicles/not-a-uuid/sightings")
    assert r.status_code == 422


# ── lawful basis: the trail, or the evidence alone ────────────────────────────


def test_earmarked_vehicle_keeps_its_whole_trail():
    rows = [_sighting(i, T0 + timedelta(minutes=10 * i)) for i in (3, 2, 1)]
    conn = Recorded(basis=(True, True), sightings=rows)
    r = _get(_app(conn), f"/api/v1/vehicles/{uuid.uuid4()}/sightings")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["disclosure"] == "active_earmark"
    assert [s["camera_id"] for s in body["sightings"]] == ["EM-CAM-03", "EM-CAM-02", "EM-CAM-01"]
    assert body["source"] == "emulated_anpr"
    assert vstore.SQL_LAWFUL_SIGHTINGS in conn.calls


def test_evidence_reads_only_without_an_earmark():
    evidence = [_sighting(2, T0 + timedelta(minutes=20))]
    conn = Recorded(basis=(False, True), sightings=evidence, trace=evidence,
                    latest_t=evidence[0]["t"])
    vid = uuid.uuid4()
    r = _get(_app(conn), f"/api/v1/vehicles/{vid}/sightings")
    assert r.status_code == 200, r.text
    assert r.json()["disclosure"] == "evidence_only"
    assert [s["sighting_id"] for s in r.json()["sightings"]] == [str(uuid.UUID(int=2))]


def test_earmarked_vehicle_never_sighted_is_200_empty():
    conn = Recorded(basis=(True, False))
    r = _get(_app(conn), f"/api/v1/vehicles/{uuid.uuid4()}/sightings")
    assert r.status_code == 200 and r.json()["sightings"] == []


def test_trace_tracks_lawful_reads_inside_the_window():
    now = datetime.now(timezone.utc)
    pts = [
        {"t": now - timedelta(minutes=m), "lon": 72.57 + m / 1000, "lat": 23.02,
         "speed_kmh": 40.0, "source": "emulated_anpr", "camera_id": f"EM-CAM-0{m}"}
        for m in (9, 5)
    ]
    conn = Recorded(basis=(True, True), trace=pts)
    r = _get(_app(conn), f"/api/v1/vehicles/{uuid.uuid4()}/trace?window_sec=900")
    body = r.json()
    assert r.status_code == 200 and body["status"] == "tracking"
    assert len(body["geometry"]["coordinates"]) == 2 and body["count"] == 2
    assert body["disclosure"] == "active_earmark"


def test_one_position_trace_has_null_geometry_and_keeps_the_position():
    """A LineString needs two positions (RFC 7946): one lawful read in the
    window is 'tracking' with that position — and geometry null, never an
    invalid one-point LineString (WP-19)."""
    now = datetime.now(timezone.utc)
    pts = [{"t": now - timedelta(minutes=3), "lon": 72.571, "lat": 23.021,
            "speed_kmh": 38.0, "source": "emulated_anpr", "camera_id": "EM-CAM-07"}]
    conn = Recorded(basis=(True, True), trace=pts)
    r = _get(_app(conn), f"/api/v1/vehicles/{uuid.uuid4()}/trace?window_sec=900")
    body = r.json()
    assert r.status_code == 200 and body["status"] == "tracking"
    assert body["geometry"] is None
    assert body["count"] == 1 and len(body["positions"]) == 1
    assert body["positions"][0]["position"] == [72.571, 23.021]


def test_quiet_trace_carries_only_a_lawful_read_time():
    """No lawful read in the window: 200 'quiet' with latest_t from the
    LAWFUL latest-time statement — a withheld read's time never leaks."""
    lawful_t = T0 + timedelta(minutes=20)
    conn = Recorded(basis=(False, True), trace=[], latest_t=lawful_t)
    r = _get(_app(conn), f"/api/v1/vehicles/{uuid.uuid4()}/trace?window_sec=900")
    body = r.json()
    assert r.status_code == 200
    assert body["status"] == "quiet" and body["disclosure"] == "evidence_only"
    assert body["geometry"] is None  # no LineString without two positions
    assert body["latest_t"] == lawful_t.isoformat()
    assert vstore.SQL_LAWFUL_LATEST_T in conn.calls


def test_reap_runs_before_the_basis_is_decided():
    """A read the retention reap removes is never disclosed: the throttled
    reap is attempted first, the basis next, the reads last."""
    vstore.REAPER.reset()  # due
    conn = Recorded(basis=(True, True), sightings=[])
    _get(_app(conn), f"/api/v1/vehicles/{uuid.uuid4()}/sightings")
    assert conn.calls[:3] == [
        vstore.REAP_LOCK_SQL, vstore.SQL_DISCLOSURE_BASIS, vstore.SQL_LAWFUL_SIGHTINGS,
    ]


def test_interception_plan_never_anchors_on_a_withheld_read():
    """A pursuit whose vehicle has no lawful read left (earmark cleared, no
    evidence) gets a 404 — the plan never reveals a withheld position."""
    eid, vid = uuid.uuid4(), uuid.uuid4()
    asked: list[str] = []

    class PursuitConn:
        async def fetchrow(self, sql, *args):
            asked.append(sql)
            if "FROM events.corridor_events" in sql:
                return {"event_id": eid, "event_kind": "vehicle_pursuit", "status": "active",
                        "title": "pursuit", "metadata": {"vehicle_id": str(vid)}}
            if sql == pursuit._SQL_LATEST_LAWFUL_SIGHTING:
                return None  # SQL_LAWFUL_READ admits no read of this vehicle
            raise AssertionError("unexpected fetchrow: " + sql[:60])

        def acquire(self):
            return _Ctx(self)

    app = FastAPI()
    app.state.pool = PursuitConn()
    app.include_router(pursuit.router)
    r = _get(app, f"/api/v1/events/{eid}/interception-plan")
    assert r.status_code == 404
    assert "no lawfully disclosable sighting" in r.json()["detail"]
    assert pursuit._SQL_LATEST_LAWFUL_SIGHTING in asked


# ── DB-backed (sandbox store only, always rolled back) ────────────────────────

_DSN = os.environ.get("VEER_TEST_PG_DSN", "")
_LIVE_PORT = ":56434/"  # the shared live store — never written by tests

db = pytest.mark.skipif(
    not _DSN or _LIVE_PORT in _DSN,
    reason="set VEER_TEST_PG_DSN to a SANDBOX store (001-013 + seed + "
           "post_seed_derivations); never the live :56434 store",
)

#: The sandbox's earmarked pursuit target (GJ27EM9001) and an unearmarked
#: vehicle with reads — present in stores cloned from the migrated live DB.
EARMARKED_VEHICLE = uuid.UUID("1b94e9d2-7e11-4174-a735-7dd3be3189cf")
UNEARMARKED_VEHICLE = uuid.UUID("6901155c-9020-4b37-88fc-26e15ed7c911")


class _OneConnPool:
    """The router's pool, bound to one connection inside an OUTER
    transaction (every inner transaction becomes a savepoint)."""

    def __init__(self, conn) -> None:
        self._conn = conn

    def acquire(self):
        return _Ctx(self._conn)


async def _rolled_back(fn):
    import asyncpg

    conn = await asyncpg.connect(_DSN)
    tx = conn.transaction()
    await tx.start()
    try:
        return await fn(conn)
    finally:
        await tx.rollback()  # ALWAYS: nothing persists
        await conn.close()


async def _three_reads(conn) -> tuple[uuid.UUID, list[uuid.UUID], list[datetime]]:
    """A test vehicle with three past reads at one camera, no earmark."""
    now = datetime.now(timezone.utc)
    veh = await vstore.upsert_vehicle_by_plate(conn, "GJ99ZZ0101", "car", now)
    vid = veh["vehicle_id"]
    ids, times = [], []
    for hours in (3, 2, 1):
        sid, t = uuid.uuid4(), now - timedelta(hours=hours)
        await conn.execute(
            "INSERT INTO corridor.vehicle_sightings (sighting_id, vehicle_id, plate, "
            "camera_id, t, position, source) VALUES ($1, $2, 'GJ99ZZ0101', 'PYTEST-CAM', "
            "$3, ST_SetSRID(ST_MakePoint(72.58, 23.02), 4326), 'emulated_anpr')",
            sid, vid, t,
        )
        ids.append(sid)
        times.append(t)
    return vid, ids, times


@db
def test_db_the_rule_on_the_real_schema():
    async def scenario(conn):
        vid, ids, times = await _three_reads(conn)
        now = datetime.now(timezone.utc)
        out = {}
        # 1. no earmark, no evidence: nothing at all
        out["none"] = (
            await vstore.disclosure_basis(conn, vid),
            await vstore.fetch_sightings(conn, vid, 100),
            await vstore.latest_sighting_t(conn, vid),
        )
        # 2. the middle read becomes a recommendation's evidence
        await conn.execute(
            "INSERT INTO events.enforcement_recommendations (sighting_id, vehicle_id, "
            "rule_id, recommendation, status) VALUES ($1, $2, 'R_SPD_SEGMENT', 'pytest', "
            "'pending')",
            ids[1], vid,
        )
        out["evidence"] = (
            await vstore.disclosure_basis(conn, vid),
            [r["sighting_id"] for r in await vstore.fetch_sightings(conn, vid, 100)],
            await vstore.latest_sighting_t(conn, vid),
            [r["camera_id"] for r in await vstore.fetch_trace(conn, vid, now - timedelta(days=1))],
        )
        # 3. an active earmark: the whole trail, newest first
        await vstore.insert_earmark(conn, vid, "watch", "pytest FIR", "stolen_vehicle_fir",
                                    30, "pytest", now, now + timedelta(days=30))
        out["earmarked"] = (
            await vstore.disclosure_basis(conn, vid),
            [r["sighting_id"] for r in await vstore.fetch_sightings(conn, vid, 100)],
            await vstore.latest_sighting_t(conn, vid),
            [r["vehicle_id"] for r in await vstore.last_sightings_by_ids(conn, [vid])],
        )
        # 4. the earmark cleared: withdrawn at once, back to the evidence
        await vstore.clear_active_earmarks(conn, vid, "pytest officer")
        out["cleared"] = (
            await vstore.disclosure_basis(conn, vid),
            [r["sighting_id"] for r in await vstore.fetch_sightings(conn, vid, 100)],
            await vstore.last_sightings_by_ids(conn, [vid]),
        )
        return ids, times, out

    ids, times, out = asyncio.run(_rolled_back(scenario))
    basis, rows, latest = out["none"]
    assert (basis.earmarked, basis.lawful_reads, basis.discloses) == (False, False, False)
    assert rows == [] and latest is None
    basis, rows, latest, trace = out["evidence"]
    assert (basis.earmarked, basis.lawful_reads, basis.label) == (False, True, "evidence_only")
    assert rows == [ids[1]]
    assert latest == times[1]  # NOT the newer, withheld read's time
    assert trace == ["PYTEST-CAM"]
    basis, rows, latest, last = out["earmarked"]
    assert (basis.earmarked, basis.label) == (True, "active_earmark")
    assert rows == list(reversed(ids)) and latest == times[2] and len(last) == 1
    basis, rows, last = out["cleared"]
    assert (basis.earmarked, basis.lawful_reads) == (False, True)
    assert rows == [ids[1]]
    # the listing's "last sighting" falls back to the newest LAWFUL read
    assert [r["t"] for r in last] == [times[1]]


@db
def test_db_router_end_to_end():
    async def scenario(conn):
        vid, ids, _ = await _three_reads(conn)
        app = _app(_OneConnPool(conn))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as c:
            before = [await c.get(f"/api/v1/vehicles/{vid}/{p}")
                      for p in ("sightings", "trace?window_sec=86400")]
            now = datetime.now(timezone.utc)
            await vstore.insert_earmark(conn, vid, "flagged", "pytest FIR", "stolen_vehicle_fir",
                                        30, "pytest", now, now + timedelta(days=30))
            after = [await c.get(f"/api/v1/vehicles/{vid}/{p}")
                     for p in ("sightings", "trace?window_sec=86400")]
            known = await c.get(f"/api/v1/vehicles/{EARMARKED_VEHICLE}/trace?window_sec=86400")
            unearmarked = await c.get(f"/api/v1/vehicles/{UNEARMARKED_VEHICLE}/sightings")
            earmarked_reads = await c.get(f"/api/v1/vehicles/{EARMARKED_VEHICLE}/sightings")
            # What the store itself says about the two reference vehicles
            # (clones of the migrated live DB carry both): the expectation
            # is derived from the data, never skipped.
            standing = {
                vid: await vstore.disclosure_basis(conn, vid)
                for vid in (EARMARKED_VEHICLE, UNEARMARKED_VEHICLE)
            }
            reads = {
                vid: await conn.fetchval(
                    "SELECT count(*) FROM corridor.vehicle_sightings "
                    "WHERE vehicle_id = $1 AND t <= NOW()", vid
                )
                for vid in (EARMARKED_VEHICLE, UNEARMARKED_VEHICLE)
            }
        return ids, before, after, known, unearmarked, earmarked_reads, standing, reads

    vstore.REAPER.reset()
    vstore.REAPER._last_attempt = vstore.REAPER._clock()  # no reap inside this test
    ids, before, after, known, unearmarked, earmarked_reads, standing, reads = asyncio.run(
        _rolled_back(scenario)
    )
    for r in before:
        assert r.status_code == 404 and r.json() == {"detail": NO_LAWFUL_BASIS_DETAIL}
    sightings, trace = (r.json() for r in after)
    assert [s["sighting_id"] for s in sightings["sightings"]] == [str(i) for i in reversed(ids)]
    assert trace["status"] == "tracking" and trace["count"] == 3
    assert trace["disclosure"] == "active_earmark"
    # GJ27EM9001 keeps its trail while its earmark is active (quiet when
    # its reads are older than the window) …
    if standing[EARMARKED_VEHICLE].earmarked:
        assert known.status_code == 200, known.text
        assert known.json()["disclosure"] == "active_earmark"
        assert known.json()["status"] in {"tracking", "quiet"}
        assert earmarked_reads.status_code == 200
        assert len(earmarked_reads.json()["sightings"]) == reads[EARMARKED_VEHICLE]
    else:  # a store whose copy of the earmark lapsed answers like any other
        assert known.status_code == (200 if standing[EARMARKED_VEHICLE].discloses else 404)
    # … and a vehicle with reads but no earmark and no evidence is withheld.
    if reads[UNEARMARKED_VEHICLE] and not standing[UNEARMARKED_VEHICLE].discloses:
        assert unearmarked.status_code == 404
        assert unearmarked.json() == {"detail": NO_LAWFUL_BASIS_DETAIL}
    assert any(
        reads[v] and (standing[v].earmarked or not standing[v].discloses)
        for v in (EARMARKED_VEHICLE, UNEARMARKED_VEHICLE)
    ), "neither reference vehicle is in this store — the live-clone checks did not run"


@db
def test_db_interception_plan_anchors_on_the_newest_lawful_read():
    async def scenario(conn):
        now = datetime.now(timezone.utc)
        veh = await vstore.upsert_vehicle_by_plate(conn, "GJ99ZZ0102", "car", now)
        vid = veh["vehicle_id"]
        cams = {
            r["camera_id"]: (float(r["lon"]), float(r["lat"]))
            for r in await conn.fetch(
                "SELECT camera_id, ST_X(geom) AS lon, ST_Y(geom) AS lat FROM corridor.cameras "
                "WHERE camera_id IN ('EM-CAM-01', 'EM-CAM-03')"
            )
        }
        if len(cams) < 2:
            return None
        reads = {}
        for name, cam, hours in (("evidence", "EM-CAM-01", 3), ("newer", "EM-CAM-03", 1)):
            sid = uuid.uuid4()
            lon, lat = cams[cam]
            await conn.execute(
                "INSERT INTO corridor.vehicle_sightings (sighting_id, vehicle_id, plate, "
                "camera_id, t, position, heading, source) VALUES ($1, $2, 'GJ99ZZ0102', $3, "
                "$4, ST_SetSRID(ST_MakePoint($5, $6), 4326), 90, 'emulated_anpr')",
                sid, vid, cam, now - timedelta(hours=hours), lon, lat,
            )
            reads[name] = str(sid)
        rid = await conn.fetchval(
            "INSERT INTO events.enforcement_recommendations (sighting_id, vehicle_id, rule_id, "
            "recommendation, status) VALUES ($1, $2, 'R_SPD_SEGMENT', 'pytest', 'approved') "
            "RETURNING recommendation_id",
            uuid.UUID(reads["evidence"]), vid,
        )
        eid = await conn.fetchval(
            "INSERT INTO events.corridor_events (event_kind, title, status, metadata) "
            "VALUES ('vehicle_pursuit', 'pytest pursuit', 'active', $1::jsonb) RETURNING event_id",
            json.dumps({"vehicle_id": str(vid), "recommendation_id": str(rid)}),
        )
        app = FastAPI()
        app.state.pool = _OneConnPool(conn)
        app.include_router(pursuit.router)
        out = {}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as c:
            # no earmark: only the evidence read may anchor the plan
            out["evidence_only"] = await c.get(f"/api/v1/events/{eid}/interception-plan")
            await vstore.insert_earmark(conn, vid, "flagged", "pytest FIR", "stolen_vehicle_fir",
                                        30, "pytest", now, now + timedelta(days=30))
            out["earmarked"] = await c.get(f"/api/v1/events/{eid}/interception-plan")
            await vstore.clear_active_earmarks(conn, vid, "pytest officer")
            await conn.execute(
                "DELETE FROM events.enforcement_recommendations WHERE recommendation_id = $1", rid
            )
            out["withheld"] = await c.get(f"/api/v1/events/{eid}/interception-plan")
        return reads, out

    result = asyncio.run(_rolled_back(scenario))
    if result is None:
        pytest.skip("camera registry (post_seed_derivations) not present")
    reads, out = result
    plan = out["evidence_only"]
    assert plan.status_code == 200, plan.text
    assert plan.json()["latest_sighting"]["sighting_id"] == reads["evidence"]
    assert plan.json()["stale"] is True  # 3 h old: the plan says so
    assert out["earmarked"].json()["latest_sighting"]["sighting_id"] == reads["newer"]
    assert out["withheld"].status_code == 404
    assert "no lawfully disclosable sighting" in out["withheld"].json()["detail"]
