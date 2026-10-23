"""City-picture fusion — brain-planned movements become corridor events.

A plan the brain produces is invisible to the unified city picture until it
exists as an ``events.corridor_events`` row: the God's-eye events layer, the
ops board, the situation engine and the citizen/driver advisories all read
GET /api/v1/events/active. After every pre-movement plan the workflow calls
:meth:`CityLink.register_plan`, which POSTs the plan to /api/v1/events as a
corridor event:

    event_kind        the movement's kind (protected_movement by default)
    protection_class  the plan's VVIP class
    route_spines      "primary" first, then "alt-1", "alt-2", ...
    phases            one closure phase from planned_departure minus the
                      protocol-floored advance_closure_sec to the end of the
                      primary's transit
    metadata          {source: "convoy-brain", brain_status, decider_pick,
                       security_score, ...}

On re-plan the superseded event is retired through the event lifecycle
endpoint (contract C-F: POST /api/v1/events/{id}/status → cancelled, actor
"convoy-brain (system)"). Until that endpoint is deployed the oracle answers
404; that is reported as ``not_found`` and tolerated.

Posture: the brain registers its plan in status 'planning' only — approval
and activation stay human decisions on the ops board. Every outcome
(registered / not_deployed / rejected / failed) is returned for the plan
document and the audit trail; nothing here can fail a plan or raise.

A movement created with a ``title`` (an acceptance test, a rehearsal)
carries it as ``metadata.label`` and as the event title's prefix, so it
never reads as a real protected movement on the board, the God's-eye or the
citizen feeds.

Re-route (contract C-O): when a live escort switches route (emergency
re-route, Plan B), :meth:`CityLink.update_route` POSTs the new spine to
/api/v1/events/{id}/routes as the event's primary (the previous primary is
superseded server-side), so the board, the situation engine and the
advisories follow the convoy's real route. Best effort and audited: a 404
is 'not_deployed', a 409/422 is kept with the oracle's detail.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import structlog

from convoy_brain.http_guard import assert_platform_service_url

logger = structlog.get_logger(__name__)

#: Actor string for system-initiated lifecycle transitions (C-F).
SYSTEM_ACTOR = "convoy-brain (system)"
CITY_LINK_TIMEOUT_SEC = 5.0

#: Oracle request bounds (events/__init__.py CorridorEventRequest).
MAX_SPINES = 8
MAX_SPINE_SEGMENTS = 64
MAX_METADATA_KEYS = 16

#: Corridor-event archetypes the oracle accepts for a brain-planned movement.
EVENT_KINDS = (
    "protected_movement", "procession", "mass_gathering", "race",
    "ceremonial", "egress_surge", "emergency_evacuation", "work_zone",
)
PROTECTION_CLASSES = ("Z+", "Z", "Y", "X", "none")

#: Closure posture by protection class (Blue Book table in mcp/prompts.py).
CLOSURE_TYPE = {
    "Z+": "full",
    "Z": "partial",
    "Y": "speed_restriction",
    "X": "signal_priority",
    "none": "managed",
}


def _parse_departure(value: Any) -> datetime:
    """ISO-8601 departure -> aware datetime (naive = IST, like the oracle)."""
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone(timedelta(hours=5, minutes=30)))
            return parsed
    return datetime.now(timezone.utc)


def _is_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
    except (TypeError, ValueError):
        return False
    return True


#: A movement label (CreateMovementRequest.title) is at most this long.
MAX_LABEL_CHARS = 120


def clean_label(value: object) -> str | None:
    """A movement label as stored: whitespace collapsed, bounded, or None."""
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())[:MAX_LABEL_CHARS]
    return text or None


def _corridor_title(
    vvip_class: str, event_kind: str, primary: dict, label: str | None = None
) -> str:
    names = [n for n in (primary.get("road_names") or []) if n]
    kind = event_kind.replace("_", " ")
    lead = f"{label} · " if label else ""
    if names:
        span = names[0] if len(names) == 1 or names[0] == names[-1] else f"{names[0]} → {names[-1]}"
        return f"{lead}{vvip_class} {kind} · {span}"[:200]
    return f"{lead}{vvip_class} {kind} · {len(primary.get('segment_ids') or [])} segments"[:200]


def build_event_payload(
    plan: dict,
    *,
    event_kind: str = "protected_movement",
    crowd_class: str = "LOW",
    advance_closure_sec: int | None = None,
    label: str | None = None,
) -> dict | None:
    """The POST /api/v1/events body for a plan document (None without a route).

    ``label`` (the movement's operator label) prefixes the title and rides
    as ``metadata.label``.
    """
    primary = plan.get("primary_route")
    if not isinstance(primary, dict) or not primary.get("segment_ids"):
        return None
    vvip_class = plan.get("vvip_class") if plan.get("vvip_class") in PROTECTION_CLASSES else "none"
    kind = event_kind if event_kind in EVENT_KINDS else "protected_movement"

    spines = [{
        "label": "primary",
        "segment_ids": list(primary["segment_ids"])[:MAX_SPINE_SEGMENTS],
    }]
    for index, alternate in enumerate(plan.get("alternate_routes") or [], start=1):
        if len(spines) >= MAX_SPINES:
            break
        if isinstance(alternate, dict) and alternate.get("segment_ids"):
            spines.append({
                "label": f"alt-{index}",
                "segment_ids": list(alternate["segment_ids"])[:MAX_SPINE_SEGMENTS],
            })

    departure = _parse_departure(plan.get("planned_departure"))
    advance = max(0, int(advance_closure_sec or 0))
    transit = max(60, int(primary.get("estimated_time_sec") or 0))
    closure_type = CLOSURE_TYPE.get(vvip_class, "managed") if advance else "signal_priority"
    phase = {
        "label": "closure" if advance else "signal_priority",
        "window_start": (departure - timedelta(seconds=advance)).isoformat(),
        "window_end": (departure + timedelta(seconds=transit)).isoformat(),
        "closure_policy": {
            "advance_closure_sec": advance,
            "closure_type": closure_type,
            "source": "protocol_floor",
        },
    }

    movement_id = plan.get("movement_id")
    metadata: dict[str, Any] = {
        "source": "convoy-brain",
        "brain_status": plan.get("status"),
        "decider_pick": plan.get("decider_pick"),
        "security_score": plan.get("security_score"),
        "security_compliant": plan.get("security_compliant"),
        "route_ids": [s.get("route_id") for s in
                      [primary, *(plan.get("alternate_routes") or [])][:MAX_SPINES]
                      if isinstance(s, dict)],
        "planned_departure": plan.get("planned_departure"),
        "movement_ref": movement_id,
        "eta_sec": primary.get("estimated_time_sec"),
    }
    label = clean_label(label)
    if label:
        metadata["label"] = label
    metadata = dict(list(metadata.items())[:MAX_METADATA_KEYS])

    return {
        "event_kind": kind,
        "protection_class": vvip_class,
        "crowd_class": (
            crowd_class if crowd_class in ("LOW", "MEDIUM", "HIGH", "EXTREME") else "LOW"
        ),
        "title": _corridor_title(vvip_class, kind, primary, label),
        "description": str(plan.get("overall_reasoning") or "")[:2000],
        "route_spines": spines,
        "phases": [phase],
        # events.corridor_events.movement_id is a UUID column; a non-UUID
        # movement id travels as metadata.movement_ref only.
        "movement_id": str(movement_id) if _is_uuid(movement_id) else None,
        "metadata": metadata,
    }


class CityLink:
    """Thin async client for the oracle's corridor-event write surface."""

    def __init__(
        self,
        base_url: str,
        client: httpx.AsyncClient | None = None,
        timeout_sec: float = CITY_LINK_TIMEOUT_SEC,
    ) -> None:
        self._base_url = assert_platform_service_url(base_url.rstrip("/"))
        self._timeout = timeout_sec
        self._owns_client = client is None
        self._client = client
        logger.info("city_link.init", base_url=self._base_url)

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def register_plan(
        self,
        plan: dict,
        *,
        event_kind: str = "protected_movement",
        crowd_class: str = "LOW",
        advance_closure_sec: int | None = None,
        label: str | None = None,
    ) -> dict:
        """POST the plan as a corridor event. Never raises.

        Returns ``{status, event_id, ...}`` with status ``registered``,
        ``skipped`` (no route), ``not_deployed`` (404), ``rejected`` (other
        4xx, with the oracle's detail) or ``failed`` (5xx / transport).
        """
        payload = build_event_payload(
            plan,
            event_kind=event_kind,
            crowd_class=crowd_class,
            advance_closure_sec=advance_closure_sec,
            label=label,
        )
        if payload is None:
            return {"status": "skipped", "event_id": None, "reason": "no primary route to register"}
        try:
            client = await self._ensure_client()
            resp = await client.post(
                f"{self._base_url}/api/v1/events", json=payload, timeout=self._timeout
            )
        except Exception as exc:
            logger.warning("city_link.register_failed", error=str(exc)[:200])
            return {"status": "failed", "event_id": None, "reason": f"transport: {str(exc)[:200]}"}
        if resp.status_code == 200:
            try:
                body = resp.json()
            except ValueError:
                body = {}
            event_id = body.get("event_id") if isinstance(body, dict) else None
            logger.info(
                "city_link.registered",
                event_id=event_id,
                movement_id=plan.get("movement_id"),
                spines=len(payload["route_spines"]),
            )
            return {
                "status": "registered" if event_id else "failed",
                "event_id": event_id,
                "spines": [s["label"] for s in payload["route_spines"]],
                "reason": None if event_id else "oracle answered 200 without an event_id",
            }
        status = "not_deployed" if resp.status_code == 404 else (
            "rejected" if 400 <= resp.status_code < 500 else "failed"
        )
        logger.warning(
            "city_link.register_refused", http_status=resp.status_code, body=resp.text[:200]
        )
        return {
            "status": status,
            "event_id": None,
            "http_status": resp.status_code,
            "reason": resp.text[:300],
        }

    async def cancel_event(self, event_id: str, note: str) -> dict:
        """Retire a superseded event (C-F lifecycle). Never raises.

        Status ``cancelled`` on 200; ``not_found`` on 404 (unknown event, or
        the lifecycle endpoint is not deployed yet — tolerated); ``conflict``
        on 409 (already completed/cancelled — the oracle's ``allowed`` list
        is kept); ``rejected`` on other 4xx; ``failed`` otherwise.
        """
        body = {"to_status": "cancelled", "officer": SYSTEM_ACTOR, "note": note[:500]}
        try:
            client = await self._ensure_client()
            resp = await client.post(
                f"{self._base_url}/api/v1/events/{event_id}/status",
                json=body,
                timeout=self._timeout,
            )
        except Exception as exc:
            logger.warning("city_link.cancel_failed", event_id=event_id, error=str(exc)[:200])
            return {
                "status": "failed", "event_id": event_id,
                "reason": f"transport: {str(exc)[:200]}",
            }
        try:
            detail = resp.json()
        except ValueError:
            detail = {"detail": resp.text[:300]}
        if resp.status_code == 200:
            logger.info("city_link.cancelled", event_id=event_id)
            return {"status": "cancelled", "event_id": event_id, "transition": detail}
        if resp.status_code == 404:
            status = "not_found"
        elif resp.status_code == 409:
            status = "conflict"
        elif 400 <= resp.status_code < 500:
            status = "rejected"
        else:
            status = "failed"
        logger.info(
            "city_link.cancel_refused",
            event_id=event_id,
            http_status=resp.status_code,
            status=status,
        )
        return {
            "status": status,
            "event_id": event_id,
            "http_status": resp.status_code,
            "detail": detail,
        }


    async def update_route(
        self,
        event_id: str | None,
        variant: str,
        segment_ids: list[int],
        *,
        make_primary: bool = True,
        officer: str | None = None,
        note: str | None = None,
    ) -> dict:
        """POST /api/v1/events/{id}/routes — the event's new primary spine (C-O).

        Never raises. ``status`` is ``updated`` (200), ``not_deployed`` (404:
        the route-update endpoint, or the event, is absent), ``conflict``
        (409: the event is completed / cancelled), ``rejected`` (422 / other
        4xx, e.g. an unknown segment), ``failed`` (5xx / transport) or
        ``skipped`` (no event, no segments).
        """
        ids = [
            int(s) for s in segment_ids
            if isinstance(s, (int, float)) and not isinstance(s, bool)
        ]
        if not event_id or not _is_uuid(event_id) or not ids:
            return {"status": "skipped", "event_id": event_id,
                    "reason": "no corridor event or no segments to publish"}
        body: dict[str, Any] = {
            "variant": variant,
            "segment_ids": ids[:MAX_SPINE_SEGMENTS],
            "make_primary": bool(make_primary),
            "officer": officer or SYSTEM_ACTOR,
        }
        if note:
            body["note"] = note[:500]
        try:
            client = await self._ensure_client()
            resp = await client.post(
                f"{self._base_url}/api/v1/events/{event_id}/routes",
                json=body,
                timeout=self._timeout,
            )
        except Exception as exc:
            logger.warning("city_link.route_update_failed", event_id=event_id, error=str(exc)[:200])
            return {"status": "failed", "event_id": event_id,
                    "reason": f"transport: {str(exc)[:200]}"}
        try:
            detail = resp.json()
        except ValueError:
            detail = {"detail": resp.text[:300]}
        if resp.status_code == 200:
            status = "updated"
        elif resp.status_code == 404:
            status = "not_deployed"
        elif resp.status_code == 409:
            status = "conflict"
        elif 400 <= resp.status_code < 500:
            status = "rejected"
        else:
            status = "failed"
        logger.info(
            "city_link.route_update",
            event_id=event_id, variant=variant, http_status=resp.status_code, status=status,
        )
        out = {
            "status": status,
            "event_id": event_id,
            "variant": variant,
            "segment_ids": body["segment_ids"],
            "http_status": resp.status_code,
        }
        if status != "updated":
            out["detail"] = (detail.get("detail") if isinstance(detail, dict) else None) or detail
        return out


def current_event_id(ctx: Any) -> str | None:
    """The corridor event currently registered for a movement context.

    Reads the first-class ``event_id`` field when the context model has one,
    otherwise the newest ``city_event`` row of the decisions_log (the audit
    trail this module's caller writes on every plan).
    """
    direct = getattr(ctx, "event_id", None)
    if direct:
        return str(direct)
    for row in reversed(getattr(ctx, "decisions_log", None) or []):
        if isinstance(row, dict) and row.get("phase") == "city_event":
            event_id = row.get("event_id")
            return str(event_id) if event_id else None
    return None
