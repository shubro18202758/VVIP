"""City fusion feed — one time-ordered narrative of the city (contract C-C).

    GET /api/v1/city/feed?since=&limit=&kinds=
    GET /api/v1/events/{id}/audit?limit=&since=

Every row is a FeedItem ``{id, t, kind, urns, actor, actor_kind, text,
source, severity, props}`` unioned from the audit sources that already
exist — nothing is synthesized that did not happen:

    kind         source (table / key)                        actor_kind
    event        events.event_status_log (013); events        human | system
                 created outside the API surface once, from
                 corridor_events.created_at
    enforcement  enforcement_recommendations.audit_tail       machine (rules)
                 (creation + every human decision)            human (decision)
    anpr         vehicle_sightings with matched_plate_hit,    machine (camera)
                 only while lawful to surface (C6, below)
    dispatch     corridor.dispatch_events                     human | system
    signals      corridor.signal_plan_log, one row per        human
                 committed apply (013); signal_program_history
                 restores (+ first-write snapshots of applies
                 made before the plan ledger), one row per
                 write transaction
    ama          adaptive.situation_log band transitions;     machine
                 adaptive.param_overrides when the params are
                 non-baseline or the shadow verdict changed
    brain        convoy-brain decisions_log (Valkey           machine
                 convoy:context:*, batched MGET) — in the
                 city-wide feed only for movements fused into
                 the city picture (a corridor event exists for
                 the movement); planner sessions that never
                 became an operation stay in the brain's own
                 ledger (/ops/degradation-taxonomy) and are
                 counted as ``unlinked_movements``

Ids are stable (``<table>:<key>``) so a poller dedupes by id.  A source
that fails is reported in ``sources`` and the rest of the narrative is
still served — degradation is visible, never a blank 500.

Scoping (dossiers, the per-event audit): a :class:`FeedScope` filters
every source IN SQL through its own keys (event, movement, vehicle,
recommendation, sighting, dispatch, unit, junction, camera), so an
entity's audit is complete however old it is — not a window of the
global feed filtered after the fact.

Plates are masked in every ``text`` (city/common.feed_item); the raw
plate rides only in ``props.plate`` for the officer-named deck (C6).

Lawful basis (C6): an ANPR hit is narrated only while its vehicle holds
an ACTIVE earmark (expiry enforced in the query path) or when the read is
the evidence of an enforcement recommendation — ``SQL_LAWFUL_READ``, the
same predicate the camera and sighting dossiers use.  A cleared or lapsed
earmark withdraws the camera-by-camera trail from every scope at once.

Provenance: every item carries ``source`` live|emulated.  Event, dispatch
and signal items take it from their corridor event's metadata; band
transitions, parameter publishes and brain decisions inherit the badge of
the event(s) their subject denotes (``ResolvedSubjects.source_of``), so an
emulated scenario never shows a LIVE row.  Input provenance (C-Q) rides
beside the badge: a band transition or a brain decision that stood on the
simulated feed (or emulated inputs) carries ``props.simulated_feed: true``
and ``props.data_quality`` and its text ends ' · simulated feed'; a
dispatch row attributed through the deck-verified header carries
``props.actor_source`` and, when the body named someone else,
``props.claimed_officer``.

Stolen vehicles (C-N): an R_STOLEN_SIGHTING row is narrated as a stolen
vehicle sighted with the e-challan WITHHELD — never as a challan — and
its human decision as a pursuit authorised (approve) or the sighting
dismissed (reject).  Route updates (C-O) are ``event_status_log`` rows
whose from and to status are equal; they read 'Route updated'.

Wire time: ``t`` is millisecond-precision (contract C-C), so ``since`` is
compared as ``t_wire > since`` (common.after_wire_ms) — a poller that
echoes the newest ``t`` back never receives that item again.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid as _uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import structlog

from traffic_oracle.city.common import (
    FEED_KINDS,
    SQL_LAWFUL_READ,
    ResolvedSubjects,
    actor_kind_of,
    after_wire_ms,
    as_dict,
    as_list,
    as_uuid,
    event_source,
    feed_item,
    finite,
    label_source,
    mask_plate,
    mask_plates_in_text,
    parse_ts,
    relation_exists,
    resolve_subjects,
    subject_urns,
    urn,
)
from traffic_oracle.enforcement.engine import SQL_STOLEN_EARMARK
from traffic_oracle.enforcement.rules import CHALLAN_WITHHELD_TEXT, is_penal
from traffic_oracle.ops.router import (
    context_key,
    decisions_of,
    read_contexts,
    scan_contexts,
)
from traffic_oracle.units.agencies import agency_label

logger = structlog.get_logger(__name__)

#: Default look-back of the GLOBAL feed when no ``since`` is given (scoped
#: audits read their entity's whole history).
GLOBAL_LOOKBACK = timedelta(days=30)

#: param_overrides: how far before ``since`` the previous publish of a
#: subject is looked up to decide whether its shadow verdict CHANGED.
VERDICT_LOOKBACK = timedelta(hours=6)

#: The implicit verdict before a subject's first publish (baseline params,
#: no shadow-eval required) — a first publish equal to it is not news.
_RESTING_VERDICT = "baseline|unavailable|shadow_eval_not_required"

#: Brain context scan cache (global feed only): the whole keyspace is read
#: at most once per this many seconds per Valkey client.
BRAIN_SCAN_TTL_SEC = 2.0

# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


@dataclass
class FeedScope:
    """Keys an audit is filtered to (any match includes the row)."""

    event_ids: list[str] = field(default_factory=list)
    movement_ids: list[str] = field(default_factory=list)
    vehicle_ids: list[str] = field(default_factory=list)
    recommendation_ids: list[str] = field(default_factory=list)
    sighting_ids: list[str] = field(default_factory=list)
    dispatch_ids: list[str] = field(default_factory=list)
    unit_ids: list[str] = field(default_factory=list)
    junction_ids: list[int] = field(default_factory=list)
    camera_ids: list[str] = field(default_factory=list)

    def uuids(self, name: str) -> list[_uuid.UUID]:
        out: list[_uuid.UUID] = []
        for v in getattr(self, name):
            u = as_uuid(v)
            if u is not None and u not in out:
                out.append(u)
        return out

    def texts(self, name: str) -> list[str]:
        return list(dict.fromkeys(str(v) for v in getattr(self, name) if v))

    def ints(self, name: str) -> list[int]:
        return list(dict.fromkeys(int(v) for v in getattr(self, name) if v is not None))

    def has_any(self, *names: str) -> bool:
        return any(getattr(self, n) for n in names)


class SourceUnavailableError(RuntimeError):
    """The source is not wired in this process (no Valkey, no 013 ledger)."""


# ---------------------------------------------------------------------------
# Read SQL (module constants — parameter-bound, read-only).  Every source
# takes ($1 since, $2 limit, $3 unscoped flag, $4.. scope arrays).
# ---------------------------------------------------------------------------

_SQL_FEED_EVENT_LOG = "SELECT l.seq, l.event_id, l.from_status, l.to_status, l.actor, l.note, l.at, e.event_kind, e.title, e.metadata FROM events.event_status_log l JOIN events.corridor_events e ON e.event_id = l.event_id WHERE l.at > $1::timestamptz AND ($3::boolean OR l.event_id = ANY($4::uuid[])) ORDER BY l.at DESC, l.seq DESC LIMIT $2"  # noqa: E501

#: Events with no creation ledger row (created before 013, or written
#: straight to the table by the emulator) — opened once, at created_at.
_SQL_FEED_EVENT_CREATED = "SELECT e.event_id, e.event_kind, e.title, e.metadata, e.created_at FROM events.corridor_events e WHERE e.created_at > $1::timestamptz AND ($3::boolean OR e.event_id = ANY($4::uuid[])) AND NOT EXISTS (SELECT 1 FROM events.event_status_log l WHERE l.event_id = e.event_id AND l.from_status IS NULL) ORDER BY e.created_at DESC LIMIT $2"  # noqa: E501

#: ``stolen_vehicle``: the recommendation's vehicle holds an ACTIVE
#: stolen-vehicle earmark right now (C-N — its e-challans are withheld).
_SQL_FEED_ENFORCEMENT = "SELECT r.recommendation_id, r.vehicle_id, r.sighting_id, r.rule_id, r.status, r.evidence, r.audit_tail, s.camera_id, s.source AS sighting_source, s.plate, " + SQL_STOLEN_EARMARK + " AS stolen_vehicle FROM events.enforcement_recommendations r LEFT JOIN corridor.vehicle_sightings s ON s.sighting_id = r.sighting_id WHERE GREATEST(r.created_at, COALESCE(r.decided_at, r.created_at)) > $1::timestamptz AND ($3::boolean OR r.recommendation_id = ANY($4::uuid[]) OR r.vehicle_id = ANY($5::uuid[]) OR r.sighting_id = ANY($6::uuid[])) ORDER BY GREATEST(r.created_at, COALESCE(r.decided_at, r.created_at)) DESC LIMIT $2"  # noqa: E501

#: ANPR hits that are lawful to surface (C6 predicate, common.py) — in the
#: global feed and in every scoped audit alike.
_SQL_FEED_ANPR = "SELECT s.sighting_id, s.vehicle_id, s.plate, s.camera_id, s.t, s.speed_kmh, s.heading, s.source, s.run_id, s.matched_plate_hit FROM corridor.vehicle_sightings s WHERE s.matched_plate_hit IS NOT NULL AND s.t > $1::timestamptz AND s.t <= NOW() AND ($3::boolean OR s.vehicle_id = ANY($4::uuid[]) OR s.camera_id = ANY($5::text[]) OR s.sighting_id = ANY($6::uuid[])) AND " + SQL_LAWFUL_READ + " ORDER BY s.t DESC LIMIT $2"  # noqa: E501

#: ``to_jsonb(de) -> 'detail'`` is the 014 actor provenance — NULL (never
#: an error) on a store that predates the column.
_SQL_FEED_DISPATCH = "SELECT de.seq, de.dispatch_id, de.from_status, de.to_status, de.actor, de.note, de.at, to_jsonb(de) -> 'detail' AS detail, d.event_id, d.agency, d.unit_id, d.waypoint_junction, ev.title AS event_title, ev.metadata AS event_metadata FROM corridor.dispatch_events de JOIN corridor.dispatches d ON d.dispatch_id = de.dispatch_id LEFT JOIN events.corridor_events ev ON ev.event_id = d.event_id WHERE de.at > $1::timestamptz AND ($3::boolean OR d.event_id = ANY($4::uuid[]) OR de.dispatch_id = ANY($5::uuid[]) OR d.unit_id = ANY($6::text[]) OR d.waypoint_junction = ANY($7::bigint[])) ORDER BY de.at DESC, de.seq DESC LIMIT $2"  # noqa: E501

#: One row per committed city signal-plan apply (013 plan ledger).
_SQL_FEED_SIGNAL_PLANS = "SELECT p.plan_id, p.event_id, p.band, p.junction_ids, p.snapshots_written, p.shadow_eval, p.actor, p.applied_at FROM corridor.signal_plan_log p WHERE p.applied_at > $1::timestamptz AND ($3::boolean OR p.event_id = ANY($4::uuid[]) OR p.junction_ids && $5::bigint[]) ORDER BY p.applied_at DESC, p.plan_id DESC LIMIT $2"  # noqa: E501

#: One row per signal-write TRANSACTION of the C7 history ledger (all rows
#: of one apply/restore share captured_at = the transaction clock):
#: restores, and the first-write snapshots of applies made BEFORE the plan
#: ledger existed.  A snapshot group whose apply has its own plan-ledger
#: row (same transaction clock) is narrated by that row instead.
_SQL_FEED_SIGNALS = "SELECT min(h.history_id) AS first_id, h.captured_at, h.event_id, h.actor, array_agg(DISTINCT h.kind) AS kinds, array_agg(DISTINCT h.junction_id) AS junction_ids, min(h.reason) AS reason FROM corridor.signal_program_history h WHERE h.captured_at > $1::timestamptz AND ($3::boolean OR h.event_id = ANY($4::uuid[]) OR h.junction_id = ANY($5::bigint[])) AND NOT (h.kind = 'snapshot' AND EXISTS (SELECT 1 FROM corridor.signal_plan_log p WHERE p.applied_at = h.captured_at)) GROUP BY h.captured_at, h.event_id, h.actor ORDER BY h.captured_at DESC LIMIT $2"  # noqa: E501

#: Band transitions with the band they left (partial index, migration 013)
#: and what each stood on (the logged inputs: simulated feed / quality —
#: the boolean cast is guarded so one odd row never fails the source).
_SQL_FEED_SITUATIONS = "SELECT x.log_id, x.movement_id, x.situation_class, x.prev_class, x.situation_code, x.severity_score, x.protection_class, x.observed_at, x.inputs_source, x.simulated_feed, x.data_quality, x.inputs_origin FROM (SELECT l.log_id, l.movement_id, l.situation_class, l.situation_code, l.severity_score, l.protection_class, l.observed_at, l.inputs->>'source' AS inputs_source, CASE WHEN jsonb_typeof(l.inputs->'simulated_feed') = 'boolean' THEN (l.inputs->>'simulated_feed')::boolean END AS simulated_feed, l.inputs->>'data_quality' AS data_quality, l.inputs->>'inputs_origin' AS inputs_origin, LAG(l.situation_class) OVER (PARTITION BY l.movement_id ORDER BY l.observed_at, l.log_id) AS prev_class FROM adaptive.situation_log l WHERE l.transitioned AND ($3::boolean OR l.movement_id = ANY($4::text[]))) x WHERE x.observed_at > $1::timestamptz ORDER BY x.observed_at DESC LIMIT $2"  # noqa: E501

#: Published params that are non-baseline, or whose shadow verdict
#: (source|shadow_eval|detail) differs from the subject's previous publish.
_SQL_FEED_OVERRIDES = "SELECT x.override_id, x.movement_id, x.situation_class, x.source, x.shadow_eval, x.detail, x.fail_closed_reason, x.floor_applied, x.advance_closure_sec, x.protection_class, x.published_at, x.prev_verdict FROM (SELECT p.override_id, p.movement_id, p.situation_class, p.source, p.shadow_eval, p.evidence->>'shadow_eval_detail' AS detail, p.evidence->>'fail_closed_reason' AS fail_closed_reason, p.evidence->>'floor_applied' AS floor_applied, p.params->>'advance_closure_sec' AS advance_closure_sec, p.protection_class, p.published_at, LAG(p.source || '|' || p.shadow_eval || '|' || COALESCE(p.evidence->>'shadow_eval_detail', '')) OVER (PARTITION BY p.movement_id ORDER BY p.published_at, p.override_id) AS prev_verdict FROM adaptive.param_overrides p WHERE p.published_at > $1::timestamptz - $5::interval AND ($3::boolean OR p.movement_id = ANY($4::text[]))) x WHERE x.published_at > $1::timestamptz AND (x.source <> 'baseline' OR COALESCE(x.prev_verdict, $6::text) <> (x.source || '|' || x.shadow_eval || '|' || COALESCE(x.detail, ''))) ORDER BY x.published_at DESC LIMIT $2"  # noqa: E501

#: The event an audit is about (+ its dispatch keys).
_SQL_AUDIT_EVENT = "SELECT e.event_id, e.movement_id, e.metadata FROM events.corridor_events e WHERE e.event_id = $1"  # noqa: E501

_SQL_AUDIT_EVENT_DISPATCHES = "SELECT d.dispatch_id FROM corridor.dispatches d WHERE d.event_id = $1"  # noqa: E501


# ---------------------------------------------------------------------------
# Row → FeedItem converters
# ---------------------------------------------------------------------------

_EVENT_VERBS = {
    "approved": "approved",
    "active": "activated",
    "completed": "completed",
    "cancelled": "cancelled",
    "planning": "opened",
}

_HIT_LABELS = {
    "stolen_vehicle": "STOLEN VEHICLE",
    "earmarked_plate": "earmarked plate",
    "watchlist": "watchlist",
    "pending_fine": "pending fine",
}


def _event_links(meta: Mapping[str, Any]) -> list[str]:
    links: list[str] = []
    for key, kind in (("vehicle_id", "vehicle"), ("recommendation_id", "recommendation")):
        value = meta.get(key)
        if isinstance(value, str) and as_uuid(value) is not None:
            links.append(urn(kind, str(as_uuid(value))))
    return links


def event_log_item(r: Mapping[str, Any]) -> dict:
    meta = as_dict(r["metadata"])
    eid = str(r["event_id"])
    title = r["title"]
    to_status = r["to_status"]
    verb = _EVENT_VERBS.get(to_status, to_status)
    # from == to is never a lifecycle move (the table forbids X → X): it is
    # the ledger row of a route update (POST /events/{id}/routes, C-O)
    route_update = r["from_status"] is not None and r["from_status"] == to_status
    if r["from_status"] is None:
        text = f"Event opened: {title} ({r['event_kind']}) by {r['actor']}"
    elif route_update:
        text = f"Route updated: {title} by {r['actor']}"
    else:
        text = (
            f"Event {verb}: {title} ({r['from_status']} → {to_status}) by {r['actor']}"
        )
    if r["note"] and r["from_status"] is not None:
        text += f" — {r['note']}"
    pursuit = r["event_kind"] == "vehicle_pursuit"
    severity = "warn" if to_status == "cancelled" or (pursuit and to_status in ("planning", "active")) else "info"  # noqa: E501
    if route_update:
        severity = "warn"  # the operation now runs on a different route
    return feed_item(
        f"event_status_log:{r['seq']}",
        r["at"],
        "event",
        [urn("event", eid), *_event_links(meta)],
        actor=r["actor"],
        actor_kind=actor_kind_of(r["actor"]),
        text=text,
        source=event_source(meta),
        severity=severity,
        props={
            "event_id": eid,
            "event_kind": r["event_kind"],
            "from_status": r["from_status"],
            "to_status": to_status,
            "note": r["note"],
            # additive (C-O): the row narrates a route update
            "action": "route_update" if route_update else None,
        },
    )


def event_created_item(r: Mapping[str, Any]) -> dict:
    meta = as_dict(r["metadata"])
    eid = str(r["event_id"])
    actor = None
    for key in ("opened_by", "source"):
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            actor = value.strip()[:64]
            break
    # No recorded opener (written straight to the table, not through the
    # API): a SYSTEM row — never presented as an anonymous human.
    kind = actor_kind_of(actor, default="system")
    if meta.get("source") == "event-emulator":
        kind = "system"
    text = f"Event opened: {r['title']} ({r['event_kind']})" + (f" by {actor}" if actor else "")
    return feed_item(
        f"corridor_events:{eid}:created",
        r["created_at"],
        "event",
        [urn("event", eid), *_event_links(meta)],
        actor=actor,
        actor_kind=kind,
        text=text,
        source=event_source(meta),
        severity="warn" if r["event_kind"] == "vehicle_pursuit" else "info",
        props={"event_id": eid, "event_kind": r["event_kind"], "to_status": "planning",
               "from_status": None, "synthesized_from": "corridor_events.created_at"},
    )


def _num(value: Any) -> str | None:
    f = finite(value)
    return f"{f:.0f}" if f is not None else None


def enforcement_items(r: Mapping[str, Any], since: datetime) -> list[dict]:
    """Every audit_tail entry of one recommendation newer than ``since``."""
    evidence = as_dict(r["evidence"])
    plate = r["plate"] or evidence.get("plate")
    plate_masked = mask_plate(plate) or "vehicle"
    rid = str(r["recommendation_id"])
    urns = [urn("recommendation", rid), urn("vehicle", str(r["vehicle_id"]))]
    if r["sighting_id"] is not None:
        urns.append(urn("sighting", str(r["sighting_id"])))
    if r["camera_id"]:
        urns.append(urn("camera", r["camera_id"]))
    speed, limit = _num(evidence.get("speed_kmh")), _num(evidence.get("speed_limit_kmh"))
    detail = f" — {speed} km/h in a {limit} km/h zone" if speed and limit else ""
    road = evidence.get("road_name")
    where = f" on {road}" if isinstance(road, str) and road else ""
    source = label_source(r["sighting_source"] or evidence.get("source"))
    sighting_rule = r["rule_id"] == "R_STOLEN_SIGHTING"
    stolen = bool(r.get("stolen_vehicle"))
    # a still-PENDING penal row of a vehicle reported stolen (created before
    # its FIR earmark) can only be rejected now — its challan is withheld; a
    # row decided before the rule existed keeps its decision as recorded
    withheld = sighting_rule or (is_penal(r["rule_id"]) and stolen and r["status"] == "pending")
    fir = evidence.get("fir_ref")
    fir_text = f" (FIR {fir})" if isinstance(fir, str) and fir else ""
    camera = r["camera_id"] or evidence.get("camera_id") or "a camera"
    out: list[dict] = []
    for n, entry in enumerate(as_list(r["audit_tail"]), start=1):
        if not isinstance(entry, Mapping):
            continue
        at = parse_ts(entry.get("at"))
        if at is None or at <= since:
            continue
        action = str(entry.get("action") or "")
        actor = entry.get("actor") if isinstance(entry.get("actor"), str) else None
        note = entry.get("note") if isinstance(entry.get("note"), str) else ""
        human = action.startswith("decision:")
        verdict = action.split(":", 1)[1] if human else ""
        if sighting_rule and action.startswith("created"):
            text = (
                f"Stolen vehicle {plate_masked}{fir_text} sighted at {camera}{where} — "
                "e-challan withheld, vehicle reported stolen; awaiting a duty officer"
            )
            severity = "warn"
        elif sighting_rule and human:
            text = (
                f"Stolen-vehicle sighting of {plate_masked} confirmed by {actor} — "
                "pursuit authorised; e-challan withheld"
                if verdict == "approved"
                else f"Stolen-vehicle sighting of {plate_masked} dismissed by {actor}"
            )
            if note:
                text += f": {note}"
            severity = "info"
        elif action.startswith("created"):
            text = (
                f"e-challan recommended ({r['rule_id']}) for {plate_masked}{detail}{where}"
                " — awaiting a human decision"
            )
            if withheld and r["status"] == "pending":
                text += f"; now {CHALLAN_WITHHELD_TEXT}"
            severity = "warn"
        elif human:
            text = f"e-challan {verdict.upper()} by {actor} ({r['rule_id']}, {plate_masked})"
            if note:
                text += f": {note}"
            severity = "info"
        else:
            text = f"{action} ({r['rule_id']}, {plate_masked})"
            severity = "info"
        out.append(
            feed_item(
                f"enforcement_recommendations:{rid}:{n}",
                at,
                "enforcement",
                urns,
                actor=actor,
                actor_kind="human" if human else actor_kind_of(actor, default="machine"),
                text=text,
                source=source,
                severity=severity,
                props={
                    "recommendation_id": rid,
                    "rule_id": r["rule_id"],
                    "action": action,
                    "status": r["status"],
                    "note": note or None,
                    "plate": plate,
                    "plate_masked": plate_masked,
                    # additive (C-N)
                    "challan_withheld": withheld,
                    "withheld_reason": "vehicle reported stolen" if withheld else None,
                    "vehicle_reported_stolen": stolen or sighting_rule,
                },
            )
        )
    return out


def anpr_item(r: Mapping[str, Any]) -> dict:
    hit = r["matched_plate_hit"]
    plate_masked = mask_plate(r["plate"])
    speed = _num(r["speed_kmh"])
    text = f"ANPR hit · {_HIT_LABELS.get(hit, hit)} · {plate_masked} at {r['camera_id']}"
    if speed:
        text += f" ({speed} km/h)"
    return feed_item(
        f"vehicle_sightings:{r['sighting_id']}",
        r["t"],
        "anpr",
        [
            urn("sighting", str(r["sighting_id"])),
            urn("vehicle", str(r["vehicle_id"])),
            urn("camera", r["camera_id"]),
        ],
        actor=r["camera_id"],
        actor_kind="machine",
        text=text,
        source=label_source(r["source"]),
        severity="critical" if hit == "stolen_vehicle" else "warn",
        props={
            "hit": hit,
            "camera_id": r["camera_id"],
            "plate": r["plate"],
            "plate_masked": plate_masked,
            "speed_kmh": finite(r["speed_kmh"]),
            "heading": finite(r["heading"]),
            "run_id": r["run_id"],
        },
    )


def dispatch_item(r: Mapping[str, Any]) -> dict:
    who = f"{agency_label(r['agency'])} {r['unit_id']}"
    wp = r["waypoint_junction"]
    where = f" → J{int(wp)}" if wp else ""
    actor = r["actor"]
    if r["from_status"] is None:
        title = mask_plates_in_text(r["event_title"]) if r["event_title"] else "an event"
        text = f"{who}{where} requested for {title} by {actor}"
    else:
        text = f"{who}{where}: {r['from_status']} → {r['to_status']} by {actor}"
    if r["note"]:
        text += f" — {r['note']}"
    urns = [
        urn("dispatch", str(r["dispatch_id"])),
        urn("event", str(r["event_id"])),
        urn("unit", r["unit_id"]),
    ]
    if wp:
        urns.append(urn("junction", int(wp)))
    # the 014 actor provenance (null on body-attributed and pre-014 rows)
    detail = as_dict(r.get("detail"))
    return feed_item(
        f"dispatch_events:{r['seq']}",
        r["at"],
        "dispatch",
        urns,
        actor=actor,
        actor_kind=actor_kind_of(actor),
        text=text,
        source=event_source(as_dict(r["event_metadata"])),
        severity="warn" if r["to_status"] == "cancelled" else "info",
        props={
            "dispatch_id": str(r["dispatch_id"]),
            "agency": r["agency"],
            "unit_id": r["unit_id"],
            "from_status": r["from_status"],
            "to_status": r["to_status"],
            "waypoint_junction": int(wp) if wp else None,
            "note": r["note"],
            # additive (WP-19): x-veer-officer when the deck's verified
            # session named the actor, null when the body did
            "actor_source": detail.get("actor_source"),
            "claimed_officer": detail.get("claimed_officer"),
        },
    )


def signals_item(r: Mapping[str, Any], resolved: ResolvedSubjects) -> dict:
    """One C7 history write transaction (restore, or a pre-ledger apply)."""
    kinds = sorted(str(k) for k in (r["kinds"] or []))
    junctions = sorted(int(j) for j in (r["junction_ids"] or []))
    n = len(junctions)
    actor = r["actor"]
    by = f" by {actor}" if actor else " (unattributed — written before officer attribution)"
    if "restore" in kinds:
        text = f"Signal programs restored to pre-calibration at {n} junction(s){by}"
        severity = "info"
    else:
        text = (
            f"City signal plan committed{by} — first calibrated write at {n} "
            "junction(s); prior programs snapshotted for restore"
        )
        severity = "warn"
    urns = [urn("junction", j) for j in junctions]
    event_id = str(r["event_id"]) if r["event_id"] is not None else None
    if event_id:
        urns.insert(0, urn("event", event_id))
    return feed_item(
        f"signal_program_history:{r['first_id']}",
        r["captured_at"],
        "signals",
        urns,
        actor=actor,
        actor_kind="human",  # only an operator request writes this ledger
        text=text,
        source=resolved.source_of(event_id) if event_id else "live",
        severity=severity,
        props={"kinds": kinds, "junction_ids": junctions, "reason": r["reason"]},
    )


def signal_plan_item(r: Mapping[str, Any], resolved: ResolvedSubjects) -> dict:
    """One committed city signal-plan apply (013 plan ledger)."""
    junctions = sorted(int(j) for j in (r["junction_ids"] or []))
    actor = r["actor"]
    by = f" by {actor}" if actor else " (unattributed — no officer named)"
    snaps = int(r["snapshots_written"] or 0)
    text = f"City signal plan {r['band']} committed{by} at {len(junctions)} junction(s)"
    if snaps:
        text += f"; {snaps} prior program(s) snapshotted for restore"
    text += f" — twin shadow-eval {r['shadow_eval']}"
    event_id = str(r["event_id"]) if r["event_id"] is not None else None
    urns = [urn("junction", j) for j in junctions]
    if event_id:
        urns.insert(0, urn("event", event_id))
    return feed_item(
        f"signal_plan_log:{r['plan_id']}",
        r["applied_at"],
        "signals",
        urns,
        actor=actor,
        actor_kind="human",  # only an operator request commits a city plan
        text=text,
        source=resolved.source_of(event_id) if event_id else "live",
        severity="warn",
        props={
            "kinds": ["apply"],
            "band": r["band"],
            "junction_ids": junctions,
            "snapshots_written": snaps,
            "shadow_eval": r["shadow_eval"],
            "event_id": event_id,
        },
    )


SIMULATED_FEED_SUFFIX = " · simulated feed"


def situation_item(r: Mapping[str, Any], resolved: ResolvedSubjects) -> dict:
    cls = r["situation_class"]
    prev = r["prev_class"]
    arrow = f"{prev} → {cls}" if prev else f"→ {cls}"
    text = f"AMA band {arrow} (severity {r['severity_score']}, {r['situation_code']})"
    # what the band stood on (the logged inputs; null on older rows)
    simulated = r.get("simulated_feed")
    simulated = simulated if isinstance(simulated, bool) else None
    if simulated:
        text += SIMULATED_FEED_SUFFIX
    severity = {"RED": "critical", "BLACK": "critical", "AMBER": "warn"}.get(cls, "info")
    return feed_item(
        f"situation_log:{r['log_id']}",
        r["observed_at"],
        "ama",
        subject_urns(r["movement_id"], resolved),
        actor="AMA situation engine",
        actor_kind="machine",
        text=text,
        source=resolved.source_of(r["movement_id"]),
        severity=severity,
        props={
            "subject": r["movement_id"],
            "situation_class": cls,
            "prev_class": prev,
            "situation_code": r["situation_code"],
            "severity_score": r["severity_score"],
            "protection_class": r["protection_class"],
            # additive (C-Q): input provenance of the band
            "simulated_feed": simulated,
            "data_quality": r.get("data_quality"),
            "inputs_source": r.get("inputs_source"),
            "inputs_origin": r.get("inputs_origin"),
        },
    )


def override_item(r: Mapping[str, Any], resolved: ResolvedSubjects) -> dict:
    cls = r["situation_class"]
    detail = r["detail"] or ""
    adv = _num(r["advance_closure_sec"])
    fail_closed = bool(r["fail_closed_reason"]) or (
        r["source"] == "baseline" and detail not in ("", "shadow_eval_not_required")
    )
    if r["source"] != "baseline":
        text = f"AMA published {r['source']} parameters at {cls} (shadow-eval {r['shadow_eval']})"
    elif fail_closed:
        text = (
            f"AMA held baseline at {cls}: twin shadow-eval {r['shadow_eval']} "
            f"({detail}) — failed closed"
        )
    elif detail == "shadow_eval_not_required":
        text = f"AMA back to baseline parameters at {cls} (no shadow-eval required)"
    else:
        text = f"AMA shadow verdict {r['shadow_eval']} ({detail}) at {cls}"
    if adv:
        text += f"; advance closure {adv} s"
    return feed_item(
        f"param_overrides:{r['override_id']}",
        r["published_at"],
        "ama",
        subject_urns(r["movement_id"], resolved),
        actor="AMA policy",
        actor_kind="machine",
        text=text,
        source=resolved.source_of(r["movement_id"]),
        severity="warn" if fail_closed else "info",
        props={
            "subject": r["movement_id"],
            "situation_class": cls,
            "params_source": r["source"],
            "shadow_eval": r["shadow_eval"],
            "shadow_eval_detail": r["detail"],
            "fail_closed_reason": r["fail_closed_reason"],
            "floor_applied": r["floor_applied"] == "true" if r["floor_applied"] else None,
            "previous_verdict": r["prev_verdict"],
        },
    )


def _brain_text(row: Mapping[str, Any]) -> tuple[str, str]:
    """(text, severity) for one decisions_log row, by the row's shape."""
    phase = str(row.get("phase") or "")
    action = row.get("action")
    rtype = row.get("type")
    if isinstance(rtype, str) and rtype and phase != "decider_checkpoint":
        text = f"Operator alert: {rtype}"
        if action:
            text += f" — {action}"
        reason = row.get("reason")
        if isinstance(reason, str) and reason:
            text += f" ({reason})"
        return text, "critical" if rtype == "decider_unavailable" else "warn"
    if phase == "decider_checkpoint":
        tier = row.get("tier") or "unknown"
        text = f"Decider checkpoint ({row.get('trigger') or 'trigger n/a'}): tier {tier}"
        latency = _num(row.get("latency_ms"))
        if latency:
            text += f", {latency} ms"
        healthy = tier in ("decider", "tier0_cache_hit")
        return text, "info" if healthy else "warn"
    if phase == "orchestrator_turn":
        text = f"Orchestrator turn: {action}"
        if row.get("confidence") is not None:
            text += f" (confidence {row.get('confidence')})"
        tiers = row.get("tiers_used")
        if isinstance(tiers, list) and tiers:
            text += " via " + ", ".join(str(t) for t in tiers[:3])
        failed = action in ("max_rounds_exceeded", "timeout", "error")
        return text, "warn" if failed else "info"
    if phase == "pre_movement":
        text = f"Pre-movement plan: {row.get('recommendation') or action or 'recorded'}"
        if row.get("security_compliant") is not None:
            text += " (security compliant)" if row.get("security_compliant") else " (security NOT compliant)"  # noqa: E501
        return text, "info"
    if phase:
        detail = action or row.get("recommendation") or "recorded"
        text = f"{phase.replace('_', ' ').capitalize()}: {detail}"
        if row.get("iterations") is not None:
            text += f" ({row.get('iterations')} iterations)"
        return text, "info"
    return f"Decision recorded: {action or 'n/a'}", "info"


def brain_items(
    movement_id: str, rows: Iterable[Any], resolved: ResolvedSubjects, since: datetime
) -> list[dict]:
    """decisions_log rows of one movement → FeedItems (timestamped only),
    badged like the corridor event(s) the movement belongs to."""
    out: list[dict] = []
    seen: dict[str, int] = {}
    urns = subject_urns(movement_id, resolved, always_convoy=True)
    source = resolved.source_of(movement_id)
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        t = parse_ts(row.get("timestamp"))
        if t is None:
            continue  # a row that cannot be placed in time is not narrated
        digest = hashlib.blake2b(
            json.dumps(row, sort_keys=True, default=str).encode("utf-8"), digest_size=8
        ).hexdigest()
        seen[digest] = seen.get(digest, 0) + 1
        if t <= since:
            continue
        item_id = f"decisions_log:{movement_id}:{digest}"
        if seen[digest] > 1:
            item_id += f":{seen[digest]}"
        text, severity = _brain_text(row)
        simulated = brain_row_simulated(row)
        if simulated:
            text += SIMULATED_FEED_SUFFIX
        tier = row.get("tier")
        if tier is None and isinstance(row.get("tiers_used"), list) and row["tiers_used"]:
            tier = row["tiers_used"][0]
        out.append(
            feed_item(
                item_id,
                t,
                "brain",
                urns,
                actor="convoy-brain",
                actor_kind="machine",
                text=text,
                source=source,
                severity=severity,
                props={
                    "movement_id": movement_id,
                    "phase": row.get("phase"),
                    "type": row.get("type"),
                    "tier": tier,
                    "confidence": row.get("confidence"),
                    "action": row.get("action"),
                    "trigger": row.get("trigger"),
                    # additive (C-Q): the feed the decision stood on
                    "data_quality": row.get("data_quality"),
                    "simulated_feed": simulated,
                },
            )
        )
    return out


def brain_row_simulated(row: Mapping[str, Any]) -> bool | None:
    """Did a brain decision stand on the simulated feed?  Pure.

    True when the row says ``simulated: true`` or its ``data_quality``
    (a list of labels or one string) includes 'synthetic'; False when it
    names its quality and none is synthetic; None when it says nothing
    (planner rows, checkpoints) — unknown, never presumed live.
    """
    if row.get("simulated") is True:
        return True
    quality = row.get("data_quality")
    labels: list[str]
    if isinstance(quality, str):
        labels = [quality]
    elif isinstance(quality, list):
        labels = [str(q) for q in quality]
    else:
        labels = []
    if "synthetic" in labels:
        return True
    if labels or row.get("simulated") is False:
        return False
    return None


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

#: Global-feed scan cache: (client, expires_monotonic, contexts).  Keyed
#: by client IDENTITY so a different client never reads another's scan.
_BRAIN_CACHE: list[Any] = [None, 0.0, []]


def reset_brain_cache() -> None:
    _BRAIN_CACHE[:] = [None, 0.0, []]


class _Feed:
    """One feed request: (since, limit, scope) over one connection."""

    def __init__(
        self,
        conn: Any,
        *,
        since: datetime,
        limit: int,
        scope: FeedScope | None,
        valkey: Any,
    ) -> None:
        self.conn = conn
        self.since = since
        self.limit = limit
        self.scope = scope
        self.valkey = valkey
        #: extra per-source facts surfaced in ``sources`` (visible filtering)
        self.meta: dict[str, dict] = {}

    @property
    def unscoped(self) -> bool:
        return self.scope is None

    def skip(self, *names: str) -> bool:
        """A scoped request with none of this source's keys reads nothing."""
        return self.scope is not None and not self.scope.has_any(*names)

    def _u(self, name: str) -> list[_uuid.UUID]:
        return self.scope.uuids(name) if self.scope is not None else []

    def _t(self, name: str) -> list[str]:
        return self.scope.texts(name) if self.scope is not None else []

    def _i(self, name: str) -> list[int]:
        return self.scope.ints(name) if self.scope is not None else []

    async def event(self) -> list[dict]:
        if self.skip("event_ids"):
            return []
        if not await relation_exists(self.conn, "events.event_status_log"):
            raise SourceUnavailableError("events.event_status_log missing (migration 013)")
        args = (self.since, self.limit, self.unscoped, self._u("event_ids"))
        items = [event_log_item(r) for r in await self.conn.fetch(_SQL_FEED_EVENT_LOG, *args)]
        items += [
            event_created_item(r) for r in await self.conn.fetch(_SQL_FEED_EVENT_CREATED, *args)
        ]
        return items

    async def enforcement(self) -> list[dict]:
        if self.skip("recommendation_ids", "vehicle_ids", "sighting_ids"):
            return []
        rows = await self.conn.fetch(
            _SQL_FEED_ENFORCEMENT,
            self.since,
            self.limit,
            self.unscoped,
            self._u("recommendation_ids"),
            self._u("vehicle_ids"),
            self._u("sighting_ids"),
        )
        out: list[dict] = []
        for r in rows:
            out.extend(enforcement_items(r, self.since))
        return out

    async def anpr(self) -> list[dict]:
        if self.skip("vehicle_ids", "camera_ids", "sighting_ids"):
            return []
        rows = await self.conn.fetch(
            _SQL_FEED_ANPR,
            self.since,
            self.limit,
            self.unscoped,
            self._u("vehicle_ids"),
            self._t("camera_ids"),
            self._u("sighting_ids"),
        )
        return [anpr_item(r) for r in rows]

    async def dispatch(self) -> list[dict]:
        if self.skip("event_ids", "dispatch_ids", "unit_ids", "junction_ids"):
            return []
        rows = await self.conn.fetch(
            _SQL_FEED_DISPATCH,
            self.since,
            self.limit,
            self.unscoped,
            self._u("event_ids"),
            self._u("dispatch_ids"),
            self._t("unit_ids"),
            self._i("junction_ids"),
        )
        return [dispatch_item(r) for r in rows]

    async def signals(self) -> list[dict]:
        if self.skip("event_ids", "junction_ids"):
            return []
        if not await relation_exists(self.conn, "corridor.signal_plan_log"):
            raise SourceUnavailableError("corridor.signal_plan_log missing (migration 013)")
        args = (
            self.since,
            self.limit,
            self.unscoped,
            self._u("event_ids"),
            self._i("junction_ids"),
        )
        plans = await self.conn.fetch(_SQL_FEED_SIGNAL_PLANS, *args)
        history = await self.conn.fetch(_SQL_FEED_SIGNALS, *args)
        resolved = await resolve_subjects(
            self.conn, [r["event_id"] for r in [*plans, *history] if r["event_id"] is not None]
        )
        items = [signal_plan_item(r, resolved) for r in plans]
        items += [signals_item(r, resolved) for r in history]
        return items

    async def situations(self) -> list[dict]:
        if self.skip("movement_ids"):
            return []
        rows = await self.conn.fetch(
            _SQL_FEED_SITUATIONS, self.since, self.limit, self.unscoped, self._t("movement_ids")
        )
        resolved = await resolve_subjects(self.conn, [r["movement_id"] for r in rows])
        return [situation_item(r, resolved) for r in rows]

    async def overrides(self) -> list[dict]:
        if self.skip("movement_ids"):
            return []
        rows = await self.conn.fetch(
            _SQL_FEED_OVERRIDES,
            self.since,
            self.limit,
            self.unscoped,
            self._t("movement_ids"),
            VERDICT_LOOKBACK,
            _RESTING_VERDICT,
        )
        resolved = await resolve_subjects(self.conn, [r["movement_id"] for r in rows])
        return [override_item(r, resolved) for r in rows]

    async def brain(self) -> list[dict]:
        if self.skip("movement_ids"):
            return []
        if self.valkey is None:
            raise SourceUnavailableError("valkey client unavailable")
        if self.unscoped:
            now = time.monotonic()
            client, expires, cached = _BRAIN_CACHE
            if client is self.valkey and expires > now:
                contexts = cached
            else:
                contexts, _, _ = await scan_contexts(self.valkey)
                _BRAIN_CACHE[:] = [self.valkey, now + BRAIN_SCAN_TTL_SEC, contexts]
        else:
            keys = [context_key(m) for m in self._t("movement_ids")]
            contexts, _ = await read_contexts(self.valkey, keys)
        mids = [m for m, _ in contexts if m]
        resolved = await resolve_subjects(self.conn, mids)
        out: list[dict] = []
        unlinked = 0
        for mid, payload in contexts:
            if not mid:
                continue
            if self.unscoped and not resolved.events_of(mid):
                unlinked += 1  # a planner session, not a city operation
                continue
            out.extend(brain_items(mid, decisions_of(payload), resolved, self.since))
        if self.unscoped:
            self.meta["decisions_log"] = {"unlinked_movements": unlinked}
        out.sort(key=lambda i: i["t"], reverse=True)
        return out[: self.limit]


_ID_PART_RE = re.compile(r"(\d+)")


def _id_order(item_id: str) -> tuple:
    """Numeric-aware id key: items sharing a wire millisecond order by
    their ledger sequence (``event_status_log:10`` after ``:9``), not by
    string comparison.  Text/number parts alternate, so keys compare
    position by position without mixing types."""
    return tuple(
        int(part) if i % 2 else part for i, part in enumerate(_ID_PART_RE.split(item_id))
    )


#: (source name, FeedItem kind, _Feed method) in a fixed order.
FEED_SOURCES: tuple[tuple[str, str, str], ...] = (
    ("event_status_log", "event", "event"),
    ("enforcement_recommendations", "enforcement", "enforcement"),
    ("vehicle_sightings", "anpr", "anpr"),
    ("dispatch_events", "dispatch", "dispatch"),
    ("signal_program_history", "signals", "signals"),
    ("situation_log", "ama", "situations"),
    ("param_overrides", "ama", "overrides"),
    ("decisions_log", "brain", "brain"),
)


async def collect_feed(
    conn: Any,
    *,
    since: datetime,
    limit: int,
    kinds: tuple[str, ...] = FEED_KINDS,
    scope: FeedScope | None = None,
    valkey: Any = None,
) -> tuple[list[dict], dict]:
    """The merged narrative, newest first: ``(items, sources)``.

    ``since`` is a WIRE instant (millisecond precision): every source
    filters ``t_wire > since`` (common.after_wire_ms).
    """
    feed = _Feed(conn, since=after_wire_ms(since), limit=limit, scope=scope, valkey=valkey)
    items: list[dict] = []
    sources: dict[str, dict] = {}
    for name, kind, method in FEED_SOURCES:
        if kind not in kinds:
            continue
        try:
            got = await getattr(feed, method)()
            sources[name] = {"status": "ok", "count": len(got), **feed.meta.get(name, {})}
        except SourceUnavailableError as exc:
            got = []
            sources[name] = {"status": "unavailable", "count": 0, "detail": str(exc)}
        except Exception as exc:  # noqa: BLE001 — one dead source never blanks the feed
            logger.warning("city.feed_source_failed", source=name, error=repr(exc))
            got = []
            sources[name] = {"status": "error", "count": 0, "detail": type(exc).__name__}
        items.extend(got)
    items.sort(key=lambda i: (i["t"] or "", _id_order(i["id"])), reverse=True)
    return items[:limit], sources


async def event_audit(
    conn: Any,
    event_id: _uuid.UUID,
    *,
    since: datetime,
    limit: int,
    valkey: Any = None,
) -> dict | None:
    """The per-event audit (contract C-C): the event's URN plus its
    movement / vehicle / recommendation / dispatch keys; None if unknown."""
    row = await conn.fetchrow(_SQL_AUDIT_EVENT, event_id)
    if row is None:
        return None
    meta = as_dict(row["metadata"])
    eid = str(row["event_id"])
    movement_ids = [eid]
    if row["movement_id"] is not None:
        movement_ids.append(str(row["movement_id"]))
    scope = FeedScope(
        event_ids=[eid],
        movement_ids=movement_ids,
        vehicle_ids=[v for v in [meta.get("vehicle_id")] if isinstance(v, str)],
        recommendation_ids=[
            v for v in [meta.get("recommendation_id")] if isinstance(v, str)
        ],
        dispatch_ids=[
            str(r["dispatch_id"])
            for r in await conn.fetch(_SQL_AUDIT_EVENT_DISPATCHES, event_id)
        ],
    )
    items, sources = await collect_feed(
        conn, since=since, limit=limit, scope=scope, valkey=valkey
    )
    return {"event_id": eid, "items": items, "sources": sources}


__all__ = [
    "BRAIN_SCAN_TTL_SEC",
    "FEED_SOURCES",
    "GLOBAL_LOOKBACK",
    "VERDICT_LOOKBACK",
    "FeedScope",
    "SourceUnavailableError",
    "SIMULATED_FEED_SUFFIX",
    "anpr_item",
    "brain_items",
    "brain_row_simulated",
    "collect_feed",
    "dispatch_item",
    "enforcement_items",
    "event_audit",
    "event_created_item",
    "event_log_item",
    "override_item",
    "reset_brain_cache",
    "signal_plan_item",
    "signals_item",
    "situation_item",
]
