-- 002_graph_extensions.sql
-- Enable pgRouting and pgvector extensions, create segment embeddings
-- and routing view for the corridor spatial-graph database.

-- Graph traversal: Dijkstra, K-shortest paths, driving distance isochrones
CREATE EXTENSION IF NOT EXISTS pgrouting;

-- Vector similarity: segment traffic pattern embeddings (HNSW index)
CREATE EXTENSION IF NOT EXISTS vector;

-- =============================================================================
-- TRAFFIC SCHEMA: Segment pattern embeddings for similarity search
-- =============================================================================

CREATE TABLE traffic.segment_embeddings (
    segment_id      BIGINT NOT NULL REFERENCES corridor.road_segments(segment_id),
    pattern_type    TEXT NOT NULL CHECK (pattern_type IN (
        'daily_profile', 'weekly_profile', 'event_response', 'incident_response'
    )),
    embedding       vector(24) NOT NULL,
    computed_at     TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (segment_id, pattern_type)
);

-- HNSW index for fast approximate nearest-neighbor queries on embeddings
CREATE INDEX idx_segment_embeddings_hnsw
    ON traffic.segment_embeddings
    USING hnsw (embedding vector_l2_ops)
    WITH (m = 16, ef_construction = 64);

-- =============================================================================
-- CORRIDOR SCHEMA: pgRouting-compatible road graph view
-- =============================================================================
-- This view presents the segment_adjacency table in the format required by
-- pgr_dijkstra, pgr_KSP, and pgr_drivingDistance.
--
-- Cost formula: turn_cost_sec + segment_length_m / (speed_limit_m_per_sec)
-- reverse_cost = -1 for oneway segments (no traversal in reverse direction)

CREATE OR REPLACE VIEW corridor.road_graph AS
SELECT
    sa.from_segment_id * 1000000 + sa.to_segment_id AS id,
    sa.from_segment_id                               AS source,
    sa.to_segment_id                                 AS target,
    sa.turn_cost_sec + (
        ST_Length(rs.geom::geography) / NULLIF(rs.speed_limit_kmh * 1000.0 / 3600.0, 0)
    )                                                AS cost,
    CASE
        WHEN rs.oneway THEN -1.0
        ELSE sa.turn_cost_sec + (
            ST_Length(rs.geom::geography) / NULLIF(rs.speed_limit_kmh * 1000.0 / 3600.0, 0)
        )
    END                                              AS reverse_cost
FROM corridor.segment_adjacency sa
JOIN corridor.road_segments rs ON rs.segment_id = sa.from_segment_id;
