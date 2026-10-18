"""Decider runtime: loads pplx-decider-v1-27b from the verified snapshot and
serves typed (choice/noul/score) decisions with compute-only serialization.

Load path (C1 contract): the model MUST be loaded via the snapshot directory
path. Every file in the HF snapshot is a double symlink into ../../blobs —
a naive copy of the tree yields dangling links, so we never copy.

This module performs NO outbound HTTP calls.
"""

from __future__ import annotations

import asyncio
import math
import os
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

import torch

from .config import Settings

MAX_OPTIONS = 249  # architecture contract: 249 single-token letter-codes


class ModelNotReady(RuntimeError):
    """Raised when /decide is called before the weights finish loading."""


class StateTooLarge(ValueError):
    """Encoded decision branch exceeded max_state_tokens (hard cap 8192)."""


class Runtime:
    """Wraps the autojev Decider with serialization, queueing and metrics."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._decider: Any = None            # inference.Decider
        self._autojev_answer: Any = None     # autojev.model.answer
        self._load_error: str | None = None
        self._load_done = threading.Event()
        self._load_started = False
        self._load_lock = threading.Lock()
        self._compute_lock = threading.Lock()   # compute-only serialization
        self._queue_counter = 0                 # running + waiting decides
        self._queue_lock = threading.Lock()
        self._latencies_ms: deque[float] = deque(
            maxlen=max(8, settings.latency_window)
        )
        self._decides_total = 0
        self._decides_failed = 0

    # ─── loading ────────────────────────────────────────────────────────

    def start_load(self) -> None:
        """Kick the (≈285 s cold) load on a daemon thread, once."""
        with self._load_lock:
            if self._load_started:
                return
            self._load_started = True
        threading.Thread(target=self._load_sync, name="decider-load", daemon=True).start()

    def _load_sync(self) -> None:
        try:
            snap = Path(self.settings.snapshot_path)
            if not snap.is_dir():
                raise FileNotFoundError(f"decider snapshot not found: {snap}")
            if self.settings.offline:
                os.environ["HF_HUB_OFFLINE"] = "1"
            # sys.path: snapshot root (inference.py) + autojev sources inside it
            if str(snap) not in sys.path:
                sys.path.insert(0, str(snap))
            src = snap / "source" / "src"
            if str(src) not in sys.path:
                sys.path.insert(0, str(src))
            t0 = time.perf_counter()
            from inference import Decider  # the snapshot's own loader

            self._decider = Decider.from_pretrained(str(snap), device=self.settings.device)
            from autojev.model import answer as autojev_answer

            self._autojev_answer = autojev_answer
            print(
                f"[model-host] decider loaded from {snap} in {time.perf_counter()-t0:.1f}s",
                flush=True,
            )
            self._warm_sync()
        except Exception as exc:  # noqa: BLE001 — surface any load failure via /health
            self._load_error = f"{type(exc).__name__}: {exc}"
            print(f"[model-host] LOAD FAILED: {self._load_error}", flush=True)
        finally:
            self._load_done.set()

    async def wait_loaded(self, timeout_s: float = 600.0) -> None:
        if not self._load_started:
            self.start_load()
        loaded = await asyncio.to_thread(self._load_done.wait, timeout_s)
        if not loaded:
            raise ModelNotReady("decider still loading")
        if self._load_error is not None:
            raise ModelNotReady(f"decider load failed: {self._load_error}")

    # ─── warmup ─────────────────────────────────────────────────────────

    def _warm_sync(self) -> dict[str, Any]:
        result = self._decide_sync(
            "Warmup: convoy platform self-test.",
            {
                "self_test": {
                    "type": "noul",
                    "instructions": "Is this a warmup self-test message?",
                }
            },
        )
        return result

    async def warmup(self) -> dict[str, Any]:
        await self.wait_loaded()
        return await asyncio.to_thread(self._warm_sync)

    # ─── queue admission ────────────────────────────────────────────────

    class _Admission:
        def __init__(self, rt: "Runtime") -> None:
            self.rt = rt

        def __enter__(self) -> None:
            with self.rt._queue_lock:
                if self.rt._queue_counter >= self.rt.settings.queue_depth:
                    raise QueueFull()
                self.rt._queue_counter += 1

        def __exit__(self, *exc: object) -> None:
            with self.rt._queue_lock:
                self.rt._queue_counter -= 1

    def admission(self) -> "Runtime._Admission":
        return self._Admission(self)

    # ─── decide ─────────────────────────────────────────────────────────

    def _build_rows(
        self, state: str, questions: dict[str, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for name in questions:  # insertion order preserved
            q = questions[name]
            qtype = q.get("type")
            instructions = q.get("instructions")
            if qtype == "choice":
                raw = q.get("options")
                if raw is None:
                    raise ValueError(f"question '{name}': choice requires options")
                if isinstance(raw, dict):
                    criteria: dict[str, Any] = {
                        str(k): v for k, v in raw.items()
                    }
                elif isinstance(raw, list):
                    criteria = {str(opt): str(opt) for opt in raw}
                else:
                    raise ValueError(f"question '{name}': options must be list or dict")
                if not 1 <= len(criteria) <= MAX_OPTIONS:
                    raise ValueError(
                        f"question '{name}': 1..{MAX_OPTIONS} options required "
                        f"(got {len(criteria)})"
                    )
                question: dict[str, Any] = {"type": "choice", "criteria": criteria}
            elif qtype == "noul":
                question = {"type": "noul"}
            elif qtype == "score":
                scale = q.get("scale")
                if not isinstance(scale, list) or len(scale) < 2:
                    raise ValueError(
                        f"question '{name}': score requires scale as >=2 level descriptions"
                    )
                question = {"type": "score", "criteria": [str(s) for s in scale]}
            else:
                raise ValueError(
                    f"question '{name}': type must be choice|noul|score (got {qtype!r})"
                )
            if instructions is not None:
                question["instructions"] = instructions
            rows.append({"state": state, "question": question})
        return rows

    def _decide_sync(
        self, state: str, questions: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        """One serialized forward pass over all questions (batched)."""
        if self._decider is None or self._autojev_answer is None:
            raise ModelNotReady("decider not loaded")
        rows = self._build_rows(state, questions)
        dm = self._decider.model  # autojev.model.DecisionModel
        t0 = time.perf_counter()
        with self._compute_lock:
            with torch.inference_mode():
                batch = dm.prepare(rows, max_length=self.settings.max_state_tokens)
                logits = dm(batch)
                probs = (
                    (logits / dm.temperature).softmax(-1).cpu().tolist()
                )
        latency_ms = (time.perf_counter() - t0) * 1000.0
        self._latencies_ms.append(latency_ms)
        self._decides_total += 1

        answers: dict[str, Any] = {}
        names = list(questions.keys())
        for name, row, p_full, count in zip(names, rows, probs, batch.counts):
            dist = [float(v) for v in p_full[:count]]
            ans = self._autojev_answer(row["question"], dist)
            if ans["type"] == "noul":
                p_true = float(ans["noul"])
                answers[name] = {
                    "value": p_true,
                    "probability": p_true,
                    "confidence": None,  # autojev noul answers carry no confidence
                    "distribution": {"false": 1.0 - p_true, "true": p_true},
                }
            else:
                distribution = {k: float(v) for k, v in ans["probabilities"].items()}
                if ans["type"] == "choice":
                    value = ans["choice"]
                    probability = distribution[value]
                else:  # score
                    value = float(ans["score"])
                    probability = max(distribution.values())
                answers[name] = {
                    "value": value,
                    "probability": probability,
                    "confidence": float(ans["confidence"]),
                    "distribution": distribution,
                }
        return {
            "answers": answers,
            "state_tokens": int(batch.input_tokens),
            "latency_ms": round(latency_ms, 3),
        }

    async def decide(
        self, state: str, questions: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        await self.wait_loaded()
        with self.admission():
            try:
                return await asyncio.to_thread(self._decide_sync, state, questions)
            except Exception:
                self._decides_failed += 1
                raise

    # ─── health / metrics ───────────────────────────────────────────────

    @staticmethod
    def _proc_rss_gib() -> float:
        try:
            pages = int(Path("/proc/self/statm").read_text().split()[1])
            return pages * 4096 / (1024 ** 3)
        except Exception:  # noqa: BLE001
            return 0.0

    def p95_latency_ms(self) -> float | None:
        if not self._latencies_ms:
            return None
        ordered = sorted(self._latencies_ms)
        idx = min(len(ordered) - 1, max(0, math.ceil(0.95 * len(ordered)) - 1))
        return round(ordered[idx], 3)

    def cuda_reserved_gib(self) -> float:
        try:
            return round(torch.cuda.memory_reserved() / (1024 ** 3), 3)
        except Exception:  # noqa: BLE001
            return 0.0

    @property
    def loaded(self) -> bool:
        return self._decider is not None and self._load_error is None

    def health(self) -> dict[str, Any]:
        return {
            "loaded": self.loaded,
            "loading": self._load_started and not self._load_done.is_set(),
            "load_error": self._load_error,
            "resident_rss_gib": round(self._proc_rss_gib(), 3),
            "cuda_reserved_gib": self.cuda_reserved_gib(),
            "p95_decide_latency_ms": self.p95_latency_ms(),
            "queue_depth": self._queue_counter,
            "decides_total": self._decides_total,
            "decides_failed": self._decides_failed,
            "model": "pplx-decider-v1-27b",
        }


class QueueFull(Exception):
    """Queue depth exceeded → HTTP 429 + Retry-After (C1)."""
