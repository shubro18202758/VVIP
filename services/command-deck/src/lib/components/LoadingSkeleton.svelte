<!-- LoadingSkeleton.svelte — Animated placeholder for async data regions -->

<script lang="ts">
	let {
		lines = 3,
		height = '0.75rem',
		gap = '0.5rem',
		variant = 'default' as 'default' | 'card' | 'inline',
	} = $props();
</script>

{#if variant === 'card'}
	<div class="skeleton-card">
		<div class="skeleton-line title" style="height: 1rem; width: 60%"></div>
		<div class="skeleton-line" style="height: {height}; width: 100%"></div>
		<div class="skeleton-line" style="height: {height}; width: 80%"></div>
	</div>
{:else if variant === 'inline'}
	<span class="skeleton-inline" style="height: {height}"></span>
{:else}
	<div class="skeleton-stack" style="gap: {gap}">
		{#each Array(lines) as _, i}
			<div
				class="skeleton-line"
				style="height: {height}; width: {i === lines - 1 ? '60%' : '100%'}"
			></div>
		{/each}
	</div>
{/if}

<style>
	.skeleton-card {
		display: flex;
		flex-direction: column;
		gap: 0.5rem;
		padding: 0.75rem;
		background: #1e293b;
		border: 1px solid #334155;
		border-radius: 0.375rem;
	}

	.skeleton-stack {
		display: flex;
		flex-direction: column;
	}

	.skeleton-line {
		background: linear-gradient(90deg, #1e293b 25%, #334155 50%, #1e293b 75%);
		background-size: 200% 100%;
		border-radius: 0.25rem;
		animation: shimmer 1.5s ease-in-out infinite;
	}

	.skeleton-line.title {
		margin-bottom: 0.25rem;
	}

	.skeleton-inline {
		display: inline-block;
		width: 4rem;
		background: linear-gradient(90deg, #1e293b 25%, #334155 50%, #1e293b 75%);
		background-size: 200% 100%;
		border-radius: 0.125rem;
		animation: shimmer 1.5s ease-in-out infinite;
		vertical-align: middle;
	}

	@keyframes shimmer {
		0% { background-position: 200% 0; }
		100% { background-position: -200% 0; }
	}
</style>
