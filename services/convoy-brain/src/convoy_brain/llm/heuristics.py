"""Deterministic third tier of the fallback chain.

Fallback chain (ask): decider -> Gemma -> deterministic heuristics.
Everything in this module is pure computation over already-fetched tool data —
no model calls, no network. It is the floor of last resort when both model
tiers are slow or unavailable, and it also powers each tactical agent's
``heuristic_*`` path when the Gemma call fails or returns unparseable output.
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Traffic analyst
# ---------------------------------------------------------------------------


def heuristic_traffic_assessment(live_traffic: Any, forecast: Any = None) -> dict:
    """Derive a traffic situation report directly from live/forecast payloads.

    Shapes come from the traffic-oracle MCP tools:
        get_live_traffic  -> {"segments": [{"segment_id", "congestion_idx", "speed_kmh", ...}]}
        predict_traffic_flow -> {"forecasts": [{"segment_id", "horizon_min", "congestion_idx", ...}]}
    """
    segments = _extract_segment_rows(live_traffic)
    if not segments:
        return {
            "overall_status": "red",
            "congestion_summary": {"avg_congestion_idx": 0.0, "peak_congestion_idx": 0.0},
            "risk_segments": [],
            "trend": "stable",
            "recommendation": "No live segment data available; treat corridor as unverified.",
            "confidence": "low",
        }

    congestions = [s["congestion_idx"] for s in segments]
    avg = sum(congestions) / len(congestions)
    peak = max(congestions)

    risk_segments = [
        {
            "segment_id": s["segment_id"],
            "issue": f"congestion_idx {s['congestion_idx']:.2f}",
            "severity": "high" if s["congestion_idx"] >= 0.8 else "medium",
        }
        for s in segments
        if s["congestion_idx"] >= 0.7
    ]

    forecast_rows = _extract_forecast_rows(forecast)
    trend = "stable"
    if forecast_rows:
        future = [r["congestion_idx"] for r in forecast_rows]
        if future:
            future_avg = sum(future) / len(future)
            if future_avg > avg + 0.05:
                trend = "deteriorating"
            elif future_avg < avg - 0.05:
                trend = "improving"

    if avg >= 0.8 or peak >= 0.95:
        status = "red"
    elif avg >= 0.6 or peak >= 0.8:
        status = "amber"
    else:
        status = "green"

    return {
        "overall_status": status,
        "congestion_summary": {
            "avg_congestion_idx": round(avg, 3),
            "peak_congestion_idx": round(peak, 3),
        },
        "risk_segments": risk_segments,
        "trend": trend,
        "recommendation": (
            f"Corridor {status} (avg congestion {avg:.2f}, peak {peak:.2f}, trend {trend}). "
            + (
                f"{len(risk_segments)} segment(s) above the 0.7 risk line — consider delaying departure or re-routing."
                if risk_segments else "No segments above the 0.7 risk line."
            )
        ),
        "confidence": "medium",
    }


def _extract_segment_rows(live_traffic: Any) -> list[dict]:
    rows: list[dict] = []
    if isinstance(live_traffic, dict):
        rows = [r for r in live_traffic.get("segments", []) if isinstance(r, dict)]
    elif isinstance(live_traffic, list):
        rows = [r for r in live_traffic if isinstance(r, dict)]
    result = []
    for r in rows:
        congestion = r.get("congestion_idx")
        if isinstance(congestion, (int, float)):
            result.append({
                "segment_id": int(r.get("segment_id") or 0),
                "congestion_idx": float(congestion),
                "speed_kmh": float(r.get("speed_kmh") or 0.0),
            })
    return result


def _extract_forecast_rows(forecast: Any) -> list[dict]:
    rows: list[dict] = []
    if isinstance(forecast, dict):
        rows = [r for r in forecast.get("forecasts", forecast.get("segments", [])) if isinstance(r, dict)]
    elif isinstance(forecast, list):
        rows = [r for r in forecast if isinstance(r, dict)]
    result = []
    for r in rows:
        congestion = r.get("congestion_idx")
        if isinstance(congestion, (int, float)):
            result.append({
                "segment_id": int(r.get("segment_id") or 0),
                "congestion_idx": float(congestion),
            })
    return result


# ---------------------------------------------------------------------------
# Route planner
# ---------------------------------------------------------------------------


def heuristic_route_ranking(
    candidates: list[dict],
    scoring_weights: dict[str, float],
    min_lanes: int = 0,
) -> list[dict]:
    """Rank find_convoy_routes candidates deterministically.

    Applies the adaptive ``scoring_weights`` brain-side (contract C4): each
    candidate's ``scores`` sub-object (emitted by the OR-Tools MIP solver) is
    dotted with the weights; candidates missing a score component fall back
    to neutral 0.5. Returns candidates sorted best-first, each annotated with
    ``weighted_score``.
    """
    weights = _normalize_weights(scoring_weights)

    def score(candidate: dict) -> float:
        raw = candidate.get("scores") if isinstance(candidate, dict) else None
        raw = raw if isinstance(raw, dict) else {}
        total = 0.0
        for key, weight in weights.items():
            component = raw.get(key)
            value = float(component) if isinstance(component, (int, float)) else 0.5
            # Normalize clamped [0,1]-style components where higher is better;
            # solver emits 0..1 for disruption/complexity and security already.
            value = min(max(value, 0.0), 1.0) if 0.0 <= value <= 10.0 else value / 10.0
            total += weight * (1.0 - value if key in ("disruption", "complexity") else value)
        return total

    ranked = []
    for candidate in candidates:
        annotated = dict(candidate) if isinstance(candidate, dict) else {"route": candidate}
        annotated["weighted_score"] = round(score(annotated), 4)
        ranked.append(annotated)
    ranked.sort(key=lambda c: c.get("weighted_score", 0.0), reverse=True)
    return ranked


def _normalize_weights(weights: dict[str, float]) -> dict[str, float]:
    keys = ("time", "disruption", "security", "complexity")
    cleaned = {k: float(weights.get(k, 0.0)) for k in keys}
    total = sum(cleaned.values())
    if total <= 0:
        return {"time": 0.3, "disruption": 0.4, "security": 0.2, "complexity": 0.1}
    return {k: v / total for k, v in cleaned.items()}


# ---------------------------------------------------------------------------
# Diversion coordinator
# ---------------------------------------------------------------------------


def heuristic_diversion_directives(
    diversion_plan: Any,
    advance_closure_sec: int = 120,
) -> dict:
    """Build per-segment activation directives directly from a plan_diversions result."""
    segments: list[dict] = []
    if isinstance(diversion_plan, dict):
        raw = diversion_plan.get("diversions", diversion_plan.get("segments", []))
        segments = [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []
    elif isinstance(diversion_plan, list):
        segments = [r for r in diversion_plan if isinstance(r, dict)]

    directives = []
    queue_alerts = []
    for row in segments:
        seg_id = row.get("segment_id")
        if seg_id is None:
            continue
        directives.append({
            "segment_id": int(seg_id),
            "action": "activate",
            "agency": "traffic_police",
            "timing_sec": int(row.get("advance_sec", advance_closure_sec)),
            "detail": (
                f"Activate closure {int(row.get('advance_sec', advance_closure_sec))}s "
                f"before convoy arrival ({row.get('closure_type', 'partial')})."
            ),
        })
        queue = row.get("estimated_queue_m")
        if isinstance(queue, (int, float)) and queue > 500:
            queue_alerts.append({
                "segment_id": int(seg_id),
                "estimated_queue_m": float(queue),
                "action": "staff junction for queue discharge",
            })

    return {
        "directives": directives,
        "queue_alerts": queue_alerts,
        "overall_status": (
            f"{len(directives)} deterministic activation directives issued "
            f"(fallback mode)."
        ),
        "confidence": "medium",
    }


# ---------------------------------------------------------------------------
# Orchestrator: tool selection + final answer
# ---------------------------------------------------------------------------

#: Keyword -> tool mapping used when BOTH model tiers are unavailable.
#: The patterns deliberately cover BOTH vocabularies (protected-movement
#: phrasing and public-event/corridor-event phrasing — contract C7): the
#: heuristic floor must keep routing operator messages about processions,
#: races, gatherings, egress surges and work zones, not only convoys.
#: ORDER MATTERS (first match wins): comparison semantics
#: (evaluate_scenarios) are checked before closure management
#: (plan_diversions), and ``\beta\b`` is word-bounded so it never matches
#: inside "details".
_TOOL_KEYWORDS: list[tuple[str, str]] = [
    (r"forecast|predict|minutes ahead|next 30", "predict_traffic_flow"),
    (r"route from|plan .*route|alternative route|alternative corridor|"
     r"best route|way from|corridor (spine|route)|event route|"
     r"procession route|race route|parade (route|strip)|convoy route",
     "find_convoy_routes"),
    (r"compare|scenario|disruption vehicle|what.?if|closure polic|"
     r"shadow.?eval",
     "evaluate_scenarios"),
    (r"diversion|closure|queue|egress|dispersal|staged ingress",
     "plan_diversions"),
    (r"\beta\b|travel time|how long|arrive|sweep pace|chariot pace",
     "predict_eta"),
    (r"shortest path", "query_shortest_path"),
    (r"alternate paths|k shortest", "query_k_shortest_paths"),
    (r"segments? (in|near|around)|bounding box|area of|near me|"
     r"work.?zone|lane loss|barricaded lanes",
     "query_segments_in_bbox"),
    (r"road (width|details|lanes)|segment details", "query_segment_details"),
    (r"current traffic|live traffic|right now|congest(ed|ion) now|"
     r"crowd|spectator|pilgrim|gathering size",
     "get_live_traffic"),
    (r"histor|typical|usually|pattern|last (year|week)'s|previous "
     r"(yatra|marathon|match|parade|gathering)",
     "get_historical_pattern"),
]


def heuristic_select_tool(message: str, available_tools: list[str]) -> str | None:
    """Pick a first tool from the operator message by keyword match.

    Returns None when no tool is obviously implicated (final answer path).
    """
    lowered = message.lower()
    for pattern, tool in _TOOL_KEYWORDS:
        if tool not in available_tools:
            continue
        if re.search(pattern, lowered):
            return tool
    return None


def heuristic_default_arguments(tool_name: str, message_context: dict[str, Any] | None = None) -> dict:
    """Deterministic default arguments when Gemma cannot draft them.

    ``message_context`` may carry known quantities (segment ids, bbox) from
    earlier in the turn; anything missing stays empty and the tool call is
    expected to fail loudly into the error path (better than fabricated data).
    """
    ctx = message_context or {}
    if tool_name in ("predict_traffic_flow", "get_live_traffic"):
        ids = ctx.get("segment_ids") or ctx.get("known_segment_ids") or []
        return {"segment_ids": [int(i) for i in ids][:200]}
    if tool_name == "find_convoy_routes":
        return {
            "origin_segment": ctx.get("origin_segment", 0),
            "destination_segment": ctx.get("destination_segment", 0),
            "max_candidates": 5,
            "avoid_segments": [],
        }
    if tool_name == "plan_diversions":
        return {
            "route_segment_ids": [int(i) for i in ctx.get("route_segment_ids", [])][:200],
            "advance_closure_sec": int(ctx.get("advance_closure_sec", 120)),
        }
    if tool_name == "evaluate_scenarios":
        return {"scenarios": ctx.get("scenarios", [])}
    if tool_name == "predict_eta":
        return {k: ctx.get(k) for k in (
            "route_length_m", "num_segments", "avg_predicted_speed",
            "avg_predicted_congestion", "hour", "day_of_week",
            "num_signals", "road_class_score",
        ) if ctx.get(k) is not None}
    if tool_name == "query_shortest_path":
        return {"source_segment": ctx.get("source_segment", 0), "target_segment": ctx.get("target_segment", 0)}
    if tool_name == "query_k_shortest_paths":
        return {
            "source_segment": ctx.get("source_segment", 0),
            "target_segment": ctx.get("target_segment", 0),
            "k": 5,
        }
    if tool_name == "query_segments_in_bbox":
        bbox = ctx.get("corridor_bbox") or (72.48, 22.95, 72.68, 23.12)
        return {
            "min_lon": bbox[0], "min_lat": bbox[1],
            "max_lon": bbox[2], "max_lat": bbox[3],
        }
    if tool_name == "query_segment_details":
        return {"segment_id": ctx.get("segment_id", 0)}
    if tool_name == "get_historical_pattern":
        return {
            "segment_id": ctx.get("segment_id", 0),
            "day_of_week": ctx.get("day_of_week", 0),
            "hour": ctx.get("hour", 8),
        }
    if tool_name == "simulate_twin":
        return {
            "route_segment_ids": [int(i) for i in ctx.get("route_segment_ids", [])][:200],
            "n_replications": 8,
        }
    return {}


def heuristic_final_answer(
    message: str,
    tool_calls_made: list[str],
    error: str | None = None,
) -> dict:
    """Final answer of last resort — honest about degraded operation."""
    return {
        "action": "degraded_text_response",
        "reasoning": (
            f"Both model tiers were unavailable{' — ' + error if error else ''}. "
            f"Responding deterministically. Tools invoked this turn: "
            f"{', '.join(tool_calls_made) or 'none'}."
        ),
        "tool_calls_made": tool_calls_made,
        "confidence": "low",
        "data": {
            "operator_message": message[:500],
            "tier": "heuristic",
        },
        "protocol_assessment": {
            "asl_status": "not_assessed",
            "protocol_violations": [],
            "threat_level": "nominal",
            "plan_b_ready": False,
        },
    }
