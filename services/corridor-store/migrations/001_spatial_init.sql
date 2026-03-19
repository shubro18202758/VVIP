-- 001_spatial_init.sql
-- Bootstrap spatial extensions and core schema for corridor traffic storage.
-- Runs automatically on first PostGIS container startup.

-- Enable PostGIS spatial extensions
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS postgis_topology;
CREATE EXTENSION IF NOT EXISTS pg_trgm;  -- trigram index for fuzzy road name search

-- Schema isolation for domain tables
CREATE SCHEMA IF NOT EXISTS corridor;
CREATE SCHEMA IF NOT EXISTS convoy;
CREATE SCHEMA IF NOT EXISTS traffic;

-- =============================================================================
-- CORRIDOR SCHEMA: Road network topology
-- =============================================================================

-- Road segments forming the corridor graph
CREATE TABLE corridor.road_segments (
    segment_id      BIGINT PRIMARY KEY,
    osm_way_id      BIGINT,
    road_name       TEXT,
    road_class      TEXT NOT NULL CHECK (road_class IN (
        'motorway', 'trunk', 'primary', 'secondary', 'tertiary', 'residential', 'service'
    )),
    lanes           SMALLINT DEFAULT 2,
    speed_limit_kmh SMALLINT DEFAULT 50,
    oneway          BOOLEAN DEFAULT FALSE,
    geom            GEOMETRY(LineString, 4326) NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_road_segments_geom ON corridor.road_segments USING GIST (geom);
CREATE INDEX idx_road_segments_class ON corridor.road_segments (road_class);
CREATE INDEX idx_road_segments_name ON corridor.road_segments USING GIN (road_name gin_trgm_ops);

-- Intersections / junctions as nodes in the corridor graph
CREATE TABLE corridor.junctions (
    junction_id     BIGSERIAL PRIMARY KEY,
    junction_type   TEXT DEFAULT 'intersection' CHECK (junction_type IN (
        'intersection', 'roundabout', 'flyover', 'underpass', 'toll_plaza', 'signal'
    )),
    signal_control  BOOLEAN DEFAULT FALSE,
    geom            GEOMETRY(Point, 4326) NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_junctions_geom ON corridor.junctions USING GIST (geom);

-- Adjacency: which segments connect at which junctions
CREATE TABLE corridor.segment_adjacency (
    from_segment_id BIGINT REFERENCES corridor.road_segments(segment_id),
    to_segment_id   BIGINT REFERENCES corridor.road_segments(segment_id),
    via_junction_id BIGINT REFERENCES corridor.junctions(junction_id),
    turn_cost_sec   REAL DEFAULT 0,
    PRIMARY KEY (from_segment_id, to_segment_id, via_junction_id)
);

-- =============================================================================
-- TRAFFIC SCHEMA: Real-time and historical observations
-- =============================================================================

-- Live traffic observations (partitioned by day for efficient retention)
CREATE TABLE traffic.observations (
    observation_id  BIGSERIAL,
    segment_id      BIGINT NOT NULL,
    timestamp_utc   TIMESTAMPTZ NOT NULL,
    speed_kmh       REAL NOT NULL,
    congestion_idx  REAL NOT NULL CHECK (congestion_idx BETWEEN 0.0 AND 1.0),
    source          TEXT NOT NULL CHECK (source IN (
        'government', 'mapping_api', 'fleet_gps', 'crowdsource'
    )),
    geom            GEOMETRY(Point, 4326),
    PRIMARY KEY (observation_id, timestamp_utc)
) PARTITION BY RANGE (timestamp_utc);

-- Create initial partitions (auto-managed by pg_partman in production)
CREATE TABLE traffic.observations_default PARTITION OF traffic.observations DEFAULT;

CREATE INDEX idx_observations_segment_time
    ON traffic.observations (segment_id, timestamp_utc DESC);
CREATE INDEX idx_observations_geom
    ON traffic.observations USING GIST (geom);

-- Hourly aggregates for ML training features
CREATE TABLE traffic.hourly_aggregates (
    segment_id      BIGINT NOT NULL,
    hour_utc        TIMESTAMPTZ NOT NULL,
    avg_speed_kmh   REAL,
    p50_speed_kmh   REAL,
    p95_congestion  REAL,
    observation_cnt INTEGER,
    PRIMARY KEY (segment_id, hour_utc)
);

-- =============================================================================
-- CONVOY SCHEMA: VVIP movement planning and execution records
-- =============================================================================

CREATE TABLE convoy.movements (
    movement_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    vvip_class      TEXT NOT NULL CHECK (vvip_class IN ('Z+', 'Z', 'Y', 'X')),
    status          TEXT NOT NULL DEFAULT 'planning' CHECK (status IN (
        'planning', 'approved', 'active', 'completed', 'cancelled'
    )),
    origin_geom     GEOMETRY(Point, 4326) NOT NULL,
    destination_geom GEOMETRY(Point, 4326) NOT NULL,
    planned_start   TIMESTAMPTZ,
    actual_start    TIMESTAMPTZ,
    actual_end      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE convoy.planned_routes (
    route_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    movement_id     UUID REFERENCES convoy.movements(movement_id),
    route_rank      SMALLINT NOT NULL,  -- 1 = primary, 2 = alternate, etc.
    total_distance_m REAL,
    estimated_time_sec INTEGER,
    disruption_score REAL,  -- predicted traffic disruption [0-100]
    route_geom      GEOMETRY(LineString, 4326) NOT NULL,
    segment_ids     BIGINT[] NOT NULL,  -- ordered list of road segments
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_planned_routes_movement ON convoy.planned_routes (movement_id);

CREATE TABLE convoy.diversions (
    diversion_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    movement_id     UUID REFERENCES convoy.movements(movement_id),
    segment_id      BIGINT REFERENCES corridor.road_segments(segment_id),
    diversion_type  TEXT NOT NULL CHECK (diversion_type IN (
        'full_closure', 'partial_closure', 'speed_restriction', 'signal_override'
    )),
    activate_at     TIMESTAMPTZ NOT NULL,
    deactivate_at   TIMESTAMPTZ NOT NULL,
    alt_route_geom  GEOMETRY(LineString, 4326),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
