"""Encode-shape checks against fakes (no live model).

The geometry contract mirrors the real encoders verified on this machine
(model manifest): pplx-embed-v2-late → per-input token sets of shape
(n_tokens, 128); bge-m3 → (N, 1024) unit-norm. Shared validators enforce
exactly this for both real and fake encoders.
"""

from __future__ import annotations

import pytest

from embedding_service import config
from embedding_service.encoders import (
    EncodingError,
    FakeDenseEncoder,
    FakeLateInteractionEncoder,
    PplxLateInteractionEncoder,
    ScriptedEncoder,
    validate_dense_vector,
    validate_token_vectors,
)


class TestTokenVectorGeometry:
    def test_valid_token_set_shape(self):
        tokens = [[0.1] * config.TOKEN_VEC_DIM for _ in range(7)]
        out = validate_token_vectors(tokens)
        assert len(out) == 7
        assert all(len(t) == config.TOKEN_VEC_DIM for t in out)

    def test_empty_token_set_rejected(self):
        with pytest.raises(EncodingError):
            validate_token_vectors([])

    def test_wrong_dim_rejected(self):
        with pytest.raises(EncodingError, match="dims"):
            validate_token_vectors([[0.1] * 64])

    def test_over_cap_rejected(self):
        too_many = [[0.0] * config.TOKEN_VEC_DIM] * (config.MAX_TOKEN_VECS_PER_DOC + 1)
        with pytest.raises(EncodingError, match="cap"):
            validate_token_vectors(too_many)

    def test_non_finite_rejected(self):
        bad = [[0.0] * config.TOKEN_VEC_DIM]
        bad[0][3] = float("nan")
        with pytest.raises(EncodingError, match="finite"):
            validate_token_vectors(bad)

    def test_cap_exactly_64_accepted(self):
        exact = [[0.01] * config.TOKEN_VEC_DIM] * config.MAX_TOKEN_VECS_PER_DOC
        assert len(validate_token_vectors(exact)) == config.MAX_TOKEN_VECS_PER_DOC


class TestDenseGeometry:
    def test_valid_dense(self):
        vec = [0.001] * config.DENSE_DIM
        out = validate_dense_vector(vec)
        assert len(out) == config.DENSE_DIM

    def test_wrong_dim_rejected(self):
        with pytest.raises(EncodingError, match="dims"):
            validate_dense_vector([0.1] * 768)


class TestFakeLateEncoder:
    def test_query_shape_matches_pplx_contract(self):
        enc = FakeLateInteractionEncoder()
        q = enc.encode_query("protest closure on ring road")
        assert 1 <= len(q) <= config.MAX_TOKEN_VECS_PER_DOC
        assert all(len(t) == config.TOKEN_VEC_DIM for t in q)

    def test_document_shapes_match_pplx_contract(self):
        enc = FakeLateInteractionEncoder()
        docs = enc.encode_documents(["short text", "a " * 200])
        assert len(docs) == 2
        for tokens in docs:
            assert 1 <= len(tokens) <= config.MAX_TOKEN_VECS_PER_DOC
            assert all(len(t) == config.TOKEN_VEC_DIM for t in tokens)

    def test_deterministic(self):
        enc_a = FakeLateInteractionEncoder(salt="s")
        enc_b = FakeLateInteractionEncoder(salt="s")
        assert enc_a.encode_query("same text") == enc_b.encode_query("same text")
        assert enc_a.encode_query("text a") != enc_a.encode_query("text b")

    def test_real_encoder_class_validates_truncation_only(self):
        # The real class can't load a model here; assert its construction is
        # cheap (lazy load) so importing it never touches torch.
        enc = PplxLateInteractionEncoder()
        assert enc.name == "pplx-embed-v2-late"
        assert enc._model is None


class TestScriptedEncoder:
    def test_injected_vectors_win_and_are_validated(self):
        good = [[0.5] * config.TOKEN_VEC_DIM]
        enc = ScriptedEncoder({"target doc": good})
        out = enc.encode_documents(["target doc body", "unrelated text"])
        assert out[0] == good
        assert all(len(t) == config.TOKEN_VEC_DIM for t in out[1])

    def test_bad_injected_shape_raises(self):
        enc = ScriptedEncoder({"target": [[0.1] * 10]})
        with pytest.raises(EncodingError):
            enc.encode_query("target")


class TestFakeDenseEncoder:
    def test_shape_and_unit_norm(self):
        enc = FakeDenseEncoder()
        out = enc.encode(["alpha", "beta gamma"])
        assert len(out) == 2
        for vec in out:
            assert len(vec) == config.DENSE_DIM
            norm = sum(v * v for v in vec) ** 0.5
            assert abs(norm - 1.0) < 1e-3

    def test_deterministic(self):
        a, b = FakeDenseEncoder(), FakeDenseEncoder()
        assert a.encode(["x"]) == b.encode(["x"])
