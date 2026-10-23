<script lang="ts">
	/**
	 * /ops — THE SITUATION WALL. Three-column rebuild on the design system:
	 *
	 *   (1) full-width band masthead + duty-officer gate (the C6 identity)
	 *       + the brain posture strip (decider / Gemma / degradation tier,
	 *       traffic-feed provenance, IST clock);
	 *   (2) four commander counters (DataTiles, each with its own badge);
	 *   (3) LEFT decision queue (108 corridor requests, e-challans, pursuit
	 *       openings) + the event board with its C-F lifecycle verbs +
	 *       agency dispatch + watchlist · CENTER God's-eye wall canvas
	 *       (auto-orbit, pursuit overlay) with the signal gate card and AMA
	 *       module card under it · RIGHT situation band panel, event dossier
	 *       (per-event audit narrative, choke plan), emulator control plane,
	 *       twin compare, degradation ledger;
	 *   (4) full-width audit stream — a view over the server audit (C-C),
	 *       pointer auto-pause, rung-labelled.
	 *
	 * Wall layout (≥1500 px): the grid row is minmax(0, 1fr) and stretched,
	 * so every column is exactly the row's height and scrolls INSIDE itself
	 * (nothing paints under the audit band); the canvas frame is a flex box
	 * at least 420 px tall, so the Cesium canvas fills it; the audit band is
	 * a fixed-height strip that scrolls on its own. Below 1500 px the page
	 * scrolls as one column with the canvas first.
	 *
	 * CommandBar state is assigned in the $effect below (the R3 contract:
	 * pages → $lib/shell/**, one-way). Every surface is live-first with a
	 * labelled fallback per panel.
	 *
	 * Deep links: City Command and the dossiers link here as /ops#evt=<id>,
	 * #rec=<id> and #dsp=<id>. The hash is read on mount and on every
	 * hashchange, the id validated (safePathId): an event is SELECTED once
	 * the board carries it; an e-challan / dispatch card is scrolled into the
	 * middle of its column and highlighted for 3 s — retried until the first
	 * polls have rendered it (20 s at most).
	 *
	 * Operator gate on and nobody signed in: the wall is not mounted (no
	 * operator poller fires, no 403 per panel) — one banner with the sign-in
	 * form stands in.
	 */
	import { onMount, onDestroy, tick } from 'svelte';
	import { ops } from '$lib/ops/store.svelte';
	import { safePathId } from '$lib/ops/endpoints';
	import OperatorSignIn from '$lib/shell/OperatorSignIn.svelte';
	import { godsEye } from '$lib/gods-eye/store.svelte';
	import { POLL_MS } from '$lib/ops/endpoints';
	import { commandBar } from '$lib/shell/command-bar.svelte';
	import { buildShareUrl } from '$lib/gods-eye/share-link';
	import { GODS_EYE_LAYER_IDS } from '$lib/gods-eye/layers';
	import { t } from '$lib/i18n/strings';
	import { tOps } from '$lib/i18n/strings.ops';
	import Panel from '$lib/primitives/Panel.svelte';
	import Badge from '$lib/primitives/Badge.svelte';
	import SituationMasthead from '$lib/ops/components/SituationMasthead.svelte';
	import OpsCounters from '$lib/ops/components/OpsCounters.svelte';
	import DecisionQueue from '$lib/ops/components/DecisionQueue.svelte';
	import DispatchTiles from '$lib/ops/components/DispatchTiles.svelte';
	import WatchlistConsole from '$lib/ops/components/WatchlistConsole.svelte';
	import SignalGateCard from '$lib/ops/components/SignalGateCard.svelte';
	import AMAModuleCard from '$lib/ops/components/AMAModuleCard.svelte';
	import EventBoard from '$lib/ops/components/EventBoard.svelte';
	import SituationBandPanel from '$lib/ops/components/SituationBandPanel.svelte';
	import TwinComparePanel from '$lib/ops/components/TwinComparePanel.svelte';
	import EventDossierDrawer from '$lib/ops/components/EventDossierDrawer.svelte';
	import AuditStream from '$lib/ops/components/AuditStream.svelte';
	import EmulatorPanel from '$lib/ops/components/EmulatorPanel.svelte';
	import DegradationLedger from '$lib/ops/components/DegradationLedger.svelte';
	import GodsEyeCanvas from '$lib/gods-eye/GodsEyeCanvas.svelte';

	/** The operator gate's state is known (or the read gave up): mount the wall, or the sign-in. */
	let sessionWaitOver = $state(false);
	const sessionKnown = $derived(ops.sessionGate !== null || sessionWaitOver);

	// ── deep links: #evt= / #rec= / #dsp= ────────────────────────────────────
	let deepTimer: ReturnType<typeof setTimeout> | null = null;
	let deepToken = 0;

	/** The deep link in a hash, validated; null when the hash carries none. */
	function parseDeepLink(hash: string): { kind: 'evt' | 'rec' | 'dsp'; id: string } | null {
		const params = new URLSearchParams(hash.replace(/^#/, ''));
		for (const kind of ['evt', 'rec', 'dsp'] as const) {
			const id = safePathId(params.get(kind));
			if (id) return { kind, id };
		}
		return null;
	}

	/** The card a rec / dsp link points at, once it is rendered. */
	function cardFor(kind: 'rec' | 'dsp', id: string): HTMLElement | null {
		const q = (sel: string) => document.querySelector<HTMLElement>(sel);
		const esc = CSS.escape(id);
		if (kind === 'rec') {
			return (
				q(`[data-testid="queue-card"][data-kind="echallan"][data-id="${esc}"]`) ??
				q(`[data-testid="queue-card"][data-rec-ids~="${esc}"]`)
			);
		}
		return (
			q(`[data-testid="queue-card"][data-kind="corridor-request"][data-id="${esc}"]`) ??
			q(`[data-testid="dispatch-tile"][data-dispatch-id="${esc}"]`)
		);
	}

	/** Bring the card into view and mark it for 3 s (its own decision stays the officer's). */
	function highlight(el: HTMLElement) {
		const target = (el.firstElementChild as HTMLElement | null) && el.matches('.card-wrap, [style*="contents"]') ? (el.firstElementChild as HTMLElement) : el;
		target.scrollIntoView({ block: 'center', behavior: 'smooth' });
		target.classList.add('deep-link-hit');
		setTimeout(() => target.classList.remove('deep-link-hit'), 3000);
	}

	/** Resolve the hash's deep link; retried until the first polls rendered its target (≤ 20 s). */
	function followDeepLink(started = Date.now(), token = ++deepToken) {
		if (deepTimer) clearTimeout(deepTimer);
		deepTimer = null;
		if (token !== deepToken) return;
		const link = parseDeepLink(location.hash);
		if (!link) return;
		let done = false;
		if (link.kind === 'evt') {
			if (ops.events.some((e) => e.eventId === link.id)) {
				if (ops.selectedEventId !== link.id) ops.selectEvent(link.id);
				done = true;
			}
		} else {
			const el = cardFor(link.kind, link.id);
			if (el) {
				highlight(el);
				done = true;
			}
		}
		if (!done && Date.now() - started < 20_000) {
			deepTimer = setTimeout(() => followDeepLink(started, token), 500);
		}
	}

	const onHashChange = () => followDeepLink();

	onMount(() => {
		godsEye.followSelection = true; // wall mode: selecting an event locks the camera
		ops.start();
		const wait = setTimeout(() => (sessionWaitOver = true), 2500);
		window.addEventListener('hashchange', onHashChange);
		void tick().then(() => followDeepLink());
		return () => clearTimeout(wait);
	});

	onDestroy(() => {
		godsEye.followSelection = false;
		godsEye.subjectLockId = null;
		if (typeof window !== 'undefined') window.removeEventListener('hashchange', onHashChange);
		if (deepTimer) clearTimeout(deepTimer);
		deepToken++;
		ops.stop();
	});

	// R3: this page owns band / lang / share on the command bar.
	$effect(() => {
		commandBar.band = ops.band;
		commandBar.bandSource = ops.bandSourceKind === 'none' ? null : ops.bandSource;
		commandBar.lang = ops.lang;
		commandBar.onLangChange = (lang) => (ops.lang = lang);
		commandBar.onShare = () => {
			const url = buildShareUrl(
				{
					lat: (22.95 + 23.12) / 2,
					lon: (72.5 + 72.7) / 2,
					alt: 2600,
					heading: 0,
					pitch: -35,
					roll: 0,
					style: 'raw', // COMMAND — the canvas default sensor
					bhot: false,
					layers: [...GODS_EYE_LAYER_IDS],
				},
				GODS_EYE_LAYER_IDS,
			);
			void navigator.clipboard?.writeText(url).catch(() => undefined);
		};
	});

	const queueStatus: { state: 'live' | 'stale'; updatedAt: number } | null = $derived(
		ops.recommendationsUpdated || ops.dispatchesUpdated
			? { state: ops.recommendationsLive === false && ops.dispatchesLive === false ? 'stale' : 'live', updatedAt: Math.max(ops.recommendationsUpdated, ops.dispatchesUpdated) }
			: null,
	);
</script>

<svelte:head>
	<title>{t(ops.lang, 'opsTitle')} · VVIP Command Deck</title>
</svelte:head>

{#if ops.signInRequired}
	<!-- Gate on, nobody signed in: one banner, no wall, no pollers. -->
	<div class="signin-wall" data-testid="ops-signin-required">
		<div class="signin-card">
			<h2>{tOps(ops.lang, 'opsSigninTitle')}</h2>
			<p>{tOps(ops.lang, 'opsSigninBody')}</p>
			<OperatorSignIn lang={ops.lang} inline />
		</div>
	</div>
{:else if !sessionKnown}
	<div class="wall pending" aria-busy="true"></div>
{:else}
<div class="wall">
	{#if ops.sessionLapsed}
		<p class="lapsed" data-testid="ops-session-lapsed">{tOps(ops.lang, 'opsSessionLapsed')}</p>
	{/if}
	<SituationMasthead />
	<OpsCounters />

	<div class="wall-grid">
		<!-- LEFT — the decision queue + the event board -->
		<div class="col col-left">
			{#snippet queueHeadRight()}
				{#if !ops.officerOn}
					<Badge variant="pending" label={tOps(ops.lang, 'queueLockedNoOfficer')} />
				{/if}
			{/snippet}
			<Panel
				title={tOps(ops.lang, 'queueTitle')}
				sub={tOps(ops.lang, 'queueSub')}
				status={queueStatus}
				pollMs={Math.max(POLL_MS.enforcement, POLL_MS.dispatch)}
				lang={ops.lang}
				scroll
				{...{ 'header-right': queueHeadRight }}
			>
				<DecisionQueue />
			</Panel>
			<EventBoard />
			<DispatchTiles />
			<WatchlistConsole />
		</div>

		<!-- CENTER — the canvas is the hero; chrome is furniture -->
		<div class="col col-center">
			<section class="canvas-frame">
				<GodsEyeCanvas hud="wall" />
			</section>
			<div class="under-canvas">
				<SignalGateCard />
				<AMAModuleCard />
			</div>
		</div>

		<!-- RIGHT — band panel, twin compare, dossier, degradation ledger -->
		<div class="col col-right">
			<SituationBandPanel />
			<EventDossierDrawer />
			<EmulatorPanel />
			<TwinComparePanel />
			<DegradationLedger />
		</div>
	</div>

	<!-- (4) full-width audit stream (its own scroll strip on the wall) -->
	<div class="audit-band">
		<AuditStream />
	</div>
</div>
{/if}

<style>
	.wall {
		display: flex;
		flex-direction: column;
		gap: var(--v-gap-panel);
		padding: var(--v-space-3);
		height: 100%;
		overflow-y: auto;
	}

	.wall-grid {
		display: grid;
		grid-template-columns: minmax(320px, 1fr);
		gap: var(--v-gap-panel);
		align-items: start;
	}

	/* ── operator gate: sign-in required (no wall mounted) ─────────────── */
	.signin-wall {
		height: 100%;
		display: grid;
		place-items: center;
		padding: var(--v-space-4);
	}

	.signin-card {
		width: min(440px, 100%);
		display: flex;
		flex-direction: column;
		gap: var(--v-space-3);
		padding: var(--v-space-5);
		background: var(--v-surface-1);
		border: 1px solid var(--v-line-2);
		border-left: 2px solid var(--v-act);
		border-radius: var(--v-radius-panel);
	}

	.signin-card h2 {
		margin: 0;
		font-family: var(--v-font-display);
		font-size: var(--v-text-lg);
		color: var(--v-ink-1);
	}

	.signin-card p {
		margin: 0;
		font-size: var(--v-text-sm);
		color: var(--v-ink-2);
	}

	.lapsed {
		margin: 0;
		padding: var(--v-space-2) var(--v-space-3);
		font-size: var(--v-text-sm);
		color: var(--v-ink-1);
		border: 1px solid var(--v-act-text);
		border-radius: var(--v-radius-panel);
	}

	/* a deep link's target: outlined for 3 s, then back to the wall's rhythm */
	.wall :global(.deep-link-hit) {
		outline: 2px solid var(--v-act-text);
		outline-offset: 2px;
		box-shadow: 0 0 0 6px var(--v-act-veil);
		transition: outline-color var(--v-dur-out) var(--v-ease);
	}

	.col {
		display: flex;
		flex-direction: column;
		gap: var(--v-gap-panel);
		min-width: 0;
	}

	/* A flex box, so the canvas (.ge-wrap) fills the frame — a percentage
	 * height on an auto-height parent collapsed it to its 240 px floor. */
	.canvas-frame {
		position: relative;
		display: flex;
		height: clamp(360px, 48vh, 560px);
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-panel);
		overflow: hidden;
		background: var(--v-canvas);
	}

	.canvas-frame > :global(.ge-wrap) {
		flex: 1;
		height: auto;
		min-width: 0;
	}

	.under-canvas {
		display: grid;
		grid-template-columns: minmax(0, 1.15fr) minmax(0, 1fr);
		gap: var(--v-gap-panel);
		align-items: start;
	}

	@media (min-width: 1500px) {
		.wall {
			overflow: hidden;
		}

		.wall-grid {
			grid-template-columns: minmax(340px, 1fr) minmax(0, 1.7fr) minmax(330px, 1fr);
			grid-template-rows: minmax(0, 1fr);
			align-items: stretch;
			min-height: 0;
			flex: 1;
		}

		.col {
			min-height: 0;
			overflow-y: auto;
			overscroll-behavior: contain;
			scrollbar-width: thin;
		}

		/* panels keep their natural height (Panel is min-height: 0) — the
		 * column scrolls instead of squashing them into the row */
		.col > :global(*) {
			flex-shrink: 0;
		}

		/* The hero: the column's height less a peek of the cards below it
		 * (which scroll into view inside the centre column). The 420 px floor
		 * never exceeds the column itself — at 1600×900 the column is ~386
		 * px, and a taller frame would clip the canvas and its attribution. */
		.canvas-frame {
			flex: none;
			height: max(min(420px, 100%), calc(100% - 56px));
		}

		/* A fixed strip: the stream's own feed is the scroller inside it
		 * (Panel fill), so the newest row stays in view. While the audit
		 * probe is open the strip grows, so its answer never pushes the feed
		 * below the fold (≥ 3 rows stay visible). */
		.audit-band {
			flex: none;
			display: flex;
			flex-direction: column;
			height: clamp(170px, 22vh, 250px);
			overflow: hidden;
		}

		.audit-band:has(:global([data-probe-open])) {
			height: clamp(300px, 38vh, 420px);
		}

		.audit-band > :global([data-prim='panel']) {
			flex: 1;
			min-height: 0;
		}
	}

	/* A short wide screen (1600×900 projector): the strip gives the canvas
	 * its ≥ 400 px — the feed still scrolls inside it, and an open audit
	 * probe still grows it (the :has rule above is more specific). */
	@media (min-width: 1500px) and (max-height: 960px) {
		.audit-band {
			height: 170px;
		}
	}

	@media (max-width: 1499px) {
		/* single-column flow: the hero canvas leads, not 2000 px down */
		.col-center {
			order: -1;
		}
	}

	@media (max-width: 1100px) {
		.under-canvas {
			grid-template-columns: minmax(0, 1fr);
		}
	}
</style>
