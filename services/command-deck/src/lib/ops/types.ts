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
	name: string;
	eventKind: EventKind;
	protectionClass: ProtectionClass;
	crowdClass: CrowdClass;
	state: EventPhaseState;
	/** Route spine as segment ids (footprint axis). */
	routeSegmentIds: number[];
	phases: EventPhase[];
	/** Current phase label, if the API provides one. */
	currentPhase: string | null;
	/** Situation band for this event, when known. */
	band: BandCode | null;
	/** ISO 8601 event window. */
	windowStart: string | null;
	windowEnd: string | null;
	/** Short operator description. */
	description: string | null;
	/** Where this row came from — always shown to the operator. */
	source: EventSource;
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
	ts: number; // epoch ms
	band: BandCode;
	severityScore: number | null;
	source: 'situation_log' | 'adaptive-status' | 'client-accumulated';
}

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

export interface AuditEntry {
	id: number;
	ts: number; // epoch ms
	source: AuditSource;
	level: AuditLevel;
	actor: string; // e.g. "decider", "gemma", "mcp:tool_name", "adaptive", "operator"
	text: string;
	detail?: unknown;
}

export type AuditLadderState = 'ws-event' | 'ws-convoy' | 'poll' | 'unavailable';

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
	ts: string | null;
	eventId: string | null;
	protectionClass: ProtectionClass | null;
	situationClass: BandCode | null;
	shadowEval: string | null;
	floorApplied: boolean | null;
	failClosedReason: string | null;
	params: Record<string, unknown> | null;
}

// ─── Emulator ────────────────────────────────────────────────────────────────

export interface EmulatorState {
	online: boolean;
	healthDetail: string | null;
	lastChecked: number;
	/** True when a start request was issued (awaiting the emulator contract). */
	runRequested: string | null;
	runError: string | null;
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
	/** Number of junction writes this plan would issue. */
	writes: number | null;
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
	gate: { writerActive: boolean | null; writerWaiters: number | null; twinInFlight: boolean | null };
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
	} | null;
	seedSegmentId: number | null;
	maxCostSec: number | null;
	chokes: InterceptionChoke[];
	note: string | null;
}
