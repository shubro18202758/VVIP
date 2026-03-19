/**
 * Simulation state store — manages WebGPU microscopic traffic simulation.
 * The simulation runs GPU compute shaders for vehicle kinematics and
 * provides state for the render pipeline.
 */

import { writable, derived } from 'svelte/store';
import type { SimulationConfig, SimulationState } from '$lib/types';

// ─── Core Stores ────────────────────────────────────────────────────────────

export const simulationConfig = writable<SimulationConfig | null>(null);

export const simulationState = writable<SimulationState>({
	running: false,
	currentTimeSec: 0,
	totalVehicles: 0,
	avgSpeedKmh: 0,
	avgDelayPerVehicleSec: 0,
	queueLengthM: 0,
	frameCount: 0,
	fps: 0,
});

export const webgpuAvailable = writable(false);
export const gpuDeviceInfo = writable<string>('');

// ─── Display Controls ───────────────────────────────────────────────────────

export const simulationSpeed = writable(1.0); // 1x real-time
export const showVehicles = writable(true);
export const showQueues = writable(true);
export const showConvoyPath = writable(true);
export const vehicleColorMode = writable<'speed' | 'delay' | 'type'>('speed');

// ─── Derived Stores ─────────────────────────────────────────────────────────

export const isRunning = derived(simulationState, ($s) => $s.running);

export const simulationProgress = derived(
	[simulationState, simulationConfig],
	([$state, $config]) => {
		if (!$config || $config.totalDurationSec === 0) return 0;
		return Math.min(1, $state.currentTimeSec / $config.totalDurationSec);
	},
);

export const formattedTime = derived(simulationState, ($s) => {
	const min = Math.floor($s.currentTimeSec / 60);
	const sec = Math.floor($s.currentTimeSec % 60);
	return `${min}:${sec.toString().padStart(2, '0')}`;
});

// ─── Actions ────────────────────────────────────────────────────────────────

export function updateSimulationState(update: Partial<SimulationState>): void {
	simulationState.update((s) => ({ ...s, ...update }));
}

export function resetSimulation(): void {
	simulationState.set({
		running: false,
		currentTimeSec: 0,
		totalVehicles: 0,
		avgSpeedKmh: 0,
		avgDelayPerVehicleSec: 0,
		queueLengthM: 0,
		frameCount: 0,
		fps: 0,
	});
}
