<script lang="ts">
	/**
	 * PursuitPanel — the ranked interception choke plan for the selected
	 * pursuit event: ranks 1→N with graph-cost ETA and hold cost, from the
	 * live pgRouting driving-distance endpoint. Captioned with the backend's
	 * own words: it assists the operator, it never dispatches on its own.
	 */
	import Panel from '$lib/primitives/Panel.svelte';
	import Badge from '$lib/primitives/Badge.svelte';
	import EmptyState from '$lib/primitives/EmptyState.svelte';
	import { ops } from '$lib/ops/store.svelte';
	import { tOps } from '$lib/i18n/strings.ops';

	const lang = $derived(ops.lang);
	const plan = $derived(ops.interception);

	/** Hold cost = the choke weight × cost seconds (the honest proxy). */
	function holdCost(score: number | null, weight: number | null): string {
		if (score == null || weight == null) return '—';
		return (score * 100).toFixed(1);
	}
</script>

<Panel
	title={tOps(lang, 'pursuitTitle')}
	sub={plan?.note ?? tOps(lang, 'pursuitCaption')}
	status={ops.interceptionUpdated ? { state: ops.interceptionNote ? 'stale' : 'live', updatedAt: ops.interceptionUpdated } : null}
	lang={lang}
>
	{#if plan && plan.latestSighting}
		<div class="sighting">
			<span class="plate mono">{plan.latestSighting.plate ?? '—'}</span>
			{#if plan.latestSighting.speedKmh != null}
				<span class="mono speed">{tOps(lang, 'watchSpeed', Math.round(plan.latestSighting.speedKmh))}</span>
			{/if}
			{#if plan.latestSighting.source === 'emulated_anpr'}
				<Badge variant="emulated" label={tOps(lang, 'labelEmulated')} />
			{/if}
			<span class="mono when">{tOps(lang, 'pursuitLatestSighting')} {plan.latestSighting.t?.slice(11, 19) ?? '—'}</span>
		</div>

		<ol class="chokes">
			{#each plan.chokes as choke (choke.junctionId)}
				<li class="choke" class:r1={choke.rank === 1} style="--i:{choke.rank}">
					<span class="rank mono">#{choke.rank}</span>
					<span class="jtype">{choke.junctionType}{choke.signalControl ? ` · ${tOps(lang, 'pursuitSignalized')}` : ''}</span>
					<span class="eta mono">{tOps(lang, 'pursuitEta')} {choke.graphCostSec != null ? Math.round(choke.graphCostSec) : '—'}</span>
					<span class="hold mono">{tOps(lang, 'pursuitHold')} {holdCost(choke.score, choke.chokeWeight)}</span>
				</li>
			{/each}
		</ol>

		<p class="caption">{tOps(lang, 'pursuitCaption')}</p>
	{:else}
		<EmptyState title={ops.interceptionNote ?? tOps(lang, 'pursuitNoneForEvent')} />
	{/if}
</Panel>

<style>
	.sighting {
		display: flex;
		align-items: baseline;
		gap: var(--v-space-3);
		flex-wrap: wrap;
		margin-bottom: var(--v-space-2);
	}

	.plate {
		font-size: var(--v-text-base);
		color: var(--v-ink-1);
	}

	.speed {
		font-size: var(--v-text-xs);
		color: var(--v-act-text);
	}

	.when {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-4);
		margin-left: auto;
	}

	.chokes {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: var(--v-space-1);
	}

	.choke {
		display: grid;
		grid-template-columns: auto 1fr auto auto;
		gap: var(--v-space-2);
		align-items: baseline;
		padding: var(--v-space-1) var(--v-space-2);
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-chip);
		background: var(--v-surface-2);
	}

	.choke.r1 {
		border-color: var(--v-act);
		background: var(--v-act-veil);
	}

	.rank {
		font-size: var(--v-text-sm);
		color: var(--v-ink-1);
	}

	.choke.r1 .rank {
		color: var(--v-act-text);
	}

	.jtype {
		font-size: var(--v-text-xs);
		color: var(--v-ink-2);
	}

	.eta,
	.hold {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.caption {
		margin: var(--v-space-2) 0 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-4);
	}

	.mono {
		font-family: var(--v-font-mono);
	}
</style>
