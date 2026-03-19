/**
 * Domain type definitions for the VVIP Command Deck.
 * Mirrors backend models from convoy-brain, traffic-oracle, and signal-ingress.
 */

// ─── VVIP Security Classifications ──────────────────────────────────────────

export type VvipClass = 'Z+' | 'Z' | 'Y' | 'X';

export type MovementStatus =
	| 'planning'
	| 'approved'
	| 'active'
	| 'completed'
	| 'cancelled';

export type DiversionType =
	| 'full_closure'
	| 'partial_closure'
	| 'speed_restriction'
	| 'signal_override';

export type DiversionStatus = 'pending' | 'active' | 'completed';

// ─── Convoy Domain ──────────────────────────────────────────────────────────

export interface ConvoyMovement {
	movementId: string;
	vvipClass: VvipClass;
	status: MovementStatus;
	origin: [number, number]; // [lon, lat]
	destination: [number, number];
	plannedDeparture: string; // ISO 8601
	position: [number, number] | null;
	speedKmh: number;
	headingDeg: number;
	selectedRouteId: string | null;
	activeDiversions: string[];
	etaSeconds: number | null;
}

export interface RouteCandidate {
	routeId: string;
	segmentIds: number[];
	geometry: [number, number][]; // [[lon,lat], ...] polyline
	totalDistanceM: number;
	estimatedTimeSec: number;
	disruptionScore: number;
	securityScore: number;
	compositeScore: number;
}

export interface DiversionEntry {
	diversionId: string;
	segmentId: number;
	segmentName: string;
	diversionType: DiversionType;
	activateAt: string;
	deactivateAt: string;
	status: DiversionStatus;
	queueLengthM: number;
	alternateRoute: [number, number][] | null;
}

// ─── Traffic Domain ─────────────────────────────────────────────────────────

export interface SegmentTraffic {
	segmentId: number;
	speedKmh: number;
	congestionIdx: number;
	lastUpdated: number; // epoch ms
}

export interface RoadSegment {
	segmentId: number;
	osmWayId: number;
	roadName: string;
	roadClass: string;
	lanes: number;
	speedLimitKmh: number;
	oneway: boolean;
	geometry: [number, number][]; // [[lon,lat], ...]
}

export interface TrafficPrediction {
	segmentId: number;
	horizonMin: number;
	predictedSpeedKmh: number;
	predictedCongestionIdx: number;
	confidence: number;
}

export interface TrafficAnomaly {
	anomalyId: number;
	segmentId: number;
	timestampUtc: string;
	anomalyType: string;
	severity: 'low' | 'medium' | 'high';
	details: Record<string, unknown>;
}

// ─── LLM Chat Domain ───────────────────────────────────────────────────────

export type ChatRole = 'user' | 'assistant' | 'system';

export interface ChatMessage {
	id: string;
	role: ChatRole;
	content: string;
	timestamp: number;
	thoughts: ThoughtStep[];
	toolCalls: ToolCallStatus[];
	isStreaming: boolean;
}

export interface ThoughtStep {
	stepIndex: number;
	text: string;
	timestamp: number;
}

export type ToolCallState = 'pending' | 'running' | 'success' | 'error';

export interface ToolCallStatus {
	callId: string;
	toolName: string;
	arguments: Record<string, unknown>;
	state: ToolCallState;
	result: unknown | null;
	durationMs: number | null;
	startedAt: number;
}

// ─── Simulation Domain ──────────────────────────────────────────────────────

export interface SimulationConfig {
	corridorSegmentIds: number[];
	convoyRoute: [number, number][];
	vehicleDensity: number; // vehicles per km
	timeStepSec: number; // simulation dt
	totalDurationSec: number;
	convoySpeedKmh: number;
	diversionPlan: DiversionEntry[];
}

export interface SimulationState {
	running: boolean;
	currentTimeSec: number;
	totalVehicles: number;
	avgSpeedKmh: number;
	avgDelayPerVehicleSec: number;
	queueLengthM: number;
	frameCount: number;
	fps: number;
}

/** Per-vehicle state packed for GPU (32 bytes per vehicle) */
export interface VehicleGPULayout {
	// float32 x 8 = 32 bytes per vehicle
	// [posX, posY, velX, velY, accelX, accelY, laneOffset, flags]
	byteStride: 32;
}

// ─── System Health ──────────────────────────────────────────────────────────

export type ServiceStatus = 'online' | 'degraded' | 'offline';

export interface ServiceHealth {
	name: string;
	status: ServiceStatus;
	latencyMs: number | null;
	lastChecked: number;
	details: Record<string, unknown>;
}

export interface GpuStatus {
	vramTotalMb: number;
	vramUsedMb: number;
	vramFreeMb: number;
	gpuUtilPercent: number;
	temperature: number;
	allocations: {
		ollamaQwen: number;
		onnxDstgat: number;
		cudaOverhead: number;
		headroom: number;
	};
}

// ─── WebSocket Events ───────────────────────────────────────────────────────

export type WsEventType =
	| 'convoy.position'
	| 'convoy.status'
	| 'diversion.activated'
	| 'diversion.deactivated'
	| 'traffic.snapshot'
	| 'agent.thought'
	| 'agent.tool_call'
	| 'agent.response';

export interface WsEvent<T = unknown> {
	type: WsEventType;
	timestamp: number;
	payload: T;
}
