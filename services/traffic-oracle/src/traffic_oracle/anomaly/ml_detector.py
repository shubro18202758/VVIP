"""Tier-2 Isolation Forest anomaly detector."""

from __future__ import annotations

import numpy as np
import structlog
from sklearn.ensemble import IsolationForest

logger = structlog.get_logger()


class MLAnomalyDetector:
    """Batch anomaly detection using scikit-learn Isolation Forest.

    Contamination = 0.05 (expect ~5% anomaly rate in traffic data).
    """

    def __init__(
        self,
        contamination: float = 0.05,
        n_estimators: int = 100,
        n_jobs: int = -1,
    ) -> None:
        self._model = IsolationForest(
            contamination=contamination,
            n_estimators=n_estimators,
            n_jobs=n_jobs,
            random_state=42,
        )
        self._is_fitted = False

    def fit(self, features: np.ndarray) -> None:
        """Train on historical normal traffic patterns.

        Args:
            features: (N, 8) feature matrix from FeatureBuilder
        """
        self._model.fit(features)
        self._is_fitted = True
        logger.info("isolation forest fitted", samples=features.shape[0])

    def detect(self, features: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Detect anomalies in a batch.

        Returns:
            labels: -1 for anomalies, 1 for normal (N,)
            scores: anomaly scores — lower is more anomalous (N,)
        """
        if not self._is_fitted:
            raise RuntimeError("Model not fitted — call fit() first")

        labels = self._model.predict(features)
        scores = self._model.decision_function(features)
        n_anomalies = int((labels == -1).sum())
        logger.debug("batch detection complete", total=len(labels), anomalies=n_anomalies)
        return labels, scores

    @property
    def is_fitted(self) -> bool:
        return self._is_fitted
