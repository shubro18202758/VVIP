# Active Movement Adaptation (AMA) — the module the band panel surfaces

The `adaptive` package under
`services/traffic-oracle/src/traffic_oracle/adaptive/` (with the city
signal-plan writer in `signals/`) IS the Active Movement Adaptation module
(R9). This note names its stages and where each one becomes visible — the
canvas and the /ops panels SURFACE the module; they re-implement none of it.
The full module description is `docs/ACTIVE_MOVEMENT_ADAPTATION.md`; the
oracle serves the same card as data at `GET /api/v1/adaptive/module`.

## The six stages

1. **Situation** — the engine loop classifies each active subject (corridor
   event or movement) from its inputs — an `adaptive:inputs:{id}` doc, else
   the live traffic cache along its CURRENT route spine — into GREEN / AMBER
   / RED / BLACK. A live event reads `traffic:latest:*` only; emulated
   traffic (`traffic:emulated:latest:*`) can drive only an EMULATED event.
2. **Hysteresis** — `hysteresis.py`: a class change must persist through its
   dwell window before it is adopted, so bands never flicker.
3. **Protocol floors** — `policy.py` applies `advance_closure_sec` floors
   (Z+ 180 / Z 120 / Y 60 s; X and `none` none) to every publish, baseline
   included; a floor is never softened.
4. **Twin shadow-eval (fail-closed)** — `shadow.py` / `twin.py`: any
   deviation from baseline must beat the baseline in a paired twin run, or
   the engine falls back to baseline.
5. **Audited persistence** — every observation lands in
   `adaptive.situation_log` with where its inputs came from; a parameter
   decision is written to `adaptive.param_overrides` only when it CHANGED,
   labelled `evidence.publish_reason` (`first_publish` | `class_change` |
   `param_change` | `verdict_change` | `escalation_change` |
   `degradation_change`); the Valkey mirror `adaptive:params:{id}` (TTL
   30 s) is kept alive in between.
6. **City signal plans** — the city band (the most severe tracked subject)
   drives the gated city signal-plan writer (`/api/v1/signals/plans`, writer
   default OFF, every apply operator-triggered and shadow-evaluated).

`GET /api/v1/adaptive/params` is READ-ONLY: it serves the subject's last
published decision (or the class's floor-clamped baseline) and never writes
an audit row, never touches the mirror and never creates subject state.

## Where it surfaces

| Surface | What it shows |
| --- | --- |
| `/ops` SituationBandPanel + AMAModuleCard | band pill, sparkline, the module card with its stages, floors, fail-closed chip and writer posture (EN/HI/GU) |
| `/gods-eye` HUD **AMA chip** | the band and the age of its newest READING (STALE when old), from the same ops-store band ladder |
| City Command vitals (`hud/CityVitals.svelte`) | city posture, aged from its newest reading, with a SIMULATED FEED pill when the band stands on simulated inputs |
| `src/lib/ops/store.svelte.ts` band ladder | the SELECTED live event's `situation_log` → else the city band from `GET /adaptive/status` (band, the moment it was entered, simulated feed, stale) → else none |

## What the band says about itself (C-Q)

The city band carries `city_band_since` (when it was ENTERED, not when it was
last read), `city_band_simulated_feed` (it stands on the simulated feed or
emulated inputs) and `city_band_stale` / `city_band_stale_since` (the engine
poll is failing — a hung tick is abandoned after 30 s — so the band is
UNKNOWN, never "at rest"). Situation-log rows carry `simulated_feed` the same
way. The deck renders a stale band as UNKNOWN and a simulated one with its
SIMULATED FEED label — never as a field measurement.

## Endpoint honesty (a fresh install of this tree, 2026-10-23)

`GET /api/v1/adaptive/module`, `/adaptive/status`, `/adaptive/situation_log`,
`/adaptive/overrides` and `/adaptive/params` all answer **200**. On a city at
rest the log and overrides are honestly empty (`count: 0`), the city band is
GREEN with its `since`, and `/adaptive/params` answers the baseline with
`evidence.read_only: true`.
