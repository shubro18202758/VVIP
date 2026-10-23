<script lang="ts">
	/**
	 * DecisionCard — one item the machine has proposed and a human must
	 * dispose of. Act-colored edge; deadline/counters mono; the commit
	 * action cannot render without officer identity (Button enforces it;
	 * this card surfaces the officer hint honestly). While `busy` (a
	 * decision for this item is in flight) the commit shows the working
	 * state and every action is disabled, so a double click can never
	 * double-POST a decision (the second one used to come back 409).
	 * `blocked` names why the commit cannot go out even with an officer
	 * on console (e.g. a pursuit with no approved recommendation, C6): the
	 * commit stays disabled and the reason replaces the officer hint.
	 * `deadlineTitle` explains the mono countdown on hover (e.g. what an
	 * evidence purge is); `badgeLabel` names the card's kind in the reader's
	 * language (the raw kind stays in data-kind).
	 */
	import Button from './Button.svelte';
	import Badge from './Badge.svelte';
	import { t } from '$lib/i18n/strings';

	export type DecisionKind = 'echallan' | 'corridor-request' | 'dispatch' | 'pursuit' | 'stolen-sighting';

	export interface DecisionAction {
		id: string;
		label: string;
		kind: 'commit' | 'reject' | 'defer' | 'ghost';
	}

	interface Props {
		kind: DecisionKind;
		title: string;
		/** Rendered mono (retention countdowns, ETAs, golden-hour). */
		deadline?: string | null;
		/** Hover explanation of the deadline (what the countdown means). */
		deadlineTitle?: string | null;
		/** The kind badge's text (defaults to the raw kind). */
		badgeLabel?: string | null;
		actions?: DecisionAction[];
		/** Named officer identity; without it commit renders locked. */
		namedActor?: string | null;
		/** A decision for this item is in flight: commit shows busy, all actions disable. */
		busy?: boolean;
		/** Why the commit is refused regardless of the officer (rendered in the footer). */
		blocked?: string | null;
		onaction?: (id: string) => void;
		lang?: string;
		evidence?: import('svelte').Snippet;
		meta?: import('svelte').Snippet;
		children?: import('svelte').Snippet;
	}

	let {
		kind,
		title,
		deadline = null,
		deadlineTitle = null,
		badgeLabel = null,
		actions = [],
		namedActor = null,
		busy = false,
		blocked = null,
		onaction,
		lang = 'en',
		evidence,
		meta,
		children
	}: Props = $props();

	const variantFor = (a: DecisionAction) =>
		a.kind === 'commit' ? 'primary' : a.kind === 'reject' ? 'danger-outline' : 'ghost';

	const hasCommit = $derived(actions.some((a) => a.kind === 'commit'));
</script>

<article class="card k-{kind}" class:busy data-prim="decision-card" data-kind={kind} aria-busy={busy || undefined}>
	<header class="head">
		<Badge variant="neutral" label={badgeLabel ?? kind} />
		<h3 class="title">{title}</h3>
		{#if deadline}<span class="deadline mono" title={deadlineTitle ?? undefined} data-testid="card-deadline">{deadline}</span>{/if}
	</header>

	{#if meta}
		<div class="meta">
			{@render meta()}
		</div>
	{/if}

	<div class="body">
		{@render children?.()}
	</div>

	{#if evidence}
		<div class="evidence">
			{@render evidence()}
		</div>
	{/if}

	<footer class="foot">
		{#if blocked}
			<span class="blocked mono" data-blocked>{blocked}</span>
		{:else if namedActor}
			<span class="actor mono" title="named officer">{namedActor}</span>
		{:else if hasCommit}
			<span class="actor-hint mono">{t(lang, 'officer_hint')}</span>
		{/if}
		<span class="actions">
			{#each actions as action (action.id)}
				<Button
					variant={variantFor(action)}
					size="sm"
					{lang}
					actor={action.kind === 'commit' ? namedActor : namedActor ?? 'ui'}
					busy={busy && action.kind === 'commit'}
					disabled={busy || (!!blocked && action.kind === 'commit')}
					onclick={() => {
						if (!busy && !(blocked && action.kind === 'commit')) onaction?.(action.id);
					}}
				>
					{action.label}
				</Button>
			{/each}
		</span>
	</footer>
</article>

<style>
	.card {
		display: flex;
		flex-direction: column;
		gap: var(--v-gap-panel);
		padding: var(--v-pad-panel);
		background: var(--v-surface-1);
		border: 1px solid var(--v-line-1);
		border-left: 2px solid var(--v-act);
		border-radius: var(--v-radius-panel);
	}

	.card.busy {
		border-left-color: var(--v-act-2);
	}

	/* a stolen vehicle: evidence for a pursuit, never a fine */
	.card.k-stolen-sighting {
		border-left-color: var(--v-band-red);
	}

	.head {
		display: flex;
		align-items: center;
		gap: var(--v-space-2);
		flex-wrap: wrap;
	}

	/* the title gets the line: a long countdown wraps under it, never squeezes it */
	.title {
		margin: 0;
		font-size: var(--v-text-base);
		color: var(--v-ink-1);
		flex: 1 1 15rem;
		min-width: 0;
		overflow-wrap: anywhere;
	}

	.deadline {
		margin-left: auto;
		font-size: var(--v-text-xs);
		color: var(--v-ink-2);
		white-space: nowrap;
	}

	.mono {
		font-family: var(--v-font-mono);
	}

	.meta {
		font-family: var(--v-font-mono);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.body {
		font-size: var(--v-text-sm);
		color: var(--v-ink-2);
	}

	.evidence {
		border: 1px dashed var(--v-line-2);
		border-radius: var(--v-radius-chip);
		padding: var(--v-space-2);
		font-family: var(--v-font-mono);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.foot {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: var(--v-space-2);
		flex-wrap: wrap;
	}

	.actor {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-2);
		border: 1px solid var(--v-line-2);
		border-radius: var(--v-radius-pill);
		padding: 1px var(--v-space-2);
	}

	.actor-hint {
		font-size: var(--v-text-2xs);
		color: var(--v-act-text);
	}

	.blocked {
		font-size: var(--v-text-2xs);
		color: var(--v-band-red);
		flex: 1;
		min-width: 0;
	}

	.actions {
		display: flex;
		gap: var(--v-space-2);
	}
</style>
