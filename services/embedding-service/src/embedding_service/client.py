"""Brain-facing HTTP client for the embedding service.

The brain (convoy-brain stream) calls this for "similar past situations".
The base URL is the platform's fixed internal endpoint (config constant
EMBEDDING_SERVICE_URL, default http://127.0.0.1:8085, env-overridable at
process start per contract C0) and is validated through the internal
allowlist path in security.py. No URL is ever taken from request payloads.

The retrieval contract (also in README.md):

    client.similar_past_situations("congestion spike on ring road after
    protest") → POST /search with kinds=['incident','scenario_run','aar']
    → {"results": [{doc_id, kind, ref_id, title, snippet, score,
                    maxsim_score, dense_score, strategy, token_count,
                    occurred_at, metadata}], "query_token_count",
       "recalled", "latency_ms"}
"""

from __future__ import annotations

from . import config
from .retrieval import DEFAULT_SEARCH_KINDS
from .security import fetch_json, validate_internal_url


class EmbeddingServiceClient:
    """Validated HTTP client over the fixed embedding-service endpoint."""

    def __init__(
        self,
        base_url: str = config.EMBEDDING_SERVICE_URL,
        timeout_sec: float = 15.0,
    ) -> None:
        # Fixed internal endpoint: allowlist-validated, never external input.
        self._base = validate_internal_url(base_url).rstrip("/")
        self._timeout = timeout_sec

    @property
    def base_url(self) -> str:
        return self._base

    async def health(self) -> dict:
        return await fetch_json(
            f"{self._base}/health", timeout_sec=self._timeout, internal=True
        )

    async def embed_documents(
        self, documents: list[dict], *, kind: str = "pattern"
    ) -> dict:
        return await fetch_json(
            f"{self._base}/embed/documents",
            method="POST",
            payload={"documents": documents, "kind": kind},
            timeout_sec=self._timeout,
            internal=True,
        )

    async def search(
        self,
        query: str,
        *,
        kinds: list[str] | None = None,
        top_k: int | None = None,
        recall_k: int | None = None,
        movement_id: str | None = None,
    ) -> dict:
        payload: dict = {"query": query}
        if kinds is not None:
            payload["kinds"] = kinds
        if top_k is not None:
            payload["top_k"] = top_k
        if recall_k is not None:
            payload["recall_k"] = recall_k
        if movement_id is not None:
            payload["movement_id"] = movement_id
        return await fetch_json(
            f"{self._base}/search",
            method="POST",
            payload=payload,
            timeout_sec=self._timeout,
            internal=True,
        )

    async def similar_past_situations(
        self, situation: str, *, top_k: int = config.DEFAULT_TOP_K
    ) -> dict:
        """The named contract: retrieve similar past situations by name."""
        return await self.search(
            situation, kinds=list(DEFAULT_SEARCH_KINDS), top_k=top_k
        )

    async def record_incident(self, incident: dict) -> dict:
        return await fetch_json(
            f"{self._base}/incidents",
            method="POST",
            payload=incident,
            timeout_sec=self._timeout,
            internal=True,
        )

    async def record_scenario_run(
        self, scenario_result: dict, **context: object
    ) -> dict:
        """Persist a ScenarioResult-shaped dict from the simulation."""
        payload = {"scenario_run": scenario_result, **context}
        return await fetch_json(
            f"{self._base}/incidents",
            method="POST",
            payload=payload,
            timeout_sec=self._timeout,
            internal=True,
        )

    async def recent_incidents(
        self, *, limit: int = 20, kind: str | None = None
    ) -> dict:
        params = f"limit={limit}"
        if kind:
            params += f"&kind={kind}"
        return await fetch_json(
            f"{self._base}/incidents/recent?{params}",
            timeout_sec=self._timeout,
            internal=True,
        )


__all__ = ["EmbeddingServiceClient"]
