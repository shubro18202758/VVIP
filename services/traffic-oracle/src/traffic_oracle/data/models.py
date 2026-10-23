"""Pydantic models mirroring Rust structs and database schema."""

from __future__ import annotations

import enum
from datetime import datetime

from pydantic import BaseModel, Field


class FeedSource(str, enum.Enum):
    GOVERNMENT_TRAFFIC = "government_traffic"
    MAPPING_API = "mapping_api"
    FLEET_GPS = "fleet_gps"
    CROWDSOURCE = "crowdsource"

    @property
    def db_value(self) -> str:
        """The ``traffic.observations.source`` CHECK vocabulary (001).

        The enum value mirrors the Rust ``FeedSource`` name, but the
        observations CHECK spells the government feed ``'government'`` —
        writing ``'government_traffic'`` violates it, so every DB writer
        goes through this explicit map, never ``.value``.
        """
        return _FEED_SOURCE_DB_VALUES[self]


_FEED_SOURCE_DB_VALUES: dict[FeedSource, str] = {
    FeedSource.GOVERNMENT_TRAFFIC: "government",
    FeedSource.MAPPING_API: "mapping_api",
    FeedSource.FLEET_GPS: "fleet_gps",
    FeedSource.CROWDSOURCE: "crowdsource",
}

#: Wire ordinals (signal-ingress pipeline/batch.rs ``source`` column; the
#: event-emulator nats_seam uses the same table) → FeedSource.
FEED_SOURCE_ORDINALS: dict[int, FeedSource] = {
    0: FeedSource.GOVERNMENT_TRAFFIC,
    1: FeedSource.MAPPING_API,
    2: FeedSource.FLEET_GPS,
    3: FeedSource.CROWDSOURCE,
}


class DataQuality(str, enum.Enum):
    REAL = "real"
    ANOMALOUS = "anomalous"
    SYNTHETIC = "synthetic"


#: Wire ordinals (Rust ``DataQuality::as_u8``) → DataQuality.  The enum
#: values ARE the ``traffic.observations.data_quality`` CHECK vocabulary (003).
DATA_QUALITY_ORDINALS: dict[int, DataQuality] = {
    0: DataQuality.REAL,
    1: DataQuality.ANOMALOUS,
    2: DataQuality.SYNTHETIC,
}


class TrafficObservation(BaseModel):
    """Canonical traffic observation — mirrors Rust TrafficObservation."""

    timestamp_ms: int
    lon: float
    lat: float
    segment_id: int
    speed_kmh: float
    congestion_idx: float = Field(ge=0.0, le=1.0)
    source: FeedSource
    data_quality: DataQuality = DataQuality.REAL
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class RoadSegment(BaseModel):
    """Road segment from corridor.road_segments."""

    segment_id: int
    osm_way_id: int | None = None
    road_name: str | None = None
    road_class: str
    lanes: int = 2
    speed_limit_kmh: float = 50.0
    oneway: bool = False
    geom_wkt: str | None = None


class SyntheticObservation(BaseModel):
    """Gap-filling synthetic observation."""

    synthetic_id: int | None = None
    segment_id: int
    timestamp_utc: datetime
    speed_kmh: float
    congestion_idx: float
    generation_method: str
    confidence: float = Field(ge=0.0, le=1.0)
    source_segments: list[int] = Field(default_factory=list)


class AnomalyRecord(BaseModel):
    """Anomaly log entry from traffic.anomaly_log."""

    anomaly_id: int | None = None
    segment_id: int
    timestamp_utc: datetime
    anomaly_type: str
    severity: str
    details: dict = Field(default_factory=dict)
