<script lang="ts">
	/**
	 * SituationMasthead — the full-width posture rows of the /ops wall.
	 *
	 *   1. Band word + one-sentence posture (BandMasthead primitive); the
	 *      hysteresis candidate comes from the subject's own log
	 *      (ops.bandCandidate — observed_class vs committed, never guessed).
	 *   2. The duty-officer identity — the C6 gate lives next to the band it
	 *      guards: no named officer, no commit. When the deck runs the
	 *      operator gate (GET /api/session gate:true) the signed-in officer
	 *      fills the field and locks it (adopted for EVERY operator route by
	 *      the (deck) layout). Plates stay masked until an officer is named.
	 *   3. The brain posture strip: decider (model host loaded / loading /
	 *      unreachable, p95, novelty check), Gemma (the tactical tier), the
	 *      adaptive service tier — from /api/convoy/health and
	 *      /api/v1/adaptive/status; the traffic feed's provenance (SIMULATED
	 *      FEED when every reading is synthetic) and the IST wall clock.
	 *      Every engine value reads in plain operator words in the console's
	 *      language; the raw code stays in the tooltip.
	 */
	import BandMasthead from '$lib/primitives/BandMasthead.svelte';
	import Badge from '$lib/primitives/Badge.svelte';
	import { ops } from '$lib/ops/store.svelte';
	import { istClock } from '$lib/ops/format';
	import { tOps } from '$lib/i18n/strings.ops';
	import type { Band } from '$lib/design/tokens';

	const lang = $derived(ops.lang);

	let clock = $state(Date.now());
	$effect(() => {
		const id = setInterval(() => (clock = Date.now()), 1000);
		return () => clearInterval(id);
	});

	/** A raw engine value in plain words (EN/HI/GU), the code itself when unmapped. */
	function plain(prefix: string, code: string | null | undefined): string {
		if (!code) return '—';
		const key = `${prefix}${code}`;
		const v = tOps(lang, key);
		return v === key ? code : v;
	}

	/**
	 * The masthead is the CITY posture: the city band (GET /adaptive/status
	 * city_band) when the oracle serves it — a stale one is UNKNOWN, said
	 * since when — else the subject band. The selected event's own band
	 * lives on its panel and the canvas chip (scope-labelled there).
	 */
	const cityBand = $derived(ops.cityBand);
	const mastBand = $derived<Band | null>(
		cityBand ? (cityBand.stale ? null : (cityBand.band as Band | null)) : (ops.band as Band | null),
	);
	const mastSource = $derived.by(() => {
		if (!cityBand) return ops.bandSourceKind === 'none' ? null : ops.bandSource;
		if (cityBand.stale) {
			const since = cityBand.staleSince; // when the poll began failing — never when the band was entered
			return tOps(lang, 'bandSourceStale', since != null ? `${istClock(since).slice(0, 5)} IST` : '—');
		}
		return `${tOps(lang, 'bandScopeCity')}${cityBand.simulatedFeed ? ` · ${tOps(lang, 'bandSimulatedTag')}` : ''}`;
	});

	const posture = $derived.by(() => {
		const band = mastBand;
		if (!band) return '';
		const bits: string[] = [];
		const c = ops.counters;
		if (c.activeEvents) bits.push(tOps(lang, 'postureActive', c.activeEvents));
		if (c.pendingDecisions) bits.push(tOps(lang, 'posturePending', c.pendingDecisions));
		if (c.unitsDispatched) bits.push(tOps(lang, 'postureUnits', c.unitsDispatched));
		const base = tOps(lang, 'posture_' + band);
		// 'Normal operations — city band GREEN — 1 active, …' (no '. —' seam; HI ends in a danda)
		return bits.length ? `${base.replace(/[.।]\s*$/u, '')} — ${bits.join(', ')}` : base;
	});

	/** The hysteresis candidate speaks for the subject's log — shown here only when the subject IS the city. */
	const candidate = $derived(
		ops.bandCandidate && (!cityBand || ops.bandSourceKind === 'city-band')
			? { band: ops.bandCandidate.band as Band, ticks: ops.bandCandidate.ticks, required: ops.bandCandidate.required }
			: null,
	);

	const brain = $derived(ops.brain);

	const deciderChip = $derived.by(() => {
		const s = brain?.decider.state ?? 'unknown';
		if (!brain?.reachable) return { variant: 'offline' as const, label: tOps(lang, 'brainUnreachable') };
		if (s === 'loaded') return { variant: 'live' as const, label: tOps(lang, 'brainLoaded') };
		if (s === 'loading') return { variant: 'pending' as const, label: tOps(lang, 'brainLoading') };
		if (s === 'unreachable') return { variant: 'offline' as const, label: tOps(lang, 'brainUnreachable') };
		return { variant: 'stale' as const, label: '—' };
	});

	const gemmaChip = $derived.by(() => {
		if (!brain?.reachable) return { variant: 'offline' as const, label: tOps(lang, 'brainUnreachable') };
		if (brain.gemma.state === 'connected') return { variant: 'live' as const, label: tOps(lang, 'brainConnected') };
		if (brain.gemma.state === 'unreachable') return { variant: 'offline' as const, label: tOps(lang, 'brainUnreachable') };
		return { variant: 'stale' as const, label: '—' };
	});

	const feedChip = $derived.by(() => {
		const f = ops.trafficFeed;
		if (!f) return { variant: 'stale' as const, label: ops.trafficFeedNote ? tOps(lang, 'feedNone') : '…' };
		if (f.label === 'simulated') return { variant: 'emulated' as const, label: tOps(lang, 'feedSimulated') };
		if (f.label === 'mixed') {
			const pct = Math.round((100 * f.synthetic) / Math.max(1, f.real + f.synthetic));
			return { variant: 'emulated' as const, label: tOps(lang, 'feedMixed', pct) };
		}
		if (f.label === 'live') return { variant: 'live' as const, label: tOps(lang, 'feedLive') };
		return { variant: 'stale' as const, label: tOps(lang, 'feedNone') };
	});
</script>

<BandMasthead band={mastBand} {posture} {candidate} bandSource={mastSource} lang={lang} />

<div class="officer-row">
	<label class="officer">
		<span class="officer-label">{tOps(lang, 'mastheadOfficer')}</span>
		<input
			class="officer-input"
			type="text"
			placeholder={tOps(lang, 'mastheadOfficerPlaceholder')}
			value={ops.dutyOfficer}
			readonly={ops.dutyOfficerFromSession}
			oninput={(e) => ops.setDutyOfficer(e.currentTarget.value)}
			autocomplete="off"
			spellcheck="false"
			data-testid="duty-officer"
		/>
	</label>
	{#if ops.officerOn}
		<Badge
			variant="act"
			label={ops.dutyOfficerFromSession ? tOps(lang, 'mastheadSessionSigned') : tOps(lang, 'mastheadOfficerSet')}
			title={ops.dutyOfficer}
		/>
	{:else}
		<Badge variant="pending" label={tOps(lang, 'mastheadOfficerMissing')} />
	{/if}

	<div class="strip mono" data-testid="brain-posture" data-decider={brain?.decider.state ?? 'unknown'} data-tier={brain?.degradationTier ?? ''}>
		<span class="cell">
			<span class="k">{tOps(lang, 'brainDecider')}</span>
			<Badge variant={deciderChip.variant} label={deciderChip.label} title={brain?.decider.model ?? undefined} />
			{#if brain?.decider.p95Ms != null}<span class="v">{tOps(lang, 'brainP95', (brain.decider.p95Ms / 1000).toFixed(1))}</span>{/if}
			{#if brain?.decider.latch}<span class="v" title={`novelty_latch: ${brain.decider.latch}`}>{tOps(lang, 'brainNovelty', plain('brainLatchState_', brain.decider.latch))}</span>{/if}
			{#if brain?.decider.degraded}<span class="v warn">{tOps(lang, 'brainDegraded')}</span>{/if}
		</span>
		<span class="cell">
			<span class="k">{tOps(lang, 'brainGemma')}</span>
			<Badge variant={gemmaChip.variant} label={gemmaChip.label} title={brain?.gemma.model ?? undefined} />
		</span>
		<span class="cell">
			<span class="k">{tOps(lang, 'brainTier')}</span>
			<span class="v" class:warn={brain?.degradationTier != null && brain.degradationTier.toUpperCase() !== 'FULL'} title={brain?.degradationTier ?? undefined}>{plain('brainTier_', brain?.degradationTier?.toUpperCase())}</span>
		</span>
		<span class="cell">
			<span class="k">{tOps(lang, 'feedTitle')}</span>
			<span data-testid="feed-provenance" data-label={ops.trafficFeed?.label ?? ''}>
				<Badge variant={feedChip.variant} label={feedChip.label} title={ops.trafficFeed ? `real ${ops.trafficFeed.real} · synthetic ${ops.trafficFeed.synthetic}` : undefined} />
			</span>
		</span>
		<span class="cell clock" aria-label="IST clock">{istClock(clock)} IST</span>
	</div>
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

	.officer-input[readonly] {
		border-color: var(--v-act);
		color: var(--v-act-text);
	}

	.officer-input:focus-visible {
		outline: 1px solid var(--v-act-focus);
		outline-offset: 2px;
	}

	.strip {
		margin-left: auto;
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: var(--v-space-3);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.cell {
		display: inline-flex;
		align-items: center;
		gap: var(--v-space-1);
	}

	.k {
		color: var(--v-ink-3);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
	}

	.v {
		color: var(--v-ink-2);
	}

	.warn {
		color: var(--v-band-amber);
	}

	.clock {
		color: var(--v-ink-2);
		font-variant-numeric: tabular-nums;
	}

	.mono {
		font-family: var(--v-font-mono);
	}
</style>
