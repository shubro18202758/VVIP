<script lang="ts">
	/**
	 * EmptyState — the honest empty. Every fixture-fed surface uses it
	 * instead of silence or invented data.
	 */
	import Button from './Button.svelte';

	interface Props {
		title: string;
		hint?: string;
		action?: { label: string; onclick: () => void } | null;
	}

	let { title, hint, action = null }: Props = $props();
</script>

<div class="empty" data-prim="empty-state">
	<p class="title">{title}</p>
	{#if hint}<p class="hint">{hint}</p>{/if}
	{#if action}
		<Button variant="secondary" size="sm" onclick={() => action.onclick()}>{action.label}</Button>
	{/if}
</div>

<style>
	.empty {
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: var(--v-space-2);
		padding: var(--v-space-6) var(--v-space-4);
		border: 1px dashed var(--v-line-2);
		border-radius: var(--v-radius-panel);
		text-align: center;
	}

	.title {
		margin: 0;
		font-size: var(--v-text-sm);
		color: var(--v-ink-2);
	}

	.hint {
		margin: 0;
		font-size: var(--v-text-xs);
		color: var(--v-ink-3);
		max-width: 44ch;
	}
</style>
