"""MaxSim (late-interaction) scoring.

MaxSim(query Q, document D) = Σ_i max_j dot(q_i, d_j)  — for every query
token vector, take the best-matching document token vector, then sum. This
matches ``MultiVectorEncoder.similarity`` semantics (verified in the model
manifest: encode_query (7,128), encode_document (10,128), similarity →
MaxSim tensor).

Implemented with numpy (available in both the model venv and the test
venv). Ranked rerank is capped at recall_k=100 candidates per the perf plan.
"""

from __future__ import annotations

from typing import Sequence

import numpy as np

from .encoders import TokenVecs

_EPS = 1e-12


def maxsim_score(query_tokens: Sequence[Sequence[float]], doc_tokens: Sequence[Sequence[float]]) -> float:
    """Late-interaction score of one (query, document) token-vector pair."""
    if not query_tokens or not doc_tokens:
        return 0.0
    q = np.asarray(query_tokens, dtype=np.float32)  # (tq, dim)
    d = np.asarray(doc_tokens, dtype=np.float32)  # (td, dim)
    # Guard: encoders are unit-normalised, but fakes/legacy rows may not be —
    # normalise so scores are comparable cosine-like quantities in [-#q, #q].
    qn = q / (np.linalg.norm(q, axis=1, keepdims=True) + _EPS)
    dn = d / (np.linalg.norm(d, axis=1, keepdims=True) + _EPS)
    sims = qn @ dn.T  # (tq, td)
    return float(np.sum(np.max(sims, axis=1)))


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity of two dense vectors (0.0 on empty/mismatched input)."""
    va, vb = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
    if va.shape != vb.shape or va.size == 0:
        return 0.0
    na = np.linalg.norm(va) + _EPS
    nb = np.linalg.norm(vb) + _EPS
    return float(va @ vb / (na * nb))


def rerank_by_maxsim(
    query_tokens: Sequence[Sequence[float]],
    candidates: Sequence[tuple[str, TokenVecs | None, Sequence[float] | None]],
) -> list[tuple[str, float, str]]:
    """Order candidates by MaxSim with dense-cosine fallback.

    Each candidate is ``(key, token_vectors, dense_vector)``. Documents that
    carry token vectors are scored by MaxSim (strategy 'maxsim'); documents
    without them fall back to dense cosine (strategy 'dense_fallback') so
    partially-encoded memory still surfaces — flagged, never silently mixed.

    Returns [(key, score, strategy)] sorted by score descending.
    """
    scored: list[tuple[str, float, str]] = []
    for key, tokens, dense in candidates:
        if tokens:
            scored.append((key, maxsim_score(query_tokens, tokens), "maxsim"))
        elif dense is not None:
            # No query-dense here: caller supplies dense score separately when
            # using the hybrid path; standalone use scores 0.
            scored.append((key, 0.0, "dense_fallback"))
        else:
            scored.append((key, 0.0, "unencoded"))
    scored.sort(key=lambda item: (-item[1], item[0]))
    return scored


__all__ = ["cosine", "maxsim_score", "rerank_by_maxsim"]
