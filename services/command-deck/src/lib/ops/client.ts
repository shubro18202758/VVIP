/**
 * Ops showcase API client — every outbound call the /ops board makes.
 *
 * Transport decisions (from the showcase plan, contract C6):
 *   - traffic-oracle + convoy-brain are reached through the deck origin's
 *     proxy prefixes (vite dev proxy; no EventSource anywhere — streaming is
 *     NDJSON over fetch with a ReadableStream reader, and WS via the ladder).
 *   - the emulator is the ONE absolute loopback endpoint (EMULATOR_BASE).
 *   - all URLs are fixed constants or built from `safePathId`-validated ids;
 *     nothing here accepts a caller-supplied URL (see endpoints.ts guard).
 *
 * Every fetch reports its degradation honestly: 404 raises ApiNotDeployed so
 * panels can fall back and label the source instead of showing a blank.
 */

import {
	BRAIN_API,
	EMULATOR_BASE,
	EMULATOR_PROXY_PATH,
	ORACLE_V1,
	ENFORCEMENT_API,
	DISPATCHES_API,
	EVENTS_API,
	SIGNALS_PLANS_API,
	assertSafeExternalUrl,
	safePathId,
} from './endpoints';
import type {
	BandCode,
	BandEvidence,
	CorridorEvent,
	CrowdClass,
	DispatchAuditEntry,
	DispatchRecord,
	EnforcementRecommendation,
	EventKind,
	EventPhase,
	EventSource,
	EventPhaseState,
	InterceptionPlan,
	OverrideRecord,
	ProtectionClass,
	SignalApplyOutcome,
	SignalBandPlanRow,
	SignalLastPlan,
	SignalPlansDoc,
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

async function request<T>(url: string, init?: RequestInit, timeoutMs = 8000): Promise<T> {
	const ctrl = new AbortController();
	const timer = setTimeout(() => ctrl.abort(), timeoutMs);
	try {
		const res = await fetch(url, { ...init, signal: ctrl.signal });
		if (res.status === 404) {
			throw new ApiNotDeployed(404, `endpoint not deployed: ${url}`);
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

function mapEventRow(row: Record<string, unknown>, source: EventSource): CorridorEvent | null {
	const eventId = row.event_id ?? row.id ?? row.eventId;
	if (typeof eventId !== 'string' && typeof eventId !== 'number') return null;
	const kind = pick(row.event_kind ?? row.eventKind, KINDS) ?? 'protected_movement';
	const protection =
		pick(row.protection_class ?? row.protectionClass ?? row.vvip_class ?? row.vvipClass, PROTECTION) ??
		'none';
	const crowd = pick(row.crowd_class ?? row.crowdClass, CROWDS) ?? 'LOW';
	const state = pick(row.state ?? row.status ?? row.lifecycle_state, [
		'planned',
		'active',
		'completed',
		'cancelled',
	]) ?? 'planned';
	const route = Array.isArray(row.route_segment_ids ?? row.routeSegmentIds)
		? ((row.route_segment_ids ?? row.routeSegmentIds) as unknown[]).filter(
				(n): n is number => typeof n === 'number',
			)
		: [];
	const rawPhases = Array.isArray(row.phases) ? (row.phases as Record<string, unknown>[]) : [];
	const phases: EventPhase[] = rawPhases.map((p) => ({
		label: typeof p.label === 'string' ? p.label : 'phase',
		startTime: typeof p.start_time === 'string' ? p.start_time : (p.startTime as string) ?? null,
		endTime: typeof p.end_time === 'string' ? p.end_time : (p.endTime as string) ?? null,
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
	return {
		eventId: String(eventId),
		name: typeof row.name === 'string' ? row.name : String(eventId),
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
		windowStart: typeof row.window_start === 'string' ? row.window_start : null,
		windowEnd: typeof row.window_end === 'string' ? row.window_end : null,
		description: typeof row.description === 'string' ? row.description : null,
		source,
	};
}

/** GET /api/v1/events/active — the generalized multi-event board feed. */
export async function fetchActiveEvents(): Promise<CorridorEvent[]> {
	const resp = await request<Record<string, unknown>>(`${ORACLE_V1}/events/active`);
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

export interface SituationLogPoint {
	ts: number;
	band: BandCode;
	severityScore: number | null;
}

/** GET /api/v1/adaptive/situation_log?event_id= — band history (primary). */
export async function fetchSituationLog(eventId: string | null): Promise<SituationLogPoint[]> {
	const id = safePathId(eventId);
	const qs = id ? `?event_id=${encodeURIComponent(id)}&limit=120` : '?limit=120';
	const resp = await request<Record<string, unknown>>(`${ORACLE_V1}/adaptive/situation_log${qs}`);
	const rows = (Array.isArray(resp) ? resp : ((resp.entries ?? resp.log ?? resp.rows ?? []) as unknown[])) as Record<
		string,
		unknown
	>[];
	const out: SituationLogPoint[] = [];
	for (const r of rows) {
		const band = pick(r.situation_class ?? r.band ?? r.situation_code, ['GREEN', 'AMBER', 'RED', 'BLACK']);
		if (!band) continue;
		const rawTs =
			(typeof r.timestamp_utc === 'string' || typeof r.ts === 'string'
				? Date.parse(String(r.timestamp_utc ?? r.ts))
				: typeof r.timestamp === 'number'
					? r.timestamp
					: NaN) || Date.now();
		out.push({
			ts: rawTs,
			band,
			severityScore: typeof r.severity_score === 'number' ? r.severity_score : null,
		});
	}
	return out.sort((a, b) => a.ts - b.ts);
}

export interface AdaptiveStatusSnapshot {
	movements: Array<{
		id: string;
		vvipClass: string | null;
		situationCode: BandCode | null;
		severityScore: number | null;
		observationCount: number;
	}>;
	degradationTier: string;
	auditTrail: Array<Record<string, unknown>>;
	lastShadowEval: string | null;
}

/** GET /api/v1/adaptive/status — today's live band source (fallback rung). */
export async function fetchAdaptiveStatus(): Promise<AdaptiveStatusSnapshot> {
	const resp = await request<Record<string, unknown>>(`${ORACLE_V1}/adaptive/status`);
	const movements = (resp.movements ?? {}) as Record<string, Record<string, unknown>>;
	const degradation = (resp.degradation ?? {}) as Record<string, unknown>;
	const shadow = (resp.last_shadow_eval ?? {}) as Record<string, unknown>;
	return {
		movements: Object.entries(movements).map(([id, m]) => ({
			id,
			vvipClass: typeof m.vvip_class === 'string' ? m.vvip_class : null,
			situationCode: pick(m.situation_code, ['GREEN', 'AMBER', 'RED', 'BLACK']),
			severityScore: typeof m.severity_score === 'number' ? m.severity_score : null,
			observationCount: typeof m.observation_count === 'number' ? m.observation_count : 0,
		})),
		degradationTier: typeof degradation.degradation_tier === 'string' ? degradation.degradation_tier : 'unknown',
		auditTrail: Array.isArray(degradation.audit_trail)
			? (degradation.audit_trail as Array<Record<string, unknown>>)
			: [],
		lastShadowEval: typeof shadow.status === 'string' ? shadow.status : null,
	};
}

/** GET /api/v1/adaptive/params?vvip_class= — band + shadow-eval evidence. */
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

/** GET /api/v1/adaptive/overrides?event_id= — param-override audit evidence. */
export async function fetchOverrides(eventId: string | null): Promise<OverrideRecord[]> {
	const id = safePathId(eventId);
	const qs = id ? `?event_id=${encodeURIComponent(id)}&limit=50` : '?limit=50';
	const resp = await request<Record<string, unknown>>(`${ORACLE_V1}/adaptive/overrides${qs}`);
	const rows = (Array.isArray(resp) ? resp : ((resp.overrides ?? resp.rows ?? []) as unknown[])) as Record<
		string,
		unknown
	>[];
	return rows.map((r) => ({
		ts: typeof r.timestamp_utc === 'string' ? r.timestamp_utc : null,
		eventId: typeof r.event_id === 'string' ? r.event_id : null,
		protectionClass: pick(r.protection_class ?? r.vvip_class, PROTECTION),
		situationClass: pick(r.situation_class, ['GREEN', 'AMBER', 'RED', 'BLACK']),
		shadowEval: typeof r.shadow_eval === 'string' ? r.shadow_eval : null,
		floorApplied: typeof r.floor_applied === 'boolean' ? r.floor_applied : null,
		failClosedReason: typeof r.fail_closed_reason === 'string' ? r.fail_closed_reason : null,
		params: (r.params ?? null) as Record<string, unknown> | null,
	}));
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

// ─── Emulator (absolute loopback, contract C6; proxied dev fallback) ─────────

/**
 * Fetch against the emulator: try the absolute loopback constant first (the
 * production path — the emulator CORS-allows the deck's :5173 origin), then
 * the same-origin dev-proxy path when the browser blocks the absolute URL.
 * Both bases are fixed config constants; `path` is a fixed template.
 */
async function emulatorFetch(path: string, init?: RequestInit, timeoutMs = 6000): Promise<Response> {
	const bases = [EMULATOR_BASE, EMULATOR_PROXY_PATH];
	let lastErr: unknown = null;
	for (const base of bases) {
		try {
			return await fetch(`${base}${path}`, { ...init, signal: AbortSignal.timeout(timeoutMs) });
		} catch (err) {
			lastErr = err;
		}
	}
	throw lastErr instanceof Error ? lastErr : new Error('emulator unreachable');
}

/** GET /health on the emulator (verified contract: {status, library, stack}). */
export async function emulatorHealth(): Promise<{ online: boolean; detail: string }> {
	try {
		const res = await emulatorFetch('/health', undefined, 4000);
		const body = await res.text().catch(() => '');
		return { online: res.ok, detail: `${res.status}: ${body.slice(0, 160)}` };
	} catch (err) {
		return { online: false, detail: err instanceof Error ? err.message : String(err) };
	}
}

/** One use-case record from GET /api/v1/emulator/use-cases. */
export interface EmulatorUseCase {
	id: string;
	name?: string;
	vvipClass?: string;
	routeSegmentIds?: number[];
	seed?: number;
}

/** GET /api/v1/emulator/use-cases — the authoritative cause-tape library. */
export async function emulatorUseCases(): Promise<EmulatorUseCase[]> {
	const res = await emulatorFetch('/api/v1/emulator/use-cases', undefined, 5000);
	if (!res.ok) throw new ApiDown(`use-cases ${res.status}`);
	const rows = (await res.json()) as Array<Record<string, unknown>>;
	return rows.map((r) => ({
		id: String(r.id ?? ''),
		name: typeof r.name === 'string' ? r.name : undefined,
		vvipClass: typeof r.vvip_class === 'string' ? r.vvip_class : undefined,
		routeSegmentIds: Array.isArray(r.route_segment_ids) ? (r.route_segment_ids as number[]) : undefined,
		seed: typeof r.seed === 'number' ? r.seed : undefined,
	}));
}

/** One emulation run row from GET /api/v1/emulator/runs. */
export interface EmulatorRun {
	runId: number;
	useCaseId: string;
	status: string;
	clockScale: number | null;
	isReplay: boolean;
	createdAt: string | null;
}

/** GET /api/v1/emulator/runs?limit= — recent cause-tape runs. */
export async function emulatorListRuns(limit = 8): Promise<EmulatorRun[]> {
	const res = await emulatorFetch(`/api/v1/emulator/runs?limit=${limit}`, undefined, 5000);
	if (!res.ok) throw new ApiDown(`runs ${res.status}`);
	const resp = (await res.json()) as { runs?: Array<Record<string, unknown>> };
	return (resp.runs ?? []).map((r) => {
		const spec = (r.spec ?? {}) as Record<string, unknown>;
		return {
			runId: typeof r.run_id === 'number' ? r.run_id : -1,
			useCaseId: typeof spec.use_case_id === 'string' ? spec.use_case_id : '—',
			status: typeof r.status === 'string' ? r.status : '—',
			clockScale: typeof r.clock_scale === 'number' ? r.clock_scale : null,
			isReplay: spec.is_replay === true,
			createdAt: typeof r.created_at === 'string' ? r.created_at : null,
		};
	});
}

/**
 * POST /api/v1/emulator/runs — stage a cause-tape run for a use case
 * (CreateRunRequest: {use_case_id, seed?, t0_anchor?, clock_scale?, tick_sec?,
 * deliver_nats?, deliver_twin?}). The endpoint holds its response until the
 * tape finishes delivering, so a long timeout is used and the RUN ROW (which
 * the emulator writes immediately) is the real confirmation — the store polls
 * GET /runs to surface "running" → "completed". A client-side timeout does
 * NOT stop the tape (verified live: run row keeps executing server-side).
 */
export async function emulatorStageUseCase(
	useCaseId: string,
	opts?: { clockScale?: number },
): Promise<{ ok: boolean; detail: string }> {
	const id = safePathId(useCaseId);
	if (!id) return { ok: false, detail: 'invalid use-case id' };
	try {
		const res = await emulatorFetch(
			'/api/v1/emulator/runs',
			{
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({
					use_case_id: id,
					...(opts?.clockScale ? { clock_scale: opts.clockScale } : {}),
				}),
			},
			180000,
		);
		const body = await res.text().catch(() => '');
		return { ok: res.ok, detail: `${res.status}: ${body.slice(0, 220)}` };
	} catch (err) {
		return {
			ok: false,
			detail: `request timed out or failed (the run may still be executing server-side): ${
				err instanceof Error ? err.message : String(err)
			}`,
		};
	}
}

// ─── Audit stream: NDJSON tail + WS ladder ───────────────────────────────────

export interface AuditStreamCallbacks {
	onThought: (text: string) => void;
	onToken: (text: string) => void;
	onToolCall: (name: string, args: Record<string, unknown>) => void;
	onToolResult: (name: string, state: string, durationMs: number) => void;
	onDone: () => void;
	onError: (message: string) => void;
}

/**
 * NDJSON audit probe over POST /chat/stream — consumed with a ReadableStream
 * reader (never EventSource; the endpoint is NDJSON, not SSE). Streams the
 * decider→Gemma→tool ladder from the brain's audit channels.
 */
export async function streamAuditProbe(
	message: string,
	callbacks: AuditStreamCallbacks,
): Promise<void> {
	const res = await fetch(`${BRAIN_API}/chat/stream`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ message, movement_id: null, vvip_class: null }),
	});
	if (!res.ok || !res.body) {
		callbacks.onError(`chat/stream ${res.status} ${res.statusText}`);
		return;
	}
	const reader = res.body.getReader();
	const decoder = new TextDecoder();
	let buffer = '';
	let currentTool = '';
	try {
		while (true) {
			const { done, value } = await reader.read();
			if (done) break;
			buffer += decoder.decode(value, { stream: true });
			const lines = buffer.split('\n');
			buffer = lines.pop() ?? '';
			for (const line of lines) {
				const trimmed = line.trim();
				if (!trimmed) continue;
				try {
					const ev = JSON.parse(trimmed) as { type: string; data: unknown };
					switch (ev.type) {
						case 'token':
							callbacks.onToken(typeof ev.data === 'string' ? ev.data : '');
							break;
						case 'thought': {
							const d = ev.data as { text?: string };
							callbacks.onThought(typeof d?.text === 'string' ? d.text : JSON.stringify(ev.data));
							break;
						}
						case 'tool_call': {
							const d = ev.data as { toolName?: string; tool_name?: string; arguments?: Record<string, unknown> };
							currentTool = d?.toolName ?? d?.tool_name ?? 'tool';
							callbacks.onToolCall(currentTool, d?.arguments ?? {});
							break;
						}
						case 'tool_result': {
							const d = ev.data as { state?: string; durationMs?: number };
							callbacks.onToolResult(currentTool, d?.state ?? 'done', d?.durationMs ?? 0);
							break;
						}
						case 'done':
							callbacks.onDone();
							return;
						case 'error':
							callbacks.onError(String(ev.data));
							return;
					}
				} catch {
					// partial JSON line — accumulate
				}
			}
		}
		callbacks.onDone();
	} catch (err) {
		callbacks.onError(err instanceof Error ? err.message : String(err));
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
	/** Fetched when the generalized per-event audit endpoint exists. */
	eventAudit: Record<string, unknown>[] | null;
	adaptive: AdaptiveStatusSnapshot | null;
	source: 'event-audit' | 'adaptive-status';
}

/**
 * The 2 s poll rung of the audit ladder: prefer the generalized per-event
 * audit tail, fall back to the adaptive status (whose audit_trail + per-event
 * situation snapshot is a real audit surface today).
 */
export async function pollAuditTail(eventId: string | null): Promise<AuditPollSnapshot> {
	const id = safePathId(eventId);
	if (id) {
		try {
			const resp = await request<Record<string, unknown>>(
				`${ORACLE_V1}/events/${encodeURIComponent(id)}/audit?limit=50`,
				undefined,
				4000,
			);
			const rows = (Array.isArray(resp) ? resp : ((resp.entries ?? resp.audit ?? []) as unknown[])) as Record<
				string,
				unknown
			>[];
			return { eventAudit: rows, adaptive: null, source: 'event-audit' };
		} catch (err) {
			if (!(err instanceof ApiNotDeployed)) {
				// Real failure on a deployed endpoint — surface it via the status rung.
				const adaptive = await fetchAdaptiveStatus();
				return { eventAudit: null, adaptive, source: 'adaptive-status' };
			}
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

/** Require a non-blank human actor for a writer action. Trims + caps. */
function requireActor(operation: string, actor: string): string {
	const who = actor.trim();
	if (who.length < 2) throw new CommitRefused(operation);
	return who.slice(0, 120);
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
	};
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
 * POST /api/v1/enforcement/recommendations/{id}/decision — CITY WRITER.
 * The C6 gate: approve|reject only ever under a named officer identity.
 * Smokes NEVER fire this (stubbed per contract R8).
 */
export async function decideEnforcementRecommendation(
	recommendationId: string,
	input: { decision: 'approve' | 'reject'; officer: string; note?: string },
): Promise<EnforcementRecommendation['status']> {
	const id = safePathId(recommendationId);
	if (!id) throw new ApiDown(`invalid recommendation id: ${recommendationId}`);
	const officer = requireActor('e-challan decision', input.officer);
	const resp = await post<Record<string, unknown>>(
		`${ENFORCEMENT_API}/recommendations/${id}/decision`,
		{
			decision: input.decision,
			officer,
			note: (input.note ?? '').slice(0, 500),
		},
	);
	return resp.status === 'approved' ? 'approved' : 'rejected';
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
	const requestedBy = requireActor('unit dispatch request', input.requestedBy);
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
	const officer = requireActor('dispatch decision', input.officer);
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
	const officer = requireActor('dispatch status', input.officer);
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
			return {
				band,
				priorityShare: typeof row.priority_share === 'number' ? row.priority_share : null,
				crossGreenFloorSec:
					typeof row.cross_green_floor_sec === 'number' ? row.cross_green_floor_sec : null,
				writes: typeof row.writes === 'number' ? row.writes : null,
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
	const resp = await request<Record<string, unknown>>(SIGNALS_PLANS_API, undefined, 8000);
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
			twinInFlight: typeof gate.twin_in_flight === 'boolean' ? gate.twin_in_flight : null,
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
 * held-at-baseline state with its reason. Smokes NEVER fire this (R8).
 */
export async function applySignalPlan(
	input: { band?: BandCode; eventId?: string; junctionIds?: number[]; routeSegmentIds?: number[] },
	actor: string,
): Promise<SignalApplyOutcome> {
	requireActor('signal plan apply', actor);
	const eventId = input.eventId ? safePathId(input.eventId) : null;
	if (input.eventId && !eventId) throw new ApiDown(`invalid event id: ${input.eventId}`);
	const body: Record<string, unknown> = {};
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

/** POST /api/v1/signals/restore — CITY WRITER (rebuild assumed programs). */
export async function restoreSignalPrograms(
	input: { eventId?: string; all?: boolean },
	actor: string,
): Promise<Record<string, unknown>> {
	requireActor('signal program restore', actor);
	const eventId = input.eventId ? safePathId(input.eventId) : null;
	if (input.eventId && !eventId) throw new ApiDown(`invalid event id: ${input.eventId}`);
	return post<Record<string, unknown>>(`${ORACLE_V1}/signals/restore`, {
		...(eventId ? { event_id: eventId } : {}),
		...(input.all ? { all: true } : {}),
	});
}

/** GET /api/v1/signals/history — prior applies/restores for the card. */
export async function fetchSignalHistory(limit = 20): Promise<Array<Record<string, unknown>>> {
	const resp = await request<{ history?: Array<Record<string, unknown>> }>(
		`${SIGNALS_PLANS_API}/history?limit=${limit}`,
		undefined,
		8000,
	);
	return resp.history ?? [];
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

/** POST /api/v1/vehicles/{vehicle_id}/earmark/clear — CITY WRITER. */
export async function clearEarmark(vehicleId: string, actor: string): Promise<void> {
	const id = safePathId(vehicleId);
	if (!id) throw new ApiDown(`invalid vehicle id: ${vehicleId}`);
	requireActor('earmark clear', actor);
	await post<Record<string, unknown>>(`${ORACLE_V1}/vehicles/${id}/earmark/clear`, {});
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
	};
}

/**
 * POST /api/v1/events — CITY WRITER (open a pursuit; human-initiated, C6).
 * The oracle requires at least one route spine; pursuits reuse the pursued
 * vehicle's seed segment as the spine so the event anchors on the graph.
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
	const now = new Date().toISOString();
	const resp = await post<Record<string, unknown>>(
		EVENTS_API,
		{
			event_kind: 'vehicle_pursuit',
			protection_class: 'none',
			crowd_class: 'LOW',
			title: `PURSUIT — ${input.plate ?? input.vehicleId.slice(0, 8)}`,
			description: `Opened by ${actor} from the watchlist console${input.recommendationId ? ` (recommendation ${input.recommendationId})` : ''}.`,
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
