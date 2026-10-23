/**
 * API client for convoy-brain and traffic-oracle backend services.
 *
 * Provides REST, WebSocket, and streaming interfaces for:
 * - Convoy movement lifecycle (plan, approve, launch, abort)
 * - Traffic data queries and real-time snapshots
 * - LLM chat with thought/tool streaming, canvas directives (C-D2) and
 *   their acknowledgement
 * - Service health checks
 *
 * Convention: Backend uses snake_case. Frontend uses camelCase.
 * All conversion happens in this module — callers always use camelCase.
 */

import type {
	ConvoyMovement,
	SegmentTraffic,
	RoadSegment,
	TrafficPrediction,
	TrafficAnomaly,
	VvipClass,
	WsEvent,
	ServiceHealth,
	GpuStatus,
} from '$lib/types';

const CONVOY_API = '/api/convoy';
const TRAFFIC_API = '/api/traffic';

// ─── Error Handling ─────────────────────────────────────────────────────────

export class ApiError extends Error {
	/**
	 * The server's own `detail` (FastAPI's error body), when it sent one —
	 * additive: a caller can map a 409's reason (e.g. "a live escort is
	 * running") without parsing the message.
	 */
	public detail: unknown;

	constructor(
		public status: number,
		message: string,
		detail: unknown = null,
	) {
		super(message);
		this.name = 'ApiError';
		this.detail = detail;
	}
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
	const res = await fetch(url, init);
	if (!res.ok) {
		const body = await res.text().catch(() => 'Unknown error');
		let detail: unknown = null;
		try {
			const parsed = JSON.parse(body) as unknown;
			detail = parsed && typeof parsed === 'object' && 'detail' in parsed ? (parsed as { detail: unknown }).detail : parsed;
		} catch {
			detail = body || null;
		}
		throw new ApiError(res.status, `${res.status} ${res.statusText}: ${body}`, detail);
	}
	return res.json();
}

function post<T>(url: string, body: unknown): Promise<T> {
	return request<T>(url, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(body),
	});
}

// ─── Convoy REST API ────────────────────────────────────────────────────────

export async function createMovement(params: {
	origin: [number, number];
	destination: [number, number];
	vvipClass: VvipClass;
	plannedDeparture: string;
	/** Optional operator label (≤120 chars, e.g. 'ACCEPTANCE TEST escort'): it
	 * prefixes the corridor event's title and rides as metadata.label. */
	title?: string | null;
}): Promise<{ movementId: string }> {
	const body: Record<string, unknown> = {
		origin: params.origin,
		destination: params.destination,
		vvip_class: params.vvipClass,
		planned_departure: params.plannedDeparture,
	};
	const title = params.title?.trim();
	if (title) body.title = title.slice(0, 120);
	const resp = await post<{ movement_id: string }>(`${CONVOY_API}/movements`, body);
	return { movementId: resp.movement_id };
}

/** One lane-floor violation the planner's protocol check found on a route. */
export interface PlanLaneViolation {
	segment_id?: number;
	detail?: string;
	type?: string;
	severity?: string;
}

/** Route candidate as it survives to the plan document (contract C-I): the
 * solver metrics (ETA always > 0, distance, scores on 0..1), the protocol
 * tier and its lane-floor violations, plus the tactical tier's per-route
 * reason. `total_distance_m` is the pre-C-I spelling some brains still send. */
export interface PlanRouteCandidate {
	route_id: string;
	segment_ids: number[];
	road_names?: string[];
	score?: number;
	reason?: string;
	distance_m?: number;
	total_distance_m?: number;
	estimated_time_sec?: number;
	disruption_score?: number;
	security_score?: number;
	composite_score?: number;
	weighted_score?: number;
	/** compliant = meets the class's lane floor; conditional = violations listed. */
	route_tier?: 'compliant' | 'conditional' | string;
	lane_violations?: PlanLaneViolation[];
}

/** The decider's strategic pick over the real route ids (contract C-I). */
export interface PlanDeciderPick {
	route_id: string | null;
	confidence: number | null;
	tier: string;
	choice?: string;
	/** True when the pick changed (or confirmed) the ranking / held the plan. */
	applied?: boolean;
	reason?: string;
}

/** POST /movements/{id}/plan — the pre-movement workflow document (C-I).
 * `status`: approved | conditional (protocol violations, waiver first) |
 * hold (the decider delays departure) | no_route. */
export interface MovementPlanDoc {
	movement_id: string;
	primary_route: PlanRouteCandidate | null;
	alternate_routes: PlanRouteCandidate[];
	rejected_routes?: Array<{ route_id: string; reason?: string }>;
	overall_reasoning?: string;
	confidence?: string;
	status?: 'approved' | 'conditional' | 'hold' | 'no_route' | string;
	hold?: boolean;
	security_compliant?: boolean;
	security_score?: number;
	security_violations?: Array<{ segment_id?: number; detail?: string; type?: string }>;
	diversion_directives?: unknown[];
	decider_checkpoint?: unknown;
	decider_pick?: PlanDeciderPick | null;
	adaptive_params?: unknown;
	/** The corridor event the plan was registered as (C-I), when fused. */
	event_id?: string | null;
	routing_tier?: string;
}

export async function planMovement(
	movementId: string,
	params: {
		origin: [number, number];
		destination: [number, number];
		vvipClass: VvipClass;
		plannedDeparture: string;
	},
): Promise<MovementPlanDoc> {
	return post(`${CONVOY_API}/movements/${movementId}/plan`, {
		origin: params.origin,
		destination: params.destination,
		vvip_class: params.vvipClass,
		planned_departure: params.plannedDeparture,
	});
}

/** POST /movements/{id}/escort answer: C-J 202 {status:'running'} or a legacy loop result. */
export interface EscortLaunch {
	movement_id?: string;
	status?: string;
	task_id?: string;
	/** Legacy (pre C-J) brains run the loop inline and answer when it ends. */
	escort_complete?: boolean;
	final_status?: string;
	total_iterations?: number;
}

/**
 * Launch the live escort (contract C-J). The named duty officer travels as
 * {officer} — the brain records it on the corridor event's activation (C-F)
 * and requires it once VVIP_REQUIRE_OFFICER=1. `destination` stays for the
 * legacy brain's emergency re-route.
 */
export async function launchEscort(
	movementId: string,
	opts: { officer?: string | null; destination?: [number, number] } = {},
): Promise<EscortLaunch> {
	const body: Record<string, unknown> = {};
	const officer = opts.officer?.trim();
	if (officer) body.officer = officer.slice(0, 64);
	if (opts.destination) body.destination = opts.destination;
	return post(`${CONVOY_API}/movements/${encodeURIComponent(movementId)}/escort`, body);
}

/** POST /movements/{id}/stop (C-J, idempotent). */
export async function stopEscort(movementId: string, officer?: string | null): Promise<unknown> {
	const who = officer?.trim();
	return post(`${CONVOY_API}/movements/${encodeURIComponent(movementId)}/stop`, who ? { officer: who.slice(0, 64) } : {});
}

export async function clearMovement(movementId: string): Promise<unknown> {
	return post(`${CONVOY_API}/movements/${movementId}/clear`, {});
}

// ─── Traffic REST API ───────────────────────────────────────────────────────

/**
 * Fetch live traffic conditions for segments within a bounding box.
 * Backend returns camelCase: [{segmentId, speedKmh, congestionIdx, lastUpdated}]
 */
export async function getTrafficSnapshot(bbox: {
	minLon: number;
	minLat: number;
	maxLon: number;
	maxLat: number;
}): Promise<SegmentTraffic[]> {
	const params = new URLSearchParams({
		min_lon: bbox.minLon.toString(),
		min_lat: bbox.minLat.toString(),
		max_lon: bbox.maxLon.toString(),
		max_lat: bbox.maxLat.toString(),
	});
	return request(`${TRAFFIC_API}/snapshot?${params}`);
}

/**
 * Fetch road segment geometries within a bounding box.
 * Backend returns camelCase: [{segmentId, osmWayId, roadName, ...geometry}]
 */
export async function getRoadSegments(bbox: {
	minLon: number;
	minLat: number;
	maxLon: number;
	maxLat: number;
}): Promise<RoadSegment[]> {
	const params = new URLSearchParams({
		min_lon: bbox.minLon.toString(),
		min_lat: bbox.minLat.toString(),
		max_lon: bbox.maxLon.toString(),
		max_lat: bbox.maxLat.toString(),
	});
	return request(`${TRAFFIC_API}/segments?${params}`);
}

/**
 * Request DSTGAT flow predictions for given segments.
 * Backend returns camelCase: [{segmentId, horizonMin, predictedSpeedKmh, ...}]
 */
export async function getTrafficPredictions(
	segmentIds: number[],
	horizonsMin: number[] = [5, 10, 15, 30],
): Promise<TrafficPrediction[]> {
	return post(`${TRAFFIC_API}/predict`, {
		segment_ids: segmentIds,
		horizons_min: horizonsMin,
	});
}

/**
 * Fetch recent anomalies detected by the Tier-1/Tier-2 anomaly pipeline.
 */
export async function getRecentAnomalies(
	hours = 1,
	limit = 100,
): Promise<TrafficAnomaly[]> {
	const params = new URLSearchParams({
		hours: hours.toString(),
		limit: limit.toString(),
	});
	const resp = await request<{
		anomalies: Array<{
			anomaly_id: number;
			segment_id: number;
			timestamp_utc: string;
			anomaly_type: string;
			severity: 'low' | 'medium' | 'high';
			details: Record<string, unknown>;
		}>;
	}>(`${TRAFFIC_API.replace('/api/traffic', '/api/v1')}/anomalies/recent?${params}`);
	return resp.anomalies.map((a) => ({
		anomalyId: a.anomaly_id,
		segmentId: a.segment_id,
		timestampUtc: a.timestamp_utc,
		anomalyType: a.anomaly_type,
		severity: a.severity,
		details: a.details,
	}));
}

/**
 * Fetch corridor-wide traffic summary. The additive honesty fields say
 * what the numbers stand on: `provenance` live | emulated | mixed | none
 * (emulated = synthetic signal-ingress rows, data_quality 'synthetic'),
 * the data age and the row quality mix — a consumer must label an
 * emulated summary SIMULATED FEED, never LIVE.
 */
export async function getCorridorSummary(): Promise<{
	totalSegments: number;
	avgSpeedKmh: number;
	avgCongestionIdx: number;
	congestedSegments: number;
	criticalSegments: number;
	status: string;
	provenance: 'live' | 'emulated' | 'mixed' | 'none' | null;
	emulatedSegments: number | null;
	dataAgeSec: number | null;
	qualityMix: { real: number; synthetic: number; anomalousExcluded: number } | null;
}> {
	const resp = await request<{
		total_segments: number;
		avg_speed_kmh: number;
		avg_congestion_idx: number;
		congested_segments: number;
		critical_segments: number;
		status: string;
		provenance?: string;
		emulated_segments?: number;
		data_age_sec?: number | null;
		quality_mix?: { real?: number; synthetic?: number; anomalous_excluded?: number };
	}>('/api/v1/corridor/summary');
	const prov = resp.provenance;
	const mix = resp.quality_mix;
	return {
		totalSegments: resp.total_segments,
		avgSpeedKmh: resp.avg_speed_kmh,
		avgCongestionIdx: resp.avg_congestion_idx,
		congestedSegments: resp.congested_segments,
		criticalSegments: resp.critical_segments,
		status: resp.status,
		provenance: prov === 'live' || prov === 'emulated' || prov === 'mixed' || prov === 'none' ? prov : null,
		emulatedSegments: typeof resp.emulated_segments === 'number' ? resp.emulated_segments : null,
		dataAgeSec: typeof resp.data_age_sec === 'number' ? resp.data_age_sec : null,
		qualityMix: mix
			? { real: mix.real ?? 0, synthetic: mix.synthetic ?? 0, anomalousExcluded: mix.anomalous_excluded ?? 0 }
			: null,
	};
}

/**
 * Fetch 24h observation history for a single segment.
 */
export async function getSegmentHistory(
	segmentId: number,
): Promise<
	Array<{
		timestampUtc: string;
		speedKmh: number;
		congestionIdx: number;
		source: string;
	}>
> {
	const resp = await request<{
		segment_id: number;
		observations: Array<{
			timestamp_utc: string;
			speed_kmh: number;
			congestion_idx: number;
			source: string;
		}>;
	}>(`/api/v1/traffic/history/${segmentId}`);
	return resp.observations.map((o) => ({
		timestampUtc: o.timestamp_utc,
		speedKmh: o.speed_kmh,
		congestionIdx: o.congestion_idx,
		source: o.source,
	}));
}

// ─── LLM Chat (Streaming) ──────────────────────────────────────────────────

/** The brain tier that produced a reasoning step (contract C-D). */
export type ChatTier = 'decider' | 'gemma' | 'heuristic';

export interface ChatThought {
	stepIndex: number;
	text: string;
	/** Null from brains that predate the tier field. */
	tier: ChatTier | null;
}

export interface ChatToolCall {
	callId: string;
	toolName: string;
	arguments: Record<string, unknown>;
	state: 'pending' | 'running' | 'success' | 'error';
}

export interface ChatToolResult {
	callId: string;
	toolName: string | null;
	state: 'success' | 'error';
	/** One-line result summary (C-D); null from older brains. */
	summary: string | null;
	result: unknown;
	/** Server-measured duration when the brain reports one; never guessed. */
	durationMs: number | null;
}

/** A URN the turn's tool results named (done.data.citations, C-D2). */
export interface ChatCitation {
	urn: string;
	/** Plate-masked by the brain; still shown through the console's mask. */
	label: string;
}

/** done.data (C-D) — every field degrades to empty on older brains. */
export interface ChatDone {
	sessionId: string | null;
	/** e.g. "decider:pplx-decider-v1-27b", "gemma:aditi-ps18-v7h-12b", "heuristic". */
	tiersUsed: string[];
	operatorAlerts: string[];
	degraded: boolean;
	/** C-D2 — [] from brains that predate citations. */
	citations: ChatCitation[];
}

/**
 * A canvas directive (C-D2): the agent PROPOSES a camera move / highlight /
 * note / sensor / layer change; a human applies or dismisses it on the
 * God's-eye canvas. Shaped here, validated by gods-eye/directives.ts.
 */
export interface ChatDirective {
	id: string;
	verb: string;
	args: Record<string, unknown>;
	label: string;
	provenance: {
		sessionId: string | null;
		/** decider | gemma | heuristic (the tier that proposed it). */
		tier: string | null;
		confidence: number | null;
		toolCallId: string | null;
	};
}

export interface ChatStreamCallbacks {
	onToken: (token: string) => void;
	onThought: (step: ChatThought) => void;
	onToolCall: (call: ChatToolCall) => void;
	onToolResult: (result: ChatToolResult) => void;
	onDone: (done: ChatDone) => void;
	onError: (error: Error) => void;
	/** C-D2 directive lines (optional: consumers that ignore them omit it). */
	onDirective?: (directive: ChatDirective) => void;
}

export interface ChatStreamOptions {
	/** Continue a brain session: the previous turn's done.data.session_id. */
	sessionId?: string | null;
	/** Abort the stream (console cleared / page left). */
	signal?: AbortSignal;
	/** The selected corridor event (C-D2 event_id): grounds the turn. */
	eventId?: string | null;
	/** The entity a dossier asks about ('<kind>:<id>', C-D2 context_urn). */
	contextUrn?: string | null;
}

const CHAT_TIERS: readonly ChatTier[] = ['decider', 'gemma', 'heuristic'];

function asRecord(v: unknown): Record<string, unknown> {
	return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
}

function asString(v: unknown): string | null {
	return typeof v === 'string' && v.length > 0 ? v : null;
}

function asStrings(v: unknown): string[] {
	return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : [];
}

function asNumber(v: unknown): number | null {
	return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

/** The citations of a done line ([] when absent or malformed). */
function asCitations(v: unknown): ChatCitation[] {
	if (!Array.isArray(v)) return [];
	const out: ChatCitation[] = [];
	for (const row of v) {
		const r = asRecord(row);
		const urn = asString(r.urn);
		if (urn) out.push({ urn, label: asString(r.label) ?? urn });
	}
	return out.slice(0, 24);
}

/** A directive line's data, shaped (null when it carries no id or verb). */
export function parseDirective(data: unknown): ChatDirective | null {
	const d = asRecord(data);
	const id = asString(d.id);
	const verb = asString(d.verb);
	if (!id || !verb) return null;
	const p = asRecord(d.provenance);
	return {
		id,
		verb,
		args: asRecord(d.args),
		label: asString(d.label) ?? verb,
		provenance: {
			sessionId: asString(p.session_id) ?? asString(p.sessionId),
			tier: asString(p.tier),
			confidence: asNumber(p.confidence),
			toolCallId: asString(p.tool_call_id) ?? asString(p.toolCallId),
		},
	};
}

/**
 * Apply one parsed NDJSON event to the callbacks. Accepts BOTH wire
 * shapes: contract C-D ({id, name, args} / {id, name, ok, summary},
 * thought.tier, done.data) and the legacy brain's ({callId, toolName,
 * arguments, state} / {callId, state, result, durationMs}, bare done).
 * A `directive` (C-D2) goes to the optional onDirective and is NOT
 * terminal; other unknown types are ignored.
 * Returns 'end' once a terminal event (done | error) was dispatched.
 */
export function dispatchChatEvent(event: unknown, callbacks: ChatStreamCallbacks): 'end' | null {
	const ev = asRecord(event);
	const data = ev.data;
	switch (ev.type) {
		case 'token': {
			const text = typeof data === 'string' ? data : data == null ? '' : JSON.stringify(data);
			if (text) callbacks.onToken(text);
			return null;
		}
		case 'thought': {
			const d = asRecord(data);
			const tier = typeof d.tier === 'string' && (CHAT_TIERS as readonly string[]).includes(d.tier)
				? (d.tier as ChatTier)
				: null;
			callbacks.onThought({
				stepIndex: typeof d.stepIndex === 'number' ? d.stepIndex : Number(d.step_index ?? 0),
				text: asString(d.text) ?? '',
				tier,
			});
			return null;
		}
		case 'tool_call': {
			const d = asRecord(data);
			const state = d.state === 'pending' || d.state === 'success' || d.state === 'error' ? d.state : 'running';
			callbacks.onToolCall({
				callId: asString(d.id) ?? asString(d.callId) ?? asString(d.call_id) ?? 'call',
				toolName: asString(d.name) ?? asString(d.toolName) ?? asString(d.tool_name) ?? 'tool',
				arguments: asRecord(d.args ?? d.arguments),
				state,
			});
			return null;
		}
		case 'tool_result': {
			const d = asRecord(data);
			const ok = typeof d.ok === 'boolean' ? d.ok : d.state !== 'error';
			const ms = d.durationMs ?? d.duration_ms;
			callbacks.onToolResult({
				callId: asString(d.id) ?? asString(d.callId) ?? asString(d.call_id) ?? 'call',
				toolName: asString(d.name) ?? asString(d.toolName) ?? null,
				state: ok ? 'success' : 'error',
				summary: asString(d.summary),
				result: d.result,
				durationMs: typeof ms === 'number' && Number.isFinite(ms) ? ms : null,
			});
			return null;
		}
		case 'directive': {
			const directive = parseDirective(data);
			if (directive) callbacks.onDirective?.(directive);
			return null;
		}
		case 'done': {
			const d = asRecord(data);
			callbacks.onDone({
				sessionId: asString(d.session_id) ?? asString(d.sessionId),
				tiersUsed: asStrings(d.tiers_used ?? d.tiersUsed),
				operatorAlerts: asStrings(d.operator_alerts ?? d.operatorAlerts),
				degraded: d.degraded === true,
				citations: asCitations(d.citations),
			});
			return 'end';
		}
		case 'error':
			callbacks.onError(new Error(typeof data === 'string' ? data : 'agent stream error'));
			return 'end';
		default:
			return null;
	}
}

/**
 * Stream a chat message to the convoy-brain agent (POST /chat/stream,
 * contract C-D). Reads newline-delimited JSON events from the response
 * stream and dispatches them as they arrive; the session id from
 * done.data feeds the next turn (follow-ups keep their history), and the
 * selected event's movement id + protection class scope the brain's
 * movement tools; `eventId` / `contextUrn` (C-D2) ground the turn in the
 * selected event and in the entity a dossier asks about. Rejects only when
 * fetch itself fails.
 */
export async function streamChat(
	message: string,
	movementId: string | null,
	vvipClass: VvipClass | null,
	callbacks: ChatStreamCallbacks,
	options: ChatStreamOptions = {},
): Promise<void> {
	const payload: Record<string, unknown> = {
		message,
		movement_id: movementId,
		vvip_class: vvipClass,
	};
	if (options.sessionId) payload.session_id = options.sessionId;
	if (options.eventId) payload.event_id = options.eventId;
	if (options.contextUrn) payload.context_urn = options.contextUrn;

	const res = await fetch(`${CONVOY_API}/chat/stream`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(payload),
		signal: options.signal,
	});

	if (!res.ok || !res.body) {
		const detail = await res.text().catch(() => '');
		callbacks.onError(
			new ApiError(res.status, `Chat stream failed: ${res.status} ${res.statusText} ${detail.slice(0, 160)}`.trim()),
		);
		return;
	}

	const reader = res.body.getReader();
	const decoder = new TextDecoder();
	let buffer = '';
	let ended = false;

	const consume = (line: string) => {
		const trimmed = line.trim();
		if (!trimmed || ended) return;
		let event: unknown;
		try {
			event = JSON.parse(trimmed);
		} catch {
			return; // malformed line — skipped, the stream continues
		}
		if (dispatchChatEvent(event, callbacks) === 'end') ended = true;
	};

	try {
		while (!ended) {
			const { done, value } = await reader.read();
			if (done) break;
			buffer += decoder.decode(value, { stream: true });
			const lines = buffer.split('\n');
			buffer = lines.pop() ?? '';
			for (const line of lines) consume(line);
		}
		// A final line without a trailing newline is still an event.
		buffer += decoder.decode();
		consume(buffer);
		if (!ended) {
			callbacks.onDone({ sessionId: null, tiersUsed: [], operatorAlerts: [], degraded: false, citations: [] });
		}
	} catch (err) {
		if (!ended) callbacks.onError(err instanceof Error ? err : new Error(String(err)));
	} finally {
		reader.cancel().catch(() => undefined);
	}
}

/** The brain's answer to a directive acknowledgement. */
export interface DirectiveAckResult {
	ok: boolean;
	status: number;
	/** Where the brain recorded it ('decisions_log' | 'brain log'), or the error detail. */
	detail: string | null;
}

/**
 * POST /chat/directives/ack (C-D2): the operator applied or dismissed a
 * canvas directive. The deck proxy adds the verified officer header; the
 * body officer is the console's named duty officer (a claim, when they
 * differ). Never throws — the canvas has already done what the human chose.
 */
export async function ackDirective(
	sessionId: string,
	directiveId: string,
	decision: 'applied' | 'dismissed',
	officer?: string | null,
): Promise<DirectiveAckResult> {
	const body: Record<string, unknown> = { session_id: sessionId, directive_id: directiveId, decision };
	const who = officer?.trim();
	if (who && who.length >= 2) body.officer = who.slice(0, 64);
	try {
		const res = await fetch(`${CONVOY_API}/chat/directives/ack`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify(body),
			signal: AbortSignal.timeout(8000),
		});
		const payload = (await res.json().catch(() => ({}))) as Record<string, unknown>;
		const detail = asString(payload.recorded_in) ?? asString(payload.detail);
		return { ok: res.ok, status: res.status, detail };
	} catch (err) {
		return { ok: false, status: 0, detail: err instanceof Error ? err.message : String(err) };
	}
}

/**
 * Non-streaming chat fallback.
 */
export async function sendChat(
	message: string,
	movementId: string | null,
	vvipClass: VvipClass | null,
): Promise<{ sessionId: string; response: string }> {
	const resp = await post<{ session_id: string; response: string }>(`${CONVOY_API}/chat`, {
		message,
		movement_id: movementId,
		vvip_class: vvipClass,
	});
	return { sessionId: resp.session_id, response: resp.response };
}

// ─── WebSocket Connection ───────────────────────────────────────────────────

export type WsEventHandler = (event: WsEvent) => void;

export interface ConvoySocket {
	close: () => void;
	sendPositionUpdate: (pos: {
		lon: number;
		lat: number;
		speedKmh: number;
		headingDeg: number;
	}) => void;
}

/**
 * Managed WebSocket connection with auto-reconnect and position update sender.
 */
export function createConvoySocket(
	movementId: string,
	onEvent: WsEventHandler,
): ConvoySocket {
	let ws: WebSocket | null = null;
	let closed = false;
	let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
	let reconnectDelay = 1000;

	function connect() {
		if (closed) return;

		const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
		const host = window.location.host;
		ws = new WebSocket(`${protocol}//${host}/api/convoy/ws/convoy/${movementId}`);

		ws.onopen = () => {
			reconnectDelay = 1000;
		};

		ws.onmessage = (event) => {
			try {
				const data = JSON.parse(event.data) as WsEvent;
				onEvent(data);
			} catch {
				// Ignore malformed messages
			}
		};

		ws.onclose = () => {
			if (!closed) {
				reconnectTimer = setTimeout(() => {
					reconnectDelay = Math.min(reconnectDelay * 2, 30000);
					connect();
				}, reconnectDelay);
			}
		};

		ws.onerror = () => {
			ws?.close();
		};
	}

	connect();

	return {
		close() {
			closed = true;
			if (reconnectTimer) clearTimeout(reconnectTimer);
			ws?.close();
		},
		sendPositionUpdate(pos) {
			if (ws?.readyState === WebSocket.OPEN) {
				ws.send(
					JSON.stringify({
						type: 'position.update',
						payload: {
							lon: pos.lon,
							lat: pos.lat,
							speedKmh: pos.speedKmh,
							headingDeg: pos.headingDeg,
						},
					}),
				);
			}
		},
	};
}

// ─── Health Checks ──────────────────────────────────────────────────────────

export async function checkConvoyBrainHealth(): Promise<{
	status: string;
	ollama: unknown;
}> {
	return request(`${CONVOY_API}/health`);
}

export async function checkServiceHealth(): Promise<ServiceHealth[]> {
	return request(`${CONVOY_API}/health/services`);
}

export async function getGpuStatus(): Promise<GpuStatus> {
	return request(`${CONVOY_API}/health/gpu`);
}
