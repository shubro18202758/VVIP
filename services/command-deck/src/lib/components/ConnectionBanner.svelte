<!-- ConnectionBanner.svelte — Global connection status indicator -->
<!-- Renders at the top of the content area when data feeds have errors or are stale -->

<script lang="ts">
	import {
		anyError,
		anyLoading,
		errorFeeds,
		initialLoadComplete,
		oldestUpdate,
	} from '$stores/connection';

	// Refresh stale computation every 5s
	let tick = $state(0);
	$effect(() => {
		const id = setInterval(() => { tick++; }, 5000);
		return () => clearInterval(id);
	});

	let staleSec = $derived.by((): number => {
		void tick; // re-evaluate when tick changes
		const oldest = $oldestUpdate;
		if (oldest === 0) return 0;
		return Math.floor((Date.now() - oldest) / 1000);
	});

	let stale = $derived(staleSec > 60);
	let errorCount = $derived($errorFeeds.length);
</script>

{#if $anyError}
	<div class="banner banner-error">
		<span class="banner-icon">!</span>
		<span>{errorCount} data feed{errorCount !== 1 ? 's' : ''} unreachable</span>
	</div>
{:else if !$initialLoadComplete}
	<div class="banner banner-loading">
		<span class="banner-spinner"></span>
		<span>Connecting to backend services...</span>
	</div>
{:else if stale}
	<div class="banner banner-stale">
		<span class="banner-icon">~</span>
		<span>Data may be stale ({staleSec}s since last update)</span>
	</div>
{/if}

<style>
	.banner {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		padding: 0.4rem 0.75rem;
		border-radius: 0.375rem;
		font-size: 0.75rem;
		font-weight: 500;
		margin-bottom: 0.75rem;
	}

	.banner-error {
		background: rgba(239, 68, 68, 0.12);
		border: 1px solid rgba(239, 68, 68, 0.3);
		color: #fca5a5;
	}

	.banner-loading {
		background: rgba(59, 130, 246, 0.1);
		border: 1px solid rgba(59, 130, 246, 0.25);
		color: #93c5fd;
	}

	.banner-stale {
		background: rgba(245, 158, 11, 0.1);
		border: 1px solid rgba(245, 158, 11, 0.25);
		color: #fcd34d;
	}

	.banner-icon {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 1.125rem;
		height: 1.125rem;
		border-radius: 50%;
		font-size: 0.625rem;
		font-weight: 700;
		flex-shrink: 0;
	}

	.banner-error .banner-icon {
		background: rgba(239, 68, 68, 0.3);
		color: #fca5a5;
	}

	.banner-stale .banner-icon {
		background: rgba(245, 158, 11, 0.25);
		color: #fcd34d;
	}

	.banner-spinner {
		width: 0.875rem;
		height: 0.875rem;
		border: 2px solid rgba(59, 130, 246, 0.2);
		border-top-color: #3b82f6;
		border-radius: 50%;
		animation: spin 0.8s linear infinite;
		flex-shrink: 0;
	}

	@keyframes spin {
		to { transform: rotate(360deg); }
	}
</style>
