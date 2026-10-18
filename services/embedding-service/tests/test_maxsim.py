"""MaxSim math + retrieval ranking sanity (fakes, exact control).

The scripted encoder injects exact token vectors so rankings are asserted
numerically — no model involved.
"""

from __future__ import annotations

import numpy as np
import pytest

from embedding_service import config
from embedding_service.encoders import FakeLateInteractionEncoder, ScriptedEncoder
from embedding_service.maxsim import cosine, maxsim_score, rerank_by_maxsim


def _unit(vec: list[float]) -> list[float]:
    arr = np.asarray(vec, dtype=np.float64)
    return (arr / np.linalg.norm(arr)).tolist()


def _e(dim: int, axis: int) -> list[float]:
    """Unit basis vector — perfectly aligned with itself, orthogonal else."""
    return [1.0 if i == axis else 0.0 for i in range(dim)]


class TestMaxSimMath:
    def test_identical_documents_score_maximal(self):
        tokens = [_e(config.TOKEN_VEC_DIM, 0), _e(config.TOKEN_VEC_DIM, 1)]
        assert maxsim_score(tokens, tokens) == pytest.approx(2.0, abs=1e-5)

    def test_orthogonal_scores_zero(self):
        q = [_e(config.TOKEN_VEC_DIM, 0)]
        d = [_e(config.TOKEN_VEC_DIM, 1)]
        assert maxsim_score(q, d) == pytest.approx(0.0, abs=1e-6)

    def test_sum_of_token_best_matches(self):
        # q has 3 tokens; doc contains best matches for 2 of them exactly
        # and for the third a 0.5-weighted direction.
        q = [
            _e(config.TOKEN_VEC_DIM, 0),
            _e(config.TOKEN_VEC_DIM, 1),
            _e(config.TOKEN_VEC_DIM, 2),
        ]
        d = [
            _e(config.TOKEN_VEC_DIM, 0),
            _e(config.TOKEN_VEC_DIM, 1),
            _unit([0.5] + [0.0] * (config.TOKEN_VEC_DIM - 1)),
            _e(config.TOKEN_VEC_DIM, 3),  # decoy, never the best match
        ]
        # third query token e2 vs [0.5,0,...]/norm → 0 (e2 has 0 in dim 0)
        assert maxsim_score(q, d) == pytest.approx(2.0, abs=1e-5)

    def test_empty_inputs_score_zero(self):
        assert maxsim_score([], [[0.1] * config.TOKEN_VEC_DIM]) == 0.0
        assert maxsim_score([[0.1] * config.TOKEN_VEC_DIM], []) == 0.0

    def test_cosine_basics(self):
        assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
        assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
        assert cosine([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)

    def test_rerank_orders_by_score(self):
        q = [_e(config.TOKEN_VEC_DIM, 0)]
        strong = [_e(config.TOKEN_VEC_DIM, 0)]
        partial = [  # true 45° to the query token → ~0.707 after normalise
            _unit(
                [0.70710678, 0.70710678] + [0.0] * (config.TOKEN_VEC_DIM - 2)
            )
        ]
        weak = [_e(config.TOKEN_VEC_DIM, 5)]  # orthogonal → 0
        ranked = rerank_by_maxsim(
            q,
            [
                ("weak", weak, None),
                ("strong", strong, None),
                ("partial", partial, None),
                ("none", None, None),
            ],
        )
        # strong(1.0) > partial(≈0.707) > {weak, none} both at 0.0, where the
        # documented tie-break orders by key alphabetically.
        assert [r[0] for r in ranked] == ["strong", "partial", "none", "weak"]
        assert ranked[0][1] == pytest.approx(1.0, abs=1e-5)
        assert 0.0 < ranked[1][1] < 1.0
        assert ranked[0][2] == "maxsim"
        by_key = {key: strategy for key, _score, strategy in ranked}
        assert by_key["weak"] == "maxsim"      # orthogonal but encoded
        assert by_key["none"] == "unencoded"   # no vectors at all


class TestRetrievalRanking:
    """Hybrid pipeline ranking sanity over the in-memory store."""

    async def test_most_similar_doc_ranks_first(self, fake_store, dense_encoder):
        from embedding_service.retrieval import HybridRetriever

        query_text = "protest causing closure on ring road"
        similar_text = "protest closure ring road heavy congestion"
        unrelated_text = "routine night patrol with no traffic impact"

        scripted = ScriptedEncoder(
            {
                query_text: [_e(config.TOKEN_VEC_DIM, 0), _e(config.TOKEN_VEC_DIM, 1)],
                similar_text: [_e(config.TOKEN_VEC_DIM, 0), _e(config.TOKEN_VEC_DIM, 2)],
                unrelated_text: [_e(config.TOKEN_VEC_DIM, 6), _e(config.TOKEN_VEC_DIM, 7)],
            },
            fallback=FakeLateInteractionEncoder(),
        )
        retriever = HybridRetriever(fake_store, scripted, dense_encoder)
        for text in (similar_text, unrelated_text):
            await fake_store.upsert_document(
                kind="incident",
                ref_id=f"ref:{text[:12]}",
                title=text,
                body=text,
                metadata={},
                dense=dense_encoder.encode([text])[0],
                token_vecs=scripted.encode_query(text),
            )

        response = await retriever.similar_past_situations(query_text)
        assert response.results[0].title == similar_text
        assert response.results[0].strategy == "maxsim"
        assert response.results[0].score > response.results[1].score
        assert response.query_token_count == 2

    async def test_maxsim_rerank_overrides_dense_order(
        self, fake_store, dense_encoder
    ):
        """Dense recall ranks A first; MaxSim rerank must promote B."""
        from embedding_service.retrieval import HybridRetriever

        query_text = "monsoon flooding near overpass"
        dense_top_text = "monsoon rains reported across the city"  # dense-friendly
        maxsim_top_text = "flooding overpass submerged underpass waterlogged"  # token-friendly

        q_tokens = [_e(config.TOKEN_VEC_DIM, 0), _e(config.TOKEN_VEC_DIM, 1)]
        scripted = ScriptedEncoder(
            {
                query_text: q_tokens,
                maxsim_top_text: [_e(config.TOKEN_VEC_DIM, 0), _e(config.TOKEN_VEC_DIM, 1)],
                dense_top_text: [_e(config.TOKEN_VEC_DIM, 8)],
            },
            fallback=FakeLateInteractionEncoder(),
        )
        retriever = HybridRetriever(fake_store, scripted, dense_encoder)

        # Craft dense vectors so dense recall puts dense_top_text FIRST.
        q_dense = _unit([1.0] * config.DENSE_DIM)
        dense_friendly = _unit(
            [1.0] * config.DENSE_DIM
        )  # cosine 1.0 with query dense
        token_friendly = _unit(
            [-1.0] + [0.5] * (config.DENSE_DIM - 1)
        )  # lower cosine
        await fake_store.upsert_document(
            kind="incident", ref_id="dense-top", title=dense_top_text,
            body=dense_top_text, metadata={},
            dense=dense_friendly, token_vecs=scripted.encode_query(dense_top_text),
        )
        await fake_store.upsert_document(
            kind="incident", ref_id="maxsim-top", title=maxsim_top_text,
            body=maxsim_top_text, metadata={},
            dense=token_friendly, token_vecs=scripted.encode_query(maxsim_top_text),
        )

        response = await retriever.search(query_text)
        recalled_order = [d.title for d in fake_store.docs.values()]
        assert set(recalled_order) == {dense_top_text, maxsim_top_text}
        # Rerank promoted the MaxSim-matching doc despite worse dense rank.
        assert response.results[0].title == maxsim_top_text
        assert response.results[0].strategy == "maxsim"

    async def test_dense_fallback_flagged_when_tokens_missing(
        self, fake_store, dense_encoder
    ):
        from embedding_service.retrieval import HybridRetriever

        retriever = HybridRetriever(
            fake_store, FakeLateInteractionEncoder(), dense_encoder
        )
        await fake_store.upsert_document(
            kind="pattern", ref_id="no-tokens", title="legacy pattern",
            body="legacy pattern without late vectors",
            metadata={}, dense=dense_encoder.encode(["legacy pattern"])[0],
            token_vecs=None,
        )
        response = await retriever.search("legacy pattern", kinds=["pattern"])
        assert response.results[0].strategy == "dense_fallback"
        assert response.results[0].maxsim_score is None
        assert response.results[0].score == pytest.approx(
            response.results[0].dense_score
        )

    async def test_top_k_and_kinds_filter(self, fake_store, retriever, dense_encoder, late_encoder):
        for i in range(10):
            await fake_store.upsert_document(
                kind="pattern",
                ref_id=f"p{i}",
                title=f"pattern doc {i}",
                body=f"pattern number {i}",
                metadata={},
                dense=dense_encoder.encode([f"pattern number {i}"])[0],
                token_vecs=late_encoder.encode_query(f"pattern number {i}"),
            )
        response = await retriever.search(
            "pattern number 3", top_k=3, kinds=["pattern"]
        )
        assert len(response.results) == 3
        assert all(r.kind == "pattern" for r in response.results)

        with pytest.raises(ValueError, match="not allowed"):
            await retriever.search("query", kinds=["evil_kind"])

    async def test_default_kind_set_is_past_situations(self, retriever):
        from embedding_service.retrieval import DEFAULT_SEARCH_KINDS

        assert set(DEFAULT_SEARCH_KINDS) == {"incident", "scenario_run", "aar"}

    async def test_top_k_capped(self, fake_store, retriever, dense_encoder, late_encoder):
        await fake_store.upsert_document(
            kind="incident", ref_id="x", title="t", body="b", metadata={},
            dense=dense_encoder.encode(["b"])[0],
            token_vecs=late_encoder.encode_query("b"),
        )
        response = await retriever.search("b", top_k=9999)
        assert len(response.results) <= config.MAX_TOP_K
