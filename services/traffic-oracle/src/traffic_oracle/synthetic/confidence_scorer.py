"""Confidence scoring for synthetic observations by generation method."""

from __future__ import annotations

# Confidence scores by generation method — higher = more trustworthy
METHOD_CONFIDENCE: dict[str, float] = {
    "historical_match": 0.8,
    "spatial_interpolation": 0.6,
    "temporal_pattern": 0.5,
    "default_fallback": 0.2,
}


def score_confidence(method: str) -> float:
    """Return confidence score for a synthetic data generation method."""
    return METHOD_CONFIDENCE.get(method, 0.1)
