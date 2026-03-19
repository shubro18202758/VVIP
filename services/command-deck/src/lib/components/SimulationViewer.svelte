<!-- SimulationViewer.svelte — WebGPU microscopic traffic simulation overlay -->
<!-- Renders simulated vehicles on top of the corridor map -->

<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import {
		simulationState,
		simulationConfig,
		simulationSpeed,
		isRunning,
		formattedTime,
		simulationProgress,
		showVehicles,
		showQueues,
		showConvoyPath,
		vehicleColorMode,
		webgpuAvailable,
		gpuDeviceInfo,
		updateSimulationState,
		resetSimulation,
	} from '$stores/simulation';

	let canvas: HTMLCanvasElement;
	let overlayVisible = $state(true);
	let fpsDisplay = $derived($simulationState.fps);

	let engineReady = $state(false);
	let errorMessage = $state('');

	// Simulation engine references (lazily initialized)
	let simEngine: Awaited<ReturnType<typeof import('$lib/simulation/engine')>>['TrafficSimulation'] extends new (...args: infer P) => infer R ? R : never;

	onMount(async () => {
		// Check WebGPU availability
		if (!navigator.gpu) {
			errorMessage = 'WebGPU not available in this browser. Use Chrome 113+ or Edge 113+.';
			webgpuAvailable.set(false);
			return;
		}

		try {
			const adapter = await navigator.gpu.requestAdapter({
				powerPreference: 'low-power',
			});

			if (!adapter) {
				// Fallback to high-performance (discrete GPU) with tight memory limits
				const fallback = await navigator.gpu.requestAdapter({
					powerPreference: 'high-performance',
				});
				if (!fallback) {
					errorMessage = 'No WebGPU adapter available.';
					return;
				}
				gpuDeviceInfo.set(`${(await fallback.requestAdapterInfo()).description} (discrete, memory-capped)`);
			} else {
				const info = await adapter.requestAdapterInfo();
				gpuDeviceInfo.set(info.description || 'Integrated GPU');
			}

			webgpuAvailable.set(true);
			engineReady = true;
		} catch (err) {
			errorMessage = `WebGPU init failed: ${err instanceof Error ? err.message : String(err)}`;
		}
	});

	onDestroy(() => {
		simEngine?.destroy?.();
	});

	function handleStart() {
		updateSimulationState({ running: true });
	}

	function handlePause() {
		updateSimulationState({ running: false });
	}

	function handleReset() {
		resetSimulation();
	}

	function handleSpeedChange(e: Event) {
		const value = parseFloat((e.target as HTMLInputElement).value);
		simulationSpeed.set(value);
	}
</script>

<div class="simulation-viewer">
	<div class="sim-header">
		<h3>Traffic Simulation</h3>
		<div class="sim-badges">
			{#if $webgpuAvailable}
				<span class="badge gpu-ok">WebGPU</span>
			{:else}
				<span class="badge gpu-off">No GPU</span>
			{/if}
			{#if $isRunning}
				<span class="badge running">LIVE</span>
			{/if}
		</div>
	</div>

	{#if errorMessage}
		<div class="error-banner">{errorMessage}</div>
	{:else if !$simulationConfig}
		<div class="empty-state">
			<p>No simulation configured.</p>
			<p class="hint">Plan a convoy route to enable traffic simulation.</p>
		</div>
	{:else}
		<!-- Canvas overlay for WebGPU rendering -->
		<div class="canvas-wrapper" class:hidden={!overlayVisible}>
			<canvas bind:this={canvas} class="sim-canvas"></canvas>
		</div>

		<!-- Simulation controls -->
		<div class="sim-controls">
			<div class="transport-controls">
				{#if !$isRunning}
					<button class="btn btn-play" onclick={handleStart} disabled={!engineReady}>
						&#9654; Play
					</button>
				{:else}
					<button class="btn btn-pause" onclick={handlePause}>
						&#9646;&#9646; Pause
					</button>
				{/if}
				<button class="btn btn-reset" onclick={handleReset}>
					&#8634; Reset
				</button>
			</div>

			<div class="speed-control">
				<label>
					Speed: {$simulationSpeed.toFixed(1)}x
					<input
						type="range"
						min="0.1"
						max="10"
						step="0.1"
						value={$simulationSpeed}
						oninput={handleSpeedChange}
					/>
				</label>
			</div>
		</div>

		<!-- Simulation stats -->
		<div class="sim-stats">
			<div class="stat">
				<span class="stat-label">Time</span>
				<span class="stat-value">{$formattedTime}</span>
			</div>
			<div class="stat">
				<span class="stat-label">Vehicles</span>
				<span class="stat-value">{$simulationState.totalVehicles.toLocaleString()}</span>
			</div>
			<div class="stat">
				<span class="stat-label">Avg Speed</span>
				<span class="stat-value">{$simulationState.avgSpeedKmh.toFixed(1)} km/h</span>
			</div>
			<div class="stat">
				<span class="stat-label">Avg Delay</span>
				<span class="stat-value">{$simulationState.avgDelayPerVehicleSec.toFixed(1)}s</span>
			</div>
			<div class="stat">
				<span class="stat-label">Queue</span>
				<span class="stat-value">{$simulationState.queueLengthM.toFixed(0)}m</span>
			</div>
			<div class="stat">
				<span class="stat-label">FPS</span>
				<span class="stat-value" class:fps-low={fpsDisplay < 30}>{fpsDisplay}</span>
			</div>
		</div>

		<!-- Progress bar -->
		<div class="progress-bar">
			<div class="progress-fill" style="width: {$simulationProgress * 100}%"></div>
		</div>

		<!-- Display toggles -->
		<div class="display-toggles">
			<label>
				<input type="checkbox" bind:checked={overlayVisible} />
				Overlay
			</label>
			<label>
				<input type="checkbox" bind:checked={$showVehicles} />
				Vehicles
			</label>
			<label>
				<input type="checkbox" bind:checked={$showQueues} />
				Queues
			</label>
			<label>
				<input type="checkbox" bind:checked={$showConvoyPath} />
				Convoy
			</label>
			<div class="color-mode">
				<label>Color:
					<select bind:value={$vehicleColorMode}>
						<option value="speed">Speed</option>
						<option value="delay">Delay</option>
						<option value="type">Type</option>
					</select>
				</label>
			</div>
		</div>

		{#if $gpuDeviceInfo}
			<div class="gpu-info">{$gpuDeviceInfo}</div>
		{/if}
	{/if}
</div>

<style>
	.simulation-viewer {
		background: #1e293b;
		border: 1px solid #334155;
		border-radius: 0.5rem;
		padding: 1rem;
		display: flex;
		flex-direction: column;
		gap: 0.75rem;
	}

	.sim-header {
		display: flex;
		justify-content: space-between;
		align-items: center;
	}

	.sim-header h3 {
		margin: 0;
		font-size: 1rem;
		color: #94a3b8;
	}

	.sim-badges {
		display: flex;
		gap: 0.5rem;
	}

	.badge {
		font-size: 0.7rem;
		padding: 0.15rem 0.4rem;
		border-radius: 0.25rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.05em;
	}

	.gpu-ok { background: #166534; color: #22c55e; }
	.gpu-off { background: #991b1b; color: #fca5a5; }
	.running { background: #1e3a5f; color: #3b82f6; animation: pulse 1.5s infinite; }

	@keyframes pulse {
		0%, 100% { opacity: 1; }
		50% { opacity: 0.5; }
	}

	.error-banner {
		padding: 0.75rem;
		background: #450a0a;
		border: 1px solid #991b1b;
		border-radius: 0.375rem;
		color: #fca5a5;
		font-size: 0.875rem;
	}

	.empty-state {
		text-align: center;
		padding: 2rem 1rem;
		color: #64748b;
	}

	.hint { font-size: 0.8rem; font-style: italic; }

	.canvas-wrapper {
		position: relative;
		width: 100%;
		height: 200px;
		border-radius: 0.375rem;
		overflow: hidden;
		background: #0f172a;
	}

	.canvas-wrapper.hidden { display: none; }

	.sim-canvas {
		width: 100%;
		height: 100%;
	}

	.sim-controls {
		display: flex;
		justify-content: space-between;
		align-items: center;
		gap: 0.5rem;
	}

	.transport-controls {
		display: flex;
		gap: 0.5rem;
	}

	.btn {
		padding: 0.35rem 0.75rem;
		border: none;
		border-radius: 0.25rem;
		font-size: 0.8rem;
		font-weight: 600;
		cursor: pointer;
	}

	.btn:disabled { opacity: 0.4; cursor: not-allowed; }
	.btn-play { background: #22c55e; color: #0f172a; }
	.btn-pause { background: #f59e0b; color: #0f172a; }
	.btn-reset { background: #334155; color: #94a3b8; }

	.speed-control label {
		font-size: 0.8rem;
		color: #94a3b8;
		display: flex;
		align-items: center;
		gap: 0.5rem;
	}

	.speed-control input[type="range"] {
		width: 80px;
	}

	.sim-stats {
		display: grid;
		grid-template-columns: repeat(3, 1fr);
		gap: 0.5rem;
	}

	.stat {
		display: flex;
		flex-direction: column;
		gap: 0.1rem;
	}

	.stat-label {
		font-size: 0.65rem;
		text-transform: uppercase;
		color: #64748b;
		letter-spacing: 0.05em;
	}

	.stat-value {
		font-size: 0.875rem;
		font-family: monospace;
		color: #f1f5f9;
	}

	.fps-low { color: #ef4444; }

	.progress-bar {
		height: 4px;
		background: #334155;
		border-radius: 2px;
		overflow: hidden;
	}

	.progress-fill {
		height: 100%;
		background: #3b82f6;
		transition: width 0.3s ease;
	}

	.display-toggles {
		display: flex;
		flex-wrap: wrap;
		gap: 0.75rem;
		font-size: 0.8rem;
		color: #94a3b8;
	}

	.display-toggles label {
		display: flex;
		align-items: center;
		gap: 0.25rem;
		cursor: pointer;
	}

	.color-mode select {
		background: #0f172a;
		color: #f1f5f9;
		border: 1px solid #334155;
		border-radius: 0.25rem;
		padding: 0.15rem 0.3rem;
		font-size: 0.75rem;
	}

	.gpu-info {
		font-size: 0.7rem;
		color: #475569;
		font-family: monospace;
		text-align: right;
	}
</style>
