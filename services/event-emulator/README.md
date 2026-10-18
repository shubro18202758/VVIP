# event-emulator — the emulated half of the corridor stack

An event-injection layer that **emulates only causes** — procession
schedules, marathon waves, weather onset, vehicle incidents, emergency
green-corridor requests, convoy departures — consuming the UseCaseLibrary
scenario configs verbatim (`src/event_emulator/use_case_library.json`, the
8 use cases) and driving the **real stack through its real APIs**. The
emulator never produces outcomes (no decisions, params, situation classes,
or anything the decision stack reads as a result): the real stack plans,
classifies, escalates and degrades on its own.

## Injection seams (all verified live 2026-10-12)

| Seam | Vehicle | Where the real stack consumes it |
|---|---|---|
| DB — event rows | `convoy.movements` + `convoy.planned_routes` (what today's running engine polls, `traffic_oracle/adaptive/engine_loop.py:40`) **and** core's generalized `events.corridor_events` + `events.event_routes` + `events.event_phases` (migration 007), soft-linked via `movement_id` | situation engine loop; `GET /api/v1/movements/active` |
| Valkey — inputs docs | `adaptive:inputs:{movement_id}` in the **exact reader shape** (`engine_loop.py:182-206`): `predicted_congestion_idx / weather_severity / crowd_score / anomaly_active / event_active` + optional weather code + `last_gps_age_sec / ambulance_delay_projection_sec` | situation classify → hysteresis → `situation:changed` |
| Simulation twin | one `POST /api/v1/simulate/twin` per run carrying the config verbatim (route, departure, policy, horizon, replications, seed) + the additive cause fields (`weather`, `event_overrides` incl. the sixth `ambulance_rate_multiplier`, `forced_events` incl. `emergency_vehicle`) riding in-body until the server widens `TwinSimRequest` | seeded Monte-Carlo twin |
| NATS — showcase plane | synthetic Arrow IPC batches on `corridor.traffic.emulated` (NEVER `.live`) in the decoded schema of the live plane (`timestamp_ms/lon/lat/segment_id/speed_kmh/congestion_index/source/data_quality/confidence`) | showcase only; the decision stack reads real subjects |

Declared property (contract C3): nothing else writes `adaptive:inputs:*`
today (repo grep — only the reader exists), so live demos on the inputs
path are emulator-fed by design until the optional feed-writer follow-on.
The showcase must label the inputs source.

## Determinism & replay

A run = `{emulation_spec, seed, t0_anchor, clock_scale}`.

* The cause tape is a pure function of (spec, seed, t0) — `derive_seed`
  blake2b + a uniform-stream `SeededRng` (same discipline as
  `traffic_oracle/simulation/rng.py`). Two builds with the same inputs are
  **byte-identical** (verified: same sha256 across processes).
* Movement identity is `uuid5(use_case_id, seed)` — deterministic, so a
  replay **re-animates the same event row** (idempotent upsert) instead of
  forking a new one.
* Runs ledger in `emulator.emulation_runs` (core's migration 009:
  spec_hash, spec, seed, t0_anchor, clock_scale, status, tape JSONB).
  `simulation.runs` (006) is never written — it is outcome-semantics-only.
* **Replay = re-posting the stored tape verbatim with a re-anchored t0**
  (verified live: ledger `tape->'frames'` equality between run and replay).
* Live injections are indistinguishable from a real feed at the API
  boundary — no emulator header anywhere. Replays carry the single tell:
  `X-Emulation-Replay: <run_id>` on HTTP requests and NATS message
  headers (verified live: 721/721 replay batches headered, live batches
  headerless). Valkey docs stay byte-identical in both modes; provenance
  rides the sidecar key `emulator:source:{id}` the reader never touches.

## Cause vocabulary (contract C4 — closed)

`event_row`, `inputs_doc`, `scheduled_event` (six EventTypes: the five
existing + additive `emergency_vehicle`), `event_config` (five config
multipliers verbatim + additive `ambulance_rate_multiplier`),
`weather_ramp`, `crowd_script`. `emergency_request` is **not** a kind —
flood relief is priority sub-event rows + scheduled `emergency_vehicle` +
`ambulance_delay_projection_sec` inputs (the green-corridor channel).

## Security (outbound HTTP + SQL)

* Scheme http/https only; **URLs from external input** are host-validated
  before any request — localhost, loopback, private, reserved, link-local,
  multicast and unspecified addresses all rejected (`safe_http.py`,
  choke-pointed in `SafeHttpClient`; DNS-resolved addresses are checked,
  defeating rebinding to private IPs).
* **Fixed internal endpoints are config constants** (traffic-oracle
  :8081) enforced by `assert_internal_endpoint` (loopback /
  host.docker.internal allowlist) — env overrides cannot redirect the
  twin seam at an arbitrary host.
* All SQL is parameter-bound (`%s` placeholders, the scripts/migrate.py
  discipline); no f-string/format/concatenated SQL exists anywhere.

## Run it

```bash
# service on :8090 (CORS open to the command-deck origin)
VVIP_DATABASE_URL=postgresql://corridor_admin:…@localhost:55434/corridor_db \
  python -m event_emulator

# CLI
python -m event_emulator.cli list
python -m event_emulator.cli inject rath_yatra_ahmedabad --clock-scale 60
python -m event_emulator.cli inject flood_evacuation_corridor --dry-run \
    --t0 2026-10-12T06:00:00+00:00 --tape-out tape.json
python -m event_emulator.cli replay 2 --clock-scale 60
python -m event_emulator.cli show 2
```

HTTP API: `GET /health`, `GET /api/v1/emulator/use-cases`,
`POST /api/v1/emulator/runs`, `GET /api/v1/emulator/runs[/{id}]`,
`POST /api/v1/emulator/runs/{id}/replay`.

Compose: `infra/compose.event-emulator.yml` (emulator-owned overlay, no
shared-file edits) + `infra/docker/Dockerfile.event-emulator`.

## Declared boundaries & fidelity limits

* Reverse-flow lanes are approximated as `partial_closure` + directional
  demand (true direction flip is an additive twin follow-on).
* Interior pedestrian dynamics are not modeled — only their vehicle-side
  pressure via `crowd_score`.
* The twin POST's additive cause fields (`weather`, `event_overrides`,
  `forced_events`) are accepted-in-body today; the running server consumes
  the scalar fields until core widens `TwinSimRequest`. Delivery level is
  recorded per run (`twin_accepted_fields`).
* The emulator does not create convoy-brain movement contexts (Valkey
  `convoy:context:*`) — that is the brain's own API surface; emulated
  events are engine-side causes.
* Tests: `pytest tests/` (79 passed, 1 skipped at last run) — security,
  determinism, replay, verbatim library consumption, runner delivery.
