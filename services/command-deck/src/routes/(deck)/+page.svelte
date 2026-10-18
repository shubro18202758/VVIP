<script lang="ts">
	/**
	 * `/` — THE CONVOY COMMAND COCKPIT (the restored VVIP template).
	 *
	 * The original template's mental model, kept whole: a full-bleed live
	 * map nobody resizes, glass instruments floating OVER it (left = plan,
	 * right = monitor), mono numerals everywhere, saffron sweep, vignette
	 * and film grain — but every instrument is wired to the REAL stack:
	 * the God's-eye Cesium canvas (events + live congestion ramp + traces
	 * + signals + units + cameras), the two-tier brain planner, the audit
	 * ledger, the band ladder. No client-side simulation exists here.
	 *
	 * The (legacy) convoy routes (command/map/planning/simulation) are
	 * superseded by this page and were removed with it.
	 */
	import { onMount, onDestroy } from 'svelte';
	import { ops } from '$lib/ops/store.svelte';
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

	let escortState = $state<{
		movementId: string | null;
		startedAt: number | null;
		plannedEtaMin: number | null;
		vvipClass: string;
		destination: string;
	} | null>(null);

	onMount(() => {
		godsEye.followSelection = false; // free camera in the cockpit; /ops owns subject-follow
		ops.start();
	});

	onDestroy(() => {
		godsEye.subjectLockId = null;
		ops.stop();
	});

	// R3: this page owns band / lang / share on the command bar.
	$effect(() => {
		commandBar.band = ops.band;
		commandBar.bandSource = ops.bandSource === 'none' ? null : ops.bandSource;
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
					style: 'flir',
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
	<title>Convoy Command · VVIP Deck</title>
</svelte:head>

<div class="cockpit" data-template="convoy-command">
	<div class="sweep" aria-hidden="true"></div>
	<StatusStrip />

	<section class="stage">
		<GodsEyeCanvas hud="wall" />

		<!-- vignette + film grain: the template's cinematic frame -->
		<div class="vignette" aria-hidden="true"></div>
		<div class="grain" aria-hidden="true"></div>

		<div class="pill-row">
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

		<ConvoyHud escort={escortState ?? { movementId: null, startedAt: null, plannedEtaMin: null, vvipClass: '', destination: '' }} />

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

<style>
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
		position: relative;
		flex: 1;
		min-height: 0;
		overflow: hidden;
	}

	.stage :global(.ge-wrap) {
		position: absolute;
		inset: 0;
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
		width: 300px;
		display: flex;
		flex-direction: column;
		gap: var(--v-gap-panel);
		overflow-y: auto;
		z-index: 20;
		transition: transform var(--v-dur-in) var(--v-ease), opacity var(--v-dur-out) var(--v-ease);
		scrollbar-width: thin;
	}

	.rail-left {
		left: var(--v-space-3);
	}

	.rail-right {
		right: var(--v-space-3);
		width: 320px;
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
		.rail-left,
		.rail-right {
			width: min(280px, 86vw);
		}
	}
</style>
