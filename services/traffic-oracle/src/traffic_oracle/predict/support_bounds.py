"""Support bounds — out-of-support detection for forecaster inputs.

Pure stdlib leaf (json/dataclasses/pathlib/typing only): importable in the
gate venv without torch/onnxruntime, and from anywhere in the service.

A SupportBounds records, per model input feature, the inclusive ``[lo, hi]``
interval the model was trained/calibrated on. FlowForecaster checks the live
observation window against these bounds before ONNX inference and abstains
to the heuristic rung (labelled ``out_of_support``) when any recorded feature
falls outside its interval — a model's answer outside its training support
is not trustworthy, so it degrades loudly instead of silently extrapolating.

Semantics:
    - Bounds are INCLUSIVE: ``lo <= observed <= hi`` is in support.
    - NaN (or non-numeric) observed values FAIL CLOSED: out of support.
    - Features with no recorded bound are never gated.
    - :meth:`SupportBounds.absent` is the "no reference recorded" sentinel:
      it gates nothing, so a missing/corrupt bounds file leaves forecaster
      behaviour unchanged.

Schema (``support_bounds.json``)::

    {"schema": 1, "recorded_from": "...", "features": {"name": [lo, hi]},
     "rungs": {"<rung>": {"recorded_from": "...", "features": {...}}}}

Per-rung bounds: the top-level ``features`` describe the DSTGAT rung's
NORMALISED feature space (``speed_norm = speed_kmh / 60``).  A rung with
its own entry under ``rungs`` is gated by THAT entry instead — the t0
foundation rung is a univariate km/h model, so its bounds are recorded in
km/h (``speed_kmh``) with a wide physical envelope: a free-flowing 100
km/h motorway or a 2 km/h gridlock is in support for it (both were gated
out by the DSTGAT feature defaults, exactly when forecasts matter).
:meth:`SupportBounds.for_rung` resolves the bounds a rung uses; a rung
without an entry falls back to the top-level features.

Recording: :func:`record_from_observations` writes empirical min/max bounds
from runtime observations. Natural wiring point once training lands: the
INT8 calibration loop (``predict/nn/quantize.py`` calibrate_and_quantize)
iterates exactly the batches that define the support, and
``predict/training_pipeline.py`` already writes its artifacts next to
``flow_forecaster.onnx`` — write the bounds JSON as a sibling there and
re-record from real calibration instead of trusting the committed
feature-space defaults.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

SCHEMA_VERSION = 1

#: Package-relative default bounds (committed with the code; see loader in
#: flow_forecaster — found under PYTHONPATH=src exactly like the package).
PACKAGE_DEFAULT_PATH = Path(__file__).parent / "data" / "support_bounds.json"

#: Feature names the runtime path feeds the model (flow_forecaster.predict):
#: speed_norm = speed_kmh / 60.0, congestion_idx as-is, neighbor_ratio = 1.0;
#: speed_kmh (raw km/h) is what the foundation rung consumes.
FEATURE_SPEED_NORM = "speed_norm"
FEATURE_SPEED_KMH = "speed_kmh"
FEATURE_CONGESTION_IDX = "congestion_idx"
FEATURE_NEIGHBOR_RATIO = "neighbor_ratio"

#: Rung names with their own bounds entry.
RUNG_FOUNDATION = "foundation"
RUNG_DSTGAT = "dstgat"


@dataclass(frozen=True)
class SupportViolation:
    """One feature value outside its recorded support interval."""

    feature: str
    segment_id: int | str | None
    observed: float  # NaN when the observed value was not a number at all
    lo: float
    hi: float


@dataclass(frozen=True)
class SupportVerdict:
    """Result of checking an observation window against the bounds."""

    in_support: bool
    violations: tuple[SupportViolation, ...] = ()

    @property
    def features(self) -> tuple[str, ...]:
        """Sorted unique names of the violated features."""
        return tuple(sorted({v.feature for v in self.violations}))


def _to_float(value: Any) -> float | None:
    """Coerce to float; None for non-numeric input (bools are not values)."""
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_interval(name: str, interval: Any) -> tuple[float, float]:
    """Validate one ``[lo, hi]`` entry; raises ValueError on anything else."""
    if isinstance(interval, (str, bytes)) or not isinstance(interval, Sequence):
        raise ValueError(
            f"support bounds for {name!r} must be a [lo, hi] pair, got {interval!r}"
        )
    if len(interval) != 2:
        raise ValueError(
            f"support bounds for {name!r} must have exactly two values, "
            f"got {list(interval)!r}"
        )
    lo, hi = _to_float(interval[0]), _to_float(interval[1])
    if lo is None or hi is None:
        raise ValueError(
            f"support bounds for {name!r} must be numeric, got {list(interval)!r}"
        )
    if not (math.isfinite(lo) and math.isfinite(hi)):
        raise ValueError(
            f"support bounds for {name!r} must be finite, got {list(interval)!r}"
        )
    if lo > hi:
        raise ValueError(
            f"support bounds for {name!r} must satisfy lo <= hi, got [{lo}, {hi}]"
        )
    return lo, hi


class SupportBounds:
    """Inclusive per-feature support intervals; gates only what is recorded."""

    def __init__(
        self,
        features: Mapping[str, Any] | None = None,
        *,
        source: str | None = None,
        rungs: Mapping[str, SupportBounds] | None = None,
    ) -> None:
        self._features: dict[str, tuple[float, float]] = {}
        for name, interval in (features or {}).items():
            self._features[str(name)] = _coerce_interval(str(name), interval)
        self._source = source
        self._rungs: dict[str, SupportBounds] = dict(rungs or {})

    @property
    def feature_names(self) -> tuple[str, ...]:
        return tuple(self._features)

    @property
    def source(self) -> str | None:
        return self._source

    @property
    def is_absent(self) -> bool:
        """True when nothing is recorded — :meth:`check` gates nothing."""
        return not self._features and not self._rungs

    @property
    def rung_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._rungs))

    def for_rung(self, rung: str) -> SupportBounds:
        """The bounds ``rung`` is gated by (its own entry, else these)."""
        return self._rungs.get(rung, self)

    # -- constructors --------------------------------------------------------

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SupportBounds":
        """Parse the ``{"schema": 1, "features": {name: [lo, hi]}}`` document."""
        if not isinstance(data, Mapping):
            raise ValueError(
                f"support bounds payload must be a JSON object, "
                f"got {type(data).__name__}"
            )
        schema = data.get("schema")
        if schema != SCHEMA_VERSION:
            raise ValueError(
                f"support bounds schema must be {SCHEMA_VERSION}, got {schema!r}"
            )
        features = data.get("features")
        if not isinstance(features, Mapping):
            raise ValueError(
                "support bounds payload needs a 'features' object mapping "
                "name -> [lo, hi]"
            )
        source = data.get("recorded_from")
        rungs_raw = data.get("rungs", {})
        if not isinstance(rungs_raw, Mapping):
            raise ValueError("support bounds 'rungs' must be an object of rung entries")
        rungs: dict[str, SupportBounds] = {}
        for rung, entry in rungs_raw.items():
            if not isinstance(entry, Mapping) or not isinstance(
                entry.get("features"), Mapping
            ):
                raise ValueError(
                    f"support bounds rung {rung!r} needs a 'features' object"
                )
            rung_source = entry.get("recorded_from")
            rungs[str(rung)] = cls(
                entry["features"],
                source=str(rung_source) if rung_source is not None else None,
            )
        return cls(
            features,
            source=str(source) if source is not None else None,
            rungs=rungs,
        )

    @classmethod
    def from_json(cls, text: str) -> "SupportBounds":
        """Parse a JSON document (json.JSONDecodeError is a ValueError)."""
        return cls.from_dict(json.loads(text))

    @classmethod
    def absent(cls) -> "SupportBounds":
        """The 'no reference recorded' sentinel — never gates."""
        return cls(source="absent")

    @classmethod
    def load(cls, path: str | Path) -> "SupportBounds":
        """Load from a file; a missing file yields the absent sentinel."""
        path = Path(path)
        if not path.exists():
            return cls.absent()
        return cls.from_json(path.read_text(encoding="utf-8"))

    # -- checking --------------------------------------------------------------

    def interval(self, feature: str) -> tuple[float, float]:
        """The recorded ``[lo, hi]`` for a feature (KeyError if unrecorded)."""
        return self._features[feature]

    def check(self, values: Mapping[Any, Mapping[str, Any]]) -> SupportVerdict:
        """Check per-segment feature values; only recorded features gate.

        ``values`` maps segment_id -> {feature_name: observed_value}. Missing
        segments/features are not gated; NaN or non-numeric observations fail
        closed (out of support).
        """
        violations: list[SupportViolation] = []
        for segment_id, features in values.items():
            if not isinstance(features, Mapping):
                continue
            for name, observed in features.items():
                if name not in self._features:
                    continue
                lo, hi = self._features[name]
                value = _to_float(observed)
                if value is None:
                    value = float("nan")  # non-numeric fails closed
                if math.isnan(value) or value < lo or value > hi:
                    violations.append(
                        SupportViolation(
                            feature=str(name),
                            segment_id=segment_id,
                            observed=value,
                            lo=lo,
                            hi=hi,
                        )
                    )
        return SupportVerdict(in_support=not violations, violations=tuple(violations))

    def check_value(
        self,
        feature: str,
        observed: Any,
        segment_id: int | str | None = None,
    ) -> SupportVerdict:
        """Convenience single-value check (inclusive bounds, NaN fails closed)."""
        return self.check({segment_id: {feature: observed}})

    def __repr__(self) -> str:  # pragma: no cover — debug aid
        return (
            f"SupportBounds(features={self._features!r}, source={self._source!r}, "
            f"rungs={sorted(self._rungs)!r})"
        )


def _obs_field(obs: Any, key: str) -> float | None:
    """Read a numeric field from an observation object or mapping."""
    if isinstance(obs, Mapping):
        value = obs.get(key)
    else:
        value = getattr(obs, key, None)
    return _to_float(value)


def record_from_observations(observations: Iterable[Any], path: str | Path) -> Path:
    """Record empirical min/max support bounds from observation-like items.

    Each item is either an object with ``speed_kmh``/``congestion_idx``
    attributes (TrafficObservation) or a mapping with those keys. Writes the
    ``{"schema": 1, ...}`` document consumed by :meth:`SupportBounds.load`,
    with speed recorded on the model's normalized scale (speed_kmh / 60.0).

    Raises ValueError when no usable values are present — recording an empty
    support would silently gate everything.
    """
    speeds: list[float] = []
    congestions: list[float] = []
    for obs in observations:
        speed = _obs_field(obs, "speed_kmh")
        congestion = _obs_field(obs, "congestion_idx")
        if speed is not None:
            speeds.append(speed)
        if congestion is not None:
            congestions.append(congestion)
    if not speeds and not congestions:
        raise ValueError(
            "record_from_observations: no usable speed_kmh/congestion_idx "
            "values to record"
        )

    features: dict[str, list[float]] = {}
    if speeds:
        features[FEATURE_SPEED_NORM] = [
            round(min(speeds) / 60.0, 6),
            round(max(speeds) / 60.0, 6),
        ]
    if congestions:
        features[FEATURE_CONGESTION_IDX] = [
            round(min(congestions), 6),
            round(max(congestions), 6),
        ]

    payload = {
        "schema": SCHEMA_VERSION,
        "recorded_from": "observations",
        "sample_count": max(len(speeds), len(congestions)),
        "features": features,
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path
