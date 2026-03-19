<!-- ThoughtChain.svelte — Vertical reasoning chain visualizer for LLM thought steps -->

<script lang="ts">
	import type { ThoughtStep } from '$lib/types';

	let { thoughts = [] as ThoughtStep[] } = $props();

	let expandedSteps = $state<Set<number>>(new Set());

	const firstTimestamp = $derived(
		thoughts.length > 0 ? thoughts[0].timestamp : 0
	);

	function toggleStep(index: number): void {
		const next = new Set(expandedSteps);
		if (next.has(index)) {
			next.delete(index);
		} else {
			next.add(index);
		}
		expandedSteps = next;
	}

	function relativeTime(timestamp: number): string {
		if (firstTimestamp === 0) return '';
		const delta = timestamp - firstTimestamp;
		if (delta < 1000) return `+${delta}ms`;
		return `+${(delta / 1000).toFixed(1)}s`;
	}

	function truncate(text: string, maxLen: number = 120): string {
		if (text.length <= maxLen) return text;
		return text.slice(0, maxLen) + '...';
	}
</script>

{#if thoughts.length === 0}
	<div class="thought-chain-empty">No reasoning steps captured.</div>
{:else}
	<div class="thought-chain">
		<div class="chain-line" aria-hidden="true"></div>

		{#each thoughts as thought, i (thought.stepIndex)}
			<div class="thought-node" class:thought-node--new={i === thoughts.length - 1}>
				<button
					class="node-indicator"
					onclick={() => toggleStep(i)}
					aria-expanded={expandedSteps.has(i)}
					aria-label="Toggle reasoning step {thought.stepIndex}"
				>
					<span class="node-number">{thought.stepIndex + 1}</span>
				</button>

				<div class="node-content">
					<div class="node-header">
						<button
							class="node-toggle"
							onclick={() => toggleStep(i)}
							aria-expanded={expandedSteps.has(i)}
						>
							<span class="toggle-icon" class:toggle-icon--open={expandedSteps.has(i)}>
								&#9656;
							</span>
							<span class="node-preview">
								{#if expandedSteps.has(i)}
									Step {thought.stepIndex + 1}
								{:else}
									{truncate(thought.text)}
								{/if}
							</span>
						</button>
						{#if thought.timestamp && firstTimestamp}
							<span class="node-time">{relativeTime(thought.timestamp)}</span>
						{/if}
					</div>

					{#if expandedSteps.has(i)}
						<div class="node-body">
							<p class="node-text">{thought.text}</p>
						</div>
					{/if}
				</div>
			</div>
		{/each}
	</div>
{/if}

<style>
	.thought-chain {
		position: relative;
		padding-left: 1.5rem;
	}

	.thought-chain-empty {
		color: #94a3b8;
		font-size: 0.8125rem;
		font-style: italic;
		padding: 0.5rem 0;
	}

	.chain-line {
		position: absolute;
		left: 0.9375rem;
		top: 0.75rem;
		bottom: 0.75rem;
		width: 2px;
		background: linear-gradient(to bottom, #6366f1, #8b5cf6);
		opacity: 0.4;
		border-radius: 1px;
	}

	.thought-node {
		position: relative;
		display: flex;
		gap: 0.75rem;
		padding: 0.375rem 0;
		animation: thoughtFadeIn 0.3s ease-out both;
	}

	.thought-node--new {
		animation: thoughtSlideIn 0.4s ease-out both;
	}

	@keyframes thoughtFadeIn {
		from {
			opacity: 0;
		}
		to {
			opacity: 1;
		}
	}

	@keyframes thoughtSlideIn {
		from {
			opacity: 0;
			transform: translateY(8px);
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}

	.node-indicator {
		flex-shrink: 0;
		width: 1.5rem;
		height: 1.5rem;
		border-radius: 50%;
		background: #1e1b4b;
		border: 2px solid #6366f1;
		display: flex;
		align-items: center;
		justify-content: center;
		cursor: pointer;
		z-index: 1;
		transition: background 0.15s, border-color 0.15s;
		padding: 0;
	}

	.node-indicator:hover {
		background: #312e81;
		border-color: #818cf8;
	}

	.node-number {
		font-size: 0.6875rem;
		font-weight: 700;
		color: #a5b4fc;
		line-height: 1;
	}

	.node-content {
		flex: 1;
		min-width: 0;
	}

	.node-header {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		min-height: 1.5rem;
	}

	.node-toggle {
		display: flex;
		align-items: center;
		gap: 0.375rem;
		background: none;
		border: none;
		padding: 0;
		cursor: pointer;
		color: #c7d2fe;
		font-size: 0.8125rem;
		text-align: left;
		line-height: 1.4;
		flex: 1;
		min-width: 0;
	}

	.node-toggle:hover {
		color: #e0e7ff;
	}

	.toggle-icon {
		flex-shrink: 0;
		font-size: 0.625rem;
		transition: transform 0.2s;
		color: #6366f1;
	}

	.toggle-icon--open {
		transform: rotate(90deg);
	}

	.node-preview {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.node-time {
		flex-shrink: 0;
		font-size: 0.6875rem;
		color: #64748b;
		font-family: monospace;
	}

	.node-body {
		margin-top: 0.375rem;
		padding: 0.5rem 0.75rem;
		background: rgba(99, 102, 241, 0.08);
		border-left: 2px solid #6366f1;
		border-radius: 0 0.375rem 0.375rem 0;
		animation: bodyExpand 0.2s ease-out;
	}

	@keyframes bodyExpand {
		from {
			opacity: 0;
			max-height: 0;
		}
		to {
			opacity: 1;
			max-height: 500px;
		}
	}

	.node-text {
		margin: 0;
		font-size: 0.8125rem;
		color: #cbd5e1;
		line-height: 1.5;
		white-space: pre-wrap;
		word-break: break-word;
	}
</style>
