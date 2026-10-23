<script lang="ts">
	/**
	 * PursuitPanel — the ranked interception choke plan for the selected
	 * pursuit event (GET /events/{id}/interception-plan): ranks 1→N with
	 * their junction names, graph-cost ETA and hold cost; which way the
	 * vehicle is going and on what basis (its heading, a one-way's legal
	 * direction — or observed AGAINST a one-way), the legal onward segments
	 * the ranking starts from (with road names), and the plan's honesty
	 * flags: a STALE sighting (older than the oracle's 10 min bound — the
	 * vehicle may be beyond every junction listed) and junctions the vehicle
	 * has LIKELY PASSED already. Plates masked unless an officer is named.
	 * Captioned with the backend's own words: it assists the operator, it
	 * never dispatches on its own.
	 */
	import Panel from '$lib/primitives/Panel.svelte';
	import Badge from '$lib/primitives/Badge.svelte';
	import EmptyState from '$lib/primitives/EmptyState.svelte';
	import { ops } from '$lib/ops/store.svelte';
	import { POLL_MS } from '$lib/ops/endpoints';
	import { fmtAgeSince, fmtDuration, istClock } from '$lib/ops/format';
	import { tOps } from '$lib/i18n/strings.ops';

	const lang = $derived(ops.lang);
	const plan = $derived(ops.interception);

	let clock = $state(Date.now());
	$effect(() => {
		const id = setInterval(() => (clock = Date.now()), 1000);
		return () => clearInterval(id);
	});

	/** Hold cost = the choke score as a percentage (the honest proxy). */
	function holdCost(score: number | null, weight: number | null): string {
		if (score == null || weight == null) return '—';
		return (score * 100).toFixed(1);
	}

	function junctionLabel(id: number): string {
		const fromPlan = plan?.chokes.find((c) => c.junctionId === id)?.name;
		return `J${id} ${fromPlan ?? ops.junctionName(id) ?? ''}`.trim();
	}

	const dir = $derived(plan?.direction ?? null);
	const onward = $derived(
		dir
			? dir.onwardSegmentIds.map((id) => {
					const road = ops.roadName(id);
					return road ? `#${id} ${road}` : `#${id}`;
				})
			: [],
	);
	const staleAfter = $derived(plan?.staleAfterSec ?? 600);
</script>

<Panel
	title={tOps(lang, 'pursuitTitle')}
	sub={tOps(lang, 'pursuitCaption')}
	status={ops.interceptionUpdated ? { state: ops.interceptionNote ? 'stale' : 'live', updatedAt: ops.interceptionUpdated } : null}
	pollMs={POLL_MS.interception}
	lang={lang}
>
	{#if plan && plan.latestSighting}
		<div class="sighting" data-testid="pursuit-sighting" data-stale={plan.stale ? '1' : '0'}>
			<span class="plate mono">{ops.plate(plan.latestSighting.plate) ?? '—'}</span>
			{#if plan.latestSighting.speedKmh != null}
				<span class="mono speed">{tOps(lang, 'watchSpeed', Math.round(plan.latestSighting.speedKmh))}</span>
			{/if}
			{#if plan.latestSighting.source === 'emulated_anpr'}
				<Badge variant="emulated" label={tOps(lang, 'labelEmulated')} />
			{/if}
			<span class="mono when">
				{tOps(lang, 'pursuitLatestSighting')} {plan.latestSighting.t ? `${istClock(plan.latestSighting.t)} IST` : '—'}
				{#if plan.latestSighting.cameraId}· {plan.latestSighting.cameraId}{/if}
				· {tOps(lang, 'pursuitAgo', fmtAgeSince(plan.latestSighting.t, clock))}
			</span>
		</div>

		{#if plan.stale}
			<p class="stale" data-testid="pursuit-stale">{tOps(lang, 'pursuitStale', fmtDuration(staleAfter))}</p>
		{/if}

		{#if dir}
			<div class="direction mono" data-testid="pursuit-direction" data-basis={dir.basis} data-against={dir.againstOneway ? '1' : '0'}>
				<span>
					{tOps(lang, 'pursuitHeading', dir.towardsJunctionIds.map(junctionLabel).join(' / ') || '—', dir.basis)}
				</span>
				{#if dir.againstOneway}<Badge variant="rejected" label={tOps(lang, 'pursuitAgainstOneway')} />{/if}
				{#if plan.seedSegmentId != null}
					<span class="dim">{tOps(lang, 'pursuitSeedSegment')} #{plan.seedSegmentId}{ops.roadName(plan.seedSegmentId) ? ` ${ops.roadName(plan.seedSegmentId)}` : ''}</span>
				{/if}
				{#if onward.length}<span class="dim">{tOps(lang, 'pursuitOnward')} {onward.join(' · ')}</span>{/if}
			</div>
		{/if}

		<ol class="chokes">
			{#each plan.chokes as choke (choke.junctionId)}
				<li
					class="choke"
					class:r1={choke.rank === 1}
					class:passed={choke.likelyPassed}
					style="--i:{choke.rank}"
					data-testid="pursuit-choke"
					data-rank={choke.rank}
					data-junction={choke.junctionId}
					data-likely-passed={choke.likelyPassed ? '1' : '0'}
				>
					<span class="rank mono">#{choke.rank}</span>
					<span class="jtype">
						<span class="jname">{choke.name ?? ops.junctionName(choke.junctionId) ?? `J${choke.junctionId}`}</span>
						<span class="dim">{choke.junctionType}{choke.signalControl ? ` · ${tOps(lang, 'pursuitSignalized')}` : ''}</span>
						{#if choke.downstream}<Badge variant="act" label={tOps(lang, 'pursuitDownstream')} />{/if}
						{#if choke.likelyPassed}<Badge variant="stale" label={tOps(lang, 'pursuitLikelyPassed')} />{/if}
					</span>
					<span class="eta mono">{tOps(lang, 'pursuitEta')} {choke.graphCostSec != null ? Math.round(choke.graphCostSec) : '—'}</span>
					<span class="hold mono">{tOps(lang, 'pursuitHold')} {holdCost(choke.score, choke.chokeWeight)}</span>
				</li>
			{/each}
		</ol>

		<p class="caption">{plan.note ?? tOps(lang, 'pursuitCaption')}</p>
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
		color: var(--v-ink-3);
		margin-left: auto;
	}

	.stale {
		margin: 0 0 var(--v-space-2);
		font-size: var(--v-text-xs);
		color: var(--v-ink-2);
		border-top: 2px solid var(--v-band-amber);
		padding-top: var(--v-space-1);
	}

	.direction {
		display: flex;
		flex-wrap: wrap;
		gap: var(--v-space-2);
		align-items: center;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-2);
		margin-bottom: var(--v-space-2);
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

	.choke.passed {
		opacity: 0.6;
	}

	.rank {
		font-size: var(--v-text-sm);
		color: var(--v-ink-1);
	}

	.choke.r1 .rank {
		color: var(--v-act-text);
	}

	.jtype {
		display: inline-flex;
		flex-wrap: wrap;
		gap: var(--v-space-1);
		align-items: baseline;
		font-size: var(--v-text-xs);
		color: var(--v-ink-2);
		min-width: 0;
	}

	.jname {
		color: var(--v-ink-1);
	}

	.eta,
	.hold {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.dim {
		color: var(--v-ink-3);
		font-size: var(--v-text-2xs);
	}

	.caption {
		margin: var(--v-space-2) 0 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.mono {
		font-family: var(--v-font-mono);
	}
</style>
