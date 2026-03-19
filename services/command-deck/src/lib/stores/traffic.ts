/**
 * Traffic state store — reactive state for corridor traffic conditions.
 * Populated from traffic-oracle REST API and real-time WebSocket snapshots.
 */

import { writable, derived } from 'svelte/store';
import type { SegmentTraffic, RoadSegment, TrafficPrediction, TrafficAnomaly } from '$lib/types';

// ─── Core Stores ────────────────────────────────────────────────────────────

/** Live traffic observations keyed by segment ID */
export const liveTraffic = writable<Map<number, SegmentTraffic>>(new Map());

/** Road network geometry for rendering */
export const roadSegments = writable<Map<number, RoadSegment>>(new Map());

/** Predicted traffic at future horizons */
export const predictions = writable<TrafficPrediction[]>([]);

/** Recent anomalies detected by the pipeline */
export const anomalies = writable<TrafficAnomaly[]>([]);

// ─── Display Controls ───────────────────────────────────────────────────────

export const heatmapVisible = writable(true);
export const heatmapOpacity = writable(0.6);
export const roadNetworkVisible = writable(true);
export const anomalyMarkersVisible = writable(true);
export const predictionHorizonMin = writable(5);

// ─── Derived Stores ─────────────────────────────────────────────────────────

/** Traffic data as a flat array for deck.gl layer consumption */
export const trafficArray = derived(liveTraffic, ($traffic) =>
	Array.from($traffic.values()),
);

/** Road segments as a flat array for deck.gl path layer */
export const roadSegmentArray = derived(roadSegments, ($segments) =>
	Array.from($segments.values()),
);

/** Corridor-wide average congestion */
export const corridorAvgCongestion = derived(liveTraffic, ($traffic) => {
	const entries = Array.from($traffic.values());
	if (entries.length === 0) return 0;
	return entries.reduce((sum, s) => sum + s.congestionIdx, 0) / entries.length;
});

/** Count of high-congestion segments (idx > 0.7) */
export const congestedSegmentCount = derived(liveTraffic, ($traffic) => {
	return Array.from($traffic.values()).filter((s) => s.congestionIdx > 0.7).length;
});

/** High-severity anomalies for alert display */
export const criticalAnomalies = derived(anomalies, ($a) =>
	$a.filter((a) => a.severity === 'high'),
);

// ─── Actions ────────────────────────────────────────────────────────────────

export function updateTrafficBatch(batch: SegmentTraffic[]): void {
	liveTraffic.update((map) => {
		const next = new Map(map);
		for (const seg of batch) {
			next.set(seg.segmentId, seg);
		}
		return next;
	});
}

export function loadRoadNetwork(segments: RoadSegment[]): void {
	roadSegments.set(new Map(segments.map((s) => [s.segmentId, s])));
}

export function setPredictions(preds: TrafficPrediction[]): void {
	predictions.set(preds);
}

export function addAnomaly(anomaly: TrafficAnomaly): void {
	anomalies.update((list) => [anomaly, ...list].slice(0, 200));
}
