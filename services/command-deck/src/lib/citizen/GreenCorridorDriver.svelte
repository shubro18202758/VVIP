<script lang="ts">
	/**
	 * GreenCorridorDriver — "The Window" (Daylight, Gujarati-first).
	 *
	 * The 108 driver flow on the design system: the golden-hour counter is
	 * always on screen (mono), one large REQUEST CORRIDOR action in the act
	 * accent with the urgency pick kept verbatim (cardiac/trauma/stroke/
	 * other), the fixed verb rail idle→evaluating→proposal→open/deferred,
	 * the proposal card with mono seconds and the 60–180 s doctrine shown
	 * honestly, and the SCRIPTED FALLBACK badge DEFAULT-ON (no live request
	 * endpoint exists; numbers are demo-scale). Granted: the corridor map
	 * with the granted stretch highlighted + a Jan Rakshak unit walking
	 * en_route→on_scene from the live /units feed when deployed, labelled
	 * otherwise; control-room line; after-action minutes saved.
	 */
	import { onMount } from 'svelte';
	import { ops } from '$lib/ops/store.svelte';
	import { localizeNumber, t } from '$lib/i18n/strings';
	import { citizenT } from '$lib/i18n/strings.citizen';
	import Badge from '$lib/primitives/Badge.svelte';
	import Button from '$lib/primitives/Button.svelte';
	import EmptyState from '$lib/primitives/EmptyState.svelte';
	import LightCorridorMap from './LightCorridorMap.svelte';
	import { fetchAgencyUnits } from '$lib/comms/dispatch-feed';
	import { clockAt, driverSpine } from './holds';

	type Phase = 'idle' | 'evaluating' | 'proposal' | 'open' | 'done' | 'deferred';
	type Urgency = 'cardiac' | 'trauma' | 'stroke' | 'other';

	let { onBack }: { onBack?: () => void } = $props();

	let phase = $state<Phase>('idle');
	/** Selected stretch as the option's string value (parsed on use). */
	let stretchId = $state<string | null>(null);
	let urgency = $state<Urgency>('cardiac');
	let reachInSec = $state(0);
	let windowSec = $state(0);
	let countdown = $state(0);
	let goldenElapsed = $state(0);

	// Jan Rakshak unit state: live /units when deployed, scripted otherwise.
	let unitStatus = $state<'none' | 'en_route' | 'on_scene'>('none');
	let unitSource = $state<'live' | 'scripted'>('scripted');

	let timer: ReturnType<typeof setInterval> | null = null;
	let goldenTimer: ReturnType<typeof setInterval> | null = null;
	let unitTimer: ReturnType<typeof setTimeout> | null = null;

	const lang = $derived(ops.lang);

	let event = $derived(
		ops.events.find((e) => e.source !== 'offline-fixture' && e.state === 'active') ??
			ops.events.find((e) => e.source !== 'offline-fixture') ??
			ops.events[0] ??
			null,
	);
	/** Live spine when the event reports one, else the badged fixture spine. */
	let spine = $derived(driverSpine(event));
	let stretches = $derived<number[]>(spine.segmentIds);
	let minutesSaved = $derived(Math.max(1, Math.round(reachInSec / 60)));

	const RAIL: Array<{ key: Phase; labelKey: string }> = [
		{ key: 'idle', labelKey: 'phase_idle' },
		{ key: 'evaluating', labelKey: 'phase_evaluating' },
		{ key: 'proposal', labelKey: 'phase_proposal' },
		{ key: 'open', labelKey: 'phase_open' },
	];

	const URGENCIES: Array<{ key: Urgency; labelKey: string }> = [
		{ key: 'cardiac', labelKey: 'urgCardiac' },
		{ key: 'trauma', labelKey: 'urgTrauma' },
		{ key: 'stroke', labelKey: 'urgStroke' },
		{ key: 'other', labelKey: 'urgOther' },
	];

	onMount(() => {
		goldenTimer = setInterval(() => (goldenElapsed += 1), 1000);
		return () => {
			if (timer) clearInterval(timer);
			if (goldenTimer) clearInterval(goldenTimer);
			if (unitTimer) clearTimeout(unitTimer);
		};
	});

	function goldenClock(): string {
		const mm = String(Math.floor(goldenElapsed / 60)).padStart(2, '0');
		const ss = String(goldenElapsed % 60).padStart(2, '0');
		return localizeNumber(`${mm}:${ss}`, lang);
	}

	function request() {
		if (!stretchId) return;
		const sid = Number(stretchId);
		phase = 'evaluating';
		// Scripted evaluation (kept verbatim from the accepted flow): the
		// platform proposes the gap ahead of the event reaching the
		// driver's stretch; numbers are demo-scale per the 60–180 s
		// activation doctrine, NOT real dispatch output.
		setTimeout(() => {
			reachInSec = 120 + (sid % 3) * 30;
			windowSec = urgency === 'cardiac' ? 90 : urgency === 'stroke' ? 75 : 60;
			phase = 'proposal';
		}, 1400);
	}

	async function approve() {
		phase = 'open';
		countdown = windowSec;
		timer = setInterval(() => {
			countdown -= 1;
			if (countdown <= 0 && timer) {
				clearInterval(timer);
				timer = null;
				phase = 'done';
			}
		}, 1000);
		await trackUnit();
	}

	/** Jan Rakshak unit en_route→on_scene: live /units when deployed. */
	async function trackUnit() {
		unitStatus = 'en_route';
		try {
			const units = await fetchAgencyUnits();
			const live = units.find(
				(u) => u.agency === 'jan_rakshak' && (u.status === 'en_route' || u.status === 'on_scene'),
			);
			if (live) {
				unitSource = 'live';
				unitStatus = live.status === 'on_scene' ? 'on_scene' : 'en_route';
				if (live.status === 'en_route') {
					unitTimer = setTimeout(() => (unitStatus = 'on_scene'), 8000);
				}
				return;
			}
		} catch {
			/* live feed not deployed / down — labelled scripted fallback */
		}
		unitSource = 'scripted';
		unitTimer = setTimeout(() => (unitStatus = 'on_scene'), 9000);
	}

	function defer() {
		phase = 'deferred';
	}

	const railActive = $derived<Phase>(phase === 'done' ? 'open' : phase);
</script>

<div class="driver" data-screen="driver" data-phase={phase}>
	<!-- Golden hour: always on screen, mono, ticking. -->
	<div class="golden" data-testid="golden-hour">
		<span class="golden-label">{citizenT(lang, 'golden_hour')}</span>
		<span class="golden-clock mono" data-golden-seconds={goldenElapsed}>{goldenClock()}</span>
		<span class="golden-elapsed">{citizenT(lang, 'golden_elapsed')}</span>
	</div>

	<header class="head">
		{#if onBack}
			<button class="back" onclick={onBack}>← {t(lang, 'driverBack')}</button>
		{/if}
		<h1>{t(lang, 'driverTitle')}</h1>
		<p>{t(lang, 'driverSub')}</p>
	</header>

	<!-- Fixed verb rail: idle → evaluating → proposal → open/deferred. -->
	<ol class="rail" data-testid="phase-rail">
		{#each RAIL as r (r.key)}
			<li class="step" class:active={railActive === r.key} class:done={railActive !== 'idle' && RAIL.findIndex((x) => x.key === railActive) > RAIL.findIndex((x) => x.key === r.key)}>
				<span class="dot" aria-hidden="true"></span>
				<span class="word">{citizenT(lang, r.labelKey)}</span>
			</li>
		{/each}
	</ol>
	{#if phase === 'deferred'}
		<p class="rail-note" data-phase-note="deferred">{citizenT(lang, 'phase_deferred')} · {t(lang, 'holdPosition')} {clockAt(reachInSec + windowSec + 180, lang)}</p>
	{/if}

	{#if event === null}
		<EmptyState title={t(lang, 'noMovement')} hint={t(lang, 'noMovementSub')} />
	{:else if phase === 'idle' || phase === 'deferred' || phase === 'done'}
		<section class="request-card">
			<label class="stretch-pick">
				<span>{t(lang, 'atStretch')}</span>
				<select bind:value={stretchId} data-testid="stretch-select">
					<option value="">—</option>
					{#each stretches as sid (sid)}
						<option value={String(sid)}>{t(lang, 'freezeZone')} {localizeNumber(sid, lang)}</option>
					{/each}
				</select>
			</label>
			{#if spine.source === 'fixture'}
				<p class="spine-note">
					<Badge variant="fixture" label={t(lang, 'opsSourceOffline')} />
					{t(lang, 'demoNote')}
				</p>
			{/if}
			<fieldset class="urgency">
				<legend>{t(lang, 'urgency')}</legend>
				{#each URGENCIES as u (u.key)}
					<button type="button" class="urg" class:active={urgency === u.key} onclick={() => (urgency = u.key)}>
						{t(lang, u.labelKey)}
					</button>
				{/each}
			</fieldset>
			<div class="request-row">
				<Button
					variant="primary"
					size="citizen"
					actor="108-driver"
					onclick={request}
					disabled={!stretchId}
				>
					{t(lang, 'requestBtn')}
				</Button>
			</div>
			{#if phase === 'done'}
				<div class="after-action" data-testid="after-action">
					<Badge variant="emulated" label={citizenT(lang, 'scripted_badge')} />
					<p class="saved mono">~{localizeNumber(minutesSaved, lang)} {t(lang, 'minutesReturned')}</p>
					<p>{citizenT(lang, 'after_action', localizeNumber(minutesSaved, lang))}</p>
				</div>
			{/if}
		</section>
	{:else if phase === 'evaluating'}
		<section class="evaluating" data-testid="evaluating">
			<div class="pulse" aria-hidden="true"></div>
			<p>{t(lang, 'evaluating')}</p>
		</section>
	{:else if phase === 'proposal'}
		<section class="proposal" data-testid="proposal">
			<div class="proposal-head">
				<h2>{t(lang, 'proposalTitle')}</h2>
				<Badge variant="emulated" label={citizenT(lang, 'scripted_badge')} />
			</div>
			<p class="reach">
				{t(lang, 'convoyReachesIn')}
				<strong class="mono">{localizeNumber(reachInSec, lang)} s</strong>
				<span class="mono">({clockAt(reachInSec, lang)})</span>
			</p>
			<p class="cross">
				{t(lang, 'crossBefore')} <strong class="mono">{clockAt(reachInSec + windowSec, lang)}</strong>
			</p>
			<p class="doctrine">{citizenT(lang, 'doctrine_note')}</p>
			<div class="officer">
				<span>{t(lang, 'officerLabel')}</span>
				<Button variant="primary" size="citizen" actor="duty-officer" onclick={approve}>
					{t(lang, 'approveBtn')}
				</Button>
				<Button variant="secondary" size="citizen" onclick={defer}>
					{t(lang, 'deferBtn')}
				</Button>
			</div>
		</section>
	{:else if phase === 'open'}
		<section class="window-open" data-testid="window-open">
			<h2 class="open-title">{t(lang, 'windowOpen')}</h2>
			<p class="countdown mono" data-countdown={countdown}>
				{t(lang, 'windowClosesIn')} {localizeNumber(countdown, lang)} s
			</p>
			<div class="corridor-block">
				<h3>{citizenT(lang, 'corridor_map')} · {citizenT(lang, 'corridor_granted_stretch')}</h3>
				<LightCorridorMap {lang} highlightSegmentId={stretchId == null ? null : Number(stretchId)} compact />
				<p class="unit" data-unit-status={unitStatus}>
					<Badge variant={unitSource === 'live' ? 'live' : 'emulated'} label={unitSource === 'live' ? citizenT(lang, 'comms_dispatch_live') : citizenT(lang, 'comms_dispatch_demo')} />
					<span>{unitStatus === 'on_scene' ? citizenT(lang, 'unit_on_scene') : citizenT(lang, 'unit_en_route')}</span>
				</p>
				{#if unitSource === 'scripted'}
					<p class="unit-note">{citizenT(lang, 'units_note_fallback')}</p>
				{/if}
			</div>
		</section>
	{/if}

	<div class="control-room">
		<h3>{citizenT(lang, 'control_room')}</h3>
		<p class="mono">{citizenT(lang, 'control_room_line')}</p>
	</div>

	<p class="baseline">{t(lang, 'baselineNote')}</p>
	<p class="scripted-note">
		<Badge variant="emulated" label={citizenT(lang, 'scripted_badge')} />
		{citizenT(lang, 'scripted_note')}
	</p>
	<p class="demo">{t(lang, 'demoNote')}</p>
</div>

<style>
	.driver {
		max-width: 640px;
		margin: 0 auto;
		padding: var(--v-space-4);
		display: flex;
		flex-direction: column;
		gap: var(--v-gap-panel);
		font-size: var(--v-text-md);
		color: var(--v-ink-1);
	}

	.mono {
		font-family: var(--v-font-mono);
	}

	/* ── Golden hour: always on screen ──────────────────────────────────── */
	.golden {
		display: flex;
		align-items: baseline;
		gap: var(--v-space-3);
		padding: var(--v-space-3) var(--v-space-4);
		background: var(--v-surface-1);
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-panel);
	}

	.golden-label {
		font-size: var(--v-text-2xs);
		font-weight: var(--v-weight-status);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
		color: var(--v-act-text);
	}

	.golden-clock {
		font-family: var(--v-font-mono);
		font-size: var(--v-text-xl);
		font-weight: 600;
	}

	.golden-elapsed {
		font-size: var(--v-text-xs);
		color: var(--v-ink-3);
	}

	.head h1 {
		margin: var(--v-space-1) 0 0;
		font-size: var(--v-text-xl);
	}

	.head p {
		margin: var(--v-space-1) 0 0;
		color: var(--v-ink-2);
		font-size: var(--v-text-sm);
	}

	.back {
		background: none;
		border: none;
		padding: 0;
		color: var(--v-data);
		font-size: var(--v-text-sm);
		cursor: pointer;
	}

	/* ── Verb rail ──────────────────────────────────────────────────────── */
	.rail {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		gap: var(--v-space-2);
		flex-wrap: wrap;
	}

	.step {
		display: inline-flex;
		align-items: center;
		gap: var(--v-space-1);
		padding: var(--v-space-1) var(--v-space-3);
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-pill);
		font-size: var(--v-text-xs);
		color: var(--v-ink-3);
	}

	.step .dot {
		width: 8px;
		height: 8px;
		border-radius: 50%;
		border: 1px solid var(--v-line-2);
	}

	.step.active {
		border-color: var(--v-act-text);
		color: var(--v-ink-1);
	}

	.step.active .dot {
		background: var(--v-act);
		border-color: var(--v-act-text);
	}

	.step.done {
		color: var(--v-ink-2);
	}

	.step.done .dot {
		background: var(--v-band-green);
		border-color: var(--v-band-green);
	}

	.rail-note {
		margin: 0;
		font-size: var(--v-text-sm);
		color: var(--v-act-text);
	}

	/* ── Request card ───────────────────────────────────────────────────── */
	.request-card,
	.proposal,
	.window-open,
	.control-room {
		background: var(--v-surface-1);
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-panel);
		padding: var(--v-space-4);
	}

	.stretch-pick {
		display: flex;
		align-items: center;
		gap: var(--v-space-3);
		margin-bottom: var(--v-space-3);
	}

	.stretch-pick select {
		flex: 1;
		border: 1px solid var(--v-line-2);
		border-radius: var(--v-radius-btn);
		padding: var(--v-space-2) var(--v-space-3);
		font-size: var(--v-text-sm);
		background: var(--v-surface-2);
		color: var(--v-ink-1);
		height: var(--v-ctl-touch);
	}

	.spine-note {
		display: flex;
		align-items: center;
		gap: var(--v-space-2);
		margin: 0 0 var(--v-space-2);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.urgency {
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-chip);
		padding: var(--v-space-2) var(--v-space-3);
		margin: 0 0 var(--v-space-4);
		display: flex;
		gap: var(--v-space-2);
		flex-wrap: wrap;
	}

	.urgency legend {
		font-size: var(--v-text-xs);
		color: var(--v-ink-3);
		padding: 0 var(--v-space-1);
	}

	.urg {
		border: 1px solid var(--v-line-2);
		background: var(--v-surface-2);
		border-radius: var(--v-radius-pill);
		padding: var(--v-space-2) var(--v-space-4);
		font-size: var(--v-text-sm);
		color: var(--v-ink-2);
		cursor: pointer;
		min-height: var(--v-ctl-sm);
	}

	.urg.active {
		border-color: var(--v-act-text);
		color: var(--v-act-text);
		background: color-mix(in srgb, var(--v-act) 8%, transparent);
	}

	.request-row {
		width: 100%;
	}

	.request-row :global(button) {
		width: 100%;
	}

	/* ── Evaluating ─────────────────────────────────────────────────────── */
	.evaluating {
		display: flex;
		align-items: center;
		gap: var(--v-space-4);
		padding: var(--v-space-6) var(--v-space-4);
		background: var(--v-surface-1);
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-panel);
	}

	.pulse {
		width: 14px;
		height: 14px;
		border-radius: 50%;
		background: var(--v-act);
		animation: pulse var(--v-dur-pulse) infinite alternate;
	}

	@keyframes pulse {
		from {
			opacity: 0.35;
		}
		to {
			opacity: 1;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.pulse {
			animation: none;
			opacity: 1;
		}
	}

	/* ── Proposal ───────────────────────────────────────────────────────── */
	.proposal-head {
		display: flex;
		justify-content: space-between;
		align-items: baseline;
		gap: var(--v-space-2);
	}

	.proposal h2 {
		margin: 0 0 var(--v-space-2);
		font-size: var(--v-text-lg);
	}

	.reach,
	.cross {
		margin: var(--v-space-1) 0;
		font-size: var(--v-text-md);
	}

	.reach strong,
	.cross strong {
		color: var(--v-act-text);
	}

	.doctrine {
		margin: var(--v-space-2) 0;
		font-size: var(--v-text-xs);
		color: var(--v-ink-3);
		border-top: 1px dashed var(--v-line-1);
		padding-top: var(--v-space-2);
	}

	.officer {
		display: flex;
		align-items: center;
		gap: var(--v-space-2);
		margin-top: var(--v-space-3);
		font-size: var(--v-text-sm);
		color: var(--v-ink-2);
		flex-wrap: wrap;
	}

	/* ── Window open ────────────────────────────────────────────────────── */
	.window-open {
		border-color: color-mix(in srgb, var(--v-band-green) 40%, transparent);
		background: color-mix(in srgb, var(--v-band-green) 6%, var(--v-surface-1));
	}

	.open-title {
		margin: 0;
		color: var(--v-band-green);
		font-size: var(--v-text-lg);
		letter-spacing: 0.04em;
	}

	.countdown {
		font-family: var(--v-font-mono);
		font-size: var(--v-text-xl);
		font-weight: 600;
		margin: var(--v-space-2) 0 var(--v-space-3);
	}

	.corridor-block h3 {
		margin: 0 0 var(--v-space-2);
		font-size: var(--v-text-sm);
		color: var(--v-ink-2);
	}

	.unit {
		display: flex;
		align-items: center;
		gap: var(--v-space-2);
		margin: var(--v-space-2) 0 0;
		font-size: var(--v-text-sm);
	}

	.unit-note {
		margin: var(--v-space-1) 0 0;
		font-size: var(--v-text-xs);
		color: var(--v-ink-3);
	}

	.after-action {
		margin-top: var(--v-space-4);
		border-top: 1px dashed var(--v-line-1);
		padding-top: var(--v-space-3);
		display: flex;
		flex-direction: column;
		gap: var(--v-space-1);
		align-items: flex-start;
	}

	.saved {
		font-family: var(--v-font-mono);
		font-size: var(--v-text-lg);
		color: var(--v-band-green);
		margin: 0;
	}

	.after-action p:last-child {
		margin: 0;
		font-size: var(--v-text-xs);
		color: var(--v-ink-3);
	}

	/* ── Control room + notes ───────────────────────────────────────────── */
	.control-room h3 {
		margin: 0 0 var(--v-space-1);
		font-size: var(--v-text-md);
	}

	.control-room p {
		margin: 0;
		font-family: var(--v-font-mono);
		font-size: var(--v-text-sm);
		color: var(--v-ink-2);
	}

	.baseline {
		margin: 0;
		font-size: var(--v-text-xs);
		color: var(--v-ink-2);
		border-top: 1px dashed var(--v-line-1);
		padding-top: var(--v-space-3);
	}

	.scripted-note {
		display: flex;
		align-items: center;
		gap: var(--v-space-2);
		margin: 0;
		font-size: var(--v-text-xs);
		color: var(--v-ink-3);
	}

	.demo {
		margin: 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}
</style>
