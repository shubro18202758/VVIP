"""Wire-shape normalisation for the live traffic plane.

Two producers publish Arrow IPC observation batches with DIFFERENT physical
schemas, and both decode through the same :class:`ArrowDecoder`:

    corridor.traffic.live      signal-ingress (pipeline/batch.rs)
        segment_id utf8, speed/congestion/confidence float32,
        source/data_quality uint8
    corridor.traffic.emulated  event-emulator showcase (nats_seam.py)
        segment_id int64, speed/congestion/confidence float64,
        source/data_quality int8

:func:`normalize_batch` maps either onto ONE canonical frame
(:data:`CANONICAL_SCHEMA`) that the cache and DB writers consume:

    timestamp_ms i64 | lon f64 | lat f64 | segment_id i64 | speed_kmh f64 |
    congestion_idx f64 [0,1] | source str (FeedSource value) |
    data_quality str (DataQuality value) | confidence f64 [0,1] |
    origin str ('live' | 'emulated')

Rows that cannot be represented honestly are DROPPED and COUNTED by reason,
never coerced into plausible-looking values: an unparseable segment id, an
unknown source/quality ordinal, a non-finite, negative or impossible speed
(above :data:`MAX_SPEED_KMH`), non-finite coordinates, or a timestamp
outside the accepted window.  Congestion and
confidence are clamped into [0, 1] (the DB CHECK range) — a finite
out-of-range value is a producer rounding artefact, not a different reading.
The emulated subject is ALWAYS persisted as ``synthetic`` (provenance wins
over the emulator's own anomalous marker).

A batch with NO ``data_quality`` column is dropped whole and counted
(``missing_data_quality``): an unlabelled producer can never be presumed
REAL, so it can never reach the real-only hourly rollup the twin
calibrates on (ingest/partitions.py).

ORIGIN (which plane a row came from) rides the canonical frame so the hot
cache can keep the event-emulator's showcase traffic in its OWN key space
(ingest/cache_writer.py): 'emulated' for the emulated subject — the one
caller that stamps a forced quality, or passes ``origin`` explicitly —
'live' for the signal-ingress plane.  traffic.observations has no origin
column (the emulated rows are labelled ``synthetic`` there); the DB writer
ignores it.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import polars as pl

from traffic_oracle.data.models import (
    DATA_QUALITY_ORDINALS,
    FEED_SOURCE_ORDINALS,
    DataQuality,
    FeedSource,
)

#: Row origins (the plane a row came from — never a data-quality claim).
ORIGIN_LIVE = "live"
ORIGIN_EMULATED = "emulated"
ORIGINS = (ORIGIN_LIVE, ORIGIN_EMULATED)

#: Canonical column order/dtypes after normalisation.
CANONICAL_SCHEMA: dict[str, pl.DataType] = {
    "timestamp_ms": pl.Int64(),
    "lon": pl.Float64(),
    "lat": pl.Float64(),
    "segment_id": pl.Int64(),
    "speed_kmh": pl.Float64(),
    "congestion_idx": pl.Float64(),
    "source": pl.String(),
    "data_quality": pl.String(),
    "confidence": pl.Float64(),
    "origin": pl.String(),
}

_REQUIRED_WIRE_COLUMNS = (
    "timestamp_ms", "lon", "lat", "segment_id", "speed_kmh", "source",
)

#: No road-vehicle reading exceeds this (km/h).  Tier-1 already flags
#: anything above 180 km/h as ``road_class_mismatch`` (those rows are kept,
#: labelled anomalous); above 300 km/h it is a unit error or a broken sensor
#: — and a value beyond float32 range cannot even be stored in the REAL
#: column, where one such row would fail every retry of its batch.
MAX_SPEED_KMH = 300.0

# Ordinal → enum VALUE (the cache/model spelling; DB text goes through
# FeedSource.db_value at write time).
_SOURCE_BY_ORDINAL = {k: v.value for k, v in FEED_SOURCE_ORDINALS.items()}
_QUALITY_BY_ORDINAL = {k: v.value for k, v in DATA_QUALITY_ORDINALS.items()}

# Producers that send names instead of ordinals (Rust serde spelling, DB
# spelling, or the enum value itself) — tolerated, mapped explicitly.
_SOURCE_BY_NAME = {
    **{s.value: s.value for s in FeedSource},
    **{s.db_value: s.value for s in FeedSource},
    "GovernmentTraffic": FeedSource.GOVERNMENT_TRAFFIC.value,
    "MappingApi": FeedSource.MAPPING_API.value,
    "FleetGps": FeedSource.FLEET_GPS.value,
    "Crowdsource": FeedSource.CROWDSOURCE.value,
}
_QUALITY_BY_NAME = {
    **{q.value: q.value for q in DataQuality},
    "Real": DataQuality.REAL.value,
    "Anomalous": DataQuality.ANOMALOUS.value,
    "Synthetic": DataQuality.SYNTHETIC.value,
}


@dataclass
class NormalizedBatch:
    """Canonical rows plus per-reason drop counts for one decoded batch."""

    frame: pl.DataFrame
    received_rows: int
    dropped: Counter = field(default_factory=Counter)

    @property
    def rows(self) -> int:
        return self.frame.height


def empty_canonical_frame() -> pl.DataFrame:
    return pl.DataFrame(schema=CANONICAL_SCHEMA)


def is_canonical(df: pl.DataFrame) -> bool:
    """True when ``df`` already carries exactly the canonical schema."""
    return dict(df.schema) == CANONICAL_SCHEMA


def _segment_id_expr(dtype: pl.DataType) -> pl.Expr:
    col = pl.col("segment_id")
    if dtype == pl.String:
        # signal-ingress: utf8 ids ("1001").  Non-numeric ids → null → dropped.
        return col.str.strip_chars().cast(pl.Int64, strict=False)
    if dtype.is_integer():
        return col.cast(pl.Int64, strict=False)
    if dtype.is_float():
        # A float id is only honest when integral (e.g. 1001.0).
        return (
            pl.when(col.is_finite() & (col == col.floor()))
            .then(col)
            .otherwise(None)
            .cast(pl.Int64, strict=False)
        )
    return pl.lit(None, dtype=pl.Int64)


def _enum_expr(
    name: str,
    dtype: pl.DataType,
    by_ordinal: dict[int, str],
    by_name: dict[str, str],
) -> pl.Expr:
    col = pl.col(name)
    if dtype.is_integer():
        return col.cast(pl.Int64).replace_strict(
            by_ordinal, default=None, return_dtype=pl.String
        )
    if dtype == pl.String:
        return col.str.strip_chars().replace_strict(
            by_name, default=None, return_dtype=pl.String
        )
    return pl.lit(None, dtype=pl.String)


def _origin_expr(df: pl.DataFrame, origin: str | None, forced: bool) -> pl.Expr:
    """The canonical ``origin`` column: explicit > forced quality (the
    emulated subject) > an existing valid origin column > 'live'."""
    if origin is not None:
        return pl.lit(origin, dtype=pl.String)
    if forced:
        return pl.lit(ORIGIN_EMULATED, dtype=pl.String)
    if "origin" in df.columns and df.schema["origin"] == pl.String:
        return (
            pl.when(pl.col("origin").is_in(list(ORIGINS)))
            .then(pl.col("origin"))
            .otherwise(pl.lit(ORIGIN_LIVE))
        )
    return pl.lit(ORIGIN_LIVE, dtype=pl.String)


def normalize_batch(
    df: pl.DataFrame,
    *,
    force_quality: DataQuality | None = None,
    origin: str | None = None,
    now_ms: int | None = None,
    max_future_ms: int | None = None,
    max_age_ms: int | None = None,
) -> NormalizedBatch:
    """Map a decoded wire batch (either producer schema) onto canonical rows.

    Args:
        df: ArrowDecoder output (live utf8/uint8 or emulated int64/int8).
        force_quality: overrides every row's data_quality (the emulated
            subject passes ``DataQuality.SYNTHETIC``) — and marks the rows'
            origin 'emulated' unless ``origin`` says otherwise.
        origin: 'live' | 'emulated' — the plane the batch arrived on
            (default: 'emulated' with a forced quality, else 'live').
        now_ms / max_future_ms / max_age_ms: optional timestamp window —
            rows stamped more than ``max_future_ms`` ahead of ``now_ms`` or
            older than ``max_age_ms`` are dropped (``future_timestamp`` /
            ``stale_timestamp``).  Omitted bounds are not enforced.
    """
    if origin is not None and origin not in ORIGINS:
        raise ValueError(f"origin must be one of {ORIGINS}")
    received = df.height
    dropped: Counter = Counter()
    if received == 0:
        return NormalizedBatch(empty_canonical_frame(), 0, dropped)
    if is_canonical(df) and force_quality is None and origin is None and now_ms is None:
        return NormalizedBatch(df, received, dropped)

    missing = [c for c in _REQUIRED_WIRE_COLUMNS if c not in df.columns]
    congestion_col = (
        "congestion_idx" if "congestion_idx" in df.columns else "congestion_index"
    )
    if congestion_col not in df.columns:
        missing.append("congestion_index")
    if missing:
        # A schema we cannot read at all: every row is dropped, loudly.
        dropped["missing_columns"] += received
        return NormalizedBatch(empty_canonical_frame(), received, dropped)

    schema = df.schema
    if force_quality is not None:
        quality = pl.lit(force_quality.value, dtype=pl.String)
    elif "data_quality" in df.columns:
        quality = _enum_expr(
            "data_quality", schema["data_quality"], _QUALITY_BY_ORDINAL, _QUALITY_BY_NAME
        )
    else:
        # No quality label at all: never presumed REAL (the real-only
        # hourly rollup is the twin's calibration input) — dropped, counted.
        dropped["missing_data_quality"] += received
        return NormalizedBatch(empty_canonical_frame(), received, dropped)
    confidence = (
        pl.col("confidence").cast(pl.Float64, strict=False)
        if "confidence" in df.columns
        else pl.lit(1.0, dtype=pl.Float64)
    )

    frame = df.select(
        pl.col("timestamp_ms").cast(pl.Int64, strict=False).alias("timestamp_ms"),
        pl.col("lon").cast(pl.Float64, strict=False).alias("lon"),
        pl.col("lat").cast(pl.Float64, strict=False).alias("lat"),
        _segment_id_expr(schema["segment_id"]).alias("segment_id"),
        pl.col("speed_kmh").cast(pl.Float64, strict=False).alias("speed_kmh"),
        pl.col(congestion_col).cast(pl.Float64, strict=False).alias("congestion_idx"),
        _enum_expr(
            "source", schema["source"], _SOURCE_BY_ORDINAL, _SOURCE_BY_NAME
        ).alias("source"),
        quality.alias("data_quality"),
        confidence.alias("confidence"),
        _origin_expr(df, origin, force_quality is not None).alias("origin"),
    )

    # Ordered drop rules: each row is counted under the FIRST reason it hits.
    rules: list[tuple[str, pl.Expr]] = [
        ("invalid_segment_id", pl.col("segment_id").is_null()),
        ("unknown_source", pl.col("source").is_null()),
        ("unknown_quality", pl.col("data_quality").is_null()),
        (
            "invalid_timestamp",
            pl.col("timestamp_ms").is_null() | (pl.col("timestamp_ms") <= 0),
        ),
        (
            "invalid_speed",
            pl.col("speed_kmh").is_null()
            | ~pl.col("speed_kmh").is_finite()
            | (pl.col("speed_kmh") < 0)
            | (pl.col("speed_kmh") > MAX_SPEED_KMH),
        ),
        (
            "invalid_congestion",
            pl.col("congestion_idx").is_null() | ~pl.col("congestion_idx").is_finite(),
        ),
        (
            "invalid_position",
            pl.col("lon").is_null()
            | pl.col("lat").is_null()
            | ~pl.col("lon").is_finite()
            | ~pl.col("lat").is_finite()
            | (pl.col("lon").abs() > 180.0)
            | (pl.col("lat").abs() > 90.0),
        ),
    ]
    if now_ms is not None and max_future_ms is not None:
        rules.append(
            ("future_timestamp", pl.col("timestamp_ms") > now_ms + max_future_ms)
        )
    if now_ms is not None and max_age_ms is not None:
        rules.append(
            ("stale_timestamp", pl.col("timestamp_ms") < now_ms - max_age_ms)
        )

    for reason, bad in rules:
        n_bad = frame.select(bad.fill_null(True).sum()).item()
        if n_bad:
            dropped[reason] += int(n_bad)
            frame = frame.filter(~bad.fill_null(True))
        if frame.is_empty():
            break

    frame = frame.with_columns(
        pl.col("congestion_idx").clip(0.0, 1.0),
        pl.when(pl.col("confidence").is_finite())
        .then(pl.col("confidence").clip(0.0, 1.0))
        .otherwise(1.0)
        .alias("confidence"),
    )
    return NormalizedBatch(frame, received, dropped)


def ensure_canonical(df: pl.DataFrame) -> pl.DataFrame:
    """Normalise ``df`` unless it already carries the canonical schema."""
    if is_canonical(df):
        return df
    return normalize_batch(df).frame


__all__ = [
    "CANONICAL_SCHEMA",
    "MAX_SPEED_KMH",
    "ORIGINS",
    "ORIGIN_EMULATED",
    "ORIGIN_LIVE",
    "NormalizedBatch",
    "empty_canonical_frame",
    "ensure_canonical",
    "is_canonical",
    "normalize_batch",
]
