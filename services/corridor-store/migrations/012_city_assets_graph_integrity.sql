-- 012_city_assets_graph_integrity.sql
-- City asset registries + routing-graph integrity + e-challan evidence
-- retention (enforcement-anpr-gis stream, wave 1).
--
-- SCHEMA ONLY.  Migrations run BEFORE the seed on a fresh install, so this
-- file never references a seed row: the registry rows, the adjacency
-- repairs, junction names and the post-seed signal programs live in
-- services/corridor-store/seed/post_seed_derivations.sql (applied after
-- sample_corridor.sql, and once to an already-seeded store).
--
--   1. corridor.cameras — ANPR / CCTV camera SITES (location registry).
--      Locations and identity only: no frame, image, stream URL or person
--      column exists here, by design (C6 — vehicles only, via ANPR plates).
--      source says where a site row comes from: emulated (the event
--      emulator's sites, labelled EMULATED everywhere) | osm | registered
--      (an integration of the state's existing ICCC / CCTV / ITMS camera
--      inventory, which this platform is built to strengthen).
--   2. corridor.agency_stations — police / fire / EMS / hospital / Jan
--      Rakshak bases, agency per the 011 dispatch list.
--   3. corridor.junctions.name / name_gu — EN + Gujarati labels.
--   4. corridor.vehicle_earmarks.cleared_by — the officer who cleared it.
--   5. corridor.vehicles plate CHECK — the normalized form the oracle
--      writes (upper-case alphanumerics, no separators).  NOT VALID: new and
--      updated rows are checked, legacy rows are left untouched.
--   6. events.enforcement_recommendations.sighting_id: ON DELETE CASCADE
--      becomes ON DELETE SET NULL (and the column nullable).  A retention
--      reap of a sighting can no longer delete an e-challan and its audit
--      trail — the recommendation keeps its evidence JSONB.
--   7. corridor.road_graph — a DIRECTED line graph: every adjacency row is
--      one directed edge (both directions are stored explicitly), so
--      reverse_cost is -1.  The 002 view added a reverse edge costed with
--      the wrong segment's length (up to 68 percent cheap) and let routes
--      run against one-way segments.  A one-way segment may only be
--      entered at its start and left at its end (judged at the adjacency
--      row's via junction).  Duplicate (from, to) pairs that meet at two
--      junctions collapse to the cheapest turn, so edge ids stay unique.
--
-- Idempotent and additive: CREATE ... IF NOT EXISTS, ADD COLUMN IF NOT
-- EXISTS, guarded DROP CONSTRAINT IF EXISTS + re-ADD, CREATE OR REPLACE
-- VIEW with the 002 column list and types.
--
-- GRAMMAR CONSTRAINT (007 header): applied by psql AND by naive
-- splitters — no semicolon inside string literals, no dollar-quoted
-- bodies, no block comments.

-- =============================================================================
-- 1. CAMERA SITE REGISTRY
-- =============================================================================

CREATE TABLE IF NOT EXISTS corridor.cameras (
    camera_id     TEXT PRIMARY KEY
                      CHECK (camera_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$'),
    kind          TEXT NOT NULL CHECK (kind IN ('anpr', 'cctv')),
    source        TEXT NOT NULL CHECK (source IN ('emulated', 'osm', 'registered')),
    label         TEXT,
    geom          GEOMETRY(Point, 4326) NOT NULL,
    junction_id   BIGINT REFERENCES corridor.junctions(junction_id)
                      ON DELETE SET NULL,
    segment_id    BIGINT REFERENCES corridor.road_segments(segment_id)
                      ON DELETE SET NULL,
    -- direction of travel the camera reads, degrees clockwise from north
    heading_deg   REAL CHECK (heading_deg IS NULL
                              OR (heading_deg >= 0 AND heading_deg < 360)),
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN (
                      'active', 'offline', 'maintenance', 'decommissioned'
                  )),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cameras_geom
    ON corridor.cameras USING GIST (geom);

CREATE INDEX IF NOT EXISTS idx_cameras_junction
    ON corridor.cameras (junction_id);

-- =============================================================================
-- 2. AGENCY STATION REGISTRY
-- =============================================================================

CREATE TABLE IF NOT EXISTS corridor.agency_stations (
    station_id    TEXT PRIMARY KEY
                      CHECK (station_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$'),
    -- the 011 dispatch agency list (corridor.dispatches.agency)
    agency        TEXT NOT NULL CHECK (agency IN (
                      'jan_rakshak', 'police', 'traffic', 'ambulance', 'fire'
                  )),
    kind          TEXT NOT NULL CHECK (kind IN (
                      'police_station', 'fire_station', 'ems_base',
                      'hospital', 'jr_post'
                  )),
    name          TEXT NOT NULL,
    name_gu       TEXT,
    geom          GEOMETRY(Point, 4326) NOT NULL,
    source        TEXT NOT NULL DEFAULT 'emulated' CHECK (source IN (
                      'emulated', 'osm', 'registered'
                  )),
    -- the OpenStreetMap object a source = osm row was taken from,
    -- e.g. node/3247518115 (ODbL attribution travels with the row)
    osm_ref       TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agency_stations_geom
    ON corridor.agency_stations USING GIST (geom);

CREATE INDEX IF NOT EXISTS idx_agency_stations_agency
    ON corridor.agency_stations (agency, kind);

-- =============================================================================
-- 3. JUNCTION NAMES (EN + Gujarati)
-- =============================================================================

ALTER TABLE corridor.junctions ADD COLUMN IF NOT EXISTS name TEXT;

ALTER TABLE corridor.junctions ADD COLUMN IF NOT EXISTS name_gu TEXT;

-- =============================================================================
-- 4. EARMARK CLEARANCE ATTRIBUTION
-- =============================================================================

ALTER TABLE corridor.vehicle_earmarks ADD COLUMN IF NOT EXISTS cleared_by TEXT;

-- =============================================================================
-- 5. NORMALIZED PLATES (NOT VALID: legacy rows untouched)
-- =============================================================================

ALTER TABLE corridor.vehicles
    DROP CONSTRAINT IF EXISTS vehicles_plate_normalized_check;

ALTER TABLE corridor.vehicles
    ADD CONSTRAINT vehicles_plate_normalized_check
    CHECK (plate ~ '^[A-Z0-9]{4,12}$') NOT VALID;

-- =============================================================================
-- 6. E-CHALLAN EVIDENCE OUTLIVES SIGHTING RETENTION
-- (constraint name = the auto-generated one from 010, verified live)
-- =============================================================================

ALTER TABLE events.enforcement_recommendations
    ALTER COLUMN sighting_id DROP NOT NULL;

ALTER TABLE events.enforcement_recommendations
    DROP CONSTRAINT IF EXISTS enforcement_recommendations_sighting_id_fkey;

ALTER TABLE events.enforcement_recommendations
    ADD CONSTRAINT enforcement_recommendations_sighting_id_fkey
    FOREIGN KEY (sighting_id) REFERENCES corridor.vehicle_sightings(sighting_id)
    ON DELETE SET NULL;

-- =============================================================================
-- 7. DIRECTED ROAD GRAPH (replaces the 002 view, same columns and types)
-- Nodes are SEGMENT ids.  Edge (from -> to) costs the turn plus the
-- traversal time of the FROM segment, exactly as in 002, but it is only
-- ever traversed forwards.
-- =============================================================================

CREATE OR REPLACE VIEW corridor.road_graph AS
SELECT DISTINCT ON (sa.from_segment_id, sa.to_segment_id)
    sa.from_segment_id * 1000000 + sa.to_segment_id AS id,
    sa.from_segment_id                               AS source,
    sa.to_segment_id                                 AS target,
    sa.turn_cost_sec + (
        ST_Length(rf.geom::geography) / NULLIF(rf.speed_limit_kmh * 1000.0 / 3600.0, 0)
    )                                                AS cost,
    -1::float8                                       AS reverse_cost
FROM corridor.segment_adjacency sa
JOIN corridor.road_segments rf ON rf.segment_id = sa.from_segment_id
JOIN corridor.road_segments rt ON rt.segment_id = sa.to_segment_id
JOIN corridor.junctions j ON j.junction_id = sa.via_junction_id
WHERE (
        NOT COALESCE(rt.oneway, FALSE)
        OR ST_DWithin(ST_StartPoint(rt.geom)::geography, j.geom::geography, 25)
      )
  AND (
        NOT COALESCE(rf.oneway, FALSE)
        OR ST_DWithin(ST_EndPoint(rf.geom)::geography, j.geom::geography, 25)
      )
ORDER BY sa.from_segment_id, sa.to_segment_id, sa.turn_cost_sec;
