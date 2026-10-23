/**
 * agency-units layer — Jan Rakshak / police / traffic unit markers.
 *
 * Feed: the canvas store's units — the city registry's unit entities, or
 * GET /api/v1/units → {units: [...]} as the fallback. Two producers share
 * them: approved dispatches projected along the corridor graph (status,
 * eta_sec, progress, arrived) and — contract C-A — emulated patrols (status
 * patrol | available | …). Agency shapes the colour (agency.ts: jan_rakshak
 * → act orange, police → data accent, traffic → tool accent, 108 → ink,
 * fire → act-2, other → dim).
 *
 * Every marker reads `label` over `STATUS · ETA · PROGRESS` — the real
 * lifecycle the oracle reports, '—' where it has none — and every emulated
 * unit carries a persistent EMULATED badge (the C6 rail). 404 → "not
 * deployed" chip.
 *
 * DRAPED (layers/draping.ts): marker and label are clamped to the terrain
 * — street level, under the photoreal 3D tiles too — and never hidden by a
 * building. The label reads in the agency's text ink on the translucent
 * canvas plate, and gives way (the canvas's declutter) to an e-challan /
 * sighting pin it would cover — hover or select the unit to read it there.
 *
 * Pick ids `unit:<unitId>` → URN `unit:<unitId>`.
 */

import { feedError, feedHint, godsEye } from '../store.svelte';
import type { AgencyUnitRecord } from '../data';
import type { GodsEyeLayer, GodsEyeLayerContext, GodsEyeLayerInstance } from '../registry';
import { cssColor } from '../theme';
import { agencyColor, agencyLabel, agencyTextColor, unitPhase } from './agency';
import { clampedDot, clampedLabel, drapeAudit, ground } from './draping';
import { labelPlate } from './labels';

export const AGENCY_UNITS_PICK_PREFIX = 'unit:';

/** '2m 05s' / '45s' / 'ARRIVED' / '—'. */
function etaText(u: AgencyUnitRecord): string {
	if (u.arrived === true) return 'ARRIVED';
	if (u.etaSec == null) return 'ETA —';
	const s = Math.max(0, Math.round(u.etaSec));
	if (s < 60) return `ETA ${s}s`;
	return `ETA ${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`;
}

/** The second label line: 'EN ROUTE · ETA 2m 05s · 45%'. Exported for tests. */
export function unitStatusLine(u: AgencyUnitRecord): string {
	const status = u.status ? u.status.replace(/_/g, ' ').toUpperCase() : 'STATUS —';
	const progress = u.progress != null ? `${Math.round(u.progress * 100)}%` : '—';
	return `${status} · ${etaText(u)} · ${progress}`;
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
			units.map((u) => [u.unitId, u.agency, u.lon, u.lat, u.emulated, unitStatusLine(u)]),
			godsEye.unitsFeed.status,
		]);
		if (sig === lastSignature) return;
		lastSignature = sig;

		for (const id of entityIds) viewer.entities.removeById(id);
		entityIds.length = 0;

		for (const u of units) {
			const line = unitStatusLine(u);
			const entity = viewer.entities.add({
				id: `${AGENCY_UNITS_PICK_PREFIX}${u.unitId}`,
				position: ground(cesium, u.lon, u.lat),
				billboard: clampedDot(cesium, {
					pixelSize: 9,
					color: agencyColor(u.agency, cesium),
					outlineColor: cssColor(cesium, '--v-canvas', cesium.Color.BLACK),
					outlineWidth: 2,
				}),
				label: clampedLabel(cesium, {
					text: `${u.label ?? u.unitId}${u.emulated ? ' ·EMULATED' : ''}\n${line}`,
					font: '12px monospace',
					// the agency's TEXT ink (act-text, not the darker act fill) on the
					// canvas plate: legible on the dark basemap and over the pins, flow
					// and trace lines it crosses
					fillColor: agencyTextColor(u.agency, cesium),
					outlineColor: cssColor(cesium, '--v-canvas', cesium.Color.BLACK),
					outlineWidth: 2,
					style: 2, // FILL_AND_OUTLINE
					...labelPlate(cesium),
					verticalOrigin: -1,
					pixelOffset: new cesium.Cartesian2(0, 12),
					// Overview declutter: the marker always shows, its status text up close.
					distanceDisplayCondition: new cesium.DistanceDisplayCondition(0, 7500),
				}),
				description: `unit ${u.unitId} · ${agencyLabel(u.agency)} · ${line} · assigned ${
					u.assignedEventId ?? '—'
				}${u.dispatchId ? ` · dispatch ${u.dispatchId}` : ''}${
					u.speedKmh != null ? ` · ${Math.round(u.speedKmh)} km/h` : ''
				} · ${u.emulated ? `EMULATED (${u.source ?? 'emulated'})` : `live (${u.source ?? '—'})`}`,
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
		urnOf(pickId) {
			// The pick id IS the URN (`unit:<unitId>`).
			return pickId.length > AGENCY_UNITS_PICK_PREFIX.length ? pickId : null;
		},
		coverageStats() {
			const feed = godsEye.unitsFeed;
			const units = godsEye.units;
			const unplaced = Math.max(0, feed.total - units.length);
			const onScene = units.filter((u) => unitPhase(u) === 'on_scene').length;
			const enRoute = units.filter((u) => unitPhase(u) === 'en_route').length;
			return {
				count: entityIds.length,
				loading: feed.status === 'empty' && feed.updatedAt === 0,
				// An empty answer is not a fault: 'no units fielded' is the hint, never the ⚠ chip.
				hint:
					feed.status === 'not-deployed'
						? '/api/v1/units 404 — signalling stream pending'
						: (feedHint(feed) ??
							`${units.length} units (EMULATED) · ${enRoute} en route · ${onScene} on scene${unplaced ? ` · ${unplaced} awaiting a position` : ''}`),
				error: feedError(feed),
				detail:
					ctx && entityIds.length
						? { units: units.length, enRoute, onScene, ...drapeAudit(ctx.viewer, entityIds) }
						: { units: units.length, enRoute, onScene },
				via: godsEye.via('unit'),
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
