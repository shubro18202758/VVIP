"""ONNX Serve — lightweight ONNX Runtime inference server for traffic models.

Provides a thin async wrapper around ONNX Runtime InferenceSession with:
    - Automatic GPU/CPU fallback governed by GpuArbiter
    - Session lazy-loading to avoid VRAM allocation until first request
    - Thread-safe concurrent inference on the same session
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np
import onnxruntime as ort
import structlog

from traffic_oracle.runtime.thread_pool import BoundedMLThreadPool

logger = structlog.get_logger(__name__)


class OnnxServe:
    """Async ONNX Runtime inference wrapper with GPU arbiter integration."""

    def __init__(
        self,
        model_path: str | Path,
        arbiter: object | None = None,
        n_threads: int = 4,
    ) -> None:
        self._model_path = Path(model_path)
        self._arbiter = arbiter
        self._n_threads = n_threads
        self._session: ort.InferenceSession | None = None
        logger.info("onnx_serve.init", model=str(self._model_path))

    def _load_session(self) -> None:
        """Lazy-load ONNX Runtime session with appropriate execution providers."""
        if self._arbiter is not None:
            providers = self._arbiter.get_onnx_providers()
        else:
            providers = ["CPUExecutionProvider"]

        sess_options = ort.SessionOptions()
        sess_options.intra_op_num_threads = self._n_threads
        sess_options.inter_op_num_threads = 2
        sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        self._session = ort.InferenceSession(
            str(self._model_path),
            sess_options=sess_options,
            providers=providers,
        )

        active_provider = self._session.get_providers()[0]
        model_size_mb = self._model_path.stat().st_size / 1_048_576
        input_meta = {i.name: i.shape for i in self._session.get_inputs()}
        output_meta = {o.name: o.shape for o in self._session.get_outputs()}

        logger.info(
            "onnx_serve.session_loaded",
            model=str(self._model_path),
            provider=active_provider,
            model_size_mb=round(model_size_mb, 2),
            inputs=input_meta,
            outputs=output_meta,
            intra_threads=self._n_threads,
        )

    async def infer(self, inputs: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        """Run inference on the loaded ONNX model.

        Args:
            inputs: Dict of input tensor name → numpy array

        Returns:
            Dict of output tensor name → numpy array
        """
        if self._session is None:
            self._load_session()

        session = self._session
        expected = {i.name: i.shape for i in session.get_inputs()}
        for name, shape in expected.items():
            if name not in inputs:
                raise ValueError(f"Missing input '{name}', expected {list(expected)}")
            actual = inputs[name].shape
            for dim_exp, dim_act in zip(shape, actual):
                if isinstance(dim_exp, int) and dim_exp != dim_act:
                    raise ValueError(
                        f"Input '{name}' shape mismatch: expected {shape}, got {actual}"
                    )

        output_names = [o.name for o in session.get_outputs()]
        loop = asyncio.get_running_loop()
        pool = BoundedMLThreadPool.get()

        results = await loop.run_in_executor(
            pool,
            lambda: session.run(output_names, inputs),
        )

        return dict(zip(output_names, results))
