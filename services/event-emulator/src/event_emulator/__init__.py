"""Cause-tape event emulator for the Veer corridor stack (the emulated half).

The emulator injects ONLY exogenous causes — procession schedules, marathon
waves, weather onset, vehicle incidents, emergency green-corridor requests,
convoy departures, ANPR camera reads and patrolling agency units — and is
structurally forbidden from producing outcomes (decisions, params, situation
bands, e-challans, approvals).  The REAL stack plans, classifies, escalates
and degrades on its own; a named human officer makes every decision.

Seams (each drives a real API of the running stack — runner.py):

* event_row — the corridor event (+ the legacy convoy.movements row, its
  route line the segments' routing lines chained in the direction of
  travel and its origin / destination that line's ends — on the road)
  with its creation ledger row, then 'active' at t0 and 'completed' at the
  horizon through POST /api/v1/events/{id}/status (C-F, actor
  'event-emulator (system)').  A run whose event never went live (the
  activation is retried every ACTIVATE_RETRY_SEC) ends 'failed'.
* inputs_doc — Valkey ``adaptive:inputs:{id}`` in the engine loop's exact
  reader shape, plus synthetic Arrow batches on ``corridor.traffic.emulated``
  (NEVER ``.live``; the oracle keeps them in ``traffic:emulated:latest:*``);
  every row is labelled data_quality SYNTHETIC (2), the congestion-outlier
  marker ANOMALOUS (1), never REAL (0), and sits at its segment's on-road
  midpoint (ST_LineInterpolatePoint(geom, 0.5), never ST_Centroid).
* anpr_read — POST /api/v1/vehicles/sightings (C-E): the ORACLE computes the
  watchlist hit and the e-challan rules.  A FIR-named stolen plate's premise
  earmark (lawful basis stolen_vehicle_fir, a scenario precondition) is
  ensured at t0, so its reads are evidence-only R_STOLEN_SIGHTING (C-N),
  never a fine; without the premise those reads are skipped, not filed.
* agency_unit — NATS ``corridor.units.emulated`` (C-A).
* twin — one POST /api/v1/simulate/twin per run, recorded, never acted on.

Runs are asynchronous (contract C-G, api.py / tasks.py): 202 at once, 409
while the same use case runs, progress {frame, total, delivered, failed,
skipped, late}, stop / interrupted / reaped rows closed through C-F.
Identity is run-scoped — uuid5 over (use_case, seed, run) — and a replay
re-posts the STORED tape verbatim with a re-anchored t0, its movement's
ends re-drawn at its route's ends on today's roads.  A tape whose
recorded route is not a legal path on the CURRENT network (a run
recorded before the WP-20 conversion) is refused before anything is
written: 422 reason 'route_not_on_current_network' (503 when the route
cannot be checked; the CLI exits 2).  The tape is a pure function of
(spec, seed, t0); tick_sec is bounded to 1..60 s.

Live injections ride the same APIs as a real feed (the showcase rows on
their own subject, labelled SYNTHETIC).
Provenance is never hidden: events carry metadata.source 'event-emulator',
reads are stored source='emulated_anpr', every plate carries the EM series
marker, the Valkey sidecar ``emulator:source:{id}`` labels inputs docs, and
replays carry ``X-Emulation-Replay`` on HTTP and NATS.  The living city
(ambient.py) is OFF unless EMULATOR_LIVING_CITY=1 or an operator starts it;
its 108 requests are never approved here and are re-filed (up to 20 min)
when their slot closes.  The HTTP surface binds loopback :8090; the deck
reaches it through its same-origin /api/emulator proxy.
"""

__version__ = "0.1.0"
