"""Tests for Pydantic data models."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from traffic_oracle.data.models import (
    AnomalyRecord,
    DataQuality,
    FeedSource,
    RoadSegment,
    SyntheticObservation,
    TrafficObservation,
)


class TestTrafficObservation:
    def test_valid_observation(self):
        obs = TrafficObservation(
            timestamp_ms=1710000000000,
            lon=77.2090,
            lat=28.6139,
            segment_id=100,
            speed_kmh=50.0,
            congestion_idx=0.3,
            source=FeedSource.FLEET_GPS,
        )
        assert obs.data_quality == DataQuality.REAL
        assert obs.confidence == 1.0

    def test_congestion_idx_bounds(self):
        with pytest.raises(ValidationError):
            TrafficObservation(
                timestamp_ms=1710000000000,
                lon=77.0, lat=28.0, segment_id=100,
                speed_kmh=50.0, congestion_idx=1.5,  # > 1.0
                source=FeedSource.FLEET_GPS,
            )

    def test_congestion_idx_negative(self):
        with pytest.raises(ValidationError):
            TrafficObservation(
                timestamp_ms=1710000000000,
                lon=77.0, lat=28.0, segment_id=100,
                speed_kmh=50.0, congestion_idx=-0.1,
                source=FeedSource.FLEET_GPS,
            )

    def test_confidence_bounds(self):
        with pytest.raises(ValidationError):
            TrafficObservation(
                timestamp_ms=1710000000000,
                lon=77.0, lat=28.0, segment_id=100,
                speed_kmh=50.0, congestion_idx=0.3,
                source=FeedSource.FLEET_GPS,
                confidence=1.5,
            )

    def test_json_roundtrip(self):
        obs = TrafficObservation(
            timestamp_ms=1710000000000,
            lon=77.2090, lat=28.6139, segment_id=100,
            speed_kmh=50.0, congestion_idx=0.3,
            source=FeedSource.FLEET_GPS,
            data_quality=DataQuality.ANOMALOUS,
            confidence=0.7,
        )
        json_str = obs.model_dump_json()
        restored = TrafficObservation.model_validate_json(json_str)
        assert restored.speed_kmh == 50.0
        assert restored.data_quality == DataQuality.ANOMALOUS
        assert restored.confidence == 0.7

    def test_all_feed_sources(self):
        for source in FeedSource:
            obs = TrafficObservation(
                timestamp_ms=1710000000000,
                lon=77.0, lat=28.0, segment_id=1,
                speed_kmh=50.0, congestion_idx=0.5,
                source=source,
            )
            assert obs.source == source

    def test_all_data_qualities(self):
        for quality in DataQuality:
            obs = TrafficObservation(
                timestamp_ms=1710000000000,
                lon=77.0, lat=28.0, segment_id=1,
                speed_kmh=50.0, congestion_idx=0.5,
                source=FeedSource.FLEET_GPS,
                data_quality=quality,
            )
            assert obs.data_quality == quality


class TestRoadSegment:
    def test_defaults(self):
        seg = RoadSegment(segment_id=1, road_class="primary")
        assert seg.lanes == 2
        assert seg.speed_limit_kmh == 50.0
        assert seg.oneway is False
        assert seg.osm_way_id is None

    def test_full_segment(self):
        seg = RoadSegment(
            segment_id=42,
            osm_way_id=123456,
            road_name="National Highway 48",
            road_class="motorway",
            lanes=4,
            speed_limit_kmh=120.0,
            oneway=True,
            geom_wkt="LINESTRING(77.0 28.0, 77.1 28.1)",
        )
        assert seg.lanes == 4
        assert seg.oneway is True


class TestSyntheticObservation:
    def test_valid(self):
        obs = SyntheticObservation(
            segment_id=100,
            timestamp_utc=datetime.now(timezone.utc),
            speed_kmh=45.0,
            congestion_idx=0.4,
            generation_method="historical_match",
            confidence=0.8,
        )
        assert obs.source_segments == []

    def test_with_source_segments(self):
        obs = SyntheticObservation(
            segment_id=100,
            timestamp_utc=datetime.now(timezone.utc),
            speed_kmh=45.0,
            congestion_idx=0.4,
            generation_method="spatial_interpolation",
            confidence=0.6,
            source_segments=[200, 201, 202],
        )
        assert len(obs.source_segments) == 3


class TestAnomalyRecord:
    def test_valid(self):
        rec = AnomalyRecord(
            segment_id=100,
            timestamp_utc=datetime.now(timezone.utc),
            anomaly_type="speed_zscore",
            severity="medium",
            details={"z_score": 3.5, "flags": ["speed_zscore"]},
        )
        assert rec.anomaly_id is None
        assert rec.details["z_score"] == 3.5

    def test_defaults(self):
        rec = AnomalyRecord(
            segment_id=100,
            timestamp_utc=datetime.now(timezone.utc),
            anomaly_type="future_timestamp",
            severity="high",
        )
        assert rec.details == {}
