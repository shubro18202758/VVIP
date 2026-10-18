<script lang="ts">
	/**
	 * AMAModuleCard — the Active Movement Adaptation module's face on the
	 * wall. Renders the module descriptor from GET /api/v1/adaptive/module
	 * (stages, protocol floors, fail-closed flag; labelled local fallback
	 * copy when the endpoint is down) PLUS the gated writer posture from
	 * GET /api/v1/signals/plans and the live band evidence. SURFACE ONLY —
	 * this card changes no adaptive behavior and degrades alone.
	 */
	import { onMount } from 'svelte';
	import Panel from '$lib/primitives/Panel.svelte';
	import Badge from '$lib/primitives/Badge.svelte';
	import BandChip from '$lib/primitives/BandChip.svelte';
	import { ops } from '$lib/ops/store.svelte';
	import { ORACLE_V1 } from '$lib/ops/endpoints';
	import { t, tOps } from '$lib/i18n/strings.ops';
	import type { Band } from '$lib/design/tokens';

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

	/** Local fallback copy of the descriptor (marked when used). */
	const FALLBACK: AmaModule = {
		module: 'Active Movement Adaptation',
		code: 'AMA',
		requirement: 'REG-14 · situation → hysteresis → floors → twin → audited writes',
		summary:
			'Adapts corridor policy to the live situation with hysteresis and protocol floors; every candidate policy change is twin shadow-evaluated and fails closed when the twin is not calibrated.',
		stages: [
			{ name: 'situation', detail: 'observe + classify (adaptive)' },
			{ name: 'hysteresis', detail: 'hold candidate band 4 ticks' },
			{ name: 'floors', detail: 'protocol minimums apply' },
			{ name: 'twin', detail: 'shadow-eval candidate policy' },
			{ name: 'persist', detail: 'audited write (append-only)' },
		],
		protocol_floors_advance_closure_sec: { 'Z+': 900, Z: 720, Y: 600, X: 480, none: 300 },
		endpoints: {},
		fail_closed: true,
	};

	const lang = $derived(ops.lang);

	let descriptor = $state<AmaModule | null>(null);
	let descriptorNote = $state<string | null>(null);

	onMount(async () => {
		try {
			const res = await fetch(`${ORACLE_V1}/adaptive/module`, { signal: AbortSignal.timeout(6000) });
			if (!res.ok) throw new Error(`${res.status}`);
			descriptor = (await res.json()) as AmaModule;
			descriptorNote = null;
		} catch {
			descriptor = FALLBACK;
			descriptorNote = 'adaptive/module unreachable — card-fallback copy (same content, labelled)';
		}
	});

	const stages = $derived(descriptor?.stages ?? FALLBACK.stages);
	const floors = $derived(descriptor?.protocol_floors_advance_closure_sec ?? FALLBACK.protocol_floors_advance_closure_sec);
	const shadowEval = $derived(ops.bandEvidence?.shadowEval ?? null);
	const floorApplied = $derived(ops.bandEvidence?.floorApplied ?? null);
	const failClosed = $derived(ops.bandEvidence?.failClosedReason ?? null);
</script>

<Panel
	title={descriptor?.module ?? t(lang, 'bandTitle')}
	sub={descriptor?.summary?.slice(0, 120) ?? t(lang, 'bandSub')}
	status={ops.bandUpdated ? { state: 'live', updatedAt: ops.bandUpdated } : null}
	lang={lang}
>
	<div class="ama-row">
		<BandChip band={(ops.band ?? 'GREEN') as Band} size="md" />
		<span class="mono age">{ops.bandUpdated ? tOps(lang, 'updatedAgo', Math.max(0, Math.round((Date.now() - ops.bandUpdated) / 1000))) : t(lang, 'never_updated')}</span>
		<span class="mono source">[{ops.bandSource === 'none' ? '—' : ops.bandSource}]</span>
		{#if descriptor?.fail_closed}
			<Badge variant="neutral" label="fail-closed" title="candidate policies never persist when the twin is not calibrated" />
		{/if}
	</div>

	{#if descriptorNote}<p class="fallback-note mono">{descriptorNote}</p>{/if}

	<section class="block">
		<h4>{tOps(lang, 'amaStages')}</h4>
		<ol class="stages">
			{#each stages as stage, i (stage.name)}
				<li class="stage" class:lit={stage.name !== 'persist' || !failClosed}>
					<span class="stage-name">{stage.name}</span>
					<span class="stage-detail">{stage.detail}</span>
				</li>
			{/each}
		</ol>
	</section>

	<section class="block">
		<h4>{tOps(lang, 'amaFloors')}</h4>
		<ul class="floors">
			{#each Object.entries(floors) as [cls, sec] (cls)}
				<li><span class="cls mono">{cls}</span><span class="sec mono">{sec != null ? `${sec}s` : '—'}</span></li>
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
				{#if ops.signalPlans.gate.twinInFlight}
					<Badge variant="pending" label="twin in flight" title="signal-policy gate: a twin shadow-eval holds the writer" />
				{/if}
			</div>
		{:else}
			<p class="dim">{ops.signalPlansNote ?? tOps(lang, 'labelOffline')}</p>
		{/if}
	</section>

	<section class="block">
		<h4>{tOps(lang, 'amaShadowEval')}</h4>
		<dl class="evidence mono">
			<div><dt>shadow_eval</dt><dd>{shadowEval ?? '—'}</dd></div>
			<div><dt>floor_applied</dt><dd>{floorApplied == null ? '—' : floorApplied ? 'yes' : 'no'}</dd></div>
			<div><dt>fail_closed</dt><dd>{failClosed ?? '—'}</dd></div>
		</dl>
	</section>
</Panel>

<style>
	.ama-row {
		display: flex;
		align-items: center;
		gap: var(--v-space-2);
		flex-wrap: wrap;
	}

	.age {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.source {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-4);
	}

	.fallback-note {
		margin: var(--v-space-1) 0 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-4);
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
	}

	.stage.lit .stage-name {
		color: var(--v-ink-1);
	}

	.stage-detail {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-4);
		text-align: right;
	}

	.floors {
		list-style: none;
		margin: 0;
		padding: 0;
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(90px, 1fr));
		gap: var(--v-space-1);
	}

	.floors li {
		display: flex;
		justify-content: space-between;
		gap: var(--v-space-1);
	}

	.cls {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-4);
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
		color: var(--v-ink-4);
		font-size: var(--v-text-2xs);
	}

	.evidence dd {
		margin: 0;
		color: var(--v-ink-2);
		font-size: var(--v-text-2xs);
	}

	.dim {
		margin: 0;
		color: var(--v-ink-4);
		font-size: var(--v-text-2xs);
	}

	.mono {
		font-family: var(--v-font-mono);
	}
</style>
