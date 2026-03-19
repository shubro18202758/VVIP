<script lang="ts">
	import ConvoyTracker from '$components/ConvoyTracker.svelte';
	import LoadingSkeleton from '$components/LoadingSkeleton.svelte';
	import { activeConvoy } from '$stores/convoy';
	import { corridorAvgCongestion, congestedSegmentCount, trafficArray } from '$stores/traffic';
	import {
		services,
		gpuStatus,
		allServicesOnline,
		vramUsagePercent,
	} from '$stores/health';
	import { trafficFeed, healthFeed } from '$stores/connection';
</script>

<div class="dashboard">
	<h1>VVIP Convoy Orchestration Dashboard</h1>

	<div class="grid">
		<!-- Active Movements -->
		<section class="card">
			<h2>Active Movements</h2>
			{#if $activeConvoy}
				<ConvoyTracker
					movementId={$activeConvoy.movementId}
					position={$activeConvoy.position}
					speed={$activeConvoy.speedKmh}
					status={$activeConvoy.status}
					vvipClass={$activeConvoy.vvipClass}
				/>
			{:else}
				<p class="placeholder">No active convoy movements</p>
			{/if}
		</section>

		<!-- Corridor Status -->
		<section class="card">
			<h2>Corridor Status</h2>
			{#if $trafficFeed.state === 'loading' && $trafficFeed.fetchCount === 0}
				<LoadingSkeleton lines={3} height="0.875rem" />
			{:else if $trafficFeed.state === 'error'}
				<p class="placeholder error-text">Traffic feed unreachable</p>
			{:else if $trafficArray.length > 0}
				<div class="corridor-stats">
					<div class="stat-row">
						<span class="stat-label">Active Segments</span>
						<span class="stat-value">{$trafficArray.length.toLocaleString()}</span>
					</div>
					<div class="stat-row">
						<span class="stat-label">Avg Congestion</span>
						<span class="stat-value" class:warning={$corridorAvgCongestion > 0.5} class:danger={$corridorAvgCongestion > 0.7}>
							{($corridorAvgCongestion * 100).toFixed(1)}%
						</span>
					</div>
					<div class="stat-row">
						<span class="stat-label">Congested (>70%)</span>
						<span class="stat-value" class:danger={$congestedSegmentCount > 0}>
							{$congestedSegmentCount}
						</span>
					</div>
				</div>
				<!-- Mini congestion bar -->
				<div class="congestion-bar">
					<div
						class="congestion-fill"
						style="width: {$corridorAvgCongestion * 100}%; background: {$corridorAvgCongestion > 0.7 ? '#ef4444' : $corridorAvgCongestion > 0.5 ? '#f59e0b' : '#22c55e'}"
					></div>
				</div>
			{:else}
				<p class="placeholder">Traffic feed aggregation offline</p>
			{/if}
		</section>

		<!-- System Health -->
		<section class="card">
			<h2>System Health</h2>
			{#if $healthFeed.state === 'loading' && $healthFeed.fetchCount === 0}
				<LoadingSkeleton lines={5} height="0.75rem" gap="0.4rem" />
			{:else}
				{#if $allServicesOnline}
				<div class="all-online">All systems operational</div>
			{/if}
			<ul class="health-list">
				{#each $services as svc}
					<li>
						<span class="health-dot" class:online={svc.status === 'online'} class:degraded={svc.status === 'degraded'} class:offline={svc.status === 'offline'}></span>
						{svc.name}:
						<span class={svc.status}>{svc.status}</span>
						{#if svc.latencyMs !== null}
							<span class="latency">{svc.latencyMs}ms</span>
						{/if}
					</li>
				{/each}
			</ul>
			{/if}
		</section>

		<!-- GPU Status -->
		<section class="card">
			<h2>GPU Status — RTX 4070 (8 GB)</h2>
			<div class="vram-bar">
				<div class="vram-used" style="width: {$vramUsagePercent}%"></div>
			</div>
			<div class="vram-label">{$gpuStatus.vramUsedMb} / {$gpuStatus.vramTotalMb} MB ({$vramUsagePercent.toFixed(1)}%)</div>
			<div class="vram-breakdown">
				<div class="alloc-row">
					<span class="alloc-color" style="background: #3b82f6"></span>
					Ollama/Qwen: {$gpuStatus.allocations.ollamaQwen} MB
				</div>
				<div class="alloc-row">
					<span class="alloc-color" style="background: #8b5cf6"></span>
					ONNX DSTGAT: {$gpuStatus.allocations.onnxDstgat} MB
				</div>
				<div class="alloc-row">
					<span class="alloc-color" style="background: #f59e0b"></span>
					CUDA: {$gpuStatus.allocations.cudaOverhead} MB
				</div>
				<div class="alloc-row">
					<span class="alloc-color" style="background: #22c55e"></span>
					Headroom: {$gpuStatus.allocations.headroom} MB
				</div>
			</div>
			{#if $gpuStatus.temperature > 0}
				<div class="gpu-temp">GPU Temp: {$gpuStatus.temperature}°C · Util: {$gpuStatus.gpuUtilPercent}%</div>
			{/if}
		</section>
	</div>
</div>

<style>
	.dashboard h1 { margin-bottom: 1.5rem; flex-shrink: 0; }
	.grid {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
		gap: 1rem;
	}
	.card {
		background: #1e293b;
		border: 1px solid #334155;
		border-radius: 0.5rem;
		padding: 1.25rem;
	}
	.card h2 { font-size: 0.9rem; margin: 0 0 0.75rem; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.05em; }
	.placeholder { color: #64748b; font-style: italic; }
	.error-text { color: #fca5a5; }

	/* Corridor Stats */
	.corridor-stats { display: flex; flex-direction: column; gap: 0.4rem; margin-bottom: 0.75rem; }
	.stat-row { display: flex; justify-content: space-between; font-size: 0.875rem; }
	.stat-label { color: #94a3b8; }
	.stat-value { font-family: monospace; font-weight: 600; }
	.stat-value.warning { color: #f59e0b; }
	.stat-value.danger { color: #ef4444; }

	.congestion-bar { height: 6px; background: #334155; border-radius: 3px; overflow: hidden; }
	.congestion-fill { height: 100%; transition: width 0.5s ease, background 0.5s ease; }

	/* Health */
	.all-online { color: #22c55e; font-size: 0.8rem; font-weight: 600; margin-bottom: 0.5rem; }
	.health-list { list-style: none; padding: 0; margin: 0; }
	.health-list li { padding: 0.3rem 0; font-size: 0.85rem; display: flex; align-items: center; gap: 0.5rem; }
	.health-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
	.health-dot.online { background: #22c55e; }
	.health-dot.degraded { background: #f59e0b; }
	.health-dot.offline { background: #ef4444; }
	.online { color: #22c55e; }
	.degraded { color: #f59e0b; }
	.offline { color: #ef4444; }
	.latency { color: #64748b; font-size: 0.75rem; font-family: monospace; }

	/* GPU */
	.vram-bar { height: 12px; background: #334155; border-radius: 6px; overflow: hidden; margin-bottom: 0.4rem; }
	.vram-used { height: 100%; background: linear-gradient(90deg, #3b82f6, #8b5cf6); transition: width 0.5s ease; }
	.vram-label { font-size: 0.75rem; color: #94a3b8; font-family: monospace; margin-bottom: 0.75rem; }
	.vram-breakdown { display: flex; flex-direction: column; gap: 0.25rem; }
	.alloc-row { font-size: 0.8rem; color: #94a3b8; display: flex; align-items: center; gap: 0.5rem; }
	.alloc-color { width: 10px; height: 10px; border-radius: 2px; }
	.gpu-temp { font-size: 0.75rem; color: #64748b; margin-top: 0.5rem; font-family: monospace; }
</style>
