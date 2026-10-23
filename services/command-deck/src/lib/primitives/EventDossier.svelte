<script lang="ts">
	/**
	 * EventDossier — the dossier frame. Supplies the frame (title, band
	 * chip, event id in mono, sectioned slots); the pages own the content:
	 * metadata, phases, audit_tail narrative (FeedItems), trilingual
	 * advisory preview, ranked choke plan.
	 */
	import type { Band } from '$lib/design/tokens';
	import BandChip from './BandChip.svelte';
	import EmptyState from './EmptyState.svelte';

	interface Props {
		event?: {
			eventId?: string | null;
			name?: string | null;
			band?: Band | null;
			state?: string | null;
		} | null;
		/** Override title when no event object is handed in. */
		title?: string;
		emptyTitle?: string;
		lang?: string;
		meta?: import('svelte').Snippet;
		phases?: import('svelte').Snippet;
		audit?: import('svelte').Snippet;
		advisory?: import('svelte').Snippet;
		footer?: import('svelte').Snippet;
		children?: import('svelte').Snippet;
	}

	let {
		event = null,
		title,
		emptyTitle = 'No event selected',
		lang = 'en',
		meta,
		phases,
		audit,
		advisory,
		footer,
		children
	}: Props = $props();

	const heading = $derived(title ?? event?.name ?? null);
	const sectionLabel = (key: string) => key;
</script>

<section class="dossier" data-prim="event-dossier">
	{#if heading}
		<header class="head">
			<div class="head-text">
				<h3 class="title">{heading}</h3>
				{#if event?.eventId}
					<span class="eid mono">{event.eventId}</span>
				{/if}
			</div>
			{#if event?.band}<BandChip band={event.band} size="sm" />{/if}
			{#if event?.state}<span class="state mono">{event.state}</span>{/if}
		</header>

		{#if meta}
			<div class="section">
				<h4 class="section-label">{sectionLabel('meta')}</h4>
				{@render meta()}
			</div>
		{/if}

		{#if phases}
			<div class="section">
				<h4 class="section-label">{sectionLabel('phases')}</h4>
				{@render phases()}
			</div>
		{/if}

		{#if audit}
			<div class="section">
				<h4 class="section-label">{sectionLabel('audit_tail')}</h4>
				{@render audit()}
			</div>
		{/if}

		{#if advisory}
			<div class="section">
				<h4 class="section-label">{sectionLabel('advisory')}</h4>
				{@render advisory()}
			</div>
		{/if}

		{@render children?.()}

		{#if footer}
			<footer class="foot">
				{@render footer()}
			</footer>
		{/if}
	{:else}
		<EmptyState title={emptyTitle} hint="Select an event on the board or the city canvas." />
	{/if}
</section>

<style>
	.dossier {
		display: flex;
		flex-direction: column;
		gap: var(--v-gap-panel);
		background: var(--v-surface-1);
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-panel);
		padding: var(--v-pad-panel);
		min-height: 0;
	}

	.head {
		display: flex;
		align-items: center;
		gap: var(--v-space-2);
	}

	.head-text {
		display: flex;
		align-items: baseline;
		gap: var(--v-space-2);
		flex: 1;
		min-width: 0;
	}

	.title {
		margin: 0;
		font-size: var(--v-text-md);
		color: var(--v-ink-1);
	}

	.eid {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.state {
		font-size: var(--v-text-2xs);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
		color: var(--v-ink-3);
	}

	.mono {
		font-family: var(--v-font-mono);
	}

	.section {
		display: flex;
		flex-direction: column;
		gap: var(--v-space-1);
		border-top: 1px solid var(--v-line-1);
		padding-top: var(--v-space-2);
	}

	.section-label {
		margin: 0;
		font-size: var(--v-text-2xs);
		font-weight: var(--v-weight-status);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
		color: var(--v-ink-3);
	}

	.foot {
		border-top: 1px solid var(--v-line-1);
		padding-top: var(--v-space-2);
	}
</style>
