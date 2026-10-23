"""Forecast quantile contract — shared producer/consumer helpers.

The contract (wave-2): a per-horizon forecast row MAY carry an optional
``quantiles`` object ALONGSIDE the existing point fields::

    {
        "speed_kmh": <float>,          # point fields stay mandatory and
        "congestion_idx": <float>,     # unchanged (point-only rows remain
        "quantiles": {                 # legal — absent == point-only),
            "speed_kmh": {"p10": f, "p50": f, "p90": f},
            "congestion_idx": {"p10": f, "p50": f, "p90": f},
        },
    }

Producers (foundation rung) attach ``quantiles`` only when the artifact
supplies valid bands; point-only producers (DSTGAT head, heuristic fallback)
omit it and label the omission through the forecaster's
``last_quantile_reason`` side channel — never by mutating point values.

Consumers MUST NOT trust ``quantiles`` blindly: :func:`validated_bands`
returns ``(bands, None)`` only when the object is present, well-formed and
monotone (``p10 <= p50 <= p90`` per field); otherwise it returns
``(None, reason)`` and the caller degrades to the row's point fields with
that labelled reason. This module is numpy/stdlib-only so it loads in the
pure-python gate venv.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

import structlog

logger = structlog.get_logger(__name__)

#: Exact quantile level keys the contract allows, in monotone order.
QUANTILE_LEVEL_KEYS: tuple[str, ...] = ("p10", "p50", "p90")
#: Fields every ``quantiles`` object must band.
BANDED_FIELDS: tuple[str, ...] = ("speed_kmh", "congestion_idx")

#: Reason tokens returned by :func:`validated_bands` (consumer side).
REASON_ABSENT = "quantiles_absent"
REASON_MALFORMED = "quantiles_malformed"
REASON_NON_MONOTONE = "quantiles_non_monotone"


class FoundationInputError(ValueError):
    """Forecast-contract failure with a machine-readable reason token.

    Defined HERE (the dependency-free contract module) so both producer
    layers can raise it — :func:`make_quantiles` below and the t0 adapter
    (:mod:`traffic_oracle.predict.nn.foundation_adapter`, which re-exports
    this class). The reason is the first whitespace-delimited word of the
    message (e.g. ``speed_bands_not_finite``); FlowForecaster maps it to
    the ``foundation_input:<reason>`` degradation label. Raising plain
    ``ValueError`` here would ESCAPE the forecaster's
    ``except FoundationInputError`` rung boundary — NaN bands must degrade
    loudly to the next rung, not crash the caller (review finding).
    """


def derive_congestion_idx(speed_kmh: float, free_flow_kmh: float) -> float:
    """congestion_idx = clip(1 - speed/free_flow, 0, 1) — labelled derivation.

    Same formula the simulation engine uses for its speed->congestion view.
    Callers pass the SEGMENT's own free flow — the foundation adapter uses
    each segment's speed limit (``corridor.road_segments.speed_limit_kmh``,
    fed by predict/history_feed.py), so a 100 km/h expressway crawling at
    60 km/h reads 0.4 congested instead of 0.0 — and fall back to the 60 km/h
    convention (``speed_norm = speed_kmh / 60.0`` in the DSTGAT feature
    build) only for a segment without one.
    """
    if free_flow_kmh <= 0:
        raise ValueError(f"free_flow_not_positive: {free_flow_kmh!r}")
    if not math.isfinite(speed_kmh):
        # NaN would silently propagate through min/max (platform order
        # dependent) into the congestion point value — refuse it loudly.
        raise FoundationInputError(f"speed_not_finite: {speed_kmh!r}")
    v = 1.0 - speed_kmh / free_flow_kmh
    return float(min(1.0, max(0.0, v)))


def make_quantiles(
    speed_p10: float,
    speed_p50: float,
    speed_p90: float,
    free_flow_kmh: float,
) -> dict[str, dict[str, float]]:
    """Producer side: build the exact contract ``quantiles`` object.

    Speed bands must already be finite and monotone (p10 <= p50 <= p90) —
    callers validate before calling; this function re-checks and raises
    rather than emitting a malformed object. NaN is checked FIRST with its
    own token: NaN comparisons are all False, so a bare monotonicity test
    would mislabel NaN as "not monotone" (or worse, pass it downstream).
    Congestion bands are DERIVED through the decreasing transform, so the
    speed band edges map in REVERSED order: fast upper speed bound -> low
    congestion bound (``p10``), keeping ``p10 <= p50 <= p90`` true in
    congestion space as well.
    """
    if not (
        math.isfinite(speed_p10)
        and math.isfinite(speed_p50)
        and math.isfinite(speed_p90)
    ):
        raise FoundationInputError(
            f"speed_bands_not_finite: p10={speed_p10} p50={speed_p50} "
            f"p90={speed_p90}"
        )
    if not (speed_p10 <= speed_p50 <= speed_p90):
        raise FoundationInputError(
            f"speed_bands_not_monotone: p10={speed_p10} p50={speed_p50} "
            f"p90={speed_p90}"
        )
    return {
        "speed_kmh": {
            "p10": float(speed_p10),
            "p50": float(speed_p50),
            "p90": float(speed_p90),
        },
        "congestion_idx": {
            "p10": derive_congestion_idx(speed_p90, free_flow_kmh),
            "p50": derive_congestion_idx(speed_p50, free_flow_kmh),
            "p90": derive_congestion_idx(speed_p10, free_flow_kmh),
        },
    }


def validated_bands(
    row: Mapping[str, object],
) -> tuple[dict[str, dict[str, float]] | None, str | None]:
    """Consumer side: read a forecast row's ``quantiles`` per the contract.

    Returns ``(bands, None)`` when the row carries a well-formed, monotone
    ``quantiles`` object — the caller may use the bands. Otherwise returns
    ``(None, reason)`` with one of:

        ``quantiles_absent``                      — no ``quantiles`` key
            (point-only producer; degrade to the point fields);
        ``quantiles_malformed:<detail>``          — present but not the
            exact contract shape ({field: {p10, p50, p90}} floats);
        ``quantiles_non_monotone:<field>``        — p10 <= p50 <= p90
            violated for that field; fall back to the point values.
    """
    if "quantiles" not in row:
        return None, REASON_ABSENT
    quant = row["quantiles"]
    if not isinstance(quant, Mapping):
        return None, f"{REASON_MALFORMED}:not_a_mapping"
    if set(quant) != set(BANDED_FIELDS):
        return None, f"{REASON_MALFORMED}:fields {sorted(map(str, quant))}"
    for field in BANDED_FIELDS:
        band = quant[field]
        if not isinstance(band, Mapping) or set(band) != set(QUANTILE_LEVEL_KEYS):
            return None, f"{REASON_MALFORMED}:{field} levels"
        try:
            p10, p50, p90 = (float(band[k]) for k in QUANTILE_LEVEL_KEYS)
        except (TypeError, ValueError):
            return None, f"{REASON_MALFORMED}:{field} not float-coercible"
        if not (p10 <= p50 <= p90):
            return None, f"{REASON_NON_MONOTONE}:{field}"
    bands: dict[str, dict[str, float]] = {}
    for field in BANDED_FIELDS:
        bands[field] = {
            k: float(quant[field][k]) for k in QUANTILE_LEVEL_KEYS  # type: ignore[index]
        }
    return bands, None
