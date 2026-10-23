"""Wave-3 brain answer quality and escort safety (WP-14 T6, T7, T9-T11, T13).

    T6   llama_bridge exposes finish_reason; a structured agent whose reply was
         cut at its cap retries ONCE as compact JSON under a larger cap (never
         the identical call), skips the repair when the plan budget has no
         room, and the floor says why it ran; the route / diversion budgets
    T7   diversion directives — the model's and the floor's — say what the
         oracle planned: an impractical detour is hold / meter with its reason
         and distance against the limit, an unpinned end is a partial detour,
         a found detour names its alternative segments
    T9   the plan is persisted in ONE field-scoped update: a GPS fix and an
         audit row written while the plan ran survive; a conditional Z+ plan
         states 'No fully compliant route:' exactly once; the reasoning ends
         with concrete factors
    T10  the deck-verified x-veer-officer is the escort's officer (the body's
         claim is kept); an escort on a cancelled event fails; an event an
         officer cancels mid-escort stops it, lifts its closures and closes
         the movement; POST /escort on a closed event is 409
    T11  a route switch publishes the new spine as the event's primary (C-O),
         best effort and audited
    T13  live-traffic summaries name the road
    plus the movement label and the segment-by-segment escort ETA
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
from conftest import FakeExecutor, json_transport
from test_agents import StubBridge
from test_live_escort_motion import (
    ALT,
    EVENT_ID,
    ROUTE,
    Clock,
    Drive,
    _planned,
    api,  # noqa: F401  (fixture)
    escort,
    network_executor,
    route_geometry,
    rows,
    seed,
)
from test_pre_movement_fusion import NARROW_ONLY, executor, plan

from convoy_brain.agents import diversion_coordinator as diversion_module
from convoy_brain.agents import route_planner as route_module
from convoy_brain.agents import security_liaison as security_module
from convoy_brain.agents import traffic_analyst as analyst_module
from convoy_brain.agents.base import (
    AgentSpec,
    UnboundedSchemaError,
    agent_budget,
    estimate_tokens,
    grounded_text,
    schema_max_instance,
    structured_agent_call,
    whole_sentences,
)
from convoy_brain.agents.diversion_coordinator import DiversionCoordinatorAgent, compact_plan_rows
from convoy_brain.city_link import CityLink, build_event_payload
from convoy_brain.config import (
    AGENT_BUDGET_MARGIN,
    AGENT_MAX_TOKENS_CEILING,
    LLAMA_CALL_TIMEOUT_SEC,
    LLAMA_DECODE_FLOOR_TOK_PER_SEC,
    LLAMA_PREFILL_ALLOWANCE_SEC,
)
from convoy_brain.gps_sim import RouteGeometry
from convoy_brain.llm.heuristics import (
    apply_detour_facts,
    diversion_posture,
    heuristic_diversion_directives,
    summarize_tool_result,
)
from convoy_brain.llm.llama_bridge import LlamaBridge
from convoy_brain.llm.schemas import load_schema
from convoy_brain.memory.convoy_context import ConvoyContext, ConvoyContextStore
from convoy_brain.workflows.live_escort import EscortRun, _EscortLoop

# ---------------------------------------------------------------------------
# T6 — budgets, finish_reason, compact repair
# ---------------------------------------------------------------------------


class MetaBridge:
    """generate_with_meta double: scripted (content, finish_reason) replies."""

    def __init__(self, replies, remaining=None):
        self.replies = list(replies)
        self.calls: list[dict] = []
        self._remaining = remaining

    async def generate_with_meta(self, **kwargs):
        self.calls.append(kwargs)
        return self.replies.pop(0)

    async def generate(self, **kwargs):  # pragma: no cover - the meta path is used
        raise AssertionError("generate() must not be used when generate_with_meta exists")


class BudgetBridge(MetaBridge):
    def remaining(self):
        return self._remaining


SPEC = AgentSpec(name="route_planner", system_prompt="sys", schema_name="route_planner")
GOOD = json.dumps({"primary_route": None, "alternate_routes": [], "rejected_routes": [],
                   "overall_reasoning": "ok", "confidence": "high"})

#: The plan agents (pre_movement): each first call is budgeted at its schema's
#: bounded maximum.
PLAN_AGENTS = (analyst_module.SPEC, route_module.SPEC, security_module.SPEC,
               diversion_module.SPEC)

#: Replies recorded from the live tactical model on the Z+ plan (SVPI Airport
#: -> Narendra Modi Stadium, 2026-10-23) with llama-server's own
#: completion_tokens: the analyst's full-precision floats (one token per
#: digit), the security review pretty-printed, and the 8-directive
#: diversion reply that ran 704 tokens (30 s) under the old schema.
LIVE_ANALYST_REPLY = (
    '{\n  "overall_status":"green","congestion_summary":{"avg_congestion_idx":0.08'
    '31033756531111,"peak_congestion_idx":0.1585751026868820},"risk_segments":[],'
    '"trend":"improving","recommendation":"No changes recommended","confidence":"'
    'medium"\n}'
)
LIVE_SECURITY_REPLY = (
    '{\n  "compliant": false,\n  "violations": [\n    {\n      "segment_id": 1044,\n  '
    '    "rule": "ROAD_WIDTH",\n      "detail": "Segment 1044 (Drive-In Road (IIM '
    'Link)) has 4 lanes, minimum 6 required for Z+",\n      "severity": "critical"'
    '\n    },\n    {\n      "segment_id": 1011,\n      "rule": "ROAD_WIDTH",\n      "d'
    'etail": "Segment 1011 (Drive-In Road) has 4 lanes, minimum 6 required for Z+'
    '",\n      "severity": "critical"\n    },\n    {\n      "segment_id": 1042,\n     '
    ' "rule": "ROAD_WIDTH",\n      "detail": "Segment 1042 (Drive-In Riverfront Li'
    'nk) has 4 lanes, minimum 6 required for Z+",\n      "severity": "critical"\n  '
    '  }\n  ],\n  "warnings": [],\n  "recommendations": [\n    "Increase road width t'
    'o at least 6 lanes for segments 1044, 1011, and 1042 to comply with Z+ road '
    'width requirements."\n  ],\n  "security_score": 0.3,\n  "confidence": "medium"\n'
    '}'
)
LIVE_DIVERSION_REPLY = (
    '{"directives":[{"segment_id":1046,"action":"activate","agency":"traffic_poli'
    'ce","timing_sec":180,"detail":"Full closure, no detour available. High prior'
    'ity due to 1562m queue and 0.14 congestion."},{"segment_id":1035,"action":"a'
    'ctivate","agency":"traffic_police","timing_sec":180,"detail":"Full closure, '
    'no detour available. High priority due to 1065m queue and 0.12 congestion."}'
    ',{"segment_id":1034,"action":"activate","agency":"traffic_police","timing_se'
    'c":180,"detail":"Full closure, no detour available. High priority due to 104'
    '5m queue and 0.08 congestion."},{"segment_id":1002,"action":"activate","agen'
    'cy":"traffic_police","timing_sec":180,"detail":"Full closure, no detour avai'
    'lable. High priority due to 931m queue and 0.02 congestion."},{"segment_id":'
    '1042,"action":"activate","agency":"traffic_police","timing_sec":180,"detail"'
    ':"Full closure, no detour available. High priority due to 930m queue and 0.1'
    '3 congestion."},{"segment_id":1041,"action":"activate","agency":"traffic_pol'
    'ice","timing_sec":180,"detail":"Full closure, no detour available. High prio'
    'rity due to 859m queue and 0.01 congestion."},{"segment_id":1033,"action":"a'
    'ctivate","agency":"traffic_police","timing_sec":180,"detail":"Full closure, '
    'no detour available. High priority due to 773m queue and 0.04 congestion."},'
    '{"segment_id":1011,"action":"activate","agency":"traffic_police","timing_sec'
    '":180,"detail":"Full closure, no detour available. High priority due to 767m'
    ' queue and 0.07 congestion."}],"queue_alerts":[{"segment_id":1046,"estimated'
    '_queue_m":1562.0,"action":"evacuate_immediately"},{"segment_id":1035,"estima'
    'ted_queue_m":1065.0,"action":"evacuate_immediately"},{"segment_id":1034,"est'
    'imated_queue_m":1045.0,"action":"evacuate_immediately"},{"segment_id":1002,"'
    'estimated_queue_m":931.0,"action":"evacuate_immediately"},{"segment_id":1042'
    ',"estimated_queue_m":930.0,"action":"evacuate_immediately"},{"segment_id":10'
    '41,"estimated_queue_m":859.0,"action":"evacuate_immediately"},{"segment_id":'
    '1033,"estimated_queue_m":773.0,"action":"evacuate_immediately"},{"segment_id'
    '":1011,"estimated_queue_m":767.0,"action":"evacuate_immediately"}],"overall_'
    'status":"active","confidence":"medium" }'
)
LIVE_REPLIES = [(LIVE_ANALYST_REPLY, 93), (LIVE_SECURITY_REPLY, 309), (LIVE_DIVERSION_REPLY, 704)]


class TestAgentBudgets:
    async def test_bridge_reports_finish_reason(self):
        def handler(request):
            return httpx.Response(200, json={"choices": [{
                "message": {"content": '{"a": "cut'}, "finish_reason": "length"}]})

        bridge = LlamaBridge(base_url="http://127.0.0.1:8084", client=json_transport(handler))
        content, finish = await bridge.generate_with_meta("s", "u", schema={"type": "object"},
                                                          max_tokens=64)
        assert (content, finish) == ('{"a": "cut', "length")
        assert await bridge.generate("s", "u", schema={"type": "object"}) == '{"a": "cut'

    async def test_a_cut_reply_is_retried_compact_under_a_larger_cap(self):
        cut = '{"primary_route": {"route_id": "r1", "segment_ids": [1, 2'
        bridge = MetaBridge([(cut, "length"), (GOOD, "stop")])
        result, tier = await structured_agent_call(
            bridge, SPEC, "payload", lambda: {"floor": True}, max_tokens=380)
        assert tier == "gemma" and result["overall_reasoning"] == "ok"
        assert [c["max_tokens"] for c in bridge.calls] == [380, 760]
        repair = bridge.calls[1]["user_prompt"]
        assert "cut off at the length limit" in repair and "single-line" in repair
        assert "20 words or fewer" in repair

    async def test_retry_cap_and_floor_reason(self):
        bridge = MetaBridge([("{", "length"), ("{", "length")])
        result, tier = await structured_agent_call(
            bridge, SPEC, "payload", lambda: {"floor": True}, max_tokens=768)
        assert tier == "heuristic" and result["floor"] is True
        assert [c["max_tokens"] for c in bridge.calls] == [768, 1024]
        assert result["_fallback_reason"] == (
            "reply cut at the 768-token cap; repair cut at the 1024-token cap")

    async def test_no_repair_without_plan_budget(self):
        bridge = BudgetBridge([("not json", "stop")], remaining=5.0)
        result, tier = await structured_agent_call(
            bridge, SPEC, "payload", lambda: {"floor": True})
        assert tier == "heuristic" and len(bridge.calls) == 1
        assert "repair skipped (5s of plan budget left)" in result["_fallback_reason"]

    async def test_a_generate_only_bridge_still_works(self):
        result, tier = await structured_agent_call(
            StubBridge(replies=[GOOD]), SPEC, "payload", lambda: {"floor": True})
        assert tier == "gemma" and result["confidence"] == "high"

    def test_route_and_diversion_schemas_are_bounded_and_compact(self):
        route = load_schema("route_planner")["properties"]
        alt = route["alternate_routes"]
        assert alt["maxItems"] == 3 and route["rejected_routes"]["maxItems"] == 4
        for entry in (route["primary_route"]["anyOf"][1], alt["items"],
                      route["rejected_routes"]["items"]):
            # route_id + reason only: no echoed segment lists or scores, no extra keys
            assert entry["additionalProperties"] is False
            assert entry["required"] == ["route_id", "reason"]
            assert set(entry["properties"]) == {"route_id", "reason"}
            assert entry["properties"]["reason"]["maxLength"] == route_module.REASON_MAX_CHARS
        assert route_module.REASON_MAX_CHARS == 160
        assert route["overall_reasoning"]["maxLength"] == route_module.OVERALL_MAX_CHARS == 600
        div = load_schema("diversion_coordinator")["properties"]
        assert div["directives"]["maxItems"] == 6
        assert div["directives"]["items"]["properties"]["detail"]["maxLength"] == 120
        assert div["queue_alerts"]["maxItems"] == 4
        assert "overall_status" not in div  # computed from the final directives, never narrated
        for spec in PLAN_AGENTS:
            assert "single-line JSON" in spec.system_prompt, spec.name

    @pytest.mark.parametrize("spec", PLAN_AGENTS, ids=lambda spec: spec.name)
    def test_each_plan_agent_budget_covers_its_schema_maximum(self, spec):
        # The longest compact reply the grammar admits (every string at its
        # maxLength, every array at maxItems, every optional key present) fits
        # the agent's first-call budget, which fits under the ceiling — so the
        # first reply is never cut at the cap — and the bridge's HTTP wall lets
        # that budget decode in full.
        widest = json.dumps(schema_max_instance(load_schema(spec.schema_name)),
                            separators=(",", ":"))
        need = estimate_tokens(widest) * AGENT_BUDGET_MARGIN
        budget = agent_budget(spec.schema_name)
        assert need <= AGENT_MAX_TOKENS_CEILING, f"{spec.name}: schema too loose for the ceiling"
        assert budget >= need
        wall = LlamaBridge(base_url="http://127.0.0.1:8084").call_wall(budget)
        assert wall >= LLAMA_PREFILL_ALLOWANCE_SEC + budget / LLAMA_DECODE_FLOOR_TOK_PER_SEC

    def test_an_unbounded_schema_has_no_maximum(self):
        for schema in ({"type": "string"}, {"type": "array", "items": {"type": "integer"}},
                       {"type": "object", "properties": {}}):
            with pytest.raises(UnboundedSchemaError):
                schema_max_instance(schema)
        # a union gives its longest branch: a 32-character id beats an 8-digit
        # integer, a 3-character one does not
        assert schema_max_instance({"type": ["string", "integer"], "maxLength": 32}) == "x" * 32
        assert schema_max_instance({"type": ["string", "integer"], "maxLength": 3}) == 12345678
        assert schema_max_instance({"anyOf": [{"type": "null"}, {"type": "boolean"}]}) is False
        assert agent_budget("tool_arguments") == 512  # open object: the flat default

    @pytest.mark.parametrize("reply, tokens", LIVE_REPLIES,
                             ids=["analyst", "security", "diversion"])
    def test_the_token_estimate_never_undercounts_a_live_reply(self, reply, tokens):
        assert estimate_tokens(reply) >= tokens

    async def test_the_http_wall_covers_the_calls_budget(self):
        walls = []

        def handler(request):
            walls.append(request.extensions["timeout"]["read"])
            return httpx.Response(200, json={"choices": [{
                "message": {"content": "{}"}, "finish_reason": "stop"}]})

        bridge = LlamaBridge(base_url="http://127.0.0.1:8084", client=json_transport(handler))
        await bridge.generate_with_meta("s", "u", schema={"type": "object"}, max_tokens=970)
        await bridge.generate_with_meta("s", "u", schema={"type": "object"}, max_tokens=48)
        # 970 tokens at the 16 tok/s floor need ~66 s: the old flat 30 s wall
        # killed a 704-token reply its budget allowed; a small budget keeps 30 s.
        assert walls[0] == pytest.approx(
            LLAMA_PREFILL_ALLOWANCE_SEC + 970 / LLAMA_DECODE_FLOOR_TOK_PER_SEC)
        assert walls[1] == LLAMA_CALL_TIMEOUT_SEC

    async def test_the_coordinator_sends_compact_rows_under_its_budget(self, fake_store):
        plan_rows = {"diversions": [
            {"segment_id": 1012, "diversion_type": "full_closure",
             "activate_at": "2026-10-22T15:44:05Z", "estimated_queue_m": 120.4,
             "detour_status": "found", "alt_segment_ids": [1013],
             "geometry": {"type": "LineString", "coordinates": [[1, 2], [3, 4]]}},
            {"segment_id": 1015, "diversion_type": "partial_closure", "estimated_queue_m": 80,
             "detour_status": "impractical", "detour_reason": "too far"},
        ]}
        live = {"observations": [{"segment_id": 1012, "congestion_idx": 0.414, "speed_kmh": 30}]}
        rows_ = compact_plan_rows(plan_rows, live)
        assert [r["segment_id"] for r in rows_] == [1015, 1012]  # no usable detour first
        assert rows_[1] == {"segment_id": 1012, "diversion_type": "full_closure",
                            "detour_status": "found", "activate_at": "2026-10-22T15:44:05Z",
                            "estimated_queue_m": 120, "congestion": 0.41}
        reply = json.dumps({"directives": [], "queue_alerts": [],
                            "overall_status": "ok", "confidence": "medium"})
        bridge = MetaBridge([(reply, "stop")])
        agent = DiversionCoordinatorAgent(
            bridge, FakeExecutor(routes={"plan_diversions": plan_rows, "get_live_traffic": live}),
            fake_store)
        await agent.activate_diversions("m", [1012, 1015], vvip_class="Z")
        assert bridge.calls[0]["max_tokens"] == agent_budget("diversion_coordinator")
        payload = json.loads(bridge.calls[0]["user_prompt"])
        assert "diversion_plan" not in payload and "live_traffic" not in payload
        assert payload["plan_rows"] == rows_


class TestNarrationHygiene:
    """What the operator reads of a narration: whole sentences with figures."""

    def test_a_clipped_string_ends_on_its_last_sentence(self):
        base = ("ETA 46 min over 43.7 km, fastest of five; 3 Drive-In Road segments are "
                "4 lanes against the Z+ 6-lane floor.")
        clipped = (base + " Therefore, the convoy needs a waiver for each of them")[:160]
        assert whole_sentences(clipped, 160) == base
        assert whole_sentences("Queue 1566 m on SP Ring Road needs marshals at", 46) == (
            "Queue 1566 m on SP Ring Road needs marshals …")
        # not at the limit: the model ended it there itself
        assert whole_sentences("staff the junction for queue discharge", 80) == (
            "staff the junction for queue discharge")
        # the model trailing off itself below the bound (live Z+ run)
        trailed = ("Second best route based on weighted score of 0.8672. The route has a good "
                   "balance between time, disruption, security, and complexity. The route has "
                   "3 lane ...")
        assert whole_sentences(trailed, 160) == (
            "Second best route based on weighted score of 0.8672. The route has a good "
            "balance between time, disruption, security, and complexity.")
        assert whole_sentences("Queue 1566 m …", 160) == "Queue 1566 m …"

    def test_a_reason_without_a_figure_is_no_reason(self):
        assert grounded_text("The route has a higher ETA than the primary route.") is None
        assert grounded_text("ETA 47 min; 4 lanes short on Drive-In Road.") == (
            "ETA 47 min; 4 lanes short on Drive-In Road.")

    def test_the_merge_keeps_figures_and_replaces_vacuous_reasons(self):
        def route(rid, segments, km, minutes, score, violations):
            return {"route_id": rid, "reason": "", "route_tier": "conditional",
                    "lane_violations": [{}] * violations, "segment_ids": list(range(segments)),
                    "distance_m": km * 1000.0, "estimated_time_sec": minutes * 60.0,
                    "weighted_score": score}

        shaped = [route("mip_a", 18, 43.7, 46, 0.88, 3), route("ksp_b", 19, 44.9, 47, 0.86, 4),
                  route("ksp_c", 20, 46.2, 69, 0.73, 3)]
        base = ("ETA 46 min over 43.7 km, fastest of five; 3 Drive-In Road segments are "
                "4 lanes against the Z+ 6-lane floor.")
        narration = {
            "primary_route": {"route_id": "mip_a", "reason": (
                base + " Therefore, the convoy needs a waiver for each of them")[:160]},
            "alternate_routes": [{"route_id": "ksp_b", "reason": (
                "The route has a higher ETA and a lower weighted score than the primary.")}],
            "rejected_routes": [{"route_id": "ksp_c", "reason": "More lane violations."}],
            "overall_reasoning": "All candidates breach the lane floor.",
            "confidence": "medium",
        }
        result = route_module._merge_narration(narration, "gemma", shaped, [])
        assert result["primary_route"]["reason"] == base
        assert result["alternate_routes"][0]["reason"] == (
            "19 segments, 44.9 km, ETA 47 min, weighted score 0.86; 4 protocol violation(s).")
        assert result["rejected_routes"] == [{"route_id": "ksp_c", "reason": (
            "rejected by the tactical review — 20 segments, 46.2 km, ETA 69 min, weighted "
            "score 0.73; 3 protocol violation(s).")}]

    def test_a_reason_contradicting_the_lane_audit_is_not_shown(self):
        def route(rid, violations):
            return {"route_id": rid, "reason": "", "route_tier": "conditional",
                    "lane_violations": [{}] * violations, "segment_ids": [1, 2, 3],
                    "distance_m": 43700.0, "estimated_time_sec": 2724.0, "weighted_score": 0.89}

        shaped = [route("mip_a", 3), route("ksp_b", 3), route("ksp_c", 4)]
        narration = {
            # seen live on the Z+ primary, which has 3 segments below the floor
            "primary_route": {"route_id": "mip_a", "reason": (
                "Best route with 0.8852 weighted score. 45.4 min ETA. No lane violations.")},
            "alternate_routes": [
                {"route_id": "ksp_b", "reason": "0.8718 weighted score. 46.4 min ETA. "
                                                "3 lane violations."},
                {"route_id": "ksp_c", "reason": "0.8697 weighted score. 3 lane-violations."},
            ],
            "rejected_routes": [], "overall_reasoning": "", "confidence": "medium",
        }
        result = route_module._merge_narration(narration, "gemma", shaped, [])
        line = route_module._route_line(shaped[0])
        assert result["primary_route"]["reason"] == line  # "No lane violations" is false
        assert result["alternate_routes"][0]["reason"] == (
            "0.8718 weighted score. 46.4 min ETA. 3 lane violations.")  # consistent: kept
        assert result["alternate_routes"][1]["reason"] == route_module._route_line(shaped[2])


# ---------------------------------------------------------------------------
# T7 — detour facts on every directive
# ---------------------------------------------------------------------------


PLAN_ROWS = {"diversions": [
    {"segment_id": 1012, "diversion_type": "full_closure",
     "activate_at": "2026-10-22T15:44:05+00:00", "alt_segment_ids": [1013, 1014],
     "detour_status": "found", "detour_distance_m": 1800,
     "entry_pinned": True, "exit_pinned": True, "detour_limit_m": 5000},
    {"segment_id": 1015, "diversion_type": "partial_closure", "activate_at": "2026-10-22T15:45:00Z",
     "alt_segment_ids": [1020], "detour_status": "impractical", "detour_distance_m": 7200,
     "detour_limit_m": 5000, "detour_reason": "every way round exceeds the limit"},
    {"segment_id": 1016, "diversion_type": "full_closure", "alt_segment_ids": [1021],
     "detour_status": "found", "entry_pinned": False, "exit_pinned": True},
    {"segment_id": 1017, "diversion_type": "speed_restriction", "alt_segment_ids": [1022]},
]}


class TestDetourDirectives:
    def test_the_floor_reads_the_oracle_vocabulary(self):
        result = heuristic_diversion_directives(PLAN_ROWS, 180)
        by_id = {d["segment_id"]: d for d in result["directives"]}
        assert by_id[1012]["detail"] == (
            "Activate full closure on segment 1012 at 21:14:05 IST (180s before the convoy "
            "reaches it). Divert via segment(s) 1013, 1014 (1.8 km).")
        assert by_id[1012]["alt_segment_ids"] == [1013, 1014]
        held = by_id[1015]
        assert held["detour_status"] == "impractical"
        assert held["detour_reason"] == "every way round exceeds the limit"
        assert "HOLD / METER" in held["detail"]
        assert "7.2 km detour vs 5.0 km practicality limit" in held["detail"]
        assert "alt_segment_ids" not in held and "1020" not in held["detail"]
        assert "Partial detour" in by_id[1016]["detail"]
        # an older oracle (no detour_status) is left as the floor wrote it
        assert by_id[1017]["detail"] == (
            "Activate speed restriction on segment 1017 180s before convoy arrival.")
        assert "detour_status" not in by_id[1017]
        assert "1 closure(s) have no practical detour" in result["overall_status"]

    def test_a_model_detour_the_oracle_found_impractical_is_replaced(self):
        model = [{"segment_id": 1015, "action": "activate", "agency": "traffic_police",
                  "timing_sec": 180, "detail": "Divert cross traffic via 1020."},
                 {"segment_id": 1012, "action": "activate", "agency": "traffic_police",
                  "timing_sec": 180, "detail": "Close with police at both ends"}]
        shaped = {d["segment_id"]: d for d in apply_detour_facts(model, PLAN_ROWS)}
        assert "1020" not in shaped[1015]["detail"]
        assert shaped[1015]["detail"].startswith(
            "Activate partial closure on segment 1015 at 21:15:00 IST.")
        assert shaped[1012]["detail"] == (
            "Close with police at both ends. Divert via segment(s) 1013, 1014 (1.8 km).")

    async def test_the_plan_carries_the_detour_fields(self, fake_store):
        doc = await plan(fake_store, executor(NARROW_ONLY, diversions=PLAN_ROWS), vvip_class="Z")
        directives = {d["segment_id"]: d for d in doc["diversion_directives"]}
        assert directives[1015]["detour_status"] == "impractical"
        assert directives[1015]["detour_reason"] == "every way round exceeds the limit"

    def test_detour_facts_are_applied_once(self):
        # The gemma path re-applies the facts to the floor rows it fills in:
        # a found detour's clause must not be stated twice ("Divert via … .
        # Divert via … ." on the Z+ plan's segment 1029).
        once = heuristic_diversion_directives(PLAN_ROWS, 180)["directives"]
        twice = apply_detour_facts(once, PLAN_ROWS)
        assert [d["detail"] for d in twice] == [d["detail"] for d in once]
        assert twice[0]["detail"].count("Divert via segment(s) 1013, 1014") == 1

    async def test_the_status_is_computed_and_floor_rows_read_once(self, fake_store):
        # The model writes its most critical closures only; a summary it might
        # still send describes that subset (live: "6 full closures and 1
        # partial closure with no detour available" for an 18-closure plan).
        model = {"directives": [{"segment_id": 1015, "action": "activate",
                                 "agency": "traffic_police", "timing_sec": 180,
                                 "detail": "Hold cross traffic; 640 m queue expected."}],
                 "queue_alerts": [{"segment_id": 1012, "estimated_queue_m": 640.0,
                                   "action": "staff the junction for queue discharge"}],
                 "overall_status": "1 full closure with no detour available.",
                 "confidence": "medium"}
        tools = FakeExecutor(routes={"plan_diversions": PLAN_ROWS, "get_live_traffic": {}})
        bridge = MetaBridge([(json.dumps(model), "stop")])
        agent = DiversionCoordinatorAgent(bridge, tools, fake_store)
        result = await agent.activate_diversions("m", [1012, 1015, 1016, 1017], vvip_class="Z")
        assert result["_tier"] == "gemma"
        assert result["overall_status"] == (
            "4 closure directive(s); 1 with no practical detour — hold / meter cross traffic "
            "there; 2 with a planned detour; longest queue 640 m on segment 1012.")
        assert result["overall_status"] == diversion_posture(result["directives"],
                                                             result["queue_alerts"])
        floor = {d["segment_id"]: d for d in result["directives"]}[1012]
        assert floor["source"] == "deterministic_floor"
        assert floor["detail"].count("Divert via segment(s) 1013, 1014") == 1
        assert "overall_status" not in bridge.calls[0]["schema"]["properties"]


# ---------------------------------------------------------------------------
# T9 — one atomic finalize, one verdict sentence, concrete basis
# ---------------------------------------------------------------------------


class TestPlanFinalize:
    async def test_a_concurrent_fix_and_audit_row_survive_the_plan(self):
        from test_convoy_context_atomic import FakeValkey

        store = ConvoyContextStore("redis://unused", client=FakeValkey())
        mid = "7f1f9c7e-8a8e-4c1e-9d55-222222222222"
        await store.put(ConvoyContext(movement_id=mid, vvip_class="Z", label="ACCEPTANCE TEST"))

        class RacingCityLink:
            """Writes a GPS fix and a chat audit row WHILE the plan registers."""

            def __init__(self):
                self.labels = []

            async def register_plan(self, plan_doc, **kwargs):
                self.labels.append(kwargs.get("label"))
                await store.update_position(mid, 72.56, 23.02, speed_kmh=30.0,
                                            t="2026-10-22T21:00:00.000Z", source="gps")
                await store.append_decision(mid, {"phase": "orchestrator_turn", "action": "chat"})
                return {"status": "registered", "event_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}

            async def cancel_event(self, event_id, note):  # pragma: no cover - no previous event
                return {"status": "cancelled"}

        city = RacingCityLink()
        doc = await plan(store, executor(NARROW_ONLY), city_link=city, movement_id=mid)
        ctx = await store.get(mid)
        assert ctx.convoy_position == (72.56, 23.02) and ctx.position_source == "gps"
        assert any(r.get("action") == "chat" for r in ctx.decisions_log)
        assert [r["phase"] for r in ctx.decisions_log][-2:] == ["pre_movement", "city_event"]
        assert ctx.event_id == doc["event_id"] and ctx.status == doc["status"]
        assert city.labels == ["ACCEPTANCE TEST"] and doc["label"] == "ACCEPTANCE TEST"

    async def test_the_verdict_sentence_appears_once(self, fake_store):
        doc = await plan(fake_store, executor(NARROW_ONLY), vvip_class="Z+")
        assert doc["status"] == "conditional"
        assert doc["overall_reasoning"].count("No fully compliant route:") == 1

    async def test_the_reasoning_ends_with_concrete_factors(self, fake_store):
        doc = await plan(fake_store, executor(NARROW_ONLY, diversions=PLAN_ROWS), vvip_class="Z+")
        basis = doc["overall_reasoning"][doc["overall_reasoning"].index("Basis: "):]
        assert "Z+ lane floor 6 lanes — 1 segment(s) of the primary below it" in basis
        assert "segments avoided on re-route: 1003" in basis
        assert ("closures activate 180 s before arrival on 4 segment(s), "
                "1 with no practical detour") in basis
        assert "least-disruptive scenario mip_nar at 3.5 vehicle-hours" in basis
        assert "no live observations on the corridor (feed silent)" in basis
        assert doc["tier_notes"]["routing"] == "tactical tier unavailable"


    def test_a_null_queue_estimate_never_fails_the_basis(self):
        from convoy_brain.workflows.pre_movement import _plan_basis

        primary = {"distance_m": 43700.0, "estimated_time_sec": 2760.0}
        diversions = {"directives": [], "queue_alerts": [
            {"segment_id": 1046, "estimated_queue_m": None, "action": "staff the junction"},
            {"segment_id": 1035, "estimated_queue_m": "long", "action": "meter"},
            {"segment_id": 1034, "estimated_queue_m": 1052.4, "action": "meter"},
        ]}
        basis = _plan_basis({"vvip_class": "Z"}, primary, {}, diversions, {}, {})
        assert "longest queue 1052 m on segment 1034" in basis
        diversions["queue_alerts"] = diversions["queue_alerts"][:2]
        assert "longest queue" not in _plan_basis({"vvip_class": "Z"}, primary, {}, diversions,
                                                  {}, {})


# ---------------------------------------------------------------------------
# T10 — verified officer, closed events
# ---------------------------------------------------------------------------


class TestVerifiedOfficer:
    def request(self, headers, host="127.0.0.1"):
        from starlette.requests import Request

        raw = [(k.lower().encode(), v.encode()) for k, v in headers]
        return Request({"type": "http", "method": "POST", "path": "/x", "headers": raw,
                        "server": (host, 8214), "query_string": b""})

    def test_header_rules(self):
        from fastapi import HTTPException

        from convoy_brain.__main__ import _attributed, verified_officer

        assert verified_officer(self.request([])) is None
        assert verified_officer(self.request([("x-veer-officer", "Duty%20Officer%20Test")])) \
            == "Duty Officer Test"
        assert verified_officer(self.request(
            [("x-veer-officer", "%E0%AA%B0%E0%AA%BE%E0%AA%9C%20Patel")])) == "રાજ Patel"
        # off loopback the header is ignored (the deck is the one LAN surface)
        remote = self.request([("x-veer-officer", "Mallory")], host="10.0.0.5")
        assert verified_officer(remote) is None
        for bad in (
            [("x-veer-officer", "A"), ("x-veer-officer", "B")],   # not exactly once
            [("x-veer-officer", "Duty%2")],                       # broken escape
            [("x-veer-officer", "%FF%FE")],                       # not UTF-8
            [("x-veer-officer", "X")],                            # too short
            [("x-veer-officer", "Officer%E2%80%AEA")],            # bidi override
            [("x-veer-officer", "Officer%0AB")],                  # control character
        ):
            with pytest.raises(HTTPException) as exc:
                verified_officer(self.request(bad))
            assert exc.value.status_code == 422
        assert _attributed(self.request([("x-veer-officer", "Duty%20Officer%20Test")]),
                           "Typed  Name") == ("Duty Officer Test", "Typed Name")
        assert _attributed(self.request([("x-veer-officer", "Duty%20Officer%20Test")]),
                           "Duty  Officer Test") == ("Duty Officer Test", None)
        assert _attributed(self.request([]), "Typed Name") == ("Typed Name", None)

    def test_the_header_officer_starts_and_stops_the_escort(self, api):  # noqa: F811
        api.client.base_url = httpx.URL("http://127.0.0.1")
        mid = _planned(api)
        started = api.client.post(f"/movements/{mid}/escort", json={"officer": "Typed Name"},
                                  headers={"x-veer-officer": "Duty%20Officer%20Test"})
        assert started.status_code == 202

        async def trail():
            ctx = await api.app.state.context_store.get(mid)
            return ctx.decisions_log

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            row = next((r for r in api.call(trail) if r.get("action") == "escort_started"), None)
            if row:
                break
            time.sleep(0.02)
        assert row["officer"] == "Duty Officer Test" and row["claimed_officer"] == "Typed Name"
        assert api.app.state.lifecycle.calls[0][2] == "Duty Officer Test"
        stopped = api.client.post(f"/movements/{mid}/stop", json={"officer": "Someone Else"},
                                  headers={"x-veer-officer": "Duty%20Officer%20Test"})
        assert stopped.status_code == 200
        stop_row = next(r for r in api.call(trail) if r.get("action") == "stop_requested")
        assert stop_row["officer"] == "Duty Officer Test"
        assert stop_row["claimed_officer"] == "Someone Else"

    def test_escort_on_a_closed_event_is_refused(self, api):  # noqa: F811
        mid = _planned(api)

        async def status(event_id, since=None):
            return {"status": "cancelled", "found": True, "source": "history"}

        api.app.state.lifecycle.status = status
        refused = api.client.post(f"/movements/{mid}/escort", json={})
        assert refused.status_code == 409
        assert refused.json()["status"] == "cancelled" and refused.json()["event_id"] == EVENT_ID


class ClosingLifecycle:
    """The event is active for ``open_checks`` reads, then an officer cancels it."""

    def __init__(self, open_checks=1, activation=None):
        self.checks = 0
        self.open_checks = open_checks
        self.activation = activation

    async def transition(self, event_id, to_status, officer, note):
        return self.activation or {"outcome": "transitioned", "event_id": event_id,
                                   "to_status": to_status, "http_status": 200,
                                   "from_status": "approved"}

    async def spines(self, event_id):
        return {}

    async def status(self, event_id, since=None):
        self.checks += 1
        return {"status": "active" if self.checks <= self.open_checks else "cancelled",
                "found": True, "source": "active"}

    async def closed_by(self, event_id, to_status):
        return "Duty Officer Test"


class TestExternalClose:
    async def test_an_officer_cancel_stops_the_escort_and_lifts_closures(self, fake_store):
        seed(fake_store, at_m=100.0, active=(3003, 3004))
        clock = Clock(step=20.0)
        drive = Drive(fake_store, "es-1", start_m=100.0, step_m=50.0)
        published: list[dict] = []
        drive.hooks.append(lambda run: published.append(run.state.to_doc()))
        drive.hooks.append(clock.advance)
        run, result = await escort(fake_store, network_executor(), lifecycle=ClosingLifecycle(),
                                   publish=drive, clock=clock, max_iterations=20)
        assert result["status"] == "stopped"
        assert "cancelled by Duty Officer Test" in result["detail"]
        assert "2 closure(s) lifted" in result["detail"]
        ctx = fake_store.contexts["es-1"]
        assert ctx.status == "cancelled" and ctx.active_diversions == []
        [closed] = rows(fake_store, action="event_closed_externally")
        assert closed["officer"] == "Duty Officer Test" and closed["to_status"] == "cancelled"
        assert sorted(closed["closures_lifted"]) == [3003, 3004]
        # The published C-J document agrees with the lift: the schedule the
        # running escort published is gone from the final document (the ops
        # HUD read the stale one as '· 2 diversions active' after the cancel).
        assert any(doc["active_diversions"] for doc in published[:-1])
        final = published[-1]
        assert final["status"] == "stopped" and final["active_diversions"] == []
        assert final["hold"] is None and run.state.to_doc()["active_diversions"] == []

    async def test_a_failed_close_write_does_not_claim_the_lift(self, fake_store, monkeypatch):
        from convoy_brain.workflows import live_escort as escort_module

        real = escort_module.mutate_context

        async def failing(store, mid, fn, **kwargs):
            if getattr(fn, "__name__", "") == "close":
                raise RuntimeError("valkey down")
            return await real(store, mid, fn, **kwargs)

        monkeypatch.setattr(escort_module, "mutate_context", failing)
        seed(fake_store, at_m=100.0, active=(3003, 3004))
        clock = Clock(step=20.0)
        drive = Drive(fake_store, "es-1", start_m=100.0, step_m=50.0)
        drive.hooks.append(clock.advance)
        run, result = await escort(fake_store, network_executor(), lifecycle=ClosingLifecycle(),
                                   publish=drive, clock=clock, max_iterations=20)
        assert result["status"] == "stopped"
        assert "closures NOT lifted" in result["detail"] and "lifted," not in result["detail"]
        assert fake_store.contexts["es-1"].active_diversions == ["3003", "3004"]
        [closed] = rows(fake_store, action="event_closed_externally")
        assert closed["closures_lifted"] == [] and closed["closure_lift_failed"] == "valkey down"
        # still in force, so the document still shows them
        assert run.state.to_doc()["active_diversions"]

    async def test_an_escort_on_a_cancelled_event_fails_at_start(self, fake_store):
        seed(fake_store, at_m=100.0)
        conflict = {"outcome": "conflict", "event_id": EVENT_ID, "to_status": "active",
                    "http_status": 409, "from_status": "cancelled", "allowed": []}
        _run, result = await escort(fake_store, network_executor(),
                                    lifecycle=ClosingLifecycle(activation=conflict))
        assert result["status"] == "failed"
        assert "already cancelled" in result["detail"]
        assert fake_store.contexts["es-1"].status == "approved"  # never activated


# ---------------------------------------------------------------------------
# T11 — the route switch publishes the new spine (C-O)
# ---------------------------------------------------------------------------


class TestRouteSpinePublication:
    async def test_city_link_update_route_outcomes(self):
        seen = []

        def handler(request):
            seen.append((request.url.path, json.loads(request.content)))
            code = {"reroute-1": 200, "reroute-2": 404, "reroute-3": 409, "reroute-4": 422}[
                json.loads(request.content)["variant"]]
            body = {"detail": "event is completed"} if code == 409 else {"spines": []}
            return httpx.Response(code, json=body)

        link = CityLink("http://127.0.0.1:8114", client=json_transport(handler))
        outcomes = [
            (await link.update_route(
                EVENT_ID, f"reroute-{n}", [3001, 3005], officer=None))["status"]
            for n in (1, 2, 3, 4)
        ]
        assert outcomes == ["updated", "not_deployed", "conflict", "rejected"]
        path, body = seen[0]
        assert path == f"/api/v1/events/{EVENT_ID}/routes"
        assert body == {"variant": "reroute-1", "segment_ids": [3001, 3005],
                        "make_primary": True, "officer": "convoy-brain (system)"}
        assert (await link.update_route("not-a-uuid", "plan_b", [1]))["status"] == "skipped"

    async def test_a_switch_publishes_and_audits(self, fake_store):
        seed(fake_store, at_m=100.0)

        class FakeLink:
            def __init__(self):
                self.calls = []

            async def update_route(self, event_id, variant, segment_ids, **kwargs):
                self.calls.append((event_id, variant, segment_ids, kwargs))
                return {"status": "not_deployed", "event_id": event_id, "http_status": 404,
                        "detail": "Not Found"}

        link = FakeLink()
        run = EscortRun("es-1")
        run.route = route_geometry(ROUTE)
        run.state.event_id = EVENT_ID
        loop = _EscortLoop(
            run=run, bridge=StubBridge(fail=True), executor=network_executor(), store=fake_store,
            decider=None, adaptive_client=None, lifecycle=None, publish=None,
            poll_interval_sec=0, gps_timeout_sec=5, incident_cooldown_sec=60,
            hold_max_sec=None, city_link=link,
        )
        loop.coordinator = DiversionCoordinatorAgent(StubBridge(fail=True), network_executor(),
                                                     fake_store)
        plan_b = route_geometry(ALT)
        plan_b.source = "plan_b"
        await loop._switch_route(plan_b, "officer Duty Officer Test", reason="Plan B")
        await asyncio.gather(*list(loop.side_tasks), return_exceptions=True)
        assert link.calls[0][:3] == (EVENT_ID, "plan_b", ALT)
        assert link.calls[0][3]["officer"] == "Duty Officer Test"
        [row] = rows(fake_store, action="route_update_not_deployed")
        assert row["variant"] == "plan_b" and row["http_status"] == 404


# ---------------------------------------------------------------------------
# T13 / labels / ETA
# ---------------------------------------------------------------------------


class TestRoadNamesLabelsEta:
    def test_live_summary_names_the_road(self):
        named = {"observations": [
            {"segment_id": 1012, "congestion_idx": 0.62, "speed_kmh": 21.0,
             "road_name": "Ashram Road", "data_quality": "synthetic"},
            {"segment_id": 1015, "congestion_idx": 0.20, "speed_kmh": 18.0}]}
        text = summarize_tool_result("get_live_traffic", {"segment_ids": [1012, 1015]}, named)
        assert "worst Ashram Road (1012) at 0.62" in text
        assert "slowest segment 1015 at 18 km/h" in text
        assert text.endswith("(SIMULATED FEED)")

    def test_a_movement_label_prefixes_the_event(self):
        plan_doc = {"movement_id": "7f1f9c7e-8a8e-4c1e-9d55-111111111111", "vvip_class": "Z",
                    "primary_route": {"segment_ids": [1001], "road_names": ["Relief Road"]}}
        payload = build_event_payload(plan_doc, label="  ACCEPTANCE   TEST escort ")
        assert payload["title"] == "ACCEPTANCE TEST escort · Z protected movement · Relief Road"
        assert payload["metadata"]["label"] == "ACCEPTANCE TEST escort"
        assert "label" not in build_event_payload(plan_doc)["metadata"]

    def test_create_movement_keeps_the_title(self, api):  # noqa: F811
        resp = api.client.post("/movements", json={
            "origin": [72.55, 23.02], "destination": [72.59, 23.02],
            "planned_departure": "2026-10-23T09:00:00+05:30", "title": "ACCEPTANCE TEST escort"})
        assert resp.json()["label"] == "ACCEPTANCE TEST escort"

        async def label():
            return (await api.app.state.context_store.get(resp.json()["movement_id"])).label

        assert api.call(label) == "ACCEPTANCE TEST escort"

    def test_eta_prices_each_segment_at_its_own_speed(self):
        lines = {1: [(72.55, 23.02), (72.56, 23.02)], 2: [(72.56, 23.02), (72.57, 23.02)]}
        geo = RouteGeometry([1, 2], lines, speed_limits={1: 40.0, 2: 100.0})
        half = geo.ranges[0][2] / 2
        expected = half / (40 / 3.6) + (geo.ranges[1][2] - geo.ranges[1][1]) / (100 / 3.6)
        assert geo.eta_sec(half, lambda limit: limit or 40.0) == pytest.approx(expected)
        # the emulated walker never exceeds its cruise
        capped = geo.eta_sec(half, lambda limit: min(limit or 40.0, 80.0))
        assert capped > geo.eta_sec(half, lambda limit: limit or 40.0)

    def test_the_default_emulated_cruise_matches_a_convoy_at_the_limit(self):
        from convoy_brain import config

        assert config.ESCORT_SIM_SPEED_KMH == 80.0
        assert config.escort_sim_speed_kmh({}) == 80.0
