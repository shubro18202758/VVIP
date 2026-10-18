# Veer — Idea Submission Deck (7 slides)

*Hack for Social Cause · Viksit Bharat Young Leaders Dialogue 2027*
*Team: ________ · State: Gujarat · Themes: Governance & Civic Technology
(primary) · Disaster Mitigation & Large Gatherings (secondary)*
*(Rendered to PPT/PDF ≤10 MB as `submission/Gujarat_Veer_Deck.pptx` / `.pdf`
— team name "Veer" per `PROBLEM_STATEMENT_GUJARAT.md`; rename per
`<State>_<TeamName>_Deck.pdf` if the registered team name differs. Slide-5
numbers are pulled live from `VVIP/scripts/impact_results.json` by
`submission/make_deck.py`.)*

---

## Slide 1 — When a corridor closes, an Indian city holds its breath

> We built the intelligence that lets it exhale — corridors managed to the
> second, not frozen for half an hour. And not just for convoys: **a
> city-scale platform where VVIP routing is one component among many.**

- Today's default: a protected movement freezes entire corridors for
  **15–45 minutes per junction**, coordinated by **phone, radio and WhatsApp
  across five separate hierarchies** (traffic police, district admin,
  SPG/state security, municipal signals, 108 dispatch) — none sharing a live
  picture. The passage itself takes seconds per junction.
- The harm is documented: ambulance held 5–10 min with siren running during a
  Union Home Minister's convoy (Mumbai 2022); a heart-attack death after a
  ~20-minute ambulance hold (Kanchipuram 2018); another ambulance halt for a
  VIP movement this February (Agartala). Commuters — Ahmedabad's own papers
  have run this story since 2009. Even the Gujarat CM's convoy has been
  stuck ~15 minutes in this same city (2022).
- **The protocol is not the villain.** The MHA Blue Book itself says traffic
  is to be *"regulated, not stopped."* The gap is tooling: paper protocol,
  phone-and-radio coordination.
- **And it is one failure mode in many costumes.** The same freeze — a
  corridor whose occupancy nobody can schedule, predict and release — hits
  the Rath Yatra's 14 km old-city loop (148 annual editions), Kumbh-class
  perimeters (240→660 million visits, 2019→2025), the 24,000-runner
  riverfront marathon, Republic Day parade strips, ~130,000-person stadium
  egress at the world's largest cricket ground, monsoon flood evacuations
  (Gujarat 2024: 28+ dead, 24,000+ relocated) and months-long metro work
  zones. **Our answer is a city-scale Corridor OS — the VVIP run is the
  base case, not the boundary** (full library: `VVIP/docs/USE_CASES.md`) —
  and the same backbone now carries everyday enforcement: a stolen-vehicle
  FIR traced from watchlist to interception plan, with a human in every
  penalty loop (`VVIP/docs/GODS_EYE_INTEGRATION.md`).

## Slide 2 — The gap, precisely (state problem statement)

Pilot geography: the Ahmedabad–Gandhinagar corridor (~25 km; the exact
40-segment, 14-junction network our prototype seeds — Ashram Road, SG
Highway, CG Road, SP Ring Road, Gandhinagar Highway, the old-city bazaar
belt, Motera Stadium Road).

| # | What does not exist today | Consequence |
|---|---|---|
| G1 | No shared operational picture across the 5 agencies | 15–45 min blanket holds |
| G2 | No prediction in the loop (fixed advance margins) | Holds far longer than the convoy needs |
| G3 | **No citizen advisory channel** | The commuter discovers the closure at the barricade |
| G4 | **No emergency-passage request channel for 108** | Golden-hour minutes burn; documented deaths |
| G5 | No measurement or accountability | Nothing improves; over-caution every time |

Beneficiaries (named): the daily-wage commuter on Ashram Road; the cardiac
patient in a 108 ambulance (Gujarat: ~585 EMRI ambulances, ~5 emergency trips
each daily); the constable absorbing public anger at the barricade; the duty
officer satisfying Blue Book security with zero tooling — and, through the
use-case library, the pilgrim, the runner, the spectator and the flood
evacuee on the same corridors.

## Slide 3 — The platform: one city-scale OS, the public's protagonist

Same engine the operators run — **but the Very Important Persons are the
Voter on the bus, the Victim in the 108 ambulance, and the Vulnerable
patient for whom ten minutes matter.**

- **Multi-agency and event-agnostic by construction:** a generalized event
  model (`events.corridor_events`) expresses any corridor operation on five
  axes — protection class (Z+/Z/Y/X/**none** — *none* = a public event with
  no protectee), crowd class, event kind (protected movement, procession,
  mass gathering, race, ceremonial, egress surge, emergency evacuation,
  work zone — **plus the enforcement family's `crime_incident` and
  `vehicle_pursuit`**), footprint, phases. The kind selects **closure
  templates, never code paths** — a Rath Yatra at 8 km/h is managed by the
  same forecaster, router and audit trail as a Z+ run.
- **Predictive rolling holds:** traffic forecasts (DSTGAT architecture,
  T+5/10/15/30; heuristic forecasts run until trained weights are exported)
  trigger closures **60–180 s** before the corridor user reaches a junction
  and release as it passes — **~2–3-minute holds instead of 25-minute
  freezes**, which is exactly what the Blue Book asks for. For public
  events the same machinery chooses the gentler policy — in the flagship
  run the engine's own answer for a procession + monsoon + ambulance was
  **signal hold at 45 s: "regulated, not stopped."**
- **A two-tier AI command mind, at the edge:** a 27B strategic *decider*
  owns strategy and tool selection; a Gemma 4 12B *tactical* tier drafts
  arguments and writes every operator-facing word. It never runs per GPS
  tick — checkpoints fire only on real events, and every answer is logged
  with its probability distribution and confidence. The security veto stays
  human. Runs on one GB10-class edge box — the motorcade command vehicle's
  form factor.
- **The God's-eye canvas — one live 3D picture for every agency:** a
  keyless CesiumJS city (OSM imagery + our own 3D entities; no cloud
  dependency) rendering corridor events, earmarked vehicle traces, the
  signal network, agency units and cameras as integrity-validated layers,
  with FLIR-class sensor modes and share links that reproduce the exact
  view. If the shared picture *is* the platform, this is it
  (`VVIP/docs/GODS_EYE_INTEGRATION.md`).
- **The enforcement family, on the same rails:** stolen-vehicle FIR →
  lawful-basis earmark (retention capped) → ANPR camera reads →
  deterministic e-challan recommendation → **duty-officer approval** →
  operator-initiated pursuit → pgRouting interception plan. Vehicles only
  — no person-tracking — and Jan Rakshak, police, traffic, 108 and fire
  are dispatchable agencies through the same human-approved, audited
  channel.
- **Active Movement Adaptation (AMA)** — the named self-adjusting module
  under it all: every active event is continuously classified into a
  situation band, bands move through hysteresis, protocol floors bound
  every published parameter, and any non-baseline plan must first beat the
  baseline in a twin shadow-evaluation — **failing closed** to baseline.
- **Citizen Advisory Layer:** free multilingual corridor alerts
  (English/हिंदी/ગુજરાતી) — which stretch freezes, when, for how long, and
  the alternate — via a light public web page with SMS/WhatsApp opt-in.
- **108 Green-Corridor mode:** the driver's one-tap request → platform
  evaluates the live corridor gap → duty officer approves/defers → window
  with countdown and a minutes-returned-to-golden-hour counter.

## Slide 4 — Working now (demo, honestly)

- **3-minute browser demo, no GPU, no backends:** `npm install && npm run dev`
  — the operator console runs a scripted full escort with rolling congestion,
  diversions and AI reasoning panels; the citizen advisory page and the 108
  driver view render the same live corridor state.
- **The flagship showcase — "God's Eye Morning" — runs on the live stack**
  (`scripts/showcase_run.sh`, re-executed for this revision: **exit 0,
  SHOWCASE PASS**): one composite morning is injected through the event
  emulator into the running 7-container fleet — Rath Yatra + monsoon burst
  + one 108 ambulance **+ a stolen-vehicle report (FIR 4401/2026, Kalupur
  PS)** — and the real engine answers everything at once: **36 audited
  decisions this run** (30 situation + 6 parameter), 3 band transitions,
  1 ambulance-delay escalation, **4 green-corridor windows**, a 12-replication
  twin with ambulance delay 7.63 s (model output) — and the **full pursuit
  chain**: plate earmarked on a lawful basis (30-day retention) → 4 emulated
  ANPR camera reads (107–112 km/h, every row labelled
  `source=emulated_anpr`) → deterministic e-challan recommendation filed
  PENDING → **approved by Duty Officer R. Mehta** (append-only audit tail)
  → operator-opened pursuit event → **9 choke junctions ranked** by
  pgRouting off the latest sighting → live trace on the God's-eye canvas.
  A **Jan Rakshak unit** is dispatched to the spillover (requested →
  approved → en_route → on_scene, audited), and the city signal-policy
  guard **refuses** an uncalibrated commit — fail-closed by design, the
  refusal itself audited. Screenshots (pixel-proven non-blank): God's-eye
  3D city, pursuit trace, citizen advisory.
- **The emulated/real split, stated plainly — and extended to ANPR:** the
  emulator injects *causes only* — procession schedules, weather onset,
  incidents, green-corridor requests, camera reads — through the real
  stack's real APIs. Every ANPR row is born labelled `emulated_anpr`
  (the `live_anpr` label is reserved for a real vendor feed behind the
  frozen `AnprSource` port; nothing writes it in this build); dispatched
  unit positions are deterministic, `emulated: true` projections. The
  emulator never produces outcomes: every decision, band transition,
  window — and every pending→approved penalty transition — quoted above
  was made by the real decision stack or a named human officer. Replay
  provenance is header-tagged; live injections are indistinguishable from
  a real feed at the API boundary — which is exactly why the demo numbers
  are measurements, not animations.
- **Measured-model impact** (`IMPACT_REPORT.md`, deterministically
  regenerated on the repo's own seed network): junction-freeze commuter
  delay falls **25×–506× (central 100×)** under the stated assumptions
  ledger — structural (delay ∝ hold²), so even the conservative bound
  clears the original unmeasured "6×" we no longer lead with. Emergency:
  ~33,000 golden-hour minutes/year returned on the pilot corridor's
  modelled exposure.
- **Under the hood — built, with tests; only measured numbers quoted:**
  the two-tier brain (27B decider + Gemma 4 12B + dual embedders) resident
  on a 121 GiB GB10 edge box. Bench re-executed by this revision's
  documentation pass (`scripts/bench_results.json`, exit 0): decider
  **546.42 tok/s, p99 866.0 ms**; Gemma 23.29 tok/s; embeddings 85.9 ms;
  62.73 GiB resident; adversarial decision suite **19/19 passed (score
  100)**, avg 25.98 s / max 47.6 s per scenario (the suite grew 14 → 19
  with five watchlist/enforcement scenarios). **All three gates
  re-executed green by the documentation pass itself:** `verify_all.sh` —
  ALL TIERS PASSED, exit 0 (frontend e2e + the two gods-eye canvas tiers
  + 875 pytest across five services, incl. the emulator's 93 and
  traffic-oracle's 453); `showcase_run.sh` — exit 0, SHOWCASE PASS;
  `bench_stack.sh` — exit 0, BENCH OK. Kept visible: open quality gaps
  tool coverage **0.684** and keyword coverage **0.548** (pass the gate,
  tracked; keyword coverage moves run to run — reported as measured), and
  a same-day bench run with one 69.99 s scenario that still passed (the
  60 s line is scored, not a hard gate; that run's report was overwritten
  by the re-run, so the figure is documented history rather than an
  on-disk artifact). Implemented, awaiting
  trained weights — the DSTGAT neural forecaster ships as PyTorch
  architecture only, on synthetic seed data. Also real: Rust/Arrow/NATS
  streaming, PostGIS/pgRouting corridor graph, OR-Tools router, the
  generalized events schema (migrations 007–011, applied live 11/11).
  Simulation-labelled, never faked.

## Slide 5 — Impact for Viksit Bharat 2047 (from the impact run + the use-case library)

- **Per movement (central case, modelled):** commuter delay on the corridor
  drops from ~571,000 to ~5,700 commuter-minutes across 14 junctions;
  vehicles caught in holds fall from ~30,450 to ~3,045.
- **Per year on the pilot corridor** (250 movement-days — RTI-able
  constant): ~2.35 million commuter-hours returned; ~1.27 million litres of
  idle fuel and ~3,179 tonnes of CO₂ avoided (modelled).
- **Health:** ~33,000 golden-hour minutes/year returned (12 intersecting
  trips/day × 11 min) — minutes returned to the golden hour, never "lives
  saved"; exactly what the documented holds (5–20 min) and the model support.
- **One engine, eight corridor classes + the enforcement family — the
  national calendar argument** (`VVIP/docs/USE_CASES.md`, each with
  documented reality and a runnable scenario in the engine today):
  protected movements (the base case) · the 148-edition Rath Yatra ·
  Kumbh-class mass gatherings (240→660 M visits) · World Athletics-calendar
  marathons · the two national ceremonial days · ~132,000-seat stadium
  egress · monsoon flood evacuation (the Gujarat 2024 class — the Disaster
  Mitigation lock) · published metro/bullet-train work zones — plus the
  stolen-vehicle watchlist-to-interception chain on the same backbone. A
  tool that manages only convoys is protocol; one that expresses all of
  this is municipal infrastructure.
- **Accountability (SDG 16):** after-action data per event reaches planners
  for the first time; every AI decision logged with distribution +
  confidence; every penalty a human-approved recommendation with an
  append-only audit trail; **vehicles-only enforcement with lawful basis
  and capped retention** — no person-tracking; protectee
  identities/destinations never logged.
- SDG 11 (sustainable cities) · SDG 3 (golden hour) · SDG 16 (accountable
  institutions) · SDG 13 (flood-response resilience).

## Slide 6 — Team, adoption, sustainability

- **Team (≤3):** [Name 1 — platform/ML] · [Name 2 — civic layer/outreach] ·
  [Name 3 — operations/impact], [institution].
- **Who runs it after the hackathon:** a Smart City SPV / city traffic
  police pilot on one corridor; BOM is one GB10-class edge box (the browser
  demo runs on any laptop); data partnerships: city ITS feeds + 108/EMRI
  dispatch. Open source (MIT) — any state can fork.
- **Roadmap:** T+1 mo: wire the citizen page to the live services behind
  the demo · T+3: pilot design with one city + 108 dispatch; lift the two
  open suite quality gaps (tool coverage 0.684, keyword coverage 0.548);
  land the cameras backend for the God's-eye canvas layer and the first
  live `AnprSource` adapter against a real vendor feed · first trained
  DSTGAT weights onto real feed data · T+6: two more languages;
  event-corridor preset for a real gathering (Rath Yatra / match night)
  with the city.
- **Costs:** the intelligence runs at the edge; marginal cost per movement
  ≈ zero; alerts ride existing SMS/WhatsApp rails.

## Slide 7 — Disclosure of AI-tool usage & references

- **AI usage disclosure (mandatory):** the base prototype and our
  enhancements were developed with the help of AI coding assistants
  (including agent sessions used to build the model stack, the
  citizen/ambulance surfaces, the event emulator, the God's-eye canvas,
  the enforcement/watchlist chain, the impact harness and documentation).
  The decision *tools* are deterministic, team-reviewed code (forecaster,
  optimizer, classifier, simulator, e-challan rules); the two-tier
  reasoning layer (27B decider + Gemma 12B) is logged answer-by-answer with
  probability and confidence, and no AI output acts unreviewed in the
  decision path — the security veto is human by design, and so is every
  penalty: recommendations are filed PENDING and only a named duty officer
  can approve. The demo runs a clearly-labelled scripted simulation in the
  browser; the flagship showcase injects emulator-generated *causes* into
  the real stack (the split stated on slide 4 — camera reads are labelled
  `emulated_anpr` at the row level, unit positions are labelled emulated
  projections); every statistic on these slides comes from the cited
  sources, the falsifiable assumptions ledger of `IMPACT_REPORT.md`, or
  the in-repo measurement artifacts (`scripts/bench_results.json`,
  `showcase/showcase_results.json`, `showcase/pursuit_chain.json`) —
  model outputs are labelled as modelled, always.
- **References:** MHA Blue Book protocol summary; Free Press Journal (Mumbai
  ambulance hold, Sept 2022); Kanchipuram 2018 case; Agartala Feb 2026;
  Times of India Ahmedabad (2009; riverfront-run diversions May 2024);
  Vibes of India (2022 CM convoy); GVK EMRI Gujarat fleet/trip figures;
  NDTV (26 Jun 2025) & utsav.gov.in (148th Rath Yatra); Gujarat Samachar
  (Jul 2026, 35-drone monitoring); Wikipedia/UP government (Kumbh
  attendance 2019→2025); ahmedabadmarathon.com, Olympics.com, ThePrint
  (2024 marathon); Gujarat CMO (76th Republic Day, Jan 2025); Wikipedia +
  Ayen News (Motera capacity / IPL 2026 final); India Today (8 Mar 2026, T20
  WC final deployment); BBC/NDTV/ReliefWeb (Gujarat floods, Aug 2024);
  PIB + Indian Express (metro Phase 2 approvals, Kankaria Road closure);
  MoRTH ITS / Smart Cities Mission / PM Gati Shakti; MyBharat HSC 2027
  brief. Full lists: `PROBLEM_STATEMENT_GUJARAT.md` §6 and
  `VVIP/docs/USE_CASES.md` (Sources). In-repo technical references:
  `VVIP/TECHNICAL_DOCS.md`, `VVIP/docs/GODS_EYE_INTEGRATION.md`,
  `VVIP/docs/ACTIVE_MOVEMENT_ADAPTATION.md`.
