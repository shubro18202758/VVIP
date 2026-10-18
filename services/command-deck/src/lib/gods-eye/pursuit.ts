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
 * Colours come from the design tokens at runtime (theme.ts) with Cesium's
 * own named constants as fallbacks — never hex literals in this file.
 */

import type { InterceptionPlan } from '$lib/ops/types';
import { cssColor } from './theme';

/** Motion timings (ms) — the contract's named durations. */
export const PURSUIT_MOTION = {
	traceDrawMs: 1200,
	cometMs: 300,
	ringMs: 200,
	chokeStaggerMs: 120,
	/** March cadence for the future arc's dashes (one cycle). */
	dashCycleMs: 900,
} as const;

export interface PursuitOverlayInput {
	/** The ranked plan; null clears the overlay. */
	plan: InterceptionPlan | null;
	/** Recent trace coordinates oldest→newest (from /vehicles/{id}/trace). */
	trace: Array<[number, number]>;
}

export interface PursuitOverlay {
	show(input: PursuitOverlayInput): void;
	clear(): void;
	/** Entity ids currently drawn (smoke hook reads this). */
	count(): number;
	destroy(): void;
}

const PURSUIT_PREFIX = 'pursuit:';

function reducedMotion(): boolean {
	if (typeof matchMedia !== 'function') return false;
	return matchMedia('(prefers-reduced-motion: reduce)').matches;
}

export function createPursuitOverlay(cesium: CesiumGlobal, viewer: CesiumViewer): PursuitOverlay {
	const entityIds: string[] = [];
	const timers: ReturnType<typeof setTimeout>[] = [];
	let raf: number | null = null;
	let marching = false;

	// Token-resolved colours (Cesium named constants as fallbacks only).
	const actColor = cssColor(cesium, '--v-act', cesium.Color.ORANGE);
	const dataColor = cssColor(cesium, '--v-data', cesium.Color.CYAN);
	const inkColor = cssColor(cesium, '--v-ink-3', cesium.Color.LIGHTGRAY);
	const outline = cssColor(cesium, '--v-canvas', cesium.Color.BLACK);

	function add(entity: Record<string, unknown>): string {
		const created = viewer.entities.add(entity);
		entityIds.push(created.id);
		return created.id;
	}

	function stopLoop() {
		marching = false;
		if (raf != null && typeof cancelAnimationFrame === 'function') cancelAnimationFrame(raf);
		raf = null;
	}

	function clear() {
		stopLoop();
		for (const t of timers) clearTimeout(t);
		timers.length = 0;
		for (const id of entityIds) viewer.entities.removeById(id);
		entityIds.length = 0;
	}

	function show({ plan, trace }: PursuitOverlayInput) {
		clear();
		if (!plan?.latestSighting) return;
		const reduced = reducedMotion();
		const [sightLon, sightLat] = plan.latestSighting.position;

		// ── 1. The newest sighting: identity point + one 200 ms ring ─────────
		add({
			id: `${PURSUIT_PREFIX}sight`,
			position: cesium.Cartesian3.fromDegrees(sightLon, sightLat, 12),
			point: {
				pixelSize: 10,
				color: dataColor,
				outlineColor: outline,
				outlineWidth: 2,
			},
			label: {
				text: `${plan.latestSighting.plate ?? 'VEHICLE'}${plan.latestSighting.source === 'emulated_anpr' ? ' ·EMULATED' : ''}`,
				font: '12px monospace',
				fillColor: cssColor(cesium, '--v-ink-1', cesium.Color.WHITE),
				outlineColor: outline,
				outlineWidth: 2,
				style: 2,
				verticalOrigin: -1,
				pixelOffset: new cesium.Cartesian2(0, -14),
			},
			description: `latest sighting ${plan.latestSighting.t ?? '—'} · speed ${plan.latestSighting.speedKmh ?? '—'} km/h · source ${plan.latestSighting.source ?? '—'}`,
		});

		// Ring: grows 0 → ~45 m in ringMs (animated) or lands complete (reduced).
		let ringT0 = reduced ? -1 : performance.now();
		const ringRadius = 45;
		const ringRadii = () => {
			if (ringT0 < 0) return ringRadius;
			const p = Math.min(1, (performance.now() - ringT0) / PURSUIT_MOTION.ringMs);
			return Math.max(2, p * ringRadius);
		};
		add({
			id: `${PURSUIT_PREFIX}ring`,
			position: cesium.Cartesian3.fromDegrees(sightLon, sightLat, 12),
			ellipse: {
				semiMinorAxis: reduced ? ringRadius : new cesium.CallbackProperty(ringRadii, false),
				semiMajorAxis: reduced ? ringRadius : new cesium.CallbackProperty(ringRadii, false),
				material: new cesium.ColorMaterialProperty({ color: dataColor }),
				outline: true,
				outlineColor: dataColor,
				height: 12,
			},
		});

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

			add({
				id: `${PURSUIT_PREFIX}trace`,
				polyline: {
					positions: reduced ? revealed() : new cesium.CallbackProperty(revealed, false),
					width: 3,
					material: new cesium.ColorMaterialProperty({ color: actColor }),
				},
				description: `pursuit trace · ${n} points · head-first draw ${reduced ? 'static (reduced motion)' : `${PURSUIT_MOTION.traceDrawMs} ms`}`,
			});

			// The comet rides the drawing tip for cometMs, then vanishes.
			if (!reduced) {
				const tip = () => {
					const p = Math.min(1, (performance.now() - drawT0) / PURSUIT_MOTION.traceDrawMs);
					const keep = Math.max(2, Math.ceil(p * n));
					return coords[n - keep];
				};
				const cometId = add({
					id: `${PURSUIT_PREFIX}comet`,
					position: new cesium.CallbackProperty(
						() => {
							const [lon, lat] = tip();
							return cesium.Cartesian3.fromDegrees(lon, lat, 14);
						},
						false,
					),
					point: {
						pixelSize: 7,
						color: cssColor(cesium, '--v-act-2', cesium.Color.ORANGE),
						outlineColor: outline,
						outlineWidth: 1,
					},
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
		const chokes = plan.chokes.slice(0, 5);
		chokes.forEach((choke, i) => {
			const draw = () => {
				const rank1 = choke.rank === 1;
				add({
					id: `${PURSUIT_PREFIX}choke-${choke.junctionId}`,
					position: cesium.Cartesian3.fromDegrees(choke.position[0], choke.position[1], 10),
					point: {
						pixelSize: rank1 ? 11 : 7,
						color: rank1 ? actColor : inkColor,
						outlineColor: outline,
						outlineWidth: rank1 ? 2 : 1,
					},
					label: {
						text: `CHOKE ${choke.rank} · ${choke.junctionType}${choke.graphCostSec != null ? ` · ${Math.round(choke.graphCostSec)}s` : ''}`,
						font: '11px monospace',
						fillColor: rank1 ? actColor : inkColor,
						outlineColor: outline,
						outlineWidth: 2,
						style: 2,
						verticalOrigin: -1,
						pixelOffset: new cesium.Cartesian2(0, 10),
					},
					description: `junction ${choke.junctionId} · ${choke.junctionType} · graph cost ${choke.graphCostSec ?? '—'} s · score ${choke.score ?? '—'}${choke.signalControl ? ' · signalized' : ''}`,
				});
			};
			if (reduced || i === 0) draw();
			else timers.push(setTimeout(draw, i * PURSUIT_MOTION.chokeStaggerMs));
		});

		// ── 4. Future arc: marching dashes toward choke #1 ───────────────────
		const choke1 = chokes.find((c) => c.rank === 1);
		if (choke1) {
			const arcMaterial = new cesium.PolylineDashMaterialProperty({
				color: actColor,
				dashLength: 18,
			});
			add({
				id: `${PURSUIT_PREFIX}future`,
				polyline: {
					positions: cesium.Cartesian3.fromDegreesArray([
						sightLon,
						sightLat,
						choke1.position[0],
						choke1.position[1],
					]),
					width: 2,
					material: arcMaterial,
				},
				description:
					"future arc to choke #1 — 'where it will be' always moves slower than 'where it was'",
			});
			if (!reduced) {
				// Slow march: oscillate dashLength over one dashCycle — subtle,
				// never a blink (alpha never drops to zero).
				marching = true;
				const marchStart = performance.now();
				const step = () => {
					if (!marching) return;
					const mat = arcMaterial as { dashLength?: number };
					const phase = ((performance.now() - marchStart) % PURSUIT_MOTION.dashCycleMs) / PURSUIT_MOTION.dashCycleMs;
					mat.dashLength = 18 + 10 * phase;
					raf = requestAnimationFrame(step);
				};
				raf = requestAnimationFrame(step);
			}
		}
	}

	return {
		show,
		clear,
		count: () => entityIds.length,
		destroy() {
			clear();
		},
	};
}
