"""Escort authority — "officer approves every switch" (user decision, 2026-10-23).

During a live escort the decider only PROPOSES; every route switch, Plan B,
new closure set and hold needs a named officer's click, and a proposal no
officer answers in time EXPIRES while the convoy holds its current approved
plan (it keeps moving; nothing switches). Covered end to end with fakes:

    - a confident decider re-route does NOT switch until approved: no
      route_switched, no closures drafted, no C-O spine update while the
      proposal is pending; the C-J ``pending_approval`` carries the exact
      route, the closures it adds and lifts, the km, the decider tier and
      confidence and ``expires_at``
    - an approval applies exactly that proposal: the route switch, its C-O
      POST /events/{id}/routes under the approving officer's name, the new
      closures, and the audit trail (operator_approved -> route_switched)
    - a rejection keeps the plan; a confident hold is never executed alone
    - no answer before the timeout -> 'expired' ("no officer response —
      current plan held"), the convoy keeps moving on its route; the timeout
      is VVIP_ESCORT_APPROVAL_TIMEOUT_SEC (default 90 s, bounded 15..600)
      and every proposal carries its deadline
    - one open proposal per escort: a newer one supersedes the older
    - the HTTP surface: POST /movements/{id}/escort/approvals/{approval_id}
      (200 / 404 / 409 expired-superseded-decided / 422 bad body), the
      deck-verified x-veer-officer as the deciding actor with the body name
      kept as claimed_officer, a proposal of an escort that is no longer
      running expires (nothing would apply it), and GET /escorts for the
      consoles' lanes
"""

from __future__ import annotations

import asyncio
import time

import httpx
from test_agents import StubBridge
from test_live_escort_motion import (  # noqa: F401 — api is a fixture
    ALT,
    EVENT_ID,
    ROUTE,
    ActionDecider,
    Clock,
    Drive,
    _planned,
    after_departure,
    api,
    live_by_iteration,
    network_executor,
    officer_approves,
    per_tick,
    route_geometry,
    rows,
    seed,
)

from convoy_brain.config import escort_approval_timeout_sec
from convoy_brain.decider import DeciderBrain
from convoy_brain.workflows.live_escort import (
    APPROVAL_TIMEOUT_REASON,
    EscortRun,
    _epoch,
    _EscortLoop,
    _Incident,
    run_live_escort_workflow,
    utc_iso,
)

BYPASS = [{"route_id": "bypass", "segment_ids": [3002, 3005, 3006], "estimated_time_sec": 160}]


class FakeLink:
    """CityLink double: records the C-O spine updates the escort publishes."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def update_route(self, event_id, variant, segment_ids, **kwargs):
        self.calls.append((event_id, variant, list(segment_ids), kwargs))
        return {"status": "updated", "event_id": event_id, "http_status": 200}


async def escort_with(store, *, decider_action, confidence=0.97, routes=None, alternates=None,
                      levels=None, hooks=(), step_m=0.0, max_iterations=6, clock=None,
                      approval_timeout_sec=None, link=None):
    """One escort over the 4 km test corridor: the convoy at 600 m on 3001,
    3003-3004 closed, congestion on 3003 from tick 2 (unless ``levels``)."""
    seed(store, at_m=600.0, active=(3003, 3004))
    run = EscortRun("es-1", officer="Duty Officer A")
    run.alternates = alternates or []
    executor = network_executor(
        live=live_by_iteration(run, levels or after_departure({3003: 0.95})), routes=routes)
    drive = Drive(store, "es-1", start_m=600.0, step_m=step_m, stay=step_m == 0.0)
    drive.hooks.extend(hooks)
    if clock is not None:
        drive.hooks.append(clock.advance)
    link = link or FakeLink()
    result = await run_live_escort_workflow(
        movement_id="es-1", bridge=StubBridge(fail=True), executor=executor,
        context_store=store,
        decider_brain=DeciderBrain(ActionDecider(decider_action, confidence=confidence),
                                   min_interval_sec=0),
        max_iterations=max_iterations, run=run, publish=drive, poll_interval_sec=0,
        gps_timeout_sec=5.0, incident_cooldown_sec=60.0, clear_confirm_sec=0.0, clock=clock,
        city_link=link, approval_timeout_sec=approval_timeout_sec,
    )
    return run, result, link, executor


def decide_at(iteration, decision, officer="Duty Officer A", seen=None):
    """A Drive hook: the officer answers the open proposal at ``iteration``."""
    seen = seen if seen is not None else []

    def hook(run: EscortRun) -> None:
        if run.state.iteration != iteration:
            return
        for row in list(run.approvals.values()):
            if row["status"] == "pending":
                seen.append(run.decide(row["approval_id"], decision, officer,
                                       claimed_officer="Typed Name"))
    return hook


class TestProposalsNeverExecuteAlone:
    async def test_a_confident_reroute_waits_for_the_officer(self, fake_store):
        views: list[tuple] = []
        record = per_tick(lambda r: views.append(
            (list(r.route.segment_ids), r.state.to_doc()["pending_approval"])))
        run, _, link, _ = await escort_with(
            fake_store, decider_action="emergency_reroute", routes=BYPASS,
            hooks=[record, decide_at(5, "approve")], max_iterations=7)
        # Ticks 2-5: proposed and pending — the route, closures and spine untouched.
        pending = [v for v in views if v[1] is not None]
        assert [ids for ids, _ in pending] == [ROUTE] * 4
        view = pending[0][1]
        assert view["kind"] == "reroute" and view["action"] == "emergency_reroute"
        assert view["tier"] == "decider" and view["confidence"] == 0.97
        assert view["reason"] == (
            "decider (decider) at confidence 0.97 — an officer approves every switch")
        assert view["summary"].startswith("re-route via North Bypass, North Link — ")
        assert "avoiding East Arterial (congestion 0.95)" in view["summary"]
        assert view["proposed"]["segment_ids"] == [3001, 3002, 3005, 3006]
        # what is new on it: the roads the convoy would take instead
        assert view["proposed"]["via_segment_ids"] == [3005, 3006]
        assert view["proposed"]["via_road_names"] == ["North Bypass", "North Link"]
        assert view["proposed"]["closures_lifted"] == [3003, 3004]
        assert view["proposed"]["closures_new"] == [3001, 3002, 3005, 3006]
        assert view["proposed"]["closure_type"] == "partial"  # Z class
        assert view["expires_at"] > view["created_at"]
        assert view["detour_segment_ids"] == [3001, 3002, 3005, 3006]  # drawn as a preview
        # Approved at tick 5: the switch ran on the next tick, by the officer.
        [switched] = rows(fake_store, action="route_switched")
        assert switched["ordered_by"] == "officer Duty Officer A"
        assert switched["claimed_officer"] == "Typed Name"
        assert switched["reason"] == "officer-approved detour avoiding [3003]"
        assert run.route.segment_ids == [3001, 3002, 3005, 3006]
        approved = rows(fake_store, action="operator_approved")[0]
        assert approved["officer"] == "Duty Officer A" and approved["kind"] == "reroute"
        assert approved["proposed_by_tier"] == "decider"
        log = [r.get("action") for r in fake_store.contexts["es-1"].decisions_log]
        assert log.index("operator_approved") < log.index("route_switched")

    async def test_the_approved_switch_publishes_its_spine_and_closures(self, fake_store):
        run, _, link, _ = await escort_with(
            fake_store, decider_action="activate_plan_b", alternates=[ALT],
            hooks=[officer_approves()], max_iterations=5)
        # C-O: the new primary spine, under the approving officer's name.
        [(event_id, variant, segment_ids, kwargs)] = link.calls
        assert (event_id, variant, segment_ids) == (EVENT_ID, "plan_b", ALT)
        assert kwargs["officer"] == "Duty Officer A" and kwargs["make_primary"] is True
        assert kwargs["note"].endswith("ordered by officer Duty Officer A")
        [update] = rows(fake_store, action="route_update_updated")
        assert update["officer"] == "Duty Officer A" and update["variant"] == "plan_b"
        # The closures the new route brings were drafted; the abandoned lifted.
        assert rows(fake_store, action="diversions_activated")
        [switched] = rows(fake_store, action="route_switched")
        assert switched["source"] == "plan_b" and switched["closures_lifted"] == [3003, 3004]
        assert fake_store.contexts["es-1"].protocol_state["plan_b"]["active"] is True
        outcome = run.state.approval_outcomes[-1]
        assert outcome["status"] == "approved" and outcome["decided_by"] == "Duty Officer A"
        assert outcome["applied"] is True

    async def test_a_rejected_proposal_keeps_the_plan(self, fake_store):
        progress: list[float] = []
        run, _, link, _ = await escort_with(
            fake_store, decider_action="emergency_reroute", routes=BYPASS, step_m=150.0,
            hooks=[decide_at(3, "reject"), per_tick(lambda r: progress.append(r.state.progress))],
            max_iterations=6)
        assert not rows(fake_store, action="route_switched") and link.calls == []
        assert run.route.segment_ids == ROUTE and not run.operator_actions
        assert run.state.approval_outcomes[-1]["status"] == "rejected"
        assert run.state.pending_approval is None
        assert progress == sorted(progress) and progress[-1] > progress[0]  # it kept moving

    async def test_a_confident_hold_is_only_proposed(self, fake_store):
        holding: list[bool] = []
        run, _, _, _ = await escort_with(
            fake_store, decider_action="hold_position", confidence=0.99,
            hooks=[per_tick(lambda r: holding.append(r.holding))])
        assert not any(holding) and not rows(fake_store, phase="live_escort",
                                             action="hold_position")
        [proposal] = rows(fake_store, type="operator_approval_required")
        assert proposal["kind"] == "hold" and proposal["confidence"] == 0.99
        assert proposal["proposed"]["hold_segment_ids"] == [3003]
        assert proposal["summary"] == (
            "hold the convoy short of East Arterial (congestion 0.95) until it clears, up to 180 s")
        assert proposal["reason"].startswith("AI proposes hold the convoy short of East Arterial")


class TestTimeout:
    async def test_no_answer_expires_and_the_convoy_holds_its_current_plan(self, fake_store):
        clock = Clock(step=5.0)
        progress: list[float] = []
        run, _, link, _ = await escort_with(
            fake_store, decider_action="activate_plan_b", alternates=[ALT], step_m=100.0,
            clock=clock, approval_timeout_sec=15.0, max_iterations=8,
            hooks=[per_tick(lambda r: progress.append(r.state.progress))])
        [expired] = rows(fake_store, action="approval_expired")
        assert expired["reason"] == APPROVAL_TIMEOUT_REASON == (
            "no officer response — current plan held")
        assert expired["timeout_sec"] == 15.0 and expired["kind"] == "plan_b"
        assert expired["summary"].startswith("Plan B via North Bypass")
        # The current approved plan held: no switch, no hold — it kept moving.
        assert not rows(fake_store, action="route_switched") and link.calls == []
        assert run.route.segment_ids == ROUTE and run.hold is None
        assert progress == sorted(progress) and progress[-1] > progress[0]
        outcome = run.state.approval_outcomes[-1]
        assert outcome["status"] == "expired"
        assert outcome["expiry_reason"] == "no officer response — current plan held"
        # A late click changes nothing.
        late, verdict = run.decide(expired["approval_id"], "approve", "Duty Officer A")
        assert verdict == "closed" and late["status"] == "expired" and not run.operator_actions

    async def test_an_answer_after_the_deadline_is_an_expiry_not_a_decision(self):
        run = EscortRun("es-1")
        now = [100.0]
        run.clock = lambda: now[0]
        run.approvals["a1"] = {"approval_id": "a1", "action": "hold_position", "kind": "hold",
                               "reason": "r", "status": "pending", "raised_at": utc_iso(),
                               "expires_mono": 130.0}
        now[0] = 131.0
        row, verdict = run.decide("a1", "approve", "Duty Officer A")
        assert verdict == "expired" and row["status"] == "expired"
        assert row["expiry_reason"] == APPROVAL_TIMEOUT_REASON and not run.operator_actions
        assert run.state.pending_approval is None


class TestTimeoutSetting:
    def test_default_and_bounds(self):
        key = "VVIP_ESCORT_APPROVAL_TIMEOUT_SEC"
        assert escort_approval_timeout_sec({}) == 90.0
        assert escort_approval_timeout_sec({key: "120"}) == 120.0
        assert escort_approval_timeout_sec({key: "5"}) == 15.0      # never shorter than 15 s
        assert escort_approval_timeout_sec({key: "9999"}) == 600.0  # never longer than 10 min
        assert escort_approval_timeout_sec({key: "soon"}) == 90.0

    async def test_a_proposal_carries_its_deadline(self, fake_store):
        seed(fake_store, at_m=600.0)
        run = EscortRun("es-1")
        run.route = route_geometry(ROUTE)
        loop = _EscortLoop(
            run=run, bridge=StubBridge(fail=True), executor=network_executor(), store=fake_store,
            decider=None, adaptive_client=None, lifecycle=None, publish=None,
            poll_interval_sec=0, gps_timeout_sec=5, incident_cooldown_sec=60,
            hold_max_sec=None, approval_timeout_sec=45.0,
        )
        inc = _Incident(peak=0.95, hot=[3003], rows=[], at=0.0, t=utc_iso())
        aid = await loop._raise_approval(inc, recommended="hold_position", reason="r",
                                         tier="decider", confidence=0.9)
        row = run.approvals[aid]
        assert row["timeout_sec"] == 45.0 and run.state.approval_timeout_sec == 45.0
        span = _epoch(row["expires_at"]) - _epoch(row["created_at"])
        assert abs(span - 45.0) < 0.01


class TestOneOpenProposal:
    async def test_a_newer_proposal_supersedes_the_older(self, fake_store):
        seed(fake_store, at_m=600.0)
        run = EscortRun("es-1")
        run.route = route_geometry(ROUTE)
        loop = _EscortLoop(
            run=run, bridge=StubBridge(fail=True), executor=network_executor(), store=fake_store,
            decider=None, adaptive_client=None, lifecycle=None, publish=None,
            poll_interval_sec=0, gps_timeout_sec=5, incident_cooldown_sec=60,
            hold_max_sec=None, approval_timeout_sec=90.0,
        )
        inc = _Incident(peak=0.95, hot=[3003], rows=[], at=0.0, t=utc_iso())
        first = await loop._raise_approval(inc, recommended="hold_position", reason="first",
                                           tier="decider", confidence=0.9)
        second = await loop._raise_approval(inc, recommended="hold_position", reason="second",
                                            tier="decider", confidence=0.9)
        assert run.approvals[first]["status"] == "superseded"
        assert run.approvals[first]["superseded_by"] == second
        assert [a["approval_id"] for a in run.state.pending_approvals] == [second]
        assert run.state.pending_approval["approval_id"] == second
        [audit] = rows(fake_store, action="approval_superseded")
        assert audit["approval_id"] == first and audit["superseded_by"] == second
        row, verdict = run.decide(first, "approve", "Duty Officer A")
        assert verdict == "closed" and row["status"] == "superseded"


class TestApprovalsApi:
    def _run_with(self, api, mid, *, alive=True):  # noqa: F811
        run = EscortRun(mid, officer="Duty Officer A")
        run.clock = time.monotonic
        if alive:
            # a parked task on the app's loop stands in for the escort loop:
            # the run reads as running until the brain's shutdown stops it
            async def park():
                await run._stop.wait()

            async def start():
                run.task = asyncio.create_task(park())

            api.call(start)
        far = time.monotonic() + 600
        for aid, status in (("p-1", "pending"), ("p-2", "pending"), ("x-1", "expired"),
                            ("s-1", "superseded"), ("late", "pending")):
            run.approvals[aid] = {
                "approval_id": aid, "action": "activate_plan_b", "kind": "plan_b",
                "summary": "Plan B via North Bypass", "reason": "r", "status": status,
                "raised_at": utc_iso(), "expires_mono": far if aid != "late" else 0.0,
                "incident": {"peak": 0.95, "hot": [3003], "rows": [], "quality": {}},
            }
        run.approvals["x-1"]["expiry_reason"] = APPROVAL_TIMEOUT_REASON
        run.approvals["s-1"]["superseded_by"] = "p-1"
        api.app.state.escorts._runs[mid] = run
        return run

    def test_answers_and_attribution(self, api):  # noqa: F811
        api.client.base_url = httpx.URL("http://127.0.0.1")
        mid = _planned(api)
        run = self._run_with(api, mid)
        url = f"/movements/{mid}/escort/approvals"
        header = {"x-veer-officer": "Duty%20Officer%20Test"}
        # 422: a bad body; no named officer at all
        assert api.client.post(f"{url}/p-1", json={}).status_code == 422
        assert api.client.post(f"{url}/p-1", json={"decision": "maybe"}).status_code == 422
        unnamed = api.client.post(f"{url}/p-1", json={"decision": "approve"})
        assert unnamed.status_code == 422 and "officer is required" in unnamed.json()["detail"]
        # 404: unknown approval / movement
        assert api.client.post(f"{url}/nope", json={"decision": "approve"},
                               headers=header).status_code == 404
        assert api.client.post("/movements/no-such/escort/approvals/p-1",
                               json={"decision": "approve"}, headers=header).status_code == 404
        # 200: the header officer decides; the body name is only a claim
        ok = api.client.post(f"{url}/p-1", json={"decision": "approve", "officer": "Typed Name"},
                             headers=header)
        assert ok.status_code == 200, ok.text
        body = ok.json()
        assert body["status"] == "approved" and body["decided_by"] == "Duty Officer Test"
        assert body["claimed_officer"] == "Typed Name" and body["kind"] == "plan_b"
        [queued] = run.operator_actions
        assert queued["decided_by"] == "Duty Officer Test"
        assert queued["claimed_officer"] == "Typed Name"
        # 409: decided once only; expired; superseded
        again = api.client.post(f"{url}/p-1", json={"decision": "reject"}, headers=header)
        assert again.status_code == 409 and again.json()["status"] == "approved"
        assert again.json()["detail"] == "proposal already approved by Duty Officer Test"
        expired = api.client.post(f"{url}/x-1", json={"decision": "approve"}, headers=header)
        assert expired.status_code == 409 and expired.json()["status"] == "expired"
        assert expired.json()["detail"] == (
            "proposal expired — no officer response — current plan held")
        superseded = api.client.post(f"{url}/s-1", json={"decision": "approve"}, headers=header)
        assert superseded.status_code == 409 and superseded.json()["status"] == "superseded"
        assert superseded.json()["superseded_by"] == "p-1"
        # an answer after the deadline is an expiry, audited, never a decision
        late = api.client.post(f"{url}/late", json={"decision": "approve"}, headers=header)
        assert late.status_code == 409 and late.json()["status"] == "expired"

        async def trail():
            ctx = await api.app.state.context_store.get(mid)
            return ctx.decisions_log

        [late_row] = [r for r in api.call(trail) if r.get("action") == "approval_expired"]
        assert late_row["approval_id"] == "late"
        assert late_row["reason"] == APPROVAL_TIMEOUT_REASON
        assert late_row["answered_late_by"] == "Duty Officer Test"
        # a rejection is recorded under the deciding officer
        rejected = api.client.post(f"{url}/p-2", json={"decision": "reject"}, headers=header)
        assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"
        [row] = [r for r in api.call(trail) if r.get("action") == "operator_rejected"]
        assert row["officer"] == "Duty Officer Test" and row["approval_id"] == "p-2"
        assert len(run.operator_actions) == 1  # a rejection queues nothing
        # the outcomes reach the C-J document
        doc = api.client.get(f"/movements/{mid}/escort").json()
        outcomes = {o["approval_id"]: o["status"] for o in doc["approval_outcomes"]}
        assert outcomes["p-1"] == "approved" and outcomes["p-2"] == "rejected"
        assert doc["pending_approval"] is None  # nothing is left open

    def test_a_proposal_of_an_escort_that_is_not_running_expires(self, api):  # noqa: F811
        mid = _planned(api)
        run = self._run_with(api, mid, alive=False)  # the loop ended / its task died
        resp = api.client.post(f"/movements/{mid}/escort/approvals/p-1",
                               json={"decision": "approve", "officer": "Duty Officer A"})
        assert resp.status_code == 409 and resp.json()["status"] == "expired"
        assert resp.json()["detail"] == (
            "proposal expired — the escort is not running — nothing to apply")
        assert not run.operator_actions  # nothing queued for a loop that will never run
        assert all(a["status"] != "pending" for a in run.approvals.values())
        doc = api.client.get(f"/movements/{mid}/escort").json()
        assert doc["pending_approval"] is None and doc["pending_approvals"] == []

    def test_the_legacy_route_needs_a_running_escort(self, api):  # noqa: F811
        mid = _planned(api)
        self._run_with(api, mid, alive=False)  # registered, but no task: not running
        resp = api.client.post(f"/movements/{mid}/escort/approve",
                               json={"approval_id": "p-1", "decision": "approve",
                                     "officer": "Duty Officer A"})
        assert resp.status_code == 404

    def test_escorts_listing_carries_the_open_proposal(self, api):  # noqa: F811
        mid = _planned(api)
        assert api.client.post(f"/movements/{mid}/escort", json={}).status_code == 202
        run = api.app.state.escorts.get(mid)
        run.approvals["p-9"] = {
            "approval_id": "p-9", "action": "emergency_reroute", "kind": "reroute",
            "summary": "re-route via North Bypass", "reason": "r", "status": "pending",
            "raised_at": utc_iso(), "expires_mono": time.monotonic() + 600,
            "expires_at": utc_iso(time.time() + 600),
        }
        run._refresh_pending()
        listing = api.client.get("/escorts").json()
        assert listing["authority"] == "officer_approves_every_switch"
        [entry] = [e for e in listing["escorts"] if e["movement_id"] == mid]
        assert entry["alive"] is True and entry["event_id"] == EVENT_ID
        assert entry["pending_approval"]["approval_id"] == "p-9"
        assert entry["pending_approval"]["kind"] == "reroute"
        assert listing["pending"] >= 1
        api.client.post(f"/movements/{mid}/stop", json={})
