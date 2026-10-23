/**
 * Ops showcase API client — every outbound call the /ops board makes.
 *
 * Transport decisions (from the showcase plan, contract C6):
 *   - traffic-oracle, convoy-brain AND the event emulator are reached through
 *     the deck origin's proxy prefixes (vite dev proxy in dev, hooks.server.ts
 *     in the adapter-node build; no EventSource anywhere — streaming is NDJSON
 *     over fetch with a ReadableStream reader, and WS via the ladder in dev).
 *   - the absolute emulator loopback URL (EMULATOR_BASE) is a dev-only second
 *     rung; production never requests it.
 *   - all URLs are fixed constants or built from `safePathId`-validated ids;
 *     nothing here accepts a caller-supplied URL (see endpoints.ts guard).
 *
 * Every fetch reports its degradation honestly: 404 raises ApiNotDeployed so
 * panels can fall back and label the source instead of showing a blank — and
 * a GET path that 404'd is not re-requested for NOT_DEPLOYED_BACKOFF_MS.
 */

import {
	ADAPTIVE_STATUS_API,
	BRAIN_API,
	BRAIN_HEALTH_API,
	CITY_ENTITIES_API,
	CITY_FEED_API,
	CORRIDOR_SUMMARY_API,
	EMULATOR_BASE,
	EMULATOR_HERO_CATEGORIES,
	EMULATOR_HERO_USE_CASES,
	EMULATOR_PROXY_PATH,
	EVENT_AUDIT_PATH,
	EVENT_STATUS_PATH,
	ORACLE_V1,
	DEGRADATION_TAXONOMY_API,
	ENFORCEMENT_API,
	DISPATCHES_API,
	EVENTS_API,
	NETWORK_BBOX,
	NOT_DEPLOYED_BACKOFF_MS,
	SIGNALS_HISTORY_API,
	SIGNALS_PLANS_API,
	SPATIAL_SEGMENTS_API,
	VEHICLE_RETENTION_API,
	assertSafeExternalUrl,
	safePathId,
} from './endpoints';
import type {
	ActorKind,
	BandCode,
	BandEvidence,
	BrainPosture,
	CorridorEvent,
	CrowdClass,
	DegradationTaxonomy,
	DispatchAuditEntry,
	DispatchRecord,
	EmulatorRunPremise,
	EmulatorRunProgress,
	EmulatorRunRow,
	EmulatorUseCaseInfo,
	EnforcementRecommendation,
	EscortLane,
	EscortProposal,
	EscortProposalOutcome,
	EscortProposedChange,
	EventKind,
	EventMetadata,
	EventPhase,
	EventSource,
	EventPhaseState,
	EventTransitionOutcome,
	EventTransitionTarget,
	InterceptionDirection,
	InterceptionPlan,
	JunctionRef,
	LivingCityDoc,
	OverrideRecord,
	ProtectionClass,
	RetentionHealth,
	RouteSpine,
	SegmentRef,
	ServerAuditRow,
	SignalApplyOutcome,
	SignalBandPlanRow,
	SignalHistoryRow,
	SignalLastPlan,
	SignalPlansDoc,
	TrafficFeedProvenance,
	TwinArm,
	TwinCompareResult,
	VehicleWatchRecord,
	ClosurePolicySpec,
} from './types';

// ─── Errors ──────────────────────────────────────────────────────────────────

export class ApiNotDeployed extends Error {
	constructor(
		public status: number,
		message: string,
	) {
		super(message);
		this.name = 'ApiNotDeployed';
	}
}

export class ApiDown extends Error {
	constructor(message: string) {
		super(message);
		this.name = 'ApiDown';
	}
}

/**
 * A proxied call the deck's operator gate refused (401/403 — no signed-in
 * duty officer). Nothing upstream is broken: the console must sign in. The
 * message is fixed operator wording — a refusal body (JSON with the gate's
 * own detail) never reaches a panel note verbatim.
 */
export class SignInRequired extends ApiDown {
	constructor(public status: number) {
		super(SIGN_IN_REQUIRED);
		this.name = 'SignInRequired';
	}
}

/** The operator wording every panel shows for a gated read (see SignInRequired). */
export const SIGN_IN_REQUIRED = 'sign-in required — sign in as the duty officer';

/** Whether an error (or a note built from one) is the operator gate's refusal. */
export function isSignInRequired(err: unknown): boolean {
	if (err instanceof SignInRequired) return true;
	const text = err instanceof Error ? err.message : typeof err === 'string' ? err : '';
	return text.includes(SIGN_IN_REQUIRED);
}

// ─── Network hygiene (shared by the ops, canvas and corridor pollers) ────────

/**
 * True only under the vite dev server. Vite replaces `import.meta.env` at
 * build time (DEV false in the adapter-node bundle); under the node contract
 * tests it is undefined, which reads as production.
 */
export const DEV_SERVER: boolean = import.meta.env?.DEV === true;

/** The page is in a background tab (pollers pause; nothing is fetched). */
export function pageHidden(): boolean {
	return typeof document !== 'undefined' && document.hidden === true;
}

/**
 * Per-path 404 memory. Keyed by path WITHOUT the query string: a 404 means
 * the route (or the addressed row) is absent, whatever the parameters.
 * Only GET requests consult it — a writer's 404 is an answer, never cached.
 */
const notDeployedUntil = new Map<string, number>();

function backoffKey(url: string): string {
	const q = url.indexOf('?');
	return q === -1 ? url : url.slice(0, q);
}

export const notDeployedBackoff = {
	/** Epoch ms until which `url` is answered from memory (0 = not backed off). */
	until(url: string, now = Date.now()): number {
		const t = notDeployedUntil.get(backoffKey(url)) ?? 0;
		return t > now ? t : 0;
	},
	remember(url: string, now = Date.now()): void {
		notDeployedUntil.set(backoffKey(url), now + NOT_DEPLOYED_BACKOFF_MS);
	},
	forget(url: string): void {
		notDeployedUntil.delete(backoffKey(url));
	},
	/** Test seam: drop every remembered path. */
	reset(): void {
		notDeployedUntil.clear();
	},
};

/**
 * A read that fails within this long of the last GOOD answer is a BLIP (an
 * upstream restart, one dropped request): the last-good picture stands —
 * stale by its own age, the note says why — instead of the screen flipping
 * to NO DATA or the board swapping its live rows for PREVIEW fixtures on a
 * single failed poll. A failure past it is an outage, said as before. A
 * 404 (not deployed) or a gate refusal is never a blip.
 */
export const BLIP_GRACE_MS = 15_000;

/** True while a failed read is still within the blip grace of the last good answer (epoch ms; 0 = never). */
export function withinBlipGrace(lastGoodAt: number, now = Date.now(), graceMs = BLIP_GRACE_MS): boolean {
	return lastGoodAt > 0 && now >= lastGoodAt && now - lastGoodAt <= graceMs;
}

/**
 * One gate per store: a named poller never overlaps itself (in-flight guard;
 * request timeouts exceed some poll intervals, so responses could otherwise
 * land out of order) and never runs while the page is hidden. Returns
 * false when the tick was skipped.
 */
export function createPollGate(): (name: string, fn: () => Promise<unknown>) => Promise<boolean> {
	const inFlight = new Set<string>();
	return async (name, fn) => {
		if (pageHidden() || inFlight.has(name)) return false;
		inFlight.add(name);
		try {
			await fn();
		} finally {
			inFlight.delete(name);
		}
		return true;
	};
}

/**
 * Concurrent identical GETs share ONE request (two pollers, or a poller and
 * a selection change, asking for the same path in the same tick) — so a 404
 * is answered once and its back-off covers every caller. Writers never share.
 */
const inFlightGets = new Map<string, Promise<unknown>>();

function request<T>(url: string, init?: RequestInit, timeoutMs = 8000): Promise<T> {
	const isGet = !init?.method || init.method.toUpperCase() === 'GET';
	if (!isGet) return send<T>(url, init, timeoutMs, false);
	if (notDeployedBackoff.until(url)) {
		return Promise.reject(
			new ApiNotDeployed(404, `endpoint not deployed (404, backing off): ${backoffKey(url)}`),
		);
	}
	let shared = inFlightGets.get(url);
	if (!shared) {
		const sent: Promise<T> = send<T>(url, init, timeoutMs, true).finally(() => {
			// a fresh read (requestFresh) may own the slot by now — leave it
			if (inFlightGets.get(url) === sent) inFlightGets.delete(url);
		});
		shared = sent;
		inFlightGets.set(url, shared);
	}
	return shared as Promise<T>;
}

/**
 * A GET that must observe a write that just committed: it never joins a
 * request already in flight (that one may have left before the write), and
 * GETs issued after it share IT instead.
 */
function requestFresh<T>(url: string, timeoutMs = 8000): Promise<T> {
	if (notDeployedBackoff.until(url)) {
		return Promise.reject(new ApiNotDeployed(404, `endpoint not deployed (404, backing off): ${backoffKey(url)}`));
	}
	const fresh: Promise<T> = send<T>(url, undefined, timeoutMs, true).finally(() => {
		if (inFlightGets.get(url) === fresh) inFlightGets.delete(url);
	});
	inFlightGets.set(url, fresh);
	return fresh;
}

async function send<T>(url: string, init: RequestInit | undefined, timeoutMs: number, isGet: boolean): Promise<T> {
	const ctrl = new AbortController();
	const timer = setTimeout(() => ctrl.abort(), timeoutMs);
	try {
		const res = await fetch(url, { ...init, signal: ctrl.signal });
		if (res.status === 404) {
			if (isGet) notDeployedBackoff.remember(url);
			throw new ApiNotDeployed(404, `endpoint not deployed: ${url}`);
		}
		if (res.status === 401 || res.status === 403) {
			await res.body?.cancel().catch(() => undefined);
			throw new SignInRequired(res.status);
		}
		if (!res.ok) {
			const body = await res.text().catch(() => '');
			throw new ApiDown(`${res.status} ${res.statusText}: ${body.slice(0, 200)}`);
		}
		return (await res.json()) as T;
	} catch (err) {
		if (err instanceof ApiNotDeployed || err instanceof ApiDown) throw err;
		if (err instanceof DOMException && err.name === 'AbortError') {
			throw new ApiDown(`timeout: ${url}`);
		}
		throw new ApiDown(`${url}: ${err instanceof Error ? err.message : String(err)}`);
	} finally {
		clearTimeout(timer);
	}
}

function post<T>(url: string, body: unknown, timeoutMs = 30000): Promise<T> {
	return request<T>(
		url,
		{
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify(body),
		},
		timeoutMs,
	);
}

// ─── Events board ────────────────────────────────────────────────────────────

const KINDS: EventKind[] = [
	'protected_movement',
	'procession',
	'mass_gathering',
	'race',
	'ceremonial',
	'egress_surge',
	'emergency_evacuation',
	'work_zone',
	'vehicle_pursuit',
	'crime_incident',
];

const PROTECTION: ProtectionClass[] = ['Z+', 'Z', 'Y', 'X', 'none'];
const CROWDS: CrowdClass[] = ['LOW', 'MODERATE', 'HIGH', 'EXTREME'];

function pick<T extends string>(v: unknown, allowed: readonly T[]): T | null {
	return typeof v === 'string' && (allowed as readonly string[]).includes(v) ? (v as T) : null;
}

function str(v: unknown): string | null {
	return typeof v === 'string' && v.length ? v : null;
}

/** Crowd class on the deck's axis: the oracle's CrowdDensity says MEDIUM. */
function crowdOf(v: unknown): CrowdClass | null {
	if (v === 'MEDIUM') return 'MODERATE';
	return pick(v, CROWDS);
}

/**
 * Wire lifecycle → the deck's axis. The oracle's events.corridor_events
 * status is planning | approved | active | completed | cancelled (migration
 * 007 + the C-F lifecycle); planning and approved are both 'planned' here
 * (the raw status rides along on CorridorEvent.status).
 */
function lifecycleOf(v: unknown): EventPhaseState | null {
	if (v === 'planning' || v === 'approved' || v === 'planned') return 'planned';
	return pick(v, ['active', 'completed', 'cancelled'] as const);
}

function segmentIds(v: unknown): number[] {
	return Array.isArray(v) ? v.filter((n): n is number => typeof n === 'number' && Number.isFinite(n)) : [];
}

/** Labelled spines in rank order (wire `routes[]` = {label, segment_ids}). */
function routesOf(v: unknown): RouteSpine[] {
	if (!Array.isArray(v)) return [];
	const out: RouteSpine[] = [];
	for (const r of v) {
		if (!r || typeof r !== 'object') continue;
		const row = r as Record<string, unknown>;
		const ids = segmentIds(row.segment_ids ?? row.segmentIds);
		if (!ids.length) continue;
		out.push({
			label: str(row.label) ?? 'spine',
			segmentIds: ids,
			...(str(row.variant) ? { variant: str(row.variant) } : {}),
			...(str(row.superseded_at) ? { supersededAt: str(row.superseded_at) } : {}),
		});
	}
	return out;
}

function metadataOf(v: unknown): EventMetadata {
	const m = v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
	const runId = m.run_id ?? m.runId;
	return {
		vehicleId: str(m.vehicle_id),
		recommendationId: str(m.recommendation_id),
		source: str(m.source),
		mode: str(m.mode),
		runId: typeof runId === 'number' ? String(runId) : str(runId),
		useCaseId: str(m.use_case_id),
		isReplay: typeof m.is_replay === 'boolean' ? m.is_replay : null,
		label: str(m.label)?.trim() || null,
	};
}

/**
 * One events-API row → CorridorEvent. Mirrors the oracle's GET /events/active
 * shape (traffic_oracle/events/__init__.py active_events): title, status,
 * routes[].segment_ids, phases[].window_start/window_end, planned_start/
 * planned_end, metadata. The legacy keys (name, state, route_segment_ids,
 * start_time/end_time, window_*) stay accepted so the movements fallback and
 * older rows keep mapping. Exported pure for tests/contract/run.mjs.
 */
export function mapEventRow(row: Record<string, unknown>, source: EventSource): CorridorEvent | null {
	const eventId = row.event_id ?? row.id ?? row.eventId;
	if (typeof eventId !== 'string' && typeof eventId !== 'number') return null;
	const kind = pick(row.event_kind ?? row.eventKind, KINDS) ?? 'protected_movement';
	const protection =
		pick(row.protection_class ?? row.protectionClass ?? row.vvip_class ?? row.vvipClass, PROTECTION) ??
		'none';
	const crowd = crowdOf(row.crowd_class ?? row.crowdClass) ?? 'LOW';
	const rawStatus = row.status ?? row.state ?? row.lifecycle_state;
	const state = lifecycleOf(rawStatus) ?? 'planned';
	const routes = routesOf(row.routes);
	// Every labelled spine, rank order, each segment once (a segment shared
	// by the primary and an alternate would otherwise draw twice).
	const route = routes.length
		? [...new Set(routes.flatMap((r) => r.segmentIds))]
		: segmentIds(row.route_segment_ids ?? row.routeSegmentIds);
	const rawPhases = Array.isArray(row.phases) ? (row.phases as Record<string, unknown>[]) : [];
	const phases: EventPhase[] = rawPhases.map((p) => ({
		label: typeof p.label === 'string' ? p.label : 'phase',
		startTime: str(p.window_start) ?? str(p.start_time) ?? str(p.startTime),
		endTime: str(p.window_end) ?? str(p.end_time) ?? str(p.endTime),
		closurePolicy:
			p.closure_policy && typeof p.closure_policy === 'object'
				? {
						advanceClosureSec:
							typeof (p.closure_policy as Record<string, unknown>).advance_closure_sec === 'number'
								? ((p.closure_policy as Record<string, unknown>).advance_closure_sec as number)
								: null,
						closureType: pick((p.closure_policy as Record<string, unknown>).closure_type, [
							'full_closure',
							'partial_closure',
							'signal_hold',
						]),
					}
				: null,
		state: pick(p.state, ['planned', 'active', 'completed', 'cancelled']) ?? 'planned',
	}));
	const metadata = metadataOf(row.metadata);
	return {
		eventId: String(eventId),
		name: str(row.title) ?? str(row.name) ?? String(eventId),
		eventKind: kind,
		protectionClass: protection,
		crowdClass: crowd,
		state,
		routeSegmentIds: route,
		phases,
		currentPhase: typeof row.current_phase === 'string' ? row.current_phase : null,
		band: pick(row.situation_class ?? row.situationClass ?? row.band, [
			'GREEN',
			'AMBER',
			'RED',
			'BLACK',
		]),
		windowStart: str(row.planned_start) ?? str(row.window_start),
		windowEnd: str(row.planned_end) ?? str(row.window_end),
		description: typeof row.description === 'string' ? row.description : null,
		source,
		routes,
		status: str(rawStatus),
		movementId: str(row.movement_id) ?? str(row.movementId),
		metadata,
		emulated: metadata.source === 'event-emulator',
		...(Array.isArray(row.route_history) ? { routeHistory: routesOf(row.route_history) } : {}),
	};
}

/** GET /api/v1/events/active — the generalized multi-event board feed. */
/**
 * GET /api/v1/events/active. `fresh` (a read that must observe a write that
 * just committed) never joins a GET already in flight — it may have left
 * before the write.
 */
export async function fetchActiveEvents(opts: { fresh?: boolean } = {}): Promise<CorridorEvent[]> {
	const url = `${ORACLE_V1}/events/active`;
	const resp = await (opts.fresh ? requestFresh<Record<string, unknown>>(url) : request<Record<string, unknown>>(url));
	const rows = (Array.isArray(resp) ? resp : ((resp.events ?? resp.active_events ?? []) as unknown[])) as Record<
		string,
		unknown
	>[];
	return rows
		.map((r) => mapEventRow(r, 'live-events-api'))
		.filter((e): e is CorridorEvent => e !== null);
}

/** GET /api/v1/movements/active — convoy-era fallback rows for the board. */
export async function fetchMovementsAsEvents(): Promise<CorridorEvent[]> {
	const resp = await request<{ movements: Record<string, unknown>[] }>(
		`${ORACLE_V1}/movements/active`,
	);
	return (resp.movements ?? [])
		.map((m) =>
			mapEventRow(
				{
					event_id: m.movement_id,
					name: m.movement_id,
					event_kind: 'protected_movement',
					protection_class: m.vvip_class,
					state: m.status ?? 'active',
					route_segment_ids: m.route_segment_ids ?? m.segment_ids,
				},
				'live-movements-api',
			),
		)
		.filter((e): e is CorridorEvent => e !== null);
}

// ─── Situation band ──────────────────────────────────────────────────────────

const BANDS = ['GREEN', 'AMBER', 'RED', 'BLACK'] as const;

export interface SituationLogPoint {
	/** Server observation time (epoch ms of timestamp_utc). */
	ts: number;
	/** Committed (post-hysteresis) class — the wire situation_class. */
	band: BandCode;
	severityScore: number | null;
	/** Raw classification this tick, when the log carries observed_class. */
	observed: BandCode | null;
	/** The committed class changed on this tick (server flag). */
	transitioned: boolean;
	movementId: string | null;
	/**
	 * C-Q input provenance of the reading: true when it was computed from a
	 * SIMULATED feed, false from a real one, null on rows that predate it.
	 */
	simulatedFeed: boolean | null;
	/** inputs_source / data_quality as the oracle logged them (null when absent). */
	inputsSource: string | null;
	dataQuality: string | null;
}

/**
 * GET /api/v1/adaptive/situation_log?event_id= — the selected subject's band
 * history, oldest → newest. A subject id is REQUIRED: the unscoped log mixes
 * every subject the oracle ever observed (including completed events), so it
 * is never a band source. Rows without a parseable server timestamp are
 * dropped — they cannot be placed honestly on the time axis.
 */
export async function fetchSituationLog(subjectId: string): Promise<SituationLogPoint[]> {
	const id = safePathId(subjectId);
	if (!id) throw new ApiDown(`invalid situation subject: ${subjectId}`);
	const resp = await request<Record<string, unknown>>(
		`${ORACLE_V1}/adaptive/situation_log?event_id=${encodeURIComponent(id)}&limit=120`,
	);
	return parseSituationLog(resp);
}

/** Pure parser behind fetchSituationLog (exported for the contract tests). */
export function parseSituationLog(resp: unknown): SituationLogPoint[] {
	const body = (resp ?? {}) as Record<string, unknown>;
	const rows = (Array.isArray(resp) ? resp : ((body.entries ?? body.log ?? body.rows ?? []) as unknown[])) as Record<
		string,
		unknown
	>[];
	const out: SituationLogPoint[] = [];
	for (const r of rows) {
		const band = pick(r.situation_class ?? r.band, BANDS);
		if (!band) continue;
		const ts =
			typeof r.timestamp_utc === 'string' || typeof r.ts === 'string'
				? Date.parse(String(r.timestamp_utc ?? r.ts))
				: typeof r.timestamp === 'number'
					? r.timestamp
					: NaN;
		if (!Number.isFinite(ts)) continue;
		const dq = str(r.data_quality) ?? str(r.inputs_data_quality);
		out.push({
			ts,
			band,
			severityScore: typeof r.severity_score === 'number' ? r.severity_score : null,
			observed: pick(r.observed_class, BANDS),
			transitioned: r.transitioned === true,
			movementId: str(r.movement_id),
			simulatedFeed: typeof r.simulated_feed === 'boolean' ? r.simulated_feed : dq ? dq === 'synthetic' : null,
			inputsSource: str(r.inputs_source),
			dataQuality: dq,
		});
	}
	return out.sort((a, b) => a.ts - b.ts);
}

export interface AdaptiveStatusSnapshot {
	movements: Array<{
		id: string;
		vvipClass: string | null;
		/**
		 * The subject's committed band: the engine's effective_class (the
		 * situation_code is a composite 'Z:FREE_FLOW:CLEAR:LOW' code, never a
		 * band); situation_code only on builds that put the band there.
		 */
		situationCode: BandCode | null;
		severityScore: number | null;
		observationCount: number;
		/** Epoch ms the committed band was entered (band_since), when served. */
		bandSince: number | null;
		/** The subject's inputs came from a SIMULATED feed (null = not reported). */
		simulatedFeed: boolean | null;
	}>;
	degradationTier: string;
	auditTrail: Array<Record<string, unknown>>;
	lastShadowEval: string | null;
	/** The city band the signal writer acts on (null on builds without it). */
	cityBand: CityBandDoc | null;
}

/**
 * GET /api/v1/adaptive/status city_band — the band the city signal writer
 * acts on, with the moment it was ENTERED (since), whether its inputs are a
 * simulated feed, and whether the engine poll behind it is failing (stale
 * since …). A stale band is never shown as a band.
 */
export interface CityBandDoc {
	band: BandCode | null;
	/** Epoch ms the band was entered (null when unknown). */
	since: number | null;
	/**
	 * Epoch ms of the engine's newest evaluation behind this band (read
	 * time − engine_loop.last_tick_age_sec), when the status doc says;
	 * null on builds without the engine loop block. Set by
	 * fetchAdaptiveStatus, never by the pure parser.
	 */
	evaluatedAt?: number | null;
	subjects: number | null;
	simulatedFeed: boolean | null;
	stale: boolean;
	staleSince: number | null;
	staleReason: string | null;
}

/** Pure parser for the adaptive city_band block (exported for tests). */
export function parseCityBand(raw: unknown): CityBandDoc | null {
	if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
	const b = raw as Record<string, unknown>;
	const ms = (v: unknown) => {
		const t = typeof v === 'string' ? Date.parse(v) : NaN;
		return Number.isFinite(t) ? t : null;
	};
	return {
		band: pick(b.band, BANDS),
		since: ms(b.since),
		subjects: typeof b.subjects === 'number' ? b.subjects : null,
		simulatedFeed: typeof b.simulated_feed === 'boolean' ? b.simulated_feed : null,
		stale: b.stale === true,
		staleSince: ms(b.stale_since),
		staleReason: str(b.stale_reason),
	};
}

/** GET /api/v1/adaptive/status — the engine's subjects, degradation and city band. */
export async function fetchAdaptiveStatus(): Promise<AdaptiveStatusSnapshot> {
	const resp = await request<Record<string, unknown>>(`${ORACLE_V1}/adaptive/status`);
	const movements = (resp.movements ?? {}) as Record<string, Record<string, unknown>>;
	const degradation = (resp.degradation ?? {}) as Record<string, unknown>;
	const shadow = (resp.last_shadow_eval ?? {}) as Record<string, unknown>;
	return {
		movements: Object.entries(movements).map(([id, m]) => {
			const since = typeof m.band_since === 'string' ? Date.parse(m.band_since) : NaN;
			return {
				id,
				vvipClass: typeof m.vvip_class === 'string' ? m.vvip_class : null,
				situationCode: pick(m.effective_class, BANDS) ?? pick(m.situation_code, BANDS),
				severityScore: typeof m.severity_score === 'number' ? m.severity_score : null,
				observationCount: typeof m.observation_count === 'number' ? m.observation_count : 0,
				bandSince: Number.isFinite(since) ? since : null,
				simulatedFeed: typeof m.simulated_feed === 'boolean' ? m.simulated_feed : null,
			};
		}),
		degradationTier: typeof degradation.degradation_tier === 'string' ? degradation.degradation_tier : 'unknown',
		auditTrail: Array.isArray(degradation.audit_trail)
			? (degradation.audit_trail as Array<Record<string, unknown>>)
			: [],
		lastShadowEval: typeof shadow.status === 'string' ? shadow.status : null,
		cityBand: withEvaluatedAt(parseCityBand(resp.city_band), resp.engine_loop),
	};
}

/** The engine's newest evaluation (now − engine_loop.last_tick_age_sec), when reported. */
function withEvaluatedAt(doc: CityBandDoc | null, loop: unknown): CityBandDoc | null {
	if (!doc) return null;
	const age = loop && typeof loop === 'object' ? (loop as Record<string, unknown>).last_tick_age_sec : null;
	return { ...doc, evaluatedAt: typeof age === 'number' && Number.isFinite(age) && age >= 0 ? Date.now() - age * 1000 : null };
}

/**
 * GET /api/v1/adaptive/params?vvip_class= — band + shadow-eval evidence.
 *
 * NOT a poller rung: on the current oracle this GET creates subject state
 * and persists a param_overrides row per call (adaptive/service.py
 * params_for → log_params). The stores derive evidence from the read-only
 * /adaptive/overrides rows instead; this stays for explicit operator use.
 */
export async function fetchAdaptiveParams(cls: ProtectionClass): Promise<BandEvidence> {
	const resp = await request<Record<string, unknown>>(
		`${ORACLE_V1}/adaptive/params?vvip_class=${encodeURIComponent(cls === 'none' ? 'X' : cls)}`,
	);
	const evidence = (resp.evidence ?? {}) as Record<string, unknown>;
	return {
		shadowEval:
			typeof resp.shadow_eval === 'string'
				? resp.shadow_eval
				: typeof evidence.shadow_eval === 'string'
					? evidence.shadow_eval
					: null,
		floorApplied: typeof evidence.floor_applied === 'boolean' ? evidence.floor_applied : null,
		failClosedReason:
			typeof evidence.fail_closed_reason === 'string' ? evidence.fail_closed_reason : null,
		situationClass: pick(resp.situation_class, ['GREEN', 'AMBER', 'RED', 'BLACK']),
	};
}

/**
 * GET /api/v1/adaptive/overrides?event_id=&limit= — param-override audit rows
 * (contract C-C: {overrides:[{published_at, event_id, movement_id,
 * situation_class, params, source, shadow_eval, shadow_eval_detail}]}),
 * newest first. Read-only evidence for the AMA card and band panel.
 */
export async function fetchOverrides(eventId: string | null): Promise<OverrideRecord[]> {
	const id = safePathId(eventId);
	const qs = id ? `?event_id=${encodeURIComponent(id)}&limit=50` : '?limit=50';
	const resp = await request<Record<string, unknown>>(`${ORACLE_V1}/adaptive/overrides${qs}`);
	const rows = (Array.isArray(resp) ? resp : ((resp.overrides ?? resp.rows ?? []) as unknown[])) as Record<
		string,
		unknown
	>[];
	return rows.map((r) => {
		const evidence =
			r.evidence && typeof r.evidence === 'object' ? (r.evidence as Record<string, unknown>) : {};
		const floor = r.floor_applied ?? evidence.floor_applied;
		return {
			ts: str(r.published_at) ?? str(r.timestamp_utc),
			eventId: str(r.event_id),
			protectionClass: pick(r.protection_class ?? r.vvip_class, PROTECTION),
			situationClass: pick(r.situation_class, BANDS),
			shadowEval: str(r.shadow_eval),
			floorApplied: typeof floor === 'boolean' ? floor : null,
			failClosedReason: str(r.fail_closed_reason) ?? str(evidence.fail_closed_reason),
			params: (r.params ?? null) as Record<string, unknown> | null,
			movementId: str(r.movement_id),
			source: str(r.source),
			shadowEvalDetail: str(r.shadow_eval_detail) ?? str(evidence.shadow_eval_detail),
		};
	});
}

// ─── Twin shadow-eval comparison ─────────────────────────────────────────────

interface TwinRunRow {
	replication?: number;
	total_disruption_vehicle_hours?: number;
	max_queue_length_m?: number;
	avg_closure_duration_sec?: number;
	convoy_transit_time_sec?: number;
	recovery_sec?: number;
	ambulance_delay_sec?: number;
}

function meanRuns(rows: TwinRunRow[]): TwinArm['metrics'] {
	const num = (xs: (number | undefined | null)[]) => {
		const valid = xs.filter((x): x is number => typeof x === 'number' && Number.isFinite(x));
		return valid.length ? valid.reduce((a, b) => a + b, 0) / valid.length : null;
	};
	return {
		disruptionVehicleHours: num(rows.map((r) => r.total_disruption_vehicle_hours)),
		maxQueueLengthM: num(rows.map((r) => r.max_queue_length_m)),
		avgClosureDurationSec: num(rows.map((r) => r.avg_closure_duration_sec)),
		transitTimeSec: num(rows.map((r) => r.convoy_transit_time_sec)),
		recoverySec: num(rows.map((r) => r.recovery_sec)),
		ambulanceDelaySec: num(rows.map((r) => r.ambulance_delay_sec)),
	};
}

function mkArm(label: string, policy: ClosurePolicySpec, rows: TwinRunRow[]): TwinArm {
	return { label, policy, runs: rows.length, metrics: meanRuns(rows) };
}

/**
 * POST /api/v1/simulate/twin/compare — the contract-C2 comparison endpoint.
 * Both arms + deltas server-side; cached per (scenario, policies, seed) by the
 * store and refreshed only on operator demand (never on an interval).
 */
export async function compareTwinPolicies(
	scenarioId: string,
	baselinePolicy: ClosurePolicySpec,
	candidatePolicy: ClosurePolicySpec,
	seed?: number,
): Promise<TwinCompareResult> {
	const id = safePathId(scenarioId);
	if (!id) throw new ApiDown(`invalid scenario id: ${scenarioId}`);
	const resp = await post<Record<string, unknown>>(
		`${ORACLE_V1}/simulate/twin/compare`,
		{
			scenario_id: id,
			baseline_policy: {
				advance_closure_sec: baselinePolicy.advanceClosureSec,
				closure_type: baselinePolicy.closureType,
			},
			candidate_policy: {
				advance_closure_sec: candidatePolicy.advanceClosureSec,
				closure_type: candidatePolicy.closureType,
			},
			...(seed != null ? { seed } : {}),
		},
		120000,
	);
	const baseline = (resp.baseline ?? {}) as Record<string, unknown>;
	const candidate = (resp.candidate ?? {}) as Record<string, unknown>;
	const bRuns = (baseline.runs ?? []) as TwinRunRow[];
	const cRuns = (candidate.runs ?? []) as TwinRunRow[];
	const bArm = mkArm('baseline', baselinePolicy, bRuns);
	const cArm = mkArm('candidate', candidatePolicy, cRuns);
	return {
		scenarioId: id,
		baseline: bArm,
		candidate: cArm,
		deltas: deltaOf(bArm, cArm),
		source: 'compare-endpoint',
		calibrated: typeof resp.calibrated === 'boolean' ? resp.calibrated : null,
		forecastCoupling: typeof resp.forecast_coupling === 'string' ? resp.forecast_coupling : null,
		fetchedAt: Date.now(),
	};
}

/**
 * Labelled fallback when /simulate/twin/compare is not deployed: run the two
 * arms DIRECTLY on POST /api/v1/simulate/twin with fully explicit fields and
 * NO scenario_id — the C2 bug was scenario_id clobbering explicit policies,
 * and this path does not use scenario_id, so the explicit policies stand.
 */
export async function compareTwinPoliciesDirect(
	scenarioId: string,
	baselinePolicy: ClosurePolicySpec,
	candidatePolicy: ClosurePolicySpec,
	explicit: {
		routeSegmentIds: number[];
		departureTime: string;
		horizonMin: number;
		nReplications: number;
		seed: number;
	},
): Promise<TwinCompareResult> {
	const id = safePathId(scenarioId);
	if (!id) throw new ApiDown(`invalid scenario id: ${scenarioId}`);
	const arm = async (policy: ClosurePolicySpec) => {
		const resp = await post<Record<string, unknown>>(
			`${ORACLE_V1}/simulate/twin`,
			{
				route_segment_ids: explicit.routeSegmentIds,
				departure_time: explicit.departureTime,
				closure_policy: {
					advance_closure_sec: policy.advanceClosureSec,
					closure_type: policy.closureType,
				},
				horizon_min: explicit.horizonMin,
				n_replications: explicit.nReplications,
				seed: explicit.seed,
			},
			120000,
		);
		return (resp.runs ?? []) as TwinRunRow[];
	};
	const [bRuns, cRuns] = await Promise.all([arm(baselinePolicy), arm(candidatePolicy)]);
	const bArm = mkArm('baseline', baselinePolicy, bRuns);
	const cArm = mkArm('candidate', candidatePolicy, cRuns);
	return {
		scenarioId: id,
		baseline: bArm,
		candidate: cArm,
		deltas: deltaOf(bArm, cArm),
		source: 'direct-two-arm',
		calibrated: null,
		forecastCoupling: null,
		fetchedAt: Date.now(),
	};
}

function deltaOf(b: TwinArm, c: TwinArm): Record<string, number | null> {
	const d: Record<string, number | null> = {};
	for (const [k, v] of Object.entries(c.metrics)) {
		const bv = b.metrics[k as keyof TwinArm['metrics']];
		d[k] = v != null && bv != null ? v - bv : null;
	}
	return d;
}

// ─── Emulator (same-origin proxy; absolute loopback is a dev-only rung) ──────

/**
 * Fetch against the emulator through the deck origin's /api/emulator proxy
 * (vite dev proxy / hooks.server.ts). Only under the vite dev server, when
 * that proxy path is unreachable, the absolute loopback constant is tried
 * second. The production build never requests http://localhost:8090 — that
 * call was CORS-blocked from every non-:5173 origin and flooded the console.
 * Both bases are fixed config constants; `path` is a fixed template.
 */
async function emulatorFetch(path: string, init?: RequestInit, timeoutMs = 6000): Promise<Response> {
	const bases = DEV_SERVER ? [EMULATOR_PROXY_PATH, EMULATOR_BASE] : [EMULATOR_PROXY_PATH];
	let lastErr: unknown = null;
	for (const base of bases) {
		try {
			const res = await fetch(`${base}${path}`, { ...init, signal: AbortSignal.timeout(timeoutMs) });
			// A 404/502 on the PROXY rung means the dev proxy entry is missing
			// or its upstream is down: only then is the dev-only absolute rung
			// worth a try. Any other answer is the emulator speaking.
			if (base === EMULATOR_PROXY_PATH && bases.length > 1 && (res.status === 404 || res.status === 502)) {
				lastErr = new Error(`emulator proxy ${res.status}`);
				continue;
			}
			return res;
		} catch (err) {
			lastErr = err;
		}
	}
	throw lastErr instanceof Error ? lastErr : new Error('emulator unreachable');
}

/** What GET /health on the emulator reports (contract C-G). */
export interface EmulatorHealth {
	online: boolean;
	detail: string;
	/** stack probes {store, valkey, twin, nats} when reported. */
	stack: Record<string, boolean> | null;
	/**
	 * The C-G living_city block — present only on a build that serves the
	 * living-city routes, so its absence means "not in this build" (the panel
	 * then never requests the route, instead of collecting 404s).
	 */
	livingCity: { enabled: boolean; status: string | null; activeSlots: number | null } | null;
}

/** GET /health on the emulator ({status, library, stack, living_city?}). */
export async function emulatorHealth(): Promise<EmulatorHealth> {
	try {
		const res = await emulatorFetch('/health', undefined, 4000);
		const text = await res.text().catch(() => '');
		let body: Record<string, unknown> = {};
		try {
			body = JSON.parse(text) as Record<string, unknown>;
		} catch {
			// non-JSON answer: keep the raw text as the detail
		}
		const stackRaw = body.stack && typeof body.stack === 'object' ? (body.stack as Record<string, unknown>) : null;
		const stack = stackRaw
			? Object.fromEntries(Object.entries(stackRaw).map(([k, v]) => [k, v === true || v === 'ok' || v === 'connected']))
			: null;
		const lc = body.living_city && typeof body.living_city === 'object' ? (body.living_city as Record<string, unknown>) : null;
		const status = typeof body.status === 'string' ? body.status : null;
		return {
			online: res.ok,
			detail: res.ok ? `GET /health ${res.status}${status ? ` · ${status}` : ''}` : `${res.status}: ${text.slice(0, 160)}`,
			stack,
			livingCity: lc
				? {
						enabled: lc.enabled === true,
						status: typeof lc.status === 'string' ? lc.status : null,
						activeSlots: typeof lc.active_slots === 'number' ? lc.active_slots : null,
					}
				: null,
		};
	} catch (err) {
		return { online: false, detail: err instanceof Error ? err.message : String(err), stack: null, livingCity: null };
	}
}

/** A flagship tape: by category (multi_event | enforcement) or by name. */
export function isHeroUseCase(id: string, category: string | null): boolean {
	return (category != null && EMULATOR_HERO_CATEGORIES.includes(category)) || EMULATOR_HERO_USE_CASES.includes(id);
}

/** Pure parser for one GET /api/v1/emulator/use-cases row (exported for tests). */
export function parseUseCase(r: Record<string, unknown>): EmulatorUseCaseInfo | null {
	const id = str(r.id);
	if (!id) return null;
	const category = str(r.category);
	return {
		id,
		name: str(r.name) ?? id,
		category,
		vvipClass: str(r.vvip_class),
		horizonMin: typeof r.horizon_min === 'number' ? r.horizon_min : null,
		seed: typeof r.seed === 'number' ? r.seed : null,
		routeSegmentIds: segmentIds(r.route_segment_ids),
		hero: isHeroUseCase(id, category),
	};
}

/** GET /api/v1/emulator/use-cases — the authoritative cause-tape library (10 tapes). */
export async function emulatorUseCases(): Promise<EmulatorUseCaseInfo[]> {
	const res = await emulatorFetch('/api/v1/emulator/use-cases', undefined, 5000);
	if (!res.ok) throw new ApiDown(`use-cases ${res.status}`);
	const body = (await res.json()) as unknown;
	const rows = (Array.isArray(body) ? body : ((body as Record<string, unknown>)?.use_cases ?? [])) as Array<
		Record<string, unknown>
	>;
	return rows.map(parseUseCase).filter((u): u is EmulatorUseCaseInfo => u !== null);
}

function runProgressOf(r: Record<string, unknown>, spec: Record<string, unknown>): EmulatorRunProgress | null {
	const p = r.progress && typeof r.progress === 'object' ? (r.progress as Record<string, unknown>) : null;
	const num = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : null);
	if (p) {
		const total = num(p.total);
		return {
			frame: num(p.frame) ?? 0,
			total: total ?? 0,
			delivered: num(p.delivered) ?? 0,
			failed: num(p.failed) ?? 0,
		};
	}
	// A finished run on an older build: the ledger's final delivery summary.
	const d = spec.delivery && typeof spec.delivery === 'object' ? (spec.delivery as Record<string, unknown>) : null;
	if (d && num(d.frames) != null) {
		return {
			frame: num(d.frames) ?? 0,
			total: num(d.frames) ?? 0,
			delivered: num(d.delivered) ?? 0,
			failed: num(d.failed) ?? 0,
		};
	}
	return null;
}

/**
 * Pure parser for one run-ledger row (GET /runs item, GET /runs/{id}, or a
 * POST answer). Accepts the C-G additive fields (progress, event_id,
 * movement_id, use_case_id at top level) and the older shape (use case and
 * delivery inside `spec`). Exported for tests.
 */
export function parseEmulatorRun(r: Record<string, unknown>): EmulatorRunRow | null {
	const runId = typeof r.run_id === 'number' ? r.run_id : Number(r.run_id);
	if (!Number.isFinite(runId) || runId < 0) return null;
	const spec = (r.spec && typeof r.spec === 'object' ? r.spec : {}) as Record<string, unknown>;
	const replayOf = spec.replay_of_run_id ?? r.replay_of_run_id;
	return {
		runId,
		useCaseId: str(r.use_case_id) ?? str(spec.use_case_id) ?? '—',
		status: str(r.status) ?? '—',
		clockScale:
			typeof r.clock_scale === 'number' ? r.clock_scale : typeof spec.clock_scale === 'number' ? spec.clock_scale : null,
		isReplay: spec.is_replay === true || r.is_replay === true,
		replayOf: typeof replayOf === 'number' ? replayOf : null,
		createdAt: str(r.created_at),
		endedAt: str(r.ended_at),
		progress: runProgressOf(r, spec),
		eventId: str(r.event_id),
		movementId: str(r.movement_id),
		// 'planning' mid-run = the corridor event's activation is still pending
		eventStatus: str(r.event_status) ?? str(spec.event_status),
		failReason: str(r.reason) ?? str(spec.reason),
		premises: parseRunPremises(spec.scenario_precondition ?? r.scenario_precondition),
	};
}

/**
 * Pure: a run's spec.scenario_precondition (C-N — the stolen-vehicle sweep's
 * EMULATED stolen-FIR earmark, created / already active / failed) → its
 * entries; [] for anything else. Exported for the contract tests.
 */
export function parseRunPremises(v: unknown): EmulatorRunPremise[] {
	const rows = Array.isArray(v) ? v : v && typeof v === 'object' ? [v] : [];
	const out: EmulatorRunPremise[] = [];
	for (const raw of rows) {
		if (!raw || typeof raw !== 'object' || Array.isArray(raw)) continue;
		const e = raw as Record<string, unknown>;
		const result = str(e.result);
		if (!result) continue;
		out.push({
			plate: str(e.plate),
			result,
			earmark: str(e.earmark),
			firRef: str(e.fir_ref),
			reason: str(e.reason),
		});
	}
	return out;
}

/** GET /api/v1/emulator/runs?limit= — recent cause-tape runs, newest first. */
export async function emulatorListRuns(limit = 8): Promise<EmulatorRunRow[]> {
	const res = await emulatorFetch(`/api/v1/emulator/runs?limit=${Math.min(Math.max(1, limit), 50)}`, undefined, 5000);
	if (!res.ok) throw new ApiDown(`runs ${res.status}`);
	const resp = (await res.json()) as { runs?: Array<Record<string, unknown>> };
	return (resp.runs ?? []).map(parseEmulatorRun).filter((r): r is EmulatorRunRow => r !== null);
}

/**
 * GET /api/v1/emulator/runs/{id}?include_tape=false — one run, with C-G
 * progress while it runs. The progress poll never needs the tape's frames
 * (a full row is ~190 KB every 2 s); builds before C-G ignore the flag.
 */
export async function emulatorGetRun(runId: number): Promise<EmulatorRunRow> {
	if (!Number.isInteger(runId) || runId < 0) throw new ApiDown(`invalid run id: ${runId}`);
	const res = await emulatorFetch(`/api/v1/emulator/runs/${runId}?include_tape=false`, undefined, 5000);
	if (res.status === 404) throw new ApiNotDeployed(404, `run ${runId} not found`);
	if (!res.ok) throw new ApiDown(`run ${runId}: ${res.status}`);
	const row = parseEmulatorRun((await res.json()) as Record<string, unknown>);
	if (!row) throw new ApiDown(`run ${runId}: unparseable ledger row`);
	return row;
}

/**
 * The answer to a run POST (launch or replay):
 *   accepted — 202 {run_id, status:'running'} (C-G async), or an older
 *              build's 200 carrying the FINISHED run (it held the request
 *              until the tape ended; `status` says how it ended);
 *   conflict — 409 {detail, run_id}: that use case is already running;
 *   refused  — any other answer (404 unknown use case, 422, 5xx, a proxy
 *              that gave up waiting) — the detail is verbatim.
 */
export type EmulatorPostResult =
	| { kind: 'accepted'; httpStatus: number; runId: number | null; status: string; detail: string }
	| { kind: 'conflict'; httpStatus: 409; runId: number | null; detail: string }
	| { kind: 'refused'; httpStatus: number; detail: string };

/** Pure classifier behind the run POSTs (exported for tests). */
export function classifyRunPost(httpStatus: number, bodyText: string): EmulatorPostResult {
	let body: Record<string, unknown> = {};
	try {
		body = JSON.parse(bodyText) as Record<string, unknown>;
	} catch {
		// non-JSON (a proxy error page): the text is the detail
	}
	const detailOf = () => {
		const d = body.detail;
		if (typeof d === 'string') return d;
		if (d && typeof d === 'object') return JSON.stringify(d).slice(0, 220);
		return bodyText.slice(0, 220) || `HTTP ${httpStatus}`;
	};
	const runIdRaw = body.run_id ?? (body.detail && typeof body.detail === 'object' ? (body.detail as Record<string, unknown>).run_id : null);
	const runId = typeof runIdRaw === 'number' ? runIdRaw : null;
	if (httpStatus === 202 || httpStatus === 200) {
		return {
			kind: 'accepted',
			httpStatus,
			runId,
			status: str(body.status) ?? (httpStatus === 202 ? 'running' : 'completed'),
			detail: `${httpStatus} ${str(body.status) ?? ''}`.trim(),
		};
	}
	if (httpStatus === 409) return { kind: 'conflict', httpStatus: 409, runId, detail: detailOf() };
	return { kind: 'refused', httpStatus, detail: detailOf() };
}

async function emulatorPostRun(path: string, body: Record<string, unknown>, timeoutMs: number): Promise<EmulatorPostResult> {
	try {
		const res = await emulatorFetch(
			path,
			{ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) },
			timeoutMs,
		);
		return classifyRunPost(res.status, await res.text().catch(() => ''));
	} catch (err) {
		return {
			kind: 'refused',
			httpStatus: 0,
			detail: `no answer (${err instanceof Error ? err.message : String(err)}) — a run that started keeps executing server-side`,
		};
	}
}

/**
 * POST /api/v1/emulator/runs {use_case_id, clock_scale} — launch a tape.
 * C-G answers 202 right after the ledger insert; an older build holds the
 * request until the tape ends. Callers must NOT block the UI on this
 * promise: the store races it and tracks the run from the ledger.
 */
export function emulatorLaunchRun(useCaseId: string, clockScale: number): Promise<EmulatorPostResult> {
	const id = safePathId(useCaseId);
	if (!id) return Promise.resolve({ kind: 'refused', httpStatus: 0, detail: 'invalid use-case id' });
	const scale = Math.min(3600, Math.max(1, Math.round(clockScale)));
	return emulatorPostRun('/api/v1/emulator/runs', { use_case_id: id, clock_scale: scale }, 30 * 60_000);
}

/** POST /api/v1/emulator/runs/{id}/replay {clock_scale} — replay a recorded tape. */
export function emulatorReplayRun(runId: number, clockScale: number): Promise<EmulatorPostResult> {
	if (!Number.isInteger(runId) || runId < 0) {
		return Promise.resolve({ kind: 'refused', httpStatus: 0, detail: `invalid run id: ${runId}` });
	}
	const scale = Math.min(3600, Math.max(1, Math.round(clockScale)));
	return emulatorPostRun(`/api/v1/emulator/runs/${runId}/replay`, { clock_scale: scale }, 30 * 60_000);
}

/**
 * The answer to a STOP: `runStatus` is the status the emulator reports for
 * the run AFTER the call — 'stopped' when this STOP ended it, the run's own
 * final status ('completed', 'interrupted', …) when it had already ended.
 */
export interface EmulatorStopResult {
	ok: boolean;
	status: number;
	runStatus: string | null;
	detail: string;
}

/** Pure classifier behind STOP (exported for tests). */
export function classifyStop(httpStatus: number, bodyText: string): EmulatorStopResult {
	let body: Record<string, unknown> = {};
	try {
		body = JSON.parse(bodyText) as Record<string, unknown>;
	} catch {
		// non-JSON (a proxy error page): the text is the detail
	}
	const detail = typeof body.detail === 'string' ? body.detail : null;
	if (httpStatus >= 200 && httpStatus < 300) {
		const runStatus = str(body.status) ?? 'stopped';
		return { ok: true, status: httpStatus, runStatus, detail: detail ?? runStatus };
	}
	// No route at all (FastAPI's bare 'Not Found') vs C-G's own 404 for an unknown run.
	if (httpStatus === 405 || (httpStatus === 404 && (!detail || detail === 'Not Found'))) {
		return { ok: false, status: httpStatus, runStatus: null, detail: 'stop is not served by this emulator build (contract C-G pending)' };
	}
	// 404 {detail:'run N not found'} · 409 {detail:'run is owned by another emulator process'}
	return { ok: false, status: httpStatus, runStatus: null, detail: detail ?? (bodyText.slice(0, 200) || `HTTP ${httpStatus}`) };
}

/**
 * POST /api/v1/emulator/runs/{id}/stop (C-G) → 200 {run_id, status:'stopped'};
 * the emulator then completes the run's corridor event through C-F. A run
 * that had already ended answers 200 with ITS status ('run already
 * finished') — reported as that, never as a stop. A build without the
 * route answers 404/405 — reported as such.
 */
export async function emulatorStopRun(runId: number): Promise<EmulatorStopResult> {
	if (!Number.isInteger(runId) || runId < 0) return { ok: false, status: 0, runStatus: null, detail: `invalid run id: ${runId}` };
	try {
		const res = await emulatorFetch(`/api/v1/emulator/runs/${runId}/stop`, { method: 'POST' }, 10000);
		return classifyStop(res.status, await res.text().catch(() => ''));
	} catch (err) {
		return { ok: false, status: 0, runStatus: null, detail: err instanceof Error ? err.message : String(err) };
	}
}

/** Pure parser for the C-G living-city document (exported for tests). */
export function parseLivingCity(body: unknown): LivingCityDoc | null {
	if (!body || typeof body !== 'object') return null;
	const b = body as Record<string, unknown>;
	const slots = Array.isArray(b.active_slots) ? (b.active_slots as Array<Record<string, unknown>>) : [];
	const causes = Array.isArray(b.next_causes) ? (b.next_causes as Array<Record<string, unknown>>) : [];
	return {
		enabled: b.enabled === true,
		status: str(b.status) ?? (b.enabled === true ? 'running' : 'stopped'),
		dateIst: str(b.date_ist),
		daySeed: typeof b.day_seed === 'number' ? b.day_seed : null,
		activeSlots: slots.map((s) => ({
			slotId: str(s.slot_id) ?? '—',
			kind: str(s.kind) ?? '—',
			title: str(s.title) ?? str(s.kind) ?? '—',
			eventId: str(s.event_id),
			startsAt: str(s.starts_at),
			endsAt: str(s.ends_at),
			routeSegmentIds: segmentIds(s.route_segment_ids),
			eventStatus: str(s.event_status),
		})),
		nextCauses: causes.map((c) => ({ at: str(c.at), kind: str(c.kind) ?? '—', title: str(c.title) ?? str(c.kind) ?? '—' })),
		units: Array.isArray(b.units) ? b.units.filter((u): u is string => typeof u === 'string') : [],
		since: str(b.since),
		violators: typeof b.violators === 'boolean' ? b.violators : null,
		yielded: Array.isArray(b.yielded) ? b.yielded.filter((y): y is string => typeof y === 'string') : [],
		preempted: b.preempted && typeof b.preempted === 'object' && !Array.isArray(b.preempted) ? Object.keys(b.preempted as object).length : 0,
		refiling: Array.isArray(b.refiling) ? b.refiling.length : 0,
		counters: countersOf(b.counters),
		detail: str(b.detail) ?? str(b.last_error),
	};
}

/** A counters object as name → finite number (anything else dropped). */
function countersOf(v: unknown): Record<string, number> {
	const out: Record<string, number> = {};
	if (!v || typeof v !== 'object' || Array.isArray(v)) return out;
	for (const [k, n] of Object.entries(v as Record<string, unknown>)) {
		if (typeof n === 'number' && Number.isFinite(n)) out[k] = n;
	}
	return out;
}

/** GET /api/v1/emulator/living-city (C-G) — the ambient day program. */
export async function emulatorLivingCity(): Promise<LivingCityDoc> {
	const res = await emulatorFetch('/api/v1/emulator/living-city', undefined, 5000);
	if (res.status === 404) throw new ApiNotDeployed(404, 'living-city not served by this emulator build');
	if (!res.ok) throw new ApiDown(`living-city ${res.status}`);
	const doc = parseLivingCity(await res.json());
	if (!doc) throw new ApiDown('living-city: unparseable document');
	return doc;
}

/** POST /api/v1/emulator/living-city {action: start|stop} (C-G). */
export async function emulatorSetLivingCity(action: 'start' | 'stop'): Promise<LivingCityDoc> {
	const res = await emulatorFetch(
		'/api/v1/emulator/living-city',
		{ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action }) },
		10000,
	);
	if (res.status === 404) throw new ApiNotDeployed(404, 'living-city not served by this emulator build');
	if (!res.ok) throw new ApiDown(`living-city ${action}: ${res.status} ${(await res.text().catch(() => '')).slice(0, 160)}`);
	const doc = parseLivingCity(await res.json());
	if (!doc) throw new ApiDown('living-city: unparseable document');
	return doc;
}

// ─── Audit stream: NDJSON tail + WS ladder ───────────────────────────────────

/** The C-D `done` payload (absent from old servers). */
export interface AuditProbeDone {
	sessionId: string | null;
	tiersUsed: string[];
	operatorAlerts: string[];
	degraded: boolean | null;
}

export interface AuditStreamCallbacks {
	/** One reasoning step; `tier` is decider | gemma | heuristic (C-D thought.tier). */
	onThought: (text: string, tier: string | null) => void;
	onToken: (text: string) => void;
	onToolCall: (name: string, args: Record<string, unknown>, id: string | null) => void;
	/** `ok` false = the tool failed; `summary` is the brain's one-line result. */
	onToolResult: (name: string, ok: boolean, summary: string) => void;
	onDone: (done: AuditProbeDone | null) => void;
	onError: (message: string) => void;
}

/** What the probe is about: the selected event's movement + protection class. */
export interface AuditProbeContext {
	movementId: string | null;
	vvipClass: string | null;
	/**
	 * The selected corridor event (C-D2 ChatRequest.event_id) and its kind.
	 * Ids only — never the title, which can carry a plate (C6).
	 */
	eventId?: string | null;
	eventKind?: string | null;
	sessionId?: string | null;
	timeoutMs?: number;
}

/**
 * The probe message as sent: an ids-only scope line first ('[selected
 * event 4508d86d-… · vehicle_pursuit]'), so a brain without C-D2
 * event_id still grounds the turn in the event the operator selected.
 */
export function scopedProbeMessage(message: string, eventId: string | null | undefined, eventKind?: string | null): string {
	const id = safePathId(eventId ?? null);
	if (!id) return message;
	const kind = eventKind && /^[a-z_]{1,40}$/.test(eventKind) ? ` · ${eventKind}` : '';
	return `[selected event ${id}${kind}] ${message}`;
}

/** Brain thought tier → the audit actor: the Gemma tier is the TACTICAL tier. */
export function probeActorOfTier(tier: string | null | undefined): string {
	if (tier === 'gemma') return 'tactical';
	if (tier === 'decider') return 'decider';
	if (tier === 'heuristic') return 'heuristic';
	return 'brain';
}

/**
 * Feed one parsed NDJSON line to the callbacks; returns true on a terminal
 * line (done | error). Accepts the C-D shapes and the legacy ones
 * (toolName/arguments, state/durationMs). Exported for tests.
 */
export function dispatchProbeLine(
	ev: { type?: unknown; data?: unknown },
	callbacks: AuditStreamCallbacks,
	tools: Map<string, string>,
): boolean {
	const d = (ev.data && typeof ev.data === 'object' ? ev.data : {}) as Record<string, unknown>;
	switch (ev.type) {
		case 'token':
			callbacks.onToken(typeof ev.data === 'string' ? ev.data : '');
			return false;
		case 'thought':
			callbacks.onThought(typeof d.text === 'string' ? d.text : JSON.stringify(ev.data), str(d.tier));
			return false;
		case 'tool_call': {
			const id = str(d.id) ?? str(d.callId);
			const name = str(d.name) ?? str(d.toolName) ?? str(d.tool_name) ?? 'tool';
			if (id) tools.set(id, name);
			const args = (d.args ?? d.arguments ?? {}) as Record<string, unknown>;
			callbacks.onToolCall(name, args && typeof args === 'object' ? args : {}, id);
			return false;
		}
		case 'tool_result': {
			const id = str(d.id) ?? str(d.callId);
			const name = str(d.name) ?? (id ? tools.get(id) : null) ?? 'tool';
			const ok = typeof d.ok === 'boolean' ? d.ok : d.state !== 'error' && d.state !== 'failed';
			const summary = str(d.summary) ?? (typeof d.state === 'string' ? d.state : ok ? 'ok' : 'failed');
			callbacks.onToolResult(name, ok, summary);
			return false;
		}
		case 'done': {
			const doneData = ev.data && typeof ev.data === 'object' ? (ev.data as Record<string, unknown>) : null;
			callbacks.onDone(
				doneData
					? {
							sessionId: str(doneData.session_id),
							tiersUsed: Array.isArray(doneData.tiers_used) ? doneData.tiers_used.map(String) : [],
							operatorAlerts: Array.isArray(doneData.operator_alerts) ? doneData.operator_alerts.map(String) : [],
							degraded: typeof doneData.degraded === 'boolean' ? doneData.degraded : null,
						}
					: null,
			);
			return true;
		}
		case 'error':
			callbacks.onError(String(ev.data));
			return true;
		default:
			return false; // unknown types are ignored (C-D)
	}
}

/**
 * NDJSON audit probe over POST /chat/stream — consumed with a ReadableStream
 * reader (never EventSource; the endpoint is NDJSON, not SSE). Streams the
 * decider → Gemma (tactical) → tool ladder for the SELECTED event: its
 * movement id and protection class ride in the body. One AbortController
 * bounds the whole turn; every failure — refused, unreachable, timed out —
 * lands in onError, never as a thrown promise.
 */
export async function streamAuditProbe(
	message: string,
	ctx: AuditProbeContext,
	callbacks: AuditStreamCallbacks,
): Promise<void> {
	const ctrl = new AbortController();
	const timeoutMs = ctx.timeoutMs ?? 120_000;
	const timer = setTimeout(() => ctrl.abort(), timeoutMs);
	const tools = new Map<string, string>();
	try {
		const res = await fetch(`${BRAIN_API}/chat/stream`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({
				message: scopedProbeMessage(message, ctx.eventId, ctx.eventKind),
				movement_id: ctx.movementId,
				vvip_class: ctx.vvipClass,
				...(safePathId(ctx.eventId ?? null) ? { event_id: ctx.eventId } : {}),
				...(ctx.sessionId ? { session_id: ctx.sessionId } : {}),
			}),
			signal: ctrl.signal,
		});
		if (!res.ok || !res.body) {
			callbacks.onError(`chat/stream ${res.status} ${res.statusText}`);
			return;
		}
		const reader = res.body.getReader();
		const decoder = new TextDecoder();
		let buffer = '';
		const feed = (line: string): boolean => {
			const trimmed = line.trim();
			if (!trimmed) return false;
			try {
				return dispatchProbeLine(JSON.parse(trimmed) as { type?: unknown; data?: unknown }, callbacks, tools);
			} catch {
				return false; // a malformed line is skipped, the turn continues
			}
		};
		while (true) {
			const { done, value } = await reader.read();
			if (done) break;
			buffer += decoder.decode(value, { stream: true });
			const lines = buffer.split('\n');
			buffer = lines.pop() ?? '';
			for (const line of lines) {
				if (feed(line)) {
					void reader.cancel().catch(() => undefined);
					return;
				}
			}
		}
		// a final line without a trailing newline
		if (buffer.trim() && feed(buffer)) return;
		callbacks.onDone(null);
	} catch (err) {
		callbacks.onError(
			err instanceof DOMException && err.name === 'AbortError'
				? `chat/stream timed out after ${Math.round(timeoutMs / 1000)} s`
				: `chat/stream unreachable: ${err instanceof Error ? err.message : String(err)}`,
		);
	} finally {
		clearTimeout(timer);
	}
}

/** Raw brain WS event (camelCase payload, per /ws/convoy handler). */
export interface BrainWsEvent {
	type: string;
	timestamp?: number;
	payload?: Record<string, unknown>;
}

export interface AuditSocket {
	close: () => void;
	/** Which rung actually connected. */
	rung: 'ws-event' | 'ws-convoy' | 'unavailable';
}

/**
 * WS audit ladder: try the generalized /ws/event/{id} first, fall through to
 * today's /ws/convoy/{movement_id}, else report unavailable. The URL is built
 * from the page origin + fixed path templates + a safePathId-validated id.
 */
export function createAuditSocket(
	eventId: string,
	onEvent: (ev: BrainWsEvent) => void,
	onRungChange: (rung: AuditSocket['rung'] | 'connecting') => void,
	openTimeoutMs: number,
): AuditSocket {
	const id = safePathId(eventId);
	if (!id) {
		onRungChange('unavailable');
		return { close: () => {}, rung: 'unavailable' };
	}
	const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
	const host = window.location.host;
	// Same-origin WS: host is the deck origin (fixed), scheme ws/wss — permitted.
	const candidates: Array<{ url: string; rung: AuditSocket['rung'] }> = [
		{ url: `${protocol}//${host}${BRAIN_API}/ws/event/${id}`, rung: 'ws-event' },
		{ url: `${protocol}//${host}${BRAIN_API}/ws/convoy/${id}`, rung: 'ws-convoy' },
	];

	let ws: WebSocket | null = null;
	let closed = false;
	let idx = 0;
	let settled: AuditSocket['rung'] = 'unavailable';
	let timer: ReturnType<typeof setTimeout> | null = null;

	function tryNext() {
		if (closed) return;
		if (idx >= candidates.length) {
			onRungChange('unavailable');
			return;
		}
		const cand = candidates[idx++];
		onRungChange('connecting');
		ws = new WebSocket(cand.url);
		timer = setTimeout(() => {
			// Connection never opened within grace → next rung.
			ws?.close();
		}, openTimeoutMs);

		ws.onopen = () => {
			if (timer) clearTimeout(timer);
			settled = cand.rung;
			onRungChange(cand.rung);
		};
		ws.onmessage = (ev) => {
			try {
				onEvent(JSON.parse(ev.data) as BrainWsEvent);
			} catch {
				// ignore malformed frames
			}
		};
		ws.onclose = () => {
			if (closed) return;
			if (settled === cand.rung) {
				// Was connected on this rung; server closed — retry the SAME rung
				// after a beat rather than silently downgrading.
				onRungChange('connecting');
				idx--; // retry current rung once, then advance
				setTimeout(tryNext, 3000);
				return;
			}
			if (timer) clearTimeout(timer);
			tryNext();
		};
		ws.onerror = () => {
			ws?.close();
		};
	}

	tryNext();

	return {
		close() {
			closed = true;
			if (timer) clearTimeout(timer);
			ws?.close();
		},
		get rung() {
			return settled;
		},
	};
}

// ─── Audit poll rung (2 s) ───────────────────────────────────────────────────

export interface AuditPollSnapshot {
	/** Server rows (oldest → newest) when the per-event audit endpoint answered. */
	eventAudit: ServerAuditRow[] | null;
	adaptive: AdaptiveStatusSnapshot | null;
	source: 'event-audit' | 'adaptive-status';
}

function actorKindOf(v: unknown): ServerAuditRow['actorKind'] {
	return v === 'human' || v === 'machine' ? v : 'system';
}

/**
 * Pure parser for GET /api/v1/events/{id}/audit (contract C-C:
 * {event_id, items: FeedItem[]}, newest first). Returns oldest → newest;
 * rows without an id, a parseable `t` or text are dropped (they cannot be
 * de-duplicated or placed). Exported for the contract tests.
 */
export function parseAuditRows(resp: unknown): ServerAuditRow[] {
	const body = (resp ?? {}) as Record<string, unknown>;
	const rows = (Array.isArray(resp) ? resp : ((body.items ?? body.entries ?? body.audit ?? []) as unknown[])) as Record<
		string,
		unknown
	>[];
	const out: ServerAuditRow[] = [];
	for (const r of rows) {
		if (!r || typeof r !== 'object') continue;
		const id = typeof r.id === 'number' ? String(r.id) : str(r.id);
		const ts = typeof r.t === 'string' ? Date.parse(r.t) : NaN;
		const text = str(r.text);
		if (!id || !Number.isFinite(ts) || !text) continue;
		out.push({
			id,
			ts,
			kind: str(r.kind),
			actor: str(r.actor),
			actorKind: actorKindOf(r.actor_kind),
			text,
			source: str(r.source),
			severity: str(r.severity),
			urns: Array.isArray(r.urns) ? r.urns.filter((u): u is string => typeof u === 'string') : [],
			simulated: simulatedFeedOf(r.props),
		});
	}
	return out.sort((a, b) => a.ts - b.ts);
}

/**
 * C-Q provenance of a FeedItem's props: its numbers came from a SIMULATED
 * feed when props.simulated_feed is true or props.data_quality is (or
 * lists) 'synthetic'. Absent fields are 'not simulated' — never guessed.
 */
export function simulatedFeedOf(props: unknown): boolean {
	if (!props || typeof props !== 'object' || Array.isArray(props)) return false;
	const p = props as Record<string, unknown>;
	if (p.simulated_feed === true || p.simulated === true) return true;
	const dq = p.data_quality;
	return dq === 'synthetic' || (Array.isArray(dq) && dq.includes('synthetic'));
}

/**
 * Classify a free-text actor the way the oracle does (city/common.py
 * actor_kind_of): bookkeeping ('system…', '…(system)', 'event-emulator')
 * is system; the named machine agents are machine; any other named actor
 * is a person. Used for rows the deck receives without an actor_kind (the
 * dispatch audit tails).
 */
export function actorKindOfName(actor: string | null | undefined, fallback: ActorKind = 'human'): ActorKind {
	if (!actor) return fallback;
	const low = actor.trim().toLowerCase();
	if (low.startsWith('system') || low.includes('(system)') || low === 'event-emulator') return 'system';
	// '108 control (emulated)' — the living city's requester is a script, not an officer
	if (isEmulatedActor(low)) return 'system';
	if (/\bbrain\b/.test(low) || low === 'traffic-oracle' || low === 'rules-engine' || low === 'ama') return 'machine';
	return 'human';
}

/** A requester the event emulator wrote ('108 control (emulated)', 'event-emulator …'). */
export function isEmulatedActor(actor: string | null | undefined): boolean {
	return !!actor && /\(emulated\)|event-emulator/i.test(actor);
}

/**
 * GET /api/v1/events/{id}/audit?limit= — one event's narrative (contract
 * C-C), oldest → newest. 404 (unknown event / not deployed) raises
 * ApiNotDeployed and is remembered by request().
 */
export async function fetchEventAudit(eventId: string, limit = 100): Promise<ServerAuditRow[]> {
	const id = safePathId(eventId);
	if (!id) throw new ApiDown(`invalid event id: ${eventId}`);
	const resp = await request<Record<string, unknown>>(
		`${EVENT_AUDIT_PATH(encodeURIComponent(id))}?limit=${Math.min(Math.max(1, limit), 500)}`,
		undefined,
		4000,
	);
	return parseAuditRows(resp);
}

/** GET /api/v1/city/feed?limit= — the whole city's narrative (C-C), oldest → newest. */
export async function fetchCityFeed(limit = 100): Promise<ServerAuditRow[]> {
	const resp = await request<Record<string, unknown>>(
		`${CITY_FEED_API}?limit=${Math.min(Math.max(1, limit), 500)}`,
		undefined,
		6000,
	);
	return parseAuditRows(resp);
}

/**
 * The 2 s poll rung of the audit ladder: prefer the generalized per-event
 * audit feed (server rows with human/machine attribution), fall back to the
 * adaptive status (whose audit_trail + per-event situation snapshot is a
 * real audit surface today). `eventId` is null for preview fixtures and when
 * nothing is selected — those never touch the per-event route. A 404 there
 * is remembered for NOT_DEPLOYED_BACKOFF_MS by request(). (The ops store
 * reads fetchEventAudit / fetchCityFeed first; this is its legacy rung.)
 */
export async function pollAuditTail(eventId: string | null): Promise<AuditPollSnapshot> {
	const id = safePathId(eventId);
	if (id) {
		try {
			const resp = await request<Record<string, unknown>>(
				`${EVENTS_API}/${encodeURIComponent(id)}/audit?limit=50`,
				undefined,
				4000,
			);
			return { eventAudit: parseAuditRows(resp), adaptive: null, source: 'event-audit' };
		} catch {
			// not deployed (404, remembered) or down — the status rung answers
		}
	}
	const adaptive = await fetchAdaptiveStatus();
	return { eventAudit: null, adaptive, source: 'adaptive-status' };
}

// re-export for panels that render guard outcomes
export { assertSafeExternalUrl };

// ─── City-writer gate: named human actor (C6) ────────────────────────────────

/**
 * Raised when a city-writer POST is attempted without a named human actor.
 * The ethics gate is client-side AND server-side (officer fields are
 * min_length-validated in the oracle); this keeps the UI honest before the
 * wire. Message is operator-facing — surfaces inline next to the commit
 * button, never as a silent no-op.
 */
export class CommitRefused extends Error {
	constructor(operation: string) {
		super(
			`${operation} refused: a named duty officer must be on the console before this action can commit (no automated enforcement)`,
		);
		this.name = 'CommitRefused';
	}
}

/**
 * Bidirectional-override and isolate controls (U+202A–U+202E,
 * U+2066–U+2069) and the LRM / RLM marks (U+200E / U+200F): an officer name
 * carrying one can render reversed in the audit trail ("Mehta" shown,
 * "athem" stored). Refused at sign-in and here — the marks too, because the
 * brain refuses them on escort approve / clear (a name the deck accepted
 * must never 422 there). ZWJ / ZWNJ (U+200D / U+200C) are NOT in this set —
 * Indic names need them.
 */
export const BIDI_CONTROL_RE = /[\u200e\u200f\u202a-\u202e\u2066-\u2069]/;

/** Control characters (C0 + DEL): never part of a recorded name. */
// eslint-disable-next-line no-control-regex
const CONTROL_CHAR_RE = /[\u0000-\u001f\u007f]/;

/** Require a non-blank human actor for a writer action. Trims + caps; bidi / control characters refused. */
function requireActor(operation: string, actor: string): string {
	const who = actor.trim();
	if (who.length < 2 || BIDI_CONTROL_RE.test(who) || CONTROL_CHAR_RE.test(who)) throw new CommitRefused(operation);
	return who.slice(0, 120);
}

/**
 * The officer as the 64-char contract fields carry it (C-F event lifecycle,
 * dispatch decisions/status, signal apply/restore, earmark clear): inner
 * whitespace collapsed, capped at 64 — a longer console name would 422.
 */
function officerField(who: string): string {
	if (BIDI_CONTROL_RE.test(who) || CONTROL_CHAR_RE.test(who)) throw new CommitRefused('officer name with bidirectional or control characters');
	return who.split(/\s+/).filter(Boolean).join(' ').slice(0, 64);
}

// ─── Enforcement recommendation queue ────────────────────────────────────────

function mapRecommendation(r: Record<string, unknown>): EnforcementRecommendation | null {
	const id = r.recommendation_id ?? r.recommendationId;
	if (!id) return null;
	const status = r.status === 'approved' || r.status === 'rejected' ? r.status : 'pending';
	return {
		recommendationId: String(id),
		sightingId: typeof r.sighting_id === 'string' ? r.sighting_id : null,
		vehicleId: typeof r.vehicle_id === 'string' ? r.vehicle_id : null,
		plate: typeof r.plate === 'string' ? r.plate : null,
		ruleId: typeof r.rule_id === 'string' ? r.rule_id : null,
		recommendation: typeof r.recommendation === 'string' ? r.recommendation : null,
		evidence:
			r.evidence && typeof r.evidence === 'object' && !Array.isArray(r.evidence)
				? (r.evidence as Record<string, unknown>)
				: null,
		status,
		decidedBy: typeof r.decided_by === 'string' ? r.decided_by : null,
		decidedAt: typeof r.decided_at === 'string' ? r.decided_at : null,
		auditTail: Array.isArray(r.audit_tail) ? (r.audit_tail as Array<Record<string, unknown>>) : [],
		createdAt: typeof r.created_at === 'string' ? r.created_at : null,
		pursuit: parsePursuitRef(r.pursuit),
	};
}

function parsePursuitRef(v: unknown): EnforcementRecommendation['pursuit'] {
	if (!v || typeof v !== 'object' || Array.isArray(v)) return null;
	const p = v as Record<string, unknown>;
	return typeof p.event_id === 'string' && p.event_id
		? { eventId: p.event_id, status: typeof p.status === 'string' ? p.status : null }
		: null;
}

/**
 * C6: one officer approval authorises ONE pursuit. An approval whose pursuit
 * is closed (completed / cancelled) is spent — e.g. a scripted showcase's
 * sighting approval — so it never pre-empts the next human AUTHORISE
 * PURSUIT (the oracle refuses to reuse it too). An approval behind a
 * still-open pursuit keeps counting.
 */
export function approvalSpent(r: Pick<EnforcementRecommendation, 'pursuit'>): boolean {
	const status = r.pursuit?.status;
	return status === 'completed' || status === 'cancelled';
}

/** GET /api/v1/enforcement/recommendations?status= — the pending queue. */
export async function fetchEnforcementRecommendations(
	status: 'pending' | 'approved' | 'rejected' = 'pending',
): Promise<EnforcementRecommendation[]> {
	const resp = await request<{ recommendations?: Record<string, unknown>[] }>(
		`${ENFORCEMENT_API}/recommendations?status=${status}&limit=50`,
		undefined,
		8000,
	);
	return (resp.recommendations ?? [])
		.map(mapRecommendation)
		.filter((r): r is EnforcementRecommendation => r !== null);
}

/**
 * The oracle refused a decision with a 409 (contract C-N: approving a penal
 * rule while the vehicle is reported stolen → 'e-challan withheld — vehicle
 * reported stolen', reason 'stolen_vehicle_fir'). The detail is the
 * oracle's own sentence, shown verbatim; the card never retries it.
 */
export class DecisionRefused extends Error {
	constructor(
		public status: number,
		public detail: string,
		public reason: string | null,
	) {
		super(detail);
		this.name = 'DecisionRefused';
	}
}

/** Pure: the 409 body of a decision → its verbatim detail + reason (exported for tests). */
export function parseDecisionRefusal(bodyText: string): { detail: string; reason: string | null } {
	try {
		const body = JSON.parse(bodyText) as Record<string, unknown>;
		const d = body.detail;
		if (typeof d === 'string') return { detail: d, reason: str(body.reason) };
		if (d && typeof d === 'object' && !Array.isArray(d)) {
			const o = d as Record<string, unknown>;
			return { detail: str(o.detail) ?? str(o.message) ?? JSON.stringify(o).slice(0, 240), reason: str(o.reason) ?? str(body.reason) };
		}
		return { detail: bodyText.slice(0, 240) || 'refused (409)', reason: str(body.reason) };
	} catch {
		return { detail: bodyText.slice(0, 240) || 'refused (409)', reason: null };
	}
}

/**
 * POST /api/v1/enforcement/recommendations/{id}/decision — CITY WRITER.
 * The C6 gate: approve|reject only ever under a named officer identity.
 * A 409 throws DecisionRefused with the oracle's sentence (never retried).
 * Smokes NEVER fire this (stubbed per contract R8).
 */
export async function decideEnforcementRecommendation(
	recommendationId: string,
	input: { decision: 'approve' | 'reject'; officer: string; note?: string },
): Promise<EnforcementRecommendation['status']> {
	const id = safePathId(recommendationId);
	if (!id) throw new ApiDown(`invalid recommendation id: ${recommendationId}`);
	const officer = requireActor('e-challan decision', input.officer);
	const ctrl = new AbortController();
	const timer = setTimeout(() => ctrl.abort(), 30000);
	let res: Response;
	try {
		res = await fetch(`${ENFORCEMENT_API}/recommendations/${id}/decision`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ decision: input.decision, officer, note: (input.note ?? '').slice(0, 500) }),
			signal: ctrl.signal,
		});
	} catch (err) {
		throw new ApiDown(
			err instanceof DOMException && err.name === 'AbortError'
				? 'decision: no answer within 30 s'
				: `decision unreachable: ${err instanceof Error ? err.message : String(err)}`,
		);
	} finally {
		clearTimeout(timer);
	}
	const text = await res.text().catch(() => '');
	if (res.status === 409) {
		const refusal = parseDecisionRefusal(text);
		throw new DecisionRefused(409, refusal.detail, refusal.reason);
	}
	if (res.status === 401 || res.status === 403) throw new SignInRequired(res.status);
	if (res.status === 404) throw new ApiNotDeployed(404, 'recommendation not found');
	if (!res.ok) throw new ApiDown(`${res.status} ${res.statusText}: ${text.slice(0, 200)}`);
	let resp: Record<string, unknown> = {};
	try {
		resp = JSON.parse(text) as Record<string, unknown>;
	} catch {
		// an empty 200 is still a decision the oracle took
	}
	return resp.status === 'approved' ? 'approved' : resp.status === 'rejected' ? 'rejected' : input.decision === 'approve' ? 'approved' : 'rejected';
}

// ─── Escort writers (contract C-J, brain via /api/convoy) ────────────────────

/** The brain's answer to an escort approval: its new status, or why it refused. */
export interface EscortApprovalOutcome {
	ok: boolean;
	httpStatus: number;
	/** approved | rejected | expired … (the brain's status), null on failure. */
	status: string | null;
	action: string | null;
	detail: string | null;
}

/**
 * POST /api/convoy/movements/{id}/escort/approve {approval_id, decision,
 * officer} — CITY WRITER. An officer answers an escort action the brain
 * referred to a human (a hold, a re-route, a detour). The AI never decides
 * these: this is the officer's call, under their name. A 409 (already
 * decided) and 404 (escort ended / unknown approval) come back as outcomes.
 */
export async function approveEscort(
	movementId: string,
	input: { approval_id: string; decision: 'approve' | 'reject'; officer: string },
): Promise<EscortApprovalOutcome> {
	const id = safePathId(movementId);
	if (!id) throw new ApiDown(`invalid movement id: ${movementId}`);
	const approvalId = safePathId(input.approval_id);
	if (!approvalId) throw new ApiDown(`invalid approval id: ${input.approval_id}`);
	const officer = officerField(requireActor(`escort ${input.decision}`, input.officer));
	const ctrl = new AbortController();
	const timer = setTimeout(() => ctrl.abort(), 15000);
	try {
		const res = await fetch(`${BRAIN_API}/movements/${id}/escort/approve`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ approval_id: approvalId, decision: input.decision, officer }),
			signal: ctrl.signal,
		});
		const text = await res.text().catch(() => '');
		let body: Record<string, unknown> = {};
		try {
			body = JSON.parse(text) as Record<string, unknown>;
		} catch {
			// non-JSON (a proxy page): the text is the detail
		}
		if (res.ok) return { ok: true, httpStatus: res.status, status: str(body.status), action: str(body.action), detail: null };
		if (res.status === 401 || res.status === 403) {
			return { ok: false, httpStatus: res.status, status: null, action: null, detail: SIGN_IN_REQUIRED };
		}
		return {
			ok: false,
			httpStatus: res.status,
			status: str(body.status),
			action: null,
			detail: str(body.detail) ?? (text.slice(0, 200) || `HTTP ${res.status}`),
		};
	} catch (err) {
		return { ok: false, httpStatus: 0, status: null, action: null, detail: err instanceof Error ? err.message : String(err) };
	} finally {
		clearTimeout(timer);
	}
}

// ─── Escort authority: the open proposal a named officer decides ──────────────

function finiteNum(v: unknown): number | null {
	return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

function epochOf(v: unknown): number | null {
	if (typeof v !== 'string' || !v) return null;
	const t = Date.parse(v);
	return Number.isFinite(t) ? t : null;
}

function strList(v: unknown): string[] {
	return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string' && x.length > 0) : [];
}

/** Pure: the brain's ``proposed`` document (exported for the contract tests). */
export function parseProposedChange(v: unknown): EscortProposedChange | null {
	if (!v || typeof v !== 'object' || Array.isArray(v)) return null;
	const p = v as Record<string, unknown>;
	return {
		routeSource: str(p.route_source),
		segmentIds: segmentIds(p.segment_ids),
		tailSegmentIds: segmentIds(p.tail_segment_ids),
		roadNames: strList(p.road_names),
		viaSegmentIds: segmentIds(p.via_segment_ids),
		viaRoadNames: strList(p.via_road_names),
		remainingM: finiteNum(p.remaining_m),
		newRemainingM: finiteNum(p.new_remaining_m),
		avoids: segmentIds(p.avoids),
		longDetour: p.long_detour === true,
		closureType: str(p.closure_type),
		closuresNew: segmentIds(p.closures_new),
		closuresLifted: segmentIds(p.closures_lifted),
		holdSegmentIds: segmentIds(p.hold_segment_ids),
		maxHoldSec: finiteNum(p.max_hold_sec),
		replan: p.replan === true,
	};
}

/**
 * Pure: one C-J ``pending_approval`` → an EscortProposal (null for anything
 * that is not an OPEN proposal). Exported for the contract tests.
 */
export function parseEscortProposal(raw: unknown, movementId: string, eventId: string | null): EscortProposal | null {
	if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
	const a = raw as Record<string, unknown>;
	const approvalId = str(a.approval_id);
	if (!approvalId || (a.status != null && a.status !== 'pending')) return null;
	const action = str(a.action) ?? 'action';
	const dq = Array.isArray(a.data_quality) ? a.data_quality : a.data_quality != null ? [a.data_quality] : [];
	return {
		movementId,
		eventId,
		approvalId,
		action,
		kind: str(a.kind) ?? action,
		summary: str(a.summary),
		reason: str(a.reason),
		trigger: str(a.trigger),
		tier: str(a.tier),
		confidence: finiteNum(a.confidence),
		createdAt: epochOf(a.created_at) ?? epochOf(a.raised_at),
		expiresAt: epochOf(a.expires_at),
		timeoutSec: finiteNum(a.timeout_sec),
		segmentIds: segmentIds(a.segment_ids),
		peakCongestion: finiteNum(a.peak_congestion),
		proposed: parseProposedChange(a.proposed),
		simulated: a.simulated === true || dq.includes('synthetic'),
	};
}

/** Pure: one C-J ``approval_outcomes[]`` row (exported for the contract tests). */
export function parseEscortOutcome(raw: unknown, movementId: string): EscortProposalOutcome | null {
	if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
	const o = raw as Record<string, unknown>;
	const approvalId = str(o.approval_id);
	const status = str(o.status);
	if (!approvalId || !status) return null;
	const action = str(o.action) ?? 'action';
	return {
		movementId,
		approvalId,
		action,
		kind: str(o.kind) ?? action,
		summary: str(o.summary),
		status,
		decidedBy: str(o.decided_by),
		decidedAt: epochOf(o.decided_at),
		claimedOfficer: str(o.claimed_officer),
		expiryReason: str(o.expiry_reason),
		applied: typeof o.applied === 'boolean' ? o.applied : null,
		appliedDetail: str(o.applied_detail),
	};
}

/** Pure: GET /api/convoy/escorts → lanes (exported for the contract tests). */
export function parseEscorts(resp: unknown): { lanes: EscortLane[]; authority: string | null } {
	const body = resp && typeof resp === 'object' ? (resp as Record<string, unknown>) : {};
	const lanes: EscortLane[] = [];
	for (const raw of Array.isArray(body.escorts) ? body.escorts : []) {
		if (!raw || typeof raw !== 'object') continue;
		const e = raw as Record<string, unknown>;
		const movementId = str(e.movement_id);
		if (!movementId) continue;
		const eventId = str(e.event_id);
		const alive = e.alive === true;
		lanes.push({
			movementId,
			eventId,
			status: str(e.status) ?? 'unknown',
			alive,
			officer: str(e.officer),
			gpsMode: str(e.gps_mode),
			trafficSimulated: typeof e.traffic_simulated === 'boolean' ? e.traffic_simulated : null,
			// a proposal is answerable only while its escort runs
			proposal: alive ? parseEscortProposal(e.pending_approval, movementId, eventId) : null,
			outcomes: (Array.isArray(e.approval_outcomes) ? e.approval_outcomes : [])
				.map((o) => parseEscortOutcome(o, movementId))
				.filter((o): o is EscortProposalOutcome => o !== null),
			timeoutSec: finiteNum(e.approval_timeout_sec),
		});
	}
	return { lanes, authority: str(body.authority) };
}

/**
 * GET /api/convoy/escorts — every running (and just-ended) escort with its
 * open proposal and recent outcomes: ONE read feeds the HUD, the /ops
 * decision queue and the city vitals. 404 → ApiNotDeployed (a brain that
 * predates escort authority; the HUD then reads the escort follower).
 */
export async function fetchEscorts(): Promise<{ lanes: EscortLane[]; authority: string | null }> {
	return parseEscorts(await request<unknown>(`${BRAIN_API}/escorts`, undefined, 6000));
}

/**
 * POST /api/convoy/movements/{id}/escort/approvals/{approval_id}
 * {decision, officer} — CITY WRITER. A named officer approves (the escort
 * applies exactly what was proposed) or rejects the open proposal. The
 * deck's verified session officer travels as x-veer-officer (the proxy sets
 * it); the body name is the console's. 409 (expired — the convoy held its
 * current plan — superseded, already decided) and 404 come back as
 * outcomes with the brain's sentence. A brain without the route (FastAPI's
 * bare 404 'Not Found') falls back to the older /escort/approve.
 */
export async function decideEscortProposal(
	movementId: string,
	input: { approval_id: string; decision: 'approve' | 'reject'; officer: string },
): Promise<EscortApprovalOutcome> {
	const id = safePathId(movementId);
	if (!id) throw new ApiDown(`invalid movement id: ${movementId}`);
	const approvalId = safePathId(input.approval_id);
	if (!approvalId) throw new ApiDown(`invalid approval id: ${input.approval_id}`);
	const officer = officerField(requireActor(`escort proposal ${input.decision}`, input.officer));
	const ctrl = new AbortController();
	const timer = setTimeout(() => ctrl.abort(), 15000);
	try {
		const res = await fetch(`${BRAIN_API}/movements/${id}/escort/approvals/${approvalId}`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ decision: input.decision, officer }),
			signal: ctrl.signal,
		});
		const text = await res.text().catch(() => '');
		let body: Record<string, unknown> = {};
		try {
			body = JSON.parse(text) as Record<string, unknown>;
		} catch {
			// non-JSON (a proxy page): the text is the detail
		}
		if (res.ok) {
			return { ok: true, httpStatus: res.status, status: str(body.status), action: str(body.action), detail: null };
		}
		if (res.status === 401 || res.status === 403) {
			return { ok: false, httpStatus: res.status, status: null, action: null, detail: SIGN_IN_REQUIRED };
		}
		if ((res.status === 404 || res.status === 405) && (body.detail === 'Not Found' || body.detail === 'Method Not Allowed')) {
			return approveEscort(movementId, input); // a brain that predates the route
		}
		return {
			ok: false,
			httpStatus: res.status,
			status: str(body.status),
			action: null,
			detail: str(body.detail) ?? (text.slice(0, 200) || `HTTP ${res.status}`),
		};
	} catch (err) {
		return { ok: false, httpStatus: 0, status: null, action: null, detail: err instanceof Error ? err.message : String(err) };
	} finally {
		clearTimeout(timer);
	}
}

/** The brain's answer to CLEAR: the corridor event's final transition (C-F). */
export interface ClearOutcome {
	ok: boolean;
	httpStatus: number;
	/** completed — the convoy passed; cancelled — the movement never went live. */
	toStatus: string | null;
	detail: string | null;
}

/**
 * POST /api/convoy/movements/{id}/clear {officer} — CITY WRITER (post-
 * clearance restoration). Retires the escort, restores traffic and closes
 * the corridor event under the named officer: 'completed' after a live run,
 * 'cancelled' for a movement that never went live (event_transition).
 */
export async function clearMovement(movementId: string, officer: string): Promise<ClearOutcome> {
	const id = safePathId(movementId);
	if (!id) throw new ApiDown(`invalid movement id: ${movementId}`);
	const who = officerField(requireActor('movement clear', officer));
	const ctrl = new AbortController();
	// post-clearance watches queue dissipation (≤ 120 s by default) before it answers
	const timer = setTimeout(() => ctrl.abort(), 180_000);
	try {
		const res = await fetch(`${BRAIN_API}/movements/${id}/clear`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ officer: who }),
			signal: ctrl.signal,
		});
		const text = await res.text().catch(() => '');
		let body: Record<string, unknown> = {};
		try {
			body = JSON.parse(text) as Record<string, unknown>;
		} catch {
			// non-JSON: the text is the detail
		}
		if (!res.ok) {
			return {
				ok: false,
				httpStatus: res.status,
				toStatus: null,
				detail: res.status === 401 || res.status === 403 ? SIGN_IN_REQUIRED : (str(body.detail) ?? (text.slice(0, 200) || `HTTP ${res.status}`)),
			};
		}
		// event_transition.outcome: transitioned | already | conflict |
		// not_found | rejected | failed | skipped (the brain's C-F client)
		const tr = body.event_transition && typeof body.event_transition === 'object' ? (body.event_transition as Record<string, unknown>) : null;
		const outcome = tr ? str(tr.outcome) : null;
		const landed = outcome === 'transitioned' || outcome === 'already';
		return {
			ok: true,
			httpStatus: res.status,
			toStatus: tr && landed ? str(tr.to_status) : null,
			detail: tr && !landed ? `${outcome ?? 'no transition'}${str(tr.reason) ? ` — ${str(tr.reason)}` : ''}` : null,
		};
	} catch (err) {
		return { ok: false, httpStatus: 0, toStatus: null, detail: err instanceof Error ? err.message : String(err) };
	} finally {
		clearTimeout(timer);
	}
}

// ─── Event lifecycle (contract C-F) ──────────────────────────────────────────

/**
 * Pure: the outcome of one POST /events/{id}/status from its HTTP status and
 * body (200 {event_id, from_status, to_status, at, actor} · 409 {detail,
 * from_status, allowed} · 404/422/503 {detail}). Exported for tests.
 */
export function parseTransitionOutcome(
	eventId: string,
	toStatus: EventTransitionTarget,
	httpStatus: number,
	bodyText: string,
	when = Date.now(),
): EventTransitionOutcome {
	let body: Record<string, unknown> = {};
	try {
		body = JSON.parse(bodyText) as Record<string, unknown>;
	} catch {
		// non-JSON (proxy error page): the text is the detail
	}
	const detail = body.detail;
	const detailText =
		typeof detail === 'string'
			? detail
			: Array.isArray(detail)
				? detail
						.map((d) => (d && typeof d === 'object' ? String((d as Record<string, unknown>).msg ?? '') : String(d)))
						.filter(Boolean)
						.join('; ')
				: bodyText.slice(0, 200) || null;
	const ok = httpStatus === 200;
	return {
		eventId,
		toStatus,
		ok,
		status: httpStatus,
		fromStatus: str(body.from_status),
		allowed: Array.isArray(body.allowed) ? body.allowed.filter((a): a is string => typeof a === 'string') : null,
		actor: ok ? str(body.actor) : null,
		at: ok ? str(body.at) : null,
		detail: ok ? null : detailText,
		closedDispatches: Array.isArray(body.closed_dispatches) ? body.closed_dispatches.length : 0,
		when,
	};
}

/**
 * POST /api/v1/events/{id}/status {to_status, officer, note?} — CITY WRITER
 * (contract C-F). Only a named duty officer moves an event along its
 * lifecycle (CommitRefused before any request otherwise). An illegal move
 * comes back 409 with the moves the server allows — returned as an outcome
 * the card renders inline, never thrown; so are 404/422/503 and transport
 * failures (status 0).
 */
export async function transitionEventStatus(
	eventId: string,
	input: { toStatus: EventTransitionTarget; officer: string; note?: string },
): Promise<EventTransitionOutcome> {
	const id = safePathId(eventId);
	if (!id) throw new ApiDown(`invalid event id: ${eventId}`);
	const officer = officerField(requireActor(`event ${input.toStatus}`, input.officer));
	const note = (input.note ?? '').trim().slice(0, 500);
	const ctrl = new AbortController();
	const timer = setTimeout(() => ctrl.abort(), 15000);
	try {
		const res = await fetch(EVENT_STATUS_PATH(id), {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ to_status: input.toStatus, officer, ...(note ? { note } : {}) }),
			signal: ctrl.signal,
		});
		return parseTransitionOutcome(id, input.toStatus, res.status, await res.text().catch(() => ''));
	} catch (err) {
		return parseTransitionOutcome(id, input.toStatus, 0, `${err instanceof Error ? err.message : String(err)}`);
	} finally {
		clearTimeout(timer);
	}
}

// ─── Agency dispatch lifecycle (Jan Rakshak tiles) ───────────────────────────

function mapDispatch(r: Record<string, unknown>): DispatchRecord | null {
	const id = r.dispatch_id ?? r.dispatchId;
	if (!id) return null;
	const audit = Array.isArray(r.audit) ? (r.audit as Array<Record<string, unknown>>) : [];
	const mappedAudit: DispatchAuditEntry[] = audit.map((a) => ({
		fromStatus: typeof a.from_status === 'string' ? a.from_status : null,
		toStatus: typeof a.to_status === 'string' ? a.to_status : '—',
		actor: typeof a.actor === 'string' ? a.actor : '—',
		note: typeof a.note === 'string' ? a.note : null,
		at: typeof a.at === 'string' ? a.at : '',
	}));
	return {
		dispatchId: String(id),
		eventId: typeof r.event_id === 'string' ? r.event_id : null,
		agency: typeof r.agency === 'string' ? r.agency : 'unknown',
		agencyLabel: typeof r.agency_label === 'string' ? r.agency_label : null,
		unitId: typeof r.unit_id === 'string' ? r.unit_id : null,
		waypointJunction: typeof r.waypoint_junction === 'number' ? r.waypoint_junction : null,
		status: typeof r.status === 'string' ? r.status : 'requested',
		requestedBy: typeof r.requested_by === 'string' ? r.requested_by : null,
		decidedBy: typeof r.decided_by === 'string' ? r.decided_by : null,
		decisionNote: typeof r.decision_note === 'string' ? r.decision_note : null,
		approvedAt: typeof r.approved_at === 'string' ? r.approved_at : null,
		createdAt: typeof r.created_at === 'string' ? r.created_at : null,
		updatedAt: typeof r.updated_at === 'string' ? r.updated_at : null,
		audit: mappedAudit,
	};
}

/** GET /api/v1/dispatches — the dispatch list for the tiles. */
export async function fetchDispatches(limit = 40): Promise<DispatchRecord[]> {
	const resp = await request<{ dispatches?: Record<string, unknown>[] }>(
		`${DISPATCHES_API}?limit=${limit}`,
		undefined,
		8000,
	);
	return (resp.dispatches ?? [])
		.map(mapDispatch)
		.filter((d): d is DispatchRecord => d !== null);
}

/**
 * POST /api/v1/events/{event_id}/dispatch — CITY WRITER (request a unit).
 * Creates a dispatch in 'requested'; a duty officer still approves it.
 */
export async function requestDispatch(
	eventId: string,
	input: {
		agency: string;
		unitId: string;
		waypointJunction?: number;
		requestedBy: string;
		note?: string;
	},
): Promise<DispatchRecord | null> {
	const id = safePathId(eventId);
	if (!id) throw new ApiDown(`invalid event id: ${eventId}`);
	const requestedBy = officerField(requireActor('unit dispatch request', input.requestedBy));
	const resp = await post<Record<string, unknown>>(
		`${EVENTS_API}/${id}/dispatch`,
		{
			agency: input.agency,
			unit_id: input.unitId,
			...(input.waypointJunction != null ? { waypoint_junction: input.waypointJunction } : {}),
			requested_by: requestedBy,
			...(input.note ? { note: input.note.slice(0, 500) } : {}),
		},
	);
	return mapDispatch(resp);
}

/** POST /api/v1/dispatches/{id}/decision — CITY WRITER (approve | defer). */
export async function decideDispatch(
	dispatchId: string,
	input: { action: 'approve' | 'defer'; officer: string; note?: string },
): Promise<DispatchRecord | null> {
	const id = safePathId(dispatchId);
	if (!id) throw new ApiDown(`invalid dispatch id: ${dispatchId}`);
	const officer = officerField(requireActor('dispatch decision', input.officer));
	const resp = await post<Record<string, unknown>>(`${DISPATCHES_API}/${id}/decision`, {
		action: input.action,
		officer,
		...(input.note ? { note: input.note.slice(0, 500) } : {}),
	});
	return mapDispatch(resp);
}

/** POST /api/v1/dispatches/{id}/status — CITY WRITER (en_route|on_scene|…). */
export async function advanceDispatch(
	dispatchId: string,
	input: { status: 'en_route' | 'on_scene' | 'completed' | 'cancelled'; officer: string; note?: string },
): Promise<DispatchRecord | null> {
	const id = safePathId(dispatchId);
	if (!id) throw new ApiDown(`invalid dispatch id: ${dispatchId}`);
	const officer = officerField(requireActor('dispatch status', input.officer));
	const resp = await post<Record<string, unknown>>(`${DISPATCHES_API}/${id}/status`, {
		status: input.status,
		officer,
		...(input.note ? { note: input.note.slice(0, 500) } : {}),
	});
	return mapDispatch(resp);
}

// ─── City signal plans (gate card) ───────────────────────────────────────────

function mapBandPlanTable(raw: unknown): SignalBandPlanRow[] {
	if (!Array.isArray(raw)) return [];
	return raw
		.map((r): SignalBandPlanRow | null => {
			const row = r as Record<string, unknown>;
			const band = pick(row.band, ['GREEN', 'AMBER', 'RED', 'BLACK']);
			if (!band) return null;
			// Wire `writes` is a boolean (signals/plans.py band_plan_table_doc):
			// does this band's plan rewrite signal programs, or hold baseline?
			const writesSignals = typeof row.writes === 'boolean' ? row.writes : null;
			return {
				band,
				priorityShare: typeof row.priority_share === 'number' ? row.priority_share : null,
				crossGreenFloorSec:
					typeof row.cross_green_floor_sec === 'number' ? row.cross_green_floor_sec : null,
				writes: writesSignals === null ? null : writesSignals ? 'writes' : 'hold',
				writesSignals,
			};
		})
		.filter((r): r is SignalBandPlanRow => r !== null);
}

function mapLastPlan(raw: unknown): SignalLastPlan | null {
	if (!raw || typeof raw !== 'object') return null;
	const row = raw as Record<string, unknown>;
	const refusal = (row.refusal ?? row.reason ?? row.detail ?? null) as Record<string, unknown> | string | null;
	return {
		applied: typeof row.applied === 'boolean' ? row.applied : null,
		band: pick(row.band, ['GREEN', 'AMBER', 'RED', 'BLACK']),
		plannedJunctions: Array.isArray(row.planned_junctions)
			? (row.planned_junctions as unknown[]).filter((n): n is number => typeof n === 'number')
			: [],
		reason:
			typeof refusal === 'string'
				? refusal
				: refusal && typeof refusal === 'object' && typeof refusal['reason'] === 'string'
					? (refusal['reason'] as string)
					: null,
		raw: row,
	};
}

/** GET /api/v1/signals/plans — writer posture for the gate card. */
export async function fetchSignalPlans(): Promise<SignalPlansDoc> {
	return parseSignalPlans(await request<Record<string, unknown>>(SIGNALS_PLANS_API, undefined, 8000));
}

/**
 * Pure parser behind fetchSignalPlans (exported for the contract tests).
 * gate.twin_in_flight is the signal-policy gate's shared-holder COUNT
 * (signals/gate.py snapshot), never a boolean.
 */
export function parseSignalPlans(resp: Record<string, unknown>): SignalPlansDoc {
	const gate = (resp.gate ?? {}) as Record<string, unknown>;
	return {
		writerEnabled: resp.writer_enabled === true,
		writerFlagEnv: typeof resp.writer_flag_env === 'string' ? resp.writer_flag_env : null,
		cityBand: pick(resp.city_band, ['GREEN', 'AMBER', 'RED', 'BLACK']),
		bandSubjects: typeof resp.band_subjects === 'number' ? resp.band_subjects : null,
		bandPlanTable: mapBandPlanTable(resp.band_plan_table),
		lastPlan: mapLastPlan(resp.last_plan),
		gate: {
			writerActive: typeof gate.writer_active === 'boolean' ? gate.writer_active : null,
			writerWaiters: typeof gate.writer_waiters === 'number' ? gate.writer_waiters : null,
			twinInFlight:
				typeof gate.twin_in_flight === 'number'
					? gate.twin_in_flight
					: typeof gate.twin_in_flight === 'boolean'
						? Number(gate.twin_in_flight)
						: null,
		},
		scope: typeof resp.scope === 'string' ? resp.scope : null,
	};
}

/**
 * Extract the refusal reason from a 409 body the way the oracle shapes it:
 * FastAPI wraps the report as {"detail": {...}} — detail may itself be the
 * report object (with reason/code keys) or a plain string. Exported pure so
 * tests/ops-write-smoke.mjs exercises it against the canned
 * twin_not_calibrated fixture without any network.
 */
export function parseSignalRefusal(bodyText: string): { reason: string | null; detail: Record<string, unknown> | null } {
	try {
		const parsed = JSON.parse(bodyText) as Record<string, unknown>;
		const detail = parsed.detail;
		if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
			const obj = detail as Record<string, unknown>;
			const reason =
				typeof obj.reason === 'string'
					? obj.reason
					: typeof obj.code === 'string'
						? obj.code
						: typeof obj.error === 'string'
							? obj.error
							: null;
			return { reason, detail: obj };
		}
		if (typeof detail === 'string') return { reason: detail, detail: { detail } };
		return { reason: null, detail: parsed };
	} catch {
		return { reason: bodyText.slice(0, 300) || null, detail: null };
	}
}

/**
 * POST /api/v1/signals/plans/apply — CITY WRITER (the gated writer).
 * A 409 fail-closed refusal is a NORMAL outcome, returned as
 * {applied:false} — never thrown, never a modal: the card renders it as the
 * held-at-baseline state with its reason. The named officer travels in the
 * body (`officer`) so signal_program_history records who applied (C6).
 * Smokes NEVER fire this (R8).
 */
export async function applySignalPlan(
	input: { band?: BandCode; eventId?: string; junctionIds?: number[]; routeSegmentIds?: number[] },
	actor: string,
): Promise<SignalApplyOutcome> {
	const officer = officerField(requireActor('signal plan apply', actor));
	const eventId = input.eventId ? safePathId(input.eventId) : null;
	if (input.eventId && !eventId) throw new ApiDown(`invalid event id: ${input.eventId}`);
	const body: Record<string, unknown> = { officer };
	if (input.band) body['band'] = input.band;
	if (eventId) body['event_id'] = eventId;
	if (input.junctionIds?.length) body['junction_ids'] = input.junctionIds;
	if (input.routeSegmentIds?.length) body['route_segment_ids'] = input.routeSegmentIds;

	const ctrl = new AbortController();
	const timer = setTimeout(() => ctrl.abort(), 30000);
	try {
		const res = await fetch(SIGNALS_PLANS_API + '/apply', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify(body),
			signal: ctrl.signal,
		});
		const text = await res.text().catch(() => '');
		if (res.status === 409) {
			const refusal = parseSignalRefusal(text);
			return { applied: false, status: 409, refusalReason: refusal.reason, detail: refusal.detail, at: Date.now() };
		}
		if (!res.ok) {
			const refusal = parseSignalRefusal(text);
			return {
				applied: false,
				status: res.status,
				refusalReason: refusal.reason ?? `${res.status} ${res.statusText}`,
				detail: refusal.detail,
				at: Date.now(),
			};
		}
		const report = JSON.parse(text) as Record<string, unknown>;
		return {
			applied: report.applied !== false,
			status: res.status,
			refusalReason: null,
			detail: report,
			at: Date.now(),
		};
	} catch (err) {
		if (err instanceof CommitRefused) throw err;
		return {
			applied: false,
			status: 0,
			refusalReason: err instanceof Error ? err.message : String(err),
			detail: null,
			at: Date.now(),
		};
	} finally {
		clearTimeout(timer);
	}
}

/**
 * POST /api/v1/signals/restore — CITY WRITER (rebuild assumed programs).
 * `eventId` must be a LIVE event's uuid (the writer binds it as $1::uuid);
 * the store never passes a preview-fixture slug. The officer rides in the
 * body for signal_program_history attribution (C6).
 */
export async function restoreSignalPrograms(
	input: { eventId?: string; all?: boolean },
	actor: string,
): Promise<Record<string, unknown>> {
	const officer = officerField(requireActor('signal program restore', actor));
	const eventId = input.eventId ? safePathId(input.eventId) : null;
	if (input.eventId && !eventId) throw new ApiDown(`invalid event id: ${input.eventId}`);
	return post<Record<string, unknown>>(`${ORACLE_V1}/signals/restore`, {
		...(eventId ? { event_id: eventId } : {}),
		...(input.all ? { all: true } : {}),
		officer,
	});
}

/** Pure parser for GET /api/v1/signals/history rows (exported for tests). */
export function parseSignalHistory(resp: unknown): SignalHistoryRow[] {
	const body = (resp ?? {}) as Record<string, unknown>;
	const rows = (Array.isArray(resp) ? resp : ((body.history ?? []) as unknown[])) as Array<Record<string, unknown>>;
	const out: SignalHistoryRow[] = [];
	for (const r of rows) {
		if (!r || typeof r !== 'object') continue;
		const id = typeof r.history_id === 'number' ? r.history_id : Number(r.history_id);
		if (!Number.isFinite(id)) continue;
		out.push({
			historyId: id,
			junctionId: typeof r.junction_id === 'number' ? r.junction_id : null,
			eventId: str(r.event_id),
			kind: str(r.kind) ?? '—',
			cycleSec: typeof r.cycle_sec === 'number' ? r.cycle_sec : null,
			source: str(r.source),
			reason: str(r.reason),
			actor: str(r.actor),
			capturedAt: str(r.captured_at),
		});
	}
	return out;
}

/** GET /api/v1/signals/history — prior applies/restores for the card. */
export async function fetchSignalHistory(limit = 20): Promise<SignalHistoryRow[]> {
	const resp = await request<Record<string, unknown>>(`${SIGNALS_HISTORY_API}?limit=${limit}`, undefined, 8000);
	return parseSignalHistory(resp);
}

// ─── Watchlist (earmarked vehicles) ──────────────────────────────────────────

function mapWatchVehicle(r: Record<string, unknown>): VehicleWatchRecord | null {
	const id = r.vehicle_id ?? r.id;
	if (!id) return null;
	const sight = (r.last_sighting ?? r.lastSighting) as Record<string, unknown> | null | undefined;
	let lastSighting: VehicleWatchRecord['lastSighting'] = null;
	if (sight && typeof sight === 'object') {
		const pos = sight.position as unknown;
		if (Array.isArray(pos) && typeof pos[0] === 'number' && typeof pos[1] === 'number') {
			lastSighting = {
				t: typeof sight.t === 'string' ? sight.t : null,
				lon: pos[0],
				lat: pos[1],
				speedKmh: typeof sight.speed_kmh === 'number' ? sight.speed_kmh : null,
				heading: typeof sight.heading === 'number' ? sight.heading : null,
				source: typeof sight.source === 'string' ? sight.source : null,
			};
		}
	}
	const state = r.earmark_state ?? r.earmarkState;
	return {
		vehicleId: String(id),
		plate: typeof r.plate === 'string' ? r.plate : null,
		earmarkState: state === 'watch' || state === 'flagged' ? (state as 'watch' | 'flagged') : null,
		earmarkReason: typeof r.earmark_reason === 'string' ? r.earmark_reason : null,
		lawfulBasis: typeof r.lawful_basis === 'string' ? r.lawful_basis : null,
		retentionUntil: typeof r.retention_until === 'string' ? r.retention_until : null,
		lastSighting,
	};
}

/** GET /api/v1/vehicles?earmark=watch|flagged — the watchlist (read-only). */
export async function fetchWatchlist(
	earmark?: 'watch' | 'flagged',
): Promise<VehicleWatchRecord[]> {
	const qs = earmark ? `?earmark=${earmark}` : '';
	const resp = await request<Record<string, unknown>[]>(`${ORACLE_V1}/vehicles${qs}`, undefined, 8000);
	const rows = (Array.isArray(resp) ? resp : ((resp as { vehicles?: Record<string, unknown>[] }).vehicles ?? [])) as Record<
		string,
		unknown
	>[];
	return rows
		.map(mapWatchVehicle)
		.filter((v): v is VehicleWatchRecord => v !== null);
}

/** POST /api/v1/vehicles/{plate}/earmark — CITY WRITER (watchlist add). */
export async function earmarkVehicle(
	plate: string,
	input: {
		state: 'watch' | 'flagged';
		reason: string;
		lawfulBasis: string;
		retentionDays?: number;
		earmarkedBy: string;
	},
): Promise<VehicleWatchRecord | null> {
	const safePlate = plate.trim().toUpperCase();
	if (!/^[A-Z0-9 -]{4,16}$/.test(safePlate)) throw new ApiDown(`invalid plate: ${plate}`);
	const actor = requireActor('vehicle earmark', input.earmarkedBy);
	if (input.reason.trim().length < 3) throw new CommitRefused('vehicle earmark (reason too short)');
	const resp = await post<Record<string, unknown>>(
		`${ORACLE_V1}/vehicles/${encodeURIComponent(safePlate)}/earmark`,
		{
			state: input.state,
			reason: input.reason.trim().slice(0, 500),
			lawful_basis: input.lawfulBasis,
			...(input.retentionDays ? { retention_days: Math.min(90, Math.max(1, input.retentionDays)) } : {}),
			earmarked_by: actor,
		},
	);
	return mapWatchVehicle(resp);
}

/**
 * POST /api/v1/vehicles/{vehicle_id}/earmark/clear — CITY WRITER. Un-flagging
 * a stolen vehicle is consequential: the named officer is sent in the body
 * ({officer}) so the earmark clear row records who decided (C6).
 */
export async function clearEarmark(vehicleId: string, actor: string): Promise<void> {
	const id = safePathId(vehicleId);
	if (!id) throw new ApiDown(`invalid vehicle id: ${vehicleId}`);
	const officer = officerField(requireActor('earmark clear', actor));
	await post<Record<string, unknown>>(`${ORACLE_V1}/vehicles/${id}/earmark/clear`, { officer });
}

// ─── Pursuit / interception ──────────────────────────────────────────────────

/**
 * GET /api/v1/events/{id}/interception-plan — ranked choke junctions.
 * Read-only; 404/422 map to null-with-note (not every event is a pursuit).
 */
export async function fetchInterceptionPlan(eventId: string): Promise<InterceptionPlan> {
	const id = safePathId(eventId);
	if (!id) throw new ApiDown(`invalid event id: ${eventId}`);
	const resp = await request<Record<string, unknown>>(
		`${EVENTS_API}/${id}/interception-plan?max_cost_sec=600`,
		undefined,
		12000,
	);
	return parseInterceptionPlan(resp, id);
}

function directionOf(raw: unknown): InterceptionDirection | null {
	if (!raw || typeof raw !== 'object') return null;
	const d = raw as Record<string, unknown>;
	const ids = (v: unknown) => (Array.isArray(v) ? v.filter((n): n is number => typeof n === 'number' && Number.isFinite(n)) : []);
	return {
		basis: str(d.basis) ?? 'unknown',
		towardsJunctionIds: ids(d.towards_junction_ids),
		segmentBearingDeg: typeof d.segment_bearing_deg === 'number' ? d.segment_bearing_deg : null,
		positionAlongSegment: typeof d.position_along_segment === 'number' ? d.position_along_segment : null,
		seedSegmentOneway: d.seed_segment_oneway === true,
		againstOneway: d.against_oneway === true,
		onwardSegmentIds: ids(d.onward_segment_ids),
	};
}

/**
 * Pure parser for GET /events/{id}/interception-plan (exported for tests):
 * the ranked chokes with their names and likely_passed flags, the sighting
 * age + stale flag and the direction of travel (WP-03) — all additive, so
 * an older oracle's plan still maps.
 */
export function parseInterceptionPlan(resp: Record<string, unknown>, eventId: string): InterceptionPlan {
	const id = eventId;
	const sight = (resp.latest_sighting ?? null) as Record<string, unknown> | null;
	let latestSighting: InterceptionPlan['latestSighting'] = null;
	if (sight) {
		const pos = sight.position as unknown;
		if (Array.isArray(pos) && typeof pos[0] === 'number' && typeof pos[1] === 'number') {
			latestSighting = {
				sightingId: typeof sight.sighting_id === 'string' ? sight.sighting_id : '—',
				plate: typeof sight.plate === 'string' ? sight.plate : null,
				t: typeof sight.t === 'string' ? sight.t : null,
				position: [pos[0], pos[1]],
				speedKmh: typeof sight.speed_kmh === 'number' ? sight.speed_kmh : null,
				heading: typeof sight.heading === 'number' ? sight.heading : null,
				source: typeof sight.source === 'string' ? sight.source : null,
				cameraId: str(sight.camera_id),
			};
		}
	}
	const chokes = (Array.isArray(resp.interception_junctions) ? resp.interception_junctions : [])
		.map((j, i): InterceptionPlan['chokes'][number] => {
			const row = j as Record<string, unknown>;
			const pos = row.position as unknown;
			return {
				rank: i + 1,
				junctionId: typeof row.junction_id === 'number' ? row.junction_id : -1,
				junctionType: typeof row.junction_type === 'string' ? row.junction_type : 'junction',
				signalControl: row.signal_control === true,
				position: Array.isArray(pos) && typeof pos[0] === 'number' && typeof pos[1] === 'number' ? [pos[0], pos[1]] : [0, 0],
				graphCostSec: typeof row.graph_cost_sec === 'number' ? row.graph_cost_sec : null,
				chokeWeight: typeof row.choke_weight === 'number' ? row.choke_weight : null,
				score: typeof row.score === 'number' ? row.score : null,
				name: str(row.name),
				downstream: row.downstream === true,
				likelyPassed: row.likely_passed === true,
			};
		})
		.slice(0, 10);
	return {
		eventId: id,
		vehicleId: typeof resp.vehicle_id === 'string' ? resp.vehicle_id : null,
		latestSighting,
		seedSegmentId: typeof resp.seed_segment_id === 'number' ? resp.seed_segment_id : null,
		maxCostSec: typeof resp.max_cost_sec === 'number' ? resp.max_cost_sec : null,
		chokes,
		note: typeof resp.note === 'string' ? resp.note : null,
		sightingAgeSec: typeof resp.sighting_age_sec === 'number' ? resp.sighting_age_sec : null,
		stale: resp.stale === true,
		staleAfterSec: typeof resp.stale_after_sec === 'number' ? resp.stale_after_sec : null,
		direction: directionOf(resp.direction_of_travel),
	};
}

/**
 * POST /api/v1/events — CITY WRITER (open a pursuit; human-initiated, C6).
 * The oracle requires at least one route spine; pursuits anchor on the
 * segment of the vehicle's latest sighting (the same nearest-road rule the
 * interception plan seeds from), so the event sits on the graph where the
 * vehicle was last read. The C6 chain is checked server-side too: the
 * vehicle needs an active earmark and `recommendationId` must be an
 * APPROVED recommendation for it (422 otherwise). The named officer rides
 * as `officer` — the creation actor in the lifecycle ledger.
 */
export async function openPursuitEvent(
	input: {
		vehicleId: string;
		recommendationId?: string;
		plate: string | null;
		seedSegmentId: number;
		actor: string;
	},
): Promise<{ eventId: string }> {
	const actor = requireActor('pursuit open', input.actor);
	const vehicleId = safePathId(input.vehicleId);
	if (!vehicleId) throw new ApiDown(`invalid vehicle id: ${input.vehicleId}`);
	if (!Number.isInteger(input.seedSegmentId)) throw new ApiDown(`invalid anchor segment: ${input.seedSegmentId}`);
	const now = new Date().toISOString();
	const resp = await post<Record<string, unknown>>(
		EVENTS_API,
		{
			event_kind: 'vehicle_pursuit',
			protection_class: 'none',
			crowd_class: 'LOW',
			title: `PURSUIT — ${input.plate ?? input.vehicleId.slice(0, 8)}`,
			description: `Opened by ${actor} from the watchlist console${input.recommendationId ? ` (recommendation ${input.recommendationId})` : ''}.`,
			officer: officerField(actor),
			route_spines: [{ label: 'pursuit anchor', segment_ids: [input.seedSegmentId] }],
			phases: [
				{
					label: 'pursuit',
					window_start: now,
					window_end: new Date(Date.now() + 45 * 60 * 1000).toISOString(),
					closure_policy: {},
				},
			],
			metadata: {
				vehicle_id: vehicleId,
				...(input.recommendationId ? { recommendation_id: input.recommendationId } : {}),
			},
		},
		15000,
	);
	const eventId = resp.event_id ?? resp.eventId;
	if (!eventId) throw new ApiDown('pursuit event created but no event_id returned');
	return { eventId: String(eventId) };
}

// ─── Degradation ledger (decisions_log failure taxonomy) ─────────────────────

/**
 * GET /api/v1/ops/degradation-taxonomy — recurring degradation/failure
 * causes mined from convoy-brain's decisions_log. Read-only; an empty log
 * is a VALID honest answer (entries: []), never an error. Defensive parse
 * per the additive-API contract: absent/partial fields degrade to their
 * null-ish forms instead of throwing.
 */
export async function fetchDegradationTaxonomy(): Promise<DegradationTaxonomy> {
	const resp = await request<Record<string, unknown>>(DEGRADATION_TAXONOMY_API);
	const rows = (
		Array.isArray(resp.entries) ? resp.entries : []
	) as Array<Record<string, unknown>>;
	const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null);
	return {
		entries: rows.map((r) => ({
			cause: typeof r.cause === 'string' ? r.cause : '?',
			count: typeof r.count === 'number' ? r.count : 0,
			firstSeen: num(r.first_seen),
			lastSeen: num(r.last_seen),
			sampleRef: typeof r.sample_ref === 'string' ? r.sample_ref : '—',
		})),
		contextsScanned: num(resp.contexts_scanned) ?? 0,
		rowsScanned: num(resp.rows_scanned) ?? 0,
		truncated: resp.truncated === true,
		contextsSkippedCorrupt: num(resp.contexts_skipped_corrupt) ?? 0,
		generatedAt: typeof resp.generated_at === 'string' ? resp.generated_at : null,
	};
}

// ─── Brain posture (masthead strip) ──────────────────────────────────────────

/**
 * Pure: the brain posture from GET /api/convoy/health and GET
 * /api/v1/adaptive/status (either may be null when it did not answer).
 * model_host 'connected' means the decider is loaded; WP-12 adds 'loading'.
 */
export function parseBrainPosture(
	health: Record<string, unknown> | null,
	adaptive: Record<string, unknown> | null,
	note: string | null = null,
	now = Date.now(),
): BrainPosture {
	const strategic = (health?.strategic_tier ?? {}) as Record<string, unknown>;
	const tactical = (health?.tactical_tier ?? {}) as Record<string, unknown>;
	const host = str(strategic.model_host);
	const llama = str(tactical.llama_server);
	const degradation = (adaptive?.degradation ?? {}) as Record<string, unknown>;
	return {
		reachable: health !== null,
		status: str(health?.status),
		decider: {
			state:
				host === 'connected' ? 'loaded' : host === 'loading' ? 'loading' : host === 'unreachable' ? 'unreachable' : 'unknown',
			model: str(strategic.model),
			p95Ms: typeof strategic.p95_decide_latency_ms === 'number' ? strategic.p95_decide_latency_ms : null,
			latch: str(strategic.novelty_latch),
			degraded: typeof strategic.degraded === 'boolean' ? strategic.degraded : null,
			degradedReason: str(strategic.degraded_reason),
		},
		gemma: {
			state: llama === 'connected' ? 'connected' : llama === 'unreachable' ? 'unreachable' : 'unknown',
			model: str(tactical.model),
		},
		degradationTier: str(degradation.degradation_tier),
		checkpointsPaused: typeof degradation.decider_checkpoints_paused === 'boolean' ? degradation.decider_checkpoints_paused : null,
		checkedAt: now,
		note,
	};
}

/** Brain health + adaptive degradation, each read independently (one may fail). */
export async function fetchBrainPosture(): Promise<BrainPosture> {
	const [health, adaptive] = await Promise.allSettled([
		request<Record<string, unknown>>(BRAIN_HEALTH_API, undefined, 6000),
		request<Record<string, unknown>>(ADAPTIVE_STATUS_API, undefined, 6000),
	]);
	const notes: string[] = [];
	if (health.status === 'rejected') notes.push(`brain: ${health.reason instanceof Error ? health.reason.message : String(health.reason)}`);
	if (adaptive.status === 'rejected') notes.push(`adaptive: ${adaptive.reason instanceof Error ? adaptive.reason.message : String(adaptive.reason)}`);
	return parseBrainPosture(
		health.status === 'fulfilled' ? health.value : null,
		adaptive.status === 'fulfilled' ? adaptive.value : null,
		notes.length ? notes.join(' · ').slice(0, 200) : null,
	);
}

// ─── Traffic feed provenance (SIMULATED FEED honesty) ───────────────────────

/** Pure: the feed label from GET /corridor/summary (quality_mix, provenance). */
export function parseTrafficProvenance(resp: Record<string, unknown>, now = Date.now()): TrafficFeedProvenance {
	const mix = (resp.quality_mix ?? {}) as Record<string, unknown>;
	const real = typeof mix.real === 'number' ? mix.real : 0;
	const synthetic = typeof mix.synthetic === 'number' ? mix.synthetic : 0;
	const segments = typeof resp.total_segments === 'number' ? resp.total_segments : 0;
	const status = str(resp.status);
	let label: TrafficFeedProvenance['label'];
	if (status === 'no_data' || segments === 0 || real + synthetic === 0) {
		// No mix reported: fall back to the wire's own provenance word.
		label = status === 'no_data' || segments === 0 ? 'none' : resp.provenance === 'emulated' ? 'simulated' : resp.provenance === 'live' ? 'live' : 'none';
	} else if (real === 0) label = 'simulated';
	else if (synthetic === 0) label = 'live';
	else label = 'mixed';
	return {
		label,
		real,
		synthetic,
		segments,
		dataAgeSec: typeof resp.data_age_sec === 'number' ? resp.data_age_sec : null,
		checkedAt: now,
	};
}

/** GET /api/v1/corridor/summary → where the traffic readings come from. */
export async function fetchTrafficProvenance(): Promise<TrafficFeedProvenance> {
	return parseTrafficProvenance(await request<Record<string, unknown>>(CORRIDOR_SUMMARY_API, undefined, 6000));
}

// ─── Retention health (C6 / DPDP) ────────────────────────────────────────────

/** GET /api/v1/vehicles/retention — the policy and whether the reaper runs. */
export async function fetchRetention(): Promise<RetentionHealth> {
	const resp = await request<Record<string, unknown>>(VEHICLE_RETENTION_API, undefined, 6000);
	const reaper = (resp.reaper ?? {}) as Record<string, unknown>;
	const last = (reaper.last_result ?? {}) as Record<string, unknown>;
	return {
		defaultDays: typeof resp.default_retention_days === 'number' ? resp.default_retention_days : null,
		earmarkCapDays: typeof resp.earmark_retention_cap_days === 'number' ? resp.earmark_retention_cap_days : null,
		policy: str(resp.policy),
		healthy: typeof reaper.healthy === 'boolean' ? reaper.healthy : null,
		consecutiveFailures: typeof reaper.consecutive_failures === 'number' ? reaper.consecutive_failures : 0,
		lastAttemptAt: str(reaper.last_attempt_at),
		lastSuccessAt: str(reaper.last_success_at),
		lastRemoved: typeof last.removed === 'number' ? last.removed : null,
		checkedAt: Date.now(),
	};
}

// ─── City geometry: junction registry + segment lines ────────────────────────

/** GET /api/v1/city/entities?kinds=junction — named junctions with positions. */
export async function fetchJunctionRegistry(): Promise<JunctionRef[]> {
	const resp = await request<Record<string, unknown>>(`${CITY_ENTITIES_API}?kinds=junction`, undefined, 8000);
	const rows = Array.isArray(resp.entities) ? (resp.entities as Array<Record<string, unknown>>) : [];
	const out: JunctionRef[] = [];
	for (const r of rows) {
		const id = Number(r.id);
		if (!Number.isInteger(id) || typeof r.lon !== 'number' || typeof r.lat !== 'number') continue;
		const props = (r.props ?? {}) as Record<string, unknown>;
		out.push({
			junctionId: id,
			name: str(props.name) ?? str(r.label) ?? `J${id}`,
			nameGu: str(props.name_gu),
			lon: r.lon,
			lat: r.lat,
			junctionType: str(props.junction_type),
			signalControl: typeof props.signal_control === 'boolean' ? props.signal_control : null,
		});
	}
	return out.sort((a, b) => a.junctionId - b.junctionId);
}

/** GET /api/v1/spatial/segments over the city bbox — road lines + names. */
export async function fetchSegmentLines(): Promise<SegmentRef[]> {
	const resp = await request<Record<string, unknown>>(
		`${SPATIAL_SEGMENTS_API}?min_lon=${NETWORK_BBOX.minLon}&min_lat=${NETWORK_BBOX.minLat}&max_lon=${NETWORK_BBOX.maxLon}&max_lat=${NETWORK_BBOX.maxLat}`,
		undefined,
		10000,
	);
	const rows = Array.isArray(resp.segments) ? (resp.segments as Array<Record<string, unknown>>) : [];
	const out: SegmentRef[] = [];
	for (const s of rows) {
		if (typeof s.segment_id !== 'number') continue;
		const geom = (s.geometry ?? {}) as Record<string, unknown>;
		const coordinates: Array<[number, number]> = [];
		for (const c of Array.isArray(geom.coordinates) ? (geom.coordinates as unknown[]) : []) {
			if (Array.isArray(c) && typeof c[0] === 'number' && typeof c[1] === 'number') coordinates.push([c[0], c[1]]);
		}
		out.push({ segmentId: s.segment_id, roadName: str(s.road_name), roadClass: str(s.road_class), coordinates });
	}
	return out;
}

/**
 * Metres from a point to a polyline: an equirectangular projection at the
 * point's latitude (city-scale distances; < 0.1 % error across Ahmedabad).
 * Exported pure for tests.
 */
export function distanceToLineM(point: [number, number], line: Array<[number, number]>): number {
	if (!line.length) return Infinity;
	const R = 6_371_008.8;
	const kx = (Math.PI / 180) * R * Math.cos((point[1] * Math.PI) / 180);
	const ky = (Math.PI / 180) * R;
	const px = point[0] * kx;
	const py = point[1] * ky;
	if (line.length === 1) return Math.hypot(line[0][0] * kx - px, line[0][1] * ky - py);
	let best = Infinity;
	for (let i = 0; i < line.length - 1; i++) {
		const ax = line[i][0] * kx;
		const ay = line[i][1] * ky;
		const bx = line[i + 1][0] * kx;
		const by = line[i + 1][1] * ky;
		const dx = bx - ax;
		const dy = by - ay;
		const len2 = dx * dx + dy * dy;
		const t = len2 > 0 ? Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / len2)) : 0;
		best = Math.min(best, Math.hypot(ax + t * dx - px, ay + t * dy - py));
	}
	return best;
}

/**
 * The road segment a sighting attributes to: the nearest line within
 * `tolM` (the oracle's interception seed rule), ties to the lower id; null
 * when no road is that close. Exported pure for tests.
 */
export function nearestSegment(
	point: [number, number],
	segments: SegmentRef[],
	tolM: number,
): { segment: SegmentRef; distanceM: number } | null {
	let best: { segment: SegmentRef; distanceM: number } | null = null;
	for (const s of segments) {
		const d = distanceToLineM(point, s.coordinates);
		if (d > tolM) continue;
		if (!best || d < best.distanceM - 1e-6 || (Math.abs(d - best.distanceM) <= 1e-6 && s.segmentId < best.segment.segmentId)) {
			best = { segment: s, distanceM: d };
		}
	}
	return best;
}

/**
 * Junctions ordered by their distance to an event's spine (its segment
 * lines), nearest first, ties to the lower id. Exported pure for tests.
 */
export function junctionsNearSpine(
	spineIds: number[],
	segments: SegmentRef[],
	junctions: JunctionRef[],
	limit = 3,
): Array<{ junction: JunctionRef; distanceM: number }> {
	const lines = segments.filter((s) => spineIds.includes(s.segmentId) && s.coordinates.length);
	if (!lines.length) return [];
	return junctions
		.map((j) => ({
			junction: j,
			distanceM: Math.min(...lines.map((s) => distanceToLineM([j.lon, j.lat], s.coordinates))),
		}))
		.sort((a, b) => a.distanceM - b.distanceM || a.junction.junctionId - b.junction.junctionId)
		.slice(0, limit);
}
