-- 008_event_profiles.sql
-- Per-use-case corridor-event profile templates (core stream).
--
-- Seeds the eight canonical use-case profiles (from the scenario library
-- configs) as events.event_profiles: the archetypes the emulator and the
-- ops board instantiate corridor_events from.  A profile is a template —
-- event_kind + protection_class + crowd_class + representative route spine,
-- departure, closure policy and simulation knobs — never a live event row.
--
-- Idempotent: ON CONFLICT (profile_id) DO NOTHING (re-running never
-- overwrites operator edits to the template table).
--
-- GRAMMAR CONSTRAINT: no ';' inside string literals and no dollar-quoted
-- bodies — these files are also applied statement-by-statement by naive
-- `;`-splitters (see 007's header and tests/test_migration_pgvector).

CREATE TABLE IF NOT EXISTS events.event_profiles (
    profile_id       TEXT PRIMARY KEY,           -- e.g. 'rath_yatra_ahmedabad'
    event_kind       TEXT NOT NULL CHECK (event_kind IN (
                         'protected_movement', 'procession', 'mass_gathering',
                         'race', 'ceremonial', 'egress_surge',
                         'emergency_evacuation', 'work_zone'
                     )),
    protection_class TEXT NOT NULL CHECK (protection_class IN (
                         'Z+', 'Z', 'Y', 'X', 'none'
                     )),
    crowd_class      TEXT NOT NULL CHECK (crowd_class IN (
                         'LOW', 'MEDIUM', 'HIGH', 'EXTREME'
                     )),
    name             TEXT NOT NULL,
    description      TEXT NOT NULL DEFAULT '',
    scenario_id      TEXT,                        -- twin scenario library key
    route_segment_ids BIGINT[] NOT NULL,
    departure_time   TIMESTAMPTZ NOT NULL,
    closure_policy   JSONB NOT NULL,
    horizon_min      SMALLINT NOT NULL CHECK (horizon_min BETWEEN 1 AND 60),
    duration_class   TEXT NOT NULL DEFAULT 'minutes' CHECK (duration_class IN (
                         'minutes', 'hours', 'multi_day', 'multi_week'
                     )),
    template         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO events.event_profiles (
    profile_id, event_kind, protection_class, crowd_class, name, description,
    scenario_id, route_segment_ids, departure_time, closure_policy,
    horizon_min, duration_class, template
) VALUES
    ('vvip_convoy_base', 'protected_movement', 'Z+', 'LOW',
     'VVIP Convoy — Base Case (Z+ state-capital run)',
     'BASE CASE. Z+ protected movement from Gandhinagar into the city core. '
     'Maximum-protocol closure (180 s advance, full closure), default event '
     'rates, no weather — the reference every other use case generalizes.',
     'vvip_convoy_base', ARRAY[1026,1025,1024,1014,1030],
     '2026-11-02T04:40:00Z'::timestamptz,
     '{"advance_closure_sec": 180, "closure_type": "full_closure"}'::jsonb,
     45, 'minutes',
     '{"convoy_speed_kmh": 60, "n_replications": 10, "seed": 20261102}'::jsonb),
    ('rath_yatra_ahmedabad', 'procession', 'X', 'EXTREME',
     'Jagannath Rath Yatra — Old-City Procession',
     '148th Ahmedabad Rath Yatra: three chariots at chariot pace (8 km/h) '
     'through the old-city bazaar belt with lakhs-strong crowd load.',
     'rath_yatra_ahmedabad', ARRAY[1012,1015,1005,1013],
     '2025-06-27T01:30:00Z'::timestamptz,
     '{"advance_closure_sec": 120, "closure_type": "full_closure"}'::jsonb,
     60, 'hours',
     '{"convoy_speed_kmh": 8, "n_replications": 12, "seed": 20250627, '
     '"event_overrides": {"market": 2.5}}'::jsonb),
    ('kumbh_scale_gathering', 'mass_gathering', 'X', 'EXTREME',
     'Kumbh-Class Mass Gathering — Perimeter Zone',
     'Bathing-day window: the zone interior is car-free and frozen — the '
     'engine manages the perimeter ring and the ingress surges.',
     'kumbh_scale_gathering', ARRAY[1001,1006,1010],
     '2025-01-28T23:00:00Z'::timestamptz,
     '{"advance_closure_sec": 90, "closure_type": "signal_hold"}'::jsonb,
     60, 'multi_day',
     '{"convoy_speed_kmh": 40, "n_replications": 12, "seed": 20250129}'::jsonb),
    ('riverfront_marathon', 'race', 'X', 'HIGH',
     'City Marathon — Sabarmati Riverfront Race Corridor',
     '05:45 gun start — the race corridor is closed in the dark and reopens '
     'in stages behind the sweep (pace/sweep convoy at 12 km/h).',
     'riverfront_marathon', ARRAY[1007,1001,1006,1010],
     '2024-11-24T00:15:00Z'::timestamptz,
     '{"advance_closure_sec": 60, "closure_type": "full_closure"}'::jsonb,
     60, 'hours',
     '{"convoy_speed_kmh": 12, "n_replications": 10, "seed": 20241124}'::jsonb),
    ('national_day_parade', 'ceremonial', 'Z', 'HIGH',
     'Republic Day / Independence Day — Ceremonial Parade Corridor',
     'Dignitary runs from the airport onto the GN Highway parade strip, '
     'arriving before the 09:00 flag while spectators saturate the strip.',
     'national_day_parade', ARRAY[1028,1027,1024,1025],
     '2027-01-26T03:05:00Z'::timestamptz,
     '{"advance_closure_sec": 150, "closure_type": "full_closure"}'::jsonb,
     60, 'minutes',
     '{"convoy_speed_kmh": 60, "n_replications": 10, "seed": 20270126}'::jsonb),
    ('motera_stadium_egress', 'egress_surge', 'X', 'EXTREME',
     'Stadium Egress — Narendra Modi Stadium Match Night',
     'The 90-120 min egress surge after a final ball: an ambulance with a '
     'green-corridor request fights the dispersal wave under signal_hold.',
     'motera_stadium_egress', ARRAY[1032,1002,1001],
     '2026-03-08T14:35:00Z'::timestamptz,
     '{"advance_closure_sec": 60, "closure_type": "signal_hold"}'::jsonb,
     60, 'hours',
     '{"convoy_speed_kmh": 50, "n_replications": 12, "seed": 20260308}'::jsonb),
    ('flood_evacuation_corridor', 'emergency_evacuation', 'X', 'MEDIUM',
     'Monsoon Flood — Evacuation and Relief Corridor',
     'Gujarat floods class: the low-lying river spine waterlogs while an '
     'X-class relief column runs the high-ground ring-road detour.',
     'flood_evacuation_corridor', ARRAY[1040,1021,1022,1008],
     '2024-08-27T05:50:00Z'::timestamptz,
     '{"advance_closure_sec": 60, "closure_type": "signal_hold"}'::jsonb,
     60, 'hours',
     '{"convoy_speed_kmh": 40, "n_replications": 10, "seed": 20240827, '
     '"event_overrides": {"breakdown": 1.8, "waterlogging": 3}, '
     '"weather": {"monsoon": true, "rain_intensity_mm_h": 22}}'::jsonb),
    ('metro_work_zone', 'work_zone', 'X', 'LOW',
     'Planned Infrastructure Work Zone — Metro/Bullet-Train Works',
     'Months-long published lane loss: an inspection convoy runs CTM Road '
     'through the Kalupur pinch while both work zones hold barricaded lanes.',
     'metro_work_zone', ARRAY[1031,1014,1024],
     '2025-08-14T03:45:00Z'::timestamptz,
     '{"advance_closure_sec": 60, "closure_type": "partial_closure"}'::jsonb,
     60, 'multi_week',
     '{"convoy_speed_kmh": 40, "n_replications": 8, "seed": 20250814, '
     '"event_overrides": {"market": 1.5}}'::jsonb)
ON CONFLICT (profile_id) DO NOTHING;
