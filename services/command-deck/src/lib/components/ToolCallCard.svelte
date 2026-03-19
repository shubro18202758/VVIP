<!-- ToolCallCard.svelte — MCP tool invocation status card with expandable arguments/results -->

<script lang="ts">
	import type { ToolCallStatus, ToolCallState } from '$lib/types';

	let { toolCall } = $props<{ toolCall: ToolCallStatus }>();

	let showArgs = $state(false);
	let showResult = $state(false);

	const toolDisplayNames: Record<string, string> = {
		predict_traffic_flow: 'Traffic Prediction (DSTGAT)',
		find_convoy_routes: 'Route Optimization (MIP Solver)',
		plan_diversions: 'Diversion Planning',
		evaluate_scenarios: 'Scenario Evaluation',
		predict_eta: 'ETA Prediction',
		query_shortest_path: 'Shortest Path Query',
		query_k_shortest_paths: 'K-Shortest Paths',
		query_segments_in_bbox: 'Spatial Query',
		query_segment_details: 'Segment Details',
		get_live_traffic: 'Live Traffic Data',
		get_historical_pattern: 'Historical Patterns',
	};

	const stateConfig: Record<ToolCallState, { color: string; bg: string; border: string; label: string }> = {
		pending: {
			color: '#94a3b8',
			bg: 'rgba(148, 163, 184, 0.08)',
			border: '#475569',
			label: 'Pending',
		},
		running: {
			color: '#3b82f6',
			bg: 'rgba(59, 130, 246, 0.08)',
			border: '#3b82f6',
			label: 'Running',
		},
		success: {
			color: '#22c55e',
			bg: 'rgba(34, 197, 94, 0.08)',
			border: '#22c55e',
			label: 'Success',
		},
		error: {
			color: '#ef4444',
			bg: 'rgba(239, 68, 68, 0.08)',
			border: '#ef4444',
			label: 'Error',
		},
	};

	const displayName = $derived(
		toolDisplayNames[toolCall.toolName] ?? toolCall.toolName
	);

	const config = $derived(stateConfig[toolCall.state]);

	const hasArgs = $derived(
		toolCall.arguments && Object.keys(toolCall.arguments).length > 0
	);

	const hasResult = $derived(toolCall.result !== null && toolCall.result !== undefined);

	const formattedArgs = $derived(
		hasArgs ? JSON.stringify(toolCall.arguments, null, 2) : ''
	);

	const formattedResult = $derived(
		hasResult ? JSON.stringify(toolCall.result, null, 2) : ''
	);

	const durationDisplay = $derived(
		toolCall.durationMs !== null
			? toolCall.durationMs < 1000
				? `${toolCall.durationMs}ms`
				: `${(toolCall.durationMs / 1000).toFixed(2)}s`
			: null
	);
</script>

<div
	class="tool-card"
	style="--tc-color: {config.color}; --tc-bg: {config.bg}; --tc-border: {config.border};"
>
	<div class="tool-header">
		<div class="tool-identity">
			<span class="tool-state-dot" class:tool-state-dot--running={toolCall.state === 'running'}></span>
			<span class="tool-name">{displayName}</span>
		</div>
		<div class="tool-meta">
			{#if durationDisplay}
				<span class="tool-duration">{durationDisplay}</span>
			{/if}
			<span class="tool-state-badge">{config.label}</span>
		</div>
	</div>

	{#if hasArgs}
		<div class="tool-section">
			<button
				class="section-toggle"
				onclick={() => (showArgs = !showArgs)}
				aria-expanded={showArgs}
			>
				<span class="toggle-chevron" class:toggle-chevron--open={showArgs}>&#9656;</span>
				Arguments
			</button>
			{#if showArgs}
				<div class="section-body">
					<pre class="json-block">{formattedArgs}</pre>
				</div>
			{/if}
		</div>
	{/if}

	{#if hasResult}
		<div class="tool-section">
			<button
				class="section-toggle"
				onclick={() => (showResult = !showResult)}
				aria-expanded={showResult}
			>
				<span class="toggle-chevron" class:toggle-chevron--open={showResult}>&#9656;</span>
				Result
			</button>
			{#if showResult}
				<div class="section-body">
					<pre class="json-block">{formattedResult}</pre>
				</div>
			{/if}
		</div>
	{/if}

	{#if toolCall.state === 'running' && !hasResult}
		<div class="running-indicator">
			<div class="running-bar"></div>
		</div>
	{/if}
</div>

<style>
	.tool-card {
		border: 1px solid var(--tc-border);
		border-radius: 0.5rem;
		background: var(--tc-bg);
		overflow: hidden;
		transition: border-color 0.2s;
	}

	.tool-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 0.5rem;
		padding: 0.625rem 0.75rem;
	}

	.tool-identity {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		min-width: 0;
	}

	.tool-state-dot {
		flex-shrink: 0;
		width: 8px;
		height: 8px;
		border-radius: 50%;
		background: var(--tc-color);
	}

	.tool-state-dot--running {
		animation: dotPulse 1.5s ease-in-out infinite;
	}

	@keyframes dotPulse {
		0%, 100% {
			opacity: 1;
			box-shadow: 0 0 0 0 var(--tc-color);
		}
		50% {
			opacity: 0.7;
			box-shadow: 0 0 0 4px transparent;
		}
	}

	.tool-name {
		font-size: 0.8125rem;
		font-weight: 600;
		color: #f1f5f9;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.tool-meta {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		flex-shrink: 0;
	}

	.tool-duration {
		font-size: 0.6875rem;
		font-family: monospace;
		color: #94a3b8;
	}

	.tool-state-badge {
		font-size: 0.6875rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.025em;
		color: var(--tc-color);
		padding: 0.125rem 0.375rem;
		border: 1px solid var(--tc-border);
		border-radius: 0.25rem;
		background: rgba(0, 0, 0, 0.2);
	}

	.tool-section {
		border-top: 1px solid rgba(51, 65, 85, 0.5);
	}

	.section-toggle {
		display: flex;
		align-items: center;
		gap: 0.375rem;
		width: 100%;
		padding: 0.4375rem 0.75rem;
		background: none;
		border: none;
		color: #94a3b8;
		font-size: 0.75rem;
		font-weight: 500;
		cursor: pointer;
		text-align: left;
		transition: color 0.15s, background 0.15s;
	}

	.section-toggle:hover {
		color: #cbd5e1;
		background: rgba(255, 255, 255, 0.03);
	}

	.toggle-chevron {
		font-size: 0.625rem;
		transition: transform 0.2s;
		color: #64748b;
	}

	.toggle-chevron--open {
		transform: rotate(90deg);
	}

	.section-body {
		padding: 0 0.75rem 0.5rem;
		animation: sectionReveal 0.2s ease-out;
	}

	@keyframes sectionReveal {
		from {
			opacity: 0;
			transform: translateY(-4px);
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}

	.json-block {
		margin: 0;
		padding: 0.5rem;
		background: rgba(0, 0, 0, 0.3);
		border-radius: 0.375rem;
		font-family: 'JetBrains Mono', 'Fira Code', monospace;
		font-size: 0.6875rem;
		color: #cbd5e1;
		line-height: 1.5;
		overflow-x: auto;
		white-space: pre-wrap;
		word-break: break-all;
		max-height: 200px;
		overflow-y: auto;
	}

	.running-indicator {
		height: 2px;
		background: rgba(59, 130, 246, 0.2);
		overflow: hidden;
	}

	.running-bar {
		height: 100%;
		width: 40%;
		background: #3b82f6;
		border-radius: 1px;
		animation: runSlide 1.2s ease-in-out infinite;
	}

	@keyframes runSlide {
		0% {
			transform: translateX(-100%);
		}
		100% {
			transform: translateX(350%);
		}
	}
</style>
