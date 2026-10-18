/**
 * signal-network layer — city junction signal state.
 *
 * Feed: GET /api/v1/signals/state (canvas store; contract C1, served by the
 * signalling stream off corridor.signal_programs + corridor.junctions).
 * Junction dots colored by the ACTIVE PHASE of the corridor's signal
 * program from the band-ladder tokens (green/amber/red; unknown/off dim);
 * radius scales with green_ratio. Source-labelled (osm_assumed vs
 * calibrated) in the pick description. 404 → "not deployed" chip.
 */

import { godsEye } from '../store.svelte';
import type { GodsEyeLayer, GodsEyeLayerContext, GodsEyeLayerInstance } from '../registry';
import { cssColor } from '../theme';

export const SIGNAL_NETWORK_PICK_PREFIX = 'sig:';

/**
 * Junction-phase colours from the band ladder tokens (feed-healthy green /
 * band amber / band red — the band ladder is the only sanctioned use of
 * amber). Unknown phases sit dim; null (no program) is hollow ink.
 */
function phaseColor(phase: string | null, cesium: CesiumGlobal): unknown {
	if (!phase) return cssColor(cesium, '--v-ink-4', cesium.Color.GRAY);
	const key = phase.toLowerCase();
	if (key.includes('green')) return cssColor(cesium, '--v-band-green', cesium.Color.LIME);
	if (key.includes('amber') || key.includes('yellow')) return cssColor(cesium, '--v-band-amber', cesium.Color.ORANGE);
	if (key.includes('red')) return cssColor(cesium, '--v-band-red', cesium.Color.RED);
	return cssColor(cesium, '--v-ink-3', cesium.Color.LIGHTGRAY);
}

function createSignalNetworkLayer(): GodsEyeLayerInstance {
	let ctx: GodsEyeLayerContext | null = null;
	let active = false;
	let timer: ReturnType<typeof setInterval> | null = null;
	const entityIds: string[] = [];
	let lastSignature = '';

	function draw() {
		if (!ctx || !active) return;
		const { cesium, viewer } = ctx;
		const signals = godsEye.signals;
		const sig = JSON.stringify([
			signals.map((s) => [s.junctionId, s.activePhase, s.greenRatio]),
			godsEye.signalsFeed.status,
		]);
		if (sig === lastSignature) return;
		lastSignature = sig;

		for (const id of entityIds) viewer.entities.removeById(id);
		entityIds.length = 0;

		for (const s of signals) {
			const size = 6 + Math.round((s.greenRatio ?? 0.3) * 10);
			const entity = viewer.entities.add({
				id: `${SIGNAL_NETWORK_PICK_PREFIX}${s.junctionId}`,
				position: cesium.Cartesian3.fromDegrees(s.lon, s.lat, 8),
				point: {
					pixelSize: size,
					color: phaseColor(s.activePhase, cesium),
					outlineColor: cssColor(cesium, '--v-canvas', cesium.Color.BLACK),
					outlineWidth: 1,
				},
				description: `junction ${s.junctionId} · phase ${s.activePhase ?? '—'} · cycle ${
					s.cycleSec ?? '—'
				}s · green_ratio ${s.greenRatio != null ? s.greenRatio.toFixed(2) : '—'} · source ${s.source ?? '—'}`,
			});
			entityIds.push(entity.id);
		}
	}

	return {
		async status() {
			return { configured: true };
		},
		init(layerCtx) {
			ctx = layerCtx;
		},
		activate() {
			active = true;
			draw();
			timer = setInterval(draw, 2000);
		},
		deactivate() {
			active = false;
			if (timer) clearInterval(timer);
			timer = null;
			if (ctx) for (const id of entityIds) ctx.viewer.entities.removeById(id);
			entityIds.length = 0;
			lastSignature = '';
		},
		refresh() {
			lastSignature = '';
			void godsEye.pollSignals();
			draw();
		},
		handlePick(pickId) {
			return pickId.startsWith(SIGNAL_NETWORK_PICK_PREFIX);
		},
		coverageStats() {
			return {
				count: entityIds.length,
				loading: godsEye.signalsFeed.status === 'empty' && godsEye.signalsFeed.updatedAt === 0,
				hint:
					godsEye.signalsFeed.status === 'not-deployed'
						? '/api/v1/signals/state 404 — signalling stream pending'
						: `${godsEye.signals.length} junctions`,
				error: godsEye.signalsFeed.note,
			};
		},
		destroy() {
			this.deactivate();
		},
	};
}

export const signalNetworkLayer: GodsEyeLayer = {
	id: 'signals',
	name: 'Signal network state',
	label: 'SIGNALS',
	requiresKeyId: null,
	pickPrefix: SIGNAL_NETWORK_PICK_PREFIX,
	defaultOn: true,
	create: createSignalNetworkLayer,
};
