"""Decodes Arrow IPC bytes into Polars DataFrames."""

from __future__ import annotations

import io

import polars as pl
import pyarrow as pa
import pyarrow.ipc
import structlog

logger = structlog.get_logger()


class ArrowDecoder:
    """Deserializes Arrow IPC streaming bytes to Polars DataFrames."""

    @staticmethod
    def decode(ipc_bytes: bytes) -> pl.DataFrame:
        """Decode Arrow IPC bytes → pyarrow RecordBatch → Polars DataFrame."""
        reader = pa.ipc.open_stream(io.BytesIO(ipc_bytes))
        table = reader.read_all()
        return pl.from_arrow(table)
