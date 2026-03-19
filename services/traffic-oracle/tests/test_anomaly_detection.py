"""Tests for Tier-2 anomaly detection: FeatureBuilder and MLAnomalyDetector."""

from __future__ import annotations

import math
import time

import numpy as np
import polars as pl
import pytest

from traffic_oracle.anomaly.feature_builder import FeatureBuilder
from traffic_oracle.anomaly.ml_detector import MLAnomalyDetector


# ─── FeatureBuilder Tests ─────────────────────────────────────────────────────


class TestFeatureBuilder:
    def _make_df(self, n: int = 5, base_ts_ms: int | None = None) -> pl.DataFrame:
        base = base_ts_ms or int(time.time() * 1000)
        return pl.DataFrame(
            {
                "timestamp_ms": [base + i * 60_000 for i in range(n)],
                "segment_id": [100 + (i % 3) for i in range(n)],
                "speed_kmh": [40.0 + i * 5.0 for i in range(n)],
                "congestion_index": [0.2 + i * 0.1 for i in range(n)],
            }
        )

    def test_output_shape(self):
        df = self._make_df(10)
        features = FeatureBuilder.build_features(df)
        assert features.shape == (10, 8)
        assert features.dtype == np.float32

    def test_speed_normalized_uses_limit(self):
        df = self._make_df(1)
        speed = df["speed_kmh"][0]
        limits = {100: 80.0}
        features = FeatureBuilder.build_features(df, speed_limits=limits)
        expected = speed / 80.0
        assert abs(features[0, 0] - expected) < 0.001

    def test_speed_normalized_default_limit(self):
        df = self._make_df(1)
        speed = df["speed_kmh"][0]
        features = FeatureBuilder.build_features(df)
        expected = speed / 60.0  # default limit
        assert abs(features[0, 0] - expected) < 0.001

    def test_congestion_passthrough(self):
        df = self._make_df(3)
        features = FeatureBuilder.build_features(df)
        for i in range(3):
            expected = 0.2 + i * 0.1
            assert abs(features[i, 1] - expected) < 0.001

    def test_cyclic_hour_encoding(self):
        # Force a known hour by setting timestamp
        hour = 6
        ts_ms = hour * 3_600_000  # epoch + 6 hours
        df = pl.DataFrame(
            {
                "timestamp_ms": [ts_ms],
                "segment_id": [100],
                "speed_kmh": [50.0],
                "congestion_index": [0.3],
            }
        )
        features = FeatureBuilder.build_features(df)
        expected_sin = math.sin(2 * math.pi * hour / 24)
        expected_cos = math.cos(2 * math.pi * hour / 24)
        assert abs(features[0, 2] - expected_sin) < 0.001
        assert abs(features[0, 3] - expected_cos) < 0.001

    def test_speed_delta_first_is_zero(self):
        df = self._make_df(3)
        features = FeatureBuilder.build_features(df)
        # First reading for each segment should have delta = 0
        assert features[0, 6] == 0.0

    def test_speed_delta_computed(self):
        # Two readings from the same segment
        df = pl.DataFrame(
            {
                "timestamp_ms": [1000, 2000],
                "segment_id": [100, 100],
                "speed_kmh": [50.0, 70.0],
                "congestion_index": [0.3, 0.4],
            }
        )
        features = FeatureBuilder.build_features(df)
        assert features[0, 6] == 0.0  # first reading
        assert abs(features[1, 6] - 20.0) < 0.001  # delta = 70 - 50

    def test_neighbor_speed_ratio_with_data(self):
        df = pl.DataFrame(
            {
                "timestamp_ms": [1000],
                "segment_id": [100],
                "speed_kmh": [60.0],
                "congestion_index": [0.3],
            }
        )
        neighbors = {100: 40.0}
        features = FeatureBuilder.build_features(df, neighbor_speeds=neighbors)
        expected = 60.0 / 40.0
        assert abs(features[0, 7] - expected) < 0.001

    def test_neighbor_speed_ratio_default(self):
        df = pl.DataFrame(
            {
                "timestamp_ms": [1000],
                "segment_id": [100],
                "speed_kmh": [50.0],
                "congestion_index": [0.3],
            }
        )
        features = FeatureBuilder.build_features(df)
        # No neighbor data → ratio = speed/speed = 1.0
        assert abs(features[0, 7] - 1.0) < 0.001

    def test_empty_dataframe(self):
        df = pl.DataFrame(
            {
                "timestamp_ms": pl.Series([], dtype=pl.Int64),
                "segment_id": pl.Series([], dtype=pl.Int64),
                "speed_kmh": pl.Series([], dtype=pl.Float64),
                "congestion_index": pl.Series([], dtype=pl.Float64),
            }
        )
        features = FeatureBuilder.build_features(df)
        assert features.shape == (0, 8)


# ─── MLAnomalyDetector Tests ─────────────────────────────────────────────────


class TestMLAnomalyDetector:
    def test_not_fitted_raises(self):
        detector = MLAnomalyDetector()
        features = np.random.randn(10, 8).astype(np.float32)
        with pytest.raises(RuntimeError, match="not fitted"):
            detector.detect(features)

    def test_is_fitted_property(self):
        detector = MLAnomalyDetector()
        assert not detector.is_fitted
        features = np.random.randn(50, 8).astype(np.float32)
        detector.fit(features)
        assert detector.is_fitted

    def test_fit_and_detect_shapes(self, normal_traffic_features):
        detector = MLAnomalyDetector(contamination=0.05, n_estimators=50)
        detector.fit(normal_traffic_features)

        test_data = np.random.randn(20, 8).astype(np.float32)
        labels, scores = detector.detect(test_data)

        assert labels.shape == (20,)
        assert scores.shape == (20,)
        assert set(np.unique(labels)).issubset({-1, 1})

    def test_detects_obvious_outliers(self, normal_traffic_features):
        detector = MLAnomalyDetector(contamination=0.1, n_estimators=100)
        detector.fit(normal_traffic_features)

        # Create obvious outliers: values far from the training distribution
        outliers = np.full((5, 8), 100.0, dtype=np.float32)
        labels, scores = detector.detect(outliers)

        # At least some should be flagged as anomalies
        n_anomalies = int((labels == -1).sum())
        assert n_anomalies >= 3, f"Expected ≥3 anomalies, got {n_anomalies}"

    def test_normal_data_mostly_clean(self, normal_traffic_features):
        detector = MLAnomalyDetector(contamination=0.05, n_estimators=100)
        detector.fit(normal_traffic_features)

        # Test on data from the same distribution
        rng = np.random.default_rng(99)
        test_normal = np.zeros((50, 8), dtype=np.float32)
        test_normal[:, 0] = rng.normal(0.8, 0.1, 50)
        test_normal[:, 1] = rng.normal(0.3, 0.05, 50)
        for i in range(2, 8):
            test_normal[:, i] = rng.normal(0, 0.5, 50)

        labels, _ = detector.detect(test_normal)
        n_normal = int((labels == 1).sum())
        assert n_normal >= 40, f"Expected ≥40 normal, got {n_normal}"

    def test_scores_lower_for_outliers(self, normal_traffic_features):
        detector = MLAnomalyDetector(contamination=0.05, n_estimators=100)
        detector.fit(normal_traffic_features)

        normal_test = normal_traffic_features[:10]
        outlier_test = np.full((10, 8), 50.0, dtype=np.float32)

        _, normal_scores = detector.detect(normal_test)
        _, outlier_scores = detector.detect(outlier_test)

        assert np.mean(outlier_scores) < np.mean(normal_scores)
