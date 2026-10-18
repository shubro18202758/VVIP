"""Encoder abstractions: late-interaction primary + dense auxiliary.

Real encoders wrap the models verified on this machine (manifest):

* pplx-embed-v2-late-0.6b — ``sentence_transformers.MultiVectorEncoder``;
  ``encode_query``/``encode_document`` return per-input tensors of shape
  ``(n_tokens, 128)``; similarity is MaxSim (see maxsim.py). Text-only and
  image-only batches must be encoded separately (model-card constraint); this
  service embeds text only.
* bge-m3 — local SentenceTransformer snapshot at
  ``/home/sandy050405/models/bge-m3``; encode → ``(N, 1024)`` float32
  unit-norm.

Heavy imports (sentence_transformers / torch) happen lazily inside
``_load_model`` so this module — and every test of it — runs without them.
Shape validation is shared by real and fake encoders, which is what the
encode-shape tests exercise.
"""

from __future__ import annotations

import hashlib
from typing import Protocol, Sequence

from . import config


class EncodingError(ValueError):
    """Raised when encoder output violates the embedding geometry contract."""


TokenVecs = list[list[float]]
"""One document's late-interaction representation: n_tokens × 128 floats."""


def validate_token_vectors(
    tokens: Sequence[Sequence[float]],
    *,
    dim: int = config.TOKEN_VEC_DIM,
    max_tokens: int = config.MAX_TOKEN_VECS_PER_DOC,
    allow_empty: bool = False,
) -> TokenVecs:
    """Validate one document's token-vector set; return it as plain lists.

    Enforces the storage contract mirrored by traffic.memory_docs:
    1..max_tokens vectors, each exactly ``dim`` wide, finite floats.
    """
    n = len(tokens)
    if n == 0:
        if allow_empty:
            return []
        raise EncodingError("token vector set must not be empty")
    if n > max_tokens:
        raise EncodingError(
            f"{n} token vectors exceeds cap {max_tokens}"
        )
    out: TokenVecs = []
    for i, vec in enumerate(tokens):
        if len(vec) != dim:
            raise EncodingError(
                f"token {i} has {len(vec)} dims, expected {dim}"
            )
        row = [float(v) for v in vec]
        for j, v in enumerate(row):
            if v != v or v in (float("inf"), float("-inf")):  # NaN/inf guard
                raise EncodingError(f"token {i} dim {j} is not finite")
        out.append(row)
    return out


def validate_dense_vector(
    vec: Sequence[float],
    *,
    dim: int = config.DENSE_DIM,
) -> list[float]:
    """Validate one document's dense (bge-m3) vector: exactly ``dim`` wide."""
    if len(vec) != dim:
        raise EncodingError(f"dense vector has {len(vec)} dims, expected {dim}")
    out = [float(v) for v in vec]
    for i, v in enumerate(out):
        if v != v or v in (float("inf"), float("-inf")):
            raise EncodingError(f"dense dim {i} is not finite")
    return out


def truncate_for_embedding(text: str, max_chars: int = config.MAX_BODY_CHARS) -> str:
    """Bound document length before it reaches an encoder or the store."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars]


class LateInteractionEncoder(Protocol):
    """Primary encoder: pplx-embed-v2-late multi-vector + MaxSim."""

    name: str

    def encode_query(self, text: str) -> TokenVecs: ...

    def encode_documents(self, texts: Sequence[str]) -> list[TokenVecs]: ...


class DenseEncoderProtocol(Protocol):
    """Auxiliary encoder: bge-m3 dense vectors for ANN recall."""

    name: str
    dim: int

    def encode(self, texts: Sequence[str]) -> list[list[float]]: ...


# ─── Real encoders (lazy heavy imports) ───────────────────────────────────────


class PplxLateInteractionEncoder:
    """pplx-embed-v2-late wrapper producing validated token-vector sets.

    Loads ``sentence_transformers.MultiVectorEncoder`` on first use inside
    the model venv (torch==2.14.0 / sentence-transformers>=6.1.0 — see the
    model manifest). Device defaults to cuda, CPU fallback supported.
    """

    def __init__(self, model_id: str = config.LATE_MODEL_ID, device: str = "cuda"):
        self.name = "pplx-embed-v2-late"
        self._model_id = model_id
        self._device = device
        self._model = None

    def _load_model(self):
        if self._model is None:
            # Lazy import: keeps this module importable without torch.
            from sentence_transformers import MultiVectorEncoder  # type: ignore

            self._model = MultiVectorEncoder(self._model_id, device=self._device)
        return self._model

    def encode_query(self, text: str) -> TokenVecs:
        model = self._load_model()
        tensors = model.encode_query([truncate_for_embedding(text)])
        tokens = [t.tolist() for t in tensors[0]]
        # A query may exceed the cap in principle; storage caps docs, and
        # MaxSim only needs the query's own vectors — but bound it anyway to
        # keep worst-case rerank work fixed.
        if len(tokens) > config.MAX_TOKEN_VECS_PER_DOC:
            tokens = tokens[: config.MAX_TOKEN_VECS_PER_DOC]
        return validate_token_vectors(tokens)

    def encode_documents(self, texts: Sequence[str]) -> list[TokenVecs]:
        model = self._load_model()
        bounded = [truncate_for_embedding(t) for t in texts]
        per_doc_tensors = model.encode_document(bounded)
        out: list[TokenVecs] = []
        for doc_tensors in per_doc_tensors:
            tokens = [t.tolist() for t in doc_tensors]
            if len(tokens) > config.MAX_TOKEN_VECS_PER_DOC:
                tokens = tokens[: config.MAX_TOKEN_VECS_PER_DOC]
            out.append(validate_token_vectors(tokens))
        return out


class BgeM3DenseEncoder:
    """bge-m3 wrapper producing validated unit-norm dense vectors."""

    def __init__(self, model_path: str = config.DENSE_MODEL_PATH, device: str = "cuda"):
        self.name = "bge-m3"
        self.dim = config.DENSE_DIM
        self._model_path = model_path
        self._device = device
        self._model = None

    def _load_model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer  # type: ignore

            self._model = SentenceTransformer(self._model_path, device=self._device)
        return self._model

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        model = self._load_model()
        bounded = [truncate_for_embedding(t) for t in texts]
        arr = model.encode(bounded)
        return [validate_dense_vector(row.tolist()) for row in arr]


# ─── Deterministic fakes (tests + offline development) ────────────────────────


def _stable_pseudovector(seed_text: str, dim: int, salt: str) -> list[float]:
    """Deterministic bounded pseudo-vector keyed by (seed_text, salt).

    Same input text → same vector; disjoint texts → uncorrelated-ish
    vectors. Never used in production paths.
    """
    values: list[float] = []
    counter = 0
    while len(values) < dim:
        block = hashlib.sha256(
            f"{salt}:{seed_text}:{counter}".encode("utf-8")
        ).digest()
        for byte in block:
            values.append((byte / 127.5) - 1.0)  # [-1, 1)
            if len(values) == dim:
                break
        counter += 1
    return values


class FakeLateInteractionEncoder:
    """Deterministic late-interaction fake for tests.

    Each token of the text contributes one 128-d pseudo-vector built from
    ``salt:token``; a ``extra_vectors`` hook lets tests inject crafted
    vectors for exact ranking control. Output always satisfies the geometry
    contract enforced by validate_token_vectors.
    """

    def __init__(self, salt: str = "fake-late"):
        self.name = "fake-late"
        self._salt = salt

    def _tokens_for(self, text: str) -> list[str]:
        return text.split()[: config.MAX_TOKEN_VECS_PER_DOC]

    def encode_query(self, text: str) -> TokenVecs:
        toks = self._tokens_for(text)
        return validate_token_vectors(
            [_stable_pseudovector(t, config.TOKEN_VEC_DIM, self._salt) for t in toks]
        )

    def encode_documents(self, texts: Sequence[str]) -> list[TokenVecs]:
        return [self.encode_query(t) for t in texts]


class ScriptedEncoder:
    """Returns exactly the vectors a test supplies, after validation.

    ``vectors_by_prefix`` maps a text prefix (or full text) to its token
    vector set; unknown texts fall back to the fake late encoder behaviour.
    Lets retrieval tests assert exact MaxSim orderings without a model.
    """

    def __init__(
        self,
        vectors_by_prefix: dict[str, TokenVecs],
        fallback: LateInteractionEncoder | None = None,
    ):
        self.name = "scripted"
        self._map = vectors_by_prefix
        self._fallback = fallback or FakeLateInteractionEncoder()

    def _lookup(self, text: str) -> TokenVecs:
        for prefix, vecs in self._map.items():
            if text.startswith(prefix):
                return vecs
        return self._fallback.encode_query(text)

    def encode_query(self, text: str) -> TokenVecs:
        return validate_token_vectors(self._lookup(text))

    def encode_documents(self, texts: Sequence[str]) -> list[TokenVecs]:
        return [self.encode_query(t) for t in texts]


class FakeDenseEncoder:
    """Deterministic dense fake: one 1024-d pseudo-vector per text word-mean."""

    def __init__(self, salt: str = "fake-dense"):
        self.name = "fake-dense"
        self.dim = config.DENSE_DIM
        self._salt = salt

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        out = []
        for text in texts:
            words = text.split() or ["empty"]
            rows = [
                _stable_pseudovector(w, config.DENSE_DIM, self._salt) for w in words
            ]
            mean = [sum(col) / len(rows) for col in zip(*rows)]
            # Unit-normalise like bge-m3 output.
            norm = sum(v * v for v in mean) ** 0.5 or 1.0
            out.append(validate_dense_vector([v / norm for v in mean]))
        return out


__all__ = [
    "BgeM3DenseEncoder",
    "DenseEncoderProtocol",
    "EncodingError",
    "FakeDenseEncoder",
    "FakeLateInteractionEncoder",
    "LateInteractionEncoder",
    "PplxLateInteractionEncoder",
    "ScriptedEncoder",
    "TokenVecs",
    "truncate_for_embedding",
    "validate_dense_vector",
    "validate_token_vectors",
]
