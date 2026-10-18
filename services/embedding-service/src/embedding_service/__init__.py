"""Embedding-service package: late-interaction memory for the Veer corridor stack.

T1 MEMORY tier — pplx-embed-v2-late multi-vector MaxSim rerank with bge-m3
dense ANN recall, backed by traffic.memory_docs / traffic.incidents
(migration corridor-store 004).

Core modules (``security``, ``config``, ``encoders``, ``maxsim``, ``store``,
``retrieval``, ``incidents``, ``api``, ``client``) import only the standard
library plus ``numpy`` at module top; FastAPI/asyncpg/sentence-transformers
are imported lazily so the whole package is testable against fakes without a
live model, database, or HTTP server.
"""

from __future__ import annotations

__all__ = ["config", "security", "encoders", "maxsim", "store", "retrieval", "incidents"]
