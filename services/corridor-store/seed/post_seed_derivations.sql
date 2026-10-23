-- post_seed_derivations.sql
-- Idempotent data repairs and derivations applied AFTER the seed and the
-- road network step.
--
-- WHEN
--   * fresh install: scripts/migrate.py (001..020), then
--     seed/sample_corridor.sql, seed/reanchor_osm_network.sql, then THIS
--     file (veer_up.sh seed() does all of them, in that order);
--   * an already-seeded store: after seed/reanchor_osm_network.sql, again
--     whenever this file changes (veer_up.sh sha-stamps it).
--   Running it again is a no-op (every statement is guarded), so applying
--   it twice is always safe:
--     docker exec -i <postgis> psql -U corridor_admin -d corridor_db \
--         < services/corridor-store/seed/post_seed_derivations.sql
--
-- THE NETWORK ITSELF is not derived here any more.  Veer's 49 segments run
-- along the real roads of their names between real intersections
-- (OpenStreetMap, WP-20): the generated, idempotent network step
-- seed/reanchor_osm_network.sql (scripts/gen_osm_network.py, mapping table
-- scripts/data/osm_network_map.json) writes the segments, junctions 1-34
-- with their EN + Gujarati names, every movement, the signal programs, the
-- emulated camera sites and the agency stations — the blocks this file
-- used to hold for the schematic network (A connectors, B junctions at
-- shared endpoints, E names, F camera sites, G stations).  They are gone
-- from here because a schematic constant applied to the real network
-- would add junk: B's fallback, for one, inserted a junction wherever none
-- sat within 5 m of a schematic point.
--
-- WHAT (all inside ONE transaction — a failure leaves nothing half-done),
-- the geometry-driven rules that hold for ANY network — on the generated
-- one every statement below is a no-op, and a store an operator edited
-- by hand is repaired the same way:
--   C. adjacency repairs: every adjacency row joins two segments that
--      MEET, at the junction where they meet:
--        C1 re-point rows whose segments meet but whose via junction is
--           elsewhere;
--        C2 add every missing movement between segments sharing an
--           endpoint, at the junction there;
--        C3 delete rows whose via junction does not lie on both segments.
--   D. signal programs for signal-controlled junctions without one — the
--      006 derivation, which runs BEFORE the seed on a fresh install and
--      therefore finds no junctions there.
--   H. provenance honesty: random observations an old seed labelled real
--      become data_quality = synthetic.
--
-- Every repair is geometry-driven or keyed by location, never by a
-- serial id the store may have assigned differently.
--
-- DISPLAY GEOMETRY (migration 015, contract C-K): the routing geom IS the
-- OpenStreetMap road now, so seed/display_geometry_osm.sql (applied after
-- this file) only clears a display line a store still carries.  A future
-- repair here that MOVES a segment's geom must clear that segment's
-- display line in the same statement
--     SET geom = ..., display_geom = NULL, display_geom_source = NULL
-- else the UPDATE fails 015's CHECK road_segments_display_geom_ends and
-- this whole transaction rolls back.

BEGIN;

-- =============================================================================
-- C. ADJACENCY REPAIRS (geometry-driven)
-- A row (from, to, via) is CORRECT when the via junction lies within 25 m
-- of BOTH segments, i.e. at the point where they meet.
-- =============================================================================

-- C1. Re-point rows whose segments meet (shared endpoint or crossing) but
--     whose via junction is elsewhere.  Keeps the seed's turn cost.
INSERT INTO corridor.segment_adjacency (from_segment_id, to_segment_id, via_junction_id, turn_cost_sec)
SELECT sa.from_segment_id, sa.to_segment_id, jx.junction_id, sa.turn_cost_sec
FROM corridor.segment_adjacency sa
JOIN corridor.road_segments a ON a.segment_id = sa.from_segment_id
JOIN corridor.road_segments b ON b.segment_id = sa.to_segment_id
JOIN corridor.junctions j ON j.junction_id = sa.via_junction_id
CROSS JOIN LATERAL (
    SELECT jj.junction_id
    FROM corridor.junctions jj
    WHERE ST_DWithin(jj.geom::geography, ST_ClosestPoint(a.geom, b.geom)::geography, 25)
    ORDER BY jj.geom <-> ST_ClosestPoint(a.geom, b.geom)
    LIMIT 1
) jx
WHERE ST_Intersects(a.geom, b.geom)
  AND NOT (
      ST_DWithin(j.geom::geography, a.geom::geography, 25)
      AND ST_DWithin(j.geom::geography, b.geom::geography, 25)
  )
ON CONFLICT DO NOTHING;

-- C2. Every movement between two segments that share an endpoint, via
--     the junction at that endpoint.  Turn cost: roundabout 15 s,
--     straight on (deflection <= 30 deg) 3 s, signalized turn 12 s, other
--     turn 10 s — the seed's own scale.  Existing rows keep their costs.
INSERT INTO corridor.segment_adjacency (from_segment_id, to_segment_id, via_junction_id, turn_cost_sec)
SELECT p.from_id, p.to_id, j.junction_id,
       CASE
           WHEN j.junction_type = 'roundabout' THEN 15
           -- turn angle in [0, 180]: 0 = straight on, 180 = U-turn
           WHEN abs(mod((p.depart_brg - p.arrive_brg + 540)::numeric, 360) - 180) <= 30 THEN 3
           WHEN j.signal_control THEN 12
           ELSE 10
       END
FROM (
    SELECT a.segment_id AS from_id, b.segment_id AS to_id, a.pt AS shared_pt,
           degrees(ST_Azimuth(a.inner_pt::geography, a.pt::geography)) AS arrive_brg,
           degrees(ST_Azimuth(b.pt::geography, b.inner_pt::geography)) AS depart_brg
    FROM (
        SELECT segment_id, ST_StartPoint(geom) AS pt, ST_PointN(geom, 2) AS inner_pt
        FROM corridor.road_segments
        UNION ALL
        SELECT segment_id, ST_EndPoint(geom), ST_PointN(geom, ST_NPoints(geom) - 1)
        FROM corridor.road_segments
    ) a
    JOIN (
        SELECT segment_id, ST_StartPoint(geom) AS pt, ST_PointN(geom, 2) AS inner_pt
        FROM corridor.road_segments
        UNION ALL
        SELECT segment_id, ST_EndPoint(geom), ST_PointN(geom, ST_NPoints(geom) - 1)
        FROM corridor.road_segments
    ) b ON b.segment_id <> a.segment_id
       AND ST_DWithin(a.pt::geography, b.pt::geography, 1)
) p
CROSS JOIN LATERAL (
    SELECT jj.junction_id, jj.junction_type, jj.signal_control
    FROM corridor.junctions jj
    WHERE ST_DWithin(jj.geom::geography, p.shared_pt::geography, 25)
    ORDER BY jj.geom <-> p.shared_pt
    LIMIT 1
) j
ON CONFLICT DO NOTHING;

-- C3. Drop every row whose via junction does not lie on both segments:
--     the teleports (segments 443 m - 2.4 km apart) and the rows C1
--     re-pointed.
DELETE FROM corridor.segment_adjacency sa
USING corridor.road_segments a, corridor.road_segments b, corridor.junctions j
WHERE a.segment_id = sa.from_segment_id
  AND b.segment_id = sa.to_segment_id
  AND j.junction_id = sa.via_junction_id
  AND NOT (
      ST_DWithin(j.geom::geography, a.geom::geography, 25)
      AND ST_DWithin(j.geom::geography, b.geom::geography, 25)
  );

-- =============================================================================
-- D. SIGNAL PROGRAMS (the 006 derivation, verbatim, now that junctions and
--    adjacency exist).  ON CONFLICT keeps every existing / calibrated row.
-- =============================================================================

INSERT INTO corridor.signal_programs (junction_id, cycle_sec, phases, offset_sec, source)
SELECT
    j.junction_id,
    (prog.program->>'cycle_sec')::INTEGER,
    prog.program->'phases',
    (j.junction_id * 17) % (prog.program->>'cycle_sec')::INTEGER,
    'osm_assumed'
FROM corridor.junctions j
CROSS JOIN LATERAL (
    SELECT
        CASE
            WHEN bool_or(rs.road_class = 'motorway') THEN
                '{"cycle_sec": 90, "phases": [{"green_sec": 44, "movements": ["through"]}, {"green_sec": 34, "movements": ["ramp", "cross"]}]}'::jsonb
            WHEN bool_or(rs.road_class = 'trunk') THEN
                '{"cycle_sec": 100, "phases": [{"green_sec": 47, "movements": ["through"]}, {"green_sec": 38, "movements": ["cross"]}]}'::jsonb
            WHEN bool_or(rs.road_class = 'primary') THEN
                '{"cycle_sec": 110, "phases": [{"green_sec": 50, "movements": ["through"]}, {"green_sec": 42, "movements": ["cross"]}]}'::jsonb
            WHEN bool_or(rs.road_class = 'secondary') THEN
                '{"cycle_sec": 120, "phases": [{"green_sec": 54, "movements": ["through"]}, {"green_sec": 46, "movements": ["cross"]}]}'::jsonb
            ELSE
                '{"cycle_sec": 120, "phases": [{"green_sec": 40, "movements": ["local"]}, {"green_sec": 34, "movements": ["cross"]}]}'::jsonb
        END AS program
    FROM corridor.segment_adjacency sa
    JOIN corridor.road_segments rs ON rs.segment_id = sa.from_segment_id
    WHERE sa.via_junction_id = j.junction_id
) AS prog
WHERE j.signal_control = TRUE
ON CONFLICT (junction_id) DO NOTHING;

-- =============================================================================
-- H. PROVENANCE HONESTY — the seed's observations are random numbers; the
-- original seed labelled them data_quality = real.  Only rows stamped at
-- or before the seed load (first road_segments row + 1 min) are touched,
-- so live ingest is never relabelled.  (sample_corridor.sql now inserts
-- them as synthetic, making this a no-op on fresh installs.)
-- =============================================================================

UPDATE traffic.observations
SET data_quality = 'synthetic',
    confidence = LEAST(confidence, 0.6)
WHERE data_quality = 'real'
  AND timestamp_utc <= (SELECT min(created_at) FROM corridor.road_segments)
                       + interval '1 minute';

COMMIT;
