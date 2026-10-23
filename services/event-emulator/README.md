# event-emulator — the emulated half of the corridor stack

An event-injection service that **emulates only causes** — procession
schedules, marathon waves, weather onset, vehicle incidents, emergency
green-corridor requests, convoy departures, ANPR camera reads of passing
vehicles and patrolling agency units — and drives the **real stack through
its real APIs**. It never produces outcomes: no decision, parameter,
situation band, e-challan or approval comes from here. The real stack plans,
classifies, escalates and degrades on its own, and a named officer makes
every decision the scenarios lead up to — a signed-in human in the judged
demo (an unattended script, like the flagship showcase, records its
approvals as `Scripted demo officer (showcase script)`).

Everything it writes says what it is: events carry `metadata.source =
'event-emulator'`, ANPR reads are stored `source = 'emulated_anpr'`, every
generated plate carries the `EM` series marker (`GJ27EM…`), units are
`emulated_patrol`, and the deck labels all of it EMULATED.

## The library

`src/event_emulator/use_case_library.json` — 10 entries
(`GET /api/v1/emulator/use-cases`): the eight corridor use cases
(`vvip_convoy_base`, `rath_yatra_ahmedabad`, `kumbh_scale_gathering`,
`riverfront_marathon`, `national_day_parade`, `motera_stadium_egress`,
`flood_evacuation_corridor`, `metro_work_zone`), the flagship composite
`rath_yatra_monsoon_ambulance` ("God's Eye Morning") and the emulated
stolen-vehicle sweep `watchlist_anpr_sweep` (category `enforcement`). Each
entry carries its `scenarioConfig`; the `event_kind` / `crowd_class` axes
come from `profiles.py`. Route spines name segments of the real
OpenStreetMap road network (ids 1001–1049), each a legal path on it. The
sweep's second plate, GJ27EM4321, is an operator exercise: the emulator
never earmarks it, so its reads hit `earmarked_plate` only after a duty
officer earmarks it (on a fresh install they are ordinary sightings);
its `expected_features` say `operator_earmark_exercise`.

## Injection seams

| Frame kind | Seam | Where the real stack consumes it |
|---|---|---|
| `event_row` | the corridor event (`events.corridor_events` + spines + phases, with its creation ledger row) and the legacy `convoy.movements` row — its route line the segments' routing lines chained in the direction of travel, its origin and destination where that line starts and ends — then `active` at t0 and `completed` at the horizon through `POST /api/v1/events/{id}/status` (C-F, actor `event-emulator (system)`) | situation engine loop, `/ops` board, citizen feed |
| `inputs_doc` | Valkey `adaptive:inputs:{id}` in the exact reader shape, plus the `corridor.traffic.emulated` Arrow plane — every row `data_quality` 2 SYNTHETIC (1 ANOMALOUS only as the congestion-outlier marker, never 0 REAL), at its segment's midpoint along the line | situation classify → hysteresis → `situation:changed` |
| `anpr_read` | `POST /api/v1/vehicles/sightings` (C-E) | the ORACLE computes the watchlist hit and the e-challan rules; the emulator asserts neither |
| `agency_unit` | NATS `corridor.units.emulated` (C-A) | oracle → Valkey `units:emulated:{unit_id}` (EX 30) → `GET /api/v1/units` |
| twin | one `POST /api/v1/simulate/twin` per run, concurrent with the frames | seeded Monte-Carlo twin; its answer is recorded compactly, never acted on |

The emulated traffic plane never becomes the live one: batches go to
`corridor.traffic.emulated` (never `.live`) with every row labelled
`data_quality` SYNTHETIC (2) on the wire — ANOMALOUS (1) only for a
congestion outlier above 0.85, the marker; never REAL (0) — and the oracle
caches them in their own key space, `traffic:emulated:latest:{sid}`,
which only EMULATED events read — emulated traffic cannot drive a real
event's band (the oracle also forces `synthetic` on ingest).

**On the road.** Every position the emulator anchors lies ON its road: a
segment's anchor is the point halfway along its routing line,
`ST_LineInterpolatePoint(geom, 0.5)` — the anchor signal-ingress gives its
feeds — never `ST_Centroid`, which on a curved OpenStreetMap road can sit
a kilometre off it; patrol paths follow the drawn line
(`COALESCE(display_geom, geom)`, C-K); a movement's route line is its
segments' routing lines oriented in the direction of travel (the
junctions recorded between consecutive segments and the one-way rule —
`tape.orient_route`), and its origin and destination are that line's ends.
The cached anchors and paths are keyed by a fingerprint of the network,
re-probed every 60 s (`store.py`), so a network converted under a running
emulator — the WP-20 conversion, a re-seed — is re-read within a minute
and a restart reads it afresh. On a fresh install: wire rows ≤ 0.006 m
from their segment, patrol units ≤ 0.066 m, movement ends 0.0 m from the
route's ends.

**The stolen-vehicle premise.** A tape that drives a FIR-named stolen plate
first makes its premise explicit at t0: `POST
/api/v1/vehicles/{plate}/earmark` with lawful basis `stolen_vehicle_fir`,
the use case's FIR and `earmarked_by` "event-emulator (system) — scenario
precondition" (reason EMULATED), unless an active stolen-FIR earmark
already exists. Its reads therefore become evidence-only
`R_STOLEN_SIGHTING` recommendations (C-N) — never a fine to the owner of a
reported-stolen vehicle. When the premise cannot be ensured, that plate's
reads are skipped and the frame log says so; they are never filed as penal.

## Runs (contract C-G)

* `POST /api/v1/emulator/runs` `{use_case_id, seed?, t0_anchor?,
  clock_scale (≤ 3600), tick_sec (1–60, default 5), deliver_nats,
  deliver_twin}` ledgers the run and answers **202** `{run_id, status:
  "running", use_case_id}` at once; the run continues as a background task.
  A second launch of a use case that is already running answers **409**
  `{detail, run_id}` naming the running run; an unknown use case is 404.
* `GET /api/v1/emulator/runs/{id}` adds `status` (`running` | `completed` |
  `stopped` | `interrupted` | `failed`), `progress` `{frame, total,
  delivered, failed, skipped, late}`, `event_id`, `movement_id` and
  `event_status`; `?include_tape=false` is the light progress poll. A
  stolen-vehicle sweep's premise (below) is on the running row as
  `spec.scenario_precondition` from the moment it is settled — mid-run,
  not only in the finished run's document.
* `POST /api/v1/emulator/runs/{id}/stop` ends a run between frames (its
  corridor event is closed through C-F, the row finishes `stopped`);
  `POST /api/v1/emulator/runs/{id}/replay` is asynchronous like a run
  (202) — see **Replay** below for when it is refused.
* Every frame fires at wall time `t0 + t_sec / clock_scale` (late frames
  fire at once, flagged late), and everything a frame stamps is wall time,
  so nothing the emulator writes sits in the future.
* Delivery is honest: every frame ends `delivered` / `failed(reason)` /
  `skipped(reason)`. The corridor event is the run's purpose, so its create
  frame counts as delivered only once the event is **live**: if the oracle
  cannot take the t0 `active` transition, the activation is retried every
  `ACTIVATE_RETRY_SEC` (5 s) of wall time, and a run whose event never
  became active ends `failed`, never `completed`. A run that delivered
  nothing is `failed`.
* Shutdown interrupts every run (`interrupted`, migration 016); a run a
  killed process could not close is reaped on the next boot (a row whose
  owner stopped heart-beating for `STALE_HEARTBEAT_SEC`, 45 s), and its
  corridor event is closed through C-F.

**Identity is run-scoped.** A ledgered run's movement / corridor-event id
is `uuid5(use_case | seed | run:{run_id})` (`tape.movement_uuid`), and each
camera read is `uuid5(use_case | seed | camera | plate | run)`
(`tape.anpr_read_uuid`) — so every run is a fresh subject and a new run
reads afresh. A **replay** re-posts the STORED tape verbatim with a
re-anchored t0 (original ids included): the oracle's ingest is idempotent
on the read id, so a replay lands as duplicates and forks nothing.

**Replay** (`runner.replay_refusal`, `api.py`, `cli.py`). A replay
re-animates the run's event on the route its tape recorded, so the route
is checked first against the CURRENT road network (`corridor.road_graph`,
one-ways included):

* a legal path → **202**, a verbatim replay; the emulator's own movement
  row is re-drawn with its origin and destination at the ends of that
  route on today's roads (an upsert that moves them, not only the
  status);
* not a legal path — a run recorded before the WP-20 conversion put the
  roads on OpenStreetMap — → **422** `{detail, reason:
  'route_not_on_current_network', run_id, use_case_id, route_segment_ids,
  problems}` before anything is ledgered or re-animated; the detail says
  to start a new run of the use case, which is built on the current
  network. It is refused rather than re-routed: a re-routed tape would be
  a new run in disguise, its camera reads timed for roads it no longer
  takes;
* the route cannot be checked (the store does not answer) → **503**;
  409 keeps its single meaning, "that use case is running".

`python -m event_emulator.cli replay` applies the same rule and exits 2
with the same message. A replay is never the God's-eye REPLAY: the deck's
REPLAY only draws a stored tape on its own clock and writes nothing.

## Determinism & provenance

* A run = `{emulation_spec, seed, t0_anchor, clock_scale}`. The cause tape
  is a pure function of (spec, seed, t0): blake2b-derived seed + a
  uniform-stream RNG; two builds with the same inputs are byte-identical.
* Runs ledger: `emulator.emulation_runs` (migrations 009 + 016: spec_hash,
  spec, seed, t0_anchor, clock_scale, status, tape JSONB).
  `simulation.runs` (006) is never written by the emulator.
* Live injections ride the same APIs as a real feed and carry no emulator
  header; the provenance rides in the rows themselves (above — the
  showcase plane's rows on their own subject, labelled SYNTHETIC) and in
  the Valkey sidecar `emulator:source:{id}`, which the engine loop reads
  to label its inputs `source='emulator'`.
  Replays carry one more tell: `X-Emulation-Replay: <run_id>` on HTTP
  requests and NATS message headers.

## Cause vocabulary (closed)

`event_row`, `inputs_doc`, `scheduled_event` (six EventTypes, incl.
`emergency_vehicle`), `event_config` (five config multipliers +
`ambulance_rate_multiplier`), `weather_ramp`, `crowd_script`, and the two
watchlist kinds `anpr_read` and `agency_unit` (`tape.CAUSE_KINDS`).
`emergency_request` is deliberately not a kind: flood relief is priority
sub-event rows + scheduled `emergency_vehicle` +
`ambulance_delay_projection_sec` inputs.

## The living city (off by default)

`ambient.py` keeps Ahmedabad from being empty at rest: two to three
emulated causes hold stretches of road, two Jan Rakshak beats patrol, the
registered ANPR cameras read passing traffic, and roughly every 25 minutes
a 108 ambulance asks for a green corridor. The day program is a pure
function of (`EMULATOR_CITY_SEED`, IST date).

* **Off unless asked:** `EMULATOR_LIVING_CITY=1` starts it at boot
  (default off; an operator can also start or stop it from the deck —
  `GET/POST /api/v1/emulator/living-city`). The deck's citizen pages must
  label emulated events before it is switched on where the public can see
  it (`scripts/veer_up.sh --demo` checks this).
* `EMULATOR_CITY_SEED` picks the day program (default 1411);
  `EMULATOR_LIVING_VIOLATORS=0` stops its deliberate violators (one every
  15–19 minutes, each a PENDING recommendation in the human review queue;
  default on).
* 108 requests land as dispatch rows in `requested` — never approved by
  the emulator. A request still waiting for an officer when its slot
  closes is **re-filed** on the next tick onto an open slot, with a note
  naming the request it replaces, for up to 20 minutes after 108 first
  asked; the duty officer's queue never silently loses a pending request.
* A manual or flagship run pre-empts the slots on its roads; at its first
  session the city checks its sites and beats against the live network
  and fails loudly on a mismatch.

## Health

`GET /health` probes every seam for real (store `SELECT 1`, Valkey PING,
NATS PING/PONG, oracle `/health`) and answers `status` with
`degraded_reasons`, the library size, the running runs, the last run, the
living city and the reaper's last pass. It is `degraded` whenever a seam is
down or the last run that ended on its own failed.

## Configuration and ports

| Setting | Default (`config.py`) | Notes |
|---|---|---|
| `HOST` / `PORT` | `127.0.0.1` / `8090` | loopback by default: the service writes events, sightings and Valkey docs, so it never answers on a venue LAN. The deck reaches it through its same-origin `/api/emulator` proxy. |
| `VVIP_DATABASE_URL` | none (env-only) | runners fail closed without it; credentials are never in code |
| `VVIP_VALKEY_URL`, `VVIP_NATS_URL` | set by the stack | the argus stack passes `redis://127.0.0.1:6380` and `nats://127.0.0.1:4223` (`infra/compose.argus-override.yml`) |
| `EMULATOR_TRAFFIC_ORACLE_URL` | set by the stack | the argus stack passes `http://localhost:8091`; validated by `assert_internal_endpoint` (loopback / `host.docker.internal` only) |
| `EMULATOR_DECK_ORIGINS` | the deployed deck `:5273` (+ the vite dev-server origin) | CORS for the dev server; comma-separated `http(s)://host[:port]`, fail closed on anything else |
| `EMULATOR_LIVING_CITY`, `EMULATOR_CITY_SEED`, `EMULATOR_LIVING_VIOLATORS` | off, 1411, on | the living city, above |

In the argus stack the emulator is the compose service
`vvip-argus-event-emulator` (host network, loopback :8090, `restart:
unless-stopped`), brought up by `scripts/veer_up.sh`.

## Security (outbound HTTP + SQL)

* Scheme http/https only; **URLs from external input** are host-validated
  before any request — localhost, loopback, private, reserved, link-local,
  multicast and unspecified addresses are all rejected (`safe_http.py`,
  choke-pointed in `SafeHttpClient`; DNS-resolved addresses are checked,
  defeating rebinding to private IPs). No URL-valued field is accepted on
  the HTTP surface.
* **Fixed internal endpoints are config constants** enforced by
  `assert_internal_endpoint` — an env override cannot redirect the oracle
  seams at an arbitrary host.
* All SQL is parameter-bound (`%s` placeholders); no f-string, format or
  concatenated SQL exists anywhere.
* Ethics (C6): vehicles only — plates, never a person, face or frame.

## Run it

```bash
# the argus stack runs it as a container: scripts/veer_up.sh
# by hand (loopback :8090):
VVIP_DATABASE_URL=postgresql://corridor_admin:…@127.0.0.1:56434/corridor_db \
VVIP_VALKEY_URL=redis://127.0.0.1:6380 VVIP_NATS_URL=nats://127.0.0.1:4223 \
EMULATOR_TRAFFIC_ORACLE_URL=http://127.0.0.1:8091 \
  python -m event_emulator

# CLI
python -m event_emulator.cli list
python -m event_emulator.cli inject rath_yatra_ahmedabad --clock-scale 60
python -m event_emulator.cli inject flood_evacuation_corridor --dry-run \
    --t0 2026-10-12T06:00:00+00:00 --tape-out tape.json
python -m event_emulator.cli replay 2 --clock-scale 60   # exit 2 when refused (Replay, above)
python -m event_emulator.cli show 2
```

## Tests

`pytest tests/` — **328 tests** collected by the gate venv on this tree
(`scripts/verify_all.sh` runs the suite): security (safe HTTP client),
determinism and the tape, runner delivery and activation retries, the
async task registry and reaper, the NATS seam and its wire labels, the
watchlist reads and the stolen-FIR premise (`test_sweep_earmark.py`), the
on-road geometry — route orientation (`test_route_geometry.py`), the
midpoint anchors (`test_store_unit.py`) and movements that start and end
at their route's ends (`test_runner.py`) — the replay refusal
(`test_api.py`), the living city,
signal-phase parity with the oracle's own clock, and the store (the
PostgreSQL-backed tests run only against a scratch clone named by
`EMULATOR_TEST_PG_DSN` — never the live store — and skip otherwise).

## Declared boundaries & fidelity limits

* Reverse-flow lanes are approximated as `partial_closure` + directional
  demand (a true direction flip is an additive twin follow-on).
* Interior pedestrian dynamics are not modelled — only their vehicle-side
  pressure via `crowd_score`.
* The twin POST's additive cause fields (`weather`, `event_overrides`,
  `forced_events`) ride in the request body; the delivery level the server
  accepted is recorded per run.
* The emulator does not create convoy-brain movement contexts (Valkey
  `convoy:context:*`) — that is the brain's own API surface; emulated
  events are engine-side causes.
