"""Foundation adapter — t0-alpha ONNX INT8 (Apache-2.0), wave-2 wiring.

Reshapes a zero-shot probabilistic time-series foundation artifact onto the
FlowForecaster contract. Point fields stay exactly
``{"speed_kmh", "congestion_idx"}``; the optional ``quantiles`` object
rides the row per the wave-2 forecast quantile contract
(predict/forecast_quantiles.py)::

    {segment_id: {horizon_min: {
        "speed_kmh": float,             # p50 (unit pass-through: km/h in,
        "congestion_idx": float,        #  derived) — km/h out
        "quantiles": {                  #  optional; ALWAYS attached by this
            "speed_kmh": {p10, p50, p90},       # producer when bands are valid
            "congestion_idx": {p10, p50, p90},
        },
    }}}

Artifact (VVIP/models-local/t0/, see its NOTICE):
    theforecastingcompany/t0-alpha-onnx-int8 — 107,151,882 bytes, Apache-2.0,
    sha256 82da4392…ed98. ONNX opset 20, int8 weights / fp32 compute.
    Graph contract (from its manifest.json): inputs
    ``target_context`` fp32 [rows, context] (NaN = missing),
    ``target_group_ids`` int32 [rows], zero-row future-covariate tensors;
    output ``quantiles`` fp32 [rows, compute_horizon, 5] at levels
    0.1 / 0.25 / 0.5 / 0.75 / 0.9; compute_horizon multiple of 32.

Measured on this aarch64 host (2026-10-18, VVIP/scripts/probe_foundation_t0.py
under VVIP/.venvs/wave2): 200 segments x 96-step context -> 485 ms bare
session.run / ~0.6–1.1 s full adapter path, quantile-monotone, bitwise
deterministic — and ~1.4–1.5 GiB process RSS at session LOAD (ORT
materializes the int8 weights as fp32), i.e. 3.4–3.7x the 0.4 GiB
traffic-oracle row-5 budget in VVIP/models/MEMORY_BUDGET.md. Hosting
decision (row renegotiation / sidecar process / smaller artifact) is open;
this module only shapes data.

This module is torch/onnxruntime-free at module scope (mirrors
predict/nn/config.py) so it loads in the pure-python gate venv; the graph
runner is duck-typed and injected — a stub in tests, an ORT session via
:func:`session_runner` in production.

Adapter-level contract failures raise :class:`FoundationInputError` whose
message starts with a machine-readable reason token (``history_too_short``,
``quantiles_crossing``, …) — FlowForecaster maps them to the
``foundation_input:<reason>`` degradation label. Nothing here zero-pads a
missing history window or swallows a missing output.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping, Sequence

import numpy as np
import structlog

from traffic_oracle.predict.forecast_quantiles import (
    FoundationInputError,  # re-exported — the rung-boundary error class
    derive_congestion_idx,  # re-exported for adapter-era callers
    make_quantiles,
)

__all__ = [
    "CONTRACT_HORIZONS",
    "DEFAULT_FREE_FLOW_KMH",
    "FoundationInputError",
    "MIN_CONTEXT_STEPS",
    "RollingHistoryWindow",
    "T0FoundationAdapter",
    "compute_horizon_for",
    "derive_congestion_idx",
    "session_runner",
]

logger = structlog.get_logger(__name__)

#: The artifact's native quantile levels, in output-axis order (manifest.json).
T0_QUANTILE_LEVELS: tuple[float, ...] = (0.1, 0.25, 0.5, 0.75, 0.9)
#: Output-axis indices for the three levels this adapter consumes.
IDX_P10, IDX_P50, IDX_P90 = 0, 2, 4

#: Frozen contract horizons (minutes) and their forecast steps on the
#: 5-minute grid the deployment samples at. Reshape NEVER emits a horizon
#: outside this mapping.
HORIZON_TO_STEP: Mapping[int, int] = {5: 1, 10: 2, 15: 3, 30: 6}
CONTRACT_HORIZONS: tuple[int, ...] = (5, 10, 15, 30)

#: The graph decodes horizons in multiples of 32 steps (manifest.json:
#: prediction_length.compute_multiple == 32); 32 covers every contract step.
COMPUTE_HORIZON_MULTIPLE = 32

#: Acceptance floor: a foundation forecaster needs real history — at least
#: 12 steps of context at 5-minute sampling. Shorter history is a loud
#: contract failure, never a silent zero-pad.
MIN_CONTEXT_STEPS = 12

#: Rolling window depth the builder keeps per segment (8 h at 5-min steps).
DEFAULT_WINDOW_STEPS = 96

#: Labelled assumption: congestion_idx is DERIVED, not learned — the
#: artifact is univariate speed-only. 60 km/h mirrors the
#: speed_norm = speed_kmh / 60.0 convention baked into the DSTGAT runtime
#: feature build; the deployment stage may override per corridor.
DEFAULT_FREE_FLOW_KMH = 60.0

#: Tolerance for the quantile-monotonicity sanity check (fp32 headroom).
_QUANTILE_TOL = 1e-6

GraphRunner = Callable[[dict[str, np.ndarray]], np.ndarray]

# FoundationInputError now lives in predict/forecast_quantiles.py (the
# dependency-free contract module) so the producer helpers there raise the
# SAME class the forecaster's rung boundary catches — it is re-exported
# here for every existing importer of this module.


def compute_horizon_for(horizons: Sequence[int]) -> int:
    """Smallest multiple of 32 covering every requested horizon's step."""
    steps = [HORIZON_TO_STEP[h] for h in horizons]
    return ((max(steps) + COMPUTE_HORIZON_MULTIPLE - 1)
            // COMPUTE_HORIZON_MULTIPLE) * COMPUTE_HORIZON_MULTIPLE


def session_runner(session: object) -> GraphRunner:
    """Wrap a duck-typed ORT InferenceSession as a GraphRunner.

    Kept free of onnxruntime imports: the session only needs
    ``run(names_or_none, feeds) -> list[array]``. The first output tensor
    is the quantiles array (manifest: single output named ``quantiles``).
    """
    def run(feeds: dict[str, np.ndarray]) -> np.ndarray:
        outputs = session.run(None, feeds)  # type: ignore[attr-defined]
        if not outputs:
            raise FoundationInputError("graph_no_output")
        return outputs[0]

    return run


class RollingHistoryWindow:
    """Per-segment speed history the foundation rung feeds the artifact.

    The plan puts the window builder in the adapter: the runtime cache only
    serves the latest observation (``get_latest``), so the window grows one
    sample per predict() call — real recorded history, never zero-padded.
    A segment becomes *ready* once it holds >= MIN_CONTEXT_STEPS samples.
    """

    def __init__(
        self,
        min_steps: int = MIN_CONTEXT_STEPS,
        max_steps: int = DEFAULT_WINDOW_STEPS,
    ) -> None:
        if min_steps > max_steps:
            raise ValueError(
                f"window misconfigured: min_steps {min_steps} > max_steps {max_steps}"
            )
        self._min = min_steps
        self._max = max_steps
        self._series: dict[int, deque[float]] = {}

    @property
    def min_steps(self) -> int:
        return self._min

    def push(self, segment_id: int, speed_kmh: float) -> None:
        """Record the latest observed speed for a segment (5-min grid)."""
        series = self._series.setdefault(
            segment_id, deque(maxlen=self._max)
        )
        series.append(float(speed_kmh))

    def ready(self, segment_id: int) -> bool:
        series = self._series.get(segment_id)
        return series is not None and len(series) >= self._min

    def history(
        self, segment_ids: Sequence[int]
    ) -> dict[int, np.ndarray]:
        """Build the per-segment history mapping for the adapter batch.

        Loud failures (whole-rung degrade), never silent zero-padding:

        - any segment with 1..min_steps-1 recorded samples raises
          ``history_too_short`` (warm-up phase);
        - segments with NO recorded sample at all are dropped from the
          batch and logged — they would be zero-padded otherwise, and the
          heuristic rung produces no rows for them either;
        - no segment with any history at all raises ``history_empty``.
        """
        never_seen = [sid for sid in segment_ids if sid not in self._series]
        short = [
            sid for sid in segment_ids
            if sid in self._series and not self.ready(sid)
        ]
        if short:
            first = short[0]
            raise FoundationInputError(
                f"history_too_short: {len(short)}/{len(segment_ids)} segments "
                f"below {self._min} steps (first: {first} with "
                f"{len(self._series[first])})"
            )
        history = {
            sid: np.asarray(self._series[sid], dtype=np.float32)
            for sid in segment_ids
            if sid in self._series
        }
        if not history:
            raise FoundationInputError("history_empty: no recorded history")
        if never_seen:
            logger.info(
                "foundation_adapter.history_unobserved_dropped",
                segments=never_seen,
                kept=len(history),
            )
        return history


class T0FoundationAdapter:
    """Reshape t0-alpha quantile forecasts onto the forecaster contract.

    The runner is injected (stub in gate-venv tests; an onnxruntime session
    via :func:`session_runner` in deployment) so this class itself never
    imports the heavy stack. Rows carry the optional ``quantiles`` object
    per predict/forecast_quantiles.py — this producer always attaches it
    when the artifact's bands are monotone (crossing bands are a LOUD
    failure, never silently dropped).
    """

    def __init__(
        self,
        runner: GraphRunner,
        free_flow_kmh: float = DEFAULT_FREE_FLOW_KMH,
        horizons: Sequence[int] = CONTRACT_HORIZONS,
    ) -> None:
        unknown = [h for h in horizons if h not in HORIZON_TO_STEP]
        if unknown:
            raise FoundationInputError(
                f"horizon_off_contract: {unknown} not in {sorted(HORIZON_TO_STEP)}"
            )
        if free_flow_kmh <= 0:
            raise FoundationInputError(
                f"free_flow_not_positive: {free_flow_kmh!r}"
            )
        self._runner = runner
        self._free_flow = float(free_flow_kmh)
        self._horizons = tuple(horizons)
        self._compute_horizon = compute_horizon_for(self._horizons)

    # ── input side ────────────────────────────────────────────────────────

    def build_inputs(
        self, history: Mapping[int, np.ndarray]
    ) -> dict[str, np.ndarray]:
        """Build the t0 graph input dict from per-segment speed history.

        ``history`` maps segment_id -> 1-D array of speed_kmh samples,
        oldest first, sampled at the 5-minute grid. NaN samples inside a
        series are the artifact's native missing marker and pass through;
        a segment with no usable history fails loudly — this adapter never
        zero-pads a missing window.

        Each segment becomes its own attention group (independent series):
        cross-segment joint attention is an opt-in the deployment stage can
        add later via shared group ids.
        """
        if not history:
            raise FoundationInputError("history_empty: no segments provided")

        rows: list[np.ndarray] = []
        for sid, series in history.items():
            arr = np.asarray(series, dtype=np.float32)
            if arr.ndim != 1:
                raise FoundationInputError(
                    f"history_not_1d: segment {sid} has ndim={arr.ndim}"
                )
            if arr.shape[0] < MIN_CONTEXT_STEPS:
                raise FoundationInputError(
                    f"history_too_short: segment {sid} has {arr.shape[0]} "
                    f"steps, need >= {MIN_CONTEXT_STEPS}"
                )
            if not np.isfinite(arr).any():
                raise FoundationInputError(
                    f"history_all_missing: segment {sid} is entirely NaN"
                )
            rows.append(arr)

        context_lengths = {r.shape[0] for r in rows}
        if len(context_lengths) != 1:
            raise FoundationInputError(
                "history_ragged: segments differ in context length "
                f"{sorted(context_lengths)} — align the window first"
            )
        context = context_lengths.pop()
        target_context = np.stack(rows, axis=0)  # [rows, context]
        return {
            "target_context": target_context,
            "target_group_ids": np.arange(
                target_context.shape[0], dtype=np.int32
            ),
            "future_covariate_context": np.empty(
                (0, context), dtype=np.float32
            ),
            "future_covariate_future": np.empty(
                (0, self._compute_horizon), dtype=np.float32
            ),
            "future_covariate_group_ids": np.empty((0,), dtype=np.int32),
        }

    # ── output side ───────────────────────────────────────────────────────

    def reshape(
        self,
        quantiles: np.ndarray,
        segment_ids: Sequence[int],
    ) -> dict[int, dict[int, dict[str, object]]]:
        """Map the artifact's quantile tensor onto the forecast rows.

        Row shape per horizon: point fields from the p50 column, plus the
        contract ``quantiles`` object built from p10/p50/p90 (congestion
        bands derived through the decreasing transform). Horizon mapping:
        decode step ``HORIZON_TO_STEP[h]`` (5-min grid), i.e. columns
        1/2/3/6 of a 32-step decode — no interpolation needed.
        """
        q = np.asarray(quantiles, dtype=np.float32)
        if q.ndim != 3:
            raise FoundationInputError(
                f"quantiles_wrong_rank: ndim={q.ndim}, expected 3"
            )
        if q.shape[0] != len(segment_ids):
            raise FoundationInputError(
                f"quantiles_row_mismatch: {q.shape[0]} rows for "
                f"{len(segment_ids)} segments"
            )
        if q.shape[2] != len(T0_QUANTILE_LEVELS):
            raise FoundationInputError(
                f"quantiles_level_mismatch: last dim {q.shape[2]}, "
                f"expected {len(T0_QUANTILE_LEVELS)}"
            )
        if q.shape[1] < max(HORIZON_TO_STEP[h] for h in self._horizons):
            raise FoundationInputError(
                f"quantiles_horizon_short: decoded {q.shape[1]} steps, "
                f"contract needs {max(HORIZON_TO_STEP[h] for h in self._horizons)}"
            )

        payload: dict[int, dict[int, dict[str, object]]] = {}
        for i, sid in enumerate(segment_ids):
            payload[sid] = {}
            for h in self._horizons:
                col = HORIZON_TO_STEP[h] - 1
                # Finiteness FIRST, on the RAW tensor values: NaN passes a
                # comparison-based crossing check vacuously (NaN > tol is
                # False) AND survives max(nan, 0.0) — the reviewer-repro'd
                # escape route. A non-finite band is a loud contract
                # failure, never a NaN forecast row.
                raw_p10 = float(q[i, col, IDX_P10])
                raw_p50 = float(q[i, col, IDX_P50])
                raw_p90 = float(q[i, col, IDX_P90])
                if not (
                    np.isfinite(raw_p10)
                    and np.isfinite(raw_p50)
                    and np.isfinite(raw_p90)
                ):
                    raise FoundationInputError(
                        f"quantiles_not_finite: segment {sid} horizon {h} "
                        f"p10={raw_p10} p50={raw_p50} p90={raw_p90}"
                    )
                p10 = max(raw_p10, 0.0)
                p50 = max(raw_p50, 0.0)
                p90 = max(raw_p90, 0.0)
                if p10 - p50 > _QUANTILE_TOL or p50 - p90 > _QUANTILE_TOL:
                    raise FoundationInputError(
                        f"quantiles_crossing: segment {sid} horizon {h} "
                        f"p10={p10:.4f} p50={p50:.4f} p90={p90:.4f}"
                    )
                speed = p50  # km/h; the artifact is unit-pass-through
                payload[sid][h] = {
                    "speed_kmh": speed,
                    "congestion_idx": derive_congestion_idx(
                        speed, self._free_flow
                    ),
                    "quantiles": make_quantiles(
                        p10, p50, p90, self._free_flow
                    ),
                }
        return payload

    # ── composition ───────────────────────────────────────────────────────

    def forecast(
        self, history: Mapping[int, np.ndarray]
    ) -> dict[int, dict[int, dict[str, object]]]:
        """End-to-end: history -> graph inputs -> runner -> forecast rows.

        Raises :class:`FoundationInputError` on any contract failure — the
        caller degrades to the next rung with a labelled reason; this
        method never returns a silently-empty mapping.
        """
        segment_ids = list(history.keys())
        feeds = self.build_inputs(history)
        quantiles = self._runner(feeds)
        payload = self.reshape(quantiles, segment_ids)
        logger.info(
            "foundation_adapter.forecast",
            segments=len(segment_ids),
            horizons=list(self._horizons),
            compute_horizon=self._compute_horizon,
        )
        return payload
