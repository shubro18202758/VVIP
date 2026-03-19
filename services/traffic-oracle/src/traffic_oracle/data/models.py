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


class DataQuality(str, enum.Enum):
    REAL = "real"
    ANOMALOUS = "anomalous"
    SYNTHETIC = "synthetic"


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
