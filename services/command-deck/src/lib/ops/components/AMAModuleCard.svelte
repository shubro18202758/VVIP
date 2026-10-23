<script lang="ts">
	/**
	 * AMAModuleCard — the Active Movement Adaptation module's face on the
	 * wall. Renders the module descriptor from GET /api/v1/adaptive/module
	 * (stages, protocol floors, fail-closed flag; a labelled local copy of
	 * the SAME content when the endpoint is down — floors Z+ 180 / Z 120 /
	 * Y 60 s, X and none none, exactly the server's policy.py), the gated
	 * writer posture from GET /api/v1/signals/plans (writer state + the twin
	 * shadow-evals in flight, as a count), the engine's degradation tier,
	 * and the newest published parameter set's shadow-eval evidence (GET
	 * /api/v1/adaptive/overrides). A band that is not known renders UNKNOWN —
	 * never GREEN by default (a stale city band says since when its engine
	 * poll has been failing), and a band computed from a simulated feed
	 * carries the SIMULATED FEED tag. Title, summary and stage names read in
	 * the console's language (the server's English stays the EN copy).
	 * SURFACE ONLY — this card changes no adaptive behavior and degrades
	 * alone.
	 */
	import { onMount } from 'svelte';
	import Panel from '$lib/primitives/Panel.svelte';
	import Badge from '$lib/primitives/Badge.svelte';
	import BandChip from '$lib/primitives/BandChip.svelte';
	import { ops } from '$lib/ops/store.svelte';
	import { ORACLE_V1, POLL_MS } from '$lib/ops/endpoints';
	import { fmtAgeSince } from '$lib/ops/format';
	import { t, tOps } from '$lib/i18n/strings.ops';

	interface AmaStage {
		name: string;
		detail: string;
	}
	interface AmaModule {
		module: string;
		code: string;
		requirement: string;
		summary: string;
		stages: AmaStage[];
		protocol_floors_advance_closure_sec: Record<string, number | null>;
		endpoints: Record<string, string>;
		fail_closed: boolean;
	}

	/**
	 * Local fallback copy of the descriptor (marked when used) — the same
	 * content traffic_oracle/adaptive/surfacing.py AMA_MODULE serves.
	 */
	const FALLBACK: AmaModule = {
		module: 'Active Movement Adaptation',
		code: 'AMA',
		requirement: 'R9',
		summary:
			'The city’s live self-adjusting layer: every active corridor event is continuously classified into a situation band, the band moves through hysteresis before anything changes, protocol floors bound every published parameter, and any non-baseline plan must first pass a twin shadow-evaluation — failing closed to baseline.',
		stages: [
			{ name: 'Situation', detail: 'classify each active event into GREEN/AMBER/RED/BLACK' },
			{ name: 'Hysteresis', detail: 'a class change must persist through its dwell window' },
			{ name: 'Protocol floors', detail: 'advance_closure_sec floors Z+ 180 / Z 120 / Y 60 s on every publish' },
			{ name: 'Twin shadow-eval', detail: 'non-baseline must beat baseline in a paired twin run, else fail closed' },
			{ name: 'Audited persistence', detail: 'situation_log + param_overrides, mirrored to Valkey' },
			{ name: 'City signal plans', detail: 'the city band drives the gated signal-plan writer' },
		],
		protocol_floors_advance_closure_sec: { 'Z+': 180, Z: 120, Y: 60, X: null, none: null },
		endpoints: {},
		fail_closed: true,
	};

	const lang = $derived(ops.lang);

	let descriptor = $state<AmaModule | null>(null);
	let descriptorNote = $state<string | null>(null);

	let clock = $state(Date.now());
	$effect(() => {
		const id = setInterval(() => (clock = Date.now()), 1000);
		return () => clearInterval(id);
	});

	onMount(async () => {
		try {
			const res = await fetch(`${ORACLE_V1}/adaptive/module`, { signal: AbortSignal.timeout(6000) });
			if (!res.ok) throw new Error(`${res.status}`);
			descriptor = (await res.json()) as AmaModule;
			descriptorNote = null;
		} catch {
			descriptor = FALLBACK;
			descriptorNote = 'adaptive/module unreachable — card-fallback copy (same content as the server, labelled)';
		}
	});

	const stages = $derived(descriptor?.stages ?? FALLBACK.stages);
	const floors = $derived(descriptor?.protocol_floors_advance_closure_sec ?? FALLBACK.protocol_floors_advance_closure_sec);
	const shadowEval = $derived(ops.bandEvidence?.shadowEval ?? null);
	const floorApplied = $derived(ops.bandEvidence?.floorApplied ?? null);
	const failClosed = $derived(ops.bandEvidence?.failClosedReason ?? null);
	const gate = $derived(ops.signalPlans?.gate ?? null);
	const newest = $derived(ops.overrides[0] ?? null);

	/** Where the band came from, in words: scoped, SIMULATED FEED-tagged, or why it is unknown. */
	const sourceWord = $derived(ops.bandSourceKind === 'none' ? '—' : ops.bandSource);

	/** A localized copy of a server string when this pack has one; else the server's own. */
	function local(key: string, server: string | null | undefined): string {
		if (lang === 'en' && server) return server;
		const v = tOps(lang, key);
		return v === key ? (server ?? key) : v;
	}

	const stageKey = (name: string) => name.toLowerCase().replace(/[^a-z]+/g, '_').replace(/^_|_$/g, '');

	const writerState = $derived.by(() => {
		if (!gate) return null;
		if (gate.writerActive) return { variant: 'pending' as const, label: tOps(lang, 'gateWriterActive') };
		if ((gate.writerWaiters ?? 0) > 0) return { variant: 'pending' as const, label: tOps(lang, 'gateWriterQueued', gate.writerWaiters) };
		return { variant: 'neutral' as const, label: tOps(lang, 'gateWriterIdle') };
	});
</script>

<Panel
	title={descriptor ? local('amaTitle', descriptor.module) : t(lang, 'bandTitle')}
	sub={descriptor ? local('amaSummary', descriptor.summary?.slice(0, 140)) : t(lang, 'bandSub')}
	status={ops.bandPolledAt ? { state: 'live', updatedAt: ops.bandPolledAt } : null}
	pollMs={POLL_MS.band}
	lang={lang}
>
	<div class="ama-row" data-testid="ama-band" data-band={ops.band ?? 'UNKNOWN'}>
		{#if ops.band}
			<BandChip band={ops.band} size="md" />
		{:else}
			<span class="band-unknown mono">{tOps(lang, 'bandUnknown')}</span>
		{/if}
		<span class="mono age">
			{ops.bandUpdated ? tOps(lang, 'bandNewestReading', fmtAgeSince(ops.bandUpdated, clock)) : t(lang, 'never_updated')}
		</span>
		<span class="mono source" data-source={ops.bandSourceKind} title={ops.bandSourceKind}>[{sourceWord}]</span>
		{#if ops.bandSimulated === true}
			<span data-testid="ama-simulated"><Badge variant="emulated" label={tOps(lang, 'bandSimulatedTag')} title={tOps(lang, 'bandSimulatedHint')} /></span>
		{/if}
		{#if descriptor?.fail_closed}
			<Badge variant="neutral" label={tOps(lang, 'amaFailClosed')} title={tOps(lang, 'amaFailClosedHint')} />
		{/if}
	</div>

	{#if descriptorNote}<p class="fallback-note mono">{descriptorNote}</p>{/if}

	<section class="block">
		<h4>{tOps(lang, 'amaStages')}</h4>
		<ol class="stages">
			{#each stages as stage (stage.name)}
				<li class="stage" class:lit={!/persist/i.test(stage.name) || !failClosed}>
					<span class="stage-name" title={stage.name}>{local(`amaStage_${stageKey(stage.name)}`, stage.name)}</span>
					<span class="stage-detail">{local(`amaStageDetail_${stageKey(stage.name)}`, stage.detail)}</span>
				</li>
			{/each}
		</ol>
	</section>

	<section class="block">
		<h4>{tOps(lang, 'amaFloors')}</h4>
		<ul class="floors" data-testid="ama-floors">
			{#each Object.entries(floors) as [cls, sec] (cls)}
				<li data-class={cls} data-sec={sec ?? ''}><span class="cls mono">{cls}</span><span class="sec mono">{sec != null ? `${sec}s` : tOps(lang, 'amaNoFloor')}</span></li>
			{/each}
		</ul>
	</section>

	<section class="block">
		<h4>{tOps(lang, 'amaWriterPosture')}</h4>
		{#if ops.signalPlans}
			<div class="posture">
				<Badge
					variant={ops.signalPlans.writerEnabled ? 'live' : 'not-deployed'}
					label={ops.signalPlans.writerEnabled ? tOps(lang, 'gateWriterEnabled') : tOps(lang, 'gateWriterDisabled')}
					title={ops.signalPlans.writerFlagEnv ?? undefined}
				/>
				{#if writerState}<Badge variant={writerState.variant} label={writerState.label} />{/if}
				<Badge
					variant={(gate?.twinInFlight ?? 0) > 0 ? 'pending' : 'neutral'}
					label={tOps(lang, 'gateTwinInFlight', gate?.twinInFlight ?? 0)}
					title="signal-policy gate: twin shadow-evals holding the writer"
				/>
			</div>
		{:else}
			<p class="dim">{ops.signalPlansNote ?? tOps(lang, 'labelOffline')}</p>
		{/if}
		{#if ops.brain?.degradationTier}
			<p class="dim mono">{tOps(lang, 'amaDegradation', ops.brain.degradationTier)}{ops.brain.checkpointsPaused ? ` · ${tOps(lang, 'amaCheckpointsPaused')}` : ''}</p>
		{/if}
	</section>

	<section class="block">
		<h4>{tOps(lang, 'amaShadowEval')}</h4>
		<dl class="evidence mono">
			<div><dt>shadow_eval</dt><dd>{shadowEval ?? '—'}</dd></div>
			<div><dt>floor_applied</dt><dd>{floorApplied == null ? '—' : floorApplied ? 'yes' : 'no'}</dd></div>
			<div><dt>fail_closed</dt><dd>{failClosed ?? '—'}</dd></div>
			{#if newest}
				<div><dt>params</dt><dd>{newest.source ?? '—'} @ {newest.situationClass ?? '—'}</dd></div>
			{/if}
		</dl>
		{#if ops.overridesNote}<p class="dim mono">{ops.overridesNote}</p>{/if}
	</section>
</Panel>

<style>
	.ama-row {
		display: flex;
		align-items: center;
		gap: var(--v-space-2);
		flex-wrap: wrap;
	}

	.band-unknown {
		font-size: var(--v-text-xs);
		letter-spacing: var(--v-tracking-status);
		color: var(--v-ink-3);
		border: 1px dashed var(--v-line-2);
		border-radius: var(--v-radius-chip);
		padding: var(--v-space-1) var(--v-space-2);
	}

	.age {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.source {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.fallback-note {
		margin: var(--v-space-1) 0 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.block {
		border-top: 1px solid var(--v-line-1);
		padding-top: var(--v-space-2);
		margin-top: var(--v-space-2);
	}

	.block h4 {
		margin: 0 0 var(--v-space-2);
		font-size: var(--v-text-2xs);
		font-weight: var(--v-weight-status);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
		color: var(--v-ink-3);
	}

	.stages {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.stage {
		display: flex;
		justify-content: space-between;
		gap: var(--v-space-2);
		padding: 1px 0;
		border-bottom: 1px solid var(--v-line-1);
	}

	.stage-name {
		font-size: var(--v-text-xs);
		color: var(--v-ink-2);
		white-space: nowrap;
	}

	.stage.lit .stage-name {
		color: var(--v-ink-1);
	}

	.stage-detail {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
		text-align: right;
	}

	.floors {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-wrap: wrap;
		gap: var(--v-space-1);
	}

	.floors li {
		display: inline-flex;
		align-items: baseline;
		gap: var(--v-space-1);
		padding: 1px var(--v-space-2);
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-chip);
	}

	.cls {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.sec {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-2);
	}

	.posture {
		display: flex;
		gap: var(--v-space-2);
		flex-wrap: wrap;
	}

	.evidence {
		margin: 0;
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.evidence div {
		display: flex;
		justify-content: space-between;
		gap: var(--v-space-2);
	}

	.evidence dt {
		color: var(--v-ink-3);
		font-size: var(--v-text-2xs);
	}

	.evidence dd {
		margin: 0;
		color: var(--v-ink-2);
		font-size: var(--v-text-2xs);
		text-align: right;
		overflow-wrap: anywhere;
	}

	.dim {
		margin: var(--v-space-1) 0 0;
		color: var(--v-ink-3);
		font-size: var(--v-text-2xs);
	}

	.mono {
		font-family: var(--v-font-mono);
	}
</style>
