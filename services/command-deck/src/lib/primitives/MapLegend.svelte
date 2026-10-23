<script lang="ts">
	/**
	 * MapLegend — toggleable layer legend. 10px swatches: filled = on,
	 * hollow = off, dashed = not deployed, dotted = empty. Maps 1:1 to the
	 * GodsEyeLayer {id,name,label} + LayerCoverageStats.hint contract
	 * (callers bind it; this component never imports the canvas layer
	 * modules — import direction stays one-way).
	 */
	import { t } from '$lib/i18n/strings';

	export type LegendItemState = 'on' | 'off' | 'not-deployed' | 'empty';

	export interface LegendItem {
		id: string;
		label: string;
		/** CSS custom property name carrying the layer color. */
		colorToken: string;
		state?: LegendItemState;
		hint?: string;
	}

	interface Props {
		items: LegendItem[];
		onToggle?: ((id: string) => void) | null;
		lang?: string;
	}

	let { items, onToggle = null, lang = 'en' }: Props = $props();

	const swatchStyle = (item: LegendItem) =>
		`--layer-color: var(${item.colorToken}, var(--v-data))`;
</script>

<ul class="legend" data-prim="map-legend">
	{#each items as item (item.id)}
		<li>
			{#if onToggle}
				<button
					class="row toggler"
					type="button"
					aria-pressed={item.state === 'on'}
					title={item.hint ?? ''}
					onclick={() => onToggle(item.id)}
				>
					<span class="swatch s-{item.state ?? 'on'}" style={swatchStyle(item)}></span>
					<span class="row-text">
						<span class="row-label">{item.label}</span>
						{#if item.hint}<span class="row-hint">{item.hint}</span>{/if}
					</span>
					<span class="state-word">{item.state ?? 'on'}</span>
				</button>
			{:else}
				<div class="row" title={item.hint ?? ''}>
					<span class="swatch s-{item.state ?? 'on'}" style={swatchStyle(item)}></span>
					<span class="row-text">
						<span class="row-label">{item.label}</span>
						{#if item.hint}<span class="row-hint">{item.hint}</span>{/if}
					</span>
					<span class="state-word">{item.state ?? 'on'}</span>
				</div>
			{/if}
		</li>
	{/each}
</ul>

<style>
	.legend {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: var(--v-space-1);
	}

	.row {
		display: flex;
		align-items: center;
		gap: var(--v-space-2);
		width: 100%;
		padding: var(--v-space-1) var(--v-space-2);
		border: 1px solid transparent;
		border-radius: var(--v-radius-chip);
		background: transparent;
		color: var(--v-ink-2);
		font: inherit;
		text-align: left;
	}

	.toggler {
		cursor: pointer;
	}

	.toggler:hover {
		border-color: var(--v-line-1);
	}

	.toggler:focus-visible {
		outline: 1px solid var(--v-act-focus);
		outline-offset: 2px;
	}

	.swatch {
		width: 10px;
		height: 10px;
		border-radius: 2px;
		flex-shrink: 0;
		border: 1px solid var(--layer-color);
	}

	.s-on {
		background: var(--layer-color);
	}

	.s-off {
		background: transparent;
	}

	.s-not-deployed {
		background: transparent;
		border-style: dashed;
		border-color: var(--v-ink-3);
	}

	.s-empty {
		background: transparent;
		border-style: dotted;
		border-color: var(--v-ink-4);
	}

	.row-text {
		display: flex;
		flex-direction: column;
		min-width: 0;
		flex: 1;
	}

	.row-label {
		font-size: var(--v-text-xs);
		color: var(--v-ink-2);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.row-hint {
		font-family: var(--v-font-mono);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.state-word {
		font-family: var(--v-font-mono);
		font-size: var(--v-text-2xs);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
		color: var(--v-ink-3);
	}

	.s-not-deployed + .row-text .row-label {
		color: var(--v-ink-3);
	}
</style>
