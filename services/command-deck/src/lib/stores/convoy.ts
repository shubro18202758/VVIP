/**
 * Convoy state store — reactive state for active convoy movements.
 * Updated via WebSocket from convoy-brain service.
 */

import { writable, derived, get } from 'svelte/store';
import type {
	ConvoyMovement,
	RouteCandidate,
	DiversionEntry,
	MovementStatus,
} from '$lib/types';

// ─── Core Stores ────────────────────────────────────────────────────────────

export const activeConvoy = writable<ConvoyMovement | null>(null);
export const convoyHistory = writable<ConvoyMovement[]>([]);
export const routeCandidates = writable<RouteCandidate[]>([]);
export const selectedRouteId = writable<string | null>(null);
export const diversions = writable<DiversionEntry[]>([]);

// ─── Derived Stores ─────────────────────────────────────────────────────────

export const selectedRoute = derived(
	[routeCandidates, selectedRouteId],
	([$routes, $id]) => $routes.find((r) => r.routeId === $id) ?? null,
);

export const activeDiversions = derived(diversions, ($d) =>
	$d.filter((d) => d.status === 'active'),
);

export const convoyIsActive = derived(
	activeConvoy,
	($c) => $c !== null && ($c.status === 'active' || $c.status === 'approved'),
);

// ─── Actions ────────────────────────────────────────────────────────────────

export function updateConvoyPosition(
	lon: number,
	lat: number,
	speedKmh: number,
	headingDeg: number,
): void {
	activeConvoy.update((c) => {
		if (!c) return c;
		return { ...c, position: [lon, lat], speedKmh, headingDeg };
	});
}

export function updateConvoyStatus(status: MovementStatus): void {
	activeConvoy.update((c) => {
		if (!c) return c;
		return { ...c, status };
	});
}

export function setRoutes(routes: RouteCandidate[]): void {
	routeCandidates.set(routes);
	if (routes.length > 0) {
		selectedRouteId.set(routes[0].routeId);
	}
}

export function selectRoute(routeId: string): void {
	selectedRouteId.set(routeId);
	activeConvoy.update((c) => {
		if (!c) return c;
		return { ...c, selectedRouteId: routeId };
	});
}

export function updateDiversion(diversionId: string, update: Partial<DiversionEntry>): void {
	diversions.update((list) =>
		list.map((d) => (d.diversionId === diversionId ? { ...d, ...update } : d)),
	);
}

export function clearConvoy(): void {
	const current = get(activeConvoy);
	if (current) {
		convoyHistory.update((h) => [current, ...h].slice(0, 50));
	}
	activeConvoy.set(null);
	routeCandidates.set([]);
	selectedRouteId.set(null);
	diversions.set([]);
}
