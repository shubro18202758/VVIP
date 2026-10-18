"""probe_foundation_t0.py — real-artifact validation + measurement for the
t0-alpha ONNX INT8 rung (runs under VVIP/.venvs/wave2; needs onnxruntime).

Invoked as::

    VVIP/.venvs/wave2/bin/python VVIP/scripts/probe_foundation_t0.py

This is part (b) of VVIP/scripts/check_t0_forecaster.sh. Every path
resolves from this file's own location (any cwd). What it does:

    1. Integrity: sha256 of VVIP/models-local/t0/t0-alpha-grouped-int8.onnx
       must equal the hash declared in the artifact's own manifest.json.
    2. Contract shapes: drive the adapter
       (traffic_oracle.predict.nn.foundation_adapter) end-to-end against the
       REAL graph — traffic-like speed history, 200 segments x 96 steps —
       and assert rows exactly {speed_kmh, congestion_idx, quantiles} with
       the wave-2 quantile object {speed_kmh, congestion_idx} x
       {p10, p50, p90} (predict/forecast_quantiles.py).
    3. Monotonic + finite: every row passes the CONSUMER validator
       (validated_bands — p10 <= p50 <= p90 per field), all point and band
       values finite; the full decode tensor is monotone across all five
       native levels at every step/segment.
    4. Determinism: two full forecasts bitwise identical.
    5. Latency bound: mean of 3 warm 200-segment adapter-path runs must
       stay under LATENCY_BUDGET_MS. Measured on this host (2026-10-18):
       485 ms bare session.run, 600–1123 ms full adapter path on a busy
       shared machine — the 5000 ms bound is ~5x the worst observed run,
       tight enough to catch a pathological regression (graph reload per
       call, single-thread fallback) while tolerating host jitter.
    6. Measurement report: artifact size, load time, cold/steady latency,
       process RSS (VmRSS/VmHWM) vs the 0.4 GiB traffic-oracle row-5
       budget. The budget comparison is REPORTED, not asserted — the
       spike measured a load-bound ~1.4 GiB (ORT materializes the int8
       weights as fp32; models-local/t0/NOTICE records it) and the hosting
       decision belongs to the deployment stage, not this probe.

Exit code: non-zero on any ASSERTED failure (integrity, contract shape,
monotonicity, finiteness, determinism, latency bound, load bound).
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort

SCRIPT_DIR = Path(__file__).resolve().parent
VVIP_DIR = SCRIPT_DIR.parent
ARTIFACT_DIR = VVIP_DIR / "models-local" / "t0"
ARTIFACT = ARTIFACT_DIR / "t0-alpha-grouped-int8.onnx"
MANIFEST = ARTIFACT_DIR / "manifest.json"
SERVICE_SRC = VVIP_DIR / "services" / "traffic-oracle" / "src"

sys.path.insert(0, str(SERVICE_SRC))

from traffic_oracle.predict.forecast_quantiles import validated_bands  # noqa: E402
from traffic_oracle.predict.nn.foundation_adapter import (  # noqa: E402
    CONTRACT_HORIZONS,
    T0FoundationAdapter,
    session_runner,
)

BUDGET_MIB = 0.4 * 1024  # MEMORY_BUDGET.md row 5: traffic-oracle 0.4 GiB
LOAD_BUDGET_S = 30.0     # measured 0.85–1.0 s; 30 s catches pathologies
LATENCY_BUDGET_MS = 5000.0  # measured mean 916 ms busy-host; see docstring
SEGMENTS = 200
CONTEXT_STEPS = 96  # 8 h of 5-min samples — well past the 12-step floor


def rss_kib(field: str) -> float:
    with open("/proc/self/status", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith(field):
                return int(line.split()[1]) / 1024
    raise RuntimeError(f"/proc/self/status field {field} not found")


def traffic_history() -> dict[int, np.ndarray]:
    """Deterministic traffic-like km/h series: daily wave + congestion dips."""
    rng = np.random.default_rng(42)
    t = np.arange(CONTEXT_STEPS, dtype=np.float32)
    history: dict[int, np.ndarray] = {}
    for sid in range(SEGMENTS):
        base = 55.0 - 10.0 * np.sin(2 * np.pi * t / 96.0)
        if sid % 3 == 0:  # a third of segments get an incident-style dip
            base = base - 25.0 * np.exp(-((t - 70.0) ** 2) / (2 * 24.0**2))
        history[sid] = np.clip(base + rng.normal(0, 1.5, CONTEXT_STEPS),
                               3.0, 80.0).astype(np.float32)
    return history


def require(condition: bool, message: str) -> None:
    if not condition:
        print(f"FAIL {message}")
        sys.exit(1)


def main() -> int:
    print(f"artifact: {ARTIFACT}")
    if not ARTIFACT.exists():
        print(f"FAIL artifact missing: {ARTIFACT} (see models-local/t0/NOTICE)")
        return 1

    # 1 ── integrity against the artifact's own manifest
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    expected = manifest["artifact"]["sha256"]
    digest = hashlib.sha256(ARTIFACT.read_bytes()).hexdigest()
    size = ARTIFACT.stat().st_size
    print(f"sha256: {digest}")
    print(f"size_bytes: {size} (manifest: {manifest['artifact']['bytes']})")
    require(digest == expected, "integrity: sha256 mismatch vs manifest.json")
    require(size == manifest["artifact"]["bytes"],
            "integrity: size mismatch vs manifest.json")
    print("PASS integrity (sha256 + size match the shipped manifest)")

    # 2 ── session (thread settings mirror OnnxServe._load_session defaults)
    so = ort.SessionOptions()
    so.intra_op_num_threads = 4
    so.inter_op_num_threads = 2
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    rss_pre = rss_kib("VmRSS")
    t0 = time.perf_counter()
    session = ort.InferenceSession(str(ARTIFACT), sess_options=so,
                                   providers=["CPUExecutionProvider"])
    load_s = time.perf_counter() - t0
    require(load_s < LOAD_BUDGET_S, f"session load {load_s:.1f}s >= {LOAD_BUDGET_S}s")
    rss_loaded = rss_kib("VmRSS")
    print(f"provider: {session.get_providers()[0]}")
    print(f"load_s: {load_s:.2f} (bound {LOAD_BUDGET_S})")
    print(f"rss_mib_after_load: {rss_loaded:.0f} (python baseline {rss_pre:.0f})")

    adapter = T0FoundationAdapter(session_runner(session))
    history = traffic_history()

    # cold call (includes first-run allocations)
    t0 = time.perf_counter()
    payload = adapter.forecast(history)
    cold_ms = (time.perf_counter() - t0) * 1000

    # 3 ── contract shapes, monotone bands, finite values — every row
    require(set(payload) == set(range(SEGMENTS)), "payload segment set wrong")
    for sid in range(SEGMENTS):
        require(set(payload[sid]) == set(CONTRACT_HORIZONS),
                f"segment {sid}: horizon set {sorted(payload[sid])}")
        for h in CONTRACT_HORIZONS:
            row = payload[sid][h]
            require(set(row) == {"speed_kmh", "congestion_idx", "quantiles"},
                    f"row keys {sorted(row)} at sid={sid} h={h}")
            require(np.isfinite(row["speed_kmh"]) and np.isfinite(row["congestion_idx"]),
                    f"non-finite point at sid={sid} h={h}")
            # Consumer contract on REAL producer rows: valid bands or bust.
            bands, reason = validated_bands(row)
            require(reason is None and bands is not None,
                    f"validated_bands rejected sid={sid} h={h}: {reason}")
            for field, band in bands.items():
                require(all(np.isfinite(v) for v in band.values()),
                        f"non-finite band {field} at sid={sid} h={h}")
    print(f"PASS contract: rows {{speed_kmh, congestion_idx, quantiles}} x {SEGMENTS} segments; every row passes validated_bands (p10<=p50<=p90 per field, finite)")

    # 4 ── full-tensor quantile sanity (all 5 native levels, all steps)
    feeds = adapter.build_inputs(history)
    quantiles = session_runner(session)(feeds)
    diffs = np.diff(quantiles, axis=-1)
    require(float(diffs.min()) >= -1e-6,
            f"crossing quantiles in decode tensor: min diff {diffs.min()}")
    print(f"PASS quantile tensor: all 5 levels non-decreasing at every step/segment (min diff {float(diffs.min()):.2e}, shape {quantiles.shape})")

    # 5 ── determinism (bitwise)
    require(adapter.forecast(history) == payload, "non-deterministic forecast")
    print("PASS determinism: two full forecasts bitwise identical")

    # 6 ── latency bound (steady state, full adapter path)
    lat: list[float] = []
    for _ in range(3):
        t0 = time.perf_counter()
        adapter.forecast(history)
        lat.append((time.perf_counter() - t0) * 1000)
    mean_ms = sum(lat) / len(lat)
    rss_after = rss_kib("VmRSS")
    hwm = rss_kib("VmHWM")
    require(mean_ms < LATENCY_BUDGET_MS,
            f"latency {mean_ms:.0f} ms >= bound {LATENCY_BUDGET_MS:.0f} ms "
            f"({SEGMENTS} segments x {CONTEXT_STEPS} steps, adapter path)")
    print(f"latency_ms_cold_{SEGMENTS}seg: {cold_ms:.0f}")
    print(f"latency_ms_{SEGMENTS}seg: mean {mean_ms:.0f} / min {min(lat):.0f} / max {max(lat):.0f} (bound {LATENCY_BUDGET_MS:.0f})")

    print(f"rss_mib_after_inference: {rss_after:.0f} | VmHWM {hwm:.0f}")
    verdict = "WITHIN" if hwm < BUDGET_MIB else "OVER"
    print(f"REPORT budget: VmHWM {hwm:.0f} MiB vs 0.4 GiB ({BUDGET_MIB:.0f} MiB) -> {verdict}"
          f" ({hwm / BUDGET_MIB:.1f}x) — load-bound, documented in models-local/t0/NOTICE")

    p50s = np.array([[payload[sid][h]["speed_kmh"] for h in CONTRACT_HORIZONS]
                     for sid in range(SEGMENTS)])
    widths = np.array([
        payload[sid][h]["quantiles"]["speed_kmh"]["p90"]
        - payload[sid][h]["quantiles"]["speed_kmh"]["p10"]
        for sid in range(SEGMENTS) for h in CONTRACT_HORIZONS
    ])
    print(f"p50 speed km/h: min {p50s.min():.1f} / max {p50s.max():.1f} / mean {p50s.mean():.1f}")
    print(f"p90-p10 width km/h: min {widths.min():.1f} / median {np.median(widths):.1f} / max {widths.max():.1f}")

    print("PASS probe_foundation_t0 (all asserted invariants hold; measurements above)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
