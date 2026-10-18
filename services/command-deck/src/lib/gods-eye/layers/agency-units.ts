/**
 * agency-units layer — Jan Rakshak / police / traffic unit markers.
 *
 * Feed: GET /api/v1/units (canvas store; contract C1, served by the
 * signalling stream off corridor.dispatches + emulator agency_unit frames).
 * Agency shapes the color (jan_rakshak → saffron, police → blue, traffic →
 * teal, other → violet); every emulated unit carries a persistent EMULATED
 * badge in its label — the C6 ethics rail. 404 → "not deployed" chip.
 */

import { godsEye } from '../store.svelte';
import type { GodsEyeLayer, GodsEyeLayerContext, GodsEyeLayerInstance } from '../registry';
import { cssColor } from '../theme';

export const AGENCY_UNITS_PICK_PREFIX = 'unit:';

/**
 * Agency colours from the tokens: Jan Rakshak units carry the HUMAN-ACT
 * orange (volunteers coordinated, never automated); police/traffic take the
 * data accent / tool accent; unknown agencies sit dim. No hex literals —
 * theme.ts reads the live token values with Cesium fallbacks.
 */
function agencyColor(agency: string, cesium: CesiumGlobal): unknown {
	switch (agency.toLowerCase()) {
		case 'jan_rakshak':
			return cssColor(cesium, '--v-act', cesium.Color.ORANGE);
		case 'police':
			return cssColor(cesium, '--v-data', cesium.Color.CYAN);
		case 'traffic':
			return cssColor(cesium, '--v-tool', cesium.Color.CYAN);
		default:
			return cssColor(cesium, '--v-ink-3', cesium.Color.LIGHTGRAY);
	}
}

function createAgencyUnitsLayer(): GodsEyeLayerInstance {
	let ctx: GodsEyeLayerContext | null = null;
	let active = false;
	let timer: ReturnType<typeof setInterval> | null = null;
	const entityIds: string[] = [];
	let lastSignature = '';

	function draw() {
		if (!ctx || !active) return;
		const { cesium, viewer } = ctx;
		const units = godsEye.units;
		const sig = JSON.stringify([
			units.map((u) => [u.unitId, u.agency, u.lon, u.lat, u.emulated]),
			godsEye.unitsFeed.status,
		]);
		if (sig === lastSignature) return;
		lastSignature = sig;

		for (const id of entityIds) viewer.entities.removeById(id);
		entityIds.length = 0;

		for (const u of units) {
			const entity = viewer.entities.add({
				id: `${AGENCY_UNITS_PICK_PREFIX}${u.unitId}`,
				position: cesium.Cartesian3.fromDegrees(u.lon, u.lat, 12),
				point: {
					pixelSize: 9,
					color: agencyColor(u.agency, cesium),
					outlineColor: cssColor(cesium, '--v-canvas', cesium.Color.BLACK),
					outlineWidth: 2,
				},
				label: {
					text: `${u.label ?? u.unitId}${u.emulated ? ' ·EMULATED' : ''}`,
					font: '12px monospace',
					fillColor: agencyColor(u.agency, cesium),
					outlineColor: cssColor(cesium, '--v-canvas', cesium.Color.BLACK),
					outlineWidth: 2,
					style: 2, // FILL_AND_OUTLINE
					verticalOrigin: -1,
					pixelOffset: new cesium.Cartesian2(0, 12),
				},
				description: `unit ${u.unitId} · agency ${u.agency} · assigned ${
					u.assignedEventId ?? '—'
				} · ${u.emulated ? 'EMULATED (deterministic from dispatch)' : 'live'}`,
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
			void godsEye.pollUnits();
			draw();
		},
		handlePick(pickId) {
			return pickId.startsWith(AGENCY_UNITS_PICK_PREFIX);
		},
		coverageStats() {
			return {
				count: entityIds.length,
				loading: godsEye.unitsFeed.status === 'empty' && godsEye.unitsFeed.updatedAt === 0,
				hint:
					godsEye.unitsFeed.status === 'not-deployed'
						? '/api/v1/units 404 — signalling stream pending'
						: `${godsEye.units.length} units`,
				error: godsEye.unitsFeed.note,
			};
		},
		destroy() {
			this.deactivate();
		},
	};
}

export const agencyUnitsLayer: GodsEyeLayer = {
	id: 'units',
	name: 'Jan Rakshak + agency units',
	label: 'UNITS',
	requiresKeyId: null,
	pickPrefix: AGENCY_UNITS_PICK_PREFIX,
	defaultOn: true,
	create: createAgencyUnitsLayer,
};
