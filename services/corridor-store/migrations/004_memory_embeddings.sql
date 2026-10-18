-- 004_memory_embeddings.sql
-- Memory spine for the embedding service (T1 MEMORY tier).
--
-- Adds late-interaction (multi-vector) memory storage alongside the existing
-- traffic.segment_embeddings vector(24) legacy store (002_graph_extensions.sql):
--   * traffic.memory_docs              — incident/AAR/decision/scenario memory
--                                        documents carrying BOTH a bge-m3 dense
--                                        vector(1024) for ANN recall AND a
--                                        pplx-embed-v2-late token-vector array
--                                        vector(128)[] for MaxSim rerank.
--   * traffic.incidents                — incident/pattern memory populated from
--                                        live incidents AND simulation scenario
--                                        runs (soft link to simulation.runs,
--                                        which migration 006 creates later —
--                                        no FK by ordering contract C12).
--   * traffic.memory_segment_embeddings — new home for model-derived segment
--                                        embeddings (e.g. DSTGAT GAT layer),
--                                        fixing the C11 defect where
--                                        pattern_type='gat_l3' violated the
--                                        traffic.segment_embeddings CHECK
--                                        constraint (002:17-19).
--
-- Idempotent and additive-only (C12): no existing table or column is altered
-- or dropped. Applied by VVIP/scripts/migrate.py lexically after 001-003.

-- =============================================================================
-- TRAFFIC SCHEMA: Memory documents (hybrid dense + late-interaction)
-- =============================================================================

CREATE TABLE IF NOT EXISTS traffic.memory_docs (
    doc_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind             TEXT NOT NULL CHECK (kind IN (
                         'incident', 'aar', 'decision', 'scenario_run', 'pattern'
                     )),
    -- External reference (incident_id, scenario_id, ...) — upsert key
    ref_id           TEXT NOT NULL DEFAULT '',
    title            TEXT NOT NULL,
    body             TEXT NOT NULL,
    metadata         JSONB NOT NULL DEFAULT '{}',
    -- bge-m3 dense embedding for ANN recall (unit-norm, cosine distance)
    dense_embedding  vector(1024),
    -- pplx-embed-v2-late per-token vectors for MaxSim rerank, capped at 64
    -- token vectors per document (64 x 128 fp16 token-vecs/doc per perf plan)
    token_vectors    vector(128)[],
    token_count      SMALLINT CHECK (token_count IS NULL OR token_count BETWEEN 1 AND 64),
    -- Soft reference to convoy.movements (no FK: memory may outlive movements)
    movement_id      UUID,
    occurred_at      TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (kind, ref_id),
    CHECK (token_vectors IS NULL
           OR (array_length(token_vectors, 1) BETWEEN 1 AND 64))
);

CREATE INDEX IF NOT EXISTS idx_memory_docs_dense_hnsw
    ON traffic.memory_docs
    USING hnsw (dense_embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE INDEX IF NOT EXISTS idx_memory_docs_kind_time
    ON traffic.memory_docs (kind, occurred_at DESC);

CREATE INDEX IF NOT EXISTS idx_memory_docs_movement
    ON traffic.memory_docs (movement_id)
    WHERE movement_id IS NOT NULL;

-- =============================================================================
-- TRAFFIC SCHEMA: Incident / pattern memory
-- =============================================================================
-- Rows arrive from two sources:
--   source='live'         — incident_detected events pushed by the brain (C3)
--   source='scenario_run' — outcome records derived from the simulation's
--                           scenario runs (evaluate/scenario_sim.py
--                           ScenarioResult shape, simulation.runs link)

CREATE TABLE IF NOT EXISTS traffic.incidents (
    incident_id      BIGSERIAL PRIMARY KEY,
    incident_kind    TEXT NOT NULL CHECK (incident_kind IN (
                         'congestion_spike', 'route_blockage', 'signal_failure',
                         'protest', 'accident', 'weather', 'vvip_hold', 'other'
                     )),
    severity         TEXT NOT NULL DEFAULT 'medium' CHECK (severity IN (
                         'low', 'medium', 'high', 'critical'
                     )),
    movement_id      UUID,
    segment_ids      BIGINT[],
    summary          TEXT NOT NULL,
    details          JSONB NOT NULL DEFAULT '{}',
    -- What actually happened: resolution, delays, decisions taken (AAR input)
    outcome          JSONB,
    -- Soft link to simulation.runs.run_id (migration 006) with no FK,
    -- because 004 applies before 006 exists and C12 pins additive-only.
    scenario_run_id  BIGINT,
    source           TEXT NOT NULL DEFAULT 'live' CHECK (source IN (
                         'live', 'scenario_run', 'import'
                     )),
    -- Set once the memory doc (with embeddings) has been written
    memory_doc_id    UUID,
    occurred_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_incidents_time
    ON traffic.incidents (occurred_at DESC);

CREATE INDEX IF NOT EXISTS idx_incidents_kind
    ON traffic.incidents (incident_kind);

CREATE INDEX IF NOT EXISTS idx_incidents_movement
    ON traffic.incidents (movement_id)
    WHERE movement_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_incidents_segments
    ON traffic.incidents USING GIN (segment_ids);

-- =============================================================================
-- TRAFFIC SCHEMA: Model-derived segment embeddings (C11(b) fix)
-- =============================================================================
-- DSTGAT GAT-layer embeddings previously attempted to insert into
-- traffic.segment_embeddings with pattern_type='gat_l3', which the CHECK
-- constraint in 002 rejects. Model-derived embeddings get their own table
-- keyed by model name. The four allowed pattern_types in
-- traffic.segment_embeddings remain reserved for traffic-profile embeddings.

CREATE TABLE IF NOT EXISTS traffic.memory_segment_embeddings (
    segment_id    BIGINT NOT NULL REFERENCES corridor.road_segments(segment_id),
    model_name    TEXT NOT NULL,
    -- DSTGAT GAT hidden dimension is 64 (dstgat.py DSTGATConfig.hidden_dim)
    embedding     vector(64) NOT NULL,
    computed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (segment_id, model_name)
);

CREATE INDEX IF NOT EXISTS idx_memory_segment_embeddings_hnsw
    ON traffic.memory_segment_embeddings
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
