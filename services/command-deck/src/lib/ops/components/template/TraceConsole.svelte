<script lang="ts">
	/**
	 * TraceConsole — the template's floating AI-trace console, rebuilt as a
	 * filter over the REAL decision-audit ledger (decisions_log through the
	 * ops audit ladder): typed chips per audit level, mono machine rows,
	 * act-rail human rows — the same FeedItem contract the /ops stream
	 * uses, so there is ONE audit renderer, not two. Row text and evidence
	 * payloads mask plates unless a named duty officer is on console (C6).
	 */
	import FeedItem from '$lib/primitives/FeedItem.svelte';
	import { ops } from '$lib/ops/store.svelte';
	import { t } from '$lib/i18n/strings';
	import { displayDetail, displayText } from '$lib/gods-eye/plates';
	import type { AuditEntry } from '$lib/ops/types';

	type Level = AuditEntry['level'] | 'all';

	const LEVELS: ReadonlyArray<{ id: Level; token: string }> = [
		{ id: 'all', token: '--v-ink-2' },
		{ id: 'decision', token: '--v-data' },
		{ id: 'tool', token: '--v-tool' },
		{ id: 'band', token: '--v-band-amber' },
		{ id: 'model', token: '--v-ink-4' },
		{ id: 'error', token: '--v-band-red' },
	];

	// 'decision' by default: the right rail already carries the raw tail —
	// the console opens on the operator-relevant rung, one click shows all.
	let filter = $state<Level>('decision');
	let open = $state(true);

	const rows = $derived(
		(filter === 'all' ? ops.audit : ops.audit.filter((a) => a.level === filter)).slice(-40).reverse(),
	);

	/**
	 * The actor as this narrow console shows it: the mcp: tool prefix dropped
	 * and a long officer label cut to fit (the row text names the officer in
	 * full) — a 40-character name must never squeeze the row's text into a
	 * one-letter column.
	 */
	function railActor(actor: string): string {
		const a = actor.startsWith('mcp:') ? actor.slice(4) : actor;
		return a.length > 26 ? `${a.slice(0, 25)}…` : a;
	}

	function actorKind(a: AuditEntry): 'human' | 'machine' {
		if (a.source === 'operator' || a.actor === 'operator') return 'human';
		if (a.actor.includes('Officer') || a.actor.includes('officer')) return 'human';
		return 'machine';
	}
</script>

<aside class="console" class:closed={!open} data-template="trace-console" aria-label={t(ops.lang, 'tpl_trace')}>
	<button type="button" class="head" onclick={() => (open = !open)} aria-expanded={open}>
		<span class="head-title">{t(ops.lang, 'tpl_trace')}</span>
		<span class="head-count mono">{ops.audit.length}</span>
		<span class="chev" aria-hidden="true">{open ? '▾' : '▴'}</span>
	</button>

	{#if open}
		<div class="chips" role="group" aria-label="trace filters">
			{#each LEVELS as lv (lv.id)}
				<button
					type="button"
					class="chip"
					class:on={filter === lv.id}
					style={`--lv: var(${lv.token})`}
					onclick={() => (filter = lv.id)}
				>
					{lv.id.toUpperCase()}
				</button>
			{/each}
		</div>

		<div class="feed" role="log">
			{#if rows.length === 0}
				<p class="empty mono">— no entries at this level —</p>
			{:else}
				{#each rows as entry (entry.id)}
					<FeedItem
						ts={entry.ts}
						actor={railActor(entry.actor)}
						actorKind={actorKind(entry)}
						level={entry.level}
						text={displayText(entry.text, ops.officerOn)}
						detail={displayDetail(entry.detail, ops.officerOn)}
						lang={ops.lang}
					/>
				{/each}
			{/if}
		</div>
	{/if}
</aside>

<style>
	.console {
		/* clear of the cockpit's right rail when it is open (--float-right) */
		position: absolute;
		right: var(--float-right, var(--v-space-3));
		bottom: var(--float-bottom, var(--v-space-3));
		width: 360px;
		max-width: calc(100vw - 48px);
		display: flex;
		flex-direction: column;
		background: var(--v-glass-bg);
		-webkit-backdrop-filter: blur(var(--v-glass-blur)) saturate(1.35);
		backdrop-filter: blur(var(--v-glass-blur)) saturate(1.35);
		border: 1px solid var(--v-glass-border);
		border-radius: var(--v-radius-panel);
		box-shadow: 0 10px 32px var(--v-veil);
		overflow: hidden;
		z-index: 30;
	}

	.head {
		display: flex;
		align-items: center;
		gap: var(--v-space-2);
		padding: var(--v-space-2) var(--v-space-3);
		background: transparent;
		border: none;
		border-bottom: 1px solid var(--v-glass-border);
		color: var(--v-ink-1);
		cursor: pointer;
		text-align: left;
	}

	.head-title {
		font-family: var(--v-font-display);
		font-size: var(--v-text-xs);
		font-weight: 700;
		letter-spacing: 0.14em;
		text-transform: uppercase;
	}

	.head-count {
		margin-left: auto;
		font-size: var(--v-text-2xs);
		color: var(--v-act-text);
		font-variant-numeric: tabular-nums;
	}

	.chev {
		color: var(--v-ink-3);
		font-size: var(--v-text-xs);
	}

	.chips {
		display: flex;
		flex-wrap: wrap;
		gap: var(--v-space-1);
		padding: var(--v-space-2) var(--v-space-3) 0;
	}

	.chip {
		height: var(--v-ctl-sm);
		padding: 0 var(--v-space-2);
		border-radius: var(--v-radius-pill);
		border: 1px solid var(--v-line-2);
		background: transparent;
		color: var(--v-ink-3);
		font-family: var(--v-font-mono);
		font-size: var(--v-text-2xs);
		letter-spacing: var(--v-tracking-status);
		cursor: pointer;
	}

	.chip.on {
		color: var(--lv);
		border-color: var(--lv);
		background: color-mix(in srgb, var(--lv) 12%, transparent);
	}

	.feed {
		max-height: 240px;
		overflow-y: auto;
		padding: var(--v-space-2) var(--v-space-3) var(--v-space-3);
		display: flex;
		flex-direction: column;
		gap: var(--v-space-1);
	}

	.empty {
		margin: 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.mono {
		font-family: var(--v-font-mono);
	}
</style>
