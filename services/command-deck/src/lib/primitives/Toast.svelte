<script lang="ts">
	/**
	 * Toast — one user-action feedback item (info | act | error). Rendered
	 * by shell/Toaster from the toast store; system chatter never lands
	 * here (the audit stream owns it).
	 */
	import type { ToastItem } from '$lib/shell/toast.svelte';

	interface Props {
		item: ToastItem;
		onDismiss: (id: number) => void;
	}

	let { item, onDismiss }: Props = $props();
</script>

<div class="toast k-{item.kind}" role="status" data-prim="toast">
	<div class="text">
		<span class="title">{item.title}</span>
		{#if item.body}<span class="body">{item.body}</span>{/if}
	</div>
	<button class="dismiss" type="button" onclick={() => onDismiss(item.id)} aria-label="dismiss">
		×
	</button>
</div>

<style>
	.toast {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: var(--v-space-2);
		width: 320px;
		padding: var(--v-space-2) var(--v-space-3);
		background: var(--v-surface-2);
		border: 1px solid var(--v-line-2);
		border-radius: var(--v-radius-panel);
		animation: toast-in var(--v-dur-in) var(--v-ease);
	}

	@keyframes toast-in {
		from {
			opacity: 0;
			transform: translateY(4px);
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}

	.k-info {
		border-color: var(--v-line-select);
	}

	.k-act {
		border-color: var(--v-act);
	}

	.k-error {
		border-color: var(--v-band-red);
	}

	.text {
		display: flex;
		flex-direction: column;
		gap: 2px;
		min-width: 0;
	}

	.title {
		font-size: var(--v-text-sm);
		color: var(--v-ink-1);
	}

	.k-act .title {
		color: var(--v-act-text);
	}

	.k-error .title {
		color: var(--v-band-red);
	}

	.body {
		font-family: var(--v-font-mono);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.dismiss {
		background: transparent;
		border: none;
		color: var(--v-ink-3);
		cursor: pointer;
		font-size: var(--v-text-md);
		line-height: 1;
		padding: 0 var(--v-space-1);
	}

	.dismiss:hover {
		color: var(--v-ink-1);
	}

	@media (prefers-reduced-motion: reduce) {
		.toast {
			animation: none;
		}
	}
</style>
