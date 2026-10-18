"""Cause-tape event emulator for the VVIP corridor stack.

The emulator injects ONLY exogenous causes — procession schedules, marathon
waves, weather onset, vehicle incidents, emergency green-corridor requests,
convoy departures — and is structurally forbidden from producing outcomes
(decisions, params, situation classes, or anything the decision stack reads
as a result).  The REAL stack plans, classifies, escalates and degrades on
its own.

Injection seams (each drives a real API of the running stack):

1. DB (corridor-store / PostGIS) — ``convoy.movements`` + ``convoy.planned_routes``
   rows the adaptive engine loop polls
   (traffic_oracle/adaptive/engine_loop.py:40); every write is parameter-bound.
   When core's ``POST /api/v1/events`` lands it becomes the preferred vehicle;
   the row writer here is the same cause at the store boundary.
2. Valkey — ``adaptive:inputs:{movement_id}`` documents in the EXACT reader
   shape of engine_loop.py:182-206 (predicted_congestion_idx / weather_severity
   / crowd_score / anomaly_active / event_active + optional weather code +
   last_gps_age_sec / ambulance_delay_projection_sec).  Nothing else in the
   repo writes these keys today, so live demos on the inputs path are
   emulator-fed by design (declared property, contract C3).
3. Simulation twin — deterministic causes posted to
   ``POST /api/v1/simulate/twin`` riding the ScenarioSpec surface
   (forced_events / event_overrides / weather); the additive cause fields ride
   in the request body and are consumed once the server widens TwinSimRequest.
4. NATS — showcase-only synthetic Arrow IPC batches on
   ``corridor.traffic.emulated`` (NEVER ``corridor.traffic.live``), matching
   the live feed's decoded schema
   (timestamp_ms/lon/lat/segment_id/speed_kmh/congestion_index/source/
   data_quality/confidence — ingest/cache_writer.py, ingest/db_writer.py).

Determinism: a run is {emulation_spec, seed, t0_anchor, clock_scale}; the
same spec+seed+t0 builds a byte-identical cause tape, and replay re-posts the
STORED tape verbatim with a re-anchored t0.  Live injections are
indistinguishable from a real feed at the API boundary; replays carry the
one tell — the ``X-Emulation-Replay`` header (HTTP requests + NATS message
headers).
"""

__version__ = "0.1.0"
