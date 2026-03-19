"""ETA Predictor — Gradient-boosted corridor travel time estimation.

Uses sklearn HistGradientBoostingRegressor (~2MB model) to predict
end-to-end travel time for a given route based on segment-level
predictions from DSTGAT and route topology features.

10-feature input:
    0: route_length_m
    1: num_segments
    2: avg_predicted_speed
    3: avg_predicted_congestion
    4: hour_sin
    5: hour_cos
    6: dow_sin
    7: dow_cos
    8: num_signals
    9: weighted_road_class_score
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import structlog
from sklearn.ensemble import HistGradientBoostingRegressor

from traffic_oracle.runtime.memory_profiler import profile_memory

logger = structlog.get_logger(__name__)


class ETAPredictor:
    """Corridor ETA prediction using gradient-boosted trees."""

    def __init__(self) -> None:
        self._model = HistGradientBoostingRegressor(
            max_iter=200,
            max_depth=6,
            learning_rate=0.05,
            min_samples_leaf=10,
            random_state=42,
        )
        self._is_fitted = False

    @profile_memory
    def train(self, features: np.ndarray, eta_seconds: np.ndarray) -> dict:
        """Fit the ETA model on historical route features.

        Args:
            features: (N, 10) feature matrix
            eta_seconds: (N,) ground-truth travel times in seconds

        Returns:
            Dict with training metrics
        """
        self._model.fit(features, eta_seconds)
        self._is_fitted = True

        train_preds = self._model.predict(features)
        mae = float(np.mean(np.abs(train_preds - eta_seconds)))
        rmse = float(np.sqrt(np.mean((train_preds - eta_seconds) ** 2)))

        logger.info(
            "eta_predictor.trained",
            samples=len(eta_seconds),
            train_mae_sec=round(mae, 1),
            train_rmse_sec=round(rmse, 1),
        )
        return {"mae_sec": mae, "rmse_sec": rmse, "samples": len(eta_seconds)}

    @profile_memory
    def predict(self, features: np.ndarray) -> np.ndarray:
        """Predict ETA for route feature vectors.

        Args:
            features: (N, 10) feature matrix

        Returns:
            (N,) predicted travel times in seconds
        """
        if not self._is_fitted:
            raise RuntimeError("ETAPredictor not fitted — call train() first")
        return self._model.predict(features)

    @staticmethod
    def build_features(
        route_length_m: float,
        num_segments: int,
        avg_speed: float,
        avg_congestion: float,
        hour: int,
        dow: int,
        num_signals: int,
        road_class_score: float,
    ) -> np.ndarray:
        """Build a single feature vector for ETA prediction.

        Args:
            route_length_m: Total route length in meters
            num_segments: Number of segments in route
            avg_speed: Average predicted speed across segments (km/h)
            avg_congestion: Average predicted congestion index
            hour: Hour of day (0-23)
            dow: Day of week (0=Monday)
            num_signals: Number of signalized intersections on route
            road_class_score: Weighted road class score (motorway=100..residential=10)

        Returns:
            (1, 10) feature array
        """
        return np.array(
            [[
                route_length_m,
                num_segments,
                avg_speed,
                avg_congestion,
                math.sin(2 * math.pi * hour / 24),
                math.cos(2 * math.pi * hour / 24),
                math.sin(2 * math.pi * dow / 7),
                math.cos(2 * math.pi * dow / 7),
                num_signals,
                road_class_score,
            ]],
            dtype=np.float32,
        )

    def save(self, path: str | Path) -> None:
        """Persist model to disk via joblib."""
        import joblib

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(self._model, path)
        logger.info("eta_predictor.saved", path=str(path))

    def load(self, path: str | Path) -> None:
        """Load model from disk."""
        import joblib

        self._model = joblib.load(path)
        self._is_fitted = True
        logger.info("eta_predictor.loaded", path=str(path))
