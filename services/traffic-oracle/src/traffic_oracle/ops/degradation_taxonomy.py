"""Degradation taxonomy — mine convoy-brain's decisions_log audit trail.

The ledger reports what the audit trail ACTUALLY says: convoy-brain's
ConvoyContextStore (``memory/convoy_context.py``) persists every
movement's ``decisions_log`` to Valkey, and this module groups the
degradation/failure rows in it into a recurring-cause taxonomy.  It
fabricates nothing — an empty log yields an honest empty taxonomy, and
every entry carries a ``sample_ref`` pointing at one real row.

Row shapes the schema actually carries (writers, by phase):

* ``decider_checkpoint`` (decider.py — Wave-1 latch / tier-0 gate audits):
  ``tier`` in ``decider | novelty_latched | tier0_cache_hit | gemma |
  rate_limited`` (``unavailable`` appears only in the CheckpointResult
  comment — no writer emits it today; the fallback rule below still
  covers it if one ever does), plus uniform ``novelty_latch`` and
  ``tier0_cache`` sub-rows whose ``status`` enums are closed sets
  (novel/covered/insufficient_evidence/disabled_unreachable/
  disabled_not_configured/skipped_low_confidence and hit/miss_no_entry/
  miss_ttl_expired/miss_novel/miss_below_routine_floor/
  miss_no_similarity_evidence/miss_ineligible_trigger/
  disabled_kill_switch).  Fallback rows carry a ``reason`` string.
  NOTE: the pre-movement flow stores the checkpoint audit in its state
  dict instead of appending it (pre_movement.py
  ``state["decider_checkpoint"] = checkpoint.audit``) — the ONLY
  degradation evidence that flow leaves in decisions_log is its alert
  rows, so the alert rule below must never miss one.
* operator-alert rows, by SHAPE not by phase: every alert writer tags
  its rows with a ``type`` field (decider_unavailable,
  novel_situation_confident_answer, novelty_check_unavailable,
  low_confidence_decision, …) plus a ``<flow>_alert`` phase — four
  exist today (operator_alert, live_escort_alert ×2, pre_movement_alert,
  post_clearance_alert) and a phase allowlist already rotted once
  (pre_movement_alert rows were silently treated as healthy, showing a
  false EMPTY ledger over a log full of low_confidence_decision alerts).
  No other row family carries ``type`` (checkpoint/lifecycle/turn rows
  never do), so the presence of a non-empty string ``type`` on a
  non-checkpoint row IS the alert signature — any future
  ``<flow>_alert`` phase classifies without touching this module.
* ``orchestrator_turn`` (orchestrator.py): carries ``action``, which is
  a failure verb (``max_rounds_exceeded`` / ``timeout`` / ``error``)
  exactly when the turn failed — per-tool retry exhaustion surfaces
  only through these turn outcomes; it is not a row of its own.
* ``pre_movement`` / ``live_escort`` / ``post_clearance`` lifecycle
  rows: healthy bookkeeping, never classified.

Cause families emitted (one row can yield more than one — a latched
checkpoint row is BOTH the latch firing and the tier-0 miss that
novelty forced; they are distinct audit facts and are counted
separately, deliberately):

* ``checkpoint_fallback:<tier>`` — any checkpoint tier that is neither
  healthy (``decider``, ``tier0_cache_hit``) nor handled elsewhere
  (``novelty_latched``, ``rate_limited``): today that is ``gemma``
  (degradation events).  The rule is negative so a future fallback tier
  surfaces instead of silently vanishing.
* ``checkpoint_rate_limited`` — cadence refusal (no strategic answer
  served for this checkpoint).
* ``novelty_latch:novel`` — the Wave-1 latch firing (confident answer
  withheld, tactical took over).
* ``novelty_latch:disabled_unreachable`` — memory outage disabled the
  latch honestly (recurring outages are themselves a failure cause).
* ``novelty_latch:insufficient_evidence`` — all-dense-fallback memory
  results: no comparable similarity evidence (recorded limitation).
* ``tier0:<miss status>`` — tier-0 routine-cache misses that mean the
  ARMED gate failed to serve (``miss_no_entry``, ``miss_ttl_expired``,
  ``miss_novel``, ``miss_below_routine_floor``,
  ``miss_no_similarity_evidence``).  ``miss_ineligible_trigger`` (the
  gate correctly declining operator_chat) and ``disabled_kill_switch``
  (tier-0 deliberately off) are configuration states, not failures —
  counting them would drown the ledger in non-degradation noise, so
  they never classify.
* ``alert:<type>`` — operator alert rows by type (shape rule above).
* ``orchestrator:<failed action>`` — failed orchestrator turns.

Everything here is pure stdlib: no I/O, no clock, no fastapi — the
Valkey reading and the HTTP surface live in ``router.py`` so the gate
venv can exercise the taxonomy logic on synthetic rows alone.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

#: Checkpoint tiers handled by their own cause (or healthy) — see
#: :data:`_HEALTHY_TIERS`.  The fallback rule is NEGATIVE: any emitted
#: tier outside the handled sets classifies as ``checkpoint_fallback:``
#: so a future fallback tier surfaces instead of silently vanishing
#: (``unavailable`` is in the CheckpointResult comment but no writer
#: emits it today; if one starts, it is already classified).
_HANDLED_TIERS = frozenset({"novelty_latched", "rate_limited"})

#: Checkpoint tiers that mean a healthy strategic answer was served.
_HEALTHY_TIERS = frozenset({"decider", "tier0_cache_hit"})

#: Novelty-latch sub-statuses that are failure/limitation signals.  ``novel``
#: is emitted via the checkpoint tier instead (tier ``novelty_latched`` ⇔
#: escalate ⇔ status ``novel`` — one event, one cause); ``covered`` is
#: healthy, ``skipped_low_confidence`` is the confidence gate's business and
#: ``disabled_not_configured`` is wiring, not a recurring failure.
_LATCH_SIGNAL_STATUSES = frozenset({"disabled_unreachable", "insufficient_evidence"})

#: Orchestrator turn actions that mark the turn as failed.
_FAILED_TURN_ACTIONS = frozenset({"max_rounds_exceeded", "timeout", "error"})

#: Tier-0 miss statuses that are degradation signals from the ARMED gate.
#: ``miss_ineligible_trigger`` (operator_chat is excluded by design) and
#: ``disabled_kill_switch`` (tier-0 off on purpose) are configuration
#: states — recurring "failures" made of those would be noise, so they are
#: deliberately absent here.
_TIER0_MISS_SIGNALS = frozenset(
    {
        "miss_no_entry",
        "miss_ttl_expired",
        "miss_novel",
        "miss_below_routine_floor",
        "miss_no_similarity_evidence",
    }
)


def _row_timestamp(row: Mapping[str, Any]) -> float | None:
    """Best-effort epoch-seconds read of a row's ``timestamp`` field.

    The writers use ``time.time()`` floats; anything else (missing,
    non-numeric) is tolerated as ``None`` — the row still counts, it just
    does not participate in first/last-seen math.
    """
    raw = row.get("timestamp")
    if isinstance(raw, bool):  # bool is an int subclass — not a timestamp
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw)
        except ValueError:
            return None
    return None


def _sub_row(row: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """Fetch a dict sub-row (novelty_latch / tier0_cache) defensively."""
    value = row.get(key)
    return value if isinstance(value, Mapping) else {}


def classify_row(row: Mapping[str, Any]) -> list[str]:
    """Causes a single decisions_log row contributes to the taxonomy.

    Returns [] for healthy rows (healthy checkpoints, lifecycle
    bookkeeping, successful turns).  Order is stable (checkpoint tier,
    then latch sub-status, then tier-0 status, then alert/turn families)
    but callers aggregate by cause, so order only matters for tests.
    """
    if not isinstance(row, Mapping):
        return []

    causes: list[str] = []
    phase = row.get("phase")

    if phase == "decider_checkpoint":
        tier = row.get("tier")
        if tier == "novelty_latched":
            causes.append("novelty_latch:novel")
        elif tier == "rate_limited":
            causes.append("checkpoint_rate_limited")
        elif isinstance(tier, str) and tier and tier not in _HEALTHY_TIERS:
            # NEGATIVE fallback rule: any tier that is neither healthy nor
            # handled above dropped the strategic tier (today: "gemma").
            # A tier no writer currently emits ("unavailable") or a future
            # fallback tier classifies here instead of silently vanishing.
            causes.append(f"checkpoint_fallback:{tier}")

        latch_status = _sub_row(row, "novelty_latch").get("status")
        if latch_status in _LATCH_SIGNAL_STATUSES:
            causes.append(f"novelty_latch:{latch_status}")

        tier0_status = _sub_row(row, "tier0_cache").get("status")
        if tier0_status in _TIER0_MISS_SIGNALS:
            causes.append(f"tier0:{tier0_status}")
        return causes

    # Operator-alert rows, BY SHAPE: every alert writer tags its rows with
    # a ``type`` plus a ``<flow>_alert`` phase — four phases exist today
    # (operator_alert, live_escort_alert, pre_movement_alert,
    # post_clearance_alert) and a phase allowlist already rotted once
    # (pre_movement_alert rows were treated as healthy, showing a FALSE
    # empty ledger over a log full of low_confidence_decision alerts).
    # No other row family carries ``type``, so its presence on a
    # non-checkpoint row IS the alert signature — future ``<flow>_alert``
    # phases classify without touching this module.
    row_type = row.get("type")
    if isinstance(row_type, str) and row_type:
        causes.append(f"alert:{row_type}")
        return causes

    if phase == "orchestrator_turn":
        action = row.get("action")
        if action in _FAILED_TURN_ACTIONS:
            causes.append(f"orchestrator:{action}")

    return causes


def _sample_ref(movement_id: str | None, row: Mapping[str, Any], ts: float | None) -> str:
    """Pointer to one real row: movement · phase · timestamp.

    Enough for an operator to pull ``convoy:context:{movement_id}`` and
    find the exact audit row; ``?`` where the row carried no timestamp.
    """
    phase = row.get("phase")
    phase_str = phase if isinstance(phase, str) and phase else "?"
    ts_str = f"{ts:.3f}" if ts is not None else "?"
    return f"{movement_id or '?'}:{phase_str}:{ts_str}"


def build_taxonomy(
    contexts: Iterable[tuple[str | None, Iterable[Mapping[str, Any]]]],
) -> dict[str, Any]:
    """Group decisions_log degradation rows into the recurring-cause taxonomy.

    ``contexts`` yields ``(movement_id, decisions_log_rows)`` pairs — the
    movement envelope is required because several row families (alert rows,
    orchestrator turns, lifecycle rows) do not repeat the movement id
    inside the row.  Deterministic and side-effect free: same input, same
    output, no clock (``generated_at`` belongs to the HTTP layer).

    Returns ``{"entries": [...], "contexts_scanned": n, "rows_scanned": m}``
    with entries sorted by count desc then cause asc; each entry is exactly
    ``{cause, count, first_seen, last_seen, sample_ref}`` where the sample
    references the EARLIEST occurrence and seen values are epoch seconds
    (None when the contributing rows carried no parsable timestamp).  An
    empty log returns empty entries — never fabricated.
    """
    # cause -> {count, first, last, sample_ref} (first/last None until a
    # parsable timestamp arrives; sample pinned to the earliest row seen).
    agg: dict[str, dict[str, Any]] = {}
    contexts_scanned = 0
    rows_scanned = 0

    for movement_id, rows in contexts:
        contexts_scanned += 1
        for row in rows:
            rows_scanned += 1
            causes = classify_row(row)
            if not causes:
                continue
            ts = _row_timestamp(row)
            for cause in causes:
                entry = agg.get(cause)
                if entry is None:
                    agg[cause] = {
                        "count": 1,
                        "first": ts,
                        "last": ts,
                        "sample_ref": _sample_ref(movement_id, row, ts),
                    }
                    continue
                entry["count"] += 1
                if ts is not None:
                    if entry["first"] is None or ts < entry["first"]:
                        # Earliest parsable timestamp so far — the sample
                        # follows it (a leading timestamp-less row's "?"
                        # reference yields to the first datable evidence).
                        entry["first"] = ts
                        entry["sample_ref"] = _sample_ref(movement_id, row, ts)
                    if entry["last"] is None or ts > entry["last"]:
                        entry["last"] = ts

    entries = [
        {
            "cause": cause,
            "count": data["count"],
            "first_seen": data["first"],
            "last_seen": data["last"],
            "sample_ref": data["sample_ref"],
        }
        for cause, data in agg.items()
    ]
    entries.sort(key=lambda e: (-e["count"], e["cause"]))
    return {
        "entries": entries,
        "contexts_scanned": contexts_scanned,
        "rows_scanned": rows_scanned,
    }


__all__ = ["build_taxonomy", "classify_row"]
