<script lang="ts">
	/**
	 * `/` — THE VVIP CORRIDOR MODULE (the restored VVIP template): one module
	 * of the city platform, on the same God's-eye canvas as City Command.
	 *
	 * The original template's mental model, kept whole: a full-bleed live
	 * map nobody resizes, glass instruments floating OVER it (left = plan,
	 * right = monitor), mono numerals everywhere, saffron sweep, vignette
	 * and film grain — but every instrument is wired to the REAL stack:
	 * the God's-eye Cesium canvas (events + congestion ramp + traces +
	 * signals + units + cameras + the convoy on GPS), the two-tier brain
	 * planner (plan → RECOMMENDED primary spine with dashed alternates →
	 * named-officer escort → the convoy marker moving, diversions ahead),
	 * the audit ledger, the band ladder. No client-side simulation exists
	 * here: the HUD reads the escort status and the convoy GPS mirror.
	 *
	 * The (legacy) convoy routes (command/map/planning/simulation) are
	 * superseded by this page and were removed with it.
	 *
	 * The canvas HUD (sensor/share/tour, layer chips, AMA, legend, pick
	 * card) is inset past whatever floats over the stage — the open rails,
	 * the dossier drawer, and the stats pill above it — through the
	 * canvas's --ge-hud-left/right/top hooks, so no instrument hides a chip.
	 * The canvas's data credits (Google, Cesium ion, © OpenStreetMap
	 * contributors) keep the bottom-left strip below the planner rail and
	 * WRAP before the tool dock (--ge-credit-max-w, from the dock's measured
	 * width): the dock used to cover the providers' tail and 'Data
	 * attribution'. The left rail is only as tall as its panels, so the
	 * strip under them is the canvas's own (no transparent rail box over the
	 * credits' links).
	 *
	 * Operator gate on and nobody signed in: the cockpit is not mounted (no
	 * canvas layer reads the city only to be refused, no 'no live corridor
	 * events' over a board nobody may read) — one sign-in card stands in, as
	 * on /ops and City Command.
	 */
	import { onMount, onDestroy } from 'svelte';
	import { ops } from '$lib/ops/store.svelte';
	import OperatorSignIn from '$lib/shell/OperatorSignIn.svelte';
	import { tOps } from '$lib/i18n/strings.ops';
	import { godsEye } from '$lib/gods-eye/store.svelte';
	import { commandBar } from '$lib/shell/command-bar.svelte';
	import { buildShareUrl } from '$lib/gods-eye/share-link';
	import { GODS_EYE_LAYER_IDS } from '$lib/gods-eye/layers';
	import GodsEyeCanvas from '$lib/gods-eye/GodsEyeCanvas.svelte';
	import EventDossierDrawer from '$lib/ops/components/EventDossierDrawer.svelte';
	import StatusStrip from '$lib/ops/components/template/StatusStrip.svelte';
	import CorridorStatsPill from '$lib/ops/components/template/CorridorStatsPill.svelte';
	import PlannerRail from '$lib/ops/components/template/PlannerRail.svelte';
	import ThreatLadder from '$lib/ops/components/template/ThreatLadder.svelte';
	import TraceConsole from '$lib/ops/components/template/TraceConsole.svelte';
	import ToolDock from '$lib/ops/components/template/ToolDock.svelte';
	import ConvoyHud from '$lib/ops/components/template/ConvoyHud.svelte';
	import LiveRail from '$lib/ops/components/template/LiveRail.svelte';

	let leftOpen = $state(true);
	let rightOpen = $state(true);
	let traceOpen = $state(true);
	let dossierOpen = $state(false);
	/** Measured stats-pill height: the canvas HUD starts below it. */
	let pillHeight = $state(34);
	/** The stage, and the tool dock's measured width (the credit bar wraps before the dock). */
	let stageEl = $state<HTMLElement | null>(null);
	let dockWidth = $state<number | null>(null);

	$effect(() => {
		const dock = stageEl?.querySelector<HTMLElement>('[data-template="tool-dock"]');
		if (!dock) return;
		const measure = () => {
			const w = dock.offsetWidth;
			dockWidth = w > 0 ? w : null;
		};
		measure();
		if (typeof ResizeObserver === 'undefined') return;
		const ro = new ResizeObserver(measure);
		ro.observe(dock);
		return () => ro.disconnect();
	});
	/**
	 * The credit bar's widest extent: from the canvas's left edge to a gap
	 * before the centred dock (half the stage less half the dock, less the
	 * gap and the bar's own padding) — it wraps onto a second line instead of
	 * running under the dock.
	 */
	const creditMax = $derived(
		dockWidth ? `--ge-credit-max-w: calc(50% - ${Math.ceil(dockWidth / 2)}px - var(--v-space-3) * 2 - 12px);` : '',
	);

	let escortState = $state<{
		movementId: string | null;
		eventId: string | null;
		startedAt: number | null;
		plannedEtaSec: number | null;
		plannedDistanceM: number | null;
		phase: 'planned' | 'starting' | 'escorting' | 'stopping' | 'ended';
		endedStatus: 'completed' | 'stopped' | 'failed' | null;
		vvipClass: string;
		destination: string;
	} | null>(null);

	/** The operator gate's state is known (or the read gave up): mount the cockpit, or the sign-in. */
	let sessionWaitOver = $state(false);
	const sessionKnown = $derived(ops.sessionGate !== null || sessionWaitOver);

	onMount(() => {
		godsEye.followSelection = false; // free camera in the cockpit; /ops owns subject-follow
		ops.start();
		const wait = setTimeout(() => (sessionWaitOver = true), 2500);
		return () => clearTimeout(wait);
	});

	onDestroy(() => {
		godsEye.subjectLockId = null;
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
</script>

<svelte:head>
	<title>VVIP Corridor · Veer</title>
</svelte:head>

{#if ops.signInRequired}
	<!-- Gate on, nobody signed in: one sign-in card, no canvas, no refused reads. -->
	<div class="signin-wall" data-testid="cockpit-signin-required">
		<div class="signin-card">
			<h2>{tOps(ops.lang, 'opsSigninTitle')}</h2>
			<p>{tOps(ops.lang, 'cockpitSigninBody')}</p>
			<OperatorSignIn lang={ops.lang} inline />
		</div>
	</div>
{:else if !sessionKnown}
	<!-- the server render and the first instant: the cockpit's frame (the
	     route contract scripts check: 200 + data-template), mounted in full
	     once the gate's state is known -->
	<div class="cockpit" data-template="convoy-command" data-pending="session" aria-busy="true"></div>
{:else}
<div class="cockpit" data-template="convoy-command">
	<div class="sweep" aria-hidden="true"></div>
	<StatusStrip />

	<section
		class="stage"
		class:left-open={leftOpen}
		class:right-open={rightOpen}
		class:dossier-open={dossierOpen}
		style={`--pill-h: ${pillHeight}px;${creditMax}`}
		bind:this={stageEl}
	>
		<GodsEyeCanvas hud="wall" />

		<!-- vignette + film grain: the template's cinematic frame -->
		<div class="vignette" aria-hidden="true"></div>
		<div class="grain" aria-hidden="true"></div>

		<div class="pill-row" bind:clientHeight={pillHeight}>
			<CorridorStatsPill />
		</div>

		<button
			type="button"
			class="tab tab-left"
			class:open={leftOpen}
			onclick={() => (leftOpen = !leftOpen)}
			aria-pressed={leftOpen}
			aria-label="toggle planner rail"
		>
			{leftOpen ? '‹' : '›'}
		</button>

		<div class="rail-left" class:open={leftOpen}>
			<PlannerRail onEscortState={(s) => (escortState = s)} />
			<ThreatLadder />
		</div>

		<button
			type="button"
			class="tab tab-right"
			class:open={rightOpen}
			onclick={() => (rightOpen = !rightOpen)}
			aria-pressed={rightOpen}
			aria-label="toggle operations rail"
		>
			{rightOpen ? '›' : '‹'}
		</button>

		<div class="rail-right" class:open={rightOpen}>
			<LiveRail />
		</div>

		<ConvoyHud
			escort={escortState ?? {
				movementId: null,
				eventId: null,
				startedAt: null,
				plannedEtaSec: null,
				plannedDistanceM: null,
				phase: 'planned',
				endedStatus: null,
				vvipClass: '',
				destination: '',
			}}
		/>

		<ToolDock
			traceOpen={traceOpen}
			onToggleTrace={() => (traceOpen = !traceOpen)}
			dossierOpen={dossierOpen}
			onToggleDossier={() => (dossierOpen = !dossierOpen)}
		/>

		{#if traceOpen}
			<TraceConsole />
		{/if}

		{#if dossierOpen}
			<aside class="dossier" data-template="dossier-drawer">
				<button type="button" class="dossier-close" onclick={() => (dossierOpen = false)}>✕</button>
				<EventDossierDrawer />
			</aside>
		{/if}
	</section>
</div>
{/if}

<style>
	/* ── operator gate: sign-in required (no cockpit mounted) ──────────── */
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

	.cockpit {
		display: flex;
		flex-direction: column;
		height: 100%;
		overflow: hidden;
		position: relative;
	}

	/* the saffron sweep — the template's top edge signature */
	.sweep {
		height: 2px;
		flex-shrink: 0;
		background: linear-gradient(
			90deg,
			transparent,
			var(--v-act) 30%,
			var(--v-act-2) 50%,
			var(--v-act) 70%,
			transparent
		);
		background-size: 220% 100%;
		animation: cockpit-sweep var(--v-dur-sweep, 3.5s) linear infinite;
	}

	@keyframes cockpit-sweep {
		from {
			background-position: 180% 0;
		}
		to {
			background-position: -80% 0;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.sweep {
			animation: none;
			background-size: 100% 100%;
		}
	}

	.stage {
		--rail-left-w: 300px;
		--rail-right-w: 320px;
		position: relative;
		flex: 1;
		min-height: 0;
		overflow: hidden;
		/* canvas HUD insets (GodsEyeCanvas --ge-hud-*): below the pill,
		 * clear of the edge tabs while the rails are closed */
		--ge-hud-top: calc(var(--v-space-3) + var(--pill-h, 34px) + var(--v-space-2));
		--ge-hud-left: calc(var(--v-space-3) + 26px);
		--ge-hud-right: calc(var(--v-space-3) + 26px);
		/* floating instruments (ConvoyHud, TraceConsole): clear of the right
		 * rail when it is open, below the canvas HUD's two chip rows */
		--float-right: calc(var(--v-space-3) + 26px);
		--float-top: calc(var(--ge-hud-top) + 64px);
		--float-bottom: calc(var(--v-space-3) + 48px); /* above the tool dock */
	}

	.stage.left-open {
		--ge-hud-left: calc(var(--v-space-3) * 2 + var(--rail-left-w));
	}

	.stage.right-open {
		--ge-hud-right: calc(var(--v-space-3) * 2 + var(--rail-right-w));
		--float-right: calc(var(--v-space-3) * 2 + var(--rail-right-w));
	}

	.stage.dossier-open {
		--ge-hud-right: calc(min(420px, 92%) + var(--v-space-3));
	}

	.stage :global(.ge-wrap) {
		position: absolute;
		inset: 0;
	}

	/* the HUD reads above the vignette/grain veil, below every instrument */
	.stage :global(.ge-hud) {
		z-index: 6;
	}

	.vignette {
		position: absolute;
		inset: 0;
		background: radial-gradient(ellipse at center, transparent 58%, var(--v-veil) 100%);
		pointer-events: none;
		z-index: 5;
	}

	.grain {
		position: absolute;
		inset: 0;
		opacity: 0.025;
		background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='140' height='140'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2'/%3E%3C/filter%3E%3Crect width='140' height='140' filter='url(%23n)'/%3E%3C/svg%3E");
		pointer-events: none;
		z-index: 5;
	}

	.pill-row {
		position: absolute;
		top: var(--v-space-3);
		left: 50%;
		transform: translateX(-50%);
		z-index: 20;
	}

	.rail-left,
	.rail-right {
		position: absolute;
		top: var(--v-space-3);
		bottom: 64px;
		display: flex;
		flex-direction: column;
		gap: var(--v-gap-panel);
		overflow-y: auto;
		z-index: 20;
		transition: transform var(--v-dur-in) var(--v-ease), opacity var(--v-dur-out) var(--v-ease);
		scrollbar-width: thin;
	}

	/* only as tall as its panels (scrolling once they outgrow the stage):
	 * below them the strip is the canvas's own — the data credits' */
	.rail-left {
		left: var(--v-space-3);
		width: var(--rail-left-w);
		bottom: auto;
		max-height: calc(100% - var(--v-space-3) - 64px);
	}

	.rail-right {
		right: var(--v-space-3);
		width: var(--rail-right-w);
	}

	.rail-left:not(.open) {
		transform: translateX(-116%);
		opacity: 0;
		pointer-events: none;
	}

	.rail-right:not(.open) {
		transform: translateX(116%);
		opacity: 0;
		pointer-events: none;
	}

	/* the template's floating chevron tabs */
	.tab {
		position: absolute;
		top: 50%;
		transform: translateY(-50%);
		width: 26px;
		height: 52px;
		border: 1px solid var(--v-glass-border);
		border-radius: var(--v-radius-btn);
		background: var(--v-glass-bg);
		-webkit-backdrop-filter: blur(var(--v-glass-blur));
		backdrop-filter: blur(var(--v-glass-blur));
		color: var(--v-ink-2);
		font-size: var(--v-text-md);
		cursor: pointer;
		z-index: 21;
		transition: color var(--v-dur-out) var(--v-ease);
	}

	.tab:hover {
		color: var(--v-act-text);
		border-color: var(--v-act);
	}

	.tab-left {
		left: 0;
		border-left: none;
		border-radius: 0 var(--v-radius-btn) var(--v-radius-btn) 0;
	}

	.tab-right {
		right: 0;
		border-right: none;
		border-radius: var(--v-radius-btn) 0 0 var(--v-radius-btn);
	}

	.stage :global([data-template='trace-console']) {
		bottom: 64px;
	}

	.dossier {
		position: absolute;
		top: 0;
		right: 0;
		bottom: 0;
		width: min(420px, 92%);
		z-index: 40;
		padding: var(--v-space-4);
		padding-right: var(--v-space-3);
		background: var(--v-glass-bg);
		-webkit-backdrop-filter: blur(var(--v-glass-blur)) saturate(1.4);
		backdrop-filter: blur(var(--v-glass-blur)) saturate(1.4);
		border-left: 1px solid var(--v-glass-border);
		box-shadow: -12px 0 40px var(--v-veil);
		overflow-y: auto;
		animation: dossier-in var(--v-dur-in) var(--v-ease);
	}

	@keyframes dossier-in {
		from {
			transform: translateX(24px);
			opacity: 0;
		}
		to {
			transform: translateX(0);
			opacity: 1;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.dossier {
			animation: none;
		}
	}

	.dossier-close {
		position: absolute;
		top: var(--v-space-2);
		right: var(--v-space-2);
		width: var(--v-ctl-md);
		height: var(--v-ctl-md);
		border: 1px solid var(--v-line-2);
		border-radius: var(--v-radius-btn);
		background: var(--v-surface-1);
		color: var(--v-ink-2);
		cursor: pointer;
	}

	@media (max-width: 1100px) {
		.stage {
			--rail-left-w: min(280px, 86vw);
			--rail-right-w: min(280px, 86vw);
		}
	}
</style>
