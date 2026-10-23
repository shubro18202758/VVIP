/**
 * dispatch layer — agency dispatches as unit → waypoint lines (dsp:).
 *
 * Feed: the city registry's dispatch entities (open dispatches of live
 * events, located at their waypoint junction — or at the event's spine
 * centroid when no waypoint was named, labelled so) and the unit entities
 * they link. In the agency's colour (agency.ts):
 *
 *   requested     a dashed outline at the waypoint — a named officer has
 *                 not approved it yet (108 requests land here first);
 *   approved /    an ARROW from the unit's projected position to the
 *   en_route      waypoint junction (the unit layer draws the unit itself);
 *   on_scene      a solid ring at the waypoint.
 *
 * Every unit on this surface is emulated (dispatch projection / C-A
 * patrols) and badged so; no person is ever named — agencies and unit ids
 * only. No fallback feed: without the city picture the chip says so.
 *
 * C6: the dispatch's event title rides raw in props.event_title (the
 * oracle masks labels and feed text, not props) — the pick description
 * prints it through displayText, so a pursuit's plate reads GJ27EM•••1
 * unless a named duty officer is on console, and re-renders when one is.
 *
 * DRAPED (layers/draping.ts): the waypoint ring and the unit → waypoint
 * arrow lie on the ground (terrain and the photoreal 3D tiles); the
 * waypoint marker and its label are clamped and never hidden by a building.
 * Ground ellipses have no outline, so the ring is a draped closed polyline
 * of its own (`dsp:ring-<id>`), and `dsp:wp-<id>` stays the marker a click —
 * or a test's locate() — lands on.
 *
 * Pick ids `dsp:line-<dispatchId>`, `dsp:wp-<dispatchId>`,
 * `dsp:ring-<dispatchId>` → URN `dispatch:<id>`.
 */

import { ops } from '$lib/ops/store.svelte';
import { cityRegistry } from '../entities/registry.svelte';
import { displayText } from '../plates';
import type { GodsEyeLayer, GodsEyeLayerContext, GodsEyeLayerInstance } from '../registry';
import { cssColor } from '../theme';
import { agencyColor, agencyLabel } from './agency';
import { DRAPE_Z, clampedDot, clampedLabel, drapeAudit, drapedPolyline, drapedRing, ground, groundPath } from './draping';

export const DISPATCH_PICK_PREFIX = 'dsp:';

function str(v: unknown): string | null {
	return typeof v === 'string' && v.length ? v : null;
}

function createDispatchLayer(): GodsEyeLayerInstance {
	let ctx: GodsEyeLayerContext | null = null;
	let active = false;
	let timer: ReturnType<typeof setInterval> | null = null;
	const entityIds: string[] = [];
	const urns = new Map<string, string>();
	let lastSignature = '';
	let counts = { requested: 0, moving: 0, onScene: 0, lines: 0 };

	function draw() {
		if (!ctx || !active) return;
		const { cesium, viewer } = ctx;
		const dispatches = cityRegistry.byKind('dispatch').filter((d) => d.lon != null && d.lat != null);
		const unitPos = (unitId: string | null) => {
			const u = unitId ? cityRegistry.get(`unit:${unitId}`) : null;
			return u && u.lon != null && u.lat != null ? ([u.lon, u.lat] as [number, number]) : null;
		};
		const officer = ops.officerOn;
		const sig = JSON.stringify([
			dispatches.map((d) => [d.id, d.state, d.lon, d.lat, unitPos(str(d.props.unit_id)), d.props.event_title ?? null]),
			officer,
		]);
		if (sig === lastSignature) return;
		lastSignature = sig;

		for (const id of entityIds) viewer.entities.removeById(id);
		entityIds.length = 0;
		urns.clear();
		counts = { requested: 0, moving: 0, onScene: 0, lines: 0 };
		const canvasInk = cssColor(cesium, '--v-canvas', cesium.Color.BLACK);

		for (const d of dispatches) {
			const agency = str(d.props.agency);
			const unitId = str(d.props.unit_id);
			const color = agencyColor(agency, cesium);
			const state = d.state ?? 'requested';
			const wp = d.props.waypoint_junction;
			const target = typeof wp === 'number' ? `J${wp}` : 'event spine';
			const urn = `dispatch:${d.id}`;
			const lineDesc = `${agencyLabel(agency)} ${unitId ?? '—'} → ${target} · ${state.replace(/_/g, ' ').toUpperCase()}${
				d.props.decided_by ? ` · approved by ${d.props.decided_by}` : ' · awaiting a named officer'
			} · for ${displayText(str(d.props.event_title), officer) || 'an event'} · unit EMULATED${
				// no waypoint named: the unit stands at its event's marker, a point ON the
				// route (oracle 'event_marker'; builds before it said 'event_spine_centroid')
				d.props.position_basis === 'event_marker' || d.props.position_basis === 'event_spine_centroid'
					? ' · no waypoint named (at the event marker on its route)'
					: ''
			}`;
			if (state === 'requested') counts.requested++;
			else if (state === 'on_scene') counts.onScene++;
			else counts.moving++;

			// Waypoint ring (every state): requested = dashed (no officer yet),
			// approved / en route = thin, on scene = the wider solid ring.
			const ring = viewer.entities.add({
				id: `${DISPATCH_PICK_PREFIX}ring-${d.id}`,
				polyline: drapedRing(cesium, {
					lon: d.lon as number,
					lat: d.lat as number,
					radiusM: state === 'on_scene' ? 70 : 55,
					width: state === 'on_scene' ? 3 : 2,
					material: state === 'requested' ? new cesium.PolylineDashMaterialProperty({ color, dashLength: 10 }) : color,
					z: DRAPE_Z.dispatch,
				}),
				description: lineDesc,
			});
			entityIds.push(ring.id);
			urns.set(ring.id, urn);

			// Waypoint marker (every state).
			const wpEntity = viewer.entities.add({
				id: `${DISPATCH_PICK_PREFIX}wp-${d.id}`,
				position: ground(cesium, d.lon as number, d.lat as number),
				billboard: clampedDot(cesium, {
					pixelSize: state === 'requested' ? 6 : 8,
					color: state === 'requested' ? canvasInk : color,
					outlineColor: color,
					outlineWidth: 2,
				}),
				label: clampedLabel(cesium, {
					text: `${agencyLabel(agency)} ${unitId ?? ''} → ${target} · ${state.replace(/_/g, ' ').toUpperCase()}`,
					font: '12px monospace',
					fillColor: color,
					outlineColor: canvasInk,
					outlineWidth: 2,
					style: 2,
					verticalOrigin: -1,
					pixelOffset: new cesium.Cartesian2(0, 10),
					distanceDisplayCondition: new cesium.DistanceDisplayCondition(0, 6500),
				}),
				description: lineDesc,
			});
			entityIds.push(wpEntity.id);
			urns.set(wpEntity.id, urn);

			// The unit → waypoint arrow while the unit is out.
			const from = unitPos(unitId);
			if (from && state !== 'requested' && state !== 'on_scene') {
				const line = viewer.entities.add({
					id: `${DISPATCH_PICK_PREFIX}line-${d.id}`,
					polyline: drapedPolyline(cesium, {
						positions: groundPath(cesium, [from, [d.lon as number, d.lat as number]]),
						width: 9,
						material: new cesium.PolylineArrowMaterialProperty(color),
						z: DRAPE_Z.dispatch,
					}),
					description: lineDesc,
				});
				entityIds.push(line.id);
				urns.set(line.id, urn);
				counts.lines++;
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
			timer = setInterval(draw, 2000);
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
			return pickId.startsWith(DISPATCH_PICK_PREFIX);
		},
		urnOf(pickId) {
			return urns.get(pickId) ?? null;
		},
		coverageStats() {
			const served = cityRegistry.served('dispatch');
			const down = cityRegistry.status !== 'idle' && !served;
			const n = counts.requested + counts.moving + counts.onScene;
			return {
				count: n,
				loading: cityRegistry.status === 'idle',
				hint: down
					? 'city fusion API unavailable — no dispatch lines'
					: `${n} open dispatches · ${counts.requested} awaiting approval · ${counts.moving} en route · ${counts.onScene} on scene (units EMULATED)`,
				error: down ? (cityRegistry.note ?? 'dispatch source not ok') : null,
				detail: ctx && entityIds.length ? { ...counts, ...drapeAudit(ctx.viewer, entityIds) } : { ...counts },
				via: 'city-api',
			};
		},
		destroy() {
			this.deactivate();
		},
	};
}

export const dispatchLayer: GodsEyeLayer = {
	id: 'dispatch',
	name: 'Agency dispatches (unit → waypoint)',
	label: 'DISPATCH',
	requiresKeyId: null,
	pickPrefix: DISPATCH_PICK_PREFIX,
	defaultOn: true,
	create: createDispatchLayer,
};
