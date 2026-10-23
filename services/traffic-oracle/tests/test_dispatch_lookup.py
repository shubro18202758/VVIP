"""GET /api/v1/dispatches/{dispatch_id} — one dispatch + its audit tail (WP-16).

The deck's public /driver projection used to filter a per-event list to
follow its own ambulance request; it can now read the one dispatch.  The
row is exactly a row of GET /api/v1/dispatches (same keys, same values)
with its whole audit tail; 404 for an unknown id, 422 for a malformed one.

Two layers:

* Fake pool through the REAL app (test_units_dispatch's canned tables):
  shape parity with the list, the statements used (one row + one tail),
  status codes.
* DB-backed (VEER_TEST_PG_DSN pointing at a SANDBOX store with 001-013 +
  seed + post_seed_derivations, migration 014 for the attribution checks;
  never the live :56434 store): request → decision with the deck's officer
  header → lifecycle → lookup against the real schema inside a transaction
  that is always rolled back — and the same flow with 014's column dropped
  inside that transaction, the pre-014 store a deploy may meet.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest

pytest.importorskip("httpx")

from httpx import ASGITransport, AsyncClient  # noqa: E402
from test_units_dispatch import DispatchFakePool, _make_app, _run  # noqa: E402

from traffic_oracle.units import router as units_router  # noqa: E402

#: The fields the deck's public projection reads from a dispatch row
#: (src/lib/citizen/public-api.server.ts publicDispatchStatus).
PUBLIC_PROJECTION_FIELDS = {
    "dispatch_id", "event_id", "agency", "status", "unit_id", "waypoint_junction",
    "approved_at", "created_at", "updated_at",
}


def _approved_dispatch(app, pool: DispatchFakePool) -> str:
    r = _run(app, "POST", "/api/v1/events/" + pool.event_id + "/dispatch",
             json={"agency": "ambulance", "unit_id": "108-14", "waypoint_junction": 2,
                   "requested_by": "108-driver"})
    did = r.json()["dispatch_id"]
    _run(app, "POST", f"/api/v1/dispatches/{did}/decision",
         json={"action": "approve", "officer": "Duty Officer R. Mehta", "note": "window open"})
    _run(app, "POST", f"/api/v1/dispatches/{did}/status",
         json={"status": "en_route", "officer": "Duty Officer R. Mehta"})
    return did


def test_one_dispatch_is_a_row_of_the_list_with_its_whole_tail():
    pool = DispatchFakePool()
    app = _make_app(pool)
    did = _approved_dispatch(app, pool)
    one = _run(app, "GET", f"/api/v1/dispatches/{did}")
    assert one.status_code == 200, one.text
    body = one.json()
    listed = next(d for d in _run(app, "GET", "/api/v1/dispatches").json()["dispatches"]
                  if d["dispatch_id"] == did)
    assert body.keys() == listed.keys()
    assert {k: v for k, v in body.items() if k != "audit"} == {
        k: v for k, v in listed.items() if k != "audit"
    }
    assert [a["to_status"] for a in body["audit"]] == ["en_route", "approved", "requested"]
    assert body["audit"][1]["actor"] == "Duty Officer R. Mehta"
    assert all("detail" in a for a in body["audit"])  # additive provenance key
    assert body.keys() >= PUBLIC_PROJECTION_FIELDS
    assert body["agency"] == "ambulance" and body["status"] == "en_route"


def test_lookup_reads_one_row_and_one_tail():
    pool = DispatchFakePool()
    app = _make_app(pool)
    did = _approved_dispatch(app, pool)
    pool.calls.clear()
    assert _run(app, "GET", f"/api/v1/dispatches/{did}").status_code == 200
    assert [sql for sql, _ in pool.calls] == [
        units_router._SQL_DISPATCH_GET, units_router._SQL_AUDIT_TAIL,
    ]


def test_unknown_dispatch_is_404():
    pool = DispatchFakePool()
    r = _run(_make_app(pool), "GET", f"/api/v1/dispatches/{uuid.uuid4()}")
    assert r.status_code == 404 and r.json() == {"detail": "dispatch not found"}


@pytest.mark.parametrize("bad", ["abc", "123", "not-a-uuid", "33333333-3333-3333-3333-33333333333"])
def test_malformed_dispatch_id_is_422(bad):
    pool = DispatchFakePool()
    r = _run(_make_app(pool), "GET", f"/api/v1/dispatches/{bad}")
    assert r.status_code == 422
    assert pool.calls == []  # refused before the store is asked anything


def test_lookup_canonicalises_the_id():
    pool = DispatchFakePool()
    app = _make_app(pool)
    did = _approved_dispatch(app, pool)
    r = _run(app, "GET", f"/api/v1/dispatches/{did.upper()}")
    assert r.status_code == 200 and r.json()["dispatch_id"] == did


def test_list_route_is_untouched():
    pool = DispatchFakePool()
    app = _make_app(pool)
    _approved_dispatch(app, pool)
    body = _run(app, "GET", "/api/v1/dispatches?agency=ambulance").json()
    assert body["count"] == 1 and len(body["dispatches"]) == 1


# ── DB-backed (sandbox store only, always rolled back) ────────────────────────

_DSN = os.environ.get("VEER_TEST_PG_DSN", "")
_LIVE_PORT = ":56434/"  # the shared live store — never written by tests

db = pytest.mark.skipif(
    not _DSN or _LIVE_PORT in _DSN,
    reason="set VEER_TEST_PG_DSN to a SANDBOX store (001-013 + seed + "
           "post_seed_derivations); never the live :56434 store",
)


class _Ctx:
    """``async with`` yielding a fixed value (an acquired conn, a tx)."""

    def __init__(self, value=None) -> None:
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *exc):
        return False


class _OneConnPool:
    """The router's pool, bound to one connection inside an OUTER
    transaction (every inner transaction becomes a savepoint)."""

    def __init__(self, conn) -> None:
        self._conn = conn

    def acquire(self):
        return _Ctx(self._conn)


async def _flow_on_store(*, drop_detail_column: bool) -> dict:
    """request → decision (deck header + a differing body officer) →
    lifecycle → lookup, on the real schema, rolled back."""
    import asyncpg
    from fastapi import FastAPI

    conn = await asyncpg.connect(_DSN)
    tx = conn.transaction()
    await tx.start()
    try:
        has_014 = await conn.fetchval(units_router._SQL_AUDIT_DETAIL_COLUMN)
        if drop_detail_column and has_014:
            await conn.execute("ALTER TABLE corridor.dispatch_events DROP COLUMN detail")
        event_id = await conn.fetchval(
            "INSERT INTO events.corridor_events (event_kind, title, status) "
            "VALUES ('work_zone', 'pytest dispatch lookup', 'active') RETURNING event_id"
        )
        units_router.reset_audit_detail_probe()
        app = FastAPI()
        app.state.pool = _OneConnPool(conn)
        app.include_router(units_router.router)
        hdr = {"x-veer-officer": "Duty%20Officer%20A"}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as c:
            made = await c.post(f"/api/v1/events/{event_id}/dispatch",
                                json={"agency": "ambulance", "unit_id": "108-14",
                                      "requested_by": "108-driver"})
            did = made.json()["dispatch_id"]
            decided = await c.post(f"/api/v1/dispatches/{did}/decision", headers=hdr,
                                   json={"action": "approve", "officer": "Officer B",
                                         "note": "window open"})
            moved = await c.post(f"/api/v1/dispatches/{did}/status", headers=hdr,
                                 json={"status": "en_route"})
            one = await c.get(f"/api/v1/dispatches/{did}")
            unknown = await c.get(f"/api/v1/dispatches/{uuid.uuid4()}")
            malformed = await c.get("/api/v1/dispatches/abc")
            listed = await c.get(f"/api/v1/dispatches?event_id={event_id}")
        rows = await conn.fetch(
            "SELECT actor, note, to_jsonb(e) -> 'detail' AS detail "
            "FROM corridor.dispatch_events e WHERE dispatch_id = $1::uuid ORDER BY seq",
            did,
        )
        return {
            "has_014": has_014, "made": made, "decided": decided, "moved": moved,
            "one": one, "unknown": unknown, "malformed": malformed, "listed": listed,
            "rows": [dict(r) for r in rows],
        }
    finally:
        await tx.rollback()  # ALWAYS: nothing persists (the DROP COLUMN included)
        await conn.close()
        units_router.reset_audit_detail_probe()


@db
def test_db_lookup_and_header_attribution_on_the_real_schema():
    out = asyncio.run(_flow_on_store(drop_detail_column=False))
    if not out["has_014"]:
        pytest.skip("migration 014 not applied to this sandbox store")
    for key in ("made", "decided", "moved", "one", "listed"):
        assert out[key].status_code == 200, (key, out[key].text)
    assert (out["unknown"].status_code, out["malformed"].status_code) == (404, 422)
    one = out["one"].json()
    assert one["decided_by"] == "Duty Officer A" and one["status"] == "en_route"
    listed = out["listed"].json()["dispatches"][0]
    assert {k: v for k, v in one.items() if k != "audit"} == {
        k: v for k, v in listed.items() if k != "audit"
    }
    assert [a["actor"] for a in one["audit"]] == ["Duty Officer A", "Duty Officer A", "108-driver"]
    assert one["audit"][1]["detail"] == {
        "actor": "Duty Officer A", "actor_source": "x-veer-officer",
        "claimed_officer": "Officer B",
    }
    assert one["audit"][0]["detail"] == {
        "actor": "Duty Officer A", "actor_source": "x-veer-officer",
    }
    assert one["audit"][2]["detail"] is None
    stored = out["rows"]
    assert json.loads(stored[1]["detail"])["claimed_officer"] == "Officer B"
    assert stored[1]["note"] == "window open"  # the claim never pollutes the note


@db
def test_db_pre_014_store_keeps_the_claim_in_the_note():
    out = asyncio.run(_flow_on_store(drop_detail_column=True))
    for key in ("made", "decided", "moved", "one"):
        assert out[key].status_code == 200, (key, out[key].text)
    one = out["one"].json()
    assert one["decided_by"] == "Duty Officer A"
    assert all(a["detail"] is None for a in one["audit"])  # read path needs no 014
    assert out["rows"][1]["note"] == (
        "window open [x-veer-officer: Duty Officer A; claimed officer: Officer B]"
    )
    assert out["rows"][1]["actor"] == "Duty Officer A"
