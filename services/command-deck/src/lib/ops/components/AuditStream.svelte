<script lang="ts">
	/**
	 * AuditStream — the decision-audit narrative as a VIEW OVER SERVER DATA
	 * (contract C-C): the selected live event's GET /api/v1/events/{id}/audit,
	 * else the whole city's GET /api/v1/city/feed (the operator can follow
	 * either). Every server row keeps the actor_kind the oracle recorded:
	 * named officers ride the act rail, machine rows (rules engine, cameras,
	 * AMA, brain) sit dim mono, system rows (emulator, event-closed walks)
	 * dimmer still — an oracle, twin or writer reply is never shown as human.
	 * Emulated rows carry the EMULATED pill. Local rows (refusals before the
	 * wire, probe output) are labelled by their rung; an officer's echo
	 * retires the moment its server row lands, so a reload shows the same
	 * narrative — and an echo about another event, or about a city-scope
	 * write, waits in the CITY view, never in this event's. Every row and
	 * the probe's answer print under the plate rule (C6). The feed follows
	 * the newest row (autoscroll keyed on the row count) and PAUSES under
	 * the pointer so the ledger can be read.
	 */
	import Panel from '$lib/primitives/Panel.svelte';
	import Button from '$lib/primitives/Button.svelte';
	import AuditRow from '$lib/primitives/AuditRow.svelte';
	import EmptyState from '$lib/primitives/EmptyState.svelte';
	import { ops, isLiveUuidEvent } from '$lib/ops/store.svelte';
	import { POLL_MS } from '$lib/ops/endpoints';
	import { t } from '$lib/i18n/strings';
	import { tOps } from '$lib/i18n/strings.ops';
	import type { AuditEntry } from '$lib/ops/types';

	let probeText = $state(
		'Audit the corridor: summarize the current situation band, closure parameters and any anomalies for the selected event.',
	);

	let feedEl: HTMLDivElement | null = $state(null);
	let paused = $state(false);
	let probeOpen = $state(false);

	$effect(() => {
		// Follow the newest row as the view grows — unless the pointer is reading.
		const n = ops.audit.length;
		if (feedEl && !paused && n >= 0) feedEl.scrollTop = feedEl.scrollHeight;
	});

	const lang = $derived(ops.lang);
	const selected = $derived(ops.selectedEvent);
	const canFollowEvent = $derived(isLiveUuidEvent(selected));

	const serverCount = $derived(ops.audit.filter((a) => a.serverId).length);
	const localCount = $derived(ops.audit.length - serverCount);
	const humanCount = $derived(ops.audit.filter((a) => a.serverId && a.actorKind === 'human').length);

	function rungLabel(): string {
		switch (ops.auditRung) {
			case 'ws-event':
				return t(lang, 'auditRungWsEvent');
			case 'ws-convoy':
				return t(lang, 'auditRungWsConvoy');
			case 'poll':
				return t(lang, 'auditRungPoll');
			case 'connecting':
				return '…';
			default:
				return t(lang, 'auditRungUnavailable');
		}
	}

	const viewLabel = $derived(
		ops.auditView === 'event'
			? tOps(lang, 'auditViewEvent', selected ? ops.text(selected.name).slice(0, 48) : '—')
			: ops.auditView === 'city'
				? tOps(lang, 'auditViewCity')
				: ops.auditView === 'legacy'
					? tOps(lang, 'auditViewLegacy')
					: tOps(lang, 'auditViewNone'),
	);

	function kindOf(a: AuditEntry): 'human' | 'machine' | 'system' {
		return a.actorKind ?? 'machine';
	}

	function sourceOf(a: AuditEntry): string {
		return a.serverId ? (a.kind ?? 'server') : a.source;
	}
</script>

{#snippet headRight()}
	<div class="scope" role="group" aria-label={tOps(lang, 'auditScopeLabel')}>
		<button
			type="button"
			class="seg"
			class:on={ops.auditScope === 'event'}
			disabled={!canFollowEvent}
			title={canFollowEvent ? undefined : tOps(lang, 'auditScopeEventNone')}
			onclick={() => ops.setAuditScope('event')}
			data-testid="audit-scope-event"
		>
			{tOps(lang, 'auditScopeEvent')}
		</button>
		<button
			type="button"
			class="seg"
			class:on={ops.auditScope === 'city' || !canFollowEvent}
			onclick={() => ops.setAuditScope('city')}
			data-testid="audit-scope-city"
		>
			{tOps(lang, 'auditScopeCity')}
		</button>
	</div>
	<span
		class="rung-chip mono"
		class:unavailable={ops.auditRung === 'unavailable'}
		class:connecting={ops.auditRung === 'connecting'}
		title="the audit ladder's live rung (adapter-node ships the 2 s poll rung by design)"
	>
		{t(lang, 'auditLadder', rungLabel())}
	</span>
{/snippet}

<Panel
	title={t(lang, 'auditTitle')}
	sub={tOps(lang, 'auditSubServer')}
	status={ops.auditServerUpdated ? { state: ops.auditView === 'legacy' ? 'mixed' : 'live', updatedAt: ops.auditServerUpdated } : null}
	pollMs={ops.auditView === 'city' ? POLL_MS.feed : POLL_MS.audit}
	lang={lang}
	fill
	{...{ 'header-right': headRight }}
>
	<div class="viewline mono" data-testid="audit-view" data-view={ops.auditView} data-server-rows={serverCount} data-human-rows={humanCount}>
		<span class="view">{viewLabel}</span>
		<span class="counts">{tOps(lang, 'auditCounts', serverCount, humanCount, localCount)}</span>
		{#if ops.auditNote}<span class="note">{ops.auditNote}</span>{/if}
		{#if ops.probeDone}<span>{tOps(lang, 'auditProbeTiers', ops.probeDone.tiersUsed.join(', ') || '—')}{ops.probeDone.degraded ? ` · ${tOps(lang, 'auditProbeDegraded')}` : ''}</span>{/if}
		<span class="spacer"></span>
		{#if !ops.officerOn}<span class="masked">{tOps(lang, 'platesMasked')}</span>{/if}
		<button type="button" class="probe-toggle" class:on={probeOpen} onclick={() => (probeOpen = !probeOpen)} aria-expanded={probeOpen}>
			{t(lang, 'auditProbe')} {probeOpen ? '▾' : '▸'}
		</button>
	</div>

	{#if probeOpen}
		<div class="probe" data-probe-open>
			<input
				class="probe-input"
				type="text"
				bind:value={probeText}
				placeholder={t(lang, 'auditProbe')}
				aria-label={t(lang, 'auditProbe')}
				title={tOps(lang, 'auditProbeHintScoped', selected && selected.source !== 'offline-fixture' ? ops.text(selected.name).slice(0, 60) : '—')}
			/>
			<Button variant="secondary" size="sm" busy={ops.auditProbeRunning} onclick={() => void ops.runAuditProbe(probeText)}>
				{t(lang, 'auditProbe')}
			</Button>
			<Button variant="ghost" size="sm" onclick={() => ops.clearAudit()}>{tOps(lang, 'auditClearLocal')}</Button>
		</div>
		<p class="hint">{tOps(lang, 'auditProbeHintScoped', selected && selected.source !== 'offline-fixture' ? ops.text(selected.name).slice(0, 60) : '—')}</p>
		{#if ops.ndjsonText}
			<!-- The brain's tools return raw event titles: the answer prints under the plate rule (C6). -->
			<details class="ndjson" open>
				<summary>{tOps(lang, 'auditProbeAnswer')}</summary>
				<pre class="mono" data-testid="audit-probe-answer">{ops.text(ops.ndjsonText)}</pre>
			</details>
		{/if}
	{/if}

	<div
		class="feed"
		role="log"
		aria-label={t(lang, 'auditTitle')}
		bind:this={feedEl}
		onpointerenter={() => (paused = true)}
		onpointerleave={() => (paused = false)}
		data-testid="audit-feed"
	>
		{#if ops.audit.length === 0}
			<EmptyState title={t(lang, 'auditEmpty')} />
		{:else}
			{#each ops.audit as entry (entry.id)}
				<AuditRow
					ts={entry.ts}
					actor={entry.actor.startsWith('mcp:') ? entry.actor.slice(4) : entry.actor}
					actorKind={kindOf(entry)}
					level={entry.level}
					text={ops.text(entry.text)}
					detail={ops.detail(entry.detail)}
					source={sourceOf(entry)}
					emulated={entry.emulated === true}
					simulated={entry.simulated === true}
					rowId={entry.serverId ?? null}
					lang={lang}
				/>
			{/each}
		{/if}
	</div>
</Panel>

<style>
	.scope {
		display: inline-flex;
		border: 1px solid var(--v-line-2);
		border-radius: var(--v-radius-pill);
		overflow: hidden;
	}

	.seg {
		font: inherit;
		font-size: var(--v-text-2xs);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
		padding: 1px var(--v-space-2);
		color: var(--v-ink-3);
		background: transparent;
		border: none;
		cursor: pointer;
	}

	.seg + .seg {
		border-left: 1px solid var(--v-line-2);
	}

	.seg.on {
		color: var(--v-data);
		background: color-mix(in srgb, var(--v-data) 12%, transparent);
	}

	.seg:disabled {
		cursor: not-allowed;
		opacity: 0.5;
	}

	.rung-chip {
		font-size: var(--v-text-2xs);
		padding: 1px var(--v-space-2);
		border-radius: var(--v-radius-pill);
		border: 1px solid var(--v-band-green);
		color: var(--v-band-green);
		white-space: nowrap;
	}

	.rung-chip.connecting {
		border-color: var(--v-band-amber);
		color: var(--v-band-amber);
	}

	.rung-chip.unavailable {
		border-color: var(--v-band-red);
		color: var(--v-band-red);
	}

	.viewline {
		display: flex;
		flex-wrap: wrap;
		gap: var(--v-space-3);
		align-items: baseline;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
		margin-bottom: var(--v-space-1);
	}

	.viewline .view {
		color: var(--v-ink-2);
	}

	.viewline .note {
		color: var(--v-band-amber);
	}

	.viewline .masked {
		color: var(--v-ink-3);
	}

	.spacer {
		flex: 1;
	}

	.probe-toggle {
		font: inherit;
		font-size: var(--v-text-2xs);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
		color: var(--v-ink-2);
		background: transparent;
		border: 1px solid var(--v-line-2);
		border-radius: var(--v-radius-btn);
		padding: 1px var(--v-space-2);
		cursor: pointer;
	}

	.probe-toggle.on,
	.probe-toggle:hover {
		border-color: var(--v-line-select);
		color: var(--v-ink-1);
	}

	.viewline,
	.probe,
	.hint,
	.ndjson {
		flex: none;
	}

	.probe {
		display: flex;
		gap: var(--v-space-2);
		margin-bottom: var(--v-space-1);
	}

	.probe-input {
		flex: 1;
		min-width: 0;
		height: var(--v-ctl-sm);
		padding: 0 var(--v-space-2);
		font: inherit;
		font-size: var(--v-text-xs);
		color: var(--v-ink-1);
		background: var(--v-surface-sink);
		border: 1px solid var(--v-line-2);
		border-radius: var(--v-radius-btn);
	}

	.hint {
		margin: 0 0 var(--v-space-1);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	/* The scroller: fills what the band leaves (wall) or caps at 320 px. */
	.feed {
		flex: 1 1 auto;
		display: flex;
		flex-direction: column;
		gap: 1px;
		min-height: 64px;
		max-height: 320px;
		overflow-y: auto;
		overscroll-behavior: contain;
		border-top: 1px solid var(--v-line-1);
		padding-top: var(--v-space-1);
	}

	.ndjson summary {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
		cursor: pointer;
	}

	/* The operator's answer — read, not metadata: ink-1 on the panel, a readable box. */
	.ndjson pre {
		margin: var(--v-space-1) 0 0;
		max-height: 132px;
		overflow: auto;
		font-size: var(--v-text-xs);
		line-height: 1.45;
		color: var(--v-ink-1);
		background: var(--v-surface-sink);
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-chip);
		padding: var(--v-space-1) var(--v-space-2);
		white-space: pre-wrap;
	}

	.mono {
		font-family: var(--v-font-mono);
	}
</style>
