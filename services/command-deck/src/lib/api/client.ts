/**
 * API client for convoy-brain and traffic-oracle backend services.
 *
 * Provides REST, WebSocket, and streaming interfaces for:
 * - Convoy movement lifecycle (plan, approve, launch, abort)
 * - Traffic data queries and real-time snapshots
 * - LLM chat with thought/tool streaming
 * - Service health checks
 *
 * Convention: Backend uses snake_case. Frontend uses camelCase.
 * All conversion happens in this module — callers always use camelCase.
 */

import type {
	ConvoyMovement,
	RouteCandidate,
	DiversionEntry,
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
	constructor(
		public status: number,
		message: string,
	) {
		super(message);
		this.name = 'ApiError';
	}
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
	const res = await fetch(url, init);
	if (!res.ok) {
		const body = await res.text().catch(() => 'Unknown error');
		throw new ApiError(res.status, `${res.status} ${res.statusText}: ${body}`);
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
}): Promise<{ movementId: string }> {
	const resp = await post<{ movement_id: string }>(`${CONVOY_API}/movements`, {
		origin: params.origin,
		destination: params.destination,
		vvip_class: params.vvipClass,
		planned_departure: params.plannedDeparture,
	});
	return { movementId: resp.movement_id };
}

export async function planMovement(
	movementId: string,
	params: {
		origin: [number, number];
		destination: [number, number];
		vvipClass: VvipClass;
		plannedDeparture: string;
	},
): Promise<{
	routes: RouteCandidate[];
	diversions: DiversionEntry[];
	recommendation: string;
}> {
	return post(`${CONVOY_API}/movements/${movementId}/plan`, {
		origin: params.origin,
		destination: params.destination,
		vvip_class: params.vvipClass,
		planned_departure: params.plannedDeparture,
	});
}

export async function launchEscort(
	movementId: string,
	destination?: [number, number],
): Promise<unknown> {
	return post(`${CONVOY_API}/movements/${movementId}/escort`, { destination });
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
 * Fetch corridor-wide traffic summary.
 */
export async function getCorridorSummary(): Promise<{
	totalSegments: number;
	avgSpeedKmh: number;
	avgCongestionIdx: number;
	congestedSegments: number;
	criticalSegments: number;
	status: 'green' | 'amber' | 'red';
}> {
	const resp = await request<{
		total_segments: number;
		avg_speed_kmh: number;
		avg_congestion_idx: number;
		congested_segments: number;
		critical_segments: number;
		status: 'green' | 'amber' | 'red';
	}>('/api/v1/corridor/summary');
	return {
		totalSegments: resp.total_segments,
		avgSpeedKmh: resp.avg_speed_kmh,
		avgCongestionIdx: resp.avg_congestion_idx,
		congestedSegments: resp.congested_segments,
		criticalSegments: resp.critical_segments,
		status: resp.status,
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

export interface ChatStreamCallbacks {
	onToken: (token: string) => void;
	onThought: (step: { stepIndex: number; text: string }) => void;
	onToolCall: (call: {
		callId: string;
		toolName: string;
		arguments: Record<string, unknown>;
		state: 'pending' | 'running' | 'success' | 'error';
	}) => void;
	onToolResult: (result: {
		callId: string;
		state: 'success' | 'error';
		result: unknown;
		durationMs: number;
	}) => void;
	onDone: () => void;
	onError: (error: Error) => void;
}

/**
 * Stream a chat message to the convoy-brain LLM agent.
 * Reads newline-delimited JSON events from the response stream.
 */
export async function streamChat(
	message: string,
	movementId: string | null,
	vvipClass: VvipClass | null,
	callbacks: ChatStreamCallbacks,
): Promise<void> {
	const res = await fetch(`${CONVOY_API}/chat/stream`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({
			message,
			movement_id: movementId,
			vvip_class: vvipClass,
		}),
	});

	if (!res.ok || !res.body) {
		callbacks.onError(new ApiError(res.status, `Chat stream failed: ${res.statusText}`));
		return;
	}

	const reader = res.body.getReader();
	const decoder = new TextDecoder();
	let buffer = '';

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
					const event = JSON.parse(trimmed);
					switch (event.type) {
						case 'token':
							callbacks.onToken(event.data);
							break;
						case 'thought':
							callbacks.onThought(event.data);
							break;
						case 'tool_call':
							callbacks.onToolCall(event.data);
							break;
						case 'tool_result':
							callbacks.onToolResult(event.data);
							break;
						case 'done':
							callbacks.onDone();
							return;
						case 'error':
							callbacks.onError(new Error(event.data));
							return;
					}
				} catch {
					// Partial JSON line — accumulate more
				}
			}
		}
		callbacks.onDone();
	} catch (err) {
		callbacks.onError(err instanceof Error ? err : new Error(String(err)));
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
