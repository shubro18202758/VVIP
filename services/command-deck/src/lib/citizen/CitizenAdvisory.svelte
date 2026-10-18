<script lang="ts">
	/**
	 * CitizenAdvisory — "Your road, next 2 hours" (Daylight, trilingual).
	 *
	 * Rebuild of the accepted advisory on the design system: same data
	 * path (holds derive from the SAME ops-store events the operators
	 * see — live events first, labelled demo fixtures otherwise), now on
	 * primitives (Badge/Button/EmptyState), plain-language band strip
	 * (citizens never see internal codes), one card per nearby corridor
	 * event with mono localized windows + alternates + consequence copy,
	 * today's timeline accordion, the light corridor map, the subscribe
	 * flow (10-digit validation, honest demo note) and the emergency
	 * card → /driver.
	 */
	import { ops } from '$lib/ops/store.svelte';
	import { SUPPORTED, localizeNumber, t } from '$lib/i18n/strings';
	import { citizenT } from '$lib/i18n/strings.citizen';
	import { USE_CASES } from '$lib/ops/usecases';
	import type { CorridorEvent } from '$lib/ops/types';
	import {
		ALTERNATE_ROADS,
		advisoryEvents,
		holdMinutes,
		isDemoEvent,
		stretchHolds,
		stripTier,
	} from './holds';
	import Badge from '$lib/primitives/Badge.svelte';
	import Button from '$lib/primitives/Button.svelte';
	import EmptyState from '$lib/primitives/EmptyState.svelte';
	import LightCorridorMap from './LightCorridorMap.svelte';

	let phone = $state('');
	let alertLang = $state('en');
	let subscribed = $state(false);
	let demoEventId = $state(USE_CASES[1].id);
	let timelineOpen = $state(false);
	let explainerOpen = $state(false);

	const lang = $derived(ops.lang);

	/** Live events first; fixtures only as the labelled fallback. */
	let events = $derived.by<CorridorEvent[]>(() => {
		if (ops.events.length) return advisoryEvents(ops.events);
		return [];
	});

	let anyActive = $derived(events.some((e) => e.state !== 'completed'));
	let tier = $derived(stripTier(ops.band, anyActive));
	let live = $derived(ops.boardStatus !== 'offline' && events.some((e) => !isDemoEvent(e)));
	let updatedLabel = $derived.by(() => {
		if (!ops.boardUpdated) return t(lang, 'updated');
		const d = new Date(ops.boardUpdated);
		const hhmm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
		return `${t(lang, 'updated')} · ${localizeNumber(hhmm, lang)}`;
	});

	function validPhone(p: string): boolean {
		return /^[0-9]{10}$/.test(p.replace(/\s+/g, ''));
	}

	function subscribe() {
		if (validPhone(phone)) subscribed = true;
	}

	const previewText = $derived.by(() => {
		const ev = events[0];
		if (!ev) return '';
		const kind = t(lang, `kind_${ev.eventKind}`);
		const minutes = localizeNumber(holdMinutes(ev), lang);
		const holds = stretchHolds(ev, lang);
		if (lang === 'hi') return `${kind}: ~${minutes} मिनट का रोक — ${holds[0]?.clock ?? ''} बजे। ${ALTERNATE_ROADS} इस्तेमाल करें।`;
		if (lang === 'gu') return `${kind}: ~${minutes} મિનિટનો અવરોધ — ${holds[0]?.clock ?? ''} વાગ્યે. ${ALTERNATE_ROADS} વાપરો.`;
		return `${kind}: ~${minutes} min hold around ${holds[0]?.clock ?? ''}. Use ${ALTERNATE_ROADS} instead.`;
	});
</script>

<div class="citizen" data-screen="citizen">
	<header class="hero">
		<div class="hero-row">
			<strong class="brand">{t(lang, 'brand')}</strong>
			<Badge variant={live ? 'live' : 'fixture'} label={live ? t(lang, 'opsSourceLive') : t(lang, 'opsSourceOffline')} />
		</div>
		<h1 class="kicker">{citizenT(lang, 'citizen_kicker')}</h1>
		<p class="tagline">{t(lang, 'tagline')}</p>
		<p class="updated mono">{updatedLabel}</p>
	</header>

	<!-- Plain-language band strip: never the internal code. -->
	<section class="strip tier-{tier}" data-strip={tier} aria-live="polite">
		<span class="strip-dot" aria-hidden="true"></span>
		<p>{citizenT(lang, tier === 'calm' ? 'strip_calm' : tier === 'watch' ? 'strip_watch' : tier === 'hold' ? 'strip_hold' : 'strip_severe')}</p>
	</section>

	{#if events.length === 0}
		<EmptyState title={t(lang, 'opsNoEvents')} hint={t(lang, 'statusClearSub')} />
	{:else}
		<!-- One card per nearby corridor event. -->
		{#each events as ev (ev.eventId)}
			{@const holds = stretchHolds(ev, lang)}
			{@const demo = isDemoEvent(ev)}
			<article class="event-card" data-event={ev.eventId}>
				<header class="card-head">
					<h2>{t(lang, `kind_${ev.eventKind}`)}</h2>
					<Badge variant={demo ? 'fixture' : 'live'} label={demo ? t(lang, 'opsSourceOffline') : t(lang, 'opsSourceLive')} />
				</header>
				<p class="consequence">{citizenT(lang, `consequence_${ev.eventKind}`)}</p>
				<ul class="holds">
					{#each holds as h (h.segmentId)}
						<li class="hold" data-hold-state={h.state}>
							<span class="seg mono">{t(lang, 'freezeZone')} {localizeNumber(h.segmentId, lang)}</span>
							<span class="window mono">
								{t(lang, 'freezeMinutes', localizeNumber(h.minutes, lang))}
								· {citizenT(lang, 'card_window_at', h.clock)}
							</span>
							<span class="state">
								{h.state === 'now' ? citizenT(lang, 'card_now') : h.state === 'next' ? citizenT(lang, 'card_soon') : '→'}
							</span>
						</li>
					{/each}
				</ul>
				<p class="alternate">
					{t(lang, 'alternate')}: <span class="alt">{ALTERNATE_ROADS}</span>
				</p>
				{#if demo}
					<label class="demo-pick">
						{t(lang, 'startDemo')}:
						<select bind:value={demoEventId}>
							{#each USE_CASES as u (u.id)}
								<option value={u.id}>{u.name}</option>
							{/each}
						</select>
					</label>
				{/if}
			</article>
		{/each}

		<!-- Today's timeline accordion. -->
		<section class="accordion">
			<button class="acc-head" aria-expanded={timelineOpen} onclick={() => (timelineOpen = !timelineOpen)}>
				<span>{citizenT(lang, 'timeline_title')}</span>
				<span class="mono" aria-hidden="true">{timelineOpen ? '−' : '+'}</span>
			</button>
			{#if timelineOpen}
				<ul class="timeline">
					{#each events as ev (ev.eventId)}
						{#each ev.phases as ph, i (ev.eventId + ':' + i)}
							<li class="tl-row" data-phase={ph.state}>
								<span class="tl-state">
									{ph.state === 'completed'
										? citizenT(lang, 'timeline_lifted')
										: ph.state === 'planned'
											? citizenT(lang, 'timeline_planned')
											: t(lang, 'opsPhase')}
								</span>
								<span class="tl-label">{ph.label}</span>
								{#if ph.startTime}
									<span class="tl-clock mono">
										{localizeNumber(new Date(ph.startTime).getHours() + ':' + String(new Date(ph.startTime).getMinutes()).padStart(2, '0'), lang)}
									</span>
								{/if}
							</li>
						{/each}
					{/each}
					{#if events.every((e) => e.phases.length === 0)}
						<li class="tl-row"><span class="tl-state">{citizenT(lang, 'timeline_empty')}</span></li>
					{/if}
				</ul>
			{/if}
		</section>

		<!-- Light corridor map. -->
		<section class="map-card">
			<LightCorridorMap
				{lang}
				compact
				routes={events.flatMap((ev) =>
					ev.routeSegmentIds.map((sid) => ({ segmentId: sid, live: !isDemoEvent(ev) })),
				)}
			/>
		</section>
	{/if}

	<!-- Why roads hold (explainer accordion). -->
	<section class="accordion">
		<button class="acc-head" aria-expanded={explainerOpen} onclick={() => (explainerOpen = !explainerOpen)}>
			<span>{citizenT(lang, 'citizen_explainer_link')}</span>
			<span class="mono" aria-hidden="true">{explainerOpen ? '−' : '+'}</span>
		</button>
		{#if explainerOpen}
			<div class="explainer">
				<h3>{t(lang, 'why')}</h3>
				<p>{t(lang, 'whyBody')}</p>
			</div>
		{/if}
	</section>

	<!-- Subscribe (10-digit validation, honest demo note). -->
	<section class="subscribe">
		<h3>{t(lang, 'subscribeTitle')}</h3>
		<p>{t(lang, 'subscribeSub')}</p>
		<form onsubmit={(e) => { e.preventDefault(); subscribe(); }}>
			<input
				type="tel"
				inputmode="numeric"
				maxlength="10"
				placeholder={t(lang, 'phonePlaceholder')}
				aria-label={t(lang, 'phoneLabel')}
				bind:value={phone}
				data-testid="subscribe-phone"
			/>
			<select bind:value={alertLang} aria-label={t(lang, 'languageLabel')}>
				{#each SUPPORTED as s (s.code)}
					<option value={s.code}>{s.label}</option>
				{/each}
			</select>
			<Button type="submit" variant="secondary" size="citizen" disabled={!validPhone(phone)}>
				{t(lang, 'subscribeBtn')}
			</Button>
		</form>
		{#if subscribed}
			<p class="subscribed">{t(lang, 'subscribed')}</p>
			<div class="preview" data-testid="subscribe-preview">
				<Badge variant="fixture" label={t(lang, 'demoBadge')} />
				<strong>{t(lang, 'previewTitle')}</strong>
				<p>{previewText}</p>
			</div>
		{/if}
		<p class="demo-note">{citizenT(lang, 'subscribe_demo_note')}</p>
	</section>

	<!-- Emergency → /driver (a real route now). -->
	<section class="emergency">
		<h3>{t(lang, 'emergencyTitle')}</h3>
		<p>{t(lang, 'emergencyBody')}</p>
		<a class="cta" href="/driver" data-testid="driver-link">{t(lang, 'emergencyCta')} →</a>
	</section>

	<footer>
		<p class="demo-note">{t(lang, 'demoNote')}</p>
		<p class="privacy">{citizenT(lang, 'footer_privacy')}</p>
		<p>{t(lang, 'footerNote')}</p>
		<a href="/ops">{t(lang, 'backToOps')} →</a>
	</footer>
</div>

<style>
	.citizen {
		max-width: 640px;
		margin: 0 auto;
		padding: var(--v-space-4) var(--v-space-4) var(--v-space-6);
		display: flex;
		flex-direction: column;
		gap: var(--v-gap-panel);
		font-size: var(--v-text-md);
		line-height: 1.55;
		color: var(--v-ink-1);
	}

	.hero-row {
		display: flex;
		justify-content: space-between;
		align-items: center;
		gap: var(--v-space-2);
	}

	.brand {
		font-size: var(--v-text-sm);
		font-weight: var(--v-weight-status);
		letter-spacing: var(--v-tracking-status);
		text-transform: uppercase;
		color: var(--v-ink-2);
	}

	.kicker {
		margin: var(--v-space-2) 0 0;
		font-size: var(--v-text-xl);
		line-height: 1.15;
		color: var(--v-ink-1);
	}

	.tagline {
		margin: var(--v-space-1) 0 0;
		color: var(--v-ink-2);
	}

	.updated {
		margin: var(--v-space-1) 0 0;
		font-family: var(--v-font-mono);
		font-size: var(--v-text-xs);
		color: var(--v-ink-3);
	}

	.mono {
		font-family: var(--v-font-mono);
	}

	/* ── Band strip: 12% tints, plain language ─────────────────────────── */
	.strip {
		display: flex;
		align-items: center;
		gap: var(--v-space-3);
		padding: var(--v-space-3) var(--v-space-4);
		border-radius: var(--v-radius-panel);
		border: 1px solid var(--v-line-1);
	}

	.strip p {
		margin: 0;
		font-size: var(--v-text-sm);
		color: var(--v-ink-1);
	}

	.strip-dot {
		width: 10px;
		height: 10px;
		border-radius: 50%;
		flex-shrink: 0;
	}

	.strip.tier-calm {
		background: color-mix(in srgb, var(--v-band-green) 12%, transparent);
	}
	.strip.tier-calm .strip-dot {
		background: var(--v-band-green);
	}
	.strip.tier-watch {
		background: color-mix(in srgb, var(--v-band-amber) 12%, transparent);
	}
	.strip.tier-watch .strip-dot {
		background: var(--v-band-amber);
	}
	.strip.tier-hold {
		background: color-mix(in srgb, var(--v-band-red) 12%, transparent);
	}
	.strip.tier-hold .strip-dot {
		background: var(--v-band-red);
	}
	.strip.tier-severe {
		background: color-mix(in srgb, var(--v-ink-1) 8%, transparent);
		border-color: var(--v-act-text);
	}
	.strip.tier-severe .strip-dot {
		background: var(--v-act-text);
	}

	/* ── Event cards ────────────────────────────────────────────────────── */
	.event-card,
	.subscribe,
	.emergency,
	.accordion {
		background: var(--v-surface-1);
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-panel);
		padding: var(--v-space-4);
	}

	.card-head {
		display: flex;
		justify-content: space-between;
		align-items: baseline;
		gap: var(--v-space-2);
	}

	.card-head h2 {
		margin: 0;
		font-size: var(--v-text-lg);
	}

	.consequence {
		margin: var(--v-space-1) 0 var(--v-space-3);
		color: var(--v-ink-2);
		font-size: var(--v-text-sm);
	}

	.holds {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: var(--v-space-1);
	}

	.hold {
		display: flex;
		align-items: center;
		gap: var(--v-space-3);
		padding: var(--v-space-2) var(--v-space-3);
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-chip);
		font-size: var(--v-text-sm);
	}

	.hold[data-hold-state='now'] {
		border-color: color-mix(in srgb, var(--v-band-red) 45%, transparent);
		background: color-mix(in srgb, var(--v-band-red) 8%, transparent);
	}

	.seg {
		font-family: var(--v-font-mono);
		font-weight: 600;
		flex: 1;
	}

	.window {
		font-family: var(--v-font-mono);
		color: var(--v-ink-2);
	}

	.state {
		font-size: var(--v-text-2xs);
		font-weight: var(--v-weight-status);
		letter-spacing: var(--v-tracking-status);
		color: var(--v-ink-2);
	}

	.alternate {
		margin: var(--v-space-3) 0 0;
		font-size: var(--v-text-sm);
		color: var(--v-ink-2);
	}

	.alt {
		color: var(--v-data);
		font-weight: 600;
	}

	.demo-pick {
		display: block;
		margin-top: var(--v-space-3);
		font-size: var(--v-text-xs);
		color: var(--v-ink-3);
	}

	.demo-pick select {
		margin-left: var(--v-space-2);
		border: 1px solid var(--v-line-2);
		border-radius: var(--v-radius-chip);
		padding: var(--v-space-1) var(--v-space-2);
		font-size: var(--v-text-xs);
		max-width: 22rem;
		background: var(--v-surface-2);
		color: var(--v-ink-1);
	}

	/* ── Accordions ─────────────────────────────────────────────────────── */
	.acc-head {
		display: flex;
		width: 100%;
		justify-content: space-between;
		align-items: center;
		background: none;
		border: none;
		padding: 0;
		font: inherit;
		font-weight: 600;
		color: var(--v-ink-1);
		cursor: pointer;
	}

	.timeline {
		list-style: none;
		margin: var(--v-space-3) 0 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: var(--v-space-1);
	}

	.tl-row {
		display: flex;
		align-items: baseline;
		gap: var(--v-space-3);
		font-size: var(--v-text-sm);
		padding: var(--v-space-1) 0;
		border-bottom: 1px dashed var(--v-line-1);
	}

	.tl-state {
		font-size: var(--v-text-2xs);
		font-weight: var(--v-weight-status);
		letter-spacing: var(--v-tracking-status);
		color: var(--v-ink-3);
		min-width: 5.5em;
	}

	.tl-label {
		flex: 1;
		color: var(--v-ink-2);
	}

	.tl-clock {
		font-family: var(--v-font-mono);
		color: var(--v-ink-3);
	}

	.explainer h3 {
		margin: var(--v-space-3) 0 var(--v-space-1);
		font-size: var(--v-text-md);
	}

	.explainer p {
		margin: 0;
		color: var(--v-ink-2);
		font-size: var(--v-text-sm);
	}

	.map-card {
		border-radius: var(--v-radius-panel);
		overflow: hidden;
	}

	/* ── Subscribe ──────────────────────────────────────────────────────── */
	.subscribe h3,
	.emergency h3 {
		margin: 0 0 var(--v-space-1);
		font-size: var(--v-text-md);
	}

	.subscribe p,
	.emergency p {
		margin: 0 0 var(--v-space-3);
		color: var(--v-ink-2);
		font-size: var(--v-text-sm);
	}

	form {
		display: flex;
		gap: var(--v-space-2);
		flex-wrap: wrap;
		align-items: center;
	}

	form input,
	form select {
		border: 1px solid var(--v-line-2);
		border-radius: var(--v-radius-btn);
		padding: var(--v-space-2) var(--v-space-3);
		font-size: var(--v-text-sm);
		background: var(--v-surface-2);
		color: var(--v-ink-1);
		height: var(--v-ctl-touch);
	}

	form input:focus-visible,
	form select:focus-visible,
	.acc-head:focus-visible,
	.cta:focus-visible {
		outline: 1px solid var(--v-act-focus);
		outline-offset: 2px;
	}

	.subscribed {
		color: var(--v-band-green);
		font-weight: 600;
		margin: var(--v-space-2) 0 0;
	}

	.preview {
		margin-top: var(--v-space-2);
		border: 1px dashed var(--v-line-2);
		border-radius: var(--v-radius-chip);
		padding: var(--v-space-3);
		background: var(--v-surface-2);
		display: flex;
		flex-direction: column;
		gap: var(--v-space-1);
		align-items: flex-start;
	}

	.preview p {
		margin: 0;
		font-size: var(--v-text-sm);
	}

	.demo-note {
		margin: var(--v-space-2) 0 0;
		font-size: var(--v-text-xs);
		color: var(--v-ink-3);
	}

	/* ── Emergency ──────────────────────────────────────────────────────── */
	.emergency {
		border-color: color-mix(in srgb, var(--v-act-text) 35%, transparent);
	}

	.cta {
		display: inline-flex;
		align-items: center;
		height: var(--v-ctl-touch);
		padding: 0 var(--v-space-5);
		border-radius: var(--v-radius-btn);
		background: var(--v-act);
		color: var(--v-paper);
		font-weight: 600;
		font-size: var(--v-text-md);
		text-decoration: none;
	}

	footer {
		text-align: center;
		color: var(--v-ink-3);
		font-size: var(--v-text-xs);
	}

	footer p {
		margin: var(--v-space-1) 0;
	}

	footer a {
		color: var(--v-data);
		font-size: var(--v-text-sm);
	}
</style>
