# embedding-service — T1 MEMORY

Late-interaction memory for the Veer corridor stack: **pplx-embed-v2-late**
multi-vector embeddings with **MaxSim** rerank, **bge-m3** dense ANN recall,
and an incident/pattern memory populated from live incidents and the
simulation's scenario runs.

Storage lives in `traffic.memory_docs` / `traffic.incidents` /
`traffic.memory_segment_embeddings` (corridor-store migration
`004_memory_embeddings.sql`; DOWN in this repo at
`migrations/004_memory_embeddings_down.sql` — kept here so the models-owned
lexical runner in `corridor-store/migrations/` never applies it as an up).

## Endpoints (architecture contract C3)

| Method | Path                | Purpose |
|-------|----------------------|---------|
| POST  | `/embed/documents`   | encode + upsert memory docs (late + dense) |
| POST  | `/search`            | hybrid: dense ANN `recall_k=100` → MaxSim rerank `top_k=20` |
| POST  | `/incidents`         | record a live incident OR a simulation scenario run |
| GET   | `/incidents/recent`  | recent incident/pattern memory (`?limit=&kind=`) |
| GET   | `/health`            | heartbeat |

## The retrieval contract — "similar past situations"

The brain calls **`POST /search`** (or the client's
`similar_past_situations()` wrapper) whenever it needs analogous history:

```jsonc
// request
{
  "query": "congestion spike on ring road after protest closure",
  "kinds": ["incident", "scenario_run", "aar"],  // default for past situations
  "top_k": 20,            // capped at 50
  "recall_k": 100,        // dense ANN candidate pool (capped at 100)
  "movement_id": "uuid?"  // optional soft preference for same-movement history
}

// response
{
  "query": "...",
  "query_token_count": 7,       // pplx-embed-v2-late query tokens
  "recalled": 42,               // docs the dense pass surfaced
  "latency_ms": 12.4,
  "results": [
    {
      "doc_id": "uuid",
      "kind": "scenario_run",           // incident | aar | decision | scenario_run | pattern
      "ref_id": "scenario:sc-042",
      "title": "Scenario run sc-042: Sardar Patel Marg",
      "snippet": "Simulation scenario run sc-042 ...",
      "score": 4.19,                    // MaxSim (or dense cosine on fallback)
      "maxsim_score": 4.19,             // null when strategy=dense_fallback
      "dense_score": 0.71,
      "strategy": "maxsim",             // maxsim | dense_fallback
      "token_count": 23,
      "occurred_at": "2026-10-12T09:00:00+00:00",
      "metadata": {"scenario_id": "sc-042", "outcome": {"total_disruption_vehicle_hours": 12.5}}
    }
  ]
}
```

Python usage from the brain (the client validates its base URL against the
platform's fixed-internal allowlist — see `security.py`):

```python
from embedding_service.client import EmbeddingServiceClient

client = EmbeddingServiceClient()  # config.EMBEDDING_SERVICE_URL (env-overridable)
hits = await client.similar_past_situations("protest closure near overpass")
await client.record_scenario_run(scenario_result)  # frozen ScenarioResult shape
```

## Populating memory from simulation scenario runs

`POST /incidents` accepts either:

* a live incident: `{"incident_kind": "protest", "severity": "high",
  "summary": "...", "segment_ids": [...], "movement_id": "..."}`; or
* a scenario run: `{"scenario_run": {<frozen ScenarioResult fields>},
  "movement_id": "...", "scenario_run_id": 52}` — the exact shape
  traffic-oracle's `evaluate/scenario_sim.py` returns
  (`scenario_id, route_name, total_disruption_vehicle_hours,
  max_queue_length_m, avg_closure_duration_sec, segments_affected,
  estimated_complaints_risk, convoy_transit_time_sec`). The result is
  stored as `traffic.incidents` (source=`scenario_run`, soft link
  `scenario_run_id` → `simulation.runs` from migration 006, no FK by the
  C12 additive-only ordering) AND as an embedded `scenario_run` memory doc,
  so twin simulation history becomes retrievable as past situations.

## Embedding geometry

| Encoder | Model | Output | Stored as |
|---|---|---|---|
| late (primary) | `perplexity-ai/pplx-embed-v2-late-0.6b` | `(n_tokens, 128)` per doc | `traffic.memory_docs.token_vectors vector(128)[]`, ≤ 64 tokens/doc |
| dense (aux) | bge-m3 local snapshot (`/home/sandy050405/models/bge-m3`) | `(1024,)` unit-norm | `traffic.memory_docs.dense_embedding vector(1024)` + HNSW cosine |

Real encoders load lazily inside the models venv
(`pip install .[models]`; torch==2.14.0 / sentence-transformers>=6.1.0 —
verified on this machine). Everything else (validation, MaxSim, retrieval,
API, codecs) is stdlib + numpy and runs anywhere.

## Security posture

* **Outbound HTTP** (`security.py`): http/https only; external-input URLs
  are host-resolved and must land on global unicast addresses (localhost /
  loopback / private / reserved / link-local / multicast rejected);
  redirects are refused; plaintext http is pinned to the validated IP
  (rebinding-proof). Fixed internal endpoints (127.0.0.1:808x,
  host.docker.internal) are compile-time config constants accepted only
  under the internal policy.
* **SQL**: every statement is a parameter-bound constant (`$n`
  placeholders; pgvector values bind as text with explicit casts — a
  `vector(128)[]` parameter must be a LIST of vector literals, not a
  single array-literal string; enforced in `pgvector_codec`).
* **Leak guard**: kind whitelists on both memory docs
  (incident/aar/decision/scenario_run/pattern) and incidents
  (congestion_spike/route_blockage/signal_failure/protest/accident/weather/
  vvip_hold/other); `scrub_text` redacts credential/contact patterns from
  free text before storage. The brain-side scrubber remains the primary
  VVIP-identity guard (plan §risk).
* **Credentials**: none in source; DSN from env
  (`VEER_DATABASE_DSN` / `VVIP_DATABASE_URL` / `DATABASE_URL`).

## Running

```bash
# tests (fakes + disposable pgvector docker container; DB tests skip w/o docker)
scripts/run_tests.sh

# serve (models venv; listens on 127.0.0.1:8085, EMBEDDING_LISTEN_* to change)
VEER_DATABASE_DSN=postgres://... python -m embedding_service
```

## C11 fixes carried by this stream

* `traffic-oracle data/vector_store.py compute_daily_profile` now reads the
  REAL `traffic.hourly_aggregates` columns (hour_utc / avg_speed_kmh /
  p95_congestion / observation_cnt) instead of the fictional
  hour_of_day / avg_congestion_idx / observation_count.
* `predict/training_pipeline.py` no longer writes `pattern_type='gat_l3'`
  into `traffic.segment_embeddings` (CHECK violation, 002:17-19); DSTGAT
  GAT embeddings (full 64-dim, real segment ids) go to
  `traffic.memory_segment_embeddings` via `store_memory_embedding`.
