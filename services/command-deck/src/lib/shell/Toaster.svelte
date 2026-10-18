<script lang="ts">
	/**
	 * Toaster — the toast region (bottom-right, max 3). Mounted by both
	 * shells; the store caps cadence, the region caps visibility.
	 */
	import Toast from '$lib/primitives/Toast.svelte';
	import { toasts } from './toast.svelte';
</script>

<div class="toaster" aria-live="polite" data-shell="toaster">
	{#each toasts.items as item (item.id)}
		<Toast item={item} onDismiss={(id) => toasts.dismiss(id)} />
	{/each}
</div>

<style>
	.toaster {
		position: fixed;
		right: var(--v-space-3);
		bottom: var(--v-space-3);
		display: flex;
		flex-direction: column;
		gap: var(--v-space-2);
		z-index: 95;
		max-width: min(360px, calc(100vw - 32px));
	}
</style>
