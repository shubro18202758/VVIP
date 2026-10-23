# Active Movement Adaptation (AMA) — the named R9 module

**Status:** named module (R9) — this document *surfaces* the existing
adaptive layer; it introduces no behavior change. The module card on the
`/ops` board (`AMAModuleCard.svelte`) and the machine-readable descriptor
at `GET /api/v1/adaptive/module` render exactly what is written here.

## What the module is

Active Movement Adaptation is Veer's live self-adjusting layer: every
active corridor event (convoy, procession, mass gathering, pursuit …) is
continuously classified into a **situation band**, the band moves through
**hysteresis** before anything changes, **protocol floors** bound every
published parameter, and any non-baseline plan must first pass a **twin
shadow-evaluation** — failing closed to baseline. Every observation and
every published parameter set is persisted to an audit trail.

Code home: `services/traffic-oracle/src/traffic_oracle/adaptive/`
(16 modules beside `__init__.py`, whose docstring maps the core).

## The six stages

| # | Stage | Where | What it does |
|---|-------|-------|--------------|
| 1 | **Situation** | `situation.py`, `inputs.py` | Classifies each active subject from live feed scores (congestion, weather, crowd, anomaly, GPS age) into GREEN / AMBER / RED / BLACK. |
| 2 | **Hysteresis** | `hysteresis.py` | A class change must persist through its dwell window before adoption — bands never flicker. |
| 3 | **Protocol floors** | `policy.py` (generated table `protocol_params_gen.py`) | `advance_closure_sec` floors **Z+ 180 / Z 120 / Y 60 s** (X and `none`: none) are applied to *every* publish, baseline included. |
| 4 | **Twin shadow-eval** | `shadow.py`, `twin.py` | Any deviation from baseline must beat the baseline in a paired digital-twin run (disruption + ambulance-delay tolerances) or the engine **fails closed** to baseline. |
| 5 | **Audited persistence** | `persistence.py`, `valkey_mirror.py` | Every observation → `adaptive.situation_log`; every publish → `adaptive.param_overrides`; params mirror to Valkey (TTL 30 s) for the decider. |
| 6 | **City signal plans** | `signals/` (011) | The **city band** (most severe active subject) drives the gated city signal-plan writer over `corridor.signal_programs` — see below. |

## Stage 6 — corridor → city (the policy application extension)

The adaptive layer originally applied its policy per corridor movement.
Stage 6 extends the application points to **city scope**:

* **City band** = the most severe band across active subjects
  (`signals/plans.py::city_band_from_status`, pure).
* **Band → plan table** (deterministic, documented, single source:
  `signals/plans.py::BAND_PLAN_TABLE`):

  | band | priority_share (of cycle moved to the priority phase) | cross_green_floor_sec |
  |------|----------------------------------------------------------|------------------------|
  | GREEN | 0.00 — no plan is written | — |
  | AMBER | 0.06 | 18 |
  | RED   | 0.12 | 14 |
  | BLACK | 0.18 | 10 |

  Cycle lengths and offsets are **never** changed (the 006 CHECKs and
  progression offsets hold trivially); only the green split shifts, and
  no cross phase ever drops below its floor. BLACK at an interception
  junction *is* the "hold cross-traffic" posture, expressed as a bounded,
  reversible split change.

* **Writer safety (contract C7)** — the writer is **default OFF**
  (`VVIP_SIGNAL_POLICY_WRITER`, unset/0); every apply is
  operator-triggered (`POST /api/v1/signals/plans/apply`); every publish
  is twin shadow-evaluated **before** the write and fails closed; the
  first calibrated write per junction snapshots the prior program into
  `corridor.signal_program_history`, and
  `POST /api/v1/signals/restore` rebuilds the `osm_assumed` rows; an
  in-process reader/writer gate (`signals/gate.py`) excludes writer
  commits while any twin simulation / shadow-eval is in flight.
* **Per-junction RL is untouched** — the reinforcement learner
  (`optimize/rl`) remains corridor-scoped by ruling; the city writer
  never reads or writes its state.

## HTTP surface

| Endpoint | Purpose |
|----------|---------|
| `GET /api/v1/adaptive/params` | C4 parameter response (per movement) — **read-only**: the subject's last published decision, or the floor-clamped baseline (`evidence.read_only: true`); a reader never creates an audit row, a mirror write or subject state |
| `GET /api/v1/adaptive/status` | degradation + per-subject situations + the city band and `city_band_since` + engine-loop health (`last_tick_age_sec`, `consecutive_errors`, per-subject input ages) + twin calibration state |
| `POST /api/v1/adaptive/observe` | feed one observation (tests / explicit feeds); persisted only when the decision changed |
| `GET /api/v1/adaptive/situation_log?event_id=&limit=` | band audit history (board primary rung), each row carrying `simulated_feed` / `inputs_data_quality` (C-Q) |
| `GET /api/v1/adaptive/overrides` | the `adaptive.param_overrides` audit rows, each with `evidence.publish_reason` |
| `GET /api/v1/adaptive/module` | this module card, as data (R9) |
| `GET /api/v1/signals/state` | per-junction active phase / green ratio (C1) + city-band provenance (`city_band_since`, `_stale`, `_stale_since`, `_simulated_feed`) |
| `GET /api/v1/signals/plans` | writer posture + last plan + band→plan table + the same city-band provenance |
| `POST /api/v1/signals/plans/apply` | operator-triggered gated apply; with no explicit band while the city band is unknown (engine poll failing) → **409** `city band unknown — pass band explicitly`, never a silent GREEN |
| `POST /api/v1/signals/restore` | rebuild `osm_assumed` from snapshots |
| `GET /api/v1/signals/history` | snapshot/restore ledger |

## Where it is rendered

* `/ops` board — `AMAModuleCard.svelte` (module card: stages, floors,
  live city band, fail-closed chip, writer posture) and
  `SituationBandPanel.svelte` (band history sparkline).
* God's-eye canvas — the AMA band chip, and the City Vitals HUD's city
  posture line (the band, how long it has held and since when in IST;
  `UNKNOWN` with the reason while the engine poll is failing).
* `/` Convoy Command — the same band on the cockpit's threat ladder.

## Behaviours that keep the module honest (waves 1–3)

* **Publish only on change, with a reason.** A decision writes ONE
  `adaptive.param_overrides` row when it changed, labelled
  `evidence.publish_reason` — `first_publish`, `class_change`,
  `param_change`, `verdict_change`, `escalation_change` or
  `degradation_change` (`service.py`). Between changes the loop re-sets the
  same 30 s mirror payload every 10 s, so the mirror lapses only when the
  loop dies; a failed audit write is retried, and one subject's decisions
  never interleave.
* **The twin must be field-calibrated before anything deviates from
  baseline.** Calibration needs ≥ **24 distinct field hours** of REAL hourly
  aggregates and a replay MAE ≤ **5.0 km/h** (`simulation/calibration.py`);
  synthetic rows never count (they live in their own rollup table,
  migration 019). At startup the oracle awaits a bounded calibration
  **prime** before the engine's first decision, so the first publish
  already quotes the true refusal reason. On a fresh install there are no
  field hours, so every non-baseline plan — and the city signal writer —
  fails closed with `twin_not_calibrated: no field observations — simulated
  feed only` (`/health` → `twin_calibration.status:
  waiting_for_field_data`). That refusal is the designed posture, shown on
  the module card, never hidden.
* **A hung tick is abandoned and the band goes stale, not quiet.** A tick
  that hangs is abandoned after **30 s** and counts as a failed poll; while
  polls fail the city band is marked **stale** (unknown, not at rest —
  `city_band.stale`, `city_band_stale_since`), and a subject whose inputs
  are missing for 300 s is evicted. A completed or cancelled event is
  evicted on the next tick, so it can never hold the city band.
* **Same-provenance inputs.** The event emulator's traffic lives in its own
  key space (`traffic:emulated:latest:{segment}`): a LIVE event's band reads
  only the live plane, an EMULATED event prefers its scenario's rows, and
  every audit row says which (`inputs_origin`, `simulated_feed`) — emulated
  traffic can never drive a real event's band.
* **Real roads under it.** The bands, the twin's shadow runs and the signal
  plans all run on the city network traced on real OpenStreetMap roads
  (49 segments, 34 junctions, 11 signal-programmed junctions;
  `TECHNICAL_DOCS.md` §16.1).
