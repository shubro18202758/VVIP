<script lang="ts">
	/**
	 * /gods-eye — VEER CITY COMMAND: one live, fused, explainable picture of
	 * Ahmedabad on the God's-eye canvas.
	 *
	 *   full-bleed canvas      every layer of the fused city registry
	 *                          (GET /api/v1/city/entities, contract C-C)
	 *   CITY VITALS  (left)    posture, decisions waiting on a human, units,
	 *                          ANPR, signals, feed freshness, AI tiers, ethics
	 *   ENTITY DOSSIER (right) whatever is selected — map pick, rail row,
	 *                          vital — with its 1-hop neighbours and audit
	 *   FUSION RAIL  (bottom)  the city narrative on human / machine rails,
	 *                          LIVE ◉ / REPLAY (an emulator tape on the clock)
	 *
	 * The route is a fixed overlay above the app shell, so it carries its own
	 * way out: the bar's nav, the canvas's EXIT chip and Esc (Esc first closes
	 * the dossier, then leaves for the situation wall). The share link
	 * (#v=2&…&sel=<urn>) restores camera, sensor, layers and the selected
	 * entity — a control room opens exactly what the sender saw.
	 *
	 * The overlay covers the shell's command bar, so it carries its own
	 * operator identity too: the on-duty chip (or the sign-in control) sits
	 * in its bar, and with the operator gate on and nobody signed in the
	 * canvas is not mounted at all — one 'Operator sign-in required' panel
	 * with the sign-in form stands in (no 403 storm, no preview fixtures
	 * passed off as the city). Signing in reloads the page with the hash,
	 * so a shared view opens as sent. The mode pill says LIVE only while the
	 * fused registry and the city feed answer — NO DATA otherwise.
	 */
	import { onMount, onDestroy } from 'svelte';
	import { goto } from '$app/navigation';
	import GodsEyeCanvas from '$lib/gods-eye/GodsEyeCanvas.svelte';
	import CityVitals from '$lib/gods-eye/hud/CityVitals.svelte';
	import EntityDossier from '$lib/gods-eye/hud/EntityDossier.svelte';
	import FusionRail from '$lib/gods-eye/hud/FusionRail.svelte';
	import { godsEye } from '$lib/gods-eye/store.svelte';
	import { selection } from '$lib/gods-eye/entities/selection.svelte';
	import { replay } from '$lib/gods-eye/hud/replay.svelte';
	import { cityRegistry } from '$lib/gods-eye/entities/registry.svelte';
	import { fusionFeed } from '$lib/gods-eye/hud/feed.svelte';
	import { ops } from '$lib/ops/store.svelte';
	import OperatorSignIn from '$lib/shell/OperatorSignIn.svelte';
	import { operatorSession } from '$lib/shell/session.svelte';
	import { commandBar } from '$lib/shell/command-bar.svelte';
	import { SUPPORTED, t } from '$lib/i18n/strings';
	import '$lib/i18n/packs';

	let clock = $state('--:--:--');
	let clockTimer: ReturnType<typeof setInterval> | null = null;

	function tickClock() {
		clock = new Intl.DateTimeFormat('en-GB', {
			timeZone: 'Asia/Kolkata',
			hour: '2-digit',
			minute: '2-digit',
			second: '2-digit',
			hour12: false,
		}).format(new Date());
	}

	/**
	 * The glass panels' footprint over the canvas (CSS px). The canvas lands
	 * every flight's subject in the region they leave clear — a selected
	 * entity never hides under the dossier, the vitals or the rail. Measured
	 * from the live layout (ResizeObserver); the dossier's width comes from
	 * its token (--dossier-w) so the inset is right in the same update that
	 * opens it, before the drawer has laid out.
	 */
	let stageEl: HTMLElement | null = $state(null);
	let vitalsEl: HTMLElement | null = $state(null);
	let railEl: HTMLElement | null = $state(null);
	let panels = $state({ left: 0, bottom: 0, gap: 12, dossierW: 392 });
	let observer: ResizeObserver | null = null;

	/**
	 * The canvas HUD chip row (EXIT … PURSUIT) above the clear region — a
	 * floor: the canvas measures the row as it lays out (two lines once the
	 * dossier narrows it) and keeps the lower edge.
	 */
	const HUD_ROW_PX = 52;

	function measurePanels() {
		if (!stageEl) return;
		const stage = stageEl.getBoundingClientRect();
		const vitals = vitalsEl?.getBoundingClientRect();
		const rail = railEl?.getBoundingClientRect();
		const gap = vitals ? Math.max(0, vitals.left - stage.left) : 12;
		const dossierW = parseFloat(getComputedStyle(stageEl).getPropertyValue('--dossier-w'));
		panels = {
			left: vitals ? vitals.right - stage.left + gap : 0,
			bottom: rail ? stage.bottom - rail.top + gap : 0,
			gap,
			dossierW: Number.isFinite(dossierW) && dossierW > 0 ? dossierW : 392,
		};
	}

	/**
	 * The stage mounts once the operator gate's state is known (or after a
	 * short wait if the session read never answers — the deck then behaves
	 * as before): an anonymous console under the gate gets the sign-in panel
	 * instead, and the canvas never starts its pollers.
	 */
	let sessionWaitOver = $state(false);
	const sessionKnown = $derived(operatorSession.gate !== null || sessionWaitOver);
	const signInNeeded = $derived(operatorSession.gate === true && !operatorSession.officer);
	const stageReady = $derived(sessionKnown && !signInNeeded);

	onMount(() => {
		godsEye.followSelection = false; // City Command: a free camera; selections fly on request
		const wait = setTimeout(() => (sessionWaitOver = true), 2500);
		ops.start();
		tickClock();
		clockTimer = setInterval(tickClock, 1000);
		measurePanels();
		if (typeof ResizeObserver !== 'undefined') {
			observer = new ResizeObserver(() => measurePanels());
			for (const el of [stageEl, vitalsEl, railEl]) if (el) observer.observe(el);
		}
		return () => clearTimeout(wait);
	});

	// The stage's glass panels appear once it mounts: measure them then.
	$effect(() => {
		if (!stageReady || !observer) return;
		queueMicrotask(() => {
			measurePanels();
			for (const el of [stageEl, vitalsEl, railEl]) if (el) observer?.observe(el);
		});
	});

	// <html lang> and the shell's language follow City Command's switch (the
	// shell bar is covered by this overlay, its state still owns <html lang>).
	$effect(() => {
		commandBar.lang = ops.lang;
		commandBar.onLangChange = (l: string) => (ops.lang = l);
	});

	onDestroy(() => {
		if (clockTimer) clearInterval(clockTimer);
		observer?.disconnect();
		if (replay.mode === 'replay') replay.exit();
		selection.clear();
		ops.stop();
	});

	function exitToWall() {
		void goto('/ops');
	}

	const lang = $derived(ops.lang);
	const dossierOpen = $derived(!!selection.current);
	/**
	 * What the mode pill may claim: LIVE only while the fused registry and
	 * the city feed both answer; NO DATA while either is down / refused /
	 * not deployed (the note says which); CONNECTING before the first answer.
	 */
	const dataState = $derived.by(() => {
		const states = [cityRegistry.status, fusionFeed.status];
		if (states.some((s) => s === 'down' || s === 'not-deployed')) return 'no-data' as const;
		if (states.every((s) => s === 'ok')) return 'live' as const;
		return 'connecting' as const;
	});
	const dataNote = $derived(
		[cityRegistry.note, fusionFeed.note].filter((n): n is string => !!n).join(' · ') || t(lang, 'cc_live_hint'),
	);
	const clearInsets = $derived({
		left: panels.left,
		right: dossierOpen ? panels.dossierW + panels.gap * 2 : panels.gap,
		top: HUD_ROW_PX,
		bottom: panels.bottom,
	});
</script>

<svelte:head>
	<title>{t(lang, 'cc_title')} · Veer</title>
	<meta name="robots" content="noindex" />
</svelte:head>

<main class="city-command" data-template="city-command" class:dossier-open={dossierOpen} class:replaying={replay.mode === 'replay'}>
	<div class="sweep" aria-hidden="true"></div>
	<header class="cc-bar">
		<div class="brand">
			<span class="brand-mark" aria-hidden="true">◎</span>
			<strong>VEER</strong>
			<span class="brand-sep">·</span>
			<span class="brand-title">{t(lang, 'cc_title')}</span>
			<span class="brand-city">{t(lang, 'cc_city')}</span>
		</div>
		<nav class="cc-nav" aria-label="operator views">
			<a href="/gods-eye" aria-current="page" class="on">◎ {t(lang, 'nav_city_command')}</a>
			<a href="/">◈ {t(lang, 'nav_dashboard')}</a>
			<a href="/ops">⬢ {t(lang, 'nav_ops')}</a>
			<a href="/comms">✎ {t(lang, 'nav_comms')}</a>
		</nav>
		<span
			class="mode-state mono"
			class:replay={replay.mode === 'replay'}
			class:no-data={replay.mode !== 'replay' && (signInNeeded || dataState === 'no-data')}
			data-testid="cc-mode"
			data-state={replay.mode === 'replay' ? 'replay' : signInNeeded ? 'signin' : dataState}
			title={replay.mode === 'replay' ? t(lang, 'cc_live_hint') : dataNote}
		>
			{#if replay.mode === 'replay'}
				◐ {t(lang, 'cc_replay')} · {t(lang, 'cc_emulated')}
			{:else if signInNeeded}
				<i class="off-dot" aria-hidden="true"></i> {t(lang, 'cc_signin_required')}
			{:else if dataState === 'no-data'}
				<i class="off-dot" aria-hidden="true"></i> {t(lang, 'cc_no_data')}
			{:else if dataState === 'connecting'}
				<i class="off-dot" aria-hidden="true"></i> {t(lang, 'cc_connecting')}
			{:else}
				<i class="live-dot" aria-hidden="true"></i> {t(lang, 'cc_live')}
			{/if}
		</span>
		<span class="cc-officer" data-testid="cc-officer"><OperatorSignIn {lang} /></span>
		<div class="lang" role="group" aria-label="language">
			{#each SUPPORTED as l (l.code)}
				<button type="button" class:on={lang === l.code} onclick={() => (ops.lang = l.code)}>{l.label}</button>
			{/each}
		</div>
		<span class="clock mono" title="India Standard Time">{clock} <small>IST</small></span>
	</header>

	{#if signInNeeded}
		<!-- Gate on, nobody signed in: no canvas, no pollers — one honest panel. -->
		<section class="gate-panel" data-testid="cc-signin-required" aria-label={t(lang, 'cc_signin_required')}>
			<div class="gate-card">
				<span class="gate-mark" aria-hidden="true">◎</span>
				<h2>{t(lang, 'cc_signin_title')}</h2>
				<p>{t(lang, 'cc_signin_body')}</p>
				<OperatorSignIn {lang} inline />
				<p class="gate-note mono">{t(lang, 'cc_signin_hash_note')}</p>
			</div>
		</section>
	{:else if stageReady}
		<section class="stage" bind:this={stageEl}>
			<GodsEyeCanvas hud="full" onexit={exitToWall} {clearInsets} />
			<aside class="glass vitals-panel" aria-label={t(lang, 'cc_vitals')} bind:this={vitalsEl}>
				<CityVitals />
			</aside>
			{#if dossierOpen}
				<aside class="glass dossier-panel" aria-label={t(lang, 'cc_dossier')}>
					<EntityDossier />
				</aside>
			{/if}
			<section class="glass rail-panel" aria-label={t(lang, 'cc_rail')} bind:this={railEl}>
				<FusionRail />
			</section>
		</section>
	{:else}
		<section class="stage pending" aria-busy="true"></section>
	{/if}
</main>

<style>
	.city-command {
		/* Cinematic: escape the app layout entirely (EXIT / nav / Esc lead back). */
		position: fixed;
		inset: 0;
		z-index: 40;
		background: var(--v-canvas);
		display: flex;
		flex-direction: column;
		--vitals-w: 300px;
		--dossier-w: 392px;
		--rail-h: 214px;
		--gap: var(--v-space-3);
	}

	.sweep {
		height: 2px;
		flex-shrink: 0;
		background: linear-gradient(90deg, transparent, var(--v-data) 30%, var(--v-act-2) 50%, var(--v-data) 70%, transparent);
		background-size: 220% 100%;
		animation: cc-sweep var(--v-dur-sweep, 3.5s) linear infinite;
	}

	@keyframes cc-sweep {
		from {
			background-position: 180% 0;
		}
		to {
			background-position: -80% 0;
		}
	}

	.cc-bar {
		display: flex;
		align-items: center;
		gap: var(--v-space-4);
		padding: var(--v-space-1) var(--v-space-4);
		min-height: 40px;
		background: linear-gradient(90deg, var(--v-surface-sink), var(--v-surface-1), var(--v-surface-sink));
		border-bottom: 1px solid var(--v-line-1);
		flex-shrink: 0;
	}

	.brand {
		display: flex;
		align-items: baseline;
		gap: var(--v-space-2);
		white-space: nowrap;
	}

	.brand-mark {
		color: var(--v-data);
		font-size: var(--v-text-md);
	}

	.brand strong {
		font-family: var(--v-font-display);
		font-size: var(--v-text-md);
		letter-spacing: 0.18em;
		color: var(--v-ink-1);
	}

	.brand-sep {
		color: var(--v-ink-3);
	}

	.brand-title {
		font-family: var(--v-font-display);
		font-size: var(--v-text-sm);
		font-weight: 700;
		letter-spacing: 0.14em;
		text-transform: uppercase;
		color: var(--v-data);
	}

	.brand-city {
		font-size: var(--v-text-xs);
		color: var(--v-ink-3);
		letter-spacing: 0.06em;
	}

	.cc-nav {
		display: flex;
		gap: var(--v-space-1);
		margin-left: var(--v-space-3);
	}

	.cc-nav a {
		font-size: var(--v-text-xs);
		color: var(--v-ink-3);
		text-decoration: none;
		padding: 3px var(--v-space-2);
		border: 1px solid transparent;
		border-radius: var(--v-radius-btn);
		white-space: nowrap;
	}

	.cc-nav a:hover {
		color: var(--v-ink-1);
		border-color: var(--v-line-1);
	}

	/* active nav: line-select edge, never orange */
	.cc-nav a.on {
		color: var(--v-ink-1);
		border-color: var(--v-line-select);
		background: var(--v-surface-2);
	}

	.mode-state {
		margin-left: auto;
		display: inline-flex;
		align-items: center;
		gap: 6px;
		font-size: var(--v-text-2xs);
		letter-spacing: 0.12em;
		color: var(--v-ink-2);
	}

	.mode-state.replay {
		color: var(--v-tool);
	}

	/* no live claim while the registry / feed is down or the gate wants a sign-in */
	.mode-state.no-data {
		color: var(--v-band-amber);
	}

	.off-dot {
		width: 8px;
		height: 8px;
		border-radius: var(--v-radius-pill);
		border: 1px solid currentColor;
	}

	.cc-officer {
		display: inline-flex;
		align-items: center;
	}

	/* ── operator gate: sign-in required (no canvas) ─────────────────────── */
	.gate-panel {
		flex: 1;
		min-height: 0;
		display: grid;
		place-items: center;
		background: radial-gradient(ellipse at center, var(--v-surface-1), var(--v-canvas) 70%);
	}

	.gate-card {
		width: min(420px, calc(100vw - 32px));
		display: flex;
		flex-direction: column;
		gap: var(--v-space-3);
		padding: var(--v-space-5);
		background: var(--v-surface-2);
		border: 1px solid var(--v-line-2);
		border-radius: var(--v-radius-panel);
		box-shadow: 0 10px 32px var(--v-veil);
	}

	.gate-mark {
		color: var(--v-data);
		font-size: var(--v-text-xl);
	}

	.gate-card h2 {
		margin: 0;
		font-family: var(--v-font-display);
		font-size: var(--v-text-lg);
		color: var(--v-ink-1);
	}

	.gate-card p {
		margin: 0;
		font-size: var(--v-text-sm);
		color: var(--v-ink-2);
	}

	.gate-note {
		font-size: var(--v-text-xs);
		color: var(--v-ink-3);
	}

	.live-dot {
		width: 8px;
		height: 8px;
		border-radius: var(--v-radius-pill);
		background: var(--v-band-green);
		box-shadow: 0 0 8px var(--v-band-green);
		animation: breathe var(--v-dur-breathe) var(--v-ease) infinite;
	}

	@keyframes breathe {
		50% {
			opacity: 0.5;
		}
	}

	.lang {
		display: inline-flex;
		border: 1px solid var(--v-line-2);
		border-radius: var(--v-radius-pill);
		overflow: hidden;
	}

	.lang button {
		padding: 1px var(--v-space-2);
		background: transparent;
		border: none;
		color: var(--v-ink-3);
		font: inherit;
		font-size: var(--v-text-2xs);
		cursor: pointer;
	}

	.lang button.on {
		background: var(--v-surface-3);
		color: var(--v-ink-1);
	}

	.clock {
		font-size: var(--v-text-sm);
		color: var(--v-ink-2);
		font-variant-numeric: tabular-nums;
	}

	.clock small {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	.stage {
		position: relative;
		flex: 1;
		min-height: 0;
		overflow: hidden;
		/* the canvas HUD row sits between the vitals rail and the dossier */
		--ge-hud-left: calc(var(--gap) * 2 + var(--vitals-w));
		--ge-hud-right: var(--gap);
		--ge-hud-top: var(--gap);
	}

	.city-command.dossier-open .stage {
		--ge-hud-right: calc(var(--gap) * 2 + var(--dossier-w));
	}

	.stage :global(.ge-wrap) {
		position: absolute;
		inset: 0;
		border-radius: 0;
	}

	.stage :global(.ge-hud) {
		z-index: 6;
	}

	.glass {
		position: absolute;
		z-index: 20;
		padding: var(--v-space-3);
		background: var(--v-glass-bg);
		-webkit-backdrop-filter: blur(var(--v-glass-blur)) saturate(1.3);
		backdrop-filter: blur(var(--v-glass-blur)) saturate(1.3);
		border: 1px solid var(--v-glass-border);
		border-radius: var(--v-radius-panel);
		box-shadow: 0 10px 32px var(--v-veil);
	}

	.vitals-panel {
		top: var(--gap);
		left: var(--gap);
		bottom: calc(var(--rail-h) + var(--gap) * 2);
		width: var(--vitals-w);
		overflow-y: auto;
		scrollbar-width: thin;
	}

	.dossier-panel {
		top: var(--gap);
		right: var(--gap);
		bottom: calc(var(--rail-h) + var(--gap) * 2);
		width: var(--dossier-w);
		overflow-y: auto;
		scrollbar-width: thin;
		animation: dossier-in var(--v-dur-in) var(--v-ease);
	}

	@keyframes dossier-in {
		from {
			transform: translateX(16px);
			opacity: 0;
		}
	}

	.rail-panel {
		left: var(--gap);
		right: var(--gap);
		bottom: var(--gap);
		height: var(--rail-h);
		padding: var(--v-space-2) var(--v-space-3);
		display: flex;
		flex-direction: column;
		min-height: 0;
	}

	.mono {
		font-family: var(--v-font-mono);
	}

	@media (max-width: 1400px) {
		.city-command {
			--vitals-w: 260px;
			--dossier-w: 340px;
		}

		.cc-nav {
			display: none;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.sweep,
		.live-dot,
		.dossier-panel {
			animation: none;
		}
	}
</style>
