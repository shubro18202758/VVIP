<script lang="ts">
	/**
	 * FusionRail — the bottom rail of VEER CITY COMMAND: the whole city's
	 * narrative, one time-ordered feed (GET /api/v1/city/feed?since=, every
	 * 3 s — hud/feed.svelte.ts), split onto two rails:
	 *
	 *   HUMAN DECISIONS   rows a named person made (approvals, dispatch
	 *                     transitions, event lifecycle, signal-plan commits) —
	 *                     the act-orange rail, the officer named
	 *   MACHINE · SYSTEM  ANPR hits, rule-engine proposals, AMA band
	 *                     transitions and shadow verdicts, brain decisions (with
	 *                     tier + confidence), system bookkeeping
	 *
	 * Filter chips: EVENTS · ENFORCEMENT · ANPR · DISPATCH · SIGNALS · AMA ·
	 * BRAIN. A row's click selects its entity (the row's first URN) and flies
	 * the camera to it; rows touching the selected entity stay marked.
	 * Emulated rows carry the EMULATED pill. In REPLAY the rail becomes the
	 * tape's scrubber + narrative (ReplayBar), never mixed with live rows.
	 */
	import { onMount, onDestroy } from 'svelte';
	import { ops } from '$lib/ops/store.svelte';
	import { t } from '$lib/i18n/strings';
	import { simulatedFeedOf } from '$lib/ops/client';
	import { selection } from '../entities/selection.svelte';
	import { FEED_KINDS, parseUrn, type FeedKind, type FeedRow } from '../city-api';
	import { fusionFeed } from './feed.svelte';
	import { replay } from './replay.svelte';
	import { ageOf, fmtAge, istStamp } from './format';
	import ReplayBar from './ReplayBar.svelte';

	const LABEL: Record<FeedKind, string> = {
		event: 'EVENTS',
		enforcement: 'ENFORCEMENT',
		anpr: 'ANPR',
		dispatch: 'DISPATCH',
		signals: 'SIGNALS',
		ama: 'AMA',
		brain: 'BRAIN',
	};

	/** Rows rendered per rail (the feed keeps up to 400). */
	const RENDER_CAP = 80;

	let enabled = $state<Record<FeedKind, boolean>>({
		event: true,
		enforcement: true,
		anpr: true,
		dispatch: true,
		signals: true,
		ama: true,
		brain: true,
	});

	let now = $state(Date.now());
	let clock: ReturnType<typeof setInterval> | null = null;

	onMount(() => {
		fusionFeed.start();
		clock = setInterval(() => (now = Date.now()), 1000);
	});

	onDestroy(() => {
		if (clock) clearInterval(clock);
		fusionFeed.stop();
	});

	const lang = $derived(ops.lang);
	const officer = $derived(ops.officerOn);
	const counts = $derived(fusionFeed.counts());
	const visible = $derived(fusionFeed.rows.filter((r) => enabled[r.kind]));
	const human = $derived(visible.filter((r) => r.actorKind === 'human').slice(0, RENDER_CAP));
	const machine = $derived(visible.filter((r) => r.actorKind !== 'human').slice(0, RENDER_CAP));
	const newestAge = $derived(fusionFeed.rows[0] ? ageOf(fusionFeed.rows[0].t, now) : null);

	function primaryUrn(r: FeedRow): string | null {
		return r.urns.find((u) => parseUrn(u)) ?? null;
	}

	function rowText(r: FeedRow): string {
		const plate = typeof r.props.plate === 'string' ? r.props.plate : null;
		const masked = typeof r.props.plate_masked === 'string' ? r.props.plate_masked : null;
		return officer && plate && masked ? r.text.split(masked).join(plate) : r.text;
	}

	/** C-Q: the row's numbers came from a SIMULATED feed (props.simulated_feed / data_quality). */
	function simulated(r: FeedRow): boolean {
		return simulatedFeedOf(r.props);
	}

	function machineDetail(r: FeedRow): string {
		const tier = typeof r.props.tier === 'string' ? r.props.tier : null;
		const conf = typeof r.props.confidence === 'number' ? r.props.confidence : null;
		return [tier ? `tier ${tier}` : null, conf != null ? `conf ${conf.toFixed(2)}` : null].filter(Boolean).join(' · ');
	}

	function open(r: FeedRow) {
		const urn = primaryUrn(r);
		if (urn) selection.select(urn, { fly: true, source: 'rail' });
	}

	function toggle(kind: FeedKind) {
		enabled = { ...enabled, [kind]: !enabled[kind] };
	}

	function setMode(mode: 'live' | 'replay') {
		if (mode === 'replay' && replay.mode !== 'replay') void replay.enter();
		if (mode === 'live' && replay.mode !== 'live') replay.exit();
	}
</script>

<div class="rail" data-testid="fusion-rail" data-mode={replay.mode}>
	<header class="r-head">
		<h2>{t(lang, 'cc_rail')}</h2>
		<div class="mode" role="group" aria-label="live or replay">
			<button type="button" class:on={replay.mode === 'live'} onclick={() => setMode('live')} title={t(lang, 'cc_live_hint')}>
				<i class="live-dot" aria-hidden="true"></i> {t(lang, 'cc_live')}
			</button>
			<button type="button" class:on={replay.mode === 'replay'} onclick={() => setMode('replay')} data-testid="replay-toggle">
				◐ {t(lang, 'cc_replay')}
			</button>
		</div>
		{#if replay.mode === 'live'}
			<div class="filters" role="group" aria-label="feed filters">
				{#each FEED_KINDS as kind (kind)}
					<button
						type="button"
						class="f-chip k-{kind}"
						class:off={!enabled[kind]}
						aria-pressed={enabled[kind]}
						data-kind={kind}
						onclick={() => toggle(kind)}
					>
						{LABEL[kind]} <span class="mono">{counts[kind]}</span>
					</button>
				{/each}
			</div>
			<span class="r-meta mono" class:warn={fusionFeed.status === 'down' || fusionFeed.status === 'not-deployed'} title={fusionFeed.note ?? undefined}>
				{fusionFeed.rows.length} {t(lang, 'cc_rows')} · {newestAge != null ? `${fmtAge(newestAge)}` : '—'}
				{#if fusionFeed.status !== 'ok' && fusionFeed.status !== 'idle'}· {fusionFeed.status}{/if}
			</span>
		{/if}
	</header>

	{#if replay.mode === 'replay'}
		<ReplayBar />
	{:else}
		<div class="lanes">
			<section class="lane human-lane" aria-label={t(lang, 'cc_human')}>
				<h3>{t(lang, 'cc_human')} <span class="mono">{human.length}</span></h3>
				<ol class="rows">
					{#each human as r (r.id)}
						{@const urn = primaryUrn(r)}
						<li>
							<button
								type="button"
								class="f-row human"
								class:linked={!!selection.current && r.urns.includes(selection.current)}
								class:fresh={fusionFeed.freshIds.has(r.id)}
								data-feed-id={r.id}
								data-urn={urn ?? ''}
								disabled={!urn}
								onclick={() => open(r)}
							>
								<span class="t mono">{istStamp(r.t, now)}</span>
								<span class="k k-{r.kind} mono">{LABEL[r.kind]}</span>
								<span class="who">{r.actor ?? 'officer'}</span>
								<span class="txt">{rowText(r)}</span>
								{#if r.source === 'emulated'}<span class="pill">{t(lang, 'cc_emulated')}</span>{/if}
								{#if simulated(r)}<span class="pill sim" data-testid="rail-simulated">{t(lang, 'cc_simulated_feed')}</span>{/if}
							</button>
						</li>
					{:else}
						<li class="empty mono">{t(lang, 'cc_rail_empty')}</li>
					{/each}
				</ol>
			</section>
			<section class="lane machine-lane" aria-label={t(lang, 'cc_machine')}>
				<h3>{t(lang, 'cc_machine')} <span class="mono">{machine.length}</span></h3>
				<ol class="rows">
					{#each machine as r (r.id)}
						{@const urn = primaryUrn(r)}
						<li>
							<button
								type="button"
								class="f-row {r.actorKind} sev-{r.severity}"
								class:linked={!!selection.current && r.urns.includes(selection.current)}
								class:fresh={fusionFeed.freshIds.has(r.id)}
								data-feed-id={r.id}
								data-urn={urn ?? ''}
								disabled={!urn}
								onclick={() => open(r)}
							>
								<span class="t mono">{istStamp(r.t, now)}</span>
								<span class="k k-{r.kind} mono">{LABEL[r.kind]}</span>
								<span class="who mono">{r.actor ?? r.actorKind}{machineDetail(r) ? ` · ${machineDetail(r)}` : ''}</span>
								<span class="txt">{rowText(r)}</span>
								{#if r.source === 'emulated'}<span class="pill">{t(lang, 'cc_emulated')}</span>{/if}
								{#if simulated(r)}<span class="pill sim" data-testid="rail-simulated">{t(lang, 'cc_simulated_feed')}</span>{/if}
							</button>
						</li>
					{:else}
						<li class="empty mono">{t(lang, 'cc_rail_empty')}</li>
					{/each}
				</ol>
			</section>
		</div>
	{/if}
</div>

<style>
	.rail {
		display: flex;
		flex-direction: column;
		gap: var(--v-space-1);
		height: 100%;
		min-height: 0;
	}

	.r-head {
		display: flex;
		align-items: center;
		gap: var(--v-space-3);
		flex-wrap: wrap;
	}

	h2 {
		margin: 0;
		font-family: var(--v-font-display);
		font-size: var(--v-text-sm);
		font-weight: 700;
		letter-spacing: 0.14em;
		text-transform: uppercase;
		color: var(--v-ink-1);
	}

	.mode {
		display: inline-flex;
		border: 1px solid var(--v-line-2);
		border-radius: var(--v-radius-pill);
		overflow: hidden;
	}

	.mode button {
		display: inline-flex;
		align-items: center;
		gap: 5px;
		padding: 2px var(--v-space-3);
		background: transparent;
		border: none;
		color: var(--v-ink-3);
		font-family: var(--v-font-mono);
		font-size: var(--v-text-2xs);
		letter-spacing: 0.1em;
		cursor: pointer;
	}

	.mode button.on {
		background: var(--v-surface-3);
		color: var(--v-ink-1);
	}

	.live-dot {
		width: 7px;
		height: 7px;
		border-radius: var(--v-radius-pill);
		background: var(--v-band-green);
	}

	.mode button:not(.on) .live-dot {
		background: var(--v-ink-4);
	}

	.filters {
		display: flex;
		flex-wrap: wrap;
		gap: 4px;
	}

	.f-chip {
		font-family: var(--v-font-mono);
		font-size: var(--v-text-2xs);
		letter-spacing: 0.08em;
		padding: 2px 7px;
		border-radius: var(--v-radius-chip);
		border: 1px solid var(--v-line-2);
		background: color-mix(in srgb, var(--v-canvas) 70%, transparent);
		color: var(--v-ink-1);
		cursor: pointer;
	}

	.f-chip span {
		color: var(--v-data);
	}

	.f-chip.off {
		opacity: 0.4;
	}

	.r-meta {
		margin-left: auto;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.r-meta.warn {
		color: var(--v-band-amber);
	}

	.lanes {
		flex: 1;
		min-height: 0;
		display: grid;
		grid-template-columns: minmax(0, 0.42fr) minmax(0, 0.58fr);
		gap: var(--v-space-2);
	}

	.lane {
		display: flex;
		flex-direction: column;
		min-height: 0;
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-btn);
		background: color-mix(in srgb, var(--v-surface-sink) 55%, transparent);
	}

	.human-lane {
		border-top: 2px solid var(--v-act);
	}

	.machine-lane {
		border-top: 2px solid var(--v-data);
	}

	h3 {
		margin: 0;
		padding: 3px var(--v-space-2);
		font-size: var(--v-text-2xs);
		font-weight: var(--v-weight-status);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
		color: var(--v-ink-3);
		border-bottom: 1px solid var(--v-line-1);
	}

	h3 span {
		color: var(--v-ink-1);
	}

	.rows {
		list-style: none;
		margin: 0;
		padding: 2px;
		overflow-y: auto;
		flex: 1;
		min-height: 0;
		scrollbar-width: thin;
	}

	.f-row {
		display: grid;
		grid-template-columns: auto auto minmax(0, auto) minmax(0, 1fr) auto;
		align-items: baseline;
		gap: var(--v-space-2);
		width: 100%;
		padding: 2px var(--v-space-2);
		background: transparent;
		border: none;
		border-left: 2px solid transparent;
		border-radius: var(--v-radius-chip);
		color: var(--v-ink-2);
		font: inherit;
		font-size: var(--v-text-2xs);
		text-align: left;
		cursor: pointer;
	}

	.f-row:hover:not(:disabled) {
		background: var(--v-surface-2);
	}

	.f-row:disabled {
		cursor: default;
	}

	.f-row.human {
		border-left-color: var(--v-act);
	}

	.f-row.machine {
		border-left-color: var(--v-data);
	}

	.f-row.system {
		border-left-color: var(--v-ink-4);
	}

	.f-row.sev-critical .txt {
		color: var(--v-ink-1);
	}

	.f-row.linked {
		background: color-mix(in srgb, var(--v-data) 14%, transparent);
	}

	.f-row.fresh {
		animation: arrive var(--v-dur-wipe) var(--v-ease);
	}

	@keyframes arrive {
		from {
			background: var(--v-act-veil);
		}
	}

	.t {
		color: var(--v-ink-3);
		white-space: nowrap;
	}

	.k {
		font-size: var(--v-text-2xs);
		letter-spacing: 0.06em;
		color: var(--v-ink-3);
	}

	.k-enforcement,
	.k-anpr {
		color: var(--v-act-text);
	}

	.k-ama {
		color: var(--v-band-amber);
	}

	.k-brain {
		color: var(--v-tool);
	}

	.k-dispatch,
	.k-event {
		color: var(--v-data);
	}

	.who {
		color: var(--v-ink-1);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		max-width: 22ch;
	}

	.f-row.human .who {
		color: var(--v-act-text);
		font-weight: 600;
	}

	.txt {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.pill.sim {
		color: var(--v-tool);
		border-color: var(--v-tool);
	}

	.pill {
		font-family: var(--v-font-mono);
		font-size: var(--v-text-2xs);
		letter-spacing: 0.06em;
		padding: 0 5px;
		border: 1px dashed var(--v-ink-3);
		border-radius: var(--v-radius-pill);
		color: var(--v-ink-2);
		white-space: nowrap;
	}

	.empty {
		padding: var(--v-space-2);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.mono {
		font-family: var(--v-font-mono);
	}

	@media (prefers-reduced-motion: reduce) {
		.f-row.fresh {
			animation: none;
		}
	}
</style>
