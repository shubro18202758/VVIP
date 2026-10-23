"""City fusion — shared helpers for the C-C surfaces (WP-02).

Everything the city routes have in common and nothing else: canonical
URNs, id validation, timestamp/JSON coercion, plate masking, source
labels, actors (classification + the named-actor rule every write
surface shares), the two record constructors every surface emits
(``CityEntity`` and ``FeedItem``, contract C-C) and a TTL-cached relation
probe so a surface can degrade visibly when a migration has not landed
yet.

Pure functions apart from the probes; no route lives here.

Ethics rails (C6) enforced by construction:
    * plates leave this layer MASKED in every label and feed text
      (``GJ27EM•••1``); the raw plate travels only in ``props.plate`` so
      the deck can unmask it for a named duty officer;
    * no person/face/frame field exists in either record shape;
    * every record carries ``source`` live|emulated — the badge cannot be
      dropped by a consumer that ignores props.  Records about an
      adaptive / brain subject inherit the badge of the corridor events
      the subject denotes (:class:`ResolvedSubjects`), so one emulated
      scenario never carries mixed badges.
"""

from __future__ import annotations

import json
import math
import re
import time
import unicodedata
import uuid as _uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Vocabulary (contract C-C)
# ---------------------------------------------------------------------------

#: Kinds GET /api/v1/city/entities can return.
ENTITY_KINDS: tuple[str, ...] = (
    "event",
    "vehicle",
    "sighting",
    "camera",
    "junction",
    "unit",
    "convoy",
    "dispatch",
    "recommendation",
    "anomaly",
)

#: Kinds GET /api/v1/city/entity/{kind}/{id} resolves (``segment`` is
#: addressable by URN — spines and anomalies link to it — but is not part
#: of the snapshot: the flow layer already draws every segment).
DOSSIER_KINDS: tuple[str, ...] = ENTITY_KINDS + ("segment",)

#: FeedItem.kind values (contract C-C).
FEED_KINDS: tuple[str, ...] = (
    "event",
    "enforcement",
    "anpr",
    "dispatch",
    "signals",
    "ama",
    "brain",
)

BANDS: tuple[str, ...] = ("GREEN", "AMBER", "RED", "BLACK")

_UUID_KINDS = frozenset({"event", "vehicle", "sighting", "dispatch", "recommendation"})
_INT_KINDS = frozenset({"junction", "segment", "anomaly"})
_SLUG_KINDS = frozenset({"camera", "unit", "convoy"})

#: Slug ids (camera_id, unit_id, convoy movement_id): bounded, no spaces,
#: no path or query metacharacters.
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,63}$")

#: Lower bound used when a caller gives no ``since``: the epoch (every
#: source filters ``t > since``; a scoped audit wants the whole history).
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# ---------------------------------------------------------------------------
# Ids and URNs
# ---------------------------------------------------------------------------


def parse_entity_id(kind: str, raw: str) -> str | int:
    """Validate one entity id for ``kind``; ValueError when malformed.

    uuid kinds return the canonical lowercase string, int kinds a
    positive int, slug kinds the slug itself.
    """
    if kind in _UUID_KINDS:
        return str(_uuid.UUID(str(raw)))
    if kind in _INT_KINDS:
        if not str(raw).isdigit():
            raise ValueError(f"{kind} id must be a positive integer")
        value = int(raw)
        if value <= 0 or value > 2**62:
            raise ValueError(f"{kind} id out of range")
        return value
    if kind in _SLUG_KINDS:
        if not _SLUG_RE.fullmatch(str(raw)):
            raise ValueError(f"{kind} id must match {_SLUG_RE.pattern}")
        return str(raw)
    raise ValueError(f"unknown kind {kind!r}")


def is_slug(value: Any) -> bool:
    return isinstance(value, str) and bool(_SLUG_RE.fullmatch(value))


def as_uuid(value: Any) -> _uuid.UUID | None:
    """Parse a uuid from any value; None when it is not one."""
    if isinstance(value, _uuid.UUID):
        return value
    if not isinstance(value, str):
        return None
    try:
        return _uuid.UUID(value)
    except ValueError:
        return None


def urn(kind: str, ident: Any) -> str:
    """Canonical ``<kind>:<id>`` URN (contract C-C)."""
    return f"{kind}:{ident}"


def dedupe(values: Iterable[str]) -> list[str]:
    """Order-preserving de-duplication (links / urns lists)."""
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


# ---------------------------------------------------------------------------
# Time + JSON coercion
# ---------------------------------------------------------------------------


def utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso_z(dt: datetime | None) -> str | None:
    """ISO-8601 UTC with millisecond precision and a ``Z`` suffix."""
    if dt is None:
        return None
    return utc(dt).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def after_wire_ms(since: datetime) -> datetime:
    """The instant a ``t > since`` filter must compare against, given that
    wire timestamps are truncated to the millisecond (:func:`iso_z`).

    A record whose true time is 15:00:04.426700 is published as
    ``…04.426Z``; a poller that sends ``since=…04.426Z`` must not get it
    again.  ``wire(t) > since`` ⇔ ``t >= floor_ms(since) + 1 ms`` ⇔
    ``t > floor_ms(since) + 999 µs`` (database clocks are µs-resolution).
    """
    since = utc(since)
    floor = since.replace(microsecond=since.microsecond - since.microsecond % 1000)
    return floor + timedelta(microseconds=999)


def parse_ts(value: Any) -> datetime | None:
    """datetime | ISO string | epoch seconds → aware UTC datetime."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return utc(value)
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            return None
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
        except ValueError:
            try:
                return parse_ts(float(text))
            except ValueError:
                return None
    return None


def age_sec(t: datetime | None, now: datetime) -> float | None:
    """Seconds since ``t`` (0 for future-dated rows, which are flagged)."""
    if t is None:
        return None
    return round(max(0.0, (now - utc(t)).total_seconds()), 1)


def as_dict(raw: Any) -> dict:
    """asyncpg returns JSONB as text — coerce to a dict ({} when not one)."""
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return dict(raw) if isinstance(raw, Mapping) else {}


def as_list(raw: Any) -> list:
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    return list(raw) if isinstance(raw, (list, tuple)) else []


def finite(value: Any) -> float | None:
    """A finite float or None (DB NULLs, NaN and junk never leak out)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def valid_lonlat(lon: Any, lat: Any) -> tuple[float, float] | None:
    lo, la = finite(lon), finite(lat)
    if lo is None or la is None:
        return None
    if not (-180.0 <= lo <= 180.0 and -90.0 <= la <= 90.0):
        return None
    return lo, la


# ---------------------------------------------------------------------------
# Plates (C6: masked by default everywhere a human reads text)
# ---------------------------------------------------------------------------

_MASK = "•••"
#: Indian registration marks inside free text: state code, RTO number,
#: optional series letters, 4-digit number (GJ27EM9001, GJ 27 EM 9001,
#: GJ-01-AB-1234).  Only the 4-digit number is masked, last digit kept.
_PLATE_IN_TEXT_RE = re.compile(
    r"\b([A-Z]{2}[ -]?\d{1,2}[ -]?(?:[A-Z]{1,3}[ -]?)?)(\d{4})\b"
)


def mask_plates_in_text(text: str | None) -> str:
    if not text:
        return ""
    return _PLATE_IN_TEXT_RE.sub(lambda m: m.group(1) + _MASK + m.group(2)[-1], text)


def mask_plate(plate: str | None) -> str | None:
    """``GJ27EM9001`` → ``GJ27EM•••1`` (the deck unmasks for a named officer)."""
    if not plate:
        return None
    text = str(plate)
    masked = mask_plates_in_text(text)
    if masked != text:
        return masked
    if len(text) >= 6:
        return text[:-4] + _MASK + text[-1]
    return _MASK + text[-1]


# ---------------------------------------------------------------------------
# Source labels (every record says live|emulated)
# ---------------------------------------------------------------------------


def label_source(raw: Any) -> str:
    """Map a producer source tag to the C-C badge: emulated|live."""
    if raw is None:
        return "live"
    return "emulated" if "emulat" in str(raw).lower() else "live"


def event_source(metadata: Mapping[str, Any] | None) -> str:
    """Corridor events created by the emulator (or its living city) are
    EMULATED; operator- and brain-created events are live."""
    meta = metadata or {}
    src = str(meta.get("source") or "").lower()
    if src == "event-emulator" or "emulat" in src:
        return "emulated"
    if str(meta.get("mode") or "").lower() == "living_city":
        return "emulated"
    return "live"


# ---------------------------------------------------------------------------
# Record constructors (contract C-C shapes, exactly)
# ---------------------------------------------------------------------------


def city_entity(
    kind: str,
    ident: Any,
    label: str,
    lon: Any,
    lat: Any,
    *,
    now: datetime,
    state: str | None = None,
    band: str | None = None,
    source: str = "live",
    t: datetime | None = None,
    links: Iterable[str] = (),
    props: Mapping[str, Any] | None = None,
) -> dict:
    """One CityEntity.  lon/lat are None only for dossier records of
    entities with no known position — the snapshot never emits those."""
    point = valid_lonlat(lon, lat)
    body = dict(props or {})
    if t is not None and (utc(t) - now).total_seconds() > 30.0:
        body.setdefault("future_dated", True)
    return {
        "urn": urn(kind, ident),
        "kind": kind,
        "id": str(ident),
        "label": label,
        "lon": round(point[0], 6) if point else None,
        "lat": round(point[1], 6) if point else None,
        "state": state,
        "band": band if band in BANDS else None,
        "source": source if source in ("live", "emulated") else "live",
        "t": iso_z(t),
        "age_sec": age_sec(t, now),
        "links": dedupe(links),
        "props": body,
    }


def feed_item(
    item_id: str,
    t: datetime,
    kind: str,
    urns: Iterable[str],
    *,
    actor: str | None,
    actor_kind: str,
    text: str,
    source: str = "live",
    severity: str = "info",
    props: Mapping[str, Any] | None = None,
) -> dict:
    """One FeedItem (contract C-C).  ``text`` is plate-masked here, once,
    so no source can forget to."""
    return {
        "id": item_id,
        "t": iso_z(t),
        "kind": kind,
        "urns": dedupe(urns),
        "actor": actor,
        "actor_kind": actor_kind if actor_kind in ("human", "machine", "system") else "system",
        "text": mask_plates_in_text(text)[:600],
        "source": source if source in ("live", "emulated") else "live",
        "severity": severity if severity in ("info", "warn", "critical") else "info",
        "props": dict(props or {}),
    }


# ---------------------------------------------------------------------------
# Actors (who did it): classification + the named-actor rule (C6)
# ---------------------------------------------------------------------------

#: Machine agents that write audit rows under their own name.  "brain" is
#: matched as a WORD (convoy-brain, brain) so an officer named "Brainard"
#: is never classified as a machine.
_MACHINE_ACTOR_RE = re.compile(r"\bbrain\b")
_MACHINE_ACTORS = frozenset({"traffic-oracle", "rules-engine", "ama"})


def actor_kind_of(actor: str | None, *, default: str = "human") -> str:
    """Classify a free-text actor: system bookkeeping, machine agents, or a
    named human (the only kind that can approve anything)."""
    if not actor:
        return default
    low = actor.strip().lower()
    if low.startswith("system") or "(system)" in low or low == "event-emulator":
        return "system"
    if _MACHINE_ACTOR_RE.search(low) or low in _MACHINE_ACTORS:
        return "machine"
    return "human"


#: Bidirectional marks (LRM / RLM) and embedding / override / isolate
#: controls: in an audit trail they can make one name DISPLAY as another
#: (C6 — the actor of a state change must read exactly as it was
#: recorded).  The same set the brain's officer rule (_OFFICER_FORBIDDEN)
#: and the deck's sign-in and writers refuse, so a name one surface takes
#: is never refused by another.  Zero-width (non-)joiners stay legal:
#: Indic names use them.
_BIDI_CONTROLS = frozenset(
    "\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
)


def _control_chars(text: str, *, allowed: str = "") -> list[str]:
    """Control characters (Unicode Cc) and bidi controls in ``text``."""
    return [
        c for c in text
        if c not in allowed and (unicodedata.category(c) == "Cc" or c in _BIDI_CONTROLS)
    ]


def named_actor(
    value: str | None,
    *,
    min_len: int = 2,
    max_len: int = 64,
    field_name: str = "officer",
) -> str | None:
    """The named-actor rule shared by every write surface (C6, C-F).

    Whitespace runs collapse to one space; the result must be
    ``min_len..max_len`` characters and free of control / bidi characters
    (a NUL can never reach Postgres as a 500, and a recorded name always
    reads as it was written).  ``None`` passes through (optional fields).
    Raises ValueError (→ 422 through pydantic) otherwise.
    """
    if value is None:
        return None
    trimmed = " ".join(str(value).split())
    if _control_chars(trimmed):
        raise ValueError(f"{field_name} must not contain control characters")
    if not min_len <= len(trimmed) <= max_len:
        raise ValueError(
            f"{field_name} must be {min_len}..{max_len} characters (a named human actor)"
        )
    return trimmed


def clean_text(value: str | None, *, field_name: str = "text") -> str | None:
    """Free text bound for a TEXT/JSONB column (notes, titles, labels).

    Tabs and line breaks are kept; any other control character (NUL above
    all — Postgres cannot store it) and bidi controls are refused with a
    ValueError (→ 422) instead of failing the write with a 500.
    """
    if value is None:
        return None
    if _control_chars(str(value), allowed="\t\n\r"):
        raise ValueError(f"{field_name} must not contain control characters")
    return value


def find_nul(value: Any, *, path: str = "") -> str | None:
    """Path of the first string containing NUL inside a JSON-like value
    (keys included), else None.  Postgres TEXT and JSONB reject NUL."""
    if isinstance(value, str):
        return (path or "value") if "\x00" in value else None
    if isinstance(value, Mapping):
        for k, v in value.items():
            here = f"{path}.{k}" if path else str(k)
            if isinstance(k, str) and "\x00" in k:
                return here
            hit = find_nul(v, path=here)
            if hit:
                return hit
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            hit = find_nul(v, path=f"{path}[{i}]")
            if hit:
                return hit
    return None


# ---------------------------------------------------------------------------
# Query-parameter parsing
# ---------------------------------------------------------------------------


def parse_kinds(raw: str | None, allowed: tuple[str, ...]) -> tuple[str, ...]:
    """CSV of kinds → validated tuple (all when empty); ValueError on junk."""
    if raw is None or not raw.strip():
        return allowed
    wanted = [k.strip() for k in raw.split(",") if k.strip()]
    unknown = [k for k in wanted if k not in allowed]
    if unknown:
        raise ValueError(f"unknown kinds {unknown}; allowed {list(allowed)}")
    return tuple(k for k in allowed if k in wanted)


def parse_bbox(raw: str | None) -> tuple[float, float, float, float] | None:
    """``minLon,minLat,maxLon,maxLat`` → tuple; ValueError when malformed."""
    if raw is None or not raw.strip():
        return None
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 4:
        raise ValueError("bbox must be minLon,minLat,maxLon,maxLat")
    vals = [finite(p) for p in parts]
    if any(v is None for v in vals):
        raise ValueError("bbox values must be finite numbers")
    min_lon, min_lat, max_lon, max_lat = (float(v) for v in vals)  # type: ignore[arg-type]
    if not (-180 <= min_lon < max_lon <= 180 and -90 <= min_lat < max_lat <= 90):
        raise ValueError("bbox must satisfy min < max within lon/lat ranges")
    return min_lon, min_lat, max_lon, max_lat


def in_bbox(entity: Mapping[str, Any], bbox: tuple[float, float, float, float]) -> bool:
    lon, lat = entity.get("lon"), entity.get("lat")
    if lon is None or lat is None:
        return False
    return bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3]


# ---------------------------------------------------------------------------
# C6 lawful basis for surfacing ONE ANPR read
# ---------------------------------------------------------------------------

#: SQL predicate over a ``corridor.vehicle_sightings`` row aliased ``s``:
#: the read may be surfaced individually (time + camera + plate) only while
#: its vehicle holds an ACTIVE earmark — expiry enforced HERE, in the query
#: path, so a cleared or lapsed earmark withdraws the trail at once — or
#: when the read is the evidence of an enforcement recommendation (the
#: human-decision audit keeps its evidence).  Every city query that can
#: expose an individual read composes this one predicate, so the rule
#: cannot drift between the feed, the camera dossier and the sighting
#: dossier.  Aggregate counts (GET /cameras) are not individual reads.
SQL_LAWFUL_READ = "(EXISTS (SELECT 1 FROM corridor.vehicle_earmarks ve WHERE ve.vehicle_id = s.vehicle_id AND ve.cleared_at IS NULL AND ve.retention_until > NOW()) OR EXISTS (SELECT 1 FROM events.enforcement_recommendations er WHERE er.sighting_id = s.sighting_id))"  # noqa: E501

# ---------------------------------------------------------------------------
# Probes (TTL-cached so a migration landing at runtime is picked up)
# ---------------------------------------------------------------------------

_SQL_REGCLASS = "SELECT to_regclass($1) IS NOT NULL AS present"

#: Whether ``schema.table`` has a live (not dropped) column ``$2``.
_SQL_COLUMN = "SELECT EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = to_regclass($1) AND a.attname = $2 AND NOT a.attisdropped) AS present"  # noqa: E501

_REL_TTL_SEC = 30.0
_REL_CACHE: dict[str, tuple[float, bool]] = {}
_COL_CACHE: dict[tuple[str, str], tuple[float, bool]] = {}

#: Migration 018 (route updates): events.event_routes.superseded_at.
ROUTES_TABLE = "events.event_routes"
ROUTES_SUPERSEDE_COLUMN = "superseded_at"

#: Migration 015 (C-K display geometry): corridor.road_segments.display_geom.
SEGMENTS_TABLE = "corridor.road_segments"
DISPLAY_GEOM_COLUMN = "display_geom"


async def relation_exists(conn: Any, qualified_name: str) -> bool:
    """True when ``schema.table`` exists (cached for 30 s per name)."""
    now = time.monotonic()
    hit = _REL_CACHE.get(qualified_name)
    if hit is not None and hit[0] > now:
        return hit[1]
    present = bool(await conn.fetchval(_SQL_REGCLASS, qualified_name))
    _REL_CACHE[qualified_name] = (now + _REL_TTL_SEC, present)
    return present


async def column_exists(conn: Any, qualified_table: str, column: str) -> bool:
    """True when ``schema.table`` has ``column`` (cached for 30 s).

    The readers of an additive migration's column pick their SQL by this
    probe, so they serve on a store that predates the migration and switch
    over within 30 s of it landing — no restart, never a 500.  A failed
    probe answers False and is NOT cached (the pre-migration SQL works on
    both schemas; the next call probes again).
    """
    key = (qualified_table, column)
    now = time.monotonic()
    hit = _COL_CACHE.get(key)
    if hit is not None and hit[0] > now:
        return hit[1]
    try:
        present = bool(await conn.fetchval(_SQL_COLUMN, qualified_table, column))
    except Exception:  # noqa: BLE001 — a probe never takes a reader down
        return False
    _COL_CACHE[key] = (now + _REL_TTL_SEC, present)
    return present


async def routes_supersede_supported(conn: Any) -> bool:
    """events.event_routes carries 018's superseded_at (route updates)."""
    return await column_exists(conn, ROUTES_TABLE, ROUTES_SUPERSEDE_COLUMN)


async def display_geom_supported(conn: Any) -> bool:
    """corridor.road_segments carries 015's display_geom (C-K)."""
    return await column_exists(conn, SEGMENTS_TABLE, DISPLAY_GEOM_COLUMN)


def reset_relation_cache() -> None:
    """Forget every cached relation AND column probe (tests / tooling)."""
    _REL_CACHE.clear()
    _COL_CACHE.clear()


# ---------------------------------------------------------------------------
# Valkey: bounded prefix scan + batched MGET (one round trip per 100 keys)
# ---------------------------------------------------------------------------

MGET_BATCH = 100


async def scan_keys(client: Any, prefix: str, *, max_keys: int) -> tuple[list[str], int]:
    """Every key under ``prefix`` (sorted → deterministic), bounded.

    Returns ``(keys[:max_keys], truncated_after)`` — keys beyond the bound
    are reported, never silently dropped.
    """
    keys: list[str] = []
    async for key in client.scan_iter(match=f"{prefix}*", count=100):
        keys.append(key.decode() if isinstance(key, bytes) else str(key))
    keys.sort()
    return keys[:max_keys], max(0, len(keys) - max_keys)


async def mget_values(client: Any, keys: list[str]) -> list[tuple[str, Any]]:
    """``(key, raw)`` pairs for ``keys`` via MGET in batches of 100."""
    out: list[tuple[str, Any]] = []
    for i in range(0, len(keys), MGET_BATCH):
        chunk = keys[i : i + MGET_BATCH]
        values = await client.mget(chunk)
        out.extend(zip(chunk, values, strict=False))
    return out


async def scan_mget(
    client: Any, prefix: str, *, max_keys: int
) -> tuple[list[tuple[str, Any]], int]:
    """Prefix scan + batched MGET; ``((key, raw) pairs, truncated_after)``."""
    keys, truncated_after = await scan_keys(client, prefix, max_keys=max_keys)
    return await mget_values(client, keys), truncated_after


def json_doc(raw: Any) -> dict | None:
    """Decode one Valkey JSON object payload; None when absent/corrupt."""
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        doc = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


# ---------------------------------------------------------------------------
# Subject resolution (adaptive / brain movement keys → event URNs)
# ---------------------------------------------------------------------------

#: The situation engine keys its subjects by event_id (events) or by the
#: legacy convoy movement id; events may soft-link a movement_id.  One
#: query resolves a batch of subject keys to the events they denote — and
#: to each event's metadata, which carries its live|emulated provenance.
_SQL_SUBJECT_EVENTS = "SELECT e.event_id, e.movement_id, e.metadata FROM events.corridor_events e WHERE e.event_id = ANY($1::uuid[]) OR e.movement_id = ANY($1::uuid[])"  # noqa: E501


def subject_key(subject_id: Any) -> str:
    """Canonical form of an adaptive / brain subject key (uuids lowercase)."""
    key = str(subject_id)
    canon = as_uuid(key)
    return str(canon) if canon is not None else key


@dataclass(frozen=True)
class ResolvedSubjects:
    """Adaptive / brain subject keys resolved to the corridor events they
    denote, with each event's provenance badge.

    ``events``: canonical subject key → event ids (the event itself, or the
    events soft-linking the movement).  ``sources``: event id →
    live|emulated (:func:`event_source` of its metadata).
    """

    events: Mapping[str, list[str]] = field(default_factory=dict)
    sources: Mapping[str, str] = field(default_factory=dict)

    def events_of(self, subject_id: Any) -> list[str]:
        return list(self.events.get(subject_key(subject_id), []))

    def source_of(self, subject_id: Any) -> str:
        """``emulated`` when ANY event the subject denotes is emulated (an
        emulated scenario's band transitions, parameter publishes and
        brain decisions are badged like the scenario itself); ``live``
        otherwise, including subjects that denote no corridor event."""
        emulated = any(self.sources.get(e) == "emulated" for e in self.events_of(subject_id))
        return "emulated" if emulated else "live"


async def resolve_subjects(conn: Any, subject_ids: Iterable[Any]) -> ResolvedSubjects:
    """Resolve subject keys to their events (+ provenance) in ONE query."""
    uuids: list[_uuid.UUID] = []
    seen: set[str] = set()
    for sid in subject_ids:
        u = as_uuid(sid) if sid is not None else None
        if u is not None and str(u) not in seen:
            seen.add(str(u))
            uuids.append(u)
    if not uuids:
        return ResolvedSubjects()
    events: dict[str, list[str]] = {}
    sources: dict[str, str] = {}
    for r in await conn.fetch(_SQL_SUBJECT_EVENTS, uuids):
        eid = str(r["event_id"])
        sources[eid] = event_source(as_dict(r["metadata"]))
        if eid in seen:
            events.setdefault(eid, []).append(eid)
        mid = r["movement_id"]
        if mid is not None and str(mid) in seen:
            events.setdefault(str(mid), []).append(eid)
    return ResolvedSubjects(
        events={k: dedupe(v) for k, v in events.items()}, sources=sources
    )


def subject_urns(
    subject_id: Any, resolved: ResolvedSubjects, *, always_convoy: bool = False
) -> list[str]:
    """URNs for one adaptive/brain subject key.

    An event-keyed subject is ``event:<id>``; anything else is the
    movement ``convoy:<id>`` plus every event soft-linking it.
    """
    norm = subject_key(subject_id)
    events = resolved.events_of(norm)
    urns = [urn("event", e) for e in events]
    if (always_convoy or norm not in events) and is_slug(norm):
        urns.append(urn("convoy", norm))
    return dedupe(urns)


__all__ = [
    "BANDS",
    "DISPLAY_GEOM_COLUMN",
    "DOSSIER_KINDS",
    "ENTITY_KINDS",
    "EPOCH",
    "FEED_KINDS",
    "ROUTES_SUPERSEDE_COLUMN",
    "ROUTES_TABLE",
    "SEGMENTS_TABLE",
    "SQL_LAWFUL_READ",
    "ResolvedSubjects",
    "actor_kind_of",
    "after_wire_ms",
    "age_sec",
    "as_dict",
    "as_list",
    "as_uuid",
    "city_entity",
    "clean_text",
    "column_exists",
    "dedupe",
    "display_geom_supported",
    "event_source",
    "feed_item",
    "find_nul",
    "finite",
    "in_bbox",
    "is_slug",
    "iso_z",
    "json_doc",
    "label_source",
    "mask_plate",
    "mask_plates_in_text",
    "mget_values",
    "named_actor",
    "parse_bbox",
    "parse_entity_id",
    "parse_kinds",
    "parse_ts",
    "relation_exists",
    "reset_relation_cache",
    "resolve_subjects",
    "routes_supersede_supported",
    "scan_keys",
    "scan_mget",
    "subject_key",
    "subject_urns",
    "urn",
    "utc",
    "valid_lonlat",
]
