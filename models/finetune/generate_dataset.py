"""Dataset Generator — Synthetic training data for VVIP Convoy Orchestration Agent.

Produces three dataset files for the sequential fine-tuning pipeline:
1. CPT corpus  — Domain text for continuous pre-training (raw text chunks)
2. SFT dataset — ChatML conversations with tool calls for supervised fine-tuning
3. GRPO prompts — Challenge prompts with verifiable answers for RL training

Data Format:
  - CPT: {"text": "..."} per line (JSONL)
  - SFT: {"conversations": [{"role":"system","content":"..."},{"role":"user",...},...]} (JSONL)
  - GRPO: {"prompt": "...", "ground_truth": {...}, "reward_metadata": {...}} (JSONL)

Usage:
  python models/finetune/generate_dataset.py --output-dir models/finetune/data --seed 42
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import click

# ─────────────────────────────────────────────────────────────────────────────
# Delhi NCR Road Network Seed Data
# ─────────────────────────────────────────────────────────────────────────────

DELHI_SEGMENTS = [
    {"id": 1001, "name": "Rajpath (Kartavya Path)", "class": "trunk", "lanes": 6,
     "speed_limit": 50, "oneway": False, "length_m": 2100},
    {"id": 1002, "name": "Ring Road (Mahatma Gandhi Marg)", "class": "primary", "lanes": 6,
     "speed_limit": 60, "oneway": False, "length_m": 3400},
    {"id": 1003, "name": "NH-48 (Delhi-Gurgaon Expressway)", "class": "motorway", "lanes": 8,
     "speed_limit": 100, "oneway": True, "length_m": 5200},
    {"id": 1004, "name": "Mathura Road", "class": "primary", "lanes": 4,
     "speed_limit": 60, "oneway": False, "length_m": 4100},
    {"id": 1005, "name": "Aurobindo Marg", "class": "secondary", "lanes": 4,
     "speed_limit": 50, "oneway": False, "length_m": 2800},
    {"id": 1006, "name": "Sardar Patel Marg", "class": "trunk", "lanes": 6,
     "speed_limit": 60, "oneway": False, "length_m": 3200},
    {"id": 1007, "name": "Lodhi Road", "class": "secondary", "lanes": 4,
     "speed_limit": 40, "oneway": False, "length_m": 2400},
    {"id": 1008, "name": "Outer Ring Road", "class": "primary", "lanes": 6,
     "speed_limit": 60, "oneway": False, "length_m": 6100},
    {"id": 1009, "name": "Vikas Marg", "class": "secondary", "lanes": 4,
     "speed_limit": 50, "oneway": False, "length_m": 3700},
    {"id": 1010, "name": "GT Karnal Road (NH-1)", "class": "trunk", "lanes": 6,
     "speed_limit": 80, "oneway": True, "length_m": 4500},
    {"id": 1011, "name": "Mehrauli-Badarpur Road", "class": "secondary", "lanes": 4,
     "speed_limit": 40, "oneway": False, "length_m": 3100},
    {"id": 1012, "name": "Nelson Mandela Marg", "class": "primary", "lanes": 6,
     "speed_limit": 60, "oneway": False, "length_m": 2900},
    {"id": 1013, "name": "Africa Avenue", "class": "secondary", "lanes": 4,
     "speed_limit": 50, "oneway": False, "length_m": 1800},
    {"id": 1014, "name": "Bahadur Shah Zafar Marg", "class": "secondary", "lanes": 4,
     "speed_limit": 40, "oneway": False, "length_m": 2200},
    {"id": 1015, "name": "DND Flyway", "class": "motorway", "lanes": 8,
     "speed_limit": 80, "oneway": True, "length_m": 9200},
    {"id": 1016, "name": "Shanti Path", "class": "trunk", "lanes": 6,
     "speed_limit": 50, "oneway": False, "length_m": 2600},
    {"id": 1017, "name": "Janpath", "class": "secondary", "lanes": 4,
     "speed_limit": 40, "oneway": True, "length_m": 1500},
    {"id": 1018, "name": "Ashoka Road", "class": "secondary", "lanes": 4,
     "speed_limit": 40, "oneway": False, "length_m": 1200},
    {"id": 1019, "name": "Race Course Road (Lok Kalyan Marg)", "class": "trunk",
     "lanes": 6, "speed_limit": 40, "oneway": False, "length_m": 1800},
    {"id": 1020, "name": "Akbar Road", "class": "secondary", "lanes": 4,
     "speed_limit": 40, "oneway": False, "length_m": 1600},
]

# Adjacency pairs (bidirectional unless oneway)
ADJACENCY = [
    (1001, 1017), (1001, 1018), (1001, 1016), (1002, 1004), (1002, 1008),
    (1003, 1008), (1004, 1007), (1004, 1011), (1005, 1013), (1005, 1007),
    (1006, 1016), (1006, 1012), (1007, 1013), (1008, 1009), (1008, 1010),
    (1009, 1014), (1010, 1002), (1011, 1015), (1012, 1013), (1016, 1019),
    (1017, 1018), (1018, 1020), (1019, 1020), (1001, 1019), (1006, 1001),
]

VVIP_CLASSES = {
    "Z+": {"min_lanes": 6, "closure": "full", "advance_sec": 180,
            "desc": "PM, President, visiting Heads of State"},
    "Z":  {"min_lanes": 4, "closure": "partial", "advance_sec": 120,
            "desc": "Cabinet Ministers, Senior Judiciary"},
    "Y":  {"min_lanes": 2, "closure": "speed_restriction", "advance_sec": 60,
            "desc": "State Ministers, Service Chiefs"},
    "X":  {"min_lanes": 1, "closure": "signal_priority", "advance_sec": 0,
            "desc": "Standard VIP — signal priority only"},
}

SECURITY_SCORES = {
    "motorway": 100, "trunk": 85, "primary": 70,
    "secondary": 50, "tertiary": 30, "residential": 10,
}

MCP_TOOLS = [
    "predict_traffic_flow", "find_convoy_routes", "plan_diversions",
    "evaluate_scenarios", "predict_eta", "query_shortest_path",
    "query_k_shortest_paths", "query_segments_in_bbox",
    "query_segment_details", "get_live_traffic", "get_historical_pattern",
]

SYSTEM_PROMPT = (
    "You are the VVIP Convoy Orchestration Agent. You coordinate convoy "
    "movements across Indian urban corridors using MCP tools for prediction, "
    "optimization, spatial queries, and real-time data. Always call tools "
    "before making recommendations. Respond only with valid JSON."
)


# ─────────────────────────────────────────────────────────────────────────────
# Utility Functions
# ─────────────────────────────────────────────────────────────────────────────

def _seg(sid: int) -> dict:
    """Look up segment by ID."""
    for s in DELHI_SEGMENTS:
        if s["id"] == sid:
            return s
    return DELHI_SEGMENTS[0]


def _rand_speed(seg: dict, congestion: float) -> float:
    """Generate realistic speed given congestion index [0,1]."""
    free_flow = seg["speed_limit"]
    return round(free_flow * (1.0 - 0.7 * congestion) + random.gauss(0, 3), 1)


def _rand_congestion() -> float:
    return round(random.betavariate(2, 5), 3)  # Skewed toward low congestion


def _rand_hour() -> int:
    # Peak hours more likely
    weights = [1]*6 + [3]*3 + [5]*3 + [2]*4 + [4]*3 + [5]*2 + [2]*3
    return random.choices(range(24), weights=weights[:24])[0]


def _route_between(origin: int, dest: int) -> list[int]:
    """BFS shortest path on adjacency graph."""
    adj: dict[int, list[int]] = {}
    for a, b in ADJACENCY:
        adj.setdefault(a, []).append(b)
        seg_a = _seg(a)
        if not seg_a.get("oneway", False):
            adj.setdefault(b, []).append(a)
    visited = {origin}
    queue = [[origin]]
    while queue:
        path = queue.pop(0)
        node = path[-1]
        if node == dest:
            return path
        for nxt in adj.get(node, []):
            if nxt not in visited:
                visited.add(nxt)
                queue.append(path + [nxt])
    return [origin, dest]  # Fallback direct


def _compute_eta_sec(route: list[int], speeds: dict[int, float]) -> float:
    """Estimate travel time along route."""
    total = 0.0
    for sid in route:
        seg = _seg(sid)
        speed = speeds.get(sid, seg["speed_limit"] * 0.5)
        if speed < 1:
            speed = 5.0
        total += seg["length_m"] / (speed * 1000 / 3600)  # seconds
    return round(total, 1)


# ─────────────────────────────────────────────────────────────────────────────
# CPT Corpus Generator
# ─────────────────────────────────────────────────────────────────────────────

def generate_cpt_corpus(rng: random.Random) -> list[dict]:
    """Generate domain-knowledge text chunks for continuous pre-training."""
    corpus: list[dict] = []

    # Road network knowledge
    for seg in DELHI_SEGMENTS:
        corpus.append({"text": (
            f"Road Segment {seg['id']}: {seg['name']} is a {seg['class']}-class road "
            f"in Delhi NCR with {seg['lanes']} lanes, a speed limit of {seg['speed_limit']} km/h, "
            f"{'one-way' if seg['oneway'] else 'two-way'} traffic flow, spanning {seg['length_m']} meters. "
            f"Security score for {seg['class']} roads: {SECURITY_SCORES[seg['class']]}/100."
        )})

    # VVIP protocol knowledge
    for cls, info in VVIP_CLASSES.items():
        corpus.append({"text": (
            f"VVIP Classification {cls}: Protectees include {info['desc']}. "
            f"Minimum road requirement: {info['min_lanes']} lanes. "
            f"Closure type: {info['closure']}. Advance closure: {info['advance_sec']} seconds. "
            f"{'Counter-assault teams required at every junction.' if cls == 'Z+' else ''}"
        )})

    # Traffic pattern knowledge
    for _ in range(50):
        seg = rng.choice(DELHI_SEGMENTS)
        hour = _rand_hour()
        cong = _rand_congestion()
        speed = _rand_speed(seg, cong)
        period = "morning rush" if 7 <= hour <= 10 else (
            "evening rush" if 17 <= hour <= 20 else (
                "night" if hour >= 22 or hour <= 5 else "midday"))
        corpus.append({"text": (
            f"Traffic pattern for {seg['name']} (segment {seg['id']}) during {period} "
            f"at {hour:02d}:00 — observed speed: {speed} km/h, congestion index: {cong:.3f}. "
            f"Free-flow speed for this {seg['class']} road is {seg['speed_limit']} km/h. "
            f"Speed ratio: {speed/seg['speed_limit']:.2f}."
        )})

    # Spatial relationship knowledge
    for a, b in ADJACENCY:
        seg_a, seg_b = _seg(a), _seg(b)
        corpus.append({"text": (
            f"Road segment {a} ({seg_a['name']}) connects to segment {b} ({seg_b['name']}). "
            f"Transition: {seg_a['class']}({seg_a['lanes']}L) → {seg_b['class']}({seg_b['lanes']}L). "
            f"{'Width reduction — potential convoy bottleneck.' if seg_b['lanes'] < seg_a['lanes'] else ''}"
        )})

    # MCP tool documentation
    tool_docs = {
        "predict_traffic_flow": (
            "The predict_traffic_flow tool forecasts speed and congestion for road segments "
            "at future horizons (T+5, T+10, T+15, T+30 minutes) using the DSTGAT spatio-temporal "
            "graph attention network. Input: segment_ids (array of integers), horizons_min "
            "(optional array, default [5,10,15,30]). Output: per-segment speed_kmh and "
            "congestion_idx predictions at each horizon."
        ),
        "find_convoy_routes": (
            "The find_convoy_routes tool uses OR-Tools CP-SAT MIP solver to find optimal "
            "convoy routes balancing transit time (weight 0.3), public disruption (0.4), "
            "security score (0.2), and route complexity (0.1). Input: origin_segment, "
            "destination_segment, optional max_candidates (default 5), optional avoid_segments."
        ),
        "plan_diversions": (
            "The plan_diversions tool generates per-segment closure plans with activation and "
            "deactivation timing, alternative routes for diverted traffic, estimated queue lengths, "
            "and closure types (full_closure for queue>500m, partial_closure for >100m, "
            "speed_restriction for ≤100m). Input: route_segment_ids, optional convoy_speed_kmh "
            "(default 60), advance_closure_sec (default 120), departure_time."
        ),
        "evaluate_scenarios": (
            "The evaluate_scenarios tool compares multiple convoy movement scenarios using "
            "simulation. Evaluates total public disruption in vehicle-hours, peak queue lengths, "
            "closure durations, and complaint risk. Input: array of scenario objects with "
            "scenario_id, route_segment_ids, departure_time, convoy_speed_kmh."
        ),
    }
    for tool_name, doc in tool_docs.items():
        corpus.append({"text": doc})

    # Routing concept knowledge
    corpus.extend([
        {"text": (
            "pgRouting Dijkstra computes shortest path using cost = turn_cost_sec + "
            "segment_length / (speed_limit * 1000/3600). Reverse cost is -1 for one-way "
            "segments. The corridor.road_graph view provides the source/target/cost format "
            "required by pgr_dijkstra and pgr_KSP."
        )},
        {"text": (
            "Multi-objective convoy routing must balance transit time minimization against "
            "public disruption. Full road closures on high-traffic corridors during peak hours "
            "can generate 50+ vehicle-hours of delay per segment. Speed restrictions cause "
            "less disruption but provide lower security margins."
        )},
        {"text": (
            "The VVIP security scoring system assigns scores by road class: motorway=100, "
            "trunk=85, primary=70, secondary=50, tertiary=30, residential=10. Scores are "
            "weighted by lane count and connectivity. Z+ protectees require routes with "
            "minimum security score of 80 on all segments."
        )},
    ])

    return corpus


# ─────────────────────────────────────────────────────────────────────────────
# SFT Conversation Generator
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ConversationBuilder:
    """Builds multi-turn ChatML conversations with tool calls and results."""
    messages: list[dict] = field(default_factory=list)

    def system(self, content: str) -> "ConversationBuilder":
        self.messages.append({"role": "system", "content": content})
        return self

    def user(self, content: str) -> "ConversationBuilder":
        self.messages.append({"role": "user", "content": content})
        return self

    def assistant_tool_call(self, tool_calls: list[dict]) -> "ConversationBuilder":
        self.messages.append({
            "role": "assistant",
            "content": json.dumps({"tool_calls": tool_calls}, indent=None),
        })
        return self

    def tool_result(self, name: str, result: Any) -> "ConversationBuilder":
        self.messages.append({
            "role": "tool",
            "name": name,
            "content": json.dumps(result, indent=None),
        })
        return self

    def assistant_answer(self, answer: dict) -> "ConversationBuilder":
        self.messages.append({
            "role": "assistant",
            "content": json.dumps(answer, indent=None),
        })
        return self

    def build(self) -> dict:
        return {"conversations": self.messages}


def _gen_route_analysis_conv(rng: random.Random) -> dict:
    """Generate a route analysis conversation with multi-step tool use."""
    origin = rng.choice(DELHI_SEGMENTS[:10])
    dest = rng.choice(DELHI_SEGMENTS[10:])
    vvip_cls = rng.choice(["Z+", "Z", "Y"])
    vvip_info = VVIP_CLASSES[vvip_cls]
    hour = _rand_hour()
    route = _route_between(origin["id"], dest["id"])

    # Generate synthetic predictions for route segments
    predictions = {}
    for sid in route:
        seg = _seg(sid)
        cong = _rand_congestion()
        predictions[str(sid)] = {
            "speed_kmh": _rand_speed(seg, cong),
            "congestion_idx": cong,
        }

    speeds = {int(k): v["speed_kmh"] for k, v in predictions.items()}
    eta = _compute_eta_sec(route, speeds)

    conv = ConversationBuilder()
    conv.system(SYSTEM_PROMPT)
    conv.user(
        f"Plan a {vvip_cls} convoy route from {origin['name']} (segment {origin['id']}) "
        f"to {dest['name']} (segment {dest['id']}). Departure at {hour:02d}:00 today."
    )

    # Step 1: Predict traffic
    conv.assistant_tool_call([{
        "name": "predict_traffic_flow",
        "arguments": {"segment_ids": route, "horizons_min": [5, 10, 15, 30]},
    }])
    conv.tool_result("predict_traffic_flow", {
        "predictions": {str(sid): {
            "5min": predictions[str(sid)],
            "10min": predictions[str(sid)],
            "15min": predictions[str(sid)],
            "30min": predictions[str(sid)],
        } for sid in route},
    })

    # Step 2: Find routes
    conv.assistant_tool_call([{
        "name": "find_convoy_routes",
        "arguments": {
            "origin_segment": origin["id"],
            "destination_segment": dest["id"],
            "max_candidates": 3,
        },
    }])

    # Check lane compliance
    compliant = all(_seg(sid)["lanes"] >= vvip_info["min_lanes"] for sid in route)
    conv.tool_result("find_convoy_routes", {
        "routes": [{
            "route_id": "R1",
            "segment_ids": route,
            "total_distance_m": sum(_seg(s)["length_m"] for s in route),
            "estimated_time_sec": eta,
            "security_score": min(SECURITY_SCORES[_seg(s)["class"]] for s in route),
            "lane_compliant": compliant,
        }],
    })

    # Step 3: Plan diversions
    conv.assistant_tool_call([{
        "name": "plan_diversions",
        "arguments": {
            "route_segment_ids": route,
            "convoy_speed_kmh": 60,
            "advance_closure_sec": vvip_info["advance_sec"],
        },
    }])
    conv.tool_result("plan_diversions", {
        "diversions": [{
            "segment_id": sid,
            "closure_type": vvip_info["closure"],
            "activation_sec_before": vvip_info["advance_sec"],
            "estimated_queue_m": rng.randint(50, 400),
            "alternative_route": [rng.choice([s["id"] for s in DELHI_SEGMENTS if s["id"] != sid])],
        } for sid in route[:3]],
    })

    # Final answer
    avg_cong = sum(v["congestion_idx"] for v in predictions.values()) / len(predictions)
    confidence = "high" if avg_cong < 0.4 else ("medium" if avg_cong < 0.7 else "low")
    conv.assistant_answer({
        "action": "recommend_route",
        "reasoning": (
            f"Analyzed {len(route)} segments from {origin['name']} to {dest['name']} "
            f"for {vvip_cls} convoy. Traffic prediction shows average congestion {avg_cong:.3f}. "
            f"{'All segments meet ' + str(vvip_info['min_lanes']) + '-lane minimum.' if compliant else 'WARNING: lane width violation detected.'} "
            f"ETA: {eta:.0f}s ({eta/60:.1f} min). Diversions planned with {vvip_info['advance_sec']}s advance."
        ),
        "tool_calls_made": ["predict_traffic_flow", "find_convoy_routes", "plan_diversions"],
        "confidence": confidence,
        "data": {
            "recommended_route": route,
            "eta_seconds": eta,
            "security_score": min(SECURITY_SCORES[_seg(s)["class"]] for s in route),
            "lane_compliant": compliant,
        },
    })
    return conv.build()


def _gen_live_monitoring_conv(rng: random.Random) -> dict:
    """Generate a live traffic monitoring conversation."""
    segments = rng.sample([s["id"] for s in DELHI_SEGMENTS], k=rng.randint(3, 6))

    conv = ConversationBuilder()
    conv.system(SYSTEM_PROMPT)
    conv.user(
        f"Check current traffic conditions on segments {segments} and flag any anomalies."
    )

    # Step 1: Get live traffic
    conv.assistant_tool_call([{
        "name": "get_live_traffic",
        "arguments": {"segment_ids": segments},
    }])
    live_data = {}
    anomaly_segs = []
    for sid in segments:
        seg = _seg(sid)
        cong = _rand_congestion()
        speed = _rand_speed(seg, cong)
        if rng.random() < 0.2:
            speed = rng.uniform(1, 10)  # Anomalous
            cong = rng.uniform(0.8, 1.0)
            anomaly_segs.append(sid)
        live_data[str(sid)] = {
            "speed_kmh": round(speed, 1),
            "congestion_idx": round(cong, 3),
            "last_updated": "2025-01-15T10:30:00Z",
        }
    conv.tool_result("get_live_traffic", {"traffic": live_data})

    # Step 2: Get historical pattern for comparison
    conv.assistant_tool_call([{
        "name": "get_historical_pattern",
        "arguments": {"segment_id": segments[0], "day_of_week": 2, "hour": 10},
    }])
    hist_seg = _seg(segments[0])
    conv.tool_result("get_historical_pattern", {
        "segment_id": segments[0],
        "avg_speed_kmh": hist_seg["speed_limit"] * 0.65,
        "p50_speed_kmh": hist_seg["speed_limit"] * 0.6,
        "p95_speed_kmh": hist_seg["speed_limit"] * 0.3,
    })

    # Final answer
    conv.assistant_answer({
        "action": "traffic_status_report",
        "reasoning": (
            f"Analyzed live traffic on {len(segments)} segments. "
            f"{'Anomalies detected on segments ' + str(anomaly_segs) + ': speeds significantly below historical averages.' if anomaly_segs else 'All segments within normal traffic patterns.'} "
            f"Historical comparison for segment {segments[0]} shows current conditions are "
            f"{'degraded' if segments[0] in anomaly_segs else 'within normal range'}."
        ),
        "tool_calls_made": ["get_live_traffic", "get_historical_pattern"],
        "confidence": "high",
        "data": {
            "monitored_segments": segments,
            "anomaly_segments": anomaly_segs,
            "live_conditions": live_data,
        },
    })
    return conv.build()


def _gen_scenario_comparison_conv(rng: random.Random) -> dict:
    """Generate a scenario comparison conversation."""
    origin = rng.choice(DELHI_SEGMENTS[:5])
    dest = rng.choice(DELHI_SEGMENTS[15:])
    vvip_cls = rng.choice(["Z+", "Z"])

    conv = ConversationBuilder()
    conv.system(SYSTEM_PROMPT)
    conv.user(
        f"Compare departure at 08:00 vs 10:00 for a {vvip_cls} convoy from "
        f"{origin['name']} to {dest['name']}. Which causes less public disruption?"
    )

    route = _route_between(origin["id"], dest["id"])

    # Step 1: Evaluate scenarios
    conv.assistant_tool_call([{
        "name": "evaluate_scenarios",
        "arguments": {
            "scenarios": [
                {
                    "scenario_id": "morning_peak",
                    "route_segment_ids": route,
                    "departure_time": "2025-01-15T08:00:00+05:30",
                    "convoy_speed_kmh": 60,
                    "route_name": "via " + _seg(route[len(route)//2])["name"],
                },
                {
                    "scenario_id": "post_peak",
                    "route_segment_ids": route,
                    "departure_time": "2025-01-15T10:00:00+05:30",
                    "convoy_speed_kmh": 60,
                    "route_name": "via " + _seg(route[len(route)//2])["name"],
                },
            ],
        },
    }])

    disruption_morning = round(rng.uniform(30, 80), 1)
    disruption_post = round(rng.uniform(10, 35), 1)
    conv.tool_result("evaluate_scenarios", {
        "results": [
            {
                "scenario_id": "morning_peak",
                "total_disruption_vehicle_hours": disruption_morning,
                "max_queue_length_m": rng.randint(400, 1200),
                "avg_closure_duration_sec": rng.randint(180, 400),
                "risk_level": "high" if disruption_morning > 50 else "medium",
            },
            {
                "scenario_id": "post_peak",
                "total_disruption_vehicle_hours": disruption_post,
                "max_queue_length_m": rng.randint(100, 400),
                "avg_closure_duration_sec": rng.randint(100, 250),
                "risk_level": "medium" if disruption_post > 20 else "low",
            },
        ],
    })

    conv.assistant_answer({
        "action": "recommend_departure_time",
        "reasoning": (
            f"Compared 08:00 (morning peak) vs 10:00 (post-peak) departure for {vvip_cls} "
            f"convoy on {len(route)}-segment route. 08:00 departure causes {disruption_morning} "
            f"vehicle-hours of disruption vs {disruption_post} for 10:00 — a "
            f"{((disruption_morning - disruption_post) / disruption_morning * 100):.0f}% reduction. "
            f"Recommending 10:00 departure to minimize public impact."
        ),
        "tool_calls_made": ["evaluate_scenarios"],
        "confidence": "high",
        "data": {
            "recommended_scenario": "post_peak",
            "disruption_reduction_pct": round(
                (disruption_morning - disruption_post) / disruption_morning * 100, 1
            ),
        },
    })
    return conv.build()


def _gen_spatial_query_conv(rng: random.Random) -> dict:
    """Generate a spatial reasoning conversation."""
    seg = rng.choice(DELHI_SEGMENTS)
    vvip_cls = rng.choice(["Z+", "Z", "Y"])
    vvip_info = VVIP_CLASSES[vvip_cls]

    conv = ConversationBuilder()
    conv.system(SYSTEM_PROMPT)
    conv.user(
        f"Can a {vvip_cls} convoy use segment {seg['id']} ({seg['name']})? "
        f"Check road specifications."
    )

    conv.assistant_tool_call([{
        "name": "query_segment_details",
        "arguments": {"segment_id": seg["id"]},
    }])
    conv.tool_result("query_segment_details", {
        "segment_id": seg["id"],
        "road_name": seg["name"],
        "road_class": seg["class"],
        "lanes": seg["lanes"],
        "speed_limit_kmh": seg["speed_limit"],
        "oneway": seg["oneway"],
        "width_m": seg["lanes"] * 3.5,
        "security_score": SECURITY_SCORES[seg["class"]],
    })

    compliant = seg["lanes"] >= vvip_info["min_lanes"]
    conv.assistant_answer({
        "action": "segment_compliance_check",
        "reasoning": (
            f"Segment {seg['id']} ({seg['name']}): {seg['class']}-class, {seg['lanes']} lanes "
            f"({seg['lanes']*3.5}m width). {vvip_cls} requires minimum {vvip_info['min_lanes']} lanes. "
            f"{'COMPLIANT — segment meets requirements.' if compliant else 'NON-COMPLIANT — insufficient lane count. Must reroute.'} "
            f"Security score: {SECURITY_SCORES[seg['class']]}/100."
        ),
        "tool_calls_made": ["query_segment_details"],
        "confidence": "high",
        "data": {
            "segment_id": seg["id"],
            "compliant": compliant,
            "lanes": seg["lanes"],
            "required_lanes": vvip_info["min_lanes"],
            "security_score": SECURITY_SCORES[seg["class"]],
        },
    })
    return conv.build()


def _gen_bbox_query_conv(rng: random.Random) -> dict:
    """Generate a bounding box spatial query conversation."""
    # Delhi NCR approximate bounds
    center_lat = 28.6139 + rng.uniform(-0.03, 0.03)
    center_lon = 77.2090 + rng.uniform(-0.03, 0.03)
    delta = 0.02

    conv = ConversationBuilder()
    conv.system(SYSTEM_PROMPT)
    conv.user(
        f"What road segments are available near coordinates ({center_lat:.4f}, {center_lon:.4f})?"
    )

    conv.assistant_tool_call([{
        "name": "query_segments_in_bbox",
        "arguments": {
            "min_lat": round(center_lat - delta, 4),
            "min_lon": round(center_lon - delta, 4),
            "max_lat": round(center_lat + delta, 4),
            "max_lon": round(center_lon + delta, 4),
        },
    }])

    found_segs = rng.sample(DELHI_SEGMENTS, k=rng.randint(2, 5))
    conv.tool_result("query_segments_in_bbox", {
        "segments": [{
            "segment_id": s["id"],
            "road_name": s["name"],
            "road_class": s["class"],
            "lanes": s["lanes"],
        } for s in found_segs],
    })

    conv.assistant_answer({
        "action": "spatial_query_result",
        "reasoning": (
            f"Found {len(found_segs)} road segments in the queried bounding box near "
            f"({center_lat:.4f}, {center_lon:.4f}). "
            f"Highest-class road: {max(found_segs, key=lambda s: SECURITY_SCORES[s['class']])['name']}. "
            f"Available lane counts: {sorted(set(s['lanes'] for s in found_segs))}."
        ),
        "tool_calls_made": ["query_segments_in_bbox"],
        "confidence": "high",
        "data": {
            "segments_found": len(found_segs),
            "segment_ids": [s["id"] for s in found_segs],
        },
    })
    return conv.build()


def generate_sft_dataset(rng: random.Random, n_each: int = 40) -> list[dict]:
    """Generate supervised fine-tuning conversations."""
    dataset: list[dict] = []
    generators = [
        _gen_route_analysis_conv,
        _gen_live_monitoring_conv,
        _gen_scenario_comparison_conv,
        _gen_spatial_query_conv,
        _gen_bbox_query_conv,
    ]
    for gen in generators:
        for _ in range(n_each):
            dataset.append(gen(rng))
    rng.shuffle(dataset)
    return dataset


# ─────────────────────────────────────────────────────────────────────────────
# GRPO Prompt Generator
# ─────────────────────────────────────────────────────────────────────────────

def _grpo_route_planning_prompt(rng: random.Random) -> dict:
    """Route planning prompt with verifiable ground truth."""
    origin = rng.choice(DELHI_SEGMENTS[:8])
    dest = rng.choice(DELHI_SEGMENTS[12:])
    vvip_cls = rng.choice(["Z+", "Z", "Y", "X"])
    vvip_info = VVIP_CLASSES[vvip_cls]
    route = _route_between(origin["id"], dest["id"])

    # Ground truth: which segments are compliant
    compliant_segs = [s for s in route if _seg(s)["lanes"] >= vvip_info["min_lanes"]]
    non_compliant = [s for s in route if s not in compliant_segs]

    return {
        "prompt": (
            f"<|im_start|>system\n{SYSTEM_PROMPT}<|im_end|>\n"
            f"<|im_start|>user\nPlan a {vvip_cls} convoy movement from segment {origin['id']} "
            f"({origin['name']}) to segment {dest['id']} ({dest['name']}). "
            f"Verify road width compliance for all segments on the route. "
            f"Use appropriate tools before making any recommendation.<|im_end|>\n"
            f"<|im_start|>assistant\n"
        ),
        "ground_truth": {
            "required_tools": ["predict_traffic_flow", "find_convoy_routes",
                               "query_segment_details"],
            "route_segments": route,
            "compliant_segments": compliant_segs,
            "non_compliant_segments": non_compliant,
            "min_lanes_required": vvip_info["min_lanes"],
            "all_compliant": len(non_compliant) == 0,
        },
        "reward_metadata": {
            "type": "route_planning",
            "vvip_class": vvip_cls,
            "difficulty": "hard" if vvip_cls in ("Z+", "Z") else "medium",
        },
    }


def _grpo_anomaly_detection_prompt(rng: random.Random) -> dict:
    """Anomaly detection prompt requiring data-grounded reasoning."""
    segments = rng.sample([s["id"] for s in DELHI_SEGMENTS], k=rng.randint(4, 8))
    anomaly_idx = rng.randint(0, len(segments) - 1)
    anomaly_seg = segments[anomaly_idx]
    seg_info = _seg(anomaly_seg)

    return {
        "prompt": (
            f"<|im_start|>system\n{SYSTEM_PROMPT}<|im_end|>\n"
            f"<|im_start|>user\nMonitor segments {segments} for traffic anomalies. "
            f"Compare current speeds against historical patterns and flag any deviations. "
            f"Segment {anomaly_seg} ({seg_info['name']}) has been reporting unusually low "
            f"speeds (5 km/h in a {seg_info['speed_limit']} km/h zone). Investigate.<|im_end|>\n"
            f"<|im_start|>assistant\n"
        ),
        "ground_truth": {
            "required_tools": ["get_live_traffic", "get_historical_pattern"],
            "anomaly_segment": anomaly_seg,
            "anomaly_type": "speed_anomaly",
            "expected_speed_limit": seg_info["speed_limit"],
            "must_flag_anomaly": True,
        },
        "reward_metadata": {
            "type": "anomaly_detection",
            "difficulty": "medium",
        },
    }


def _grpo_scenario_eval_prompt(rng: random.Random) -> dict:
    """Scenario evaluation requiring quantitative comparison."""
    origin = rng.choice(DELHI_SEGMENTS[:5])
    dest = rng.choice(DELHI_SEGMENTS[15:])
    dep_hours = sorted(rng.sample(range(6, 22), k=3))

    return {
        "prompt": (
            f"<|im_start|>system\n{SYSTEM_PROMPT}<|im_end|>\n"
            f"<|im_start|>user\nCompare departures at {dep_hours[0]:02d}:00, "
            f"{dep_hours[1]:02d}:00, and {dep_hours[2]:02d}:00 for a Z-class convoy from "
            f"{origin['name']} to {dest['name']}. Recommend the time with lowest disruption. "
            f"Show your analysis step by step.<|im_end|>\n"
            f"<|im_start|>assistant\n"
        ),
        "ground_truth": {
            "required_tools": ["evaluate_scenarios", "predict_traffic_flow"],
            "num_scenarios": 3,
            "must_compare_quantitatively": True,
            "must_recommend_one": True,
        },
        "reward_metadata": {
            "type": "scenario_evaluation",
            "difficulty": "hard",
        },
    }


def _grpo_multi_tool_prompt(rng: random.Random) -> dict:
    """Complex prompt requiring multiple independent tool calls."""
    seg1, seg2 = rng.sample(DELHI_SEGMENTS, k=2)

    return {
        "prompt": (
            f"<|im_start|>system\n{SYSTEM_PROMPT}<|im_end|>\n"
            f"<|im_start|>user\nI need to assess two potential escort routes simultaneously. "
            f"Route A starts at segment {seg1['id']} ({seg1['name']}). "
            f"Route B starts at segment {seg2['id']} ({seg2['name']}). "
            f"Get live traffic for both starting segments and check their road specifications. "
            f"Which starting segment has better conditions right now?<|im_end|>\n"
            f"<|im_start|>assistant\n"
        ),
        "ground_truth": {
            "required_tools": ["get_live_traffic", "query_segment_details"],
            "must_call_parallel": True,
            "segments_to_check": [seg1["id"], seg2["id"]],
        },
        "reward_metadata": {
            "type": "multi_tool_parallel",
            "difficulty": "medium",
        },
    }


def _grpo_security_protocol_prompt(rng: random.Random) -> dict:
    """Security protocol compliance check."""
    vvip_cls = rng.choice(["Z+", "Z"])
    vvip_info = VVIP_CLASSES[vvip_cls]
    # Deliberately choose a segment that may not comply
    seg = rng.choice([s for s in DELHI_SEGMENTS if s["lanes"] <= 4])

    return {
        "prompt": (
            f"<|im_start|>system\n{SYSTEM_PROMPT}<|im_end|>\n"
            f"<|im_start|>user\nA {vvip_cls} protectee ({vvip_info['desc']}) needs to travel "
            f"through segment {seg['id']} ({seg['name']}). This is a {seg['class']} road with "
            f"{seg['lanes']} lanes. Can we proceed? If not, find an alternative segment "
            f"in the area.<|im_end|>\n"
            f"<|im_start|>assistant\n"
        ),
        "ground_truth": {
            "required_tools": ["query_segment_details"],
            "segment_id": seg["id"],
            "lanes": seg["lanes"],
            "min_required": vvip_info["min_lanes"],
            "is_compliant": seg["lanes"] >= vvip_info["min_lanes"],
            "must_reject_if_non_compliant": True,
            "should_suggest_alternative": seg["lanes"] < vvip_info["min_lanes"],
        },
        "reward_metadata": {
            "type": "security_compliance",
            "vvip_class": vvip_cls,
            "difficulty": "hard",
        },
    }


def generate_grpo_dataset(rng: random.Random, n_each: int = 30) -> list[dict]:
    """Generate GRPO prompts with verifiable ground truths."""
    dataset: list[dict] = []
    generators = [
        _grpo_route_planning_prompt,
        _grpo_anomaly_detection_prompt,
        _grpo_scenario_eval_prompt,
        _grpo_multi_tool_prompt,
        _grpo_security_protocol_prompt,
    ]
    for gen in generators:
        for _ in range(n_each):
            dataset.append(gen(rng))
    rng.shuffle(dataset)
    return dataset


# ─────────────────────────────────────────────────────────────────────────────
# CLI Entry Point
# ─────────────────────────────────────────────────────────────────────────────

@click.command()
@click.option("--output-dir", default="models/finetune/data", help="Output directory")
@click.option("--seed", default=42, type=int, help="Random seed for reproducibility")
@click.option("--sft-per-type", default=40, type=int, help="SFT examples per conversation type")
@click.option("--grpo-per-type", default=30, type=int, help="GRPO prompts per prompt type")
def main(output_dir: str, seed: int, sft_per_type: int, grpo_per_type: int) -> None:
    """Generate all training datasets for VVIP agent fine-tuning."""
    rng = random.Random(seed)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # 1. CPT corpus
    click.echo("Generating CPT corpus...")
    cpt = generate_cpt_corpus(rng)
    cpt_path = out / "cpt_corpus.jsonl"
    with open(cpt_path, "w", encoding="utf-8") as f:
        for item in cpt:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    click.echo(f"  → {len(cpt)} entries written to {cpt_path}")

    # 2. SFT conversations
    click.echo("Generating SFT conversations...")
    sft = generate_sft_dataset(rng, n_each=sft_per_type)
    sft_path = out / "sft_conversations.jsonl"
    with open(sft_path, "w", encoding="utf-8") as f:
        for item in sft:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    click.echo(f"  → {len(sft)} conversations written to {sft_path}")

    # 3. GRPO prompts
    click.echo("Generating GRPO prompts...")
    grpo = generate_grpo_dataset(rng, n_each=grpo_per_type)
    grpo_path = out / "grpo_prompts.jsonl"
    with open(grpo_path, "w", encoding="utf-8") as f:
        for item in grpo:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    click.echo(f"  → {len(grpo)} prompts written to {grpo_path}")

    # Summary
    click.echo(f"\nDataset generation complete:")
    click.echo(f"  CPT:  {len(cpt):>4} domain text chunks")
    click.echo(f"  SFT:  {len(sft):>4} multi-turn conversations")
    click.echo(f"  GRPO: {len(grpo):>4} RL prompts with ground truth")

    # Compute dataset fingerprint
    all_data = json.dumps({"cpt": len(cpt), "sft": len(sft), "grpo": len(grpo), "seed": seed})
    fingerprint = hashlib.sha256(all_data.encode()).hexdigest()[:12]
    click.echo(f"  Fingerprint: {fingerprint}")


if __name__ == "__main__":
    main()
