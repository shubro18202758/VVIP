"""Tactical agent tests — Gemma path, heuristic floor, adaptive threading."""

from __future__ import annotations

import json

from convoy_brain.adaptive_params import AdaptiveParams
from convoy_brain.agents.diversion_coordinator import DiversionCoordinatorAgent
from convoy_brain.agents.route_planner import RoutePlannerAgent
from convoy_brain.agents.security_liaison import SecurityLiaisonAgent
from convoy_brain.agents.traffic_analyst import TrafficAnalystAgent
from convoy_brain.llm.heuristics import (
    dedupe_route_candidates,
    derive_route_scores,
    heuristic_diversion_directives,
    heuristic_final_answer,
    heuristic_route_ranking,
    heuristic_traffic_assessment,
    summarize_tool_result,
)
from convoy_brain.llm.llama_bridge import LlamaUnavailable
from conftest import (
    DESTINATION,
    NETWORK,
    ORIGIN,
    FakeContextStore,
    FakeExecutor,
    bbox_handler,
    details_handler,
)


class StubBridge:
    """Returns scripted completions, or raises LlamaUnavailable when failing."""

    def __init__(self, replies: list[str] | None = None, fail: bool = False):
        self.replies = list(replies or [])
        self.fail = fail
        self.calls: list[str] = []

    async def generate(self, system_prompt, user_prompt, json_mode=True,
                       suppress_thinking=True, **kwargs):
        self.calls.append(user_prompt)
        if self.fail:
            raise LlamaUnavailable("tactical tier down")
        if self.replies:
            return self.replies.pop(0)
        return "{}"


LIVE = {
    "segments": [
        {"segment_id": 1, "congestion_idx": 0.2, "speed_kmh": 55},
        {"segment_id": 2, "congestion_idx": 0.85, "speed_kmh": 12},
        {"segment_id": 3, "congestion_idx": 0.6, "speed_kmh": 30},
    ]
}


class TestTrafficAnalyst:
    async def test_gemma_path(self):
        bridge = StubBridge(replies=[json.dumps({
            "overall_status": "amber",
            "congestion_summary": {"avg_congestion_idx": 0.55, "peak_congestion_idx": 0.85},
            "risk_segments": [{"segment_id": 2, "issue": "spike", "severity": "high"}],
            "trend": "stable", "recommendation": "watch segment 2",
            "confidence": "medium",
        })])
        executor = FakeExecutor(routes={
            "query_segments_in_bbox": {"segments": [{"segment_id": 1}, {"segment_id": 2}]},
            "get_live_traffic": LIVE,
            "predict_traffic_flow": {"forecasts": []},
        })
        agent = TrafficAnalystAgent(bridge, executor)
        result = await agent.assess_corridor((72.5, 23.0, 72.6, 23.1))
        assert result["overall_status"] == "amber"
        assert result["_tier"] == "gemma"
        assert result["risk_segments"][0]["segment_id"] == 2

    async def test_heuristic_floor_when_model_down(self):
        bridge = StubBridge(fail=True)
        executor = FakeExecutor(routes={
            "query_segments_in_bbox": {"segments": [{"segment_id": 1}, {"segment_id": 2}]},
            "get_live_traffic": LIVE,
            "predict_traffic_flow": {"forecasts": []},
        })
        agent = TrafficAnalystAgent(bridge, executor)
        result = await agent.assess_corridor((0, 0, 1, 1))
        assert result["_tier"] == "heuristic"
        assert result["overall_status"] == "amber"  # avg 0.55
        assert result["congestion_summary"]["peak_congestion_idx"] == 0.85
        assert result["risk_segments"][0]["segment_id"] == 2


class TestTrafficAnalystSilentFeed:
    async def test_zero_observations_skips_the_tactical_call(self):
        bridge = StubBridge(replies=["{}"])
        executor = FakeExecutor(routes={
            "query_segments_in_bbox": {"segments": [{"segment_id": 1}, {"segment_id": 2}]},
            "get_live_traffic": {"observations": []},
        })
        result = await TrafficAnalystAgent(bridge, executor).assess_corridor((0, 0, 1, 1))
        assert bridge.calls == []  # nothing to narrate -> no Gemma call
        assert result["overall_status"] == "unknown"
        assert result["_tier"] == "deterministic"
        assert result["segments"] == 2
        assert [name for name, _ in executor.calls] == [
            "query_segments_in_bbox", "get_live_traffic",
        ]  # no forecast for an empty feed either

    async def test_tool_failure_degrades_instead_of_raising(self):
        from convoy_brain.mcp.server import ToolExecutionError

        executor = FakeExecutor(routes={
            "query_segments_in_bbox": {"segments": [{"segment_id": 1}]},
            "get_live_traffic": ToolExecutionError("oracle HTTP 500"),
        })
        result = await TrafficAnalystAgent(StubBridge(), executor).assess_corridor((0, 0, 1, 1))
        assert result["overall_status"] == "unknown"
        assert result["degraded"] is True
        assert "live traffic lookup failed" in result["recommendation"]


class TestHeuristics:
    def test_traffic_assessment_empty_is_unknown_not_red(self):
        # A silent feed is unverified, not congested (and never an all-clear).
        result = heuristic_traffic_assessment(None)
        assert result["overall_status"] == "unknown"
        assert result["confidence"] == "low"
        assert result["congestion_summary"]["avg_congestion_idx"] is None
        assert result["observations"] == 0
        assert heuristic_traffic_assessment({"observations": []})["overall_status"] == "unknown"

    def test_reads_the_oracle_live_shape(self):
        live = {"observations": [
            {"segment_id": 1012, "congestion_idx": 0.85, "speed_kmh": 9.0,
             "timestamp_ms": 1, "lon": 72.58, "lat": 23.03, "source": "fleet_gps"},
            {"segment_id": 1015, "congestion_idx": 0.45, "speed_kmh": 30.0},
        ]}
        result = heuristic_traffic_assessment(live)
        assert result["observations"] == 2
        assert result["overall_status"] == "amber"  # peak 0.85
        assert result["risk_segments"][0]["segment_id"] == 1012

    def test_trend_reads_the_oracle_prediction_shape(self):
        live = {"observations": [{"segment_id": 1, "congestion_idx": 0.3, "speed_kmh": 40}]}
        forecast = {"predictions": {"1": {
            "5": {"speed_kmh": 35.0, "congestion_idx": 0.35},
            "30": {"speed_kmh": 20.0, "congestion_idx": 0.7},
        }}}
        result = heuristic_traffic_assessment(live, forecast)
        assert result["trend"] == "deteriorating"  # furthest horizon 0.7 vs 0.3 now

    def test_traffic_assessment_trend_from_forecast(self):
        forecast = {"forecasts": [{"segment_id": 1, "congestion_idx": 0.9}]}
        result = heuristic_traffic_assessment(LIVE, forecast)
        assert result["trend"] == "deteriorating"

    def test_route_ranking_uses_scoring_weights(self):
        candidates = [
            {"route_id": "slow_safe", "scores": {"time": 0.9, "disruption": 0.1, "security": 0.9, "complexity": 0.2}},
            {"route_id": "fast_dirty", "scores": {"time": 0.2, "disruption": 0.9, "security": 0.4, "complexity": 0.8}},
        ]
        ranked = heuristic_route_ranking(candidates, {
            "time": 0.3, "disruption": 0.4, "security": 0.2, "complexity": 0.1,
        })
        # disruption weight dominates -> slow_safe (low disruption) wins
        assert ranked[0]["route_id"] == "slow_safe"
        assert ranked[0]["weighted_score"] > ranked[1]["weighted_score"]

    def test_route_ranking_weights_normalize(self):
        candidates = [{"route_id": "x", "scores": {}}]
        ranked = heuristic_route_ranking(candidates, {"time": 100})
        assert ranked[0]["weighted_score"] > 0

    def test_scores_are_derived_from_solver_metrics(self):
        # /optimize/routes never emits `scores`: derive them so the C4
        # weights act (previously every component read a neutral 0.5).
        solver = [
            {"route_id": "fast", "segment_ids": [1, 2, 3], "estimated_time_sec": 300,
             "disruption_score": 70.0, "security_score": 85.0},
            {"route_id": "quiet", "segment_ids": [1, 4, 5, 6], "estimated_time_sec": 600,
             "disruption_score": 10.0, "security_score": 70.0},
        ]
        derived = {r["route_id"]: r for r in derive_route_scores(solver)}
        assert derived["fast"]["scores"] == {
            "time": 1.0, "disruption": 0.7, "security": 0.85, "complexity": 0.0,
        }
        assert derived["quiet"]["scores"]["time"] == 0.5
        assert derived["quiet"]["scores"]["complexity"] == 0.25
        assert derived["quiet"]["scores_source"] == "derived"
        by_time = heuristic_route_ranking(solver, {"time": 1.0})
        by_disruption = heuristic_route_ranking(solver, {"disruption": 1.0})
        assert by_time[0]["route_id"] == "fast"
        assert by_disruption[0]["route_id"] == "quiet"

    def test_identical_segment_lists_are_deduplicated(self):
        rows = [
            {"route_id": "mip_1", "segment_ids": [1, 2, 3]},
            {"route_id": "ksp_1", "segment_ids": [1, 2, 3]},
            {"route_id": "ksp_2", "segment_ids": [1, 4, 3]},
        ]
        unique = dedupe_route_candidates(rows)
        assert [r["route_id"] for r in unique] == ["mip_1", "ksp_2"]
        assert unique[0]["duplicate_route_ids"] == ["ksp_1"]
        assert "duplicate_route_ids" not in rows[0]  # inputs untouched

    def test_tool_summaries_say_empty_plainly(self):
        empty = summarize_tool_result("get_live_traffic", {"segment_ids": [1012]},
                                      {"observations": []})
        assert empty.startswith("get_live_traffic: no live observations for segments 1012")
        failed = summarize_tool_result("predict_eta", {}, {"error": True, "message": "422"})
        assert failed == "predict_eta failed: 422"
        routes = summarize_tool_result("find_convoy_routes", {}, {"routes": [
            {"route_id": "r", "segment_ids": [1, 2], "total_distance_m": 2500,
             "estimated_time_sec": 240}]})
        assert "2.5 km" in routes and "ETA 4 min" in routes

    def test_final_answer_floor_is_honest_about_which_tier_failed(self):
        answer = heuristic_final_answer(
            "q", ["get_live_traffic"], error="timeout",
            tool_summaries=["get_live_traffic: 2 observation(s)"],
        )
        assert "Both model tiers" not in answer["reasoning"]
        assert "tactical tier could not compose" in answer["reasoning"]
        assert "2 observation(s)" in answer["reasoning"]
        assert answer["data"]["tier"] == "heuristic"

    def test_diversion_directives_from_plan(self):
        plan = {"diversions": [
            {"segment_id": 7, "advance_sec": 90, "closure_type": "partial", "estimated_queue_m": 800},
            {"segment_id": 8, "advance_sec": 60, "closure_type": "partial", "estimated_queue_m": 100},
        ]}
        result = heuristic_diversion_directives(plan, advance_closure_sec=120)
        assert len(result["directives"]) == 2
        assert result["directives"][0]["timing_sec"] == 90
        assert result["queue_alerts"] == [{"segment_id": 7, "estimated_queue_m": 800.0, "action": "staff junction for queue discharge"}]


class TestRoutePlanner:
    async def test_ranking_applied_and_primary_returned(self):
        gemma_reply = json.dumps({
            "primary_route": {"route_id": "r-best", "segment_ids": [4, 5], "score": 0.9,
                              "reason": "top: ETA 11 min on 3 segments"},
            "alternate_routes": [{"route_id": "r-alt", "segment_ids": [6], "score": 0.7, "reason": "backup"}],
            "rejected_routes": [],
            "overall_reasoning": "disruption-weighted",
            "confidence": "high",
        })
        bridge = StubBridge(replies=[gemma_reply])
        routes = [
            {"route_id": "r-best", "segment_ids": [4, 5],
             "scores": {"time": 0.3, "disruption": 0.1, "security": 0.9, "complexity": 0.2}},
            {"route_id": "r-alt", "segment_ids": [6],
             "scores": {"time": 0.2, "disruption": 0.8, "security": 0.4, "complexity": 0.7}},
        ]
        executor = FakeExecutor(routes={
            "query_segments_in_bbox": bbox_handler(NETWORK),
            "query_segment_details": details_handler(NETWORK),
            "find_convoy_routes": {"routes": [
                dict(routes[0], segment_ids=[1001, 1002, 1004], estimated_time_sec=640),
                dict(routes[1], segment_ids=[1005, 1001, 1002, 1004]),
            ]},
            "predict_traffic_flow": {"predictions": {}},
        })
        agent = RoutePlannerAgent(bridge, executor, adaptive=AdaptiveParams())
        result = await agent.analyze_routes("m1", ORIGIN, DESTINATION, "Z")
        assert result["primary_route"]["route_id"] == "r-best"
        assert result["_tier"] == "gemma"
        # solver metrics survive the narration merge (ETA 0 min regression)
        assert result["primary_route"]["estimated_time_sec"] == 640
        assert result["primary_route"]["reason"] == "top: ETA 11 min on 3 segments"
        # pre-ranked candidates were computed brain-side with the weights
        assert result["_ranked_candidates"][0]["route_id"] == "r-best"
        # the adaptive weights were included in the model prompt
        assert "scoring_weights" in bridge.calls[0]

    async def test_no_candidates(self):
        bridge = StubBridge()
        executor = FakeExecutor(routes={
            "query_segments_in_bbox": {"segments": [{"segment_id": 1}]},
            "find_convoy_routes": {"routes": []},
            "predict_traffic_flow": {"forecasts": []},
        })
        agent = RoutePlannerAgent(bridge, executor)
        result = await agent.analyze_routes("m1", (0.0, 0.0), (0.0, 0.0), "Z")
        assert result["primary_route"] is None
        assert result["confidence"] == "low"


class TestSecurityLiaison:
    async def test_hard_lane_violation_is_authoritative(self):
        gemma_reply = json.dumps({
            "compliant": True,  # model tries to waive; hard check must override
            "violations": [],
            "warnings": [],
            "recommendations": [],
            "security_score": 0.9,
            "confidence": "high",
        })
        bridge = StubBridge(replies=[gemma_reply])
        executor = FakeExecutor(routes={
            "query_segment_details": lambda args: {"segment_id": args["segment_id"], "lanes": 2},
        })
        agent = SecurityLiaisonAgent(bridge, executor)
        result = await agent.validate_route([101, 102], "Z+")  # Z+ needs 6 lanes
        assert result["compliant"] is False
        assert len(result["violations"]) == 2
        assert all(v["severity"] == "critical" for v in result["violations"])

    async def test_heuristic_floor(self):
        bridge = StubBridge(fail=True)
        executor = FakeExecutor(routes={
            "query_segment_details": {"segment_id": 1, "lanes": 8},
        })
        agent = SecurityLiaisonAgent(bridge, executor)
        result = await agent.validate_route([1], "Z+")
        assert result["_tier"] == "heuristic"
        assert result["compliant"] is True

    async def test_model_raised_violation_is_advisory(self):
        # The segment data cannot support a "market area" claim: it rides as
        # a labelled warning and does not flip a compliant verdict.
        bridge = StubBridge(replies=[json.dumps({
            "compliant": False,
            "violations": [{"segment_id": 1001, "rule": "MARKET_AREA",
                            "detail": "busy market", "severity": "critical"}],
            "warnings": [], "recommendations": ["stage barricades"],
            "security_score": 72, "confidence": "medium",
        })])
        executor = FakeExecutor(routes={"query_segment_details": details_handler(NETWORK)})
        result = await SecurityLiaisonAgent(bridge, executor).validate_route([1001, 1002], "Z")
        assert result["compliant"] is True
        assert result["violations"] == []
        assert any("model-raised (unverified): MARKET_AREA" in w["concern"]
                   for w in result["warnings"])
        assert result["recommendations"] == ["stage barricades"]
        assert result["model_security_score"] == 0.72  # 0-100 normalized, audit only
        assert 0.0 < result["security_score"] <= 1.0   # grounded score

    async def test_grounded_score_and_one_way_warning(self):
        network = dict(NETWORK)
        network[1002] = dict(NETWORK[1002], oneway=True)
        executor = FakeExecutor(routes={"query_segment_details": details_handler(network)})
        agent = SecurityLiaisonAgent(StubBridge(fail=True), executor)
        compliant = await agent.audit_route([1001, 1002], "Z")
        narrow = await agent.audit_route([1001, 1003], "Z+")
        assert compliant["compliant"] is True
        assert any(w["segment_id"] == 1002 and "one-way" in w["concern"]
                   for w in compliant["warnings"])
        assert narrow["compliant"] is False
        assert compliant["security_score"] > narrow["security_score"]

    async def test_unfetchable_segment_is_unverified_not_compliant(self):
        executor = FakeExecutor(routes={"query_segment_details": details_handler(NETWORK)})
        result = await SecurityLiaisonAgent(StubBridge(fail=True), executor).audit_route(
            [1001, 9999], "Z")
        assert result["compliant"] is False
        assert [v["rule"] for v in result["violations"]] == ["UNVERIFIED"]


class TestDiversionCoordinator:
    async def test_advance_closure_threaded_and_floored(self):
        bridge = StubBridge(fail=True)  # force heuristic path to inspect the MCP call
        captured: dict = {}

        def plan(args):
            captured.update(args)
            return {"diversions": [{"segment_id": 9, "advance_sec": 45,
                                    "closure_type": "full", "estimated_queue_m": 300}]}

        executor = FakeExecutor(routes={
            "plan_diversions": plan,
            "get_live_traffic": LIVE,
        })
        store = FakeContextStore()
        from convoy_brain.memory.convoy_context import ConvoyContext
        await store.put(ConvoyContext(movement_id="m1", vvip_class="Z+", status="active"))

        agent = DiversionCoordinatorAgent(
            bridge, executor, store,
            adaptive=AdaptiveParams(advance_closure_sec=90),
        )
        result = await agent.activate_diversions("m1", [9])
        # 90 < 180 floor for Z+ -> floored to 180 and threaded into the call
        assert captured["advance_closure_sec"] == 180
        assert captured["route_segment_ids"] == [9]
        assert result["_tier"] == "heuristic"
        assert result["directives"][0]["segment_id"] == 9

    async def test_x_class_gets_zero_closure(self):
        bridge = StubBridge(fail=True)
        captured: dict = {}

        def plan(args):
            captured.update(args)
            return {"diversions": []}

        executor = FakeExecutor(routes={"plan_diversions": plan, "get_live_traffic": LIVE})
        store = FakeContextStore()
        agent = DiversionCoordinatorAgent(bridge, executor, store, adaptive=AdaptiveParams())
        await agent.activate_diversions("m2", [5], vvip_class="X")
        assert captured["advance_closure_sec"] == 0

    async def test_deactivate_passed_segments(self):
        from convoy_brain.memory.convoy_context import ConvoyContext

        bridge = StubBridge()
        executor = FakeExecutor()
        store = FakeContextStore()
        ctx = ConvoyContext(movement_id="m3", vvip_class="Z")
        ctx.active_diversions = ["1", "2", "3"]
        await store.put(ctx)

        agent = DiversionCoordinatorAgent(bridge, executor, store)
        result = await agent.deactivate_passed_segments("m3", [1, 3])
        assert sorted(result["deactivated"]) == ["1", "3"]
        assert store.contexts["m3"].active_diversions == ["2"]
