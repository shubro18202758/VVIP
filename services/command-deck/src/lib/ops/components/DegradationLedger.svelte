<script lang="ts">
	/**
	 * DegradationLedger — the recurring-failure taxonomy mined from the
	 * brain's decisions_log audit trail (oracle /ops/degradation-taxonomy).
	 * Self-criticism surface: every row is a cause the audit log ACTUALLY
	 * carries (Wave-1 latch firings, tier-0 misses, tactical fallbacks,
	 * operator alerts, failed turns) with its recurrence count, the
	 * first→last seen window, and the sample ref naming one real row
	 * (click a row for the full entry). An empty log renders the honest
	 * EmptyState — never invented entries — and a down endpoint degrades
	 * to a labelled note over the last-good ledger.
	 */
	import Panel from '$lib/primitives/Panel.svelte';
	import FeedItem from '$lib/primitives/FeedItem.svelte';
	import EmptyState from '$lib/primitives/EmptyState.svelte';
	import { ops } from '$lib/ops/store.svelte';
	import { tOps } from '$lib/i18n/strings.ops';
	import type { DegradationTaxonomyEntry } from '$lib/ops/types';

	const lang = $derived(ops.lang);
	const taxonomy = $derived(ops.degradation);

	const panelStatus = $derived(
		ops.degradationUpdated
			? {
					state: (ops.degradationLive === false ? 'stale' : 'live') as 'stale' | 'live',
					updatedAt: ops.degradationUpdated,
				}
			: null,
	);

	/** Cause → feed level: hard failures error, withheld/outage signals band,
	 * everything else (misses, refusals, limitations) info. */
	function levelFor(cause: string): 'error' | 'band' | 'info' {
		if (
			cause.startsWith('checkpoint_fallback:') ||
			cause.startsWith('orchestrator:') ||
			cause.includes('unavailable') ||
			cause.endsWith(':error')
		) {
			return 'error';
		}
		if (cause.startsWith('novelty_latch:') || cause.startsWith('alert:novel')) return 'band';
		return 'info';
	}

	function clock(ts: number | null): string {
		if (ts == null) return '—';
		const d = new Date(ts * 1000);
		const hh = String(d.getHours()).padStart(2, '0');
		const mm = String(d.getMinutes()).padStart(2, '0');
		const ss = String(d.getSeconds()).padStart(2, '0');
		return `${hh}:${mm}:${ss}`;
	}

	function rowText(entry: DegradationTaxonomyEntry): string {
		const first = entry.firstSeen != null ? clock(entry.firstSeen) : '?';
		return `${tOps(lang, 'ledgerTimes', entry.count)} · ${tOps(lang, 'ledgerFirstSeen', first)}`;
	}
</script>

<Panel
	title={tOps(lang, 'ledgerTitle')}
	sub={taxonomy
		? tOps(lang, 'ledgerScanLine', taxonomy.rowsScanned, taxonomy.contextsScanned)
		: tOps(lang, 'ledgerSub')}
	status={panelStatus}
	lang={lang}
>
	{#if taxonomy && taxonomy.entries.length}
		<div class="feed" role="log" aria-label={tOps(lang, 'ledgerTitle')}>
			{#each taxonomy.entries as entry (entry.cause)}
				<FeedItem
					ts={(entry.lastSeen ?? entry.firstSeen ?? 0) * 1000}
					actor={entry.cause}
					actorKind="machine"
					level={levelFor(entry.cause)}
					text={rowText(entry)}
					detail={entry}
					lang={lang}
				>
					{#snippet right()}
						<span class="sample mono" title={tOps(lang, 'ledgerSampleRef')}>{entry.sampleRef}</span>
					{/snippet}
				</FeedItem>
			{/each}
		</div>
		{#if taxonomy.truncated}
			<p class="foot-note">{tOps(lang, 'ledgerTruncated')}</p>
		{/if}
	{:else}
		<EmptyState
			title={ops.degradationNote ?? tOps(lang, 'ledgerEmpty')}
			hint={taxonomy ? tOps(lang, 'ledgerEmptyHint') : tOps(lang, 'ledgerSub')}
		/>
	{/if}
</Panel>

<style>
	.feed {
		display: flex;
		flex-direction: column;
		gap: 1px;
		max-height: 280px;
		overflow-y: auto;
		border-top: 1px solid var(--v-line-1);
		padding-top: var(--v-space-2);
	}

	.sample {
		font-size: 10px;
		color: var(--v-ink-4);
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-pill);
		padding: 0 var(--v-space-1);
		align-self: center;
		flex-shrink: 0;
		max-width: 18ch;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.foot-note {
		margin: var(--v-space-2) 0 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-4);
	}

	.mono {
		font-family: var(--v-font-mono);
	}
</style>
