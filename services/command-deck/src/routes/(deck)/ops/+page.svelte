<script lang="ts">
	/**
	 * /ops — THE SITUATION WALL. Three-column rebuild on the design system:
	 *
	 *   (1) full-width band masthead + duty-officer gate (the C6 identity);
	 *   (2) four commander counters (DataTiles, each with its own badge);
	 *   (3) LEFT decision queue (e-challans, 108 corridor requests, pursuit
	 *       confirmations) + event board · CENTER God's-eye wall canvas
	 *       (auto-orbit, pursuit overlay) with the signal gate card and AMA
	 *       module card under it · RIGHT situation band panel, twin compare,
	 *       event dossier, degradation ledger;
	 *   (4) full-width audit stream (pointer auto-pause, rung-labelled).
	 *
	 * CommandBar state is assigned in the $effect below (the R3 contract:
	 * pages → $lib/shell/**, one-way). Every surface is live-first with a
	 * labelled fallback per panel.
	 */
	import { onMount, onDestroy } from 'svelte';
	import { ops } from '$lib/ops/store.svelte';
	import { godsEye } from '$lib/gods-eye/store.svelte';
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

	onMount(() => {
		godsEye.followSelection = true; // wall mode: selecting an event locks the camera
		ops.start();
	});

	onDestroy(() => {
		godsEye.followSelection = false;
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

	const queueStatus: { state: 'live' | 'stale'; updatedAt: number } | null = $derived(
		ops.recommendationsUpdated || ops.dispatchesUpdated
			? { state: ops.recommendationsLive === false && ops.dispatchesLive === false ? 'stale' : 'live', updatedAt: Math.max(ops.recommendationsUpdated, ops.dispatchesUpdated) }
			: null,
	);
</script>

<svelte:head>
	<title>{t(ops.lang, 'opsTitle')} · VVIP Command Deck</title>
</svelte:head>

<div class="wall">
	<SituationMasthead />
	<OpsCounters />

	<div class="wall-grid">
		<!-- LEFT — the decision queue + the event board -->
		<div class="col col-left">
			{#snippet queueHeadRight()}
				{#if !ops.officerOn}
					<Badge variant="pending" label={tOps(ops.lang, 'gateRefusedNoOfficer')} />
				{/if}
			{/snippet}
			<Panel
				title={tOps(ops.lang, 'queueTitle')}
				sub={tOps(ops.lang, 'queueSub')}
				status={queueStatus}
				lang={ops.lang}
				scroll
				{...{ 'header-right': queueHeadRight }}
			>
				<DecisionQueue />
			</Panel>
			<DispatchTiles />
			<WatchlistConsole />
			<EventBoard />
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
			<TwinComparePanel />
			<EventDossierDrawer />
			<EmulatorPanel />
			<DegradationLedger />
		</div>
	</div>

	<!-- (4) full-width audit stream -->
	<AuditStream />
</div>

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

	.col {
		display: flex;
		flex-direction: column;
		gap: var(--v-gap-panel);
		min-width: 0;
	}

	.canvas-frame {
		position: relative;
		height: clamp(360px, 48vh, 560px);
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-panel);
		overflow: hidden;
		background: var(--v-canvas);
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
			min-height: 0;
			flex: 1;
		}

		.col {
			min-height: 0;
			overflow-y: auto;
		}

		.canvas-frame {
			height: auto;
			flex: 1;
			min-height: 420px;
		}

		.col-center {
			overflow: hidden;
		}
	}

	@media (max-width: 1100px) {
		.under-canvas {
			grid-template-columns: minmax(0, 1fr);
		}
	}
</style>
