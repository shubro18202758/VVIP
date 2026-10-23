/**
 * Corridor-event domain types for the ops showcase.
 *
 * The generalized event model is carried by five orthogonal axes (see the
 * architecture plan): protection_class, crowd_class, event_kind, footprint,
 * phases. These types are the deck-side mirror of the events API contract
 * (GET /api/v1/events/active and friends) — every field is optional-tolerant
 * because the API is additive and may not be deployed yet; the panels render
 * whatever is present and label the source instead of inventing values.
 */

// ─── Axes ────────────────────────────────────────────────────────────────────

export type ProtectionClass = 'Z+' | 'Z' | 'Y' | 'X' | 'none';

export type CrowdClass = 'LOW' | 'MODERATE' | 'HIGH' | 'EXTREME';

/** Corridor-event archetypes (the eight templates + the two watchlist-anpr event kinds the oracle persists: vehicle_pursuit, crime_incident — metadata doc, migration 010). */
export type EventKind =
	| 'protected_movement'
	| 'procession'
	| 'mass_gathering'
	| 'race'
	| 'ceremonial'
	| 'egress_surge'
	| 'emergency_evacuation'
	| 'work_zone'
	| 'vehicle_pursuit'
	| 'crime_incident';

/** Lifecycle state of a corridor event. */
export type EventPhaseState = 'planned' | 'active' | 'completed' | 'cancelled';

/** Adaptive situation band (adaptive/situation.py GREEN..BLACK). */
export type BandCode = 'GREEN' | 'AMBER' | 'RED' | 'BLACK';

export type ClosureType = 'full_closure' | 'partial_closure' | 'signal_hold';

// ─── Corridor event ──────────────────────────────────────────────────────────

export interface EventPhase {
	label: string;
	/** ISO 8601 window start (planned or actual). */
	startTime: string | null;
	/** ISO 8601 window end, null while open-ended. */
	endTime: string | null;
	closurePolicy: {
		advanceClosureSec: number | null;
		closureType: ClosureType | null;
	} | null;
	state: EventPhaseState;
}

export interface CorridorEvent {
	eventId: string;
	/** Operator title (wire `title`; the event id only when the API sent none). */
	name: string;
	eventKind: EventKind;
	protectionClass: ProtectionClass;
	crowdClass: CrowdClass;
	/** Lifecycle on the deck's axis (wire planning|approved → planned). */
	state: EventPhaseState;
	/**
	 * Route spine as segment ids (footprint axis): the ordered, de-duplicated
	 * union of every labelled spine in `routes` (rank order kept).
	 */
	routeSegmentIds: number[];
	phases: EventPhase[];
	/** Current phase label, if the API provides one. */
	currentPhase: string | null;
	/** Situation band for this event, when known. */
	band: BandCode | null;
	/** ISO 8601 event window (wire planned_start / planned_end). */
	windowStart: string | null;
	windowEnd: string | null;
	/** Short operator description. */
	description: string | null;
	/** Where this row came from — always shown to the operator. */
	source: EventSource;

	// ── additive (events API, migration 007/010) — absent on older rows ──
	/** Labelled spines in rank order (rank 1 = primary), wire `routes[]`. */
	routes?: RouteSpine[];
	/** Raw wire lifecycle status (planning | approved | active | …). */
	status?: string | null;
	/** Convoy movement this event generalizes (soft link), when any. */
	movementId?: string | null;
	/** Linkage + provenance carried from the event's metadata JSON. */
	metadata?: EventMetadata;
	/** True when the event emulator staged the row (metadata.source). */
	emulated?: boolean;
	/**
	 * Spines a re-route superseded (contract C-O `route_history`, additive):
	 * drawn dimmed as 'superseded', never as the live corridor.
	 */
	routeHistory?: RouteSpine[];
}

/** One labelled route spine of a corridor event (wire `routes[]`). */
export interface RouteSpine {
	label: string;
	segmentIds: number[];
	/** C-O: how the spine came to be — 'planned', 'reroute-N', 'plan_b' (additive). */
	variant?: string | null;
	/** C-O: when a superseded spine was replaced (route_history rows only). */
	supersededAt?: string | null;
}

/**
 * The event metadata fields the deck consumes. Pursuits carry the pursued
 * vehicle and the approved recommendation they were opened from (C6: human
 * initiated); emulator-staged rows carry their provenance so every surface
 * can badge them EMULATED.
 */
export interface EventMetadata {
	vehicleId: string | null;
	recommendationId: string | null;
	/** Producer tag, e.g. 'event-emulator'. */
	source: string | null;
	mode: string | null;
	runId: string | null;
	useCaseId: string | null;
	isReplay: boolean | null;
	/**
	 * Operator label of a test / rehearsal movement (the brain carries a
	 * movement's title as metadata.label): such a row wears a TEST pill and
	 * never reads as a real movement. Absent on unlabelled rows.
	 */
	label?: string | null;
}

export type EventSource =
	| 'live-events-api' // GET /api/v1/events/active
	| 'live-movements-api' // GET /api/v1/movements/active (convoy-era fallback)
	| 'offline-fixture'; // labelled demo fixture from the use-case library

export interface FeedStatus<T> {
	data: T;
	/** epoch ms of the last successful fetch; 0 = never. */
	lastUpdated: number;
	/** true while the live source has never answered and fixtures stand in. */
	offline: boolean;
	/** Human-readable note, e.g. "events API not deployed (404)". */
	note: string | null;
}

// ─── Situation band ──────────────────────────────────────────────────────────

export interface BandPoint {
	ts: number; // epoch ms (the server's observation time when it sent one)
	/**
	 * The READING at this tick — the raw observed class when the log carries
	 * it, else the committed class. A newest reading that differs from the
	 * committed band is the hysteresis candidate (band_easing semantics).
	 */
	band: BandCode;
	severityScore: number | null;
	source: 'situation_log' | 'adaptive-status' | 'city-band' | 'client-accumulated';
	/** Committed (post-hysteresis) class at this tick, when known. */
	committed?: BandCode | null;
	/** True on the tick the committed class changed (server flag). */
	transitioned?: boolean;
}

/** A band reading awaiting hysteresis confirmation (never the committed band). */
export interface BandCandidate {
	band: BandCode;
	/** Consecutive newest readings at the candidate band. */
	ticks: number;
	/**
	 * Confirmations the server needs (adaptive/hysteresis.py shipping
	 * defaults: escalation 2, de-escalation 4 plus the 120 s dwell; BLACK 1).
	 */
	required: number;
	/** Epoch ms of the first reading in the run. */
	since: number;
}

/** Where the masthead band came from (shown raw on every band surface). */
export type BandSourceLabel = 'situation_log' | 'city-band' | 'adaptive-status' | 'none';

export interface BandEvidence {
	shadowEval: string | null;
	floorApplied: boolean | null;
	failClosedReason: string | null;
	situationClass: BandCode | null;
}

// ─── Decision audit stream ───────────────────────────────────────────────────

export type AuditSource =
	| 'ws-event' // generalized WS /ws/event/{id}
	| 'ws-convoy' // WS /ws/convoy/{movement_id} (today's live rung)
	| 'ndjson-chat' // NDJSON /chat/stream ReadableStream (decider+Gemma+tools)
	| 'audit-poll' // 2 s poll rung
	| 'operator'; // local operator action echo

export type AuditLevel = 'info' | 'decision' | 'tool' | 'model' | 'band' | 'error';

/** Who acted on an audit row: a named person, a machine agent, or bookkeeping. */
export type ActorKind = 'human' | 'machine' | 'system';

export interface AuditEntry {
	id: number;
	ts: number; // epoch ms
	source: AuditSource;
	level: AuditLevel;
	actor: string; // e.g. "decider", "gemma", "mcp:tool_name", "adaptive", "operator"
	text: string;
	detail?: unknown;
	/**
	 * Who acted — drives the human/machine rails. Server rows carry the C-C
	 * FeedItem actor_kind; local rows set it explicitly (an officer's own
	 * request is human; oracle, twin, writer and deck replies never are).
	 */
	actorKind?: ActorKind;
	/** Server rows only: the stable FeedItem id (de-duplication key). */
	serverId?: string;
	/** Server rows only: the FeedItem kind (event|enforcement|anpr|dispatch|signals|ama|brain). */
	kind?: string | null;
	/** Server rows only: the row's source is emulated (C-C FeedItem.source) — badge EMULATED. */
	emulated?: boolean;
	/** Server rows only: info | warn | critical. */
	severity?: string | null;
	/** Server rows only: the C-C FeedItem urns (what the row is about). */
	urns?: string[];
	/** Server rows only: its numbers came from a SIMULATED feed (C-Q props) — the SIMULATED FEED pill. */
	simulated?: boolean;
	/** Local rows only: the corridor event the row concerns (dossier narrative). */
	eventId?: string | null;
}

/**
 * One server audit row (contract C-C FeedItem) as the poll rung reads it from
 * GET /api/v1/events/{id}/audit or GET /api/v1/city/feed — the fields the
 * stream renders.
 */
export interface ServerAuditRow {
	id: string;
	/** Epoch ms of `t`. */
	ts: number;
	kind: string | null;
	actor: string | null;
	actorKind: ActorKind;
	text: string;
	source: string | null;
	severity: string | null;
	urns: string[];
	/** C-Q: props.simulated_feed === true or props.data_quality includes 'synthetic'. */
	simulated?: boolean;
}

export type AuditLadderState = 'ws-event' | 'ws-convoy' | 'poll' | 'unavailable';

/**
 * Which server narrative the audit stream is a view over: the selected live
 * event's own audit (GET /events/{id}/audit), the whole city's feed (GET
 * /city/feed), or — only on an oracle that serves neither — the legacy
 * adaptive-status rung.
 */
export type AuditView = 'event' | 'city' | 'legacy' | 'none';

/** The operator's choice for the stream: follow the selected event, or the city. */
export type AuditScope = 'event' | 'city';

// ─── Event lifecycle (contract C-F) ──────────────────────────────────────────

/** Wire statuses of events.corridor_events (migration 007 CHECK). */
export type EventWireStatus = 'planning' | 'approved' | 'active' | 'completed' | 'cancelled';

/** The statuses POST /events/{id}/status accepts as to_status. */
export type EventTransitionTarget = 'approved' | 'active' | 'completed' | 'cancelled';

/** One lifecycle attempt — a 409 refusal is a first-class outcome, never a toast. */
export interface EventTransitionOutcome {
	eventId: string;
	toStatus: EventTransitionTarget;
	ok: boolean;
	/** HTTP status of the attempt (0 = transport failure). */
	status: number;
	fromStatus: string | null;
	/** 409: the moves the server allows from fromStatus. */
	allowed: string[] | null;
	/** The actor the server recorded (200). */
	actor: string | null;
	/** Server ledger time (ISO) on 200. */
	at: string | null;
	/** Refusal / error detail (non-200). */
	detail: string | null;
	/** Dispatches the close walked shut (completed / cancelled). */
	closedDispatches: number;
	/** Client epoch ms the outcome landed. */
	when: number;
}

// ─── City geometry refs (junction registry + segment lines) ─────────────────

/** A named junction (GET /api/v1/city/entities?kinds=junction). */
export interface JunctionRef {
	junctionId: number;
	name: string;
	nameGu: string | null;
	lon: number;
	lat: number;
	junctionType: string | null;
	signalControl: boolean | null;
}

/** A road segment line (GET /api/v1/spatial/segments, city bbox). */
export interface SegmentRef {
	segmentId: number;
	roadName: string | null;
	roadClass: string | null;
	/** GeoJSON LineString coordinates [lon, lat][]. */
	coordinates: Array<[number, number]>;
}

/** A dispatch waypoint the tiles offer (sent as waypoint_junction). */
export interface WaypointOption {
	junctionId: number;
	/** 'choke #N' from the interception plan, or the junction nearest the event spine. */
	source: 'interception' | 'spine';
	/** 1-based rank inside its source. */
	rank: number;
	name: string;
	/** Distance from the event spine (spine source), metres. */
	distanceM: number | null;
	/** Interception source: the vehicle has likely passed this junction already. */
	likelyPassed: boolean;
}

/** Where a pursuit opened for a watchlist vehicle would anchor on the graph. */
export interface PursuitAnchor {
	segmentId: number;
	roadName: string | null;
	/** Distance from the latest sighting to the segment line, metres. */
	distanceM: number;
}

/**
 * A pursuit the queue can offer: an earmarked vehicle with a latest sighting
 * and NO open pursuit referencing it. `refusal` names the broken link when
 * it cannot open (no road anchor, or no human-approved recommendation — C6).
 */
export interface PursuitCandidate {
	vehicle: VehicleWatchRecord;
	anchor: PursuitAnchor | null;
	recommendationId: string | null;
	refusal: 'no-anchor' | 'no-approved-recommendation' | null;
}

// ─── Brain posture (masthead strip) ──────────────────────────────────────────

export interface BrainPosture {
	/** GET /api/convoy/health answered. */
	reachable: boolean;
	status: string | null;
	decider: {
		/** model-host: loaded (connected) | loading | unreachable. */
		state: 'loaded' | 'loading' | 'unreachable' | 'unknown';
		model: string | null;
		p95Ms: number | null;
		latch: string | null;
		degraded: boolean | null;
		degradedReason: string | null;
	};
	gemma: { state: 'connected' | 'unreachable' | 'unknown'; model: string | null };
	/** GET /api/v1/adaptive/status degradation.degradation_tier. */
	degradationTier: string | null;
	checkpointsPaused: boolean | null;
	checkedAt: number;
	note: string | null;
}

// ─── Traffic feed provenance (SIMULATED FEED honesty) ───────────────────────

/**
 * Where the city's traffic readings come from right now (GET
 * /api/v1/corridor/summary quality_mix): every reading synthetic (the
 * signal-ingress Ahmedabad generator) → 'simulated'; some real → 'mixed';
 * only real → 'live'; nothing fresh → 'none'.
 */
export interface TrafficFeedProvenance {
	label: 'simulated' | 'mixed' | 'live' | 'none';
	real: number;
	synthetic: number;
	segments: number;
	dataAgeSec: number | null;
	checkedAt: number;
}

// ─── Retention health (C6 / DPDP) ────────────────────────────────────────────

export interface RetentionHealth {
	defaultDays: number | null;
	earmarkCapDays: number | null;
	policy: string | null;
	healthy: boolean | null;
	consecutiveFailures: number;
	lastAttemptAt: string | null;
	lastSuccessAt: string | null;
	lastRemoved: number | null;
	checkedAt: number;
}

// ─── Signal program history (gate card list) ─────────────────────────────────

export interface SignalHistoryRow {
	historyId: number;
	junctionId: number | null;
	eventId: string | null;
	/** snapshot (first calibrated write) | restore. */
	kind: string;
	cycleSec: number | null;
	source: string | null;
	reason: string | null;
	actor: string | null;
	capturedAt: string | null;
}

// ─── Twin shadow-eval comparison ─────────────────────────────────────────────

export interface ClosurePolicySpec {
	advanceClosureSec: number;
	closureType: ClosureType;
}

export interface TwinArm {
	label: string;
	policy: ClosurePolicySpec;
	runs: number;
	metrics: {
		disruptionVehicleHours: number | null;
		maxQueueLengthM: number | null;
		avgClosureDurationSec: number | null;
		transitTimeSec: number | null;
		recoverySec: number | null;
		ambulanceDelaySec: number | null;
	};
}

export interface TwinCompareResult {
	scenarioId: string;
	baseline: TwinArm;
	candidate: TwinArm;
	/** candidate − baseline, per metric (negative = candidate better). */
	deltas: Record<string, number | null>;
	/** 'compare-endpoint' (POST /simulate/twin/compare) or the labelled fallback. */
	source: 'compare-endpoint' | 'direct-two-arm';
	calibrated: boolean | null;
	forecastCoupling: string | null;
	fetchedAt: number;
}

// ─── Param overrides (shadow-eval evidence panel) ────────────────────────────

export interface OverrideRecord {
	/** ISO publish time (wire `published_at`; legacy `timestamp_utc`). */
	ts: string | null;
	eventId: string | null;
	protectionClass: ProtectionClass | null;
	situationClass: BandCode | null;
	shadowEval: string | null;
	floorApplied: boolean | null;
	failClosedReason: string | null;
	params: Record<string, unknown> | null;
	// ── additive (contract C-C /adaptive/overrides) ──
	movementId?: string | null;
	/** baseline | retrieved | learned. */
	source?: string | null;
	shadowEvalDetail?: string | null;
}

// ─── Emulator ────────────────────────────────────────────────────────────────

export interface EmulatorState {
	online: boolean;
	healthDetail: string | null;
	lastChecked: number;
	/** True when a start request was issued (awaiting the emulator contract). */
	runRequested: string | null;
	runError: string | null;
	/** /health stack probes (store/valkey/twin/nats), when reported. */
	stack?: Record<string, boolean> | null;
	/** /health living_city block present → the living-city routes exist (C-G). */
	livingCitySupported?: boolean;
}

/** One cause tape from GET /api/v1/emulator/use-cases (the emulator's own library). */
export interface EmulatorUseCaseInfo {
	id: string;
	name: string;
	category: string | null;
	vvipClass: string | null;
	horizonMin: number | null;
	seed: number | null;
	routeSegmentIds: number[];
	/** A flagship tape: launches are capped at ×10 (engine sampling, see format.ts). */
	hero: boolean;
}

/** C-G run progress: frames walked / total, delivered / failed. */
export interface EmulatorRunProgress {
	frame: number;
	total: number;
	delivered: number;
	failed: number;
}

/** One row of the run ledger (GET /runs, GET /runs/{id}). */
export interface EmulatorRunRow {
	runId: number;
	useCaseId: string;
	/** running | completed | stopped | interrupted | failed (C-G) — raw otherwise. */
	status: string;
	clockScale: number | null;
	isReplay: boolean;
	replayOf: number | null;
	createdAt: string | null;
	endedAt: string | null;
	/** C-G live progress, else the ledger's final delivery summary. */
	progress: EmulatorRunProgress | null;
	eventId: string | null;
	movementId: string | null;
	/**
	 * The run's corridor event lifecycle (additive, C-G): 'planning' while
	 * the run is live means its activation has not landed yet (the emulator
	 * retries every 5 s); null on older builds.
	 */
	eventStatus?: string | null;
	/** Why a failed / interrupted run ended (spec.reason), verbatim. */
	failReason?: string | null;
	/**
	 * The run's scenario precondition (C-N, spec.scenario_precondition): a
	 * stolen-vehicle sweep makes its FIR premise explicit before the first
	 * camera read — an EMULATED stolen-FIR earmark created, or found already
	 * active, or failed (that plate's reads are then skipped). [] when the
	 * use case has none or the build predates it.
	 */
	premises?: EmulatorRunPremise[];
}

/** One scenario-precondition entry of an emulator run (spec.scenario_precondition[]). */
export interface EmulatorRunPremise {
	/** The FIR-named (emulated, EM-segment) plate — masked on screen without an officer. */
	plate: string | null;
	/** delivered | failed | skipped (a seam that cannot read earmarks). */
	result: string;
	/** created | already_active — how a delivered premise was met. */
	earmark: string | null;
	firRef: string | null;
	/** Why it failed / was skipped, verbatim. */
	reason: string | null;
}

/**
 * The panel's launch (or replay) in flight. `contract` says how the emulator
 * answered: 'c-g' = 202 + run id (async, C-G); 'sync' = an older build that
 * holds the POST until the tape ends (the run is then tracked from the
 * ledger, the request is never awaited by the UI).
 */
export interface EmulatorLaunchState {
	kind: 'launch' | 'replay';
	useCaseId: string;
	clockScale: number;
	runId: number | null;
	phase: 'posting' | 'running' | 'completed' | 'stopped' | 'interrupted' | 'refused' | 'failed';
	contract: 'c-g' | 'sync' | null;
	detail: string | null;
	at: number;
	/**
	 * The newest run id the ledger showed before this launch (null = never
	 * read): a synchronous build's run is the first newer one of its tape.
	 */
	ledgerMaxRunId: number | null;
}

/** One living-city slot (C-G GET /api/v1/emulator/living-city). */
export interface LivingCitySlot {
	slotId: string;
	kind: string;
	title: string;
	eventId: string | null;
	startsAt: string | null;
	endsAt: string | null;
	routeSegmentIds: number[];
	/** The slot's corridor event: 'active', or 'planning' while its activation is pending. */
	eventStatus?: string | null;
}

export interface LivingCityDoc {
	enabled: boolean;
	status: 'running' | 'stopped' | string;
	dateIst: string | null;
	daySeed: number | null;
	activeSlots: LivingCitySlot[];
	nextCauses: Array<{ at: string | null; kind: string; title: string }>;
	units: string[];
	since: string | null;
	// ── additive (C-G living-city status doc) ──
	/** Deliberate violators driven (each one a PENDING e-challan); null = not reported. */
	violators?: boolean | null;
	/** Slots a use-case run pre-empted that may resume. */
	yielded?: string[];
	/** How many slots are pre-empted by a running use case. */
	preempted?: number;
	/** 108 requests waiting to be re-filed. */
	refiling?: number;
	/** The program's own counters (events opened, requests filed, …). */
	counters?: Record<string, number>;
	/** The emulator's own status line or last error. */
	detail?: string | null;
}

// ─── Enforcement recommendation queue (ops-screens stream) ───────────────────

/** A pending machine-filed e-challan recommendation awaiting a human decision. */
export interface EnforcementRecommendation {
	recommendationId: string;
	sightingId: string | null;
	vehicleId: string | null;
	plate: string | null;
	ruleId: string | null;
	/** One-line operator summary, e.g. '112 km/h in a 60 km/h segment'. */
	recommendation: string | null;
	/** Raw evidence JSONB (speed, segment, camera, limits) for the expander. */
	evidence: Record<string, unknown> | null;
	status: 'pending' | 'approved' | 'rejected';
	decidedBy: string | null;
	decidedAt: string | null;
	auditTail: Array<Record<string, unknown>>;
	createdAt: string | null;
	/**
	 * The pursuit this approval authorised (oracle `pursuit`, C6: one officer
	 * approval authorises ONE pursuit) — null/absent when it authorised none.
	 */
	pursuit?: { eventId: string; status: string | null } | null;
}

// ─── Agency dispatch lifecycle (Jan Rakshak tiles) ───────────────────────────

export interface DispatchAuditEntry {
	fromStatus: string | null;
	toStatus: string;
	actor: string;
	note: string | null;
	at: string;
}

export interface DispatchRecord {
	dispatchId: string;
	eventId: string | null;
	agency: string;
	agencyLabel: string | null;
	unitId: string | null;
	waypointJunction: number | null;
	status: string;
	requestedBy: string | null;
	decidedBy: string | null;
	decisionNote: string | null;
	approvedAt: string | null;
	createdAt: string | null;
	updatedAt: string | null;
	audit: DispatchAuditEntry[];
}

// ─── City signal plans (the gate card) ───────────────────────────────────────

export interface SignalBandPlanRow {
	band: BandCode;
	/** Share of cycle given to the priority corridor (0..1). */
	priorityShare: number | null;
	/** Minimum cross-street green the floor guarantees (seconds). */
	crossGreenFloorSec: number | null;
	/**
	 * What this band's plan does, as the gate card prints it: 'writes' when
	 * the plan rewrites signal programs, 'hold' when it keeps the baseline
	 * (wire `writes` is a boolean).
	 */
	writes: 'writes' | 'hold' | null;
	/** The raw wire boolean behind `writes`. */
	writesSignals: boolean | null;
}

export interface SignalLastPlan {
	applied: boolean | null;
	band: BandCode | null;
	plannedJunctions: number[];
	/** Refusal reason when applied === false (fail-closed, held at baseline). */
	reason: string | null;
	raw: Record<string, unknown> | null;
}

export interface SignalPlansDoc {
	writerEnabled: boolean;
	writerFlagEnv: string | null;
	cityBand: BandCode | null;
	bandSubjects: number | null;
	bandPlanTable: SignalBandPlanRow[];
	lastPlan: SignalLastPlan | null;
	/** twinInFlight is a COUNT of twin shadow-evals holding the writer (> 0 = in flight). */
	gate: { writerActive: boolean | null; writerWaiters: number | null; twinInFlight: number | null };
	scope: string | null;
}

/** Result of an APPLY attempt — a refusal is a first-class outcome, never an error toast. */
export interface SignalApplyOutcome {
	applied: boolean;
	/** HTTP status of the attempt (200 applied / 409 held at baseline). */
	status: number;
	/** Human-readable refusal reason when held (e.g. twin_not_calibrated). */
	refusalReason: string | null;
	/** Full refusal report for the inline expander. */
	detail: Record<string, unknown> | null;
	at: number;
}

// ─── Watchlist (earmarked vehicles) ──────────────────────────────────────────

export interface VehicleWatchRecord {
	vehicleId: string;
	plate: string | null;
	earmarkState: 'watch' | 'flagged' | null;
	earmarkReason: string | null;
	lawfulBasis: string | null;
	retentionUntil: string | null;
	lastSighting: {
		t: string | null;
		lon: number;
		lat: number;
		speedKmh: number | null;
		heading: number | null;
		source: string | null;
	} | null;
}

// ─── Pursuit / interception ──────────────────────────────────────────────────

export interface InterceptionChoke {
	rank: number;
	junctionId: number;
	junctionType: string;
	signalControl: boolean;
	position: [number, number];
	/** Travel-time cost from the latest sighting on the road graph (seconds). */
	graphCostSec: number | null;
	chokeWeight: number | null;
	score: number | null;
	// ── additive (WP-03 interception plan) ──
	/** Junction name (migration 012 corridor.junctions.name). */
	name?: string | null;
	/** One of the junctions the vehicle drives towards (the plan's roots). */
	downstream?: boolean;
	/** Graph time shorter than the sighting's age: the vehicle has likely passed it. */
	likelyPassed?: boolean;
}

/** How the plan decided which way the vehicle is going (WP-03). */
export interface InterceptionDirection {
	/** heading | oneway | dead_end | unknown. */
	basis: string;
	towardsJunctionIds: number[];
	segmentBearingDeg: number | null;
	positionAlongSegment: number | null;
	seedSegmentOneway: boolean;
	/** Observed driving AGAINST a one-way segment — projected as the camera saw it. */
	againstOneway: boolean;
	/** The legal onward movements the ranking starts from. */
	onwardSegmentIds: number[];
}

export interface InterceptionPlan {
	eventId: string;
	vehicleId: string | null;
	latestSighting: {
		sightingId: string;
		plate: string | null;
		t: string | null;
		position: [number, number];
		speedKmh: number | null;
		heading: number | null;
		source: string | null;
		cameraId?: string | null;
	} | null;
	seedSegmentId: number | null;
	maxCostSec: number | null;
	chokes: InterceptionChoke[];
	note: string | null;
	// ── additive (WP-03 interception plan) ──
	sightingAgeSec?: number | null;
	/** The sighting is older than staleAfterSec: the vehicle may be beyond every junction listed. */
	stale?: boolean;
	staleAfterSec?: number | null;
	direction?: InterceptionDirection | null;
}

// ─── Degradation ledger (decisions_log failure taxonomy) ─────────────────────

/**
 * One recurring-failure cause mined from convoy-brain's decisions_log by
 * the oracle's /ops/degradation-taxonomy endpoint. The five fields are the
 * server contract (traffic_oracle/ops/degradation_taxonomy.py); everything
 * is optional-tolerant because the API is additive and may not be deployed
 * yet — the panel renders whatever is present and never invents values.
 */
export interface DegradationTaxonomyEntry {
	/** Cause family, e.g. "novelty_latch:novel" / "checkpoint_fallback:gemma". */
	cause: string;
	count: number;
	/** Epoch seconds of the earliest / latest contributing row (null = none datable). */
	firstSeen: number | null;
	lastSeen: number | null;
	/** Pointer to one real row: "<movement>:<phase>:<timestamp>". */
	sampleRef: string;
}

export interface DegradationTaxonomy {
	/** Causes sorted by count desc — the ledger proper. */
	entries: DegradationTaxonomyEntry[];
	contextsScanned: number;
	rowsScanned: number;
	/** True when the Valkey scan hit its context bound (honest escape hatch). */
	truncated: boolean;
	contextsSkippedCorrupt: number;
	generatedAt: string | null;
}

// ─── Escort authority (officer approves every switch, C-J pending_approval) ──

/**
 * What approving an escort proposal would do — the exact route the brain
 * computed (Plan B or a re-route), the closures it adds and lifts, the km
 * left before and after — or the hold it proposes. Every field mirrors the
 * brain's ``proposed`` document; absent ones read as null / [].
 */
export interface EscortProposedChange {
	/** plan_b | reroute (route kinds) — null for a hold. */
	routeSource: string | null;
	/** The whole route if approved (the dashed preview on the canvas). */
	segmentIds: number[];
	/** From the convoy's segment on. */
	tailSegmentIds: number[];
	/** Road names along the new route ahead (signage order). */
	roadNames: string[];
	/** What is NEW on it — the segments / roads taken instead (route order). */
	viaSegmentIds: number[];
	viaRoadNames: string[];
	/** Routing metres left now, and on the proposed route. */
	remainingM: number | null;
	newRemainingM: number | null;
	/** Congested segments the new route avoids. */
	avoids: number[];
	longDetour: boolean;
	/** partial | full | signal_priority … (the class's closure kind). */
	closureType: string | null;
	closuresNew: number[];
	closuresLifted: number[];
	/** A hold: the segments it waits on and its bound. */
	holdSegmentIds: number[];
	maxHoldSec: number | null;
	/** No route geometry: the route is re-planned on approval. */
	replan: boolean;
}

/**
 * ONE open escort proposal (C-J ``pending_approval``): the decider proposed
 * a route switch / Plan B / hold / closures and a named officer must approve
 * it before it runs. With no answer by ``expiresAt`` it expires and the
 * convoy holds its current approved plan.
 */
export interface EscortProposal {
	movementId: string;
	eventId: string | null;
	approvalId: string;
	/** activate_plan_b | emergency_reroute | hold_position | continue_monitoring */
	action: string;
	/** plan_b | reroute | hold | continue */
	kind: string;
	/** The brain's own English sentence (audit / tooltip). */
	summary: string | null;
	/** Why an officer is asked (the decider's tier + confidence, or a degradation). */
	reason: string | null;
	/** incident_detected | phase_transition (a held DEPARTURE) */
	trigger: string | null;
	tier: string | null;
	confidence: number | null;
	createdAt: number | null;
	expiresAt: number | null;
	timeoutSec: number | null;
	/** The congested segments behind it. */
	segmentIds: number[];
	peakCongestion: number | null;
	proposed: EscortProposedChange | null;
	/** Its numbers came from a SIMULATED feed. */
	simulated: boolean;
}

/** A proposal that closed: approved / rejected by a named officer, expired, superseded. */
export interface EscortProposalOutcome {
	movementId: string;
	approvalId: string;
	action: string;
	kind: string;
	summary: string | null;
	status: string;
	decidedBy: string | null;
	decidedAt: number | null;
	claimedOfficer: string | null;
	expiryReason: string | null;
	/** An approval that could not be applied (the convoy had passed the branch point). */
	applied: boolean | null;
	appliedDetail: string | null;
}

/** One escort as GET /api/convoy/escorts lists it (one read for every console). */
export interface EscortLane {
	movementId: string;
	eventId: string | null;
	status: string;
	alive: boolean;
	officer: string | null;
	gpsMode: string | null;
	trafficSimulated: boolean | null;
	proposal: EscortProposal | null;
	outcomes: EscortProposalOutcome[];
	timeoutSec: number | null;
}

/**
 * What ONE followed escort's own status document says about its proposals
 * (GET /api/convoy/movements/{id}/escort — C-J `pending_approval` and
 * `approval_outcomes`, parsed by gods-eye/city-api's parseEscortStatus): the
 * store's source for that escort when the /escorts lanes are not answering.
 */
export interface FollowedEscortDoc {
	movementId: string;
	/** The document reports escort authority: its proposal fields are authoritative. */
	proposalsReported: boolean;
	proposal: EscortProposal | null;
	outcomes: EscortProposalOutcome[];
}
