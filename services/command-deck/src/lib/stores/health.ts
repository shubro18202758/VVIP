/**
 * System health store — tracks service status and GPU resource utilization.
 */

import { writable, derived } from 'svelte/store';
import type { ServiceHealth, GpuStatus, ServiceStatus } from '$lib/types';

// ─── Core Stores ────────────────────────────────────────────────────────────

export const services = writable<ServiceHealth[]>([
	{ name: 'signal-ingress', status: 'offline', latencyMs: null, lastChecked: 0, details: {} },
	{ name: 'corridor-store', status: 'offline', latencyMs: null, lastChecked: 0, details: {} },
	{ name: 'traffic-oracle', status: 'offline', latencyMs: null, lastChecked: 0, details: {} },
	{ name: 'convoy-brain', status: 'offline', latencyMs: null, lastChecked: 0, details: {} },
	{ name: 'Ollama (Qwen 3.5 9B)', status: 'offline', latencyMs: null, lastChecked: 0, details: {} },
]);

export const gpuStatus = writable<GpuStatus>({
	vramTotalMb: 8192,
	vramUsedMb: 0,
	vramFreeMb: 8192,
	gpuUtilPercent: 0,
	temperature: 0,
	allocations: {
		ollamaQwen: 5632,
		onnxDstgat: 409,
		cudaOverhead: 307,
		headroom: 1844,
	},
});

// ─── Derived Stores ─────────────────────────────────────────────────────────

export const allServicesOnline = derived(services, ($s) =>
	$s.every((svc) => svc.status === 'online'),
);

export const offlineServices = derived(services, ($s) =>
	$s.filter((svc) => svc.status === 'offline'),
);

export const vramUsagePercent = derived(gpuStatus, ($g) =>
	$g.vramTotalMb > 0 ? ($g.vramUsedMb / $g.vramTotalMb) * 100 : 0,
);

// ─── Actions ────────────────────────────────────────────────────────────────

export function updateServiceStatus(
	name: string,
	status: ServiceStatus,
	latencyMs?: number,
): void {
	services.update((list) =>
		list.map((s) =>
			s.name === name
				? { ...s, status, latencyMs: latencyMs ?? s.latencyMs, lastChecked: Date.now() }
				: s,
		),
	);
}

export function updateGpuStatus(update: Partial<GpuStatus>): void {
	gpuStatus.update((g) => ({ ...g, ...update }));
}
