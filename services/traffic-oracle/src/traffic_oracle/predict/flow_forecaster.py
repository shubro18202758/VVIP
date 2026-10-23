"""Flow Forecaster — Spatio-temporal traffic flow prediction model.

Uses a lightweight DSTGAT (Dynamic Spatio-Temporal Graph Attention Network)
operating on the corridor road graph to predict traffic speed/congestion
5-30 minutes into the future. ONNX-exported INT8 model (~82KB) runs via
OnnxServe within the strict VRAM budget.

Architecture:
    - Input: Historical traffic observations (speed, congestion) on road segments
    - Graph: Road network topology from PostGIS corridor.segment_adjacency
    - Output: Per-segment speed and congestion forecast at T+5, T+10, T+15, T+30 min

VRAM Policy:
    - Training: Offload to CPU if LLM is active; use gpu_arbiter to acquire GPU lease.
    - Inference: Export to ONNX → run via ONNX Runtime CUDA EP (~300MB VRAM)
    - Fallback: ONNX Runtime CPU EP if GPU memory pressure detected

Out-of-support hatch (PER RUNG):
    predict() checks each segment's latest usable observation against the
    support bounds of the rung about to answer it (predict/data/
    support_bounds.json by default, override via FORECAST_SUPPORT_BOUNDS_PATH)
    BEFORE any model inference.  The foundation rung is gated by its own
    km/h envelope (``rungs.foundation``: a 100 km/h motorway or a 2 km/h
    gridlock is in its support), the DSTGAT rung by its normalised
    feature-space defaults.  Each segment outside the bounds of the rung
    that would answer it abstains to a heuristic (cache-fallback) row with a
    ``flow_forecaster.out_of_support`` warning, ``last_degradation_reason =
    "out_of_support:<features>"`` and ``last_out_of_support_segments`` —
    never an unlabelled model answer off distribution.  In-support segments
    of the same batch still get the model rung.  A missing/corrupt bounds
    file disables the gate loudly and leaves behaviour unchanged.

    The normalised speed the DSTGAT rung sees — and is gated on — is the
    segment's RELATIVE speed, ``speed_kmh / free_flow`` (its speed limit,
    :meth:`set_free_flow`), so a free-flowing 100 km/h motorway reads 1.0
    instead of falling out of a 60 km/h-normalised support; a segment with
    no known free flow keeps the 60 km/h convention
    (:data:`DEFAULT_FREE_FLOW_KMH`).  The warning itself is RATE-LIMITED:
    at most once per (rung, segment) per :data:`OOS_LOG_WINDOW_SEC`; every
    repeat inside the window is counted as suppressed (``/health`` →
    ``forecaster.out_of_support_log``) — the abstention itself is never
    suppressed, only the log line.

Data quality: a ``traffic:latest`` row flagged ``anomalous`` (Tier-1) never
feeds a rung.  The DSTGAT and heuristic rungs project that latest row, so
they give such a segment no row; the foundation rung never reads it — its
binned history holds no anomalous sample — and may still answer the
segment from that clean history (``ForecastMeta.anomalous_latest`` lists
the flagged segments, ``anomalous_excluded`` those left without a row).
``synthetic`` (simulated-feed) rows are usable, and every answer's input
provenance is reported per segment (``ForecastMeta.input_quality``; the
HTTP layer surfaces it).

Foundation rung (wave-2):
    When FOUNDATION_MODEL_PATH (VVIP_FOUNDATION_MODEL_PATH) points at the
    t0-alpha ONNX INT8 artifact (VVIP/models-local/t0/, Apache-2.0), a
    zero-shot quantile forecaster rung sits ahead of the DSTGAT/ONNX rung:
    gate -> foundation -> DSTGAT -> heuristic cache fallback.  The rung
    reads REAL history from a time-binned window
    (predict/nn.foundation_adapter.RollingHistoryWindow: 300 s bins keyed
    by observation time, NaN-padded, aligned per batch) that the ingest
    history feed fills from traffic.observations (predict/history_feed.py)
    — predict() itself never writes the window, so a decode step means
    five minutes of observation time, whoever calls and however often.
    Segments that are not ready (fewer than 12 observed 5-min steps, or a
    stale newest bin) fall through to the next rung; when none is ready the
    rung degrades loudly with ``last_degradation_reason =
    "foundation_input:<reason>"``.  Inference runs on a dedicated worker
    thread (never on the event loop) and is cached per (segments,
    horizons, window version).  Congestion is derived against each
    segment's own free flow (its speed limit, :meth:`set_free_flow`).
    Forecast rows from this rung carry the optional ``quantiles`` object of
    the wave-2 contract (predict/forecast_quantiles.py): ``{speed_kmh:
    {p10,p50,p90}, congestion_idx: {p10,p50,p90}}`` alongside the
    unchanged point fields.  Point-only rungs (DSTGAT head, heuristic) omit
    ``quantiles`` and label the omission on ``last_quantile_reason``;
    ``last_forecast_rung`` records which rung produced the returned
    payload.  Side channels ride attributes + log events, never the point
    fields' values; :meth:`predict_with_meta` returns the same labels per
    call (race-free for concurrent callers).
"""

from __future__ import annotations

import asyncio
import functools
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import structlog

from traffic_oracle.data.cache import TrafficCache
from traffic_oracle.data.graph_queries import CorridorGraphDB
from traffic_oracle.predict.nn.config import DSTGATConfig
from traffic_oracle.predict.nn.foundation_adapter import (
    HORIZON_TO_STEP,
    FoundationInputError,
    RollingHistoryWindow,
)
from traffic_oracle.predict.support_bounds import (
    PACKAGE_DEFAULT_PATH,
    RUNG_DSTGAT,
    RUNG_FOUNDATION,
    SupportBounds,
)
from traffic_oracle.runtime.memory_profiler import profile_memory

if TYPE_CHECKING:  # pragma: no cover — static type checkers only
    # Heavy imports stay OUT of module scope so this module (and everything
    # that imports it) loads in the pure-python gate venv: OnnxServe pulls
    # onnxruntime, and the construction sites below import it lazily — the
    # same pattern train()/export_onnx() already use for torch.
    from collections.abc import Callable, Mapping, Sequence

    from traffic_oracle.predict.nn.foundation_adapter import T0FoundationAdapter
    from traffic_oracle.runtime.onnx_serve import OnnxServe

logger = structlog.get_logger(__name__)

HORIZONS = [5, 10, 15, 30]  # minutes

#: The out-of-support warning fires at most once per (rung, segment) per
#: this many seconds; repeats inside the window are counted, not logged.
OOS_LOG_WINDOW_SEC = 600.0

#: Free flow assumed for a segment whose speed limit is unknown — the
#: speed_norm = speed_kmh / 60 convention of the DSTGAT feature space.
DEFAULT_FREE_FLOW_KMH = 60.0

#: Foundation results cached per (segments, horizons, window/free-flow
#: version): repeated callers inside one window version reuse one run.
_FOUNDATION_CACHE_SIZE = 16


def _load_support_bounds(path: str | None) -> tuple[SupportBounds, str]:
    """Resolve the support-bounds reference for the out-of-support gate.

    Explicit path wins; None falls back to the committed package default.
    A missing or malformed file disables the gate LOUDLY (warning + absent
    sentinel) rather than bricking predictions — an operator
    misconfiguration must not take the forecaster down with it.
    """
    resolved = Path(path) if path else PACKAGE_DEFAULT_PATH
    label = str(resolved)
    if not resolved.exists():
        logger.warning("flow_forecaster.support_bounds_missing", path=label)
        return SupportBounds.absent(), label
    try:
        bounds = SupportBounds.from_json(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(
            "flow_forecaster.support_bounds_invalid", path=label, error=str(exc)
        )
        return SupportBounds.absent(), label
    return bounds, label


@dataclass(frozen=True)
class ForecastMeta:
    """Per-call labels of one predict() answer (race-free side channel)."""

    rung: str | None
    degradation_reason: str | None
    quantile_reason: str | None
    out_of_support_segments: tuple[int, ...] = ()
    input_quality: dict[int, str] = field(default_factory=dict)
    foundation_segments: tuple[int, ...] = ()
    fallback_segments: tuple[int, ...] = ()
    #: segments left WITHOUT a row because their latest input was anomalous
    anomalous_excluded: tuple[int, ...] = ()
    #: segments whose latest cached row is flagged anomalous (answered only
    #: when the foundation rung could forecast them from clean history)
    anomalous_latest: tuple[int, ...] = ()

    def provenance(self) -> dict:
        """Compact, JSON-safe summary for HTTP responses."""
        counts: dict[str, int] = {}
        for quality in self.input_quality.values():
            counts[quality] = counts.get(quality, 0) + 1
        return {
            "rung": self.rung,
            "degradation_reason": self.degradation_reason,
            "quantile_reason": self.quantile_reason,
            "input_quality": counts,
            "simulated_feed": counts.get("synthetic", 0) + counts.get("mixed", 0) > 0,
            "foundation_segments": len(self.foundation_segments),
            "fallback_segments": len(self.fallback_segments),
            "out_of_support_segments": list(self.out_of_support_segments),
            "anomalous_excluded": list(self.anomalous_excluded),
            "anomalous_latest": list(self.anomalous_latest),
        }


def free_flow_or_default(free_flow_kmh: float | None) -> float:
    """A segment's free flow (its speed limit), or the 60 km/h convention
    when it is unknown or not positive.  Pure."""
    if free_flow_kmh and free_flow_kmh > 0:
        return float(free_flow_kmh)
    return DEFAULT_FREE_FLOW_KMH


def speed_norm(speed_kmh: float, free_flow_kmh: float | None) -> float:
    """Relative speed: km/h over the segment's free flow (its speed limit),
    the 60 km/h convention when the free flow is unknown.  Pure."""
    return float(speed_kmh) / free_flow_or_default(free_flow_kmh)


def speed_norm_matrix(
    speed_kmh: np.ndarray,
    segment_ids: Sequence[int],
    free_flow_kmh: Mapping[int, float | None],
) -> np.ndarray:
    """:func:`speed_norm` over a (time x segment) km/h matrix: column ``j``
    divided by the free flow of ``segment_ids[j]``.

    The DSTGAT training loader builds its features with this, so a model is
    trained on the same relative speed the out-of-support gate checks and
    the inference rung feeds (a free-flowing 100 km/h motorway reads 1.0,
    not 1.67).  Pure; float32 out.
    """
    speeds = np.asarray(speed_kmh, dtype=np.float32)
    if speeds.ndim != 2 or speeds.shape[1] != len(segment_ids):
        raise ValueError(
            f"speed matrix {speeds.shape} does not match {len(segment_ids)} segment ids"
        )
    free_flow = np.array(
        [free_flow_or_default(free_flow_kmh.get(int(sid))) for sid in segment_ids],
        dtype=np.float32,
    )
    return speeds / free_flow


def _gate_values(obs: object, free_flow_kmh: float | None = None) -> dict[str, float]:
    """The per-rung gate features of one observation (km/h + normalised)."""
    speed = float(obs.speed_kmh)  # type: ignore[attr-defined]
    return {
        "speed_kmh": speed,
        "speed_norm": speed_norm(speed, free_flow_kmh),
        "congestion_idx": float(obs.congestion_idx),  # type: ignore[attr-defined]
    }


def _quality_of(obs: object) -> str:
    raw = getattr(obs, "data_quality", "real")
    return str(getattr(raw, "value", raw) or "real")


class FlowForecaster:
    """Spatio-temporal traffic flow prediction using DSTGAT via ONNX.

    Operates on the road segment graph to predict future corridor conditions.
    Exports to ONNX for low-footprint GPU inference.
    """

    def __init__(
        self,
        onnx_model_path: str | None = None,
        cache: TrafficCache | None = None,
        graph_db: CorridorGraphDB | None = None,
        arbiter: object | None = None,
        config: DSTGATConfig | None = None,
        support_bounds_path: str | None = None,
        support_bounds: SupportBounds | None = None,
        foundation_model_path: str | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self._onnx_path = onnx_model_path
        self._cache = cache
        self._graph_db = graph_db
        self._arbiter = arbiter
        self._onnx_serve: OnnxServe | None = None
        self._config = config or DSTGATConfig()
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        #: Reason for the last heuristic-rung answer ("no_model" |
        #: "out_of_support:<features>" | "foundation_input:<reason>"); rides
        #: log events + this attribute, never the forecast payload (the
        #: point fields stay {speed_kmh, congestion_idx} exactly).
        self.last_degradation_reason: str | None = None
        #: Wave-2 quantile contract side channel: None when the returned
        #: rows carry ``quantiles`` objects; otherwise
        #: "quantiles_absent:<producing rung>" labels the point-only answer.
        self.last_quantile_reason: str | None = None
        #: Which rung produced the last returned payload: "foundation" |
        #: "dstgat" | "heuristic" (None before the first predict).  In a
        #: mixed batch it names the model rung; the out-of-support rows are
        #: listed on ``last_out_of_support_segments``.
        self.last_forecast_rung: str | None = None
        #: Segments of the last predict() answered by heuristic rows
        #: because their observation was outside the support bounds.
        self.last_out_of_support_segments: list[int] = []
        #: The full per-call labels of the last predict().
        self.last_meta: ForecastMeta | None = None
        #: Rate limit of the out_of_support warning: (rung, segment) → the
        #: monotonic time it was last logged; repeats inside the window are
        #: counted in ``oos_log_suppressed`` (surfaced on /health).
        self._oos_logged: dict[tuple[str, int], float] = {}
        self.oos_log_emitted = 0
        self.oos_log_suppressed = 0
        self._monotonic: Callable[[], float] = time.monotonic

        if onnx_model_path and Path(onnx_model_path).exists():
            # Lazy import keeps onnxruntime out of module scope (see above).
            from traffic_oracle.runtime.onnx_serve import OnnxServe

            self._onnx_serve = OnnxServe(onnx_model_path, arbiter)

        # Foundation rung: a SEPARATE slot from the dormant DSTGAT
        # ONNX_MODEL_PATH. Loaded only when the path is set AND present —
        # absent keeps the rung dormant with "no_model" semantics intact.
        # A PRESENT-but-unloadable artifact (corrupt protobuf, unreadable
        # bytes, missing onnxruntime) disables the rung loudly and leaves
        # behaviour unchanged — the same fail-open philosophy as the
        # support-bounds gate (a misconfigured file must not brick
        # startup; the reviewer-repro'd InvalidProtobuf crash).
        self._foundation_adapter: T0FoundationAdapter | None = None
        if foundation_model_path and Path(foundation_model_path).exists():
            try:
                self._foundation_adapter = self._load_foundation(
                    Path(foundation_model_path)
                )
            except Exception as exc:  # noqa: BLE001 — loader boundary, fails OPEN
                logger.warning(
                    "flow_forecaster.foundation_load_failed",
                    path=foundation_model_path,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
        # Time-binned history window (300 s bins keyed by observation
        # time) — filled by the ingest history feed, read by the rung.
        self._foundation_window = RollingHistoryWindow()
        self._foundation_executor: ThreadPoolExecutor | None = None
        self._foundation_cache: OrderedDict[tuple, dict] = OrderedDict()
        self._free_flow: dict[int, float] = {}
        self._free_flow_version = 0

        if support_bounds is not None:
            self._support_bounds = support_bounds
            self._bounds_path = support_bounds.source or "<injected>"
        else:
            self._support_bounds, self._bounds_path = _load_support_bounds(
                support_bounds_path
            )

        logger.info(
            "flow_forecaster.init",
            onnx_path=onnx_model_path,
            foundation_path=foundation_model_path,
            foundation_active=self._foundation_adapter is not None,
            bounds_path=self._bounds_path,
            bounds_features=list(self._support_bounds.feature_names),
            bounds_rungs=list(self._support_bounds.rung_names),
        )

    # -- history + free flow (fed by predict/history_feed.py) ---------------

    @property
    def history_window(self) -> RollingHistoryWindow:
        """The foundation rung's time-binned history (the feed writes it)."""
        return self._foundation_window

    @property
    def foundation_active(self) -> bool:
        return self._foundation_adapter is not None

    def set_free_flow(self, free_flow_kmh: dict[int, float]) -> None:
        """Per-segment free-flow speeds (speed limits) for derived congestion."""
        cleaned = {
            int(k): float(v)
            for k, v in free_flow_kmh.items()
            if v is not None and np.isfinite(v) and float(v) > 0
        }
        if cleaned != self._free_flow:
            self._free_flow = cleaned
            self._free_flow_version += 1

    def close(self) -> None:
        """Release the foundation worker thread (lifespan teardown)."""
        if self._foundation_executor is not None:
            self._foundation_executor.shutdown(wait=False, cancel_futures=True)
            self._foundation_executor = None

    @staticmethod
    def _load_foundation(path: Path) -> T0FoundationAdapter:
        """Load the t0-alpha ONNX INT8 artifact behind the foundation rung.

        Lazy onnxruntime import (module scope stays gate-venv pure). CPU
        execution provider only: measured on this aarch64 host
        (probe_foundation_t0.py); onnxruntime-gpu aarch64 availability is
        unverified, so the arbiter's GPU path is deliberately not wired
        here — the GpuArbiter keeps serving the DSTGAT rung.
        """
        import onnxruntime as ort

        from traffic_oracle.predict.nn.foundation_adapter import (
            T0FoundationAdapter,
            session_runner,
        )

        options = ort.SessionOptions()
        options.intra_op_num_threads = 4
        options.inter_op_num_threads = 2
        options.graph_optimization_level = (
            ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        )
        session = ort.InferenceSession(
            str(path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        logger.info(
            "flow_forecaster.foundation_loaded",
            path=str(path),
            provider=session.get_providers()[0],
            size_mb=round(path.stat().st_size / 1_048_576, 1),
        )
        return T0FoundationAdapter(session_runner(session))

    # -- prediction ------------------------------------------------------------

    @profile_memory
    async def predict(
        self,
        segment_ids: list[int],
        horizons_min: list[int] | None = None,
    ) -> dict:
        """Predict traffic flow for given segments at specified horizons.

        Args:
            segment_ids: Road segment IDs to forecast
            horizons_min: Prediction horizons in minutes (default: [5, 10, 15, 30])

        Returns:
            Dict mapping segment_id → {horizon_min: {speed_kmh, congestion_idx}}
        """
        result, _meta = await self.predict_with_meta(segment_ids, horizons_min)
        return result

    async def predict_with_meta(
        self,
        segment_ids: list[int],
        horizons_min: list[int] | None = None,
    ) -> tuple[dict, ForecastMeta]:
        """:meth:`predict` plus this call's own labels (:class:`ForecastMeta`).

        The ``last_*`` attributes are also set (backward compatible), but a
        concurrent caller may overwrite them; the returned meta is this
        call's.
        """
        if horizons_min is None:
            horizons_min = HORIZONS

        if self._foundation_adapter is None and self._onnx_serve is None:
            logger.warning("flow_forecaster.no_model, using cache fallback")
            rows, quality, anomalous = await self._heuristic_rows(
                segment_ids, horizons_min
            )
            meta = ForecastMeta(
                rung="heuristic",
                degradation_reason="no_model",
                quantile_reason="quantiles_absent:heuristic_rung",
                input_quality=quality,
                fallback_segments=tuple(rows),
                anomalous_excluded=tuple(anomalous),
                anomalous_latest=tuple(anomalous),
            )
            return rows, self._publish_meta(meta)

        N_max = self._config.max_nodes
        candidate_ids = segment_ids[:N_max]

        # Latest usable cached observation per candidate segment (gate
        # input).  A Tier-1 'anomalous' latest row feeds nothing: the rungs
        # that project it (DSTGAT, heuristic) skip that segment.
        observed: dict[int, object] = {}
        anomalous: list[int] = []
        if self._cache is not None:
            for sid in candidate_ids:
                obs = await self._cache.get_latest(sid)
                if obs is None:
                    continue
                if _quality_of(obs) == "anomalous":
                    anomalous.append(sid)
                    continue
                observed[sid] = obs
        excluded = set(anomalous)

        result: dict = {}
        quality: dict[int, str] = {}
        reasons: list[str] = []
        abstained: list[int] = []
        foundation_ids: list[int] = []
        dstgat_ids: list[int] = []
        # The foundation rung reads only its binned history (anomalous
        # samples never enter it), so every candidate may be answered by
        # it; the later rungs project the latest row and skip `excluded`.
        remaining = list(candidate_ids)

        # Foundation rung (wave-2): zero-shot quantile forecaster over the
        # time-binned history window — preferred over DSTGAT when wired.
        # Contract failures degrade LOUDLY to the next rung with a labelled
        # reason; they never zero-pad history nor fake bands.
        foundation_failure: str | None = None
        if self._foundation_adapter is not None and remaining:
            oos = self._gate(RUNG_FOUNDATION, remaining, observed, reasons)
            abstained.extend(oos)
            in_support = [sid for sid in remaining if sid not in oos]
            if in_support:
                try:
                    rows = await self._run_foundation(in_support, horizons_min)
                except FoundationInputError as exc:
                    foundation_failure = (
                        "foundation_input:" + str(exc).split()[0].rstrip(":")
                    )
                    logger.warning(
                        "flow_forecaster.foundation_input",
                        reason=foundation_failure,
                        error=str(exc),
                    )
                else:
                    result.update(rows)
                    foundation_ids = list(rows)
                    quality.update(
                        self._foundation_window.quality(foundation_ids)
                    )
            remaining = [sid for sid in in_support if sid not in result]
        remaining = [sid for sid in remaining if sid not in excluded]

        # DSTGAT rung (dormant unless its ONNX export exists).
        if self._onnx_serve is not None and remaining:
            oos = self._gate(RUNG_DSTGAT, remaining, observed, reasons)
            abstained.extend(oos)
            active = [sid for sid in remaining if sid not in oos]
            whole_batch = not any(sid in observed for sid in active)
            if active and not whole_batch:
                rows = await self._run_dstgat(active, horizons_min, observed)
                result.update(rows)
                dstgat_ids = list(rows)
                for sid in dstgat_ids:
                    if sid in observed:
                        quality[sid] = _quality_of(observed[sid])
                remaining = [sid for sid in active if sid not in result]
            elif active:
                remaining = active

        # Cache the model rows (point fields only — see _cache_forecast_rows).
        if result:
            await self._cache_forecast_rows(result)

        # Heuristic rows for every other requested segment (abstained,
        # not-ready, beyond the node cap) — never for anomalous ones.
        fallback_ids = [
            sid for sid in segment_ids if sid not in result and sid not in excluded
        ]
        fallback_rows: dict = {}
        if fallback_ids:
            fallback_rows, fb_quality, fb_anomalous = await self._heuristic_rows(
                fallback_ids, horizons_min
            )
            quality.update(fb_quality)
            anomalous.extend(s for s in fb_anomalous if s not in excluded)
        result.update(fallback_rows)

        if foundation_ids:
            rung = "foundation"
        elif dstgat_ids:
            rung = "dstgat"
        else:
            rung = "heuristic"
        if dstgat_ids:
            quantile_reason: str | None = "quantiles_absent:dstgat_point_head"
        elif fallback_rows:
            quantile_reason = "quantiles_absent:heuristic_rung"
        else:
            quantile_reason = None
        degradation = foundation_failure or (
            "out_of_support:" + ",".join(sorted(set(reasons))) if reasons else None
        )
        if rung != "heuristic":
            logger.info(
                "flow_forecaster.predict",
                segments=len(foundation_ids) + len(dstgat_ids),
                horizons=horizons_min,
                rung=rung,
                out_of_support_rows=len(abstained),
                fallback_rows=len(fallback_rows),
            )
        meta = ForecastMeta(
            rung=rung,
            degradation_reason=degradation,
            quantile_reason=quantile_reason,
            out_of_support_segments=tuple(sorted(set(abstained))),
            input_quality=quality,
            foundation_segments=tuple(foundation_ids),
            fallback_segments=tuple(fallback_rows),
            anomalous_excluded=tuple(
                sorted(sid for sid in set(anomalous) if sid not in result)
            ),
            anomalous_latest=tuple(sorted(set(anomalous))),
        )
        return result, self._publish_meta(meta)

    def _publish_meta(self, meta: ForecastMeta) -> ForecastMeta:
        self.last_meta = meta
        self.last_forecast_rung = meta.rung
        self.last_degradation_reason = meta.degradation_reason
        self.last_quantile_reason = meta.quantile_reason
        self.last_out_of_support_segments = list(meta.out_of_support_segments)
        return meta

    def out_of_support_log_health(self) -> dict:
        """The out_of_support warning's rate limit, for /health."""
        return {
            "window_sec": OOS_LOG_WINDOW_SEC,
            "emitted": self.oos_log_emitted,
            "suppressed": self.oos_log_suppressed,
            "tracked": len(self._oos_logged),
        }

    def _oos_to_log(self, rung: str, segments: list[int]) -> list[int]:
        """The violators whose (rung, segment) warning is due — at most one
        per :data:`OOS_LOG_WINDOW_SEC`; the rest are counted suppressed."""
        now = self._monotonic()
        due: list[int] = []
        for sid in segments:
            last = self._oos_logged.get((rung, sid))
            if last is not None and now - last < OOS_LOG_WINDOW_SEC:
                self.oos_log_suppressed += 1
                continue
            self._oos_logged[(rung, sid)] = now
            due.append(sid)
        if len(self._oos_logged) > 4096:  # bounded: forget expired entries
            self._oos_logged = {
                k: t for k, t in self._oos_logged.items() if now - t < OOS_LOG_WINDOW_SEC
            }
        return due

    def _gate(
        self,
        rung: str,
        segment_ids: list[int],
        observed: dict[int, object],
        reasons: list[str],
    ) -> list[int]:
        """Per-segment out-of-support check for ``rung``; returns violators.

        The abstention is always applied; only the warning is rate-limited
        (once per rung and segment per window, repeats counted)."""
        bounds = self._support_bounds.for_rung(rung)
        values = {
            sid: _gate_values(observed[sid], self._free_flow.get(sid))
            for sid in segment_ids
            if sid in observed
        }
        verdict = bounds.check(values)
        if verdict.in_support:
            return []
        oos = sorted(
            {v.segment_id for v in verdict.violations if v.segment_id is not None}
        )
        reasons.extend(verdict.features)
        due = self._oos_to_log(rung, oos)
        if due:
            self.oos_log_emitted += 1
            logger.warning(
                "flow_forecaster.out_of_support",
                rung=rung,
                features=list(verdict.features),
                segments=due,
                bounds_path=self._bounds_path,
                whole_batch=not any(sid not in oos for sid in values),
                suppressed_repeats=len(oos) - len(due),
            )
        return oos

    async def _run_foundation(
        self,
        active_ids: list[int],
        horizons_min: list[int],
    ) -> dict:
        """Foundation rung body: binned window -> adapter (worker thread).

        Raises :class:`FoundationInputError` (mapped by the caller to the
        ``foundation_input:<reason>`` label) whenever the artifact's input
        contract is not met — no ready segment (warm-up / stale feed), an
        unobserved-only batch, or crossing quantiles. History comes from
        the time-binned window only; this rung never zero-pads.  Segments
        the window could not make ready are simply absent from the result.
        """
        horizons = [h for h in horizons_min if h in HORIZON_TO_STEP][:4]
        if not horizons:
            raise FoundationInputError(
                f"horizons_off_contract: {horizons_min} has no horizon in "
                f"{sorted(HORIZON_TO_STEP)}"
            )
        # PARTIALLY off-contract requests (e.g. [5, 60]) are answered for
        # the on-contract subset — but never silently: the dropped horizons
        # are labelled on the log so no caller mistakes a subset for the
        # full request (review finding). The [:4] cap mirrors the DSTGAT
        # rung's positional horizons_min[:4] semantics.
        dropped = [h for h in horizons_min if h not in HORIZON_TO_STEP]
        if dropped:
            logger.warning(
                "flow_forecaster.foundation_horizons_dropped",
                requested=list(horizons_min),
                dropped=dropped,
                answered=horizons,
            )
        window = self._foundation_window
        key = (
            tuple(sorted(active_ids)),
            tuple(horizons),
            window.version,
            self._free_flow_version,
            window.bin_of(self._clock_ms()),
        )
        cached = self._foundation_cache.get(key)
        if cached is not None:
            self._foundation_cache.move_to_end(key)
            return {sid: dict(per) for sid, per in cached.items()}

        history = window.history(active_ids, now_ms=self._clock_ms())
        adapter = self._foundation_adapter
        free_flow = dict(self._free_flow)
        if self._foundation_executor is None:
            self._foundation_executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="foundation"
            )
        loop = asyncio.get_running_loop()
        rows = await loop.run_in_executor(
            self._foundation_executor,
            functools.partial(adapter.forecast, history, free_flow),  # type: ignore[union-attr]
        )
        result = {
            sid: {h: row for h, row in per.items() if h in horizons}
            for sid, per in rows.items()
        }
        self._foundation_cache[key] = result
        while len(self._foundation_cache) > _FOUNDATION_CACHE_SIZE:
            self._foundation_cache.popitem(last=False)
        return {sid: dict(per) for sid, per in result.items()}

    async def _run_dstgat(
        self,
        active_ids: list[int],
        horizons_min: list[int],
        observed: dict[int, object],
    ) -> dict:
        """DSTGAT rung body (point head; dormant without its ONNX export)."""
        N_max = self._config.max_nodes
        T = self._config.lookback_steps
        N = len(active_ids)

        # Build node features from cache (in-support / unobserved segments)
        node_features = np.zeros((1, N_max, T, 8), dtype=np.float32)
        for i, sid in enumerate(active_ids):
            obs = observed.get(sid)
            if obs is not None:
                # Fill last timestep (most recent) — the same relative speed
                # the gate checked (speed / the segment's free flow)
                node_features[0, i, T - 1, 0] = speed_norm(
                    obs.speed_kmh, self._free_flow.get(sid)  # type: ignore[attr-defined]
                )
                node_features[0, i, T - 1, 1] = obs.congestion_idx  # type: ignore[attr-defined]
                node_features[0, i, T - 1, 7] = 1.0  # neighbor ratio default

        # Build adjacency from graph DB
        adjacency = np.zeros((N_max, N_max), dtype=np.float32)
        if self._graph_db is not None:
            seg_to_idx = {sid: i for i, sid in enumerate(active_ids)}
            for sid in active_ids:
                neighbors = await self._graph_db.multi_hop_neighbors(sid, hops=1)
                idx_src = seg_to_idx[sid]
                for nbr in neighbors:
                    if nbr in seg_to_idx:
                        adjacency[idx_src, seg_to_idx[nbr]] = 1.0

        # Node mask: True for padding nodes
        node_mask = np.ones((1, N_max), dtype=bool)
        node_mask[0, :N] = False

        # Run ONNX inference
        inputs = {
            "node_features": node_features,
            "adjacency": adjacency,
            "node_mask": node_mask,
        }
        outputs = await self._onnx_serve.infer(inputs)  # type: ignore[union-attr]
        predictions = outputs.get("predictions")  # (1, N_max, 4, 2)

        if predictions is None:
            return {}

        # Unpack into per-segment results
        result: dict = {}
        for i, sid in enumerate(active_ids):
            result[sid] = {}
            for hi, horizon in enumerate(horizons_min[:4]):
                result[sid][horizon] = {
                    "speed_kmh": float(predictions[0, i, hi, 0]),
                    "congestion_idx": float(np.clip(predictions[0, i, hi, 1], 0, 1)),
                }
        return result

    async def _cache_forecast_rows(self, result: dict) -> None:
        """Write the POINT fields of forecast rows to the shared cache.

        Deliberately lossy: the ``quantiles`` objects do NOT ride the cache
        — the ``traffic:forecast:{sid}:{h}`` JSON format is point-only and
        every current reader consumes the point fields; bands are available
        on the returned payload (HTTP /predict/flow) and via the adapter.
        """
        if self._cache is None:
            return
        for sid, preds in result.items():
            for horizon, vals in preds.items():
                key = f"traffic:forecast:{sid}:{horizon}"
                await self._cache._client.set(
                    key,
                    f'{{"speed_kmh":{vals["speed_kmh"]:.1f},"congestion_idx":{vals["congestion_idx"]:.3f}}}',
                    ex=60,
                )

    async def _cache_fallback(
        self,
        segment_ids: list[int],
        horizons_min: list[int],
    ) -> dict:
        """Fallback prediction using cached live data when no ONNX model is available.

        Projects current traffic conditions forward with slight temporal decay,
        simulating realistic near-term forecast degradation. Point-only rows —
        the producing rung is labelled on the quantile side channel.
        """
        self.last_forecast_rung = "heuristic"
        self.last_quantile_reason = "quantiles_absent:heuristic_rung"
        rows, _quality, _anomalous = await self._heuristic_rows(segment_ids, horizons_min)
        return rows

    async def _heuristic_rows(
        self,
        segment_ids: list[int],
        horizons_min: list[int],
    ) -> tuple[dict, dict[int, str], list[int]]:
        """The heuristic rung's rows (no side-channel writes).

        Projects current traffic conditions forward with slight temporal
        decay; segments without a cached observation get no row, and
        neither do segments whose latest row is Tier-1 ``anomalous``.
        Returns (rows, per-segment input quality, anomalous segments).
        """
        if self._cache is None:
            return {}, {}, []

        result: dict = {}
        quality: dict[int, str] = {}
        anomalous: list[int] = []
        for sid in segment_ids:
            obs = await self._cache.get_latest(sid)
            if obs is None:
                continue
            q = _quality_of(obs)
            if q == "anomalous":
                anomalous.append(sid)
                continue
            quality[sid] = q
            base_speed = obs.speed_kmh
            base_cong = obs.congestion_idx
            result[sid] = {}
            for horizon in horizons_min[:4]:
                # Apply small temporal drift: congestion rises ~2% per 5min horizon
                drift = 1.0 + (horizon / 5) * 0.02
                predicted_cong = min(1.0, base_cong * drift)
                # Speed decreases slightly as congestion increases
                speed_factor = 1.0 - (horizon / 5) * 0.015
                predicted_speed = max(5.0, base_speed * speed_factor)
                result[sid][horizon] = {
                    "speed_kmh": round(predicted_speed, 1),
                    "congestion_idx": round(predicted_cong, 3),
                }

        logger.info(
            "flow_forecaster.cache_fallback",
            segments=len(result),
            horizons=horizons_min,
            anomalous_excluded=len(anomalous),
        )
        return result, quality, anomalous

    @profile_memory
    async def train(self, epochs: int = 50, batch_size: int = 32) -> None:
        """Train the DSTGAT model on historical traffic data.

        VRAM Policy: Only train when LLM is idle. Use gpu_arbiter to acquire GPU lease.
        """
        from traffic_oracle.predict.data_loader import TrafficDataLoader
        from traffic_oracle.predict.training_pipeline import TrainingPipeline
        from traffic_oracle.runtime.onnx_serve import OnnxServe

        data_loader = TrafficDataLoader(
            database_url="postgresql://corridor_admin:dev_changeme@localhost:5432/corridor_db",
            config=self._config,
        )

        pipeline = TrainingPipeline(
            config=self._config,
            data_loader=data_loader,
            model_output_dir="models/onnx",
        )

        result = await pipeline.run(epochs=epochs, batch_size=batch_size)

        # Reload ONNX session with new model
        self._onnx_serve = OnnxServe(result.onnx_path, self._arbiter)
        logger.info(
            "flow_forecaster.training_complete",
            val_loss=result.best_val_loss,
            epochs=result.epochs_trained,
            model_kb=result.model_size_kb,
        )

    @profile_memory
    def export_onnx(self, output_path: str) -> None:
        """Export trained PyTorch model to ONNX for low-VRAM inference."""
        from traffic_oracle.predict.nn.dstgat import DSTGAT
        from traffic_oracle.predict.nn.quantize import ModelQuantizer

        model = DSTGAT(self._config)
        ModelQuantizer.export_to_onnx(
            model, output_path,
            self._config.max_nodes,
            self._config.lookback_steps,
            self._config.in_features,
        )
        logger.info("flow_forecaster.export_onnx", path=output_path)
