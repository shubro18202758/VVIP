<script lang="ts">
	/**
	 * EventDossierDrawer — the selected event's dossier: metadata, phases,
	 * the audit_tail narrative (AuditRows), a trilingual advisory preview
	 * (one fact, three languages), and the ranked choke plan when the event
	 * is a pursuit. Built on the EventDossier frame primitive; the page owns
	 * placement (right column drawer).
	 */
	import EventDossier from '$lib/primitives/EventDossier.svelte';
	import Badge from '$lib/primitives/Badge.svelte';
	import AuditRow from '$lib/primitives/AuditRow.svelte';
	import PursuitPanel from './PursuitPanel.svelte';
	import { ops } from '$lib/ops/store.svelte';
	import { t, tOps } from '$lib/i18n/strings.ops';
	import { localizeNumber } from '$lib/i18n/strings';
	import type { Band } from '$lib/design/tokens';

	const lang = $derived(ops.lang);
	const ev = $derived(ops.selectedEvent);

	/** The narrative: audit rows for the selected event (last 12). */
	const narrative = $derived(ops.audit.slice(-12));

	/** One fact, three languages — the advisory preview beat. */
	const windowFact = $derived.by(() => {
		if (!ev) return null;
		const phase = ev.phases.find((p) => p.state === 'active') ?? ev.phases[0] ?? null;
		const start = phase?.startTime ? phase.startTime.slice(11, 16) : null;
		const end = phase?.endTime ? phase.endTime.slice(11, 16) : null;
		return start ? { start, end } : null;
	});
</script>

<EventDossier
	event={ev ? { eventId: ev.eventId, name: ev.name, band: (ev.band ?? null) as Band | null, state: ev.state } : null}
	lang={lang}
>
	{#if ev}
		{#snippet meta()}
			<dl class="meta mono">
				<div><dt>kind</dt><dd>{ev.eventKind}</dd></div>
				<div><dt>protection</dt><dd>{ev.protectionClass}</dd></div>
				<div><dt>crowd</dt><dd>{ev.crowdClass}</dd></div>
				<div><dt>state</dt><dd>{ev.state}</dd></div>
				<div><dt>source</dt><dd>{ev.source}</dd></div>
				<div><dt>spine</dt><dd>{ev.routeSegmentIds.join(' · ')}</dd></div>
			</dl>
		{/snippet}

		{#snippet phases()}
			<ul class="phases">
				{#each ev.phases as ph, i (i)}
					<li>
						<span class="ph-label">{ph.label}</span>
						<span class="ph-window mono">{ph.startTime?.slice(11, 16) ?? '—'} → {ph.endTime?.slice(11, 16) ?? 'open'}</span>
						<span class="ph-state mono">{ph.state}</span>
					</li>
				{/each}
			</ul>
		{/snippet}

		{#snippet audit()}
			{#if narrative.length === 0}
				<p class="dim">{t(lang, 'auditEmpty')}</p>
			{:else}
				<div class="narrative">
					{#each narrative as entry (entry.id)}
						<AuditRow
							ts={entry.ts}
							actor={entry.actor}
							actorKind={entry.source === 'operator' ? 'human' : 'machine'}
							level={entry.level}
							text={entry.text}
							detail={entry.detail}
							source={entry.source}
							lang={lang}
						/>
					{/each}
				</div>
			{/if}
		{/snippet}

		{#snippet advisory()}
			{#if windowFact}
				<div class="advisory">
					<p lang="en">Road holds expected {windowFact.start}–{windowFact.end ?? 'open'} · {ev.name}.</p>
					<p lang="hi">इस दौरान रोड रुकेगा {localizeNumber(windowFact.start, 'hi')}–{windowFact.end ? localizeNumber(windowFact.end, 'hi') : 'खुला'} · {ev.name}।</p>
					<p lang="gu">આ સમયે રોડ રોકાશે {localizeNumber(windowFact.start, 'gu')}–{windowFact.end ? localizeNumber(windowFact.end, 'gu') : 'ખુલ્લું'} · {ev.name}.</p>
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
	{:else}
		{#snippet meta()}<p class="dim">{t(lang, 'opsNoEvents')}</p>{/snippet}
	{/if}
</EventDossier>

<style>
	.meta {
		margin: 0;
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
		gap: var(--v-space-1);
	}

	.meta div {
		display: flex;
		justify-content: space-between;
		gap: var(--v-space-2);
	}

	.meta dt {
		color: var(--v-ink-4);
		font-size: var(--v-text-2xs);
	}

	.meta dd {
		margin: 0;
		color: var(--v-ink-2);
		font-size: var(--v-text-2xs);
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
		color: var(--v-ink-4);
	}

	.narrative {
		display: flex;
		flex-direction: column;
		gap: 1px;
		max-height: 200px;
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
		color: var(--v-ink-4);
	}

	.mono {
		font-family: var(--v-font-mono);
	}
</style>
