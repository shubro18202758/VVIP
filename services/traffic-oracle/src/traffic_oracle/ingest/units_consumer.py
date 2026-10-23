"""Emulated agency-unit positions: NATS ``corridor.units.emulated`` → Valkey.

Contract C-A (producer side).  The event-emulator publishes one clearly
labelled JSON document per agency-unit frame (runner.py
``_deliver_agency_unit`` over tape.py ``_build_agency_frames``), today::

    {"agency": "jan_rakshak", "position": [lon, lat], "source": "emulated",
     "status": "patrolling", "unit_id": "JR-EM-01"}

Replays carry the ``X-Emulation-Replay: <run_id>`` header.  Each document
is normalised and written to ``units:emulated:{unit_id}`` with ``EX 30`` as::

    {unit_id, agency, label, lon, lat, status, heading, speed_kmh, t,
     source: "emulated_patrol", run_id}

so the city picture (WP-02 /units + /city) can merge patrols that age out
on their own 30 s after the emulator stops.  Unknown agency/status strings
are mapped to the nearest contract value or DROPPED and counted — never
guessed into a plausible unit.  ``source`` is always ``emulated_patrol``:
the subject is emulated-only, so nothing here can ever surface as live.
Ethics (C6): agencies and units only — no person, face or name field is
read or written.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any

import structlog

from traffic_oracle.units.agencies import agency_label

logger = structlog.get_logger(__name__)

SUBJECT_UNITS_EMULATED = "corridor.units.emulated"
UNIT_KEY_PREFIX = "units:emulated:"
UNIT_TTL_SEC = 30
UNIT_SOURCE = "emulated_patrol"
REPLAY_HEADER = "X-Emulation-Replay"

_UNIT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")

#: Producer agency spellings → C-A agency codes (the units/agencies.py set).
_AGENCY_ALIASES = {
    "jan_rakshak": "jan_rakshak",
    "janrakshak": "jan_rakshak",
    "jan-rakshak": "jan_rakshak",
    "jan rakshak": "jan_rakshak",
    "jr": "jan_rakshak",
    "police": "police",
    "police_control": "police",
    "pcr": "police",
    "traffic": "traffic",
    "traffic_police": "traffic",
    "ambulance": "ambulance",
    "108": "ambulance",
    "ems": "ambulance",
    "medical": "ambulance",
    "fire": "fire",
    "fire_brigade": "fire",
    "fire_and_emergency": "fire",
}

#: Producer status spellings → C-A status vocabulary.
_STATUS_ALIASES = {
    "patrol": "patrol",
    "patrolling": "patrol",
    "on_patrol": "patrol",
    "available": "available",
    "idle": "available",
    "standby": "available",
    "en_route": "en_route",
    "enroute": "en_route",
    "en-route": "en_route",
    "dispatched": "en_route",
    "responding": "en_route",
    "on_scene": "on_scene",
    "onscene": "on_scene",
    "on-scene": "on_scene",
    "arrived": "on_scene",
    "returning": "returning",
    "return": "returning",
    "rtb": "returning",
}


class UnitDocError(ValueError):
    """A unit document that cannot be represented under C-A (carries reason)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _iso_utc(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError, OverflowError):  # OverflowError: a 400-digit int
        return None
    return f if math.isfinite(f) else None


def _position(doc: dict) -> tuple[float, float]:
    pos = doc.get("position")
    if isinstance(pos, (list, tuple)) and len(pos) >= 2:
        lon, lat = _finite(pos[0]), _finite(pos[1])
    elif isinstance(pos, dict):
        lon, lat = _finite(pos.get("lon")), _finite(pos.get("lat"))
    else:
        lon, lat = _finite(doc.get("lon")), _finite(doc.get("lat"))
    if lon is None or lat is None or abs(lon) > 180.0 or abs(lat) > 90.0:
        raise UnitDocError("invalid_position")
    return lon, lat


def _timestamp(doc: dict, received_at: datetime) -> str:
    raw = doc.get("t")
    if isinstance(raw, str) and raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return _iso_utc(parsed)
    return _iso_utc(received_at)


def normalize_unit_doc(
    doc: Any,
    *,
    received_at: datetime | None = None,
    replay_run_id: str | None = None,
) -> dict:
    """Map one emulator unit document onto the C-A Valkey JSON shape.

    Raises :class:`UnitDocError` (``.reason`` is the drop counter key) for
    anything that cannot be represented honestly.
    """
    if not isinstance(doc, dict):
        raise UnitDocError("not_an_object")
    unit_id = doc.get("unit_id")
    if not isinstance(unit_id, str) or not _UNIT_ID_RE.match(unit_id.strip()):
        raise UnitDocError("invalid_unit_id")
    unit_id = unit_id.strip()

    agency_raw = doc.get("agency")
    agency = (
        _AGENCY_ALIASES.get(agency_raw.strip().lower())
        if isinstance(agency_raw, str)
        else None
    )
    if agency is None:
        raise UnitDocError("unknown_agency")

    status_raw = doc.get("status")
    if status_raw is None:
        status = "patrol"  # the subject carries patrol frames by definition
    elif isinstance(status_raw, str):
        status = _STATUS_ALIASES.get(status_raw.strip().lower())
        if status is None:
            raise UnitDocError("unknown_status")
    else:
        raise UnitDocError("unknown_status")

    lon, lat = _position(doc)

    heading = _finite(doc.get("heading"))
    if heading is not None:
        heading = round(heading % 360.0, 1)
    speed = _finite(doc.get("speed_kmh"))
    if speed is not None and speed < 0:
        speed = None

    label_raw = doc.get("label")
    label = (
        label_raw.strip()[:80]
        if isinstance(label_raw, str) and label_raw.strip()
        else f"{agency_label(agency)} {unit_id}"
    )

    run_raw = doc.get("run_id")
    run_id = str(run_raw)[:64] if run_raw not in (None, "") else replay_run_id

    received_at = received_at or datetime.now(timezone.utc)
    return {
        "unit_id": unit_id,
        "agency": agency,
        "label": label,
        "lon": round(lon, 7),
        "lat": round(lat, 7),
        "status": status,
        "heading": heading,
        "speed_kmh": round(speed, 1) if speed is not None else None,
        "t": _timestamp(doc, received_at),
        "source": UNIT_SOURCE,
        "run_id": run_id,
    }


class UnitsConsumer:
    """Writes normalised emulated unit positions to Valkey (C-A)."""

    def __init__(self, valkey_client: Any, ttl_sec: int = UNIT_TTL_SEC) -> None:
        self._valkey = valkey_client
        self._ttl_sec = ttl_sec
        self.written_total = 0
        self.dropped: Counter = Counter()

    async def handle(
        self,
        data: bytes,
        headers: dict[str, str] | None = None,
        received_at: datetime | None = None,
    ) -> bool:
        """Process one NATS payload. Returns True when a key was written."""
        try:
            doc = json.loads(data)
        except (ValueError, UnicodeDecodeError, RecursionError):
            self.dropped["invalid_json"] += 1
            return False
        replay = (headers or {}).get(REPLAY_HEADER)
        try:
            unit = normalize_unit_doc(
                doc,
                received_at=received_at,
                replay_run_id=str(replay)[:64] if replay else None,
            )
        except UnitDocError as exc:
            self.dropped[exc.reason] += 1
            logger.debug("ingest.units.dropped", reason=exc.reason)
            return False
        await self._valkey.set(
            f"{UNIT_KEY_PREFIX}{unit['unit_id']}",
            json.dumps(unit, separators=(",", ":")),
            ex=self._ttl_sec,
        )
        self.written_total += 1
        return True


__all__ = [
    "REPLAY_HEADER",
    "SUBJECT_UNITS_EMULATED",
    "UNIT_KEY_PREFIX",
    "UNIT_SOURCE",
    "UNIT_TTL_SEC",
    "UnitDocError",
    "UnitsConsumer",
    "normalize_unit_doc",
]
