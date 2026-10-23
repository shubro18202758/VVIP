"""Pre-movement workflow: protocol loop, strategic application, city fusion.

Pins the WP-04 plan-path fixes:
    - the security re-route loop keeps the best candidate set (and its
      violations) and returns it as 'conditional' instead of an empty plan;
    - scenario payloads use the oracle's keys (scenario_id /
      route_segment_ids) — checked against the oracle's own source;
    - the decider checkpoint runs AFTER routing over the real candidates
      (offered under content labels, resolved back to their route ids), its
      answer is applied (or explicitly not applied) and audited;
    - every plan is registered on the city picture as a corridor event and a
      re-plan retires the superseded event (C-F, 404 tolerated);
    - the plan deadline degrades the remaining agents to their floors.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import httpx
import pytest

from convoy_brain.city_link import (
    SYSTEM_ACTOR,
    CityLink,
    build_event_payload,
    current_event_id,
)
from convoy_brain.decider import DeciderBrain
from convoy_brain.llm.decider_client import DeciderAnswer
from convoy_brain.memory.convoy_context import ConvoyContext
from convoy_brain.workflows import pre_movement
from convoy_brain.workflows.pre_movement import route_option_label, run_pre_movement_workflow
from conftest import (
    DESTINATION,
    NETWORK,
    ORIGIN,
    FakeExecutor,
    bbox_handler,
    details_handler,
)
from test_agents import StubBridge
from test_planner_snap import route

DEPARTURE = "2026-10-23T09:00:00+05:30"


class FakeCityLink:
    """Records register/cancel calls; scripted outcomes."""

    def __init__(self, cancel_status: str = "cancelled"):
        self.registered: list[dict] = []
        self.cancelled: list[tuple[str, str]] = []
        self.cancel_status = cancel_status
        self._n = 0

    async def register_plan(self, plan, *, event_kind, crowd_class, advance_closure_sec,
                            label=None):
        self._n += 1
        payload = build_event_payload(
            plan, event_kind=event_kind, crowd_class=crowd_class,
            advance_closure_sec=advance_closure_sec, label=label,
        )
        self.registered.append(payload)
        if payload is None:
            return {"status": "skipped", "event_id": None, "reason": "no primary route"}
        return {"status": "registered", "event_id": f"evt-{self._n}", "reason": None}

    async def cancel_event(self, event_id, note):
        self.cancelled.append((event_id, note))
        return {"status": self.cancel_status, "event_id": event_id}


class RouteDecider:
    """Decider double answering the pre-movement question set."""

    def __init__(self, choice=None, confidence=0.9, *, first=False):
        self.choice = choice
        self.confidence = confidence
        self.first = first
        self.questions: list[dict] = []
        self.states: list[str] = []

    async def decide(self, state, questions, request_id=None):
        self.questions.append(questions)
        self.states.append(state)
        options = questions["route_strategy"]["options"]
        value = options[0] if self.first else self.choice
        return {
            name: DeciderAnswer(
                name=name, type=spec["type"],
                value=value if spec["type"] == "choice" else 0.6,
                probability=0.8,
                confidence=None if spec["type"] == "noul" else self.confidence,
            )
            for name, spec in questions.items()
        }

    async def health(self, timeout_sec=3.0):
        return {"loaded": True}

    async def warmup(self):
        return True


def executor(routes_by_pair, *, scenario_capture=None, diversions=None, live=None):
    def find(args):
        return {"routes": routes_by_pair.get(
            (args["origin_segment"], args["destination_segment"]), [])}

    def scenarios(args):
        if scenario_capture is not None:
            scenario_capture.extend(args["scenarios"])
        return {"results": [
            {"scenario_id": s["scenario_id"], "total_disruption_vehicle_hours": 3.5 + i}
            for i, s in enumerate(args["scenarios"])
        ]}

    return FakeExecutor(routes={
        "query_segments_in_bbox": bbox_handler(NETWORK),
        "query_segment_details": details_handler(NETWORK),
        "find_convoy_routes": find,
        "predict_traffic_flow": {"predictions": {}},
        "get_live_traffic": live if live is not None else {"observations": []},
        "plan_diversions": diversions if diversions is not None else {"diversions": []},
        "evaluate_scenarios": scenarios,
    })


async def plan(store, ex, *, vvip_class="Z", decider=None, city_link=None,
               movement_id="7f1f9c7e-8a8e-4c1e-9d55-111111111111", bridge=None, **kwargs):
    return await run_pre_movement_workflow(
        movement_id=movement_id,
        origin=ORIGIN,
        destination=DESTINATION,
        vvip_class=vvip_class,
        planned_departure=DEPARTURE,
        bridge=bridge or StubBridge(fail=True),
        executor=ex,
        context_store=store,
        decider_brain=DeciderBrain(decider, min_interval_sec=0) if decider else None,
        city_link=city_link,
        **kwargs,
    )


COMPLIANT = {(1001, 1003): [route("mip_by", [1001, 1002, 1004], eta=700),
                            route("ksp_alt", [1005, 1001, 1002, 1004], eta=900)]}
NARROW_ONLY = {(1001, 1003): [route("mip_nar", [1001, 1002, 1003], eta=650)]}


def label_of(routes_by_pair: dict, route_id: str) -> str:
    """The decider option label of a fixture candidate (the decider answers
    in labels — content hashes of the segment sequence — never route ids)."""
    for candidates in routes_by_pair.values():
        for candidate in candidates:
            if candidate["route_id"] == route_id:
                return route_option_label(candidate)
    raise KeyError(route_id)


class TestSecurityLoop:
    async def test_reroute_keeps_best_candidates_and_violations(self, fake_store):
        # Z+ (6 lanes): the only route crosses 4-lane 1003; avoiding 1003
        # disconnects the destination -> the first candidate set survives.
        ex = executor(NARROW_ONLY)
        doc = await plan(fake_store, ex, vvip_class="Z+")
        assert doc["status"] == "conditional"
        assert doc["primary_route"]["route_id"] == "mip_nar"
        assert doc["primary_route"]["route_tier"] == "conditional"
        assert [v["segment_id"] for v in doc["security_violations"]] == [1003]
        assert "No fully compliant route" in doc["overall_reasoning"]
        assert "6-lane floor" in doc["overall_reasoning"]
        find_calls = [a for n, a in ex.calls if n == "find_convoy_routes"]
        assert find_calls[0]["avoid_segments"] == []
        assert 1003 in find_calls[-1]["avoid_segments"]
        # the loop stopped as soon as the planner returned nothing
        assert len(doc["security_reroutes"]) == 2
        assert doc["security_reroutes"][-1]["primary_route_id"] is None

    async def test_reroute_that_clears_the_floor_wins(self, fake_store):
        routes = dict(NARROW_ONLY)
        ex = executor(routes)
        # avoiding 1003 leaves 1004 as the destination snap -> compliant bypass
        routes[(1001, 1004)] = [route("mip_by", [1001, 1002, 1004])]
        doc = await plan(fake_store, ex, vvip_class="Z+")
        assert doc["status"] == "approved"
        assert doc["primary_route"]["route_id"] == "mip_by"
        assert doc["security_violations"] == []

    async def test_no_route_is_its_own_status(self, fake_store):
        doc = await plan(fake_store, executor({}))
        assert doc["status"] == "no_route"
        assert doc["primary_route"] is None
        assert doc["overall_reasoning"]
        assert fake_store.contexts[doc["movement_id"]].status == "planning"


class TestScenarioContract:
    async def test_scenario_keys_match_the_oracle(self, fake_store):
        captured: list = []
        doc = await plan(fake_store, executor(COMPLIANT, scenario_capture=captured))
        assert [s["scenario_id"] for s in captured] == ["mip_by", "ksp_alt"]
        for scenario in captured:
            assert scenario["route_segment_ids"]
            assert scenario["route_id"] == scenario["scenario_id"]  # alias kept
            assert scenario["departure_time"] == DEPARTURE
        results = doc["scenario_comparison"]["results"]
        assert results and all(r["total_disruption_vehicle_hours"] > 0 for r in results)
        assert doc["scenario_comparison"]["best"] == "mip_by"

    def test_payload_keys_are_the_ones_scenario_sim_reads(self):
        sim = (Path(__file__).resolve().parents[2] / "traffic-oracle" / "src"
               / "traffic_oracle" / "evaluate" / "scenario_sim.py")
        if not sim.is_file():
            pytest.skip("traffic-oracle source not in this checkout")
        read_keys = set(re.findall(r'\b\w+\.get\("([a-z_]+)"', sim.read_text()))
        assert {"scenario_id", "route_segment_ids"} <= read_keys
        sent = {"scenario_id", "route_id", "route_segment_ids", "route_name", "departure_time"}
        assert {"scenario_id", "route_segment_ids"} <= sent


class TestDeciderApplication:
    async def test_checkpoint_runs_after_routing_over_the_candidates(self, fake_store):
        decider = RouteDecider(first=True)
        doc = await plan(fake_store, executor(COMPLIANT), decider=decider)
        by_label = label_of(COMPLIANT, "mip_by")
        alt_label = label_of(COMPLIANT, "ksp_alt")
        options = decider.questions[0]["route_strategy"]["options"]
        # The real candidates, in tactical rank order, under content labels.
        assert options == [by_label, alt_label, "delay_departure"]
        # The state doc describes the routes under the SAME labels; the
        # oracle's per-request ids appear in neither the doc nor the options.
        state = decider.states[0]
        assert f"- {by_label}: 3 segments" in state
        assert f"- {alt_label}: 4 segments" in state
        assert "mip_by" not in state and "ksp_alt" not in state
        rows = fake_store.contexts[doc["movement_id"]].decisions_log
        checkpoints = [r for r in rows if r.get("phase") == "decider_checkpoint"]
        assert len(checkpoints) == 1
        assert checkpoints[0]["decider_pick"]["applied"] is True
        # The audit row resolves every label to its oracle route id.
        assert checkpoints[0]["route_labels"] == {by_label: "mip_by", alt_label: "ksp_alt"}
        assert checkpoints[0]["answers"]["route_strategy"]["value"] == by_label
        assert doc["decider_checkpoint"]["route_labels"] == checkpoints[0]["route_labels"]
        assert doc["decider_pick"]["route_id"] == "mip_by"
        assert doc["decider_pick"]["choice"] == by_label
        assert doc["decider_pick"]["tier"] == "decider"

    async def test_confident_pick_reorders_primary(self, fake_store):
        doc = await plan(fake_store, executor(COMPLIANT),
                         decider=RouteDecider(label_of(COMPLIANT, "ksp_alt")))
        assert doc["primary_route"]["route_id"] == "ksp_alt"
        assert doc["alternate_routes"][0]["route_id"] == "mip_by"
        assert doc["decider_pick"]["applied"] is True
        assert doc["decider_pick"]["route_id"] == "ksp_alt"
        assert doc["decider_pick"]["reason"] == "strategic tier promoted ksp_alt over mip_by"
        plan_rows = [r for r in fake_store.contexts[doc["movement_id"]].decisions_log
                     if r.get("phase") == "pre_movement"]
        assert plan_rows[-1]["primary_route_id"] == "ksp_alt"
        assert plan_rows[-1]["alternate_route_ids"] == ["mip_by"]

    async def test_low_confidence_disagreement_keeps_tactical_ranking(self, fake_store):
        doc = await plan(fake_store, executor(COMPLIANT),
                         decider=RouteDecider(label_of(COMPLIANT, "ksp_alt"), confidence=0.2))
        assert doc["primary_route"]["route_id"] == "mip_by"
        assert doc["decider_pick"]["applied"] is False
        assert "below the" in doc["decider_pick"]["reason"]
        assert "tactical primary mip_by" in doc["decider_pick"]["reason"]

    async def test_low_confidence_agreement_confirms_the_primary(self, fake_store):
        doc = await plan(fake_store, executor(COMPLIANT),
                         decider=RouteDecider(label_of(COMPLIANT, "mip_by"), confidence=0.2))
        assert doc["primary_route"]["route_id"] == "mip_by"
        assert doc["decider_pick"]["applied"] is True
        assert doc["decider_pick"]["reason"] == (
            "strategic tier confirms the tactical primary (low confidence, tactical agreement)"
        )

    async def test_pick_naming_no_candidate_keeps_the_ranking(self, fake_store):
        # A raw oracle id is not an option any more: it names no candidate.
        doc = await plan(fake_store, executor(COMPLIANT), decider=RouteDecider("ksp_alt"))
        assert doc["primary_route"]["route_id"] == "mip_by"
        assert doc["decider_pick"]["applied"] is False
        assert doc["decider_pick"]["route_id"] is None
        assert doc["decider_pick"]["choice"] == "ksp_alt"
        assert "is not a candidate" in doc["decider_pick"]["reason"]

    async def test_delay_departure_holds_a_compliant_plan(self, fake_store):
        doc = await plan(fake_store, executor(COMPLIANT),
                         decider=RouteDecider("delay_departure"))
        assert doc["status"] == "hold"
        assert doc["hold"] is True
        assert doc["decider_pick"]["route_id"] is None
        assert fake_store.contexts[doc["movement_id"]].status == "hold"

    async def test_conditional_outranks_hold(self, fake_store):
        doc = await plan(fake_store, executor(NARROW_ONLY), vvip_class="Z+",
                         decider=RouteDecider("delay_departure"))
        assert doc["status"] == "conditional"
        assert doc["hold"] is True
        assert "stays conditional" in doc["decider_pick"]["reason"]

    async def test_pick_breaking_the_floor_is_not_applied(self, fake_store):
        routes = {(1001, 1003): [route("mip_by", [1001, 1002, 1004]),
                                 route("ksp_nar", [1001, 1002, 1003])]}
        # Y floor is 2 lanes -> both compliant; Z+ -> only mip_by compliant.
        # The planner already rejects ksp_nar for Z+ (a compliant candidate
        # exists), so it is never offered; the decider-side floor guard is
        # pinned directly in test_pre_movement_route_labels.py.
        decider = RouteDecider(label_of(routes, "ksp_nar"))
        doc = await plan(fake_store, executor(routes), vvip_class="Z+", decider=decider)
        assert doc["primary_route"]["route_id"] == "mip_by"
        assert doc["decider_pick"]["applied"] is False
        assert label_of(routes, "ksp_nar") not in decider.questions[0]["route_strategy"]["options"]

    async def test_withheld_pick_is_recorded_not_applied(self, fake_store):
        class Latched:
            async def search(self, state_doc, *, movement_id=None, top_k=5):
                from test_novelty_latch import memory_ok
                return memory_ok(best_score=0.05)

        brain = DeciderBrain(RouteDecider(label_of(COMPLIANT, "ksp_alt")),
                             memory_client=Latched(), min_interval_sec=0)
        doc = await run_pre_movement_workflow(
            movement_id="m-latch", origin=ORIGIN, destination=DESTINATION,
            vvip_class="Z", planned_departure=DEPARTURE, bridge=StubBridge(fail=True),
            executor=executor(COMPLIANT), context_store=fake_store, decider_brain=brain,
        )
        assert doc["decider_pick"]["tier"] == "novelty_latched"
        assert doc["decider_pick"]["applied"] is False
        assert doc["decider_pick"]["route_id"] == "ksp_alt"  # recorded, resolved
        assert doc["decider_pick"]["reason"].startswith("withheld: novel situation")
        assert doc["primary_route"]["route_id"] == "mip_by"


class TestCityFusion:
    async def test_plan_registers_a_protected_movement_event(self, fake_store):
        link = FakeCityLink()
        doc = await plan(fake_store, executor(COMPLIANT), city_link=link)
        assert doc["event_id"] == "evt-1"
        assert doc["city_event"]["status"] == "registered"
        payload = link.registered[0]
        assert payload["event_kind"] == "protected_movement"
        assert payload["protection_class"] == "Z"
        assert [s["label"] for s in payload["route_spines"]] == ["primary", "alt-1"]
        assert payload["route_spines"][0]["segment_ids"] == [1001, 1002, 1004]
        assert payload["movement_id"] == doc["movement_id"]
        phase = payload["phases"][0]
        assert phase["closure_policy"]["advance_closure_sec"] == 120  # Z floor
        assert phase["window_start"].startswith("2026-10-23T08:58:00")
        meta = payload["metadata"]
        assert meta["source"] == "convoy-brain"
        assert meta["brain_status"] == doc["status"]
        assert "security_score" in meta and "decider_pick" in meta
        ctx = fake_store.contexts[doc["movement_id"]]
        assert current_event_id(ctx) == "evt-1"

    async def test_replan_cancels_the_superseded_event(self, fake_store):
        link = FakeCityLink(cancel_status="not_found")  # C-F not deployed yet
        first = await plan(fake_store, executor(COMPLIANT), city_link=link)
        second = await plan(fake_store, executor(COMPLIANT), city_link=link)
        assert first["event_id"] == "evt-1" and second["event_id"] == "evt-2"
        assert link.cancelled[0][0] == "evt-1"
        assert second["city_event"]["superseded"]["status"] == "not_found"
        rows = [r for r in fake_store.contexts[second["movement_id"]].decisions_log
                if r.get("phase") == "city_event"]
        assert rows[-1]["superseded_event_id"] == "evt-1"

    async def test_no_route_registers_nothing(self, fake_store):
        link = FakeCityLink()
        doc = await plan(fake_store, executor({}), city_link=link)
        assert doc["event_id"] is None
        assert doc["city_event"]["status"] == "skipped"


class TestCityLinkClient:
    def link(self, handler) -> CityLink:
        return CityLink(
            base_url="http://127.0.0.1:8091",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

    def plan_doc(self, **overrides):
        doc = {
            "movement_id": "7f1f9c7e-8a8e-4c1e-9d55-111111111111",
            "vvip_class": "Z+", "status": "approved", "planned_departure": DEPARTURE,
            "primary_route": {"route_id": "p", "segment_ids": [1, 2],
                              "estimated_time_sec": 600, "road_names": ["A", "B"]},
            "alternate_routes": [{"route_id": "a", "segment_ids": [1, 3]}],
            "overall_reasoning": "why", "security_score": 0.9, "decider_pick": None,
        }
        doc.update(overrides)
        return doc

    async def test_register_posts_the_event(self):
        seen = {}

        def handler(request):
            seen["path"] = request.url.path
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"event_id": "e-1", "status": "planning"})

        result = await self.link(handler).register_plan(self.plan_doc(), advance_closure_sec=180)
        assert result["status"] == "registered" and result["event_id"] == "e-1"
        assert seen["path"] == "/api/v1/events"
        body = seen["body"]
        assert body["title"].startswith("Z+ protected movement · A → B")
        assert body["phases"][0]["closure_policy"]["closure_type"] == "full"

    async def test_register_outcomes_never_raise(self):
        for code, status in ((404, "not_deployed"), (422, "rejected"), (500, "failed")):
            result = await self.link(lambda r, c=code: httpx.Response(c, text="x")).register_plan(
                self.plan_doc(), advance_closure_sec=120)
            assert result["status"] == status and result["event_id"] is None

        def boom(request):
            raise httpx.ConnectError("refused")

        assert (await self.link(boom).register_plan(self.plan_doc()))["status"] == "failed"

    async def test_cancel_uses_the_lifecycle_contract(self):
        seen = {}

        def handler(request):
            seen["path"] = request.url.path
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"event_id": "e-1", "from_status": "planning",
                                             "to_status": "cancelled"})

        result = await self.link(handler).cancel_event("e-1", "superseded")
        assert result["status"] == "cancelled"
        assert seen["path"] == "/api/v1/events/e-1/status"
        assert seen["body"]["to_status"] == "cancelled"
        assert seen["body"]["officer"] == SYSTEM_ACTOR
        assert 2 <= len(SYSTEM_ACTOR) <= 64

    async def test_cancel_tolerates_404_and_409(self):
        not_found = await self.link(lambda r: httpx.Response(404, json={"detail": "Not Found"})
                                    ).cancel_event("e-1", "n")
        conflict = await self.link(lambda r: httpx.Response(
            409, json={"detail": "illegal", "from_status": "completed", "allowed": []})
        ).cancel_event("e-1", "n")
        assert not_found["status"] == "not_found"
        assert conflict["status"] == "conflict"

    def test_payload_respects_oracle_bounds(self):
        doc = self.plan_doc(
            movement_id="not-a-uuid",
            primary_route={"route_id": "p", "segment_ids": list(range(100)),
                           "estimated_time_sec": 60},
            alternate_routes=[{"route_id": f"a{i}", "segment_ids": [i]} for i in range(12)],
        )
        payload = build_event_payload(doc, advance_closure_sec=0)
        assert len(payload["route_spines"]) == 8
        assert len(payload["route_spines"][0]["segment_ids"]) == 64
        assert payload["movement_id"] is None
        assert payload["metadata"]["movement_ref"] == "not-a-uuid"
        assert len(payload["metadata"]) <= 16
        assert payload["phases"][0]["closure_policy"]["closure_type"] == "signal_priority"


class TestLatencyShape:
    async def test_analyst_and_planner_run_concurrently(self, fake_store):
        state = {"inflight": 0, "peak": 0}

        class SlowBridge:
            async def generate(self, system_prompt, user_prompt, json_mode=True,
                               suppress_thinking=True, **kwargs):
                state["inflight"] += 1
                state["peak"] = max(state["peak"], state["inflight"])
                await asyncio.sleep(0.05)
                state["inflight"] -= 1
                return "{}"

        live = {"observations": [{"segment_id": 1001, "congestion_idx": 0.3, "speed_kmh": 40}]}
        await plan(fake_store, executor(COMPLIANT, live=live), bridge=SlowBridge())
        assert state["peak"] >= 2

    async def test_silent_corridor_skips_analyst_narration(self, fake_store):
        bridge = StubBridge(replies=[])
        doc = await plan(fake_store, executor(COMPLIANT), bridge=bridge)
        assert doc["corridor_status"] == "unknown"
        assert doc["tiers"]["corridor"] == "deterministic"
        assert not any("assess_corridor_conditions" in c for c in bridge.calls)

    async def test_empty_oracle_diversion_plan_skips_the_coordinator(self, fake_store):
        bridge = StubBridge(replies=[])
        doc = await plan(fake_store, executor(COMPLIANT), bridge=bridge)
        assert doc["diversion_directives"] == []
        assert doc["tiers"]["diversions"] == "deterministic"
        assert not any("generate_activation_directives" in c for c in bridge.calls)

    async def test_deadline_degrades_remaining_agents(self, fake_store, monkeypatch):
        monkeypatch.setattr(pre_movement, "MIN_TACTICAL_CALL_SEC", 0.0)
        bridge = StubBridge(replies=["{}"] * 10)
        doc = await plan(fake_store, executor(COMPLIANT), bridge=bridge, deadline_sec=0.0)
        assert doc["deadline"]["tactical_calls_skipped"] >= 1
        assert bridge.calls == []  # nothing reached the tactical tier
        assert doc["primary_route"]["route_id"] == "mip_by"  # deterministic floor
        assert doc["routing_tier"] == "heuristic"
