# Active Movement Adaptation (AMA) — the module the band panel surfaces

The `adaptive` package under
`services/traffic-oracle/src/traffic_oracle/adaptive/` IS the Active Movement
Adaptation module (R9). This document names its stages and where each one
becomes visible — the canvas and the /ops panel SURFACE the module; they do
not re-implement any of it. The city-canvas stream adds no Python.

## The five stages (per movement)

1. **Situation** — `engine_loop` classifies each movement from its valkey
   inputs doc (`adaptive:inputs:{event_id}`, fed by the emulator's NATS
   tape) into GREEN / AMBER / RED / BLACK.
2. **Hysteresis** — `hysteresis.py` holds a band through transient
   oscillation so the city plan doesn't flap.
3. **Policy floors** — `policy.py` clamps the escalation parameters to the
   protocol floors (`ADVANCE_CLOSURE_FLOOR_SEC`: Z+ 180 s / Z 120 s / Y 60 s
   at `policy.py:82-89`); the floor is never softened, only applied.
4. **Twin shadow-eval (fail-closed)** — `shadow.py`/`twin.py` simulate the
   proposed plan; anything that fails or diverges closes DOWN, never up.
5. **Audited persistence** — outcomes land in `adaptive.situation_log` and
   `adaptive.param_overrides` (migrations 005/007) with the shadow-eval
   verdict; the /ops audit stream tails them.

## Where it surfaces (this stream's deliverables)

| Surface | What it shows |
| --- | --- |
| `/ops` SituationBandPanel | re-captioned **Active Movement Adaptation** (EN/HI/GU) with the five stages in the sub-caption; band pill, sparkline, shadow-eval + floor evidence chips |
| `/gods-eye` HUD **AMA chip** | current band, last-transition age, and the source rung (`situation_log` primary → `adaptive/status` fallback) — fed by the same ops-store band ladder |
| `src/lib/ops/store.svelte.ts pollBand()` | the ladder itself (pre-existing): `GET /api/v1/adaptive/situation_log` → `GET /api/v1/adaptive/status` → `/adaptive/params` evidence |

## Endpoint honesty (live state, 2026-10-13)

- `GET /api/v1/adaptive/status` → **200** (verified live this session) —
  the AMA chip keys off this rung today.
- `GET /api/v1/adaptive/situation_log` → **404** today; the signalling
  stream adds it as a read-only endpoint (contract C1). When it lands, the
  chip's source label flips from `adaptive/status` to `situation_log` with
  no canvas change — the ladder prefers it automatically.
