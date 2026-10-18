<script lang="ts">
	/**
	 * AuditStream — the immutable decision-audit narrative, rebuilt on
	 * FeedItem/AuditRow primitives: human-actor rows carry the 200 ms act
	 * flash + persistent rail, machine rows sit dim mono; appends settle,
	 * never slide; the feed auto-scrolls but PAUSES under the pointer (the
	 * ledger must be readable while it grows); every row is labelled with
	 * its ladder rung (poll-rung-aware — adapter-node cannot upgrade WS, so
	 * the 2 s poll rung is the shipped behaviour in the container).
	 */
	import Panel from '$lib/primitives/Panel.svelte';
	import Button from '$lib/primitives/Button.svelte';
	import AuditRow from '$lib/primitives/AuditRow.svelte';
	import EmptyState from '$lib/primitives/EmptyState.svelte';
	import { ops } from '$lib/ops/store.svelte';
	import { t } from '$lib/i18n/strings';
	import type { AuditEntry } from '$lib/ops/types';

	let probeText = $state(
		'Audit the corridor: summarize the current situation band, closure parameters and any anomalies for the selected event.',
	);

	let feedEl: HTMLDivElement | null = $state(null);
	let paused = $state(false);

	$effect(() => {
		// Keep the newest entries in view — unless the pointer is reading.
		if (feedEl && !paused) feedEl.scrollTop = feedEl.scrollHeight;
	});

	function rungLabel(): string {
		switch (ops.auditRung) {
			case 'ws-event':
				return t(ops.lang, 'auditRungWsEvent');
			case 'ws-convoy':
				return t(ops.lang, 'auditRungWsConvoy');
			case 'poll':
				return t(ops.lang, 'auditRungPoll');
			case 'connecting':
				return '…';
			default:
				return t(ops.lang, 'auditRungUnavailable');
		}
	}

	/** Human actors are people; everything else (incl. mcp:) is machine. */
	function actorKind(a: AuditEntry): 'human' | 'machine' {
		if (a.source === 'operator' || a.actor === 'operator') return 'human';
		if (a.actor.includes('Officer') || a.actor.includes('officer')) return 'human';
		return 'machine';
	}
</script>

{#snippet headRight()}
	<span
		class="rung-chip mono"
		class:unavailable={ops.auditRung === 'unavailable'}
		class:connecting={ops.auditRung === 'connecting'}
		title="the audit ladder's live rung (adapter-node ships the 2 s poll rung by design)"
	>
		{t(ops.lang, 'auditLadder', rungLabel())}
	</span>
{/snippet}

<Panel
	title={t(ops.lang, 'auditTitle')}
	sub={t(ops.lang, 'auditSub')}
	lang={ops.lang}
	{...{ 'header-right': headRight }}
>
	<div class="probe">
		<input
			class="probe-input"
			type="text"
			bind:value={probeText}
			placeholder={t(ops.lang, 'auditProbe')}
			aria-label={t(ops.lang, 'auditProbe')}
		/>
		<Button variant="secondary" size="sm" busy={ops.auditProbeRunning} onclick={() => void ops.runAuditProbe(probeText)}>
			{t(ops.lang, 'auditProbe')}
		</Button>
		<Button variant="ghost" size="sm" onclick={() => ops.clearAudit()}>{t(ops.lang, 'auditClear')}</Button>
	</div>
	<p class="hint">{t(ops.lang, 'auditProbeHint')}</p>

	<div
		class="feed"
		role="log"
		aria-label={t(ops.lang, 'auditTitle')}
		bind:this={feedEl}
		onpointerenter={() => (paused = true)}
		onpointerleave={() => (paused = false)}
	>
		{#if ops.audit.length === 0}
			<EmptyState title={t(ops.lang, 'auditEmpty')} />
		{:else}
			{#each ops.audit as entry (entry.id)}
				<AuditRow
					ts={entry.ts}
					actor={entry.actor.startsWith('mcp:') ? entry.actor.slice(4) : entry.actor}
					actorKind={actorKind(entry)}
					level={entry.level}
					text={entry.text}
					detail={entry.detail}
					source={entry.source}
					lang={ops.lang}
				/>
			{/each}
		{/if}
	</div>

	{#if ops.ndjsonText}
		<details class="ndjson">
			<summary>Gemma output tail (NDJSON tokens)</summary>
			<pre class="mono">{ops.ndjsonText}</pre>
		</details>
	{/if}
</Panel>

<style>
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

	.probe {
		display: flex;
		gap: var(--v-space-2);
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
		margin: 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-4);
	}

	.feed {
		display: flex;
		flex-direction: column;
		gap: 1px;
		min-height: 120px;
		max-height: 320px;
		overflow-y: auto;
		border-top: 1px solid var(--v-line-1);
		padding-top: var(--v-space-2);
	}

	.ndjson summary {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
		cursor: pointer;
	}

	.ndjson pre {
		margin: var(--v-space-1) 0 0;
		max-height: 120px;
		overflow: auto;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-4);
	}

	.mono {
		font-family: var(--v-font-mono);
	}
</style>
