<script lang="ts">
	/**
	 * EventDossierDrawer — the selected event's dossier: metadata, phases,
	 * the C-F verb rail, its OWN audit narrative (the server's GET
	 * /api/v1/events/{id}/audit rows — human decisions on the act rail —
	 * plus this console's notes about the event), a trilingual advisory
	 * preview (one fact, three languages — withheld for the operator-only
	 * kinds, pursuits and crime, which never get citizen copy) and the
	 * ranked choke plan when the event is a pursuit. Every time is IST through the board's own window
	 * formatter, so the dossier and the board always print the same window.
	 * Built on the EventDossier frame primitive; the page owns placement.
	 */
	import EventDossier from '$lib/primitives/EventDossier.svelte';
	import Badge from '$lib/primitives/Badge.svelte';
	import AuditRow from '$lib/primitives/AuditRow.svelte';
	import PursuitPanel from './PursuitPanel.svelte';
	import EventLifecycleVerbs from './EventLifecycleVerbs.svelte';
	import { ops, isLiveUuidEvent } from '$lib/ops/store.svelte';
	import { fmtWindowIst, istHm } from '$lib/ops/format';
	import { t, tOps } from '$lib/i18n/strings.ops';
	import { kindLabel, localizeNumber } from '$lib/i18n/strings';
	import { isPublicKind } from '$lib/citizen/holds';
	import type { Band } from '$lib/design/tokens';
	import type { AuditEntry } from '$lib/ops/types';

	const lang = $derived(ops.lang);

	/** An enum value in the console's language ('planning' → योजना); the raw value when unmapped. */
	function enumWord(prefix: string, value: string | null | undefined): string {
		if (!value) return '—';
		const key = `${prefix}${value}`;
		const v = tOps(lang, key);
		return v === key ? value.replace(/_/g, ' ') : v;
	}
	const ev = $derived(ops.selectedEvent);

	let clock = $state(Date.now());
	$effect(() => {
		const id = setInterval(() => (clock = Date.now()), 1000);
		return () => clearInterval(id);
	});

	/**
	 * The narrative: the selected event's server audit (newest last), plus
	 * this console's local notes that name the event (refusals, echoes) —
	 * not the audit probe's transcript, which stays in the stream.
	 */
	const narrative = $derived.by((): AuditEntry[] => {
		if (!ev) return [];
		const server = ops.eventAuditFor === ev.eventId ? ops.eventAudit : [];
		const local = ops.audit.filter((a) => !a.serverId && a.eventId === ev.eventId && a.source !== 'ndjson-chat');
		return [...server, ...local].sort((a, b) => a.ts - b.ts || a.id - b.id).slice(-40);
	});

	let narrativeEl: HTMLDivElement | null = $state(null);
	$effect(() => {
		// keep the newest rows in view as the narrative grows
		void narrative.length;
		if (narrativeEl) narrativeEl.scrollTop = narrativeEl.scrollHeight;
	});

	/** One fact, three languages — the advisory preview beat (IST). */
	const windowFact = $derived.by(() => {
		if (!ev) return null;
		const phase = ev.phases.find((p) => p.state === 'active') ?? ev.phases[0] ?? null;
		const start = phase?.startTime ?? ev.windowStart;
		const end = phase?.endTime ?? ev.windowEnd;
		return start ? { start: istHm(start), end: end ? istHm(end) : null } : null;
	});

	const spine = $derived(
		ev
			? ev.routeSegmentIds.map((id) => {
					const road = ops.roadName(id);
					return road ? `${id} ${road}` : String(id);
				})
			: [],
	);

	function actorKindOf(entry: AuditEntry): 'human' | 'machine' | 'system' {
		return entry.actorKind ?? (entry.source === 'operator' && entry.level === 'decision' ? 'human' : 'machine');
	}
</script>

<!--
	The section snippets must be DIRECT children of <EventDossier> to reach it
	as props — declared under an {#if} inside the tag they would only be local
	to the children block and never render. So the branch wraps the frame.
-->
{#if ev}
	<EventDossier
		event={{ eventId: ev.eventId, name: ops.text(ev.name), band: (ev.band ?? null) as Band | null, state: ev.status ?? ev.state }}
		lang={lang}
	>
		{#snippet meta()}
			<!-- Labels and enum values read in the console's language; the raw code rides in the title. -->
			<dl class="meta mono">
				<div><dt>{tOps(lang, 'dossierKind')}</dt><dd title={ev.eventKind}>{kindLabel(lang, ev.eventKind)}</dd></div>
				<div><dt>{tOps(lang, 'dossierProtection')}</dt><dd title={ev.protectionClass}>{ev.protectionClass === 'none' ? tOps(lang, 'dossierProtectionNone') : ev.protectionClass}</dd></div>
				<div><dt>{tOps(lang, 'dossierCrowd')}</dt><dd title={ev.crowdClass}>{enumWord('dossierCrowd_', ev.crowdClass)}</dd></div>
				<div><dt>{tOps(lang, 'dossierStatus')}</dt><dd title={ev.status ?? ev.state}>{enumWord('dossierStatus_', ev.status ?? ev.state)}</dd></div>
				<div>
					<dt>{tOps(lang, 'dossierSource')}</dt>
					<dd title={ev.source}>
						{enumWord('dossierSource_', ev.source)}
						{#if ev.emulated}<Badge variant="emulated" label={tOps(lang, 'labelEmulated')} />{/if}
					</dd>
				</div>
				{#if ev.metadata?.runId}<div><dt>{tOps(lang, 'dossierRun')}</dt><dd>#{ev.metadata.runId}{ev.metadata.mode ? ` · ${enumWord('dossierMode_', ev.metadata.mode)}` : ''}</dd></div>{/if}
				{#if ev.movementId}<div><dt>{tOps(lang, 'dossierMovement')}</dt><dd title={ev.movementId}>{ev.movementId.slice(0, 8)}</dd></div>{/if}
				<div class="wide"><dt>{tOps(lang, 'dossierSpine')}</dt><dd>{spine.join(' · ') || '—'}</dd></div>
			</dl>
			{#if isLiveUuidEvent(ev)}
				<div class="verbs-block">
					<EventLifecycleVerbs event={ev} />
				</div>
			{/if}
		{/snippet}

		{#snippet phases()}
			<ul class="phases">
				{#each ev.phases as ph, i (i)}
					<li>
						<span class="ph-label">{ph.label}</span>
						<span class="ph-window mono" data-testid="dossier-phase-window" data-phase-index={i}>{fmtWindowIst(ph.startTime, ph.endTime, clock)}</span>
						<span class="ph-state mono" title={ph.state}>{enumWord('dossierStatus_', ph.state)}</span>
					</li>
				{:else}
					<li class="dim">—</li>
				{/each}
			</ul>
		{/snippet}

		{#snippet audit()}
			{#if narrative.length === 0}
				<p class="dim">{isLiveUuidEvent(ev) ? tOps(lang, 'dossierNoAudit') : tOps(lang, 'dossierFixtureAudit')}</p>
			{:else}
				<div class="narrative" bind:this={narrativeEl} data-testid="dossier-narrative">
					{#each narrative as entry (entry.id)}
						<AuditRow
							ts={entry.ts}
							actor={entry.actor.startsWith('mcp:') ? entry.actor.slice(4) : entry.actor}
							actorKind={actorKindOf(entry)}
							level={entry.level}
							text={ops.text(entry.text)}
							detail={ops.detail(entry.detail)}
							source={entry.kind ?? entry.source}
							emulated={entry.emulated === true}
							rowId={entry.serverId ?? null}
							lang={lang}
						/>
					{/each}
				</div>
			{/if}
		{/snippet}

		{#snippet advisory()}
			{#if !isPublicKind(ev.eventKind)}
				<p class="dim" data-testid="dossier-advisory-withheld">{tOps(lang, 'dossierAdvisoryWithheld')}</p>
			{:else if windowFact}
				<div class="advisory">
					<p lang="en">Road holds expected {windowFact.start}–{windowFact.end ?? 'open'} IST · {ops.text(ev.name)}.</p>
					<p lang="hi">इस दौरान रोड रुकेगा {localizeNumber(windowFact.start, 'hi')}–{windowFact.end ? localizeNumber(windowFact.end, 'hi') : 'खुला'} IST · {ops.text(ev.name)}।</p>
					<p lang="gu">આ સમયે રોડ રોકાશે {localizeNumber(windowFact.start, 'gu')}–{windowFact.end ? localizeNumber(windowFact.end, 'gu') : 'ખુલ્લું'} IST · {ops.text(ev.name)}.</p>
				</div>
			{:else}
				<p class="dim">—</p>
			{/if}
		{/snippet}

		{#snippet footer()}
			{#if ev.eventKind === 'vehicle_pursuit' || ops.interception}
				<PursuitPanel />
			{/if}
		{/snippet}
	</EventDossier>
{:else}
	<EventDossier event={null} emptyTitle={t(lang, 'opsNoEvents')} lang={lang} />
{/if}

<style>
	/* each fact stacks its label over its value (side by side, a 140 px
	   column wrapped 'Vehicle / pursuit' and 'none (public / event)' into a
	   ragged grid at 1920 px) — whole words, aligned left, in even cells */
	.meta {
		margin: 0;
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
		gap: var(--v-space-2) var(--v-space-3);
	}

	.meta div {
		display: flex;
		flex-direction: column;
		gap: 1px;
		min-width: 0;
	}

	.meta div.wide {
		grid-column: 1 / -1;
	}

	.meta dt {
		color: var(--v-ink-3);
		font-size: var(--v-text-2xs);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
	}

	.meta dd {
		margin: 0;
		color: var(--v-ink-1);
		font-size: var(--v-text-xs);
		text-align: left;
		overflow-wrap: anywhere;
		display: inline-flex;
		flex-wrap: wrap;
		gap: var(--v-space-1);
		align-items: center;
		justify-content: flex-start;
	}

	.verbs-block {
		margin-top: var(--v-space-2);
		display: flex;
		flex-direction: column;
		gap: var(--v-space-1);
	}

	.phases {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.phases li {
		display: flex;
		justify-content: space-between;
		gap: var(--v-space-2);
		font-size: var(--v-text-2xs);
		border-bottom: 1px solid var(--v-line-1);
		padding: 1px 0;
	}

	.ph-label {
		color: var(--v-ink-2);
	}

	.ph-window {
		color: var(--v-ink-3);
	}

	.ph-state {
		color: var(--v-ink-3);
	}

	.narrative {
		display: flex;
		flex-direction: column;
		gap: 1px;
		max-height: 220px;
		overflow-y: auto;
	}

	.advisory {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.advisory p {
		margin: 0;
		font-size: var(--v-text-xs);
		color: var(--v-ink-2);
	}

	.dim {
		margin: 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.mono {
		font-family: var(--v-font-mono);
	}
</style>
