<script lang="ts">
	/**
	 * DecisionCard — one item the machine has proposed and a human must
	 * dispose of. Act-colored edge; deadline/counters mono; the commit
	 * action cannot render without officer identity (Button enforces it;
	 * this card surfaces the officer hint honestly).
	 */
	import Button from './Button.svelte';
	import Badge from './Badge.svelte';
	import { t } from '$lib/i18n/strings';

	export type DecisionKind = 'echallan' | 'corridor-request' | 'dispatch' | 'pursuit';

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
		actions?: DecisionAction[];
		/** Named officer identity; without it commit renders locked. */
		namedActor?: string | null;
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
		actions = [],
		namedActor = null,
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

<article class="card k-{kind}" data-prim="decision-card">
	<header class="head">
		<Badge variant="neutral" label={kind} />
		<h3 class="title">{title}</h3>
		{#if deadline}<span class="deadline mono">{deadline}</span>{/if}
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
		{#if namedActor}
			<span class="actor mono" title="named officer">{namedActor}</span>
		{:else if hasCommit}
			<span class="actor-hint mono">{t(lang, 'officer_hint')}</span>
		{/if}
		<span class="actions">
			{#each actions as action (action.id)}
				<Button
					variant={variantFor(action)}
					size="sm"
					actor={action.kind === 'commit' ? namedActor : namedActor ?? 'ui'}
					onclick={() => onaction?.(action.id)}
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

	.head {
		display: flex;
		align-items: center;
		gap: var(--v-space-2);
		flex-wrap: wrap;
	}

	.title {
		margin: 0;
		font-size: var(--v-text-base);
		color: var(--v-ink-1);
		flex: 1;
		min-width: 12ch;
	}

	.deadline {
		font-size: var(--v-text-xs);
		color: var(--v-ink-2);
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

	.actions {
		display: flex;
		gap: var(--v-space-2);
	}
</style>
