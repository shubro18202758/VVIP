/**
 * convoy layer — the escorted movement in motion (cvy:).
 *
 * Feed: the city registry's convoy entities — the escort GPS mirror
 * (contract C-B, Valkey convoy:position:{movement_id}, EX 30, written by the
 * brain's escort loop, contract C-J / WP-12). The marker sits exactly where
 * the last fix put it and moves when the next snapshot moves it: positions
 * are never interpolated or extrapolated client-side, and a frame older
 * than the 30 s key TTL simply disappears from the snapshot.
 *
 * Badge: 'EMULATED GPS' when the producer says emulated_gps (the brain's
 * route-walking GPS simulator), 'GPS' for a real device. The heading tick
 * points along the reported heading; speed and progress are the producer's.
 *
 * Pending escort approvals (C-J `pending_approvals[].detour_segment_ids`,
 * the escort followed by the corridor module): the detour an officer is
 * being asked to approve is drawn as a DASHED preview — a proposal, never
 * the route the convoy takes until a named officer approves it.
 *
 * DRAPED (layers/draping.ts): the 90 m ring, the heading tick and the
 * detour previews lie on the ground (terrain and the photoreal 3D tiles) —
 * the detours above the corridor spines; the marker and its label are
 * clamped and never hidden by a building. The ring is a draped closed
 * polyline of its own (`cvy:ring-<id>`: ground ellipses have no outline).
 *
 * Pick ids `cvy:<movementId>`, `cvy:hdg-<movementId>`, `cvy:ring-<movementId>`
 * → URN `convoy:<id>`; `cvy:detour-<movementId>-<approvalId>-<segmentId>` →
 * the convoy too.
 */

import { cityRegistry } from '../entities/registry.svelte';
import { escortFollower } from '$lib/ops/components/template/escort.svelte';
import { godsEye } from '../store.svelte';
import type { CityEntity } from '../city-api';
import type { GodsEyeLayer, GodsEyeLayerContext, GodsEyeLayerInstance } from '../registry';
import { cssColor, cssVar, withAlpha } from '../theme';
import { DRAPE_Z, clampedDot, clampedLabel, drapeAudit, drapedPolyline, drapedRing, ground, groundPath } from './draping';

export const CONVOY_PICK_PREFIX = 'cvy:';

/** Length (m) of the heading tick. */
const HEADING_TICK_M = 220;

function num(v: unknown): number | null {
	return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

/** Display line: 'CONVOY 3f2a… · 42 km/h · 37% · EMULATED GPS'. */
export function convoyLine(c: CityEntity): string {
	const speed = num(c.props.speed_kmh);
	const progress = num(c.props.progress);
	const emulated = c.source === 'emulated';
	return `${c.label.toUpperCase()}${speed != null ? ` · ${Math.round(speed)} km/h` : ''}${
		progress != null ? ` · ${Math.round(progress * 100)}%` : ''
	} · ${emulated ? 'EMULATED GPS' : 'GPS'}`;
}

function createConvoyLayer(): GodsEyeLayerInstance {
	let ctx: GodsEyeLayerContext | null = null;
	let active = false;
	let timer: ReturnType<typeof setInterval> | null = null;
	const entityIds: string[] = [];
	const urns = new Map<string, string>();
	let lastSignature = '';

	function draw() {
		if (!ctx || !active) return;
		const { cesium, viewer } = ctx;
		const convoys = cityRegistry.byKind('convoy').filter((c) => c.lon != null && c.lat != null);
		// The followed escort's approvals awaiting an officer: their detours preview dashed.
		const followedId = escortFollower.movementId;
		const approvals = followedId ? (escortFollower.status?.pendingApprovals ?? []).filter((a) => a.detourSegmentIds.length) : [];
		const sig = JSON.stringify([
			convoys.map((c) => [c.id, c.lon, c.lat, c.props.speed_kmh, c.props.heading, c.props.progress, c.source, c.t]),
			approvals.map((a) => [a.approvalId, a.detourSegmentIds.join('.')]),
			godsEye.segments.length,
		]);
		if (sig === lastSignature) return;
		lastSignature = sig;

		for (const id of entityIds) viewer.entities.removeById(id);
		entityIds.length = 0;
		urns.clear();
		const act = cssColor(cesium, '--v-act', cesium.Color.ORANGE);
		const ink = cssColor(cesium, '--v-ink-1', cesium.Color.WHITE);
		const canvasInk = cssColor(cesium, '--v-canvas', cesium.Color.BLACK);

		for (const c of convoys) {
			const lon = c.lon as number;
			const lat = c.lat as number;
			const urn = `convoy:${c.id}`;
			const ageSec = c.t != null ? Math.max(0, Math.round((Date.now() - c.t) / 1000)) : null;
			const description = `${convoyLine(c)} · movement ${c.id}${
				typeof c.props.event_id === 'string' ? ` · event ${c.props.event_id}` : ''
			}${num(c.props.segment_id) != null ? ` · segment ${c.props.segment_id}` : ''} · fix ${
				ageSec != null ? `${ageSec}s ago` : 'time —'
			} · source ${c.props.position_source ?? c.source}`;
			const ring = viewer.entities.add({
				id: `${CONVOY_PICK_PREFIX}ring-${c.id}`,
				polyline: drapedRing(cesium, { lon, lat, radiusM: 90, width: 2.5, material: act, z: DRAPE_Z.convoy }),
				description,
			});
			entityIds.push(ring.id);
			urns.set(ring.id, urn);
			const marker = viewer.entities.add({
				id: `${CONVOY_PICK_PREFIX}${c.id}`,
				position: ground(cesium, lon, lat),
				billboard: clampedDot(cesium, {
					pixelSize: 16,
					color: act,
					outlineColor: ink,
					outlineWidth: 3,
				}),
				label: clampedLabel(cesium, {
					text: convoyLine(c),
					font: '600 13px monospace',
					fillColor: ink,
					outlineColor: canvasInk,
					outlineWidth: 3,
					style: 2,
					verticalOrigin: 1,
					pixelOffset: new cesium.Cartesian2(0, -16),
				}),
				description,
			});
			entityIds.push(marker.id);
			urns.set(marker.id, urn);

			const heading = num(c.props.heading);
			if (heading != null) {
				const rad = (heading * Math.PI) / 180;
				const dLat = (HEADING_TICK_M * Math.cos(rad)) / 111_320;
				const dLon = (HEADING_TICK_M * Math.sin(rad)) / (111_320 * Math.cos((lat * Math.PI) / 180));
				const tick = viewer.entities.add({
					id: `${CONVOY_PICK_PREFIX}hdg-${c.id}`,
					polyline: drapedPolyline(cesium, {
						positions: groundPath(cesium, [
							[lon, lat],
							[lon + dLon, lat + dLat],
						]),
						width: 10,
						material: new cesium.PolylineArrowMaterialProperty(act),
						z: DRAPE_Z.convoy,
					}),
					description,
				});
				entityIds.push(tick.id);
				urns.set(tick.id, urn);
			}
		}

		// Detour previews: dashed, translucent act colour — a PROPOSAL awaiting
		// a named officer's approval (the AI never applies it on its own).
		if (followedId && approvals.length) {
			const urn = `convoy:${followedId}`;
			const dash = cesium.Color.fromCssColorString(withAlpha(cssVar('--v-act-text') ?? 'orange', 0.85));
			for (const a of approvals) {
				for (const segId of a.detourSegmentIds) {
					const seg = godsEye.segmentById.get(segId);
					if (!seg || seg.coordinates.length < 2) continue;
					const preview = viewer.entities.add({
						id: `${CONVOY_PICK_PREFIX}detour-${followedId}-${a.approvalId}-${segId}`,
						polyline: drapedPolyline(cesium, {
							positions: groundPath(cesium, seg.coordinates),
							width: 5,
							material: new cesium.PolylineDashMaterialProperty({ color: dash, dashLength: 16 }),
							z: DRAPE_Z.detour,
						}),
						description: `PROPOSED detour (${a.action.replace(/_/g, ' ')}) · awaiting a duty officer's approval · segment ${segId}${
							seg.roadName ? ` (${seg.roadName})` : ''
						}${a.detourM != null ? ` · detour ${(a.detourM / 1000).toFixed(1)} km` : ''}${a.simulated ? ' · SIMULATED FEED' : ''}`,
					});
					entityIds.push(preview.id);
					urns.set(preview.id, urn);
				}
			}
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
			timer = setInterval(draw, 1000);
		},
		deactivate() {
			active = false;
			if (timer) clearInterval(timer);
			timer = null;
			if (ctx) for (const id of entityIds) ctx.viewer.entities.removeById(id);
			entityIds.length = 0;
			urns.clear();
			lastSignature = '';
		},
		refresh() {
			lastSignature = '';
			void cityRegistry.refresh();
			draw();
		},
		handlePick(pickId) {
			return pickId.startsWith(CONVOY_PICK_PREFIX);
		},
		urnOf(pickId) {
			return urns.get(pickId) ?? null;
		},
		coverageStats() {
			const served = cityRegistry.served('convoy');
			const down = cityRegistry.status !== 'idle' && !served;
			const n = cityRegistry.byKind('convoy').length;
			return {
				count: n,
				loading: cityRegistry.status === 'idle',
				hint: down
					? 'city fusion API unavailable — convoy GPS not shown'
					: n
						? `${n} escort${n === 1 ? '' : 's'} on GPS (C-B mirror, 30 s TTL — no interpolation)`
						: 'no escort reporting GPS (start one from the VVIP corridor module)',
				error: down ? (cityRegistry.note ?? 'convoy source not ok') : null,
				detail: ctx && entityIds.length ? { convoys: n, ...drapeAudit(ctx.viewer, entityIds) } : { convoys: n },
				via: 'city-api',
			};
		},
		destroy() {
			this.deactivate();
		},
	};
}

export const convoyLayer: GodsEyeLayer = {
	id: 'convoy',
	name: 'Escorted convoy (GPS)',
	label: 'CONVOY',
	requiresKeyId: null,
	pickPrefix: CONVOY_PICK_PREFIX,
	defaultOn: true,
	create: createConvoyLayer,
};
