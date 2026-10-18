<script lang="ts">
	/**
	 * DataTile — one of the commander's counters. The value is ALWAYS
	 * mono at the 28px display step (every mutable number is mono).
	 */
	import Badge, { type BadgeVariant } from './Badge.svelte';

	interface Props {
		label: string;
		value: string | number;
		unit?: string;
		/** Source honesty chip (live / fixture / emulated …). */
		source?: { variant: BadgeVariant; label: string } | null;
		onClick?: (e: MouseEvent) => void;
	}

	let { label, value, unit, source = null, onClick }: Props = $props();

	const interactive = $derived(typeof onClick === 'function');
</script>

{#if interactive}
	<button class="tile clickable" type="button" onclick={onClick} data-prim="data-tile">
		<span class="inner">
			<span class="label">{label}</span>
			<span class="value-line">
				<span class="value">{value}</span>
				{#if unit}<span class="unit">{unit}</span>{/if}
			</span>
			{#if source}<Badge variant={source.variant} label={source.label} />{/if}
		</span>
	</button>
{:else}
	<div class="tile" data-prim="data-tile">
		<span class="label">{label}</span>
		<span class="value-line">
			<span class="value">{value}</span>
			{#if unit}<span class="unit">{unit}</span>{/if}
		</span>
		{#if source}<Badge variant={source.variant} label={source.label} />{/if}
	</div>
{/if}

<style>
	.tile {
		display: flex;
		flex-direction: column;
		gap: var(--v-space-1);
		min-width: 0;
		padding: var(--v-pad-panel);
		background: var(--v-surface-1);
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-panel);
		text-align: left;
		font: inherit;
		color: inherit;
	}

	.clickable {
		cursor: pointer;
		transition: border-color var(--v-dur-out) var(--v-ease);
	}

	.clickable:hover,
	.clickable:focus-visible {
		border-color: var(--v-line-select);
	}

	.clickable:focus-visible {
		outline: 1px solid var(--v-act-focus);
		outline-offset: 2px;
	}

	.inner {
		display: flex;
		flex-direction: column;
		gap: var(--v-space-1);
	}

	.label {
		font-size: var(--v-text-xs);
		font-weight: var(--v-weight-status);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
		color: var(--v-ink-3);
	}

	.value-line {
		display: flex;
		align-items: baseline;
		gap: var(--v-space-1);
	}

	.value {
		font-family: var(--v-font-mono);
		font-size: var(--v-text-xl);
		line-height: 1.1;
		color: var(--v-ink-1);
	}

	.unit {
		font-family: var(--v-font-mono);
		font-size: var(--v-text-xs);
		color: var(--v-ink-3);
	}
</style>
