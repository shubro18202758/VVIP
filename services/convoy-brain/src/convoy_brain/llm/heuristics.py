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

    Shapes come from the traffic-oracle MCP tools (legacy shapes accepted):
        get_live_traffic     -> {"observations": [{"segment_id", "congestion_idx",
                                                   "speed_kmh", ...}]}
                                (legacy {"segments": [...]})
        predict_traffic_flow -> {"predictions": {sid: {horizon_min: {"speed_kmh",
                                                                     "congestion_idx"}}}}
                                (legacy {"forecasts": [{"segment_id", "horizon_min", ...}]})

    No live observations is NOT a red corridor: the status is ``unknown``
    (unverified, low confidence) so a silent feed never masquerades as
    congestion — or as an all-clear.
    """
    segments = _extract_segment_rows(live_traffic)
    if not segments:
        return {
            "overall_status": "unknown",
            "congestion_summary": {"avg_congestion_idx": None, "peak_congestion_idx": None},
            "risk_segments": [],
            "trend": "unknown",
            "recommendation": (
                "No live observations for the corridor segments — conditions are "
                "unverified (the live feed is silent). Plan on protocol floors and "
                "re-check before departure."
            ),
            "confidence": "low",
            "observations": 0,
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

    # Trend: each segment's furthest forecast horizon against the live mean.
    forecast_rows = _extract_forecast_rows(forecast)
    trend = "stable"
    if forecast_rows:
        furthest: dict[int, dict] = {}
        for row in forecast_rows:
            current = furthest.get(row["segment_id"])
            if current is None or row["horizon_min"] > current["horizon_min"]:
                furthest[row["segment_id"]] = row
        future = [r["congestion_idx"] for r in furthest.values()]
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
        "observations": len(segments),
    }


def live_observation_rows(live_traffic: Any) -> list[dict]:
    """Public view of the normalized live rows (``segment_id``, ``congestion_idx``,
    ``speed_kmh``) — the agents use it to decide whether there is anything to
    narrate before spending a tactical call."""
    return _extract_segment_rows(live_traffic)


def forecast_rows(forecast: Any) -> list[dict]:
    """Public view of the normalized forecast rows (``segment_id``,
    ``horizon_min``, ``congestion_idx``, ``speed_kmh``)."""
    return _extract_forecast_rows(forecast)


def _extract_segment_rows(live_traffic: Any) -> list[dict]:
    rows: list[dict] = []
    if isinstance(live_traffic, dict):
        raw = live_traffic.get("observations")
        if not isinstance(raw, list):
            raw = live_traffic.get("segments", [])
        rows = [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []
    elif isinstance(live_traffic, list):
        rows = [r for r in live_traffic if isinstance(r, dict)]
    result = []
    for r in rows:
        congestion = r.get("congestion_idx")
        if isinstance(congestion, (int, float)) and not isinstance(congestion, bool):
            result.append({
                "segment_id": _as_int(r.get("segment_id")),
                "congestion_idx": float(congestion),
                "speed_kmh": float(r.get("speed_kmh") or 0.0),
            })
    return result


def _extract_forecast_rows(forecast: Any) -> list[dict]:
    result: list[dict] = []
    if isinstance(forecast, dict) and isinstance(forecast.get("predictions"), dict):
        # Oracle shape: {"predictions": {sid: {horizon_min: {speed_kmh, congestion_idx}}}}
        # (JSON object keys arrive as strings).
        for sid, horizons in forecast["predictions"].items():
            if not isinstance(horizons, dict):
                continue
            for horizon, row in horizons.items():
                if not isinstance(row, dict):
                    continue
                congestion = row.get("congestion_idx")
                if not isinstance(congestion, (int, float)) or isinstance(congestion, bool):
                    continue
                result.append({
                    "segment_id": _as_int(sid),
                    "horizon_min": _as_int(horizon),
                    "congestion_idx": float(congestion),
                    "speed_kmh": _as_float(row.get("speed_kmh")),
                })
        return result

    rows: list[dict] = []
    if isinstance(forecast, dict):
        raw = forecast.get("forecasts", forecast.get("segments", []))
        rows = [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []
    elif isinstance(forecast, list):
        rows = [r for r in forecast if isinstance(r, dict)]
    for r in rows:
        congestion = r.get("congestion_idx")
        if isinstance(congestion, (int, float)) and not isinstance(congestion, bool):
            result.append({
                "segment_id": _as_int(r.get("segment_id")),
                "horizon_min": _as_int(r.get("horizon_min")),
                "congestion_idx": float(congestion),
                "speed_kmh": _as_float(r.get("speed_kmh")),
            })
    return result


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Route planner
# ---------------------------------------------------------------------------


def dedupe_route_candidates(candidates: list[dict]) -> list[dict]:
    """Drop candidates whose segment list repeats an earlier candidate's.

    The oracle appends the MIP optimum and the k-shortest-path alternatives
    without de-duplicating, so the same path routinely arrives twice (MIP and
    KSP ids, identical segments) and the operator was shown two identical
    "alternatives". The first occurrence wins (the oracle sorts by composite
    score, lower is better); dropped ids are kept on the survivor under
    ``duplicate_route_ids`` for the audit trail.
    """
    kept: list[dict] = []
    by_path: dict[tuple, dict] = {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        path = tuple(candidate.get("segment_ids") or [])
        if not path:
            continue
        survivor = by_path.get(path)
        if survivor is not None:
            survivor.setdefault("duplicate_route_ids", []).append(candidate.get("route_id"))
            continue
        copy = dict(candidate)
        by_path[path] = copy
        kept.append(copy)
    return kept


def derive_route_scores(candidates: list[dict]) -> list[dict]:
    """Attach a 0..1 ``scores`` object to each candidate when the oracle omits it.

    /optimize/routes emits ``estimated_time_sec``, ``total_distance_m``,
    ``disruption_score`` (0-100, lower is better), ``security_score`` (0-100,
    higher is better) and ``composite_score`` — never the ``scores``
    sub-object the C4 ranking dots with the adaptive weights, so every
    component read a neutral 0.5 and the weights had no effect. Conventions
    match :func:`heuristic_route_ranking`: ``time`` and ``security`` are
    benefits (1 = fastest / most secure), ``disruption`` and ``complexity``
    are costs (1 = most disruptive / most complex). Time and complexity are
    relative to the candidate set (fastest / fewest segments = reference);
    disruption and security come straight from the solver's 0-100 scales.
    """
    def positive(value: Any) -> float | None:
        number = _as_float(value)
        return number if number is not None and number > 0 else None

    etas = [e for e in (positive(c.get("estimated_time_sec")) for c in candidates) if e]
    lengths = [len(c.get("segment_ids") or []) for c in candidates if c.get("segment_ids")]
    fastest = min(etas) if etas else None
    shortest = min(lengths) if lengths else None

    annotated: list[dict] = []
    for candidate in candidates:
        row = dict(candidate)
        existing = row.get("scores")
        if isinstance(existing, dict) and existing:
            row.setdefault("scores_source", "oracle")
            annotated.append(row)
            continue
        eta = positive(row.get("estimated_time_sec"))
        disruption = _as_float(row.get("disruption_score"))
        security = _as_float(row.get("security_score"))
        n_segments = len(row.get("segment_ids") or [])
        row["scores"] = {
            "time": round(fastest / eta, 4) if (eta and fastest) else 0.5,
            "disruption": round(_clamp01(disruption / 100.0), 4) if disruption is not None else 0.5,
            "security": round(_clamp01(security / 100.0), 4) if security is not None else 0.5,
            "complexity": (
                round(1.0 - shortest / n_segments, 4) if (n_segments and shortest) else 0.0
            ),
        }
        row["scores_source"] = "derived"
        annotated.append(row)
    return annotated


def _clamp01(value: float) -> float:
    return min(max(value, 0.0), 1.0)


def heuristic_route_ranking(
    candidates: list[dict],
    scoring_weights: dict[str, float],
    min_lanes: int = 0,
) -> list[dict]:
    """Rank find_convoy_routes candidates deterministically.

    Applies the adaptive ``scoring_weights`` brain-side (contract C4): each
    candidate's ``scores`` sub-object is dotted with the weights. The oracle
    does not emit ``scores``, so they are derived from the solver metrics
    first (:func:`derive_route_scores`); a component that cannot be derived
    falls back to neutral 0.5. Returns candidates sorted best-first, each
    annotated with ``weighted_score``.
    """
    candidates = derive_route_scores(
        [c for c in candidates if isinstance(c, dict)]
    ) + [c for c in candidates if not isinstance(c, dict)]
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


def diversion_rows(diversion_plan: Any) -> list[dict]:
    """The plan_diversions rows (``diversions``; legacy ``segments``)."""
    if isinstance(diversion_plan, dict):
        raw = diversion_plan.get("diversions", diversion_plan.get("segments", []))
        return [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []
    if isinstance(diversion_plan, list):
        return [r for r in diversion_plan if isinstance(r, dict)]
    return []


_IST_OFFSET_SEC = 5 * 3600 + 30 * 60


def _ist_clock(stamp: Any) -> str | None:
    """'21:14:05 IST' for an ISO-8601 instant (naive = UTC); None when unparseable."""
    if not isinstance(stamp, str) or not stamp.strip():
        return None
    from datetime import UTC, datetime, timedelta, timezone

    text = stamp.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    ist = moment.astimezone(timezone(timedelta(seconds=_IST_OFFSET_SEC)))
    return ist.strftime("%H:%M:%S") + " IST"


def _closure_words(diversion_type: Any) -> str:
    """'full_closure' -> 'full closure' (the oracle's diversion_type vocabulary)."""
    text = str(diversion_type or "closure").replace("_", " ").strip()
    return text if "closure" in text or "restriction" in text else f"{text} closure"


def _ids_list(ids: Any, limit: int = 4) -> str:
    values = [str(int(i)) for i in ids if isinstance(i, (int, float)) and not isinstance(i, bool)] \
        if isinstance(ids, list) else []
    shown = ", ".join(values[:limit])
    return shown + (f" (+{len(values) - limit} more)" if len(values) > limit else "")


def _detour_text(row: dict) -> str | None:
    """The detour clause a directive must state for its plan row, or None.

    Reads the oracle's additive detour facts (WP-16): ``detour_status``
    found / impractical / no_path / dead_end, ``entry_pinned`` /
    ``exit_pinned`` (False = the detour covers only part of the closure),
    ``detour_distance_m`` against ``detour_limit_m`` and ``detour_reason``.
    An older oracle without ``detour_status`` gets None (nothing invented).
    """
    status = row.get("detour_status")
    if not isinstance(status, str) or not status:
        return None
    reason = str(row.get("detour_reason") or "").strip().rstrip(".")
    distance = _as_float(row.get("detour_distance_m"))
    limit = _as_float(row.get("detour_limit_m"))
    measured = (
        f" ({distance / 1000:.1f} km detour vs {limit / 1000:.1f} km practicality limit)"
        if distance is not None and limit is not None else
        f" (practicality limit {limit / 1000:.1f} km)" if limit is not None else ""
    )
    if status == "impractical":
        return (
            "HOLD / METER cross traffic at the closure — no practical detour"
            + (f": {reason}" if reason else "") + measured + "; no alternative segments offered"
        )
    if status in ("no_path", "dead_end"):
        return (
            f"HOLD / METER cross traffic — no detour exists ({status.replace('_', ' ')})"
            + (f": {reason}" if reason else "")
        )
    partial = row.get("entry_pinned") is False or row.get("exit_pinned") is False
    alts = _ids_list(row.get("alt_segment_ids"))
    via = f"divert via segment(s) {alts}" if alts else "detour planned"
    if partial:
        return (
            f"partial detour — {via}; it covers only part of the closed segment"
            + (f" ({reason})" if reason else "")
        )
    return via + (f" ({distance / 1000:.1f} km)" if distance is not None else "")


def apply_detour_facts(directives: list[dict], diversion_plan: Any) -> list[dict]:
    """Hold every directive to its plan row's detour facts (model AND floor).

    The tactical narration and the deterministic floor both write free
    text; this post-step makes the text say what the oracle planned: an
    impractical (or missing) detour is a hold / meter instruction quoting
    the reason and the distance against the limit, with the alternative
    segments removed; an unpinned end is a 'partial detour'; a found detour
    names its alternative segments. The row's ``detour_status`` /
    ``detour_reason`` ride along on the directive (the plan document's
    diversion_directives carry them). Rows of an older oracle without the
    fields pass through untouched.
    """
    by_segment = {
        str(r.get("segment_id")): r for r in diversion_rows(diversion_plan)
        if r.get("segment_id") is not None
    }
    shaped: list[dict] = []
    for directive in directives:
        if not isinstance(directive, dict):
            continue
        row = by_segment.get(str(directive.get("segment_id")))
        clause = _detour_text(row) if row is not None else None
        if row is None or clause is None:
            shaped.append(directive)
            continue
        out = dict(directive)
        status = row.get("detour_status")
        out["detour_status"] = status
        if row.get("detour_reason"):
            out["detour_reason"] = row.get("detour_reason")
        for key in ("detour_distance_m", "detour_limit_m", "entry_pinned", "exit_pinned"):
            if row.get(key) is not None:
                out[key] = row[key]
        if status == "found" and row.get("alt_segment_ids"):
            out["alt_segment_ids"] = list(row["alt_segment_ids"])
        else:
            out.pop("alt_segment_ids", None)
        if out.get("action") in ("activate", "hold"):
            # A found detour keeps the writer's own sentence; anywhere else
            # the sentence may offer a detour the oracle found impractical
            # or absent, so the row's own facts replace it. Idempotent: a
            # detail that already states the clause (the floor's, filled in
            # after the model wrote its rows) is not given it twice.
            own = str(out.get("detail") or "").strip().rstrip(".")
            sentence = f"{clause[0].upper()}{clause[1:]}"
            if status == "found" and own and sentence.lower() in own.lower():
                out["detail"] = f"{own}."
            else:
                lead = own if (status == "found" and own) else _row_lead(row, out)
                out["detail"] = f"{lead}. {sentence}."
        shaped.append(out)
    return shaped


def diversion_posture(directives: list[dict], queue_alerts: Any = None) -> str:
    """One grounded sentence on a closure plan: closures, holds, diversions,
    the longest queue — what a figure-free model status is replaced by."""
    rows = [d for d in directives if isinstance(d, dict)]
    held = sum(1 for d in rows if d.get("detour_status") in ("impractical", "no_path", "dead_end"))
    diverted = sum(1 for d in rows if d.get("detour_status") == "found")
    text = f"{len(rows)} closure directive(s)"
    if held:
        text += f"; {held} with no practical detour — hold / meter cross traffic there"
    if diverted:
        text += f"; {diverted} with a planned detour"
    queues = [
        q for q in (queue_alerts if isinstance(queue_alerts, list) else [])
        if isinstance(q, dict) and isinstance(q.get("estimated_queue_m"), (int, float))
    ]
    if queues:
        worst = max(queues, key=lambda q: q["estimated_queue_m"])
        text += (
            f"; longest queue {worst['estimated_queue_m']:.0f} m on segment "
            f"{worst.get('segment_id')}"
        )
    return text + "."


def _row_lead(row: dict, directive: dict) -> str:
    """'Activate full closure on segment 1012 at 21:14:05 IST' from a plan row."""
    sid = row.get("segment_id", directive.get("segment_id"))
    verb = "Hold" if directive.get("action") == "hold" else "Activate"
    closure = _closure_words(row.get("diversion_type") or row.get("closure_type") or "closure")
    at = _ist_clock(row.get("activate_at"))
    return f"{verb} {closure} on segment {sid}" + (f" at {at}" if at else "")


def heuristic_diversion_directives(
    diversion_plan: Any,
    advance_closure_sec: int = 120,
) -> dict:
    """Build per-segment activation directives directly from a plan_diversions result.

    Reads the oracle's own row vocabulary: ``diversion_type`` (full_closure /
    partial_closure / speed_restriction), ``activate_at`` (the closure's
    wall-clock start, printed in IST) and the detour facts (see
    :func:`apply_detour_facts`). The legacy ``closure_type`` / ``advance_sec``
    keys are still honoured when an older payload carries them.
    """
    directives = []
    queue_alerts = []
    rows = diversion_rows(diversion_plan)
    for row in rows:
        seg_id = row.get("segment_id")
        if seg_id is None:
            continue
        advance = int(row.get("advance_sec", advance_closure_sec))
        closure = _closure_words(row.get("diversion_type") or row.get("closure_type") or "partial")
        at = _ist_clock(row.get("activate_at"))
        directives.append({
            "segment_id": int(seg_id),
            "action": "activate",
            "agency": "traffic_police",
            "timing_sec": advance,
            "detail": (
                f"Activate {closure} on segment {int(seg_id)} "
                + (f"at {at} ({advance}s before the convoy reaches it)" if at
                   else f"{advance}s before convoy arrival")
                + "."
            ),
        })
        queue = row.get("estimated_queue_m")
        if isinstance(queue, (int, float)) and queue > 500:
            queue_alerts.append({
                "segment_id": int(seg_id),
                "estimated_queue_m": float(queue),
                "action": "staff junction for queue discharge",
            })
    directives = apply_detour_facts(directives, rows)
    held = sum(
        1 for d in directives
        if d.get("detour_status") in ("impractical", "no_path", "dead_end")
    )

    return {
        "directives": directives,
        "queue_alerts": queue_alerts,
        "overall_status": (
            f"{len(directives)} deterministic activation directives issued (fallback mode)"
            + (
                f"; {held} closure(s) have no practical detour — hold / meter traffic there"
                if held else ""
            )
            + "."
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
    (r"interception|choke|intercept",
     "get_interception_plan"),
    (r"stolen|earmark|watchlist|\bplate\b|suspect vehicle|which vehicle|"
     r"where is (the )?(vehicle|car|convoy|unit)|on the map|show me|locate",
     "city_query_entities"),
    (r"e-?challan|recommendation|fines? pending|penalt",
     "list_enforcement_recommendations"),
    (r"dispatch(es|ed)?\b|requested units?",
     "list_dispatches"),
    (r"\bunits?\b|jan rakshak|patrol|ambulance|fire tender",
     "list_units"),
    (r"signal (state|phase|plan|network)|junction phase|green wave",
     "get_signal_state"),
    (r"situation (band|log)|\bband\b|ama posture|posture",
     "get_situation_log"),
    (r"what happened|latest (events|activity)|city feed|narrative|audit trail",
     "city_feed"),
    (r"status of (the )?(corridor|event|pursuit|movement)|corridor status|"
     r"active (corridor )?events|corridor events (are )?(active|live|running|registered)|"
     r"registered (corridor )?events|events? on the (board|map|canvas)|"
     r"which (corridor )?events",
     "get_active_corridor_events"),
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
    if tool_name == "city_query_entities":
        return {k: v for k, v in (
            ("kinds", ctx.get("kinds")), ("match", ctx.get("match")), ("limit", 25),
        ) if v}
    if tool_name == "get_active_corridor_events":
        return {k: v for k, v in (("event_id", ctx.get("event_id")),) if v}
    if tool_name in ("get_interception_plan", "list_dispatches", "get_situation_log"):
        return {"event_id": ctx["event_id"]} if ctx.get("event_id") else {}
    if tool_name == "city_get_dossier":
        return {"urn": ctx["context_urn"]} if ctx.get("context_urn") else {}
    if tool_name == "list_enforcement_recommendations":
        return {"status": "pending"}
    if tool_name in ("canvas_fly_to", "canvas_track_entity") and ctx.get("top_urn"):
        return {"urn": ctx["top_urn"]}
    if tool_name == "canvas_highlight" and ctx.get("top_urns"):
        return {"urns": list(ctx["top_urns"])[:20]}
    return {}


def heuristic_final_answer(
    message: str,
    tool_calls_made: list[str],
    error: str | None = None,
    *,
    tool_summaries: list[str] | None = None,
) -> dict:
    """Final answer of last resort — honest about degraded operation.

    The tactical tier composes final answers; this floor runs when it could
    not (unreachable, timed out, or unparseable output). The prose says
    exactly that — it never claims both model tiers were down when only the
    tactical composition failed — and carries the deterministic one-line
    summaries of whatever the tools returned this turn, so the operator
    still gets the data.
    """
    reason = f" ({error})" if error else ""
    if tool_summaries:
        prose = (
            f"The tactical tier could not compose this answer{reason}, so here is "
            f"the deterministic summary of what the tools returned: "
            + "; ".join(tool_summaries)
            + "."
        )
    else:
        prose = (
            f"The tactical tier could not compose this answer{reason} and no tool "
            f"data was gathered this turn (tools invoked: "
            f"{', '.join(tool_calls_made) or 'none'}). Please retry or rephrase."
        )
    return {
        "action": "degraded_text_response",
        "reasoning": prose,
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


# ---------------------------------------------------------------------------
# Tool-result summaries (stream tool_result.summary + the final-answer floor)
# ---------------------------------------------------------------------------


def summarize_tool_result(tool_name: str, arguments: dict | None, result: Any) -> str:
    """One deterministic, human-readable line describing a tool result.

    Used for the chat stream's ``tool_result.summary`` (contract C-D) and by
    :func:`heuristic_final_answer`. Pure computation over the payload: empty
    results are said plainly ("no live observations ...") instead of being
    rendered as zeros.
    """
    args = arguments if isinstance(arguments, dict) else {}
    if isinstance(result, dict) and result.get("error"):
        message = result.get("message") or result.get("error")
        return f"{tool_name} failed: {str(message)[:200]}"
    try:
        summarizer = _SUMMARIZERS.get(tool_name)
        if summarizer is not None:
            return summarizer(args, result)
    except Exception:  # summaries are cosmetic — never break a turn
        pass
    if isinstance(result, dict):
        parts = []
        for key, value in list(result.items())[:4]:
            if isinstance(value, list):
                parts.append(f"{len(value)} {key}")
            elif isinstance(value, (str, int, float, bool)):
                parts.append(f"{key}={str(value)[:40]}")
        return f"{tool_name}: " + (", ".join(parts) if parts else "empty result")
    if isinstance(result, list):
        return f"{tool_name}: {len(result)} row(s)"
    return f"{tool_name}: {str(result)[:120]}"


def _ids_text(ids: Any, limit: int = 6) -> str:
    if not isinstance(ids, list) or not ids:
        return "the requested segments"
    shown = ", ".join(str(i) for i in ids[:limit])
    return shown + (f" (+{len(ids) - limit} more)" if len(ids) > limit else "")


def _road_names(result: Any) -> dict[int, str]:
    """``{segment_id: road_name}`` from observations that carry one (C-P)."""
    names: dict[int, str] = {}
    observations = result.get("observations") if isinstance(result, dict) else result
    for obs in observations if isinstance(observations, list) else []:
        road = obs.get("road_name") if isinstance(obs, dict) else None
        if isinstance(road, str) and road.strip():
            names[_as_int(obs.get("segment_id"))] = road.strip()
    return names


def _segment_label(segment_id: int, names: dict[int, str]) -> str:
    """'Ashram Road (1012)' when the observation names the road, else 'segment 1012'."""
    name = names.get(segment_id)
    return f"{name} ({segment_id})" if name else f"segment {segment_id}"


def _summarize_live(args: dict, result: Any) -> str:
    rows = _extract_segment_rows(result)
    requested = args.get("segment_ids")
    if not rows:
        return (
            f"get_live_traffic: no live observations for segments {_ids_text(requested)} "
            f"(the live feed has nothing cached for them)"
        )
    names = _road_names(result)
    speeds = [r["speed_kmh"] for r in rows]
    congestion = [r["congestion_idx"] for r in rows]
    worst = max(rows, key=lambda r: r["congestion_idx"])
    slowest = min(rows, key=lambda r: r["speed_kmh"])
    qualities = sorted({
        str(o.get("data_quality")) for o in (result.get("observations") or [])
        if isinstance(o, dict) and o.get("data_quality")
    }) if isinstance(result, dict) else []
    return (
        f"get_live_traffic: {len(rows)} observation(s) — avg speed "
        f"{sum(speeds) / len(speeds):.1f} km/h, avg congestion "
        f"{sum(congestion) / len(congestion):.2f}, "
        f"worst {_segment_label(worst['segment_id'], names)} "
        f"at {worst['congestion_idx']:.2f}"
        + (
            f", slowest {_segment_label(slowest['segment_id'], names)} "
            f"at {slowest['speed_kmh']:.0f} km/h"
            if slowest is not worst else ""
        )
        + (" (SIMULATED FEED)" if "synthetic" in qualities else "")
    )


def _summarize_forecast(args: dict, result: Any) -> str:
    rows = _extract_forecast_rows(result)
    if not rows:
        return (
            f"predict_traffic_flow: no forecast rows for segments "
            f"{_ids_text(args.get('segment_ids'))} (no input history to forecast from)"
        )
    horizon = max(r["horizon_min"] for r in rows)
    at_h = [r for r in rows if r["horizon_min"] == horizon]
    mean = sum(r["congestion_idx"] for r in at_h) / len(at_h)
    return (
        f"predict_traffic_flow: {len({r['segment_id'] for r in rows})} segment(s) "
        f"forecast; mean congestion {mean:.2f} at T+{horizon} min"
    )


def _summarize_routes(args: dict, result: Any) -> str:
    routes = result.get("routes", []) if isinstance(result, dict) else []
    routes = [r for r in routes if isinstance(r, dict)]
    if not routes:
        return (
            f"find_convoy_routes: no route between segments {args.get('origin_segment')} "
            f"and {args.get('destination_segment')}"
        )
    best = routes[0]
    distance = _as_float(best.get("total_distance_m")) or 0.0
    eta = _as_float(best.get("estimated_time_sec")) or 0.0
    return (
        f"find_convoy_routes: {len(routes)} candidate(s); best {best.get('route_id')} "
        f"— {len(best.get('segment_ids') or [])} segments, {distance / 1000:.1f} km, "
        f"ETA {eta / 60:.0f} min"
    )


def _summarize_bbox(args: dict, result: Any) -> str:
    rows = result.get("segments", []) if isinstance(result, dict) else []
    rows = [r for r in rows if isinstance(r, dict)]
    names = [str(r.get("road_name")) for r in rows[:4] if r.get("road_name")]
    return (
        f"query_segments_in_bbox: {len(rows)} segment(s)"
        + (f" — {', '.join(names)}" if names else "")
    )


def _summarize_segment(args: dict, result: Any) -> str:
    if not isinstance(result, dict):
        return "query_segment_details: no data"
    length = _as_float(result.get("length_m"))
    return (
        f"query_segment_details: segment {result.get('segment_id')} "
        f"{result.get('road_name') or ''} — {result.get('road_class')}, "
        f"{result.get('lanes')} lanes, {result.get('speed_limit_kmh')} km/h"
        + (f", {length / 1000:.2f} km" if length else "")
        + (", one-way" if result.get("oneway") else "")
    )


def _summarize_diversions(args: dict, result: Any) -> str:
    rows = result.get("diversions", []) if isinstance(result, dict) else []
    rows = [r for r in rows if isinstance(r, dict)]
    if not rows:
        return "plan_diversions: no diversion rows returned for the route"
    queues = [_as_float(r.get("estimated_queue_m")) or 0.0 for r in rows]
    return (
        f"plan_diversions: {len(rows)} diversion(s); longest estimated queue "
        f"{max(queues):.0f} m"
    )


def _summarize_scenarios(args: dict, result: Any) -> str:
    rows = result.get("results", []) if isinstance(result, dict) else []
    rows = [r for r in rows if isinstance(r, dict)]
    if not rows:
        return "evaluate_scenarios: no scenario results"
    best = min(rows, key=lambda r: _as_float(r.get("total_disruption_vehicle_hours")) or 0.0)
    return (
        f"evaluate_scenarios: {len(rows)} scenario(s); least disruptive "
        f"{best.get('scenario_id')} at "
        f"{_as_float(best.get('total_disruption_vehicle_hours')) or 0.0:.1f} vehicle-hours"
    )


def _summarize_eta(args: dict, result: Any) -> str:
    seconds = _as_float(result.get("eta_seconds")) if isinstance(result, dict) else None
    if seconds is None:
        return "predict_eta: no ETA returned"
    model = result.get("model") if isinstance(result, dict) else None
    return f"predict_eta: {seconds / 60:.1f} min" + (f" (model {model})" if model else "")


def _summarize_path(args: dict, result: Any) -> str:
    path = result.get("path", []) if isinstance(result, dict) else []
    nodes = [r.get("node") for r in path if isinstance(r, dict) and (r.get("node") or 0) > 0]
    if not nodes:
        return "query_shortest_path: no path"
    cost = _as_float(path[-1].get("agg_cost")) if isinstance(path[-1], dict) else None
    return (
        f"query_shortest_path: {len(nodes)} segments ({_ids_text(nodes)})"
        + (f", cost {cost:.0f} s" if cost is not None else "")
    )


def _summarize_ksp(args: dict, result: Any) -> str:
    paths = result.get("paths", []) if isinstance(result, dict) else []
    return f"query_k_shortest_paths: {len(paths)} alternative path(s)"


def _summarize_events(args: dict, result: Any) -> str:
    events = result.get("events", []) if isinstance(result, dict) else []
    events = [e for e in events if isinstance(e, dict)]
    total = result.get("total_active") if isinstance(result, dict) else None
    note = result.get("note") if isinstance(result, dict) else None
    if not events:
        if isinstance(total, int) and total:
            return (
                f"get_active_corridor_events: none match the filter "
                f"({total} active event(s) on the board in total)"
            )
        return "get_active_corridor_events: no active corridor events"
    if len(events) == 1:
        e = events[0]
        spines = [r for r in e.get("routes") or [] if isinstance(r, dict)]
        status = str(e.get("status"))
        meaning = _STATUS_MEANING.get(status)
        spine_text = "; ".join(
            f"{r.get('label')}: segments {_ids_text(r.get('segment_ids'))}" for r in spines[:3]
        )
        return (
            f"get_active_corridor_events: {str(e.get('event_kind')).replace('_', ' ')} "
            f"'{e.get('title')}' — status {status}" + (f" ({meaning})" if meaning else "")
            + (f"; spine(s) {spine_text}" if spine_text else "; no route spines")
            + (f" ({note})" if note else "")
        )
    kinds: dict[str, int] = {}
    for event in events:
        kinds[str(event.get("event_kind"))] = kinds.get(str(event.get("event_kind")), 0) + 1
    return "get_active_corridor_events: " + ", ".join(
        f"{count} {kind}" for kind, count in sorted(kinds.items())
    ) + (f" ({note})" if note else "")


#: What a corridor-event status means (the C-F lifecycle), for summaries.
_STATUS_MEANING = {
    "planning": "registered, not yet approved or live",
    "approved": "approved by an officer, not yet live",
    "active": "live now",
    "completed": "finished",
    "cancelled": "cancelled",
}


def _summarize_history(args: dict, result: Any) -> str:
    if isinstance(result, dict) and result.get("ok") is False:
        return f"get_historical_pattern: {result.get('detail') or 'no answer'}"
    if isinstance(result, dict):
        real = result.get("aggregates") if isinstance(result.get("aggregates"), list) else None
        synthetic = result.get("synthetic_aggregates")
        provenance = result.get("provenance")
        if real:
            speeds = [r.get("avg_speed_kmh") for r in real if isinstance(r, dict)
                      and isinstance(r.get("avg_speed_kmh"), (int, float))]
            return (
                f"get_historical_pattern: {len(real)} real hourly row(s) at the same IST "
                f"weekday/hour" + (f", avg {sum(speeds) / len(speeds):.1f} km/h" if speeds else "")
            )
        if isinstance(synthetic, list) and synthetic:
            return (
                f"get_historical_pattern: no real history yet — {len(synthetic)} synthetic "
                f"row(s) of the SIMULATED feed (labelled, not used for calibration)"
            )
        if provenance == "none" or real is not None:
            return "get_historical_pattern: no history for this segment at that weekday/hour"
        for key in ("patterns", "rows", "history"):
            if isinstance(result.get(key), list):
                return f"get_historical_pattern: {len(result[key])} row(s)"
    return "get_historical_pattern: no historical rows"


def _not_answered(tool: str, result: Any) -> str | None:
    """The one-line summary of a ``{ok: false}`` read (unknown / not deployed)."""
    if isinstance(result, dict) and result.get("ok") is False:
        if result.get("not_deployed"):
            return f"{tool}: not deployed on this oracle (404)"
        subject = result.get("subject") or "subject"
        return f"{tool}: {subject} not found ({result.get('detail') or '404'})"
    return None


def _entity_word(row: dict) -> str:
    label = str(row.get("label") or row.get("urn") or "?")
    state = row.get("state")
    return label + (f" [{state}]" if state else "")


def _summarize_entities(args: dict, result: Any) -> str:
    missing = _not_answered("city_query_entities", result)
    if missing:
        return missing
    rows = [r for r in (result.get("entities") or []) if isinstance(r, dict)] \
        if isinstance(result, dict) else []
    if not rows:
        typed = result.get("match") if isinstance(result, dict) else None
        match = f" matching '{typed}'" if typed else ""
        return f"city_query_entities: no entities{match} in the city picture"
    kinds: dict[str, int] = {}
    for row in rows:
        kinds[str(row.get("kind"))] = kinds.get(str(row.get("kind")), 0) + 1
    emulated = sum(1 for r in rows if r.get("source") == "emulated")
    return (
        "city_query_entities: " + ", ".join(f"{n} {k}" for k, n in sorted(kinds.items()))
        + f" — e.g. {'; '.join(_entity_word(r) for r in rows[:3])}"
        + (f" ({emulated} EMULATED)" if emulated else "")
    )


def _summarize_dossier(args: dict, result: Any) -> str:
    missing = _not_answered("city_get_dossier", result)
    if missing:
        return missing
    entity = result.get("entity") if isinstance(result, dict) else None
    if not isinstance(entity, dict):
        return "city_get_dossier: no entity"
    related = result.get("related_total", len(result.get("related") or []))
    audit = len(result.get("audit") or [])
    return (
        f"city_get_dossier: {entity.get('kind')} {_entity_word(entity)} "
        f"({entity.get('source')}) — {related} related, {audit} audit row(s)"
    )


def _summarize_feed(args: dict, result: Any) -> str:
    missing = _not_answered("city_feed", result)
    if missing:
        return missing
    items = [i for i in (result.get("items") or []) if isinstance(i, dict)] \
        if isinstance(result, dict) else []
    if not items:
        return "city_feed: no city feed items in the window"
    newest = items[0]
    return (
        f"city_feed: {len(items)} item(s); newest {newest.get('kind')}: "
        f"{str(newest.get('text'))[:120]}"
    )


def _summarize_signals(args: dict, result: Any) -> str:
    missing = _not_answered("get_signal_state", result)
    if missing:
        return missing
    rows = [j for j in (result.get("junctions") or []) if isinstance(j, dict)] \
        if isinstance(result, dict) else []
    all_red = sum(1 for j in rows if (j.get("phase") or {}).get("all_red"))
    band = result.get("city_band") if isinstance(result, dict) else None
    band_text = band.get("band") if isinstance(band, dict) else band
    return (
        f"get_signal_state: {result.get('count', len(rows))} programmed junction(s), "
        f"{all_red} in all-red now" + (f"; city band {band_text}" if band_text else "")
    )


def _summarize_units(args: dict, result: Any) -> str:
    missing = _not_answered("list_units", result)
    if missing:
        return missing
    rows = [u for u in (result.get("units") or []) if isinstance(u, dict)] \
        if isinstance(result, dict) else []
    if not rows:
        return "list_units: no units fielded right now (EMULATED feed)"
    by_status: dict[str, int] = {}
    for u in rows:
        by_status[str(u.get("status"))] = by_status.get(str(u.get("status")), 0) + 1
    counts = ", ".join(f"{n} {k}" for k, n in sorted(by_status.items()))
    return f"list_units: {counts} (EMULATED units)"


def _summarize_dispatches(args: dict, result: Any) -> str:
    missing = _not_answered("list_dispatches", result)
    if missing:
        return missing
    rows = [d for d in (result.get("dispatches") or []) if isinstance(d, dict)] \
        if isinstance(result, dict) else []
    if not rows:
        return "list_dispatches: no dispatch records"
    pending = sum(1 for d in rows if d.get("status") == "requested")
    return f"list_dispatches: {len(rows)} dispatch(es), {pending} awaiting a named officer"


def _summarize_interception(args: dict, result: Any) -> str:
    missing = _not_answered("get_interception_plan", result)
    if missing:
        return missing
    if not isinstance(result, dict):
        return "get_interception_plan: no plan"
    sighting = result.get("latest_sighting") or {}
    age = result.get("sighting_age_sec")
    chokes = [c for c in (result.get("chokes") or []) if isinstance(c, dict)]
    first = chokes[0] if chokes else {}
    return (
        f"get_interception_plan: latest read at {sighting.get('camera_id') or '—'}"
        + (f", {age / 60:.0f} min old" if isinstance(age, (int, float)) else "")
        + (" (STALE)" if result.get("stale") else "")
        + f"; {len(chokes)} ranked choke(s)"
        + (f", first {first.get('name') or first.get('urn')}" if first else "")
    )


def _summarize_situation(args: dict, result: Any) -> str:
    missing = _not_answered("get_situation_log", result)
    if missing:
        return missing
    rows = [e for e in (result.get("entries") or []) if isinstance(e, dict)] \
        if isinstance(result, dict) else []
    if not rows:
        return "get_situation_log: no situation rows"
    newest = rows[0]
    return (
        f"get_situation_log: band {newest.get('band')} ({newest.get('code')}), "
        f"{sum(1 for e in rows if e.get('transitioned'))} transition(s) in {len(rows)} row(s)"
    )


def _summarize_recommendations(args: dict, result: Any) -> str:
    missing = _not_answered("list_enforcement_recommendations", result)
    if missing:
        return missing
    rows = [r for r in (result.get("recommendations") or []) if isinstance(r, dict)] \
        if isinstance(result, dict) else []
    status = result.get("status", "pending") if isinstance(result, dict) else "pending"
    if not rows:
        return f"list_enforcement_recommendations: no {status} recommendations"
    rules: dict[str, int] = {}
    for r in rows:
        rules[str(r.get("rule_id"))] = rules.get(str(r.get("rule_id")), 0) + 1
    return (
        f"list_enforcement_recommendations: {len(rows)} {status} — "
        + ", ".join(f"{n} {k}" for k, n in sorted(rules.items()))
        + " (a named officer decides each)"
    )


def _summarize_canvas(args: dict, result: Any) -> str:
    directive = result.get("directive") if isinstance(result, dict) else None
    if isinstance(directive, dict):
        return (
            f"{directive.get('verb')}: proposed '{directive.get('label')}' — "
            "the operator applies or dismisses it"
        )
    return "canvas directive: none"


_SUMMARIZERS = {
    "get_live_traffic": _summarize_live,
    "predict_traffic_flow": _summarize_forecast,
    "find_convoy_routes": _summarize_routes,
    "query_segments_in_bbox": _summarize_bbox,
    "query_segment_details": _summarize_segment,
    "plan_diversions": _summarize_diversions,
    "evaluate_scenarios": _summarize_scenarios,
    "predict_eta": _summarize_eta,
    "query_shortest_path": _summarize_path,
    "query_k_shortest_paths": _summarize_ksp,
    "get_active_corridor_events": _summarize_events,
    "get_historical_pattern": _summarize_history,
    "city_query_entities": _summarize_entities,
    "city_get_dossier": _summarize_dossier,
    "city_feed": _summarize_feed,
    "get_signal_state": _summarize_signals,
    "list_units": _summarize_units,
    "list_dispatches": _summarize_dispatches,
    "get_interception_plan": _summarize_interception,
    "get_situation_log": _summarize_situation,
    "list_enforcement_recommendations": _summarize_recommendations,
    "canvas_fly_to": _summarize_canvas,
    "canvas_track_entity": _summarize_canvas,
    "canvas_highlight": _summarize_canvas,
    "canvas_annotate": _summarize_canvas,
    "canvas_set_sensor": _summarize_canvas,
    "canvas_toggle_layer": _summarize_canvas,
}
