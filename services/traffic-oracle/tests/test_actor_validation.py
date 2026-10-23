"""Attribution and input validation on every human write (WP-19, #45 #56).

One rule everywhere a human changes state: the deck-verified
``x-veer-officer`` header (honoured on a loopback connection only —
units/router.py) IS the actor; a differing body name is kept as the claim;
without the header the body names the actor, held to the shared
named-actor rule.  Text bound for TEXT/JSONB is refused with a 422 when it
carries a NUL, a bidi override (U+202E) or a bidi mark (LRM / RLM — the
brain's and the deck's officer rule refuse them too) — never a 500, never
a name that reads differently from what was recorded.

    * earmark create / clear (vehicles): earmarked_by, reason, officer;
      'operator' only while VVIP_REQUIRE_OFFICER is unset;
    * e-challan decision: the note;
    * event lifecycle (POST /events/{id}/status): header-first actor, the
      claim in the ledger note, a one-character body officer is only ever
      a claim (2..64 for a recorded actor, C-F);
    * event create: header-first creator;
    * city signal-plan apply / restore: header-first actor.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("httpx")

from httpx import ASGITransport, AsyncClient  # noqa: E402
from test_event_lifecycle import EVENT, LifecyclePool, _app  # noqa: E402
from test_vehicles_watchlist import FakeRow, FakeStore, _client  # noqa: E402

from traffic_oracle.signals import router as signals_router_mod  # noqa: E402

T0 = datetime(2026, 10, 18, 15, 0, tzinfo=timezone.utc)
LOCAL = "http://127.0.0.1"
HEADER = {"x-veer-officer": "Duty%20Officer%20A"}
#: NUL, a bidi override, and the bidi MARKS (LRM / RLM) the brain and the
#: deck refuse in an officer name too (one name rule on every surface).
BAD = [("NUL", "\x00"), ("U+202E", "‮"), ("LRM", "\u200e"), ("RLM", "\u200f")]


def _earmark_store(vid: uuid.UUID | None = None) -> FakeStore:
    return FakeStore(matches=[
        ("INSERT INTO corridor.vehicles", FakeRow(vehicle_id=vid or uuid.uuid4())),
        ("INSERT INTO corridor.vehicle_earmarks",
         FakeRow(earmark_id=uuid.uuid4(), earmarked_at=T0,
                 retention_until=T0 + timedelta(days=30))),
    ])


EARMARK = {"state": "flagged", "reason": "FIR 4401/2026, Kalupur PS",
           "lawful_basis": "stolen_vehicle_fir"}


def _earmark_args(store: FakeStore) -> tuple:
    (args,) = [a for _k, sql, a in store.calls if "INSERT INTO corridor.vehicle_earmarks" in sql]
    return args


# ─── earmarks ────────────────────────────────────────────────────────────────


class TestEarmarkCreate:
    @pytest.mark.parametrize("label, bad", BAD)
    @pytest.mark.parametrize("field", ["earmarked_by", "reason"])
    def test_control_and_bidi_characters_are_422(self, field, label, bad):
        store = _earmark_store()
        body = {**EARMARK, "earmarked_by": "Duty Officer B"}
        body[field] = f"Duty{bad}Officer B"
        r = _client(store).post("/api/v1/vehicles/GJ27EM9001/earmark", json=body)
        assert r.status_code == 422, (field, label, r.text)
        assert not any("INSERT" in sql for _k, sql, _a in store.calls)  # nothing written

    def test_the_body_officer_is_held_to_the_named_actor_rule(self):
        store = _earmark_store()
        r = _client(store).post("/api/v1/vehicles/GJ27EM9001/earmark",
                                json={**EARMARK, "earmarked_by": "  Duty   Officer   B "})
        assert r.status_code == 200, r.text
        assert r.json()["earmarked_by"] == "Duty Officer B"  # whitespace collapsed
        assert r.json()["actor_source"] == "body"
        for name in ("x", "y" * 121):
            r = _client(_earmark_store()).post("/api/v1/vehicles/GJ27EM9001/earmark",
                                               json={**EARMARK, "earmarked_by": name})
            assert r.status_code == 422, name

    def test_unnamed_is_operator_unless_the_deployment_requires_an_officer(self, monkeypatch):
        store = _earmark_store()
        r = _client(store).post("/api/v1/vehicles/GJ27EM9001/earmark", json=EARMARK)
        assert r.status_code == 200, r.text
        assert r.json()["earmarked_by"] == "operator"
        assert r.json()["actor_source"] == "default"
        assert _earmark_args(store)[5] == "operator"
        monkeypatch.setenv("VVIP_REQUIRE_OFFICER", "1")
        store2 = _earmark_store()
        r2 = _client(store2).post("/api/v1/vehicles/GJ27EM9001/earmark", json=EARMARK)
        assert r2.status_code == 422
        assert "earmarked_by is required" in r2.json()["detail"]
        assert not any("INSERT" in sql for _k, sql, _a in store2.calls)

    def test_the_verified_header_earmarks_and_a_differing_body_is_a_claim(self):
        store = _earmark_store()
        c = _client(store, base_url=LOCAL)
        r = c.post("/api/v1/vehicles/GJ27EM9001/earmark", headers=HEADER,
                   json={**EARMARK, "earmarked_by": "Duty Officer B"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert (body["earmarked_by"], body["actor_source"], body["claimed_officer"]) == (
            "Duty Officer A", "x-veer-officer", "Duty Officer B"
        )
        assert _earmark_args(store)[5] == "Duty Officer A"

    def test_the_header_off_loopback_is_ignored(self):
        store = _earmark_store()
        r = _client(store).post("/api/v1/vehicles/GJ27EM9001/earmark", headers=HEADER,
                                json={**EARMARK, "earmarked_by": "Duty Officer B"})
        assert r.json()["earmarked_by"] == "Duty Officer B"


def _clear_store(cleared_by: str | None) -> FakeStore:
    return FakeStore(matches=[
        ("UPDATE corridor.vehicle_earmarks",
         [FakeRow(earmark_id=uuid.uuid4(), cleared_at=T0, cleared_by=cleared_by)]),
    ])


class TestEarmarkClear:
    @pytest.mark.parametrize("label, bad", BAD)
    def test_control_and_bidi_characters_are_422(self, label, bad):
        store = _clear_store("x")
        r = _client(store).post(f"/api/v1/vehicles/{uuid.uuid4()}/earmark/clear",
                                json={"officer": f"Duty{bad}Officer"})
        assert r.status_code == 422, label
        assert store.calls == []

    def test_the_verified_header_clears(self):
        store = _clear_store("Duty Officer A")
        r = _client(store, base_url=LOCAL).post(
            f"/api/v1/vehicles/{uuid.uuid4()}/earmark/clear", headers=HEADER,
            json={"officer": "Duty Officer B"},
        )
        assert r.status_code == 200, r.text
        (args,) = [a for _k, sql, a in store.calls if "UPDATE corridor.vehicle_earmarks" in sql]
        assert args[1] == "Duty Officer A"
        assert (r.json()["actor_source"], r.json()["claimed_officer"]) == (
            "x-veer-officer", "Duty Officer B"
        )

    def test_the_header_alone_satisfies_require_officer(self, monkeypatch):
        monkeypatch.setenv("VVIP_REQUIRE_OFFICER", "1")
        store = _clear_store("Duty Officer A")
        r = _client(store, base_url=LOCAL).post(
            f"/api/v1/vehicles/{uuid.uuid4()}/earmark/clear", headers=HEADER, json={}
        )
        assert r.status_code == 200, r.text


# ─── the e-challan decision note ─────────────────────────────────────────────


class TestDecisionNote:
    @pytest.mark.parametrize("label, bad", BAD)
    def test_control_and_bidi_characters_are_422(self, label, bad):
        rid = uuid.uuid4()
        store = FakeStore(matches=[
            ("SELECT r.recommendation_id, r.status, r.audit_tail",
             FakeRow(recommendation_id=rid, status="pending", audit_tail=[],
                     sighting_t=T0, rule_id="R_SPD_SEGMENT", vehicle_id=uuid.uuid4(),
                     stolen_vehicle=False)),
        ])
        r = _client(store).post(f"/api/v1/enforcement/recommendations/{rid}/decision",
                                json={"decision": "reject", "officer": "Duty Officer A",
                                      "note": f"plate{bad} misread"})
        assert r.status_code == 422, label
        assert store.calls == []  # refused before the database is touched

    def test_tabs_and_line_breaks_stay_legal(self):
        from traffic_oracle.enforcement import DecisionRequest

        req = DecisionRequest(decision="reject", officer="Duty Officer A",
                              note="line one\nline two\tend")
        assert req.note == "line one\nline two\tend"


# ─── event lifecycle + creation ──────────────────────────────────────────────


def _post(app, path: str, body: dict, *, base: str = "http://t", headers: dict | None = None):
    async def run():
        async with AsyncClient(transport=ASGITransport(app=app), base_url=base) as c:
            return await c.post(path, json=body, headers=headers)

    return asyncio.run(run())


STATUS = f"/api/v1/events/{EVENT}/status"


class TestEventStatusAttribution:
    def test_the_header_is_the_actor_and_the_body_a_claim_in_the_note(self):
        pool = LifecyclePool()
        r = _post(_app(pool), STATUS,
                  {"to_status": "approved", "officer": "B", "note": "night block"},
                  base=LOCAL, headers=HEADER)
        assert r.status_code == 200, r.text
        body = r.json()
        assert (body["actor"], body["actor_source"], body["claimed_officer"]) == (
            "Duty Officer A", "x-veer-officer", "B"
        )
        (row,) = pool.db.log
        assert row["actor"] == "Duty Officer A"
        assert row["note"] == "night block [x-veer-officer: Duty Officer A; claimed officer: B]"

    def test_the_header_alone_needs_no_body_officer(self):
        pool = LifecyclePool()
        r = _post(_app(pool), STATUS, {"to_status": "approved"}, base=LOCAL, headers=HEADER)
        assert r.status_code == 200, r.text
        assert pool.db.log[0]["actor"] == "Duty Officer A"
        assert pool.db.log[0]["note"] is None  # no claim: nothing appended

    def test_a_matching_body_officer_is_no_claim(self):
        pool = LifecyclePool()
        r = _post(_app(pool), STATUS,
                  {"to_status": "approved", "officer": "Duty  Officer A"},
                  base=LOCAL, headers=HEADER)
        assert r.json()["claimed_officer"] is None

    def test_without_the_header_the_body_officer_decides_as_before(self):
        pool = LifecyclePool()
        r = _post(_app(pool), STATUS, {"to_status": "approved", "officer": "Duty Officer B"},
                  headers=HEADER)  # http://t: not loopback, the header is ignored
        assert r.status_code == 200, r.text
        assert (r.json()["actor"], r.json()["actor_source"]) == ("Duty Officer B", "body")

    def test_a_one_character_officer_is_only_ever_a_claim(self):
        pool = LifecyclePool()
        r = _post(_app(pool), STATUS, {"to_status": "approved", "officer": "B"})
        assert r.status_code == 422
        assert pool.db.log == []

    @pytest.mark.parametrize("label, bad", BAD)
    def test_control_and_bidi_characters_are_422(self, label, bad):
        for body in ({"to_status": "approved", "officer": f"Duty{bad}Officer"},
                     {"to_status": "approved", "officer": "Duty Officer", "note": f"a{bad}b"}):
            pool = LifecyclePool()
            r = _post(_app(pool), STATUS, body, base=LOCAL, headers=HEADER)
            assert r.status_code == 422, (label, body)
            assert pool.db.log == []


class TestEventCreateAttribution:
    BODY = {"event_kind": "work_zone", "title": "Metro works",
            "route_spines": [{"segment_ids": [1001]}]}

    def test_the_header_opens_the_event(self):
        pool = LifecyclePool()
        r = _post(_app(pool), "/api/v1/events", {**self.BODY, "officer": "Someone Else"},
                  base=LOCAL, headers=HEADER)
        assert r.status_code == 200, r.text
        body = r.json()
        assert (body["actor"], body["actor_source"], body["claimed_officer"]) == (
            "Duty Officer A", "x-veer-officer", "Someone Else"
        )
        row = pool.db.log[-1]
        assert row["actor"] == "Duty Officer A"
        assert row["note"].endswith(
            "[x-veer-officer: Duty Officer A; claimed officer: Someone Else]"
        )

    def test_a_machine_opener_is_labelled_as_such(self):
        pool = LifecyclePool()
        r = _post(_app(pool), "/api/v1/events",
                  {**self.BODY, "metadata": {"source": "convoy-brain"}})
        assert (r.json()["actor"], r.json()["actor_source"]) == ("convoy-brain", "metadata")
        r2 = _post(_app(LifecyclePool()), "/api/v1/events", self.BODY)
        assert (r2.json()["actor"], r2.json()["actor_source"]) == ("operator", "default")


# ─── city signal writes ──────────────────────────────────────────────────────


class TestSignalAttribution:
    def _app(self, monkeypatch):
        from fastapi import FastAPI

        seen: list[dict] = []

        async def fake_apply(pool, **kw):
            seen.append(kw)
            return {"applied": True, "band": kw["band"], "actor": kw["actor"]}

        async def fake_restore(pool, **kw):
            seen.append(kw)
            return {"restored": 1, "actor": kw["actor"]}

        monkeypatch.setattr(signals_router_mod.signal_writer, "apply_city_plan", fake_apply)
        monkeypatch.setattr(signals_router_mod.signal_writer, "restore_programs", fake_restore)
        app = FastAPI()
        app.include_router(signals_router_mod.router)
        app.state.pool = object()
        return app, seen

    def test_the_header_is_the_recorded_actor_of_an_apply(self, monkeypatch):
        app, seen = self._app(monkeypatch)
        r = _post(app, "/api/v1/signals/plans/apply",
                  {"band": "AMBER", "officer": "Duty Officer B"}, base=LOCAL, headers=HEADER)
        assert r.status_code == 200, r.text
        assert seen[0]["actor"] == "Duty Officer A"
        assert (r.json()["actor_source"], r.json()["claimed_officer"]) == (
            "x-veer-officer", "Duty Officer B"
        )

    def test_the_header_is_the_recorded_actor_of_a_restore(self, monkeypatch):
        app, seen = self._app(monkeypatch)
        r = _post(app, "/api/v1/signals/restore", {"all": True}, base=LOCAL, headers=HEADER)
        assert r.status_code == 200, r.text
        assert seen[0]["actor"] == "Duty Officer A"

    def test_require_officer_is_met_by_the_header(self, monkeypatch):
        monkeypatch.setenv("VVIP_REQUIRE_OFFICER", "1")
        app, seen = self._app(monkeypatch)
        assert _post(app, "/api/v1/signals/plans/apply", {"band": "AMBER"}).status_code == 422
        r = _post(app, "/api/v1/signals/plans/apply", {"band": "AMBER"}, base=LOCAL,
                  headers=HEADER)
        assert r.status_code == 200 and seen[-1]["actor"] == "Duty Officer A"

    def test_a_body_only_apply_is_unchanged(self, monkeypatch):
        app, seen = self._app(monkeypatch)
        r = _post(app, "/api/v1/signals/plans/apply",
                  {"band": "AMBER", "officer": "Duty Officer B"})
        assert r.status_code == 200
        assert seen[0]["actor"] == "Duty Officer B" and r.json()["actor_source"] == "body"


# ─── the shared named-actor rule itself ──────────────────────────────────────


class TestSharedNameRule:
    """city/common.named_actor refuses exactly the set the brain
    (_OFFICER_FORBIDDEN) and the deck (sign-in + every writer) refuse."""

    @pytest.mark.parametrize("mark", ["\u200e", "\u200f"])
    def test_bidi_marks_are_refused_in_a_name(self, mark):
        from traffic_oracle.city.common import named_actor

        with pytest.raises(ValueError, match="control characters"):
            named_actor(f"Duty{mark} Officer A")

    def test_zero_width_joiners_stay_legal(self):
        from traffic_oracle.city.common import named_actor

        # Indic names are written with ZWJ / ZWNJ — never refused
        for name in ("\u0aa6\u0ac7\u0ab5\u0ac0\u200d\u0aac\u0ac7\u0aa8", "Ra\u200cvi Shah"):
            assert named_actor(name) == name

    @pytest.mark.parametrize("escaped", ["%E2%80%8E", "%E2%80%8F"])
    def test_a_header_carrying_a_bidi_mark_is_422(self, escaped):
        pool = LifecyclePool()
        r = _post(_app(pool), STATUS, {"to_status": "approved"}, base=LOCAL,
                  headers={"x-veer-officer": f"Duty%20Officer{escaped}%20A"})
        assert r.status_code == 422, r.text
        assert pool.db.log == []
