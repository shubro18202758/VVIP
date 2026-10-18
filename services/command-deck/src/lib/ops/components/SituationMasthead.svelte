<script lang="ts">
	/**
	 * SituationMasthead — the full-width band posture row of the /ops wall.
	 *
	 * Band word + one-sentence posture (BandMasthead primitive), plus the
	 * duty-officer identity input — the C6 gate lives next to the band it
	 * guards: no named officer, no commit. The hysteresis candidate is read
	 * client-side from the band history (the showcase/band_easing artifact:
	 * a differing newest reading held for ticks before the commit wipes).
	 */
	import BandMasthead from '$lib/primitives/BandMasthead.svelte';
	import Badge from '$lib/primitives/Badge.svelte';
	import { ops } from '$lib/ops/store.svelte';
	import { tOps } from '$lib/i18n/strings.ops';
	import type { Band } from '$lib/design/tokens';

	/** Dwell window — the band_easing.json shape (RED held n=1..3, commit at n=4). */
	const DWELL_REQUIRED = 4;

	const lang = $derived(ops.lang);

	const posture = $derived.by(() => {
		const band = ops.band;
		if (!band) return '';
		const bits: string[] = [];
		const live = ops.events.filter((e) => e.source !== 'offline-fixture');
		const active = live.filter((e) => e.state === 'active');
		if (active.length) bits.push(`${active.length} active`);
		if (ops.counters.pendingDecisions) bits.push(`${ops.counters.pendingDecisions} pending decision${ops.counters.pendingDecisions === 1 ? '' : 's'}`);
		if (ops.counters.unitsDispatched) bits.push(`${ops.counters.unitsDispatched} unit${ops.counters.unitsDispatched === 1 ? '' : 's'} out`);
		const head = bits.length ? ` — ${bits.join(', ')}` : '';
		return `${tOps(lang, 'posture_' + band)}${head}`;
	});

	/** Candidate band during hysteresis, from the live history tail. */
	const candidate = $derived.by(() => {
		const h = ops.bandHistory;
		if (h.length < 2 || !ops.band) return null;
		const newest = h[h.length - 1].band;
		if (newest === ops.band) return null;
		let ticks = 0;
		for (let i = h.length - 1; i >= 0 && h[i].band === newest; i--) ticks++;
		return { band: newest as Band, ticks, required: DWELL_REQUIRED };
	});
</script>

<BandMasthead
	band={ops.band as Band | null}
	{posture}
	{candidate}
	bandSource={ops.bandSource === 'none' ? null : ops.bandSource}
	lang={lang}
/>

<div class="officer-row">
	<label class="officer">
		<span class="officer-label">{tOps(lang, 'mastheadOfficer')}</span>
		<input
			class="officer-input"
			type="text"
			placeholder={tOps(lang, 'mastheadOfficerPlaceholder')}
			value={ops.dutyOfficer}
			oninput={(e) => ops.setDutyOfficer(e.currentTarget.value)}
			autocomplete="off"
			spellcheck="false"
		/>
	</label>
	{#if ops.officerOn}
		<Badge variant="act" label={tOps(lang, 'mastheadOfficerSet')} title={ops.dutyOfficer} />
	{:else}
		<Badge variant="pending" label={tOps(lang, 'mastheadOfficerMissing')} />
	{/if}
	<span class="posture-word mono">{tOps(lang, 'mastheadPosture')}</span>
</div>

<style>
	.officer-row {
		display: flex;
		align-items: center;
		gap: var(--v-space-3);
		padding: var(--v-space-2) var(--v-space-4);
		background: var(--v-surface-1);
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-panel);
		flex-wrap: wrap;
	}

	.officer {
		display: inline-flex;
		align-items: center;
		gap: var(--v-space-2);
		min-width: 0;
	}

	.officer-label {
		font-size: var(--v-text-xs);
		font-weight: var(--v-weight-status);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
		color: var(--v-ink-3);
		white-space: nowrap;
	}

	.officer-input {
		height: var(--v-ctl-md);
		min-width: 220px;
		padding: 0 var(--v-space-2);
		font: inherit;
		font-size: var(--v-text-sm);
		color: var(--v-ink-1);
		background: var(--v-surface-sink);
		border: 1px solid var(--v-line-2);
		border-radius: var(--v-radius-btn);
	}

	.officer-input:focus-visible {
		outline: 1px solid var(--v-act-focus);
		outline-offset: 2px;
	}

	.posture-word {
		margin-left: auto;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-4);
	}

	.mono {
		font-family: var(--v-font-mono);
	}
</style>
