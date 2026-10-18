"""Tactical agent tests — Gemma path, heuristic floor, adaptive threading."""

from __future__ import annotations

import json

from convoy_brain.adaptive_params import AdaptiveParams
from convoy_brain.agents.diversion_coordinator import DiversionCoordinatorAgent
from convoy_brain.agents.route_planner import RoutePlannerAgent
from convoy_brain.agents.security_liaison import SecurityLiaisonAgent
from convoy_brain.agents.traffic_analyst import TrafficAnalystAgent
from convoy_brain.llm.heuristics import (
    heuristic_diversion_directives,
    heuristic_route_ranking,
    heuristic_traffic_assessment,
)
from convoy_brain.llm.llama_bridge import LlamaUnavailable
from conftest import FakeContextStore, FakeExecutor


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


class TestHeuristics:
    def test_traffic_assessment_empty(self):
        result = heuristic_traffic_assessment(None)
        assert result["overall_status"] == "red"
        assert result["confidence"] == "low"

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
            "primary_route": {"route_id": "r-best", "segment_ids": [4, 5], "score": 0.9, "reason": "top"},
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
            "query_segments_in_bbox": lambda args: (
                {"segments": [{"segment_id": 100}]}
                if args["min_lon"] < -1 else {"segments": [{"segment_id": 200}]}
            ),
            "find_convoy_routes": {"routes": routes},
            "predict_traffic_flow": {"forecasts": []},
        })
        agent = RoutePlannerAgent(bridge, executor, adaptive=AdaptiveParams())
        result = await agent.analyze_routes("m1", (72.55, 23.02), (72.6, 23.05), "Z")
        assert result["primary_route"]["route_id"] == "r-best"
        assert result["_tier"] == "gemma"
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
