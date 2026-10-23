<script lang="ts">
	/**
	 * EventLifecycleVerbs — the C-F verb rail for one corridor event:
	 * APPROVE / ACTIVATE / COMPLETE / CANCEL, offered strictly from the
	 * server's transition table for the event's RAW status (planning →
	 * approved|active|cancelled; approved → active|cancelled; active →
	 * completed|cancelled). Every verb is a city-writer commit under the
	 * named duty officer (POST /api/v1/events/{id}/status); while one is in
	 * flight every verb is disabled, so a double click sends one POST.
	 * CANCEL asks once more (with an optional reason for the ledger). The
	 * outcome renders inline — 'activated by <officer> · HH:MM:SS IST' or the
	 * server's refusal with the moves it allows.
	 */
	import Button from '$lib/primitives/Button.svelte';
	import { ops, eventVerbs } from '$lib/ops/store.svelte';
	import { tOps } from '$lib/i18n/strings.ops';
	import { istClock } from '$lib/ops/format';
	import type { CorridorEvent, EventTransitionTarget } from '$lib/ops/types';

	interface Props {
		event: CorridorEvent;
		/** Compact rail (board card) vs the dossier's full rail. */
		compact?: boolean;
	}

	let { event, compact = false }: Props = $props();

	const lang = $derived(ops.lang);
	const verbs = $derived(eventVerbs(event));
	const busy = $derived(ops.eventBusy === event.eventId);
	const anyBusy = $derived(ops.eventBusy !== null);
	const outcome = $derived(ops.eventOutcome?.eventId === event.eventId ? ops.eventOutcome : null);

	let confirmCancel = $state(false);
	let cancelNote = $state('');

	const VERB_KEY: Record<EventTransitionTarget, string> = {
		approved: 'verbApprove',
		active: 'verbActivate',
		completed: 'verbComplete',
		cancelled: 'verbCancel',
	};
	const DONE_KEY: Record<EventTransitionTarget, string> = {
		approved: 'lifecycleApproved',
		active: 'lifecycleActivated',
		completed: 'lifecycleCompleted',
		cancelled: 'lifecycleCancelled',
	};

	async function fire(to: EventTransitionTarget) {
		if (to === 'cancelled' && !confirmCancel) {
			confirmCancel = true;
			return;
		}
		const note = to === 'cancelled' ? cancelNote : '';
		const ok = await ops.transitionEvent(event.eventId, to, note);
		if (ok) {
			confirmCancel = false;
			cancelNote = '';
		}
	}
</script>

{#if verbs.length}
	<div class="verbs" class:compact data-testid="event-verbs" data-event-id={event.eventId} data-status={event.status ?? ''}>
		{#each verbs as verb (verb)}
			{#if verb === 'cancelled'}
				<span class="verb-wrap" data-verb={verb}>
					<Button
						variant="danger-outline"
						size="sm"
						busy={busy}
						disabled={anyBusy || !ops.officerOn}
						title={ops.officerOn ? undefined : tOps(lang, 'lifecycleNeedsOfficer')}
						onclick={() => void fire(verb)}
					>
						{confirmCancel ? tOps(lang, 'verbConfirmCancel') : tOps(lang, VERB_KEY[verb])}
					</Button>
				</span>
			{:else}
				<span class="verb-wrap" data-verb={verb}>
					<Button
						variant="primary"
						size="sm"
						busy={busy}
						disabled={anyBusy}
						actor={ops.officerOn ? ops.dutyOfficer : null}
						onclick={() => void fire(verb)}
					>
						{tOps(lang, VERB_KEY[verb])}
					</Button>
				</span>
			{/if}
		{/each}
	</div>
	{#if confirmCancel}
		<div class="cancel-row">
			<input
				class="note-input"
				type="text"
				placeholder={tOps(lang, 'verbCancelPlaceholder')}
				bind:value={cancelNote}
				maxlength="500"
			/>
			<button class="link" type="button" onclick={() => ((confirmCancel = false), (cancelNote = ''))}>
				{tOps(lang, 'verbKeep')}
			</button>
		</div>
	{/if}
{/if}

{#if outcome}
	<p class="outcome mono" class:ok={outcome.ok} class:refused={!outcome.ok} data-testid="event-outcome" data-ok={outcome.ok}>
		{#if outcome.ok}
			{tOps(lang, DONE_KEY[outcome.toStatus], outcome.actor ?? ops.dutyOfficer, istClock(outcome.at ?? outcome.when))}
			{#if outcome.closedDispatches}· {tOps(lang, 'lifecycleClosedDispatches', outcome.closedDispatches)}{/if}
		{:else}
			{tOps(lang, 'lifecycleRefused', outcome.status || '—', ops.text(outcome.detail))}
			{#if outcome.allowed}· {tOps(lang, 'lifecycleAllowed', outcome.allowed.length ? outcome.allowed.join(' | ') : '—')}{/if}
		{/if}
	</p>
{/if}

<style>
	.verbs {
		display: flex;
		flex-wrap: wrap;
		gap: var(--v-space-1);
	}

	.verb-wrap {
		display: inline-flex;
	}

	.cancel-row {
		display: flex;
		gap: var(--v-space-2);
		align-items: center;
	}

	.note-input {
		flex: 1;
		min-width: 0;
		height: var(--v-ctl-sm);
		padding: 0 var(--v-space-2);
		font: inherit;
		font-size: var(--v-text-xs);
		color: var(--v-ink-1);
		background: var(--v-surface-sink);
		border: 1px solid var(--v-band-red);
		border-radius: var(--v-radius-btn);
	}

	.link {
		font: inherit;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
		background: transparent;
		border: none;
		border-bottom: 1px dashed var(--v-line-2);
		padding: 0;
		cursor: pointer;
	}

	.outcome {
		margin: 0;
		font-size: var(--v-text-2xs);
		padding: var(--v-space-1) var(--v-space-2);
		border-radius: var(--v-radius-chip);
		border: 1px solid var(--v-line-1);
		overflow-wrap: anywhere;
	}

	.outcome.ok {
		border-color: var(--v-band-green);
		color: var(--v-band-green);
	}

	.outcome.refused {
		border-color: var(--v-line-1);
		border-top: 2px solid var(--v-band-red);
		color: var(--v-ink-2);
	}

	.mono {
		font-family: var(--v-font-mono);
	}
</style>
