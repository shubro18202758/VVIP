-- Comprehensive Ahmedabad corridor seed data
-- Covers major VVIP routes: Ashram Road, SG Highway, CG Road, Riverfront Road,
-- Relief Road, Airport Road, SP Ring Road, Gandhinagar Highway, etc.
-- Used for development and testing

-- ═══════════════════════════════════════════════════════════════════
-- ROAD SEGMENTS (40 segments covering Ahmedabad central + approach corridors)
-- ═══════════════════════════════════════════════════════════════════

INSERT INTO corridor.road_segments (segment_id, road_name, road_class, lanes, speed_limit_kmh, oneway, geom)
VALUES
    -- Central Ahmedabad — VVIP core corridor
    (1001, 'Ashram Road (South)', 'trunk', 6, 60, FALSE,
     ST_GeomFromText('LINESTRING(72.5714 23.0225, 72.5780 23.0350)', 4326)),
    (1002, 'Ashram Road (North)', 'trunk', 6, 60, FALSE,
     ST_GeomFromText('LINESTRING(72.5780 23.0350, 72.5802 23.0500, 72.5802 23.0607)', 4326)),
    (1003, 'CG Road (West)', 'primary', 4, 50, FALSE,
     ST_GeomFromText('LINESTRING(72.5500 23.0300, 72.5560 23.0300, 72.5620 23.0300)', 4326)),
    (1004, 'CG Road (East)', 'primary', 4, 50, FALSE,
     ST_GeomFromText('LINESTRING(72.5620 23.0300, 72.5700 23.0295, 72.5780 23.0290)', 4326)),
    (1005, 'Relief Road', 'primary', 4, 40, FALSE,
     ST_GeomFromText('LINESTRING(72.5798 23.0254, 72.5850 23.0200, 72.5900 23.0180)', 4326)),
    (1006, 'Riverfront Road (West)', 'trunk', 6, 60, FALSE,
     ST_GeomFromText('LINESTRING(72.5650 23.0350, 72.5714 23.0225)', 4326)),
    (1007, 'Riverfront Road (East)', 'trunk', 6, 60, FALSE,
     ST_GeomFromText('LINESTRING(72.5780 23.0350, 72.5850 23.0300, 72.5900 23.0270)', 4326)),
    (1008, 'SP Ring Road (East)', 'motorway', 8, 80, FALSE,
     ST_GeomFromText('LINESTRING(72.6300 23.0000, 72.6400 23.0200, 72.6500 23.0400)', 4326)),

    -- Sabarmati & North Ahmedabad
    (1009, 'Sabarmati Ashram Road', 'secondary', 4, 40, FALSE,
     ST_GeomFromText('LINESTRING(72.5802 23.0607, 72.5790 23.0650, 72.5770 23.0700)', 4326)),
    (1010, 'Usmanpura Overbridge Road', 'secondary', 4, 40, FALSE,
     ST_GeomFromText('LINESTRING(72.5620 23.0450, 72.5650 23.0500, 72.5680 23.0550)', 4326)),
    (1011, 'Drive-In Road', 'primary', 4, 50, FALSE,
     ST_GeomFromText('LINESTRING(72.5293 23.0400, 72.5350 23.0380, 72.5420 23.0350)', 4326)),

    -- Lal Darwaja to Kalupur area
    (1012, 'Gandhi Road', 'trunk', 6, 40, FALSE,
     ST_GeomFromText('LINESTRING(72.5798 23.0254, 72.5810 23.0280, 72.5830 23.0310)', 4326)),
    (1013, 'Tilak Road', 'primary', 4, 50, TRUE,
     ST_GeomFromText('LINESTRING(72.5900 23.0180, 72.5950 23.0200, 72.6006 23.0251)', 4326)),
    (1014, 'Station Road (Kalupur)', 'primary', 4, 50, FALSE,
     ST_GeomFromText('LINESTRING(72.6006 23.0251, 72.6050 23.0280, 72.6100 23.0300)', 4326)),
    (1015, 'Nehru Bridge Road', 'trunk', 6, 60, FALSE,
     ST_GeomFromText('LINESTRING(72.5714 23.0225, 72.5750 23.0250, 72.5798 23.0254)', 4326)),

    -- SG Highway corridor
    (1016, 'SG Highway (Vastrapur)', 'motorway', 8, 80, FALSE,
     ST_GeomFromText('LINESTRING(72.5150 23.0300, 72.5200 23.0290, 72.5293 23.0327)', 4326)),
    (1017, 'SG Highway (Bodakdev)', 'motorway', 8, 80, FALSE,
     ST_GeomFromText('LINESTRING(72.5000 23.0350, 72.5070 23.0330, 72.5150 23.0300)', 4326)),
    (1018, 'SG Highway (Thaltej)', 'motorway', 8, 80, FALSE,
     ST_GeomFromText('LINESTRING(72.4900 23.0400, 72.4950 23.0380, 72.5000 23.0350)', 4326)),

    -- Satellite & Prahlad Nagar
    (1019, 'Satellite Road', 'secondary', 4, 40, FALSE,
     ST_GeomFromText('LINESTRING(72.5100 23.0200, 72.5150 23.0220, 72.5200 23.0250)', 4326)),
    (1020, 'Prahlad Nagar Road', 'secondary', 4, 40, FALSE,
     ST_GeomFromText('LINESTRING(72.5200 23.0250, 72.5250 23.0230, 72.5293 23.0200)', 4326)),

    -- SP Ring Road (West & South)
    (1021, 'SP Ring Road (South)', 'motorway', 8, 80, FALSE,
     ST_GeomFromText('LINESTRING(72.5100 22.9900, 72.5400 22.9850, 72.5700 22.9900)', 4326)),
    (1022, 'SP Ring Road (Southeast)', 'motorway', 8, 80, FALSE,
     ST_GeomFromText('LINESTRING(72.5700 22.9900, 72.6000 22.9950, 72.6300 23.0000)', 4326)),
    (1023, 'Bopal Road', 'primary', 4, 50, FALSE,
     ST_GeomFromText('LINESTRING(72.5100 22.9900, 72.5050 22.9950, 72.5000 23.0050)', 4326)),

    -- Gandhinagar Highway approach
    (1024, 'Gandhinagar Highway (South)', 'motorway', 8, 100, FALSE,
     ST_GeomFromText('LINESTRING(72.6100 23.0300, 72.6150 23.0400, 72.6200 23.0500)', 4326)),
    (1025, 'Gandhinagar Highway (Central)', 'motorway', 8, 100, FALSE,
     ST_GeomFromText('LINESTRING(72.6200 23.0500, 72.6250 23.0600, 72.6300 23.0700)', 4326)),
    (1026, 'Gandhinagar Highway (North)', 'motorway', 8, 100, FALSE,
     ST_GeomFromText('LINESTRING(72.6300 23.0700, 72.6350 23.0800, 72.6400 23.0900)', 4326)),

    -- Airport approach corridor
    (1027, 'Airport Road (South)', 'motorway', 8, 80, FALSE,
     ST_GeomFromText('LINESTRING(72.6200 23.0500, 72.6300 23.0550, 72.6400 23.0600)', 4326)),
    (1028, 'Airport Road (North)', 'motorway', 8, 80, FALSE,
     ST_GeomFromText('LINESTRING(72.6400 23.0600, 72.6450 23.0650, 72.6266 23.0733)', 4326)),
    (1029, 'Hansol-Airport Connector', 'motorway', 6, 80, FALSE,
     ST_GeomFromText('LINESTRING(72.6300 23.0700, 72.6350 23.0720, 72.6266 23.0733)', 4326)),

    -- Naroda & East Ahmedabad
    (1030, 'Naroda Road', 'trunk', 6, 60, FALSE,
     ST_GeomFromText('LINESTRING(72.6100 23.0300, 72.6200 23.0350, 72.6350 23.0450)', 4326)),
    (1031, 'CTM Road', 'primary', 4, 50, FALSE,
     ST_GeomFromText('LINESTRING(72.6100 23.0300, 72.6000 23.0350, 72.5900 23.0400)', 4326)),

    -- Motera / Stadium area
    (1032, 'Motera Stadium Road', 'trunk', 6, 60, FALSE,
     ST_GeomFromText('LINESTRING(72.5900 23.0800, 72.5930 23.0850, 72.5957 23.0916)', 4326)),

    -- SP Ring Road (North & West)
    (1033, 'SP Ring Road (West)', 'motorway', 8, 80, FALSE,
     ST_GeomFromText('LINESTRING(72.4900 23.0400, 72.4850 23.0500, 72.4800 23.0600)', 4326)),
    (1034, 'SP Ring Road (North)', 'motorway', 8, 70, FALSE,
     ST_GeomFromText('LINESTRING(72.5500 23.1000, 72.5700 23.1050, 72.5900 23.1000)', 4326)),
    (1035, 'SP Ring Road (Northeast)', 'motorway', 6, 70, FALSE,
     ST_GeomFromText('LINESTRING(72.5900 23.1000, 72.6100 23.0950, 72.6300 23.0900)', 4326)),

    -- Connecting arterials
    (1036, 'Paldi Road', 'secondary', 4, 40, FALSE,
     ST_GeomFromText('LINESTRING(72.5620 23.0300, 72.5650 23.0250, 72.5714 23.0225)', 4326)),
    (1037, 'Science City Road', 'secondary', 4, 40, FALSE,
     ST_GeomFromText('LINESTRING(72.6500 23.0400, 72.6550 23.0500, 72.6588 23.0684)', 4326)),
    (1038, 'Kankaria Road', 'secondary', 4, 40, FALSE,
     ST_GeomFromText('LINESTRING(72.5900 23.0180, 72.5950 23.0100, 72.6000 23.0070)', 4326)),
    (1039, 'Ellis Bridge Road', 'secondary', 4, 40, FALSE,
     ST_GeomFromText('LINESTRING(72.5620 23.0300, 72.5670 23.0270, 72.5714 23.0225)', 4326)),
    (1040, 'Sarkhej Road', 'primary', 4, 50, FALSE,
     ST_GeomFromText('LINESTRING(72.5100 22.9900, 72.5050 23.0000, 72.5000 23.0100, 72.5000 23.0200, 72.5000 23.0350)', 4326));


-- ═══════════════════════════════════════════════════════════════════
-- JUNCTIONS (14 junctions at major intersections)
-- ═══════════════════════════════════════════════════════════════════

INSERT INTO corridor.junctions (junction_type, signal_control, geom)
VALUES
    -- J1: Ashram Road / CG Road / Riverfront junction
    ('intersection', TRUE,  ST_GeomFromText('POINT(72.5780 23.0350)', 4326)),
    -- J2: Income Tax / Ashram Road / Nehru Bridge
    ('roundabout',   FALSE, ST_GeomFromText('POINT(72.5714 23.0225)', 4326)),
    -- J3: Lal Darwaja (Relief Road / Gandhi Road / Nehru Bridge)
    ('signal',       TRUE,  ST_GeomFromText('POINT(72.5798 23.0254)', 4326)),
    -- J4: CG Road / Paldi junction
    ('roundabout',   FALSE, ST_GeomFromText('POINT(72.5620 23.0300)', 4326)),
    -- J5: Relief Road / Tilak Road / Kankaria
    ('intersection', TRUE,  ST_GeomFromText('POINT(72.5900 23.0180)', 4326)),
    -- J6: Riverfront West / Ashram Road
    ('signal',       TRUE,  ST_GeomFromText('POINT(72.5650 23.0350)', 4326)),
    -- J7: Sabarmati Ashram / Ashram Road North
    ('intersection', TRUE,  ST_GeomFromText('POINT(72.5802 23.0607)', 4326)),
    -- J8: Kalupur / Station / Gandhinagar Highway
    ('signal',       TRUE,  ST_GeomFromText('POINT(72.6100 23.0300)', 4326)),
    -- J9: SG Highway / Vastrapur / IIM
    ('intersection', TRUE,  ST_GeomFromText('POINT(72.5293 23.0327)', 4326)),
    -- J10: SP Ring Road South / Bopal / Sarkhej
    ('intersection', TRUE,  ST_GeomFromText('POINT(72.5100 22.9900)', 4326)),
    -- J11: SP Ring Road East / Gandhinagar Hwy
    ('intersection', TRUE,  ST_GeomFromText('POINT(72.6300 23.0000)', 4326)),
    -- J12: SG Highway / SP Ring Road West
    ('roundabout',   FALSE, ST_GeomFromText('POINT(72.4900 23.0400)', 4326)),
    -- J13: Gandhinagar Hwy / Airport Road
    ('signal',       TRUE,  ST_GeomFromText('POINT(72.6200 23.0500)', 4326)),
    -- J14: Gandhinagar Hwy North / Airport
    ('intersection', TRUE,  ST_GeomFromText('POINT(72.6300 23.0700)', 4326));


-- ═══════════════════════════════════════════════════════════════════
-- SEGMENT ADJACENCY (connectivity graph for pgRouting)
-- Turn costs: right turn ~5s, left turn ~10s, roundabout entry ~15s, signal ~12s
-- ═══════════════════════════════════════════════════════════════════

INSERT INTO corridor.segment_adjacency (from_segment_id, to_segment_id, via_junction_id, turn_cost_sec)
VALUES
    -- Via J1 (Ashram Road / CG Road / Riverfront junction)
    (1001, 1004, 1, 10),   -- Ashram Rd S → CG Rd E (left)
    (1004, 1001, 1, 5),    -- CG Rd E → Ashram Rd S (right)
    (1001, 1002, 1, 3),    -- Ashram Rd S → Ashram Rd N (straight)
    (1002, 1001, 1, 3),    -- Ashram Rd N → Ashram Rd S (straight)
    (1007, 1001, 1, 10),   -- Riverfront E → Ashram Rd S
    (1001, 1007, 1, 10),   -- Ashram Rd S → Riverfront E

    -- Via J2 (Income Tax roundabout)
    (1001, 1015, 2, 15),   -- Ashram Rd S → Nehru Bridge
    (1015, 1001, 2, 15),   -- Nehru Bridge → Ashram Rd S
    (1006, 1001, 2, 15),   -- Riverfront W → Ashram Rd S
    (1001, 1006, 2, 15),   -- Ashram Rd S → Riverfront W
    (1006, 1015, 2, 15),   -- Riverfront W → Nehru Bridge
    (1015, 1006, 2, 15),   -- Nehru Bridge → Riverfront W
    (1039, 1001, 2, 15),   -- Ellis Bridge → Ashram Rd S
    (1001, 1039, 2, 15),   -- Ashram Rd S → Ellis Bridge

    -- Via J3 (Lal Darwaja signal)
    (1015, 1005, 3, 12),   -- Nehru Bridge → Relief Road
    (1005, 1015, 3, 12),   -- Relief Road → Nehru Bridge
    (1015, 1012, 3, 12),   -- Nehru Bridge → Gandhi Road
    (1012, 1015, 3, 12),   -- Gandhi Road → Nehru Bridge

    -- Via J4 (CG Road / Paldi roundabout)
    (1003, 1004, 4, 15),   -- CG Rd W → CG Rd E (straight)
    (1004, 1003, 4, 15),   -- CG Rd E → CG Rd W (straight)
    (1036, 1003, 4, 10),   -- Paldi → CG Rd W
    (1003, 1036, 4, 10),   -- CG Rd W → Paldi
    (1039, 1003, 4, 10),   -- Ellis Bridge → CG Rd W
    (1003, 1039, 4, 10),   -- CG Rd W → Ellis Bridge

    -- Via J5 (Relief Road / Tilak / Kankaria)
    (1005, 1013, 5, 12),   -- Relief Rd → Tilak Rd
    (1013, 1005, 5, 12),   -- Tilak Rd → Relief Rd
    (1005, 1038, 5, 10),   -- Relief Rd → Kankaria
    (1038, 1005, 5, 10),   -- Kankaria → Relief Rd

    -- Via J6 (Riverfront West / Ashram Road)
    (1006, 1010, 6, 10),   -- Riverfront W → Usmanpura
    (1010, 1006, 6, 10),   -- Usmanpura → Riverfront W
    (1006, 1011, 6, 10),   -- Riverfront W → Drive-In
    (1011, 1006, 6, 10),   -- Drive-In → Riverfront W

    -- Via J7 (Sabarmati Ashram junction)
    (1002, 1009, 7, 3),    -- Ashram Rd N → Sabarmati Ashram Rd (straight)
    (1009, 1002, 7, 3),    -- Sabarmati Ashram Rd → Ashram Rd N (straight)
    (1002, 1032, 7, 10),   -- Ashram Rd N → Motera
    (1032, 1002, 7, 10),   -- Motera → Ashram Rd N

    -- Via J8 (Kalupur / Station / Gandhinagar Hwy)
    (1014, 1024, 8, 12),   -- Station Rd → Gandhinagar Hwy S
    (1024, 1014, 8, 12),   -- Gandhinagar Hwy S → Station Rd
    (1014, 1030, 8, 10),   -- Station Rd → Naroda Rd
    (1030, 1014, 8, 10),   -- Naroda Rd → Station Rd
    (1031, 1014, 8, 10),   -- CTM Rd → Station Rd
    (1014, 1031, 8, 10),   -- Station Rd → CTM Rd

    -- Via J9 (SG Highway / Vastrapur / IIM)
    (1016, 1011, 9, 10),   -- SG Hwy Vastrapur → Drive-In
    (1011, 1016, 9, 10),   -- Drive-In → SG Hwy Vastrapur
    (1016, 1020, 9, 10),   -- SG Hwy → Prahlad Nagar
    (1020, 1016, 9, 10),   -- Prahlad Nagar → SG Hwy

    -- Via J10 (SP Ring Road South / Bopal / Sarkhej)
    (1021, 1022, 10, 5),   -- SP RR South → SP RR SE (straight)
    (1022, 1021, 10, 5),   -- SP RR SE → SP RR South (straight)
    (1021, 1023, 10, 12),  -- SP RR South → Bopal
    (1023, 1021, 10, 12),  -- Bopal → SP RR South
    (1040, 1021, 10, 12),  -- Sarkhej → SP RR South
    (1021, 1040, 10, 12),  -- SP RR South → Sarkhej

    -- Via J11 (SP Ring Road East / Gandhinagar Hwy)
    (1022, 1008, 11, 5),   -- SP RR SE → SP RR East (straight)
    (1008, 1022, 11, 5),   -- SP RR East → SP RR SE
    (1008, 1037, 11, 10),  -- SP RR East → Science City Rd
    (1037, 1008, 11, 10),  -- Science City Rd → SP RR East

    -- Via J12 (SG Highway / SP Ring Road West roundabout)
    (1018, 1033, 12, 15),  -- SG Hwy Thaltej → SP RR West
    (1033, 1018, 12, 15),  -- SP RR West → SG Hwy Thaltej
    (1018, 1017, 12, 3),   -- SG Hwy Thaltej → SG Hwy Bodakdev (straight)
    (1017, 1018, 12, 3),   -- SG Hwy Bodakdev → SG Hwy Thaltej
    (1017, 1016, 12, 3),   -- SG Hwy Bodakdev → SG Hwy Vastrapur
    (1016, 1017, 12, 3),   -- SG Hwy Vastrapur → SG Hwy Bodakdev
    (1033, 1040, 12, 15),  -- SP RR West → Sarkhej
    (1040, 1033, 12, 15),  -- Sarkhej → SP RR West

    -- Via J13 (Gandhinagar Hwy / Airport Road)
    (1024, 1025, 13, 3),   -- GN Hwy S → GN Hwy Central (straight)
    (1025, 1024, 13, 3),   -- GN Hwy Central → GN Hwy S
    (1024, 1027, 13, 10),  -- GN Hwy S → Airport Rd S
    (1027, 1024, 13, 10),  -- Airport Rd S → GN Hwy S
    (1027, 1028, 13, 3),   -- Airport Rd S → Airport Rd N
    (1028, 1027, 13, 3),   -- Airport Rd N → Airport Rd S

    -- Via J14 (Gandhinagar Hwy North / Airport)
    (1025, 1026, 14, 3),   -- GN Hwy Central → GN Hwy North (straight)
    (1026, 1025, 14, 3),   -- GN Hwy North → GN Hwy Central
    (1025, 1029, 14, 10),  -- GN Hwy Central → Hansol Connector
    (1029, 1025, 14, 10);  -- Hansol Connector → GN Hwy Central


-- ═══════════════════════════════════════════════════════════════════
-- TRAFFIC OBSERVATIONS (synthetic real-time data)
-- Multiple observations per segment with varied congestion levels
-- Timestamps within last 30 minutes to appear as live data
-- ═══════════════════════════════════════════════════════════════════

-- Create observations partition for current date if not exists
DO $$
DECLARE
    part_name TEXT;
    start_date DATE;
    end_date DATE;
BEGIN
    start_date := CURRENT_DATE;
    end_date := CURRENT_DATE + 1;
    part_name := 'observations_' || to_char(start_date, 'YYYY_MM_DD');
    IF NOT EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'traffic' AND c.relname = part_name
    ) THEN
        EXECUTE format(
            'CREATE TABLE traffic.%I PARTITION OF traffic.observations FOR VALUES FROM (%L) TO (%L)',
            part_name, start_date, end_date
        );
    END IF;
END $$;

-- Insert synthetic traffic observations for all 40 segments
-- Each segment gets 5 observations spread over last 25 minutes
INSERT INTO traffic.observations (segment_id, timestamp_utc, speed_kmh, congestion_idx, source, geom, data_quality, confidence)
SELECT
    s.segment_id,
    NOW() - (interval '5 minutes' * gen.i),
    -- Speed varies by road class and time offset (simulates fluctuation)
    CASE s.road_class
        WHEN 'motorway' THEN 55 + (random() * 30)::real - (gen.i * 2)::real
        WHEN 'trunk' THEN 40 + (random() * 25)::real - (gen.i * 3)::real
        WHEN 'primary' THEN 30 + (random() * 20)::real - (gen.i * 2)::real
        WHEN 'secondary' THEN 25 + (random() * 15)::real - (gen.i * 1.5)::real
        ELSE 20 + (random() * 10)::real
    END,
    -- Congestion index: higher for older observations (simulates building congestion)
    LEAST(1.0, GREATEST(0.0,
        CASE s.road_class
            WHEN 'motorway' THEN 0.15 + (random() * 0.25)::real + (gen.i * 0.05)::real
            WHEN 'trunk' THEN 0.25 + (random() * 0.30)::real + (gen.i * 0.06)::real
            WHEN 'primary' THEN 0.35 + (random() * 0.30)::real + (gen.i * 0.04)::real
            WHEN 'secondary' THEN 0.30 + (random() * 0.25)::real + (gen.i * 0.03)::real
            ELSE 0.20 + (random() * 0.20)::real
        END
    )),
    (ARRAY['government', 'mapping_api', 'fleet_gps', 'crowdsource'])[1 + (random() * 3)::int],
    ST_LineInterpolatePoint(s.geom, random()),
    'real',
    0.85 + (random() * 0.15)::real
FROM corridor.road_segments s
CROSS JOIN generate_series(0, 4) AS gen(i);

-- Insert additional peak-hour congestion observations for central Ahmedabad segments
INSERT INTO traffic.observations (segment_id, timestamp_utc, speed_kmh, congestion_idx, source, geom, data_quality, confidence)
SELECT
    s.segment_id,
    NOW() - (interval '2 minutes' * gen.i),
    -- Peak hour: slower speeds
    CASE s.road_class
        WHEN 'motorway' THEN 40 + (random() * 20)::real
        WHEN 'trunk' THEN 25 + (random() * 15)::real
        WHEN 'primary' THEN 15 + (random() * 12)::real
        WHEN 'secondary' THEN 12 + (random() * 10)::real
        ELSE 10 + (random() * 8)::real
    END,
    -- Higher congestion during peak
    LEAST(1.0, GREATEST(0.0,
        CASE s.road_class
            WHEN 'motorway' THEN 0.40 + (random() * 0.30)::real
            WHEN 'trunk' THEN 0.50 + (random() * 0.35)::real
            WHEN 'primary' THEN 0.55 + (random() * 0.35)::real
            WHEN 'secondary' THEN 0.45 + (random() * 0.30)::real
            ELSE 0.35 + (random() * 0.25)::real
        END
    )),
    (ARRAY['government', 'mapping_api', 'fleet_gps', 'crowdsource'])[1 + (random() * 3)::int],
    ST_LineInterpolatePoint(s.geom, random()),
    'real',
    0.90 + (random() * 0.10)::real
FROM corridor.road_segments s
CROSS JOIN generate_series(0, 2) AS gen(i)
WHERE s.segment_id IN (1001, 1002, 1003, 1004, 1005, 1006, 1011, 1012, 1014, 1016, 1018, 1019);

-- Insert hourly_aggregates for the corridor summary endpoint
INSERT INTO traffic.hourly_aggregates (segment_id, hour_utc, avg_speed_kmh, p50_speed_kmh, p95_congestion, observation_cnt)
SELECT
    s.segment_id,
    date_trunc('hour', NOW()),
    CASE s.road_class
        WHEN 'motorway' THEN 60 + (random() * 20)::real
        WHEN 'trunk' THEN 40 + (random() * 15)::real
        WHEN 'primary' THEN 30 + (random() * 12)::real
        WHEN 'secondary' THEN 25 + (random() * 10)::real
        ELSE 20 + (random() * 8)::real
    END,
    CASE s.road_class
        WHEN 'motorway' THEN 55 + (random() * 20)::real
        WHEN 'trunk' THEN 35 + (random() * 15)::real
        WHEN 'primary' THEN 25 + (random() * 12)::real
        WHEN 'secondary' THEN 20 + (random() * 10)::real
        ELSE 18 + (random() * 8)::real
    END,
    LEAST(1.0, GREATEST(0.0,
        CASE s.road_class
            WHEN 'motorway' THEN 0.30 + (random() * 0.30)::real
            WHEN 'trunk' THEN 0.45 + (random() * 0.35)::real
            WHEN 'primary' THEN 0.55 + (random() * 0.35)::real
            WHEN 'secondary' THEN 0.45 + (random() * 0.30)::real
            ELSE 0.35 + (random() * 0.25)::real
        END
    )),
    5 + (random() * 10)::int
FROM corridor.road_segments s;
