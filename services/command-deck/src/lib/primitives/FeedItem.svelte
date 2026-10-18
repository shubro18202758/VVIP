<script lang="ts">
	/**
	 * FeedItem — one row of the decision audit ledger.
	 *
	 * human actor: 200ms act left-edge flash, then a persistent 2px act
	 * rail; machine rows sit in dim mono. Appends settle by 1px — never
	 * slide. Click (when detail is present) opens the evidence Modal.
	 * AuditRow = FeedItem + a source chip.
	 */
	import Modal from './Modal.svelte';

	/** Structural mirror of ops AuditLevel (design-shell cannot import $lib/ops). */
	export type FeedLevel = 'info' | 'decision' | 'tool' | 'model' | 'band' | 'error';
	/** Structural mirror of ops AuditSource. */
	export type FeedSource =
		| 'ws-event'
		| 'ws-convoy'
		| 'ndjson-chat'
		| 'audit-poll'
		| 'operator'
		| (string & {});

	interface Props {
		ts: number;
		actor: string;
		actorKind: 'human' | 'machine';
		level: FeedLevel;
		text: string;
		detail?: unknown;
		source?: FeedSource | null;
		lang?: string;
		right?: import('svelte').Snippet;
		children?: import('svelte').Snippet;
	}

	let {
		ts,
		actor,
		actorKind,
		level,
		text,
		detail,
		source = null,
		lang = 'en',
		right,
		children
	}: Props = $props();

	let open = $state(false);

	const time = $derived.by(() => {
		const d = new Date(ts);
		const hh = String(d.getHours()).padStart(2, '0');
		const mm = String(d.getMinutes()).padStart(2, '0');
		const ss = String(d.getSeconds()).padStart(2, '0');
		return `${hh}:${mm}:${ss}`;
	});

	const hasDetail = $derived(detail !== undefined && detail !== null);

	function rowText(): string {
		return `[${time}] ${level} ${actor} — ${text}`;
	}
</script>

{#if hasDetail}
	<button
		class="row clickable {actorKind}"
		type="button"
		aria-label={rowText()}
		onclick={() => (open = true)}
		data-prim="feed-item"
		data-level={level}
	>
		<span class="ts">{time}</span>
		<span class="level-dot lv-{level}" aria-hidden="true"></span>
		<span class="actor" class:human-actor={actorKind === 'human'}>{actor}</span>
		<span class="text">{text}{@render children?.()}</span>
		{#if source}<span class="source mono">{source}</span>{/if}
		{@render right?.()}
	</button>
{:else}
	<div class="row {actorKind}" data-prim="feed-item" data-level={level}>
		<span class="ts">{time}</span>
		<span class="level-dot lv-{level}" aria-hidden="true"></span>
		<span class="actor" class:human-actor={actorKind === 'human'}>{actor}</span>
		<span class="text">{text}{@render children?.()}</span>
		{#if source}<span class="source mono">{source}</span>{/if}
		{@render right?.()}
	</div>
{/if}

{#if hasDetail}
	<Modal open={open} title="{actor} · {text}" onClose={() => (open = false)}>
		<pre class="detail mono">{JSON.stringify(detail, null, 2)}</pre>
	</Modal>
{/if}

<style>
	.row {
		display: flex;
		align-items: baseline;
		gap: var(--v-space-2);
		width: 100%;
		padding: var(--v-space-1) var(--v-space-2);
		border: none;
		border-left: 2px solid transparent;
		border-radius: var(--v-radius-chip);
		background: transparent;
		font: inherit;
		text-align: left;
		animation: settle var(--v-dur-out) var(--v-ease);
	}

	@keyframes settle {
		from {
			opacity: 0;
			transform: translateY(-1px);
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}

	.clickable {
		cursor: pointer;
	}

	.clickable:hover,
	.clickable:focus-visible {
		background: var(--v-surface-2);
	}

	.clickable:focus-visible {
		outline: 1px solid var(--v-act-focus);
		outline-offset: 2px;
	}

	/* machine rows: dim mono, no rail. */
	.machine {
		font-family: var(--v-font-mono);
		color: var(--v-ink-4);
		font-size: var(--v-text-xs);
	}

	.machine .ts {
		color: var(--v-ink-4);
	}

	/* human rows: one 200ms act flash, then a persistent 2px act rail. */
	.human {
		font-family: var(--v-font-ui);
		font-size: var(--v-text-sm);
		color: var(--v-ink-1);
		border-left-color: var(--v-act);
		animation:
			settle var(--v-dur-out) var(--v-ease),
			flash var(--v-dur-pulse) var(--v-ease) 1;
	}

	@keyframes flash {
		from {
			box-shadow: inset 2px 0 0 var(--v-act);
			color: var(--v-act-text);
		}
		to {
			box-shadow: inset 2px 0 0 transparent;
		}
	}

	.human .ts {
		color: var(--v-ink-2);
	}

	.human .actor {
		color: var(--v-act-text);
	}

	.ts {
		font-family: var(--v-font-mono);
		font-size: var(--v-text-2xs);
		flex-shrink: 0;
	}

	.level-dot {
		width: 6px;
		height: 6px;
		border-radius: 50%;
		flex-shrink: 0;
		align-self: center;
		border: 1px solid var(--v-ink-4);
	}

	.lv-decision {
		background: var(--v-data);
		border-color: var(--v-data);
	}

	.lv-tool {
		background: var(--v-tool);
		border-color: var(--v-tool);
	}

	.lv-band {
		background: var(--v-band-amber);
		border-color: var(--v-band-amber);
	}

	.lv-error {
		background: var(--v-band-red);
		border-color: var(--v-band-red);
	}

	.lv-model {
		background: transparent;
		border-color: var(--v-ink-4);
	}

	.actor {
		flex-shrink: 0;
		font-family: var(--v-font-mono);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.human-actor {
		color: var(--v-act-text);
	}

	.text {
		flex: 1;
		min-width: 0;
		/* long audit tokens (ids, channel names) must wrap, not overflow */
		overflow-wrap: anywhere;
	}

	.source {
		font-size: 10px;
		color: var(--v-ink-4);
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-pill);
		padding: 0 var(--v-space-1);
		align-self: center;
		flex-shrink: 0;
	}

	.mono {
		font-family: var(--v-font-mono);
	}

	.detail {
		margin: 0;
		font-size: var(--v-text-xs);
		color: var(--v-ink-2);
		overflow-x: auto;
	}

	@media (prefers-reduced-motion: reduce) {
		.row {
			animation: none;
		}
	}
</style>
