"""Incident & scenario-run ingestion into the memory tables.

Two ingestion paths (contract C3):

* live incidents — the brain POSTs /incidents on incident_detected events;
* scenario runs — outcome records shaped exactly like the FROZEN
  traffic-oracle ScenarioResult (evaluate/scenario_sim.py:33-43:
  scenario_id, route_name, total_disruption_vehicle_hours,
  max_queue_length_m, avg_closure_duration_sec, segments_affected,
  estimated_complaints_risk, convoy_transit_time_sec) become
  source='scenario_run' pattern memory, so the twin's simulation history is
  retrievable as "similar past situations".

Leak posture (plan risk register): the kind whitelist is the memory-side
guard. Bodies are composed from structured fields only; free-text operator
notes pass through ``scrub_text`` which redacts obvious credential and
contact patterns before storage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

from . import config
from .encoders import DenseEncoderProtocol, LateInteractionEncoder, truncate_for_embedding
from .store import MemoryStore

# Frozen ScenarioResult field names (evaluate/scenario_sim.py) — everything
# else in the payload is captured into metadata, never into SQL.
_SCENARIO_FIELDS = (
    "scenario_id",
    "route_name",
    "total_disruption_vehicle_hours",
    "max_queue_length_m",
    "avg_closure_duration_sec",
    "segments_affected",
    "estimated_complaints_risk",
    "convoy_transit_time_sec",
)

_CREDENTIAL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[=:]\s*\S+"), "[redacted-credential]"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"), "[redacted-email]"),
    (re.compile(r"\b(?:\+91[- ]?)?[6-9]\d{9}\b"), "[redacted-phone]"),
)


def scrub_text(text: str) -> str:
    """Redact credential-like / contact strings before memory storage."""
    for pattern, replacement in _CREDENTIAL_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _risk_to_severity(estimated_complaints_risk: str) -> str:
    """Map the frozen scenario risk label onto incident severities."""
    return {
        "low": "low",
        "medium": "medium",
        "high": "high",
    }.get(str(estimated_complaints_risk).lower(), "medium")


def _scenario_to_incident_kind(scenario: dict) -> str:
    """Classify a scenario run into the incident_kind whitelist.

    Simulation outcomes model closure-induced disruption, so they land as
    congestion_spike unless the payload tags something more specific via the
    optional ``incident_kind`` hint (still whitelist-checked).
    """
    hint = scenario.get("incident_kind")
    if hint in config.INCIDENT_KINDS:
        return hint
    return "congestion_spike"


@dataclass(frozen=True)
class IngestedIncident:
    incident_id: int
    doc_id: str


class IncidentIngestor:
    """Writes incidents/scenario runs into traffic.incidents + memory_docs."""

    def __init__(
        self,
        store: MemoryStore,
        late_encoder: LateInteractionEncoder,
        dense_encoder: DenseEncoderProtocol,
    ) -> None:
        self._store = store
        self._late = late_encoder
        self._dense = dense_encoder

    async def ingest_live_incident(
        self,
        *,
        incident_kind: str,
        severity: str,
        summary: str,
        segment_ids: Sequence[int] | None = None,
        movement_id: str | None = None,
        details: dict | None = None,
        occurred_at: datetime | None = None,
        embed: bool = True,
    ) -> IngestedIncident | None:
        """Record a live incident and (optionally) embed it as memory.

        Returns None when the caller passed kind/severity values off the
        whitelist — the store would reject them; failing early here gives
        the API a clean 4xx instead of a database error.
        """
        if incident_kind not in config.INCIDENT_KINDS:
            return None
        if severity not in config.INCIDENT_SEVERITIES:
            return None
        summary = scrub_text(truncate_for_embedding(summary))
        occurred = occurred_at or datetime.now(timezone.utc)
        incident_id = await self._store.insert_incident(
            incident_kind=incident_kind,
            severity=severity,
            summary=summary,
            segment_ids=segment_ids,
            movement_id=movement_id,
            details=details,
            source="live",
            occurred_at=occurred,
        )
        doc_id = None
        if embed:
            body = summary if summary else f"{incident_kind} incident"
            doc_id = await self._embed_doc(
                kind="incident",
                ref_id=f"incident:{incident_id}",
                title=f"Incident {incident_kind} ({severity})",
                body=body,
                metadata={
                    "incident_id": incident_id,
                    "incident_kind": incident_kind,
                    "severity": severity,
                    "segment_ids": list(segment_ids or []),
                },
                movement_id=movement_id,
                occurred_at=occurred,
            )
            await self._store.link_memory_doc(incident_id, doc_id)
        return IngestedIncident(incident_id=incident_id, doc_id=doc_id)

    async def ingest_scenario_run(
        self,
        scenario: dict,
        *,
        movement_id: str | None = None,
        scenario_run_id: int | None = None,
        segment_ids: Sequence[int] | None = None,
        occurred_at: datetime | None = None,
        extra: dict | None = None,
    ) -> IngestedIncident | None:
        """Persist one ScenarioResult-shaped dict as pattern memory.

        Unknown fields beyond the frozen ScenarioResult keys are captured
        verbatim into the memory doc metadata (not into incident columns).
        Returns None if the payload is not a scenario result at all.
        """
        if not isinstance(scenario, dict) or "scenario_id" not in scenario:
            return None
        scenario_id = str(scenario.get("scenario_id", ""))
        route_name = scrub_text(str(scenario.get("route_name", "unnamed")))
        disruption = scenario.get("total_disruption_vehicle_hours")
        if not isinstance(disruption, (int, float)):
            return None

        occurred = occurred_at or datetime.now(timezone.utc)
        severity = _risk_to_severity(scenario.get("estimated_complaints_risk", "medium"))
        incident_kind = _scenario_to_incident_kind(scenario)

        outcome = {
            key: scenario.get(key)
            for key in _SCENARIO_FIELDS
            if key in scenario
        }
        details = {"route_name": route_name}
        if extra:
            details.update(extra)

        summary = (
            f"Scenario {scenario_id} ({route_name}): "
            f"disruption {disruption} veh·h, "
            f"max queue {scenario.get('max_queue_length_m')} m, "
            f"convoy transit {scenario.get('convoy_transit_time_sec')} s"
        )
        incident_id = await self._store.insert_incident(
            incident_kind=incident_kind,
            severity=severity,
            summary=scrub_text(summary),
            segment_ids=segment_ids,
            movement_id=movement_id,
            details=details,
            outcome=outcome,
            scenario_run_id=scenario_run_id,
            source="scenario_run",
            occurred_at=occurred,
        )
        body = (
            f"Simulation scenario run {scenario_id} on route {route_name}. "
            f"Predicted disruption {disruption} vehicle-hours; "
            f"max queue {scenario.get('max_queue_length_m')} m; "
            f"average closure {scenario.get('avg_closure_duration_sec')} s; "
            f"segments affected {scenario.get('segments_affected')}; "
            f"complaints risk {scenario.get('estimated_complaints_risk')}; "
            f"convoy transit {scenario.get('convoy_transit_time_sec')} s."
        )
        doc_id = await self._embed_doc(
            kind="scenario_run",
            ref_id=f"scenario:{scenario_id}",
            title=f"Scenario run {scenario_id}: {route_name}",
            body=scrub_text(truncate_for_embedding(body)),
            metadata={
                "scenario_id": scenario_id,
                "route_name": route_name,
                "outcome": outcome,
                "scenario_run_id": scenario_run_id,
            },
            movement_id=movement_id,
            occurred_at=occurred,
        )
        await self._store.link_memory_doc(incident_id, doc_id)
        return IngestedIncident(incident_id=incident_id, doc_id=doc_id)

    async def _embed_doc(
        self,
        *,
        kind: str,
        ref_id: str,
        title: str,
        body: str,
        metadata: dict,
        movement_id: str | None,
        occurred_at: datetime,
    ) -> str:
        token_sets = self._late.encode_documents([body])
        dense_vecs = self._dense.encode([f"{title}. {body}"])
        return await self._store.upsert_document(
            kind=kind,
            ref_id=ref_id,
            title=truncate_for_embedding(title, 300),
            body=body,
            metadata=metadata,
            dense=dense_vecs[0],
            token_vecs=token_sets[0] if token_sets else None,
            movement_id=movement_id,
            occurred_at=occurred_at,
        )


__all__ = ["IncidentIngestor", "IngestedIncident", "scrub_text"]
