"""Tests for synthetic data generation: confidence scorer, gap detector,
spatial interpolator, and generator cascade."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from traffic_oracle.data.models import (
    DataQuality,
    FeedSource,
    TrafficObservation,
)
from traffic_oracle.synthetic.confidence_scorer import (
    METHOD_CONFIDENCE,
    score_confidence,
)
from traffic_oracle.synthetic.gap_detector import GapDetector
from traffic_oracle.synthetic.spatial_interpolator import SpatialInterpolator

from conftest import MockValkey, make_observation


# ─── ConfidenceScorer Tests ───────────────────────────────────────────────────


class TestConfidenceScorer:
    def test_historical_match_score(self):
        assert score_confidence("historical_match") == 0.8

    def test_spatial_interpolation_score(self):
        assert score_confidence("spatial_interpolation") == 0.6

    def test_temporal_pattern_score(self):
        assert score_confidence("temporal_pattern") == 0.5

    def test_default_fallback_score(self):
        assert score_confidence("default_fallback") == 0.2

    def test_unknown_method_returns_low(self):
        assert score_confidence("completely_unknown") == 0.1

    def test_all_defined_methods(self):
        for method, expected in METHOD_CONFIDENCE.items():
            assert score_confidence(method) == expected


# ─── GapDetector Tests ────────────────────────────────────────────────────────


class TestGapDetector:
    @pytest.fixture
    def detector(self, mock_valkey: MockValkey) -> GapDetector:
        return GapDetector(mock_valkey)

    async def test_all_segments_stale(self, detector: GapDetector):
        # No keys set in mock valkey → all segments are stale
        stale = await detector.find_stale_segments([100, 101, 102])
        assert stale == [100, 101, 102]

    async def test_no_segments_stale(self, detector: GapDetector, mock_valkey: MockValkey):
        # Pre-populate all keys
        for seg_id in [100, 101, 102]:
            mock_valkey.seed(f"traffic:latest:{seg_id}", '{"data": "ok"}', ttl=300)
        stale = await detector.find_stale_segments([100, 101, 102])
        assert stale == []

    async def test_partial_staleness(self, detector: GapDetector, mock_valkey: MockValkey):
        mock_valkey.seed("traffic:latest:100", '{"data": "ok"}', ttl=300)
        # 101 and 102 are missing
        stale = await detector.find_stale_segments([100, 101, 102])
        assert 100 not in stale
        assert 101 in stale
        assert 102 in stale

    async def test_empty_segment_list(self, detector: GapDetector):
        stale = await detector.find_stale_segments([])
        assert stale == []


# ─── SpatialInterpolator Tests ────────────────────────────────────────────────


class TestSpatialInterpolator:
    @pytest.fixture
    def interpolator(self, mock_valkey: MockValkey) -> SpatialInterpolator:
        return SpatialInterpolator(mock_valkey)

    def _cache_observation(
        self, mock_valkey: MockValkey, segment_id: int, speed: float, congestion: float
    ) -> None:
        obs = make_observation(segment_id=segment_id, speed_kmh=speed, congestion_idx=congestion)
        mock_valkey.seed(f"traffic:latest:{segment_id}", obs.model_dump_json())

    async def test_interpolate_single_neighbor(
        self, interpolator: SpatialInterpolator, mock_valkey: MockValkey
    ):
        self._cache_observation(mock_valkey, 200, speed=60.0, congestion=0.4)
        result = await interpolator.interpolate(100, [200])
        assert result is not None
        assert abs(result["speed_kmh"] - 60.0) < 0.01
        assert abs(result["congestion_idx"] - 0.4) < 0.01

    async def test_interpolate_multiple_neighbors_idw(
        self, interpolator: SpatialInterpolator, mock_valkey: MockValkey
    ):
        # Neighbor 200 at hop 1 (weight 1.0), neighbor 201 at hop 2 (weight 0.5)
        self._cache_observation(mock_valkey, 200, speed=60.0, congestion=0.4)
        self._cache_observation(mock_valkey, 201, speed=30.0, congestion=0.8)

        result = await interpolator.interpolate(100, [200, 201])
        assert result is not None

        # IDW: (60*1.0 + 30*0.5) / (1.0 + 0.5) = 75/1.5 = 50.0
        assert abs(result["speed_kmh"] - 50.0) < 0.01
        # IDW: (0.4*1.0 + 0.8*0.5) / (1.0 + 0.5) = 0.8/1.5 ≈ 0.533
        assert abs(result["congestion_idx"] - 0.5333) < 0.01

    async def test_interpolate_no_neighbor_data(
        self, interpolator: SpatialInterpolator
    ):
        result = await interpolator.interpolate(100, [200, 201])
        assert result is None

    async def test_interpolate_empty_neighbors(
        self, interpolator: SpatialInterpolator
    ):
        result = await interpolator.interpolate(100, [])
        assert result is None

    async def test_interpolate_partial_neighbor_data(
        self, interpolator: SpatialInterpolator, mock_valkey: MockValkey
    ):
        # Only neighbor 201 has data (at index 1, weight = 0.5)
        self._cache_observation(mock_valkey, 201, speed=80.0, congestion=0.2)

        result = await interpolator.interpolate(100, [200, 201])
        assert result is not None
        # Only one neighbor contributes: 80.0 * 0.5 / 0.5 = 80.0
        assert abs(result["speed_kmh"] - 80.0) < 0.01
