/**
 * Data service — bridges API client to Svelte stores.
 *
 * Manages periodic polling, viewport-based data fetching, and real-time
 * updates. Intended to be started once from the root layout and torn
 * down on app destruction.
 */

import {
	getTrafficSnapshot,
	getRoadSegments,
	getTrafficPredictions,
	getRecentAnomalies,
	checkConvoyBrainHealth,
	checkServiceHealth,
	getGpuStatus,
} from './client';
import {
	updateTrafficBatch,
	loadRoadNetwork,
	setPredictions,
	anomalies,
} from '$stores/traffic';
import {
	services,
	updateServiceStatus,
	updateGpuStatus,
} from '$stores/health';
import {
	trafficFeed,
	healthFeed,
	anomalyFeed,
	predictionFeed,
	roadNetworkFeed,
	markLoading,
	markSuccess,
	markError,
} from '$stores/connection';
import type { TrafficAnomaly } from '$lib/types';

// ─── Viewport state ─────────────────────────────────────────────────────────

let currentBbox: { minLon: number; minLat: number; maxLon: number; maxLat: number } | null = null;
let visibleSegmentIds: number[] = [];

// ─── Interval handles ───────────────────────────────────────────────────────

let trafficInterval: ReturnType<typeof setInterval> | null = null;
let anomalyInterval: ReturnType<typeof setInterval> | null = null;
let healthInterval: ReturnType<typeof setInterval> | null = null;
let predictionInterval: ReturnType<typeof setInterval> | null = null;

// ─── Public API ─────────────────────────────────────────────────────────────

/**
 * Initialize all data polling loops. Call once from root layout onMount.
 */
export function startDataService(): void {
	// Initial fetches
	fetchHealth();
	fetchServiceHealth();

	// Health: every 10 seconds
	healthInterval = setInterval(() => {
		fetchHealth();
		fetchServiceHealth();
	}, 10_000);

	// Anomalies: every 15 seconds
	fetchAnomalies();
	anomalyInterval = setInterval(fetchAnomalies, 15_000);
}

/**
 * Stop all data polling. Call from root layout onDestroy.
 */
export function stopDataService(): void {
	if (trafficInterval) clearInterval(trafficInterval);
	if (anomalyInterval) clearInterval(anomalyInterval);
	if (healthInterval) clearInterval(healthInterval);
	if (predictionInterval) clearInterval(predictionInterval);
	trafficInterval = null;
	anomalyInterval = null;
	healthInterval = null;
	predictionInterval = null;
}

/**
 * Update the map viewport bounding box. Triggers an immediate data fetch
 * and restarts the traffic & prediction polls for the new area.
 */
export function updateViewport(bbox: {
	minLon: number;
	minLat: number;
	maxLon: number;
	maxLat: number;
}): void {
	currentBbox = bbox;

	// Fetch road network once per viewport change
	fetchRoadSegments(bbox);

	// Fetch traffic immediately and restart polling
	fetchTrafficSnapshot(bbox);
	if (trafficInterval) clearInterval(trafficInterval);
	trafficInterval = setInterval(() => {
		if (currentBbox) fetchTrafficSnapshot(currentBbox);
	}, 10_000);

	// Predictions: every 30 seconds for visible segments
	if (predictionInterval) clearInterval(predictionInterval);
	predictionInterval = setInterval(fetchPredictions, 30_000);
}

// ─── Internal fetchers ──────────────────────────────────────────────────────

async function fetchRoadSegments(bbox: {
	minLon: number;
	minLat: number;
	maxLon: number;
	maxLat: number;
}): Promise<void> {
	markLoading(roadNetworkFeed);
	try {
		const segments = await getRoadSegments(bbox);
		loadRoadNetwork(segments);
		markSuccess(roadNetworkFeed);
	} catch (err) {
		const msg = err instanceof Error ? err.message : 'Unknown error';
		console.error('[data-service] Failed to fetch road segments:', err);
		markError(roadNetworkFeed, msg);
	}
}

async function fetchTrafficSnapshot(bbox: {
	minLon: number;
	minLat: number;
	maxLon: number;
	maxLat: number;
}): Promise<void> {
	markLoading(trafficFeed);
	try {
		const snapshot = await getTrafficSnapshot(bbox);
		updateTrafficBatch(snapshot);
		visibleSegmentIds = snapshot.map((s) => s.segmentId);
		markSuccess(trafficFeed);
	} catch (err) {
		const msg = err instanceof Error ? err.message : 'Unknown error';
		console.error('[data-service] Failed to fetch traffic snapshot:', err);
		markError(trafficFeed, msg);
	}
}

async function fetchPredictions(): Promise<void> {
	if (visibleSegmentIds.length === 0) return;
	// Sample up to 50 segments for prediction to limit request size
	const sample = visibleSegmentIds.slice(0, 50);
	markLoading(predictionFeed);
	try {
		const preds = await getTrafficPredictions(sample);
		setPredictions(preds);
		markSuccess(predictionFeed);
	} catch (err) {
		const msg = err instanceof Error ? err.message : 'Unknown error';
		console.error('[data-service] Failed to fetch predictions:', err);
		markError(predictionFeed, msg);
	}
}

async function fetchAnomalies(): Promise<void> {
	markLoading(anomalyFeed);
	try {
		const recent = await getRecentAnomalies(1, 100);
		// Replace entire anomaly list with fresh data
		anomalies.set(recent);
		markSuccess(anomalyFeed);
	} catch (err) {
		const msg = err instanceof Error ? err.message : 'Unknown error';
		console.error('[data-service] Failed to fetch anomalies:', err);
		markError(anomalyFeed, msg);
	}
}

async function fetchHealth(): Promise<void> {
	markLoading(healthFeed);
	try {
		const health = await checkConvoyBrainHealth();
		updateServiceStatus('convoy-brain', 'online');
		if (health.ollama) {
			updateServiceStatus('Ollama (Qwen 3.5 9B)', 'online');
		} else {
			updateServiceStatus('Ollama (Qwen 3.5 9B)', 'degraded');
		}
		markSuccess(healthFeed);
	} catch {
		updateServiceStatus('convoy-brain', 'offline');
		markError(healthFeed, 'convoy-brain unreachable');
	}

	try {
		const gpu = await getGpuStatus();
		updateGpuStatus(gpu);
	} catch {
		// GPU status unavailable
	}
}

async function fetchServiceHealth(): Promise<void> {
	try {
		const svcList = await checkServiceHealth();
		for (const svc of svcList) {
			updateServiceStatus(svc.name, svc.status, svc.latencyMs ?? undefined);
		}
	} catch {
		// Service health endpoint unavailable
	}
}
