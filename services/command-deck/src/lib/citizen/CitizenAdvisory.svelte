<script lang="ts">
	/**
	 * CitizenAdvisory — "Your road, next 2 hours" (Daylight, trilingual).
	 *
	 * Fed ONLY by the public feed (public-feed.svelte.ts → the deck's
	 * server-side projection): public-kind events, signage road names, the
	 * hold length with its provenance, a road-graph alternate and a
	 * plain-language tier. No operator store, no band codes, no pursuit or
	 * crime events, no titles. One card per event with mono localized
	 * windows; the hold's provenance as the oracle labels it (closure plan,
	 * live adaptation, or the standard baseline — never a baseline passed
	 * off as live); an honest "not published" when a hold length is unknown,
	 * "no signed alternate" when the graph offers none, and "unavailable
	 * right now" when the read failed. Only an active event holds "now";
	 * a planned one reads "next" only inside the 2 h horizon, and events
	 * further out sit in a dated Later list. Every clock is IST, prefixed
	 * with its day when that is not today. An event the emulator opened is
	 * badged SIMULATED FEED · EMULATED (never LIVE), its routes are drawn
	 * non-live and it never moves the strip. The labelled offline preview
	 * stands in only while the feed is unreachable. Subscribe flow (10-digit
	 * validation, honest demo note, preview in the chosen alert language —
	 * the page's language until the citizen picks another) and the
	 * emergency card → /driver.
	 */
	import { SUPPORTED, kindLabel, localizeNumber, t } from '$lib/i18n/strings';
	import { citizenT, consequenceLabel, phaseLabelText } from '$lib/i18n/strings.citizen';
	import { composeAdvisory, factsFromPublicEvent, holdSourceKey } from '$lib/comms/advisory';
	import { clockAtMs, holdAnchorMs, stretchHolds, type StripTier } from './holds';
	import { publicFeed } from './public-feed.svelte';
	import type { PublicCorridorEvent, PublicLaterEvent } from './public-model';
	import Badge from '$lib/primitives/Badge.svelte';
	import Button from '$lib/primitives/Button.svelte';
	import EmptyState from '$lib/primitives/EmptyState.svelte';
	import LightCorridorMap from './LightCorridorMap.svelte';

	let { lang }: { lang: string } = $props();

	let phone = $state('');
	/** The citizen's own pick; until then alerts follow the page language. */
	let alertLangPicked = $state<string | null>(null);
	const alertLang = $derived(alertLangPicked ?? lang);
	let subscribed = $state(false);
	let timelineOpen = $state(false);
	let explainerOpen = $state(false);

	const events = $derived<PublicCorridorEvent[]>(publicFeed.events);
	const later = $derived<PublicLaterEvent[]>(publicFeed.later);
	const preview = $derived(publicFeed.preview);
	const live = $derived(publicFeed.status === 'live' || publicFeed.status === 'stale');
	const tier = $derived<StripTier | null>(live ? publicFeed.tier : null);
	/** Emulated rows on the page (cards or Later): they get the explicit note. */
	const anyEmulated = $derived(events.some((e) => e.source === 'emulated') || later.some((e) => e.source === 'emulated'));
	/** The hero never reads LIVE when every event it would vouch for is emulated. */
	const heroEmulated = $derived(live && events.length > 0 && events.every((e) => e.source === 'emulated'));

	/** When the advisory feed last answered (IST) — no claim about live traffic. */
	const updatedLabel = $derived(
		publicFeed.updatedAt && !preview ? `${t(lang, 'updated')} · ${clockAtMs(publicFeed.updatedAt, lang)}` : '',
	);

	function stripKey(tr: StripTier | null): string {
		if (tr === null) return publicFeed.status === 'connecting' ? 'citizen_feed_connecting' : 'citizen_feed_offline';
		return tr === 'calm' ? 'strip_calm' : tr === 'watch' ? 'strip_watch' : tr === 'hold' ? 'strip_hold' : 'strip_severe';
	}

	function stretchName(roadName: string | null, segmentId: number): string {
		return roadName ?? `${t(lang, 'freezeZone')} ${localizeNumber(segmentId, lang)}`;
	}

	function validPhone(p: string): boolean {
		return /^[0-9]{10}$/.test(p.replace(/\s+/g, ''));
	}

	function subscribe() {
		if (validPhone(phone)) subscribed = true;
	}

	/** The exact citizen draft the comms desk would write, in the alert language. */
	const previewText = $derived.by(() => {
		const ev = events[0];
		if (!ev) return citizenT(alertLang, 'citizen_preview_none');
		return composeAdvisory(factsFromPublicEvent(ev), 'citizens', alertLang).text;
	});
</script>

<div class="citizen" data-screen="citizen" data-feed={publicFeed.status}>
	<header class="hero">
		<div class="hero-row">
			<strong class="brand">{t(lang, 'brand')}</strong>
			{#if !live}
				<Badge variant="fixture" label={t(lang, 'opsSourceOffline')} />
			{:else if heroEmulated}
				<span class="source-badge" data-source-badge="emulated"><Badge variant="emulated" label={citizenT(lang, 'citizen_emulated_badge')} /></span>
			{:else}
				<span class="source-badge" data-source-badge="live"><Badge variant="live" label={t(lang, 'opsSourceLive')} /></span>
			{/if}
		</div>
		<h1 class="kicker">{citizenT(lang, 'citizen_kicker')}</h1>
		<p class="tagline">{t(lang, 'tagline')}</p>
		{#if updatedLabel}
			<p class="updated mono" data-testid="citizen-updated">{updatedLabel}</p>
		{/if}
	</header>

	<!-- Plain-language strip: never the internal band code. -->
	<section class="strip tier-{tier ?? 'unknown'}" data-strip={tier ?? 'unknown'} aria-live="polite">
		<span class="strip-dot" aria-hidden="true"></span>
		<p>{citizenT(lang, stripKey(tier))}</p>
	</section>

	{#if publicFeed.status === 'stale'}
		<p class="feed-note" data-feed-note="stale">{citizenT(lang, 'citizen_feed_stale')}</p>
	{:else if preview}
		<p class="feed-note" data-feed-note="preview">
			<Badge variant="fixture" label={t(lang, 'opsSourceOffline')} />
			<span class="note-text">{citizenT(lang, 'citizen_feed_offline')}</span>
		</p>
	{/if}
	{#if anyEmulated && !preview}
		<p class="feed-note" data-feed-note="emulated">
			<Badge variant="emulated" label={citizenT(lang, 'citizen_emulated_badge')} />
			<span class="note-text">{citizenT(lang, 'citizen_emulated_note')}</span>
		</p>
	{/if}

	{#if events.length === 0}
		<EmptyState
			title={publicFeed.status === 'connecting' ? citizenT(lang, 'citizen_feed_connecting') : citizenT(lang, 'citizen_none_now')}
			hint={t(lang, 'statusClearSub')}
		/>
	{:else}
		<!-- One card per public corridor event. -->
		{#each events as ev (ev.eventId)}
			{@const holds = stretchHolds(ev.stretches, ev.hold.minutes, holdAnchorMs(ev), lang, ev.state === 'active')}
			{@const demo = ev.source === 'offline-fixture'}
			{@const emulated = ev.source === 'emulated'}
			<article class="event-card" class:emulated data-event={ev.eventId} data-kind={ev.eventKind} data-source={ev.source}>
				<header class="card-head">
					<h2>{kindLabel(lang, ev.eventKind)}</h2>
					{#if emulated}
						<span class="source-badge" data-source-badge="emulated"><Badge variant="emulated" label={citizenT(lang, 'citizen_emulated_badge')} /></span>
					{:else}
						<span class="source-badge" data-source-badge={demo ? 'fixture' : 'live'}><Badge variant={demo ? 'fixture' : 'live'} label={demo ? t(lang, 'opsSourceOffline') : t(lang, 'opsSourceLive')} /></span>
					{/if}
				</header>
				<p class="consequence">{consequenceLabel(lang, ev.eventKind, ev.state !== 'active')}</p>
				{#if holds.length}
					<ul class="holds">
						{#each holds as h (h.segmentId)}
							<li class="hold" data-hold-state={h.state}>
								<span class="seg">{stretchName(h.roadName, h.segmentId)}</span>
								<span class="window mono">
									{t(lang, 'freezeMinutes', localizeNumber(h.minutes, lang))}
									· {citizenT(lang, 'card_window_at', h.clock)}
								</span>
								<!-- an emulated hold is a demonstration: never 'Holding now' on a real road -->
								<span class="state">
									{h.state === 'now'
										? citizenT(lang, emulated ? 'card_sim_now' : 'card_now')
										: h.state === 'next'
											? citizenT(lang, emulated ? 'card_sim_soon' : 'card_soon')
											: '→'}
								</span>
							</li>
						{/each}
					</ul>
					<p class="provenance" data-hold-source={ev.hold.source}>
						{citizenT(lang, holdSourceKey(ev.hold.source))}
					</p>
				{:else if ev.unavailable.hold}
					<p class="hold-unknown" data-hold-state="unavailable">{citizenT(lang, 'card_hold_unavailable')}</p>
				{:else}
					<p class="hold-unknown" data-hold-state="unpublished">{citizenT(lang, 'card_hold_unknown')}</p>
				{/if}
				{#if ev.alternate}
					<p class="alternate" data-alternate="graph">
						{t(lang, 'alternate')}: <span class="alt">{ev.alternate.roadNames.join(' / ')}</span>
						<span class="provenance">· {citizenT(lang, 'card_alt_from_graph')}</span>
					</p>
				{:else if ev.unavailable.alternate}
					<p class="alternate" data-alternate="unavailable">{citizenT(lang, 'card_alt_unavailable')}</p>
				{:else}
					<p class="alternate" data-alternate="none">{citizenT(lang, 'card_alt_none')}</p>
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
							{@const ended = ph.endTime != null && Date.parse(ph.endTime) < Date.now()}
							<li class="tl-row" data-phase={ended ? 'completed' : 'planned'}>
								<span class="tl-state">
									{ended ? citizenT(lang, 'timeline_lifted') : citizenT(lang, 'timeline_planned')}
								</span>
								<span class="tl-label" data-phase-label={ph.label}>{kindLabel(lang, ev.eventKind)} · {phaseLabelText(lang, ph.label, i + 1)}</span>
								{#if ph.startTime}
									<span class="tl-clock mono" data-ts={ph.startTime}>{clockAtMs(Date.parse(ph.startTime), lang)}</span>
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
	{/if}

	{#if later.length}
		<!-- Beyond the 2 h horizon: dated heads-up rows only (no hold plan yet). -->
		<section class="later" data-testid="citizen-later">
			<h3>{citizenT(lang, 'citizen_later_title')}</h3>
			<ul>
				{#each later as ev (ev.eventId)}
					<li class="later-row" data-event={ev.eventId} data-kind={ev.eventKind} data-source={ev.source} data-starts={ev.startsAt ?? ''}>
						<span class="later-when mono">
							{ev.startsAt != null ? clockAtMs(ev.startsAt, lang) : citizenT(lang, 'citizen_later_unscheduled')}
						</span>
						<span class="later-what">
							<strong>{kindLabel(lang, ev.eventKind)}</strong>
							{#if ev.roadsWithheld}
								<span class="later-roads" data-roads-withheld="true"> · {citizenT(lang, 'citizen_later_roads_withheld')}</span>
							{:else if ev.roadNames.length}
								<span class="later-roads"> · {ev.roadNames.join(' / ')}</span>
							{/if}
						</span>
						{#if ev.source === 'emulated'}
							<span class="source-badge" data-source-badge="emulated"><Badge variant="emulated" label={citizenT(lang, 'citizen_emulated_badge')} /></span>
						{/if}
					</li>
				{/each}
			</ul>
			<p class="provenance">{citizenT(lang, 'citizen_later_note')}</p>
		</section>
	{/if}

	<!-- Light corridor map (always mounted: an empty city is information too). -->
	<section class="map-card">
		<LightCorridorMap
			{lang}
			compact
			routes={events.flatMap((ev) => ev.stretches.map((s) => ({ segmentId: s.segmentId, live: ev.source === 'live' })))}
		/>
	</section>

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
			<select
				value={alertLang}
				onchange={(e) => (alertLangPicked = e.currentTarget.value)}
				aria-label={t(lang, 'languageLabel')}
				data-testid="subscribe-lang"
			>
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
			<div class="preview" data-testid="subscribe-preview" lang={alertLang}>
				<Badge variant="fixture" label={t(lang, 'demoBadge')} />
				<strong>{t(lang, 'previewTitle')}</strong>
				<p>{previewText}</p>
			</div>
		{/if}
		<p class="demo-note">{citizenT(lang, 'subscribe_demo_note')}</p>
	</section>

	<!-- Emergency → /driver. -->
	<section class="emergency">
		<h3>{t(lang, 'emergencyTitle')}</h3>
		<p>{t(lang, 'emergencyBody')}</p>
		<a class="cta" href="/driver" data-testid="driver-link">{t(lang, 'emergencyCta')} →</a>
	</section>

	<footer>
		{#if preview}
			<p class="demo-note">{t(lang, 'demoNote')}</p>
		{/if}
		<p class="privacy">{citizenT(lang, 'footer_privacy')}</p>
		<p>{t(lang, 'footerNote')}</p>
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

	/* the source badge drops under the brand on a phone — it never squeezes
	 * the brand into three lines */
	.hero-row {
		display: flex;
		flex-wrap: wrap;
		justify-content: space-between;
		align-items: center;
		gap: var(--v-space-1) var(--v-space-2);
	}

	.brand {
		flex: 1 1 12rem;
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
	.strip.tier-unknown {
		border-style: dashed;
	}
	.strip.tier-unknown .strip-dot {
		background: var(--v-line-2);
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

	/* the source badge wraps under the kind on a phone — never past the card edge */
	.card-head {
		display: flex;
		flex-wrap: wrap;
		justify-content: space-between;
		align-items: baseline;
		gap: var(--v-space-1) var(--v-space-2);
	}

	.card-head h2 {
		flex: 1 1 10rem;
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

	/* A hold row WRAPS on a phone: the road name keeps its line and the
	 * window / state drop below it — they never print over the name. */
	.hold {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: var(--v-space-1) var(--v-space-3);
		padding: var(--v-space-2) var(--v-space-3);
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-chip);
		font-size: var(--v-text-sm);
	}

	.hold[data-hold-state='now'] {
		border-color: color-mix(in srgb, var(--v-band-red) 45%, transparent);
		background: color-mix(in srgb, var(--v-band-red) 8%, transparent);
	}

	/* A simulated hold never wears the live 'holding now' alarm. */
	.event-card.emulated .hold[data-hold-state='now'] {
		border-style: dashed;
		border-color: var(--v-line-2);
		background: transparent;
	}

	/* Signage road names read as text; only the numbers stay mono. */
	.seg {
		font-weight: 600;
		flex: 1 1 9rem;
		min-width: 8ch;
		overflow-wrap: anywhere;
	}

	.window {
		font-family: var(--v-font-mono);
		color: var(--v-ink-2);
		white-space: nowrap;
	}

	.state {
		font-size: var(--v-text-2xs);
		font-weight: var(--v-weight-status);
		letter-spacing: var(--v-tracking-status);
		color: var(--v-ink-2);
		white-space: nowrap;
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

	.provenance {
		margin: var(--v-space-1) 0 0;
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.alternate .provenance {
		margin: 0;
	}

	.hold-unknown {
		margin: 0;
		font-size: var(--v-text-sm);
		color: var(--v-ink-3);
		border: 1px dashed var(--v-line-2);
		border-radius: var(--v-radius-chip);
		padding: var(--v-space-2) var(--v-space-3);
	}

	/* badge + note: side by side when there is room, the note under the
	 * badge on a phone (never a sliver of a column beside it) */
	.feed-note {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: var(--v-space-1) var(--v-space-2);
		margin: 0;
		font-size: var(--v-text-xs);
		color: var(--v-ink-3);
	}

	.note-text {
		flex: 1 1 14rem;
		min-width: 0;
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

	.source-badge {
		display: inline-flex;
		flex-shrink: 0;
	}

	/* An emulated card is a demonstration: dashed, never the live hairline. */
	.event-card.emulated {
		border-style: dashed;
		border-color: var(--v-line-2);
	}

	/* ── Later (beyond the 2 h horizon) ─────────────────────────────────── */
	.later {
		background: var(--v-surface-1);
		border: 1px dashed var(--v-line-2);
		border-radius: var(--v-radius-panel);
		padding: var(--v-space-4);
	}

	.later h3 {
		margin: 0 0 var(--v-space-2);
		font-size: var(--v-text-md);
	}

	.later ul {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: var(--v-space-1);
	}

	.later-row {
		display: flex;
		align-items: baseline;
		flex-wrap: wrap;
		gap: var(--v-space-2) var(--v-space-3);
		font-size: var(--v-text-sm);
		padding: var(--v-space-1) 0;
		border-bottom: 1px dashed var(--v-line-1);
	}

	.later-when {
		font-family: var(--v-font-mono);
		color: var(--v-ink-1);
		min-width: 9em;
	}

	.later-what {
		flex: 1;
		min-width: 0;
		color: var(--v-ink-2);
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
		/* the CTA FILL is the deep act (act-2): paper text on it clears 4.5:1
		 * (tests/design-tokens.mjs asserts it) — the bright act stays for
		 * edges and dots */
		background: var(--v-act-2);
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

</style>
