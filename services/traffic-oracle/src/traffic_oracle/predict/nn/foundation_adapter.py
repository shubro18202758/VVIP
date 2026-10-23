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
    "BIN_SEC",
    "CONTRACT_HORIZONS",
    "DEFAULT_FREE_FLOW_KMH",
    "FoundationInputError",
    "MAX_LAG_BINS",
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

#: The grid the artifact's horizons assume: one context step = 300 s of
#: OBSERVATION time (HORIZON_TO_STEP maps 5/10/15/30 min onto it).
BIN_SEC = 300

#: A segment whose newest observed bin lags the newest bin of the batch
#: (or the wall-clock bin) by more than this is stale: its forecast would
#: start from old data, so it is dropped from the batch (labelled).
MAX_LAG_BINS = 2

#: Labelled assumption: congestion_idx is DERIVED, not learned — the
#: artifact is univariate speed-only.  Each segment's own free-flow speed
#: (``corridor.road_segments.speed_limit_kmh``, the twin's free flow) is
#: used when the caller supplies it; 60 km/h — the DSTGAT feature
#: convention — only for a segment without one.
DEFAULT_FREE_FLOW_KMH = 60.0

#: Tolerance for the quantile-monotonicity sanity check (fp32 headroom).
_QUANTILE_TOL = 1e-6

#: Observation timestamps remembered per segment for re-read de-duplication
#: (~4 h of a 1 Hz feed; the DB feed upserts whole bins and needs none).
_SEEN_PER_SEGMENT = 16384

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


class _Bin:
    """One 300 s bin of one segment: running mean speed + provenance."""

    __slots__ = ("count", "mean", "real", "synthetic")

    def __init__(self) -> None:
        self.count = 0
        self.mean = 0.0
        self.real = 0
        self.synthetic = 0

    def add(self, speed: float, quality: str) -> None:
        self.count += 1
        self.mean += (speed - self.mean) / self.count
        if quality == "synthetic":
            self.synthetic += 1
        else:
            self.real += 1


class RollingHistoryWindow:
    """Per-segment speed history on the artifact's 5-minute grid.

    Samples are keyed by OBSERVATION time (``timestamp_ms``), averaged into
    300 s bins — never one sample per ``predict()`` call (that fed twelve
    cache reads taken over 35 s to the artifact as an hour of context, so
    its "T+30" was ~9 s ahead).  A new context step appears only when a new
    bin opens; the same cached observation read twice is ignored.  The
    production feed (predict/history_feed.py) upserts whole bins from the
    ingested ``traffic.observations`` aggregates.

    ``history()`` aligns a batch on one grid ending at its newest bin and
    pads missing bins (gaps, ragged starts) with NaN — the artifact's
    native missing marker — instead of failing the batch.  A segment is
    READY with >= ``min_steps`` observed bins in the window and a newest
    bin no more than ``max_lag_bins`` behind; not-ready segments are
    dropped from the batch and labelled (their callers get the next rung).
    ``anomalous`` samples never enter; ``synthetic`` samples do, with their
    provenance kept per bin (:meth:`quality`).
    """

    def __init__(
        self,
        min_steps: int = MIN_CONTEXT_STEPS,
        max_steps: int = DEFAULT_WINDOW_STEPS,
        bin_sec: int = BIN_SEC,
        max_lag_bins: int = MAX_LAG_BINS,
    ) -> None:
        if min_steps > max_steps:
            raise ValueError(
                f"window misconfigured: min_steps {min_steps} > max_steps {max_steps}"
            )
        self._min = min_steps
        self._max = max_steps
        self._bin_ms = int(bin_sec) * 1000
        self._max_lag = int(max_lag_bins)
        self._series: dict[int, dict[int, _Bin]] = {}
        # Timestamps already folded in, per segment (bounded): a re-read of
        # the same cached observation is ignored, a late one still counts.
        self._seen: dict[int, tuple[deque[int], set[int]]] = {}
        #: Bumps on every change — forecast caches key on it.
        self.version = 0

    @property
    def min_steps(self) -> int:
        return self._min

    @property
    def bin_ms(self) -> int:
        return self._bin_ms

    def bin_of(self, timestamp_ms: int) -> int:
        return int(timestamp_ms) // self._bin_ms

    def _prune(self, segment_id: int) -> None:
        bins = self._series.get(segment_id)
        if not bins:
            return
        floor = max(bins) - self._max + 1
        for b in [b for b in bins if b < floor]:
            del bins[b]

    def record(
        self,
        segment_id: int,
        timestamp_ms: int,
        speed_kmh: float,
        data_quality: str = "real",
    ) -> bool:
        """Fold one observation into its 300 s bin.

        Returns True when the sample opened a NEW bin (a new context step).
        Anomalous, non-finite, already-recorded (the same observation
        timestamp) and older-than-the-window samples are refused; a late
        sample for an earlier bin still lands in that bin.
        """
        if data_quality == "anomalous":
            return False
        speed = float(speed_kmh)
        if not np.isfinite(speed) or speed < 0.0:
            return False
        ts = int(timestamp_ms)
        order, seen = self._seen.setdefault(segment_id, (deque(), set()))
        if ts in seen:
            return False
        bins = self._series.setdefault(segment_id, {})
        b = self.bin_of(ts)
        if bins and b <= max(bins) - self._max:
            return False
        seen.add(ts)
        order.append(ts)
        while len(order) > _SEEN_PER_SEGMENT:
            seen.discard(order.popleft())
        opened = b not in bins
        if opened:
            bins[b] = _Bin()
        bins[b].add(speed, data_quality)
        if opened:
            self._prune(segment_id)
        self.version += 1
        return opened

    def set_bin(
        self,
        segment_id: int,
        bin_index: int,
        mean_speed_kmh: float,
        count: int,
        real: int = 0,
        synthetic: int = 0,
    ) -> None:
        """Upsert a whole bin from an authoritative aggregate (the DB feed)."""
        speed = float(mean_speed_kmh)
        if not np.isfinite(speed) or count <= 0:
            return
        bins = self._series.setdefault(segment_id, {})
        entry = _Bin()
        entry.count = int(count)
        entry.mean = speed
        entry.real = int(real)
        entry.synthetic = int(synthetic)
        bins[int(bin_index)] = entry
        self._prune(segment_id)
        self.version += 1

    def segments(self) -> list[int]:
        return sorted(self._series)

    def latest_bin(self, segment_id: int) -> int | None:
        bins = self._series.get(segment_id)
        return max(bins) if bins else None

    def observed_steps(self, segment_id: int, end_bin: int | None = None) -> int:
        bins = self._series.get(segment_id)
        if not bins:
            return 0
        end = max(bins) if end_bin is None else end_bin
        return sum(1 for b in bins if end - self._max < b <= end)

    def ready(self, segment_id: int, now_ms: int | None = None) -> bool:
        latest = self.latest_bin(segment_id)
        if latest is None:
            return False
        if now_ms is not None and self.bin_of(now_ms) - latest > self._max_lag:
            return False
        return self.observed_steps(segment_id, latest) >= self._min

    def quality(self, segment_ids: Sequence[int]) -> dict[int, str]:
        """Per segment: 'real' | 'synthetic' | 'mixed' over its window."""
        out: dict[int, str] = {}
        for sid in segment_ids:
            bins = self._series.get(sid)
            if not bins:
                continue
            real = sum(b.real for b in bins.values())
            synth = sum(b.synthetic for b in bins.values())
            out[sid] = (
                "mixed" if real and synth else "synthetic" if synth else "real"
            )
        return out

    def history(
        self, segment_ids: Sequence[int], now_ms: int | None = None
    ) -> dict[int, np.ndarray]:
        """Aligned, NaN-padded history for the ready segments of a batch.

        Every returned series spans the SAME bins, ending at the newest
        bin among the batch's fresh segments, so one decode step means the
        same 5 minutes for every row.  Loud failures (whole-rung degrade),
        never silent zero-padding:

        - no requested segment has any recorded bin → ``history_empty``;
        - none is ready (warm-up or stale) → ``history_too_short``.

        Segments that are not ready are dropped from the batch and logged.
        """
        recorded = [sid for sid in segment_ids if self._series.get(sid)]
        if not recorded:
            raise FoundationInputError("history_empty: no recorded history")
        fresh_latest = [
            self.latest_bin(sid)
            for sid in recorded
            if now_ms is None or self.bin_of(now_ms) - self.latest_bin(sid) <= self._max_lag
        ]
        if not fresh_latest:
            raise FoundationInputError(
                f"history_too_short: 0/{len(segment_ids)} segments fresh "
                f"(newest bin older than {self._max_lag} x {self._bin_ms // 1000}s)"
            )
        end = max(fresh_latest)
        ready = [
            sid for sid in recorded
            if end - self.latest_bin(sid) <= self._max_lag
            and self.observed_steps(sid, end) >= self._min
        ]
        not_ready = [sid for sid in segment_ids if sid not in ready]
        if not ready:
            first = recorded[0]
            raise FoundationInputError(
                f"history_too_short: 0/{len(segment_ids)} segments have "
                f"{self._min} observed {self._bin_ms // 60000}-min steps "
                f"(first: {first} with {self.observed_steps(first, end)})"
            )
        start = min(
            max(min(b for b in self._series[sid] if b > end - self._max), end - self._max + 1)
            for sid in ready
        )
        start = min(start, end - self._min + 1)
        length = end - start + 1
        history: dict[int, np.ndarray] = {}
        for sid in ready:
            bins = self._series[sid]
            arr = np.full(length, np.nan, dtype=np.float32)
            for b, entry in bins.items():
                if start <= b <= end:
                    arr[b - start] = entry.mean
            history[sid] = arr
        if not_ready:
            logger.info(
                "foundation_adapter.history_segments_not_ready",
                segments=not_ready,
                kept=len(history),
                end_bin=end,
            )
        return history

    def health(self, now_ms: int | None = None) -> dict:
        segments = self.segments()
        latest = [self.latest_bin(s) for s in segments]
        newest = max((b for b in latest if b is not None), default=None)
        return {
            "segments": len(segments),
            "segments_ready": sum(1 for s in segments if self.ready(s, now_ms)),
            "min_steps": self._min,
            "max_steps": self._max,
            "bin_sec": self._bin_ms // 1000,
            # seconds since the newest bin OPENED (0..300 while it is the
            # current, still-filling bin; larger when the feed went quiet)
            "newest_bin_opened_sec_ago": (
                round((now_ms - newest * self._bin_ms) / 1000.0, 1)
                if newest is not None and now_ms is not None
                else None
            ),
            "version": self.version,
        }


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

    def _free_flow_for(
        self, segment_id: int, free_flow: Mapping[int, float] | None
    ) -> float:
        if free_flow:
            value = free_flow.get(segment_id)
            if value is not None and np.isfinite(value) and value > 0:
                return float(value)
        return self._free_flow

    def reshape(
        self,
        quantiles: np.ndarray,
        segment_ids: Sequence[int],
        free_flow: Mapping[int, float] | None = None,
    ) -> dict[int, dict[int, dict[str, object]]]:
        """Map the artifact's quantile tensor onto the forecast rows.

        Row shape per horizon: point fields from the p50 column, plus the
        contract ``quantiles`` object built from p10/p50/p90 (congestion
        bands derived through the decreasing transform against the
        segment's OWN free flow when ``free_flow`` names it). Horizon
        mapping: decode step ``HORIZON_TO_STEP[h]`` (5-min grid), i.e.
        columns 1/2/3/6 of a 32-step decode — no interpolation needed.
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
            free_flow_kmh = self._free_flow_for(sid, free_flow)
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
                        speed, free_flow_kmh
                    ),
                    "quantiles": make_quantiles(
                        p10, p50, p90, free_flow_kmh
                    ),
                }
        return payload

    # ── composition ───────────────────────────────────────────────────────

    def forecast(
        self,
        history: Mapping[int, np.ndarray],
        free_flow: Mapping[int, float] | None = None,
    ) -> dict[int, dict[int, dict[str, object]]]:
        """End-to-end: history -> graph inputs -> runner -> forecast rows.

        ``free_flow`` optionally maps segment_id -> free-flow km/h (the
        segment's speed limit) for the derived congestion fields.
        Raises :class:`FoundationInputError` on any contract failure — the
        caller degrades to the next rung with a labelled reason; this
        method never returns a silently-empty mapping.
        """
        segment_ids = list(history.keys())
        feeds = self.build_inputs(history)
        quantiles = self._runner(feeds)
        payload = self.reshape(quantiles, segment_ids, free_flow)
        logger.info(
            "foundation_adapter.forecast",
            segments=len(segment_ids),
            horizons=list(self._horizons),
            compute_horizon=self._compute_horizon,
        )
        return payload
