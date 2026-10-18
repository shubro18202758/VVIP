<script lang="ts">
	/**
	 * LayerRail — MapLegend bound to a layer list. The layer registry
	 * itself stays with the canvas stream (this component must not import
	 * $lib/gods-eye/**); callers map GodsEyeLayer + LayerCoverageStats into
	 * plain legend items:
	 *
	 *   items = GODS_EYE_LAYERS.map((l) => ({
	 *     id: l.id, label: l.label,
	 *     colorToken: '<token>', state: on ? 'on' : 'off', hint: stats.hint
	 *   }))
	 */
	import MapLegend, { type LegendItem } from './MapLegend.svelte';

	interface Props {
		items: LegendItem[];
		onToggle?: ((id: string) => void) | null;
		lang?: string;
	}

	let { items, onToggle = null, lang = 'en' }: Props = $props();
</script>

<aside class="rail" data-prim="layer-rail">
	<MapLegend {items} {onToggle} {lang} />
</aside>

<style>
	.rail {
		display: flex;
		flex-direction: column;
		gap: var(--v-space-2);
		padding: var(--v-space-2);
		background: var(--v-surface-sink);
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-chip);
	}
</style>
