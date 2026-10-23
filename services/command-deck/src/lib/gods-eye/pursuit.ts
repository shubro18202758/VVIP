/**
 * Pursuit overlay — the single cinematic shared by /ops (wall) and /gods-eye.
 *
 * Motion doctrine (design contract, one curve cubic-bezier(.2,0,0,1)):
 *   · the vehicle trace draws HEAD-FIRST over 1200 ms — the newest sighting
 *     appears first and history extends backwards from it;
 *   · a 300 ms comet rides the drawing tip while it runs;
 *   · the newest sighting pulses EXACTLY ONCE (one 200 ms ring, then static);
 *   · ranked choke points cascade at 120 ms stagger (rank 1 first);
 *   · the future arc to choke #1 is a slowly marching dashed line —
 *     'where it will be' always moves slower than 'where it was'.
 *
 * prefers-reduced-motion: the trace snaps complete, the comet is dropped,
 * the ring is static, chokes land together, the arc is a static dash — the
 * information is identical, only the cinema is withdrawn.
 *
 * Honesty (the plan's WP-03 fields, when the detail read lands):
 *   · a STALE sighting (older than the plan's stale_after_sec) is labelled
 *     STALE with its age, and its ring goes dim — the vehicle may be beyond
 *     every junction listed;
 *   · a choke the vehicle has LIKELY PASSED (graph time < sighting age) is
 *     drawn dim and labelled so, and the future arc aims at the first choke
 *     still ahead;
 *   · the direction of travel names its basis (heading / one-way /
 *     unknown) and flags a read AGAINST a one-way; the legal onward
 *     segments the plan starts from are drawn as a short dashed fan.
 *
 * C6: the sighting label masks the plate unless a named duty officer is on
 * console.
 *
 * DRAPED (layers/draping.ts): the trace, the onward fan, the future arc and
 * the sighting ring are ground polylines on terrain and the photoreal 3D
 * tiles, on top of every other draped layer (DRAPE_Z.pursuit); the ring's
 * veil is a ground fill; the sighting, the comet and the chokes are clamped
 * markers no building hides. (ColorMaterialProperty takes the COLOUR itself:
 * handed an options object it painted the trace and the veil opaque white.)
 * Animated geometry is dynamic only while it moves: the head-first trace and
 * the growing ring hand back constant positions once their draw completes
 * (a dynamic ground line is rebuilt every frame), and the arc marches
 * through a time-varying dash length inside its material (uniform updates —
 * the geometry never rebuilds).
 *
 * Colours come from the design tokens at runtime (theme.ts) with Cesium's
 * own named constants as fallbacks — never hex literals in this file.
 */

import type { InterceptionPlan } from '$lib/ops/types';
import type { InterceptionDetail, SegmentGeometry } from './data';
import { displayPlate } from './plates';
import { cssColor, cssVar, withAlpha } from './theme';
import { istStamp } from './hud/format';
import {
	DRAPE_Z,
	clampedDot,
	clampedLabel,
	drapeAudit,
	drapedEllipse,
	drapedPolyline,
	ground,
	groundPath,
	ringCoords,
	type DrapeAudit,
} from './layers/draping';

/** Motion timings (ms) — the contract's named durations. */
export const PURSUIT_MOTION = {
	traceDrawMs: 1200,
	cometMs: 300,
	ringMs: 200,
	chokeStaggerMs: 120,
	/** March cadence for the future arc's dashes (one cycle). */
	dashCycleMs: 900,
} as const;

/** The sighting ring's fill alpha: a veil under its outline, never a disc. */
export const PURSUIT_RING_FILL_ALPHA = 0.15;

/** The sighting ring's radius (m) once grown. */
export const PURSUIT_RING_RADIUS_M = 45;

export interface PursuitOverlayInput {
	/** The ranked plan; null clears the overlay. */
	plan: InterceptionPlan | null;
	/** Recent trace coordinates oldest→newest (from /vehicles/{id}/trace). */
	trace: Array<[number, number]>;
	/** The plan's honesty fields (stale, likely passed, direction), when read. */
	detail?: InterceptionDetail | null;
	/** Road geometry for the onward-segment fan. */
	segments?: Map<number, SegmentGeometry>;
	/** A named duty officer is on console (plates unmasked). */
	officer?: boolean;
}

export interface PursuitOverlay {
	show(input: PursuitOverlayInput): void;
	clear(): void;
	/** Entity ids currently drawn (smoke hook reads this). */
	count(): number;
	/** How the drawn entities meet the ground (draped / clamped / floating — the 3D smoke asserts floating 0). */
	audit(): DrapeAudit;
	destroy(): void;
}

const PURSUIT_PREFIX = 'pursuit:';

/** Compact age: 42s · 4m · 3h · 2d. */
function fmtAge(sec: number): string {
	const s = Math.max(0, Math.round(sec));
	if (s < 120) return `${s}s`;
	if (s < 7200) return `${Math.round(s / 60)}m`;
	if (s < 172_800) return `${Math.round(s / 3600)}h`;
	return `${Math.round(s / 86_400)}d`;
}

function reducedMotion(): boolean {
	if (typeof matchMedia !== 'function') return false;
	return matchMedia('(prefers-reduced-motion: reduce)').matches;
}

export function createPursuitOverlay(cesium: CesiumGlobal, viewer: CesiumViewer): PursuitOverlay {
	const entityIds: string[] = [];
	const timers: ReturnType<typeof setTimeout>[] = [];

	// Token-resolved colours (Cesium named constants as fallbacks only).
	const actColor = cssColor(cesium, '--v-act', cesium.Color.ORANGE);
	const dataColor = cssColor(cesium, '--v-data', cesium.Color.CYAN);
	/** Dim ink for points (a passed choke, a stale sighting) — never for text. */
	const inkColor = cssColor(cesium, '--v-ink-3', cesium.Color.LIGHTGRAY);
	/** Label inks: secondary text stays legible on a projector (ink-2, never ink-3). */
	const labelInk = cssColor(cesium, '--v-ink-1', cesium.Color.WHITE);
	const labelInk2 = cssColor(cesium, '--v-ink-2', cesium.Color.LIGHTGRAY);
	const outline = cssColor(cesium, '--v-canvas', cesium.Color.BLACK);
	/** A translucent canvas plate behind every overlay label (legible over any basemap). */
	const labelPlate = cesium.Color.fromCssColorString(withAlpha(cssVar('--v-canvas') ?? 'black', 0.72));
	const platePad = new cesium.Cartesian2(5, 3);

	function add(entity: Record<string, unknown>): string {
		const created = viewer.entities.add(entity);
		entityIds.push(created.id);
		return created.id;
	}

	function clear() {
		for (const t of timers) clearTimeout(t);
		timers.length = 0;
		for (const id of entityIds) viewer.entities.removeById(id);
		entityIds.length = 0;
	}

	function show({ plan, trace, detail = null, segments, officer = false }: PursuitOverlayInput) {
		clear();
		if (!plan?.latestSighting) return;
		const reduced = reducedMotion();
		const [sightLon, sightLat] = plan.latestSighting.position;
		// The detail must describe THIS plan's sighting to be trusted here.
		const facts =
			detail && detail.eventId === plan.eventId && (!detail.sightingId || detail.sightingId === plan.latestSighting.sightingId)
				? detail
				: null;
		const stale = facts?.stale === true;
		const age = facts?.sightingAgeSec ?? null;
		const passed = new Set((facts?.chokes ?? []).filter((c) => c.likelyPassed).map((c) => c.junctionId));
		const names = new Map((facts?.chokes ?? []).map((c) => [c.junctionId, c.name]));
		const plate = displayPlate(plan.latestSighting.plate, officer) ?? 'VEHICLE';
		const ageText = age != null ? fmtAge(age) : null;

		// ── 1. The newest sighting: identity point + one 200 ms ring ─────────
		add({
			id: `${PURSUIT_PREFIX}sight`,
			position: ground(cesium, sightLon, sightLat),
			billboard: clampedDot(cesium, {
				pixelSize: 10,
				color: stale ? inkColor : dataColor,
				outlineColor: outline,
				outlineWidth: 2,
			}),
			label: clampedLabel(cesium, {
				text: `${plate}${plan.latestSighting.source === 'emulated_anpr' ? ' ·EMULATED' : ''}${
					stale ? ` · STALE${ageText ? ` ${ageText}` : ''}` : ageText ? ` · ${ageText} ago` : ''
				}${facts?.direction?.againstOneway ? ' · AGAINST ONE-WAY' : ''}`,
				font: '13px monospace',
				fillColor: stale ? labelInk2 : labelInk,
				outlineColor: outline,
				outlineWidth: 2,
				style: 2,
				showBackground: true,
				backgroundColor: labelPlate,
				backgroundPadding: platePad,
				// BOTTOM-anchored ABOVE the point: the plate never covers the
				// sighting point or a primitive under it (a plate is pickable)
				verticalOrigin: 1,
				pixelOffset: new cesium.Cartesian2(0, -12),
			}),
			description: `latest sighting ${plan.latestSighting.t ? `${istStamp(Date.parse(plan.latestSighting.t))} IST` : '—'}${ageText ? ` (${ageText} ago)` : ''}${
				stale ? ' · STALE — the vehicle may be beyond every junction listed' : ''
			} · camera ${facts?.cameraId ?? '—'} · speed ${plan.latestSighting.speedKmh ?? '—'} km/h · direction ${
				facts?.direction?.basis ?? '—'
			}${facts?.direction?.againstOneway ? ' (observed AGAINST a one-way)' : ''} · source ${plan.latestSighting.source ?? '—'}`,
		});

		// ── 1b. The legal onward segments the plan starts from (dashed fan) ──
		for (const segId of facts?.direction?.onwardSegmentIds ?? []) {
			const seg = segments?.get(segId);
			if (!seg || seg.coordinates.length < 2) continue;
			add({
				id: `${PURSUIT_PREFIX}onward-${segId}`,
				polyline: drapedPolyline(cesium, {
					positions: groundPath(cesium, seg.coordinates),
					width: 3.5,
					material: new cesium.PolylineDashMaterialProperty({ color: stale ? inkColor : actColor, dashLength: 10 }),
					z: DRAPE_Z.pursuit,
				}),
				description: `onward movement · segment ${segId}${seg.roadName ? ` (${seg.roadName})` : ''} · direction basis ${
					facts?.direction?.basis ?? '—'
				}${facts?.direction?.towardsJunctionIds.length ? ` · towards ${facts.direction.towardsJunctionIds.map((j) => names.get(j) ?? `J${j}`).join(', ')}` : ''}`,
			});
		}

		// Ring: grows 0 → 45 m in ringMs (animated) or lands complete (reduced).
		// A RING, never a disc: a draped rim carries it (ground ellipses have
		// no outline) over a 15 % veil filled on the ground, so the vehicle
		// mark and the selection halo inside stay visible (an opaque 45 m fill
		// covered both in the judge view). A STALE sighting's ring goes dim
		// with it. The rim is a dynamic ground line only while it grows; it
		// lands as constant positions.
		const ringT0 = performance.now();
		const ringRadius = PURSUIT_RING_RADIUS_M;
		const rimAt = (r: number) => groundPath(cesium, ringCoords(sightLon, sightLat, r));
		const ringInk = stale ? inkColor : dataColor;
		const ringVeil =
			(ringInk as { withAlpha?: (a: number) => unknown }).withAlpha?.(PURSUIT_RING_FILL_ALPHA) ??
			cesium.Color.TRANSPARENT;
		// no veil colour on this build: the rim alone
		if (ringVeil != null) {
			add({
				id: `${PURSUIT_PREFIX}veil`,
				position: ground(cesium, sightLon, sightLat),
				ellipse: drapedEllipse(cesium, {
					radiusM: ringRadius,
					material: new cesium.ColorMaterialProperty(ringVeil),
					z: DRAPE_Z.pursuit,
				}),
			});
		}
		const ringId = add({
			id: `${PURSUIT_PREFIX}ring`,
			polyline: drapedPolyline(cesium, {
				positions: reduced
					? rimAt(ringRadius)
					: new cesium.CallbackProperty(
							() => rimAt(Math.max(2, Math.min(1, (performance.now() - ringT0) / PURSUIT_MOTION.ringMs) * ringRadius)),
							false,
						),
				width: 2.5,
				material: ringInk,
				z: DRAPE_Z.pursuit,
			}),
		});
		if (!reduced) timers.push(setTimeout(() => settle(ringId, rimAt(ringRadius)), PURSUIT_MOTION.ringMs + 50));

		// ── 2. The trace, drawn head-first over 1.2 s ─────────────────────────
		// Coordinates arrive oldest→newest; the draw reveals newest→oldest so
		// the head (the vehicle's NOW) lands first.
		if (trace.length >= 2) {
			const coords: Array<[number, number]> = [...trace];
			if (
				coords[coords.length - 1][0] !== sightLon ||
				coords[coords.length - 1][1] !== sightLat
			) {
				coords.push([sightLon, sightLat]);
			}
			const n = coords.length;
			let drawT0 = reduced ? -1 : performance.now();
			const drawStart = drawT0;

			const revealed = (): unknown[] => {
				if (drawT0 < 0) {
					// reduced motion (or draw finished): full trace, oldest→newest
					const flat: number[] = [];
					for (const [lon, lat] of coords) flat.push(lon, lat);
					return cesium.Cartesian3.fromDegreesArray(flat);
				}
				const p = Math.min(1, (performance.now() - drawT0) / PURSUIT_MOTION.traceDrawMs);
				const keep = Math.max(2, Math.ceil(p * n));
				const slice = coords.slice(n - keep);
				const flat: number[] = [];
				for (const [lon, lat] of slice) flat.push(lon, lat);
				return cesium.Cartesian3.fromDegreesArray(flat);
			};

			const traceId = add({
				id: `${PURSUIT_PREFIX}trace`,
				polyline: drapedPolyline(cesium, {
					positions: reduced ? revealed() : new cesium.CallbackProperty(revealed, false),
					width: 4,
					material: new cesium.ColorMaterialProperty(actColor),
					z: DRAPE_Z.pursuit,
				}),
				description: `pursuit trace · ${n} points · head-first draw ${reduced ? 'static (reduced motion)' : `${PURSUIT_MOTION.traceDrawMs} ms`}`,
			});
			// The draw done, the trace stands still: constant positions (a
			// dynamic ground line would be rebuilt every frame from here on).
			if (!reduced) {
				timers.push(
					setTimeout(() => {
						drawT0 = -1;
						settle(traceId, revealed());
					}, PURSUIT_MOTION.traceDrawMs + 50),
				);
			}

			// The comet rides the drawing tip for cometMs, then vanishes.
			if (!reduced) {
				const tip = () => {
					const p = Math.min(1, (performance.now() - drawStart) / PURSUIT_MOTION.traceDrawMs);
					const keep = Math.max(2, Math.ceil(p * n));
					return coords[n - keep];
				};
				const cometId = add({
					id: `${PURSUIT_PREFIX}comet`,
					position: new cesium.CallbackProperty(
						() => {
							const [lon, lat] = tip();
							return ground(cesium, lon, lat);
						},
						false,
					),
					billboard: clampedDot(cesium, {
						pixelSize: 7,
						color: cssColor(cesium, '--v-act-2', cesium.Color.ORANGE),
						outlineColor: outline,
						outlineWidth: 1,
					}),
				});
				timers.push(
					setTimeout(() => {
						viewer.entities.removeById(cometId);
						const i = entityIds.indexOf(cometId);
						if (i >= 0) entityIds.splice(i, 1);
					}, PURSUIT_MOTION.cometMs + PURSUIT_MOTION.traceDrawMs),
				);
			}
		}

		// ── 3. Ranked chokes, 120 ms cascade ─────────────────────────────────
		// The lead is the best-ranked choke the vehicle has NOT likely passed.
		const chokes = plan.chokes.slice(0, 5);
		const lead = chokes.find((c) => !passed.has(c.junctionId)) ?? null;
		chokes.forEach((choke, i) => {
			const draw = () => {
				const isLead = lead?.junctionId === choke.junctionId;
				const gone = passed.has(choke.junctionId);
				const name = names.get(choke.junctionId);
				add({
					id: `${PURSUIT_PREFIX}choke-${choke.junctionId}`,
					position: ground(cesium, choke.position[0], choke.position[1]),
					billboard: clampedDot(cesium, {
						pixelSize: isLead ? 11 : 7,
						color: isLead ? actColor : inkColor,
						outlineColor: outline,
						outlineWidth: isLead ? 2 : 1,
					}),
					label: clampedLabel(cesium, {
						text: `CHOKE ${choke.rank}${name ? ` · ${name}` : ` · ${choke.junctionType}`}${
							choke.graphCostSec != null ? ` · ${Math.round(choke.graphCostSec)}s` : ''
						}${gone ? ' · LIKELY PASSED' : ''}`,
						font: isLead ? '13px monospace' : '12px monospace',
						fillColor: isLead ? actColor : gone ? labelInk2 : labelInk,
						outlineColor: outline,
						outlineWidth: 2,
						style: 2,
						showBackground: true,
						backgroundColor: labelPlate,
						backgroundPadding: platePad,
						verticalOrigin: -1,
						pixelOffset: new cesium.Cartesian2(0, 10),
						// The lead choke reads at any range; the rest (and passed ones) only
						// closer than the wall's overview (~5.5 km), so they never stack.
						distanceDisplayCondition: new cesium.DistanceDisplayCondition(0, isLead ? 16_000 : 4500),
					}),
					description: `${name ? `${name} · ` : ''}junction ${choke.junctionId} · ${choke.junctionType} · graph cost ${
						choke.graphCostSec ?? '—'
					} s · score ${choke.score ?? '—'}${choke.signalControl ? ' · signalized' : ''}${
						gone ? ' · LIKELY PASSED (graph time shorter than the sighting age)' : ''
					}`,
				});
			};
			if (reduced || i === 0) draw();
			else timers.push(setTimeout(draw, i * PURSUIT_MOTION.chokeStaggerMs));
		});

		// ── 4. Future arc: marching dashes toward the lead choke ─────────────
		// Slow march: the dash length oscillates over one dashCycle — subtle,
		// never a blink (alpha never drops to zero). The oscillation is a
		// time-varying property INSIDE the material: the draped line's
		// geometry is built once and only the material uniform moves (setting
		// the length from a frame loop re-built the line every frame).
		const choke1 = stale ? null : lead;
		if (choke1) {
			const marchStart = performance.now();
			const marchLength = () =>
				18 + 10 * (((performance.now() - marchStart) % PURSUIT_MOTION.dashCycleMs) / PURSUIT_MOTION.dashCycleMs);
			add({
				id: `${PURSUIT_PREFIX}future`,
				polyline: drapedPolyline(cesium, {
					positions: groundPath(cesium, [
						[sightLon, sightLat],
						choke1.position,
					]),
					width: 3,
					material: new cesium.PolylineDashMaterialProperty({
						color: actColor,
						dashLength: reduced ? 18 : new cesium.CallbackProperty(marchLength, false),
					}),
					z: DRAPE_Z.pursuit,
				}),
				description: `future arc to choke ${choke1.rank}${
					names.get(choke1.junctionId) ? ` (${names.get(choke1.junctionId)})` : ''
				} — 'where it will be' always moves slower than 'where it was'`,
			});
		}
	}

	/** Swap an animated draped line onto constant positions (its draw is done). */
	function settle(id: string, positions: unknown[]) {
		const entity = viewer.entities.getById(id) as { polyline?: { positions?: unknown } } | undefined;
		if (entity?.polyline) entity.polyline.positions = positions;
	}

	return {
		show,
		clear,
		count: () => entityIds.length,
		audit: () => drapeAudit(viewer, entityIds),
		destroy() {
			clear();
		},
	};
}
