/**
 * Replay overlay — an emulator cause tape drawn on the viewer CLOCK.
 *
 * The tape's frames become time-dynamic entities: every plate read by an
 * ANPR camera is a SampledPositionProperty track (samples at its reads,
 * linear between them, held after the last read and hidden before the
 * first — the vehicle simply has not been seen yet), every agency unit
 * likewise. The canvas sets `viewer.clock.currentTime` from the replay
 * scrubber; Cesium evaluates every track at that instant, so scrubbing
 * moves the vehicle marker across the EM-CAM hops exactly as the tape
 * recorded them. Camera sites seen on the tape are drawn as fixed rings.
 * Everything stands ON the ground (layers/draping.ts): the rings are draped
 * on terrain and the photoreal 3D tiles, the tracks sample ground positions
 * and their dots / labels clamp with the depth test off — a replay on the
 * 3D city never runs inside its buildings.
 *
 * Honesty: a tape is a recording of EMULATED causes — every label carries
 * EMULATED, plates stay masked unless a named duty officer is on console,
 * and nothing here delivers anything to the stack.
 *
 * Pick ids `rpl:veh-<n>`, `rpl:unit-<n>`, `rpl:cam-<cameraId>`.
 */

import type { ReplayTape } from './hud/replay.svelte';
import { displayPlate } from './plates';
import { cssColor } from './theme';
import { agencyColor } from './layers/agency';
import { DRAPE_Z, clampedDot, clampedLabel, drapedRing, ground } from './layers/draping';

export const REPLAY_PICK_PREFIX = 'rpl:';

export interface ReplayMarker {
	/** The plate as shown (masked unless a duty officer is named). */
	label: string;
	lon: number;
	lat: number;
	/** The camera of the newest read at or before the clock. */
	camera: string | null;
	hit: string | null;
}

export interface ReplayOverlay {
	/** Draw `tape` (replaces any previous one); `officer` unmasks plates. */
	show(tape: ReplayTape, officer: boolean): void;
	/** Put the viewer clock at `sec` tape-seconds. */
	setTime(sec: number): void;
	clear(): void;
	/** Vehicle track positions at the current clock, by track id (test snapshot + HUD). */
	markers(): Record<string, ReplayMarker>;
	count(): number;
	destroy(): void;
}

export function createReplayOverlay(cesium: CesiumGlobal, viewer: CesiumViewer): ReplayOverlay {
	const entityIds: string[] = [];
	let tape: ReplayTape | null = null;
	let t0: CesiumJulianDate | null = null;
	/** Per vehicle track: display label, sampled property, and its reads (camera at time). */
	let tracks: Array<{
		id: string;
		label: string;
		hit: string | null;
		prop: { getValue(t: CesiumJulianDate): unknown };
		reads: Array<{ t: number; camera: string | null }>;
	}> = [];
	let cursor = 0;

	function at(sec: number): CesiumJulianDate {
		return cesium.JulianDate.addSeconds(t0 as CesiumJulianDate, sec, new cesium.JulianDate());
	}

	function clear() {
		for (const id of entityIds) viewer.entities.removeById(id);
		entityIds.length = 0;
		tracks = [];
		tape = null;
		t0 = null;
		try {
			viewer.clock.shouldAnimate = false;
			viewer.clock.currentTime = cesium.JulianDate.now();
		} catch {
			// clock mid-teardown
		}
	}

	function add(entity: Record<string, unknown>): void {
		const e = viewer.entities.add(entity);
		entityIds.push(e.id);
	}

	function show(next: ReplayTape, officer: boolean) {
		const keepCursor = tape?.runId === next.runId ? cursor : 0;
		for (const id of entityIds) viewer.entities.removeById(id);
		entityIds.length = 0;
		tracks = [];
		tape = next;
		t0 = cesium.JulianDate.fromDate(new Date(next.t0));
		const clock = viewer.clock;
		clock.startTime = cesium.JulianDate.clone(t0);
		clock.stopTime = at(Math.max(1, next.durationSec));
		clock.clockRange = cesium.ClockRange.CLAMPED;
		clock.shouldAnimate = false;
		clock.multiplier = 1;

		const canvasInk = cssColor(cesium, '--v-canvas', cesium.Color.BLACK);
		const ink = cssColor(cesium, '--v-ink-1', cesium.Color.WHITE);
		const act = cssColor(cesium, '--v-act', cesium.Color.ORANGE);
		const tool = cssColor(cesium, '--v-tool', cesium.Color.CYAN);

		// Camera sites seen on the tape (fixed rings, labelled).
		const cams = new Map<string, [number, number]>();
		for (const r of next.anpr) if (r.cameraId && !cams.has(r.cameraId)) cams.set(r.cameraId, [r.lon, r.lat]);
		for (const [id, [lon, lat]] of cams) {
			add({
				id: `${REPLAY_PICK_PREFIX}cam-${id}`,
				position: ground(cesium, lon, lat),
				// a draped 60 m ring (ground ellipses have no outline)
				polyline: drapedRing(cesium, { lon, lat, radiusM: 60, width: 2, material: tool, z: DRAPE_Z.trace }),
				label: clampedLabel(cesium, {
					text: `${id} ·EMULATED`,
					font: '11px monospace',
					fillColor: tool,
					outlineColor: canvasInk,
					outlineWidth: 2,
					style: 2,
					verticalOrigin: -1,
					pixelOffset: new cesium.Cartesian2(0, 10),
				}),
				description: `replay camera site ${id} (from the tape) · EMULATED ANPR`,
			});
		}

		// One track per plate.
		const byPlate = new Map<string, typeof next.anpr>();
		for (const r of next.anpr) {
			const list = byPlate.get(r.plate);
			if (list) list.push(r);
			else byPlate.set(r.plate, [r]);
		}
		let n = 0;
		for (const [plate, reads] of byPlate) {
			reads.sort((a, b) => a.t - b.t);
			const prop = new cesium.SampledPositionProperty();
			for (const r of reads) prop.addSample(at(r.t), ground(cesium, r.lon, r.lat));
			prop.backwardExtrapolationType = cesium.ExtrapolationType.NONE;
			prop.forwardExtrapolationType = cesium.ExtrapolationType.HOLD;
			const hit = reads.find((r) => r.hit)?.hit ?? null;
			const label = displayPlate(plate, officer) ?? plate;
			const id = `veh-${n++}`;
			tracks.push({ id, label, hit, prop, reads: reads.map((r) => ({ t: r.t, camera: r.cameraId })) });
			add({
				id: `${REPLAY_PICK_PREFIX}${id}`,
				position: prop,
				// a clamped DOT billboard — never a clamped `point` (draping.ts)
				billboard: clampedDot(cesium, {
					pixelSize: hit ? 13 : 8,
					color: hit ? act : ink,
					outlineColor: canvasInk,
					outlineWidth: 2,
				}),
				label: clampedLabel(cesium, {
					text: `${label}${hit ? ` ·${hit.replace(/_/g, ' ').toUpperCase()}` : ''} ·EMULATED`,
					font: hit ? '600 12px monospace' : '11px monospace',
					fillColor: hit ? act : ink,
					outlineColor: canvasInk,
					outlineWidth: 2,
					style: 2,
					verticalOrigin: 1,
					pixelOffset: new cesium.Cartesian2(0, -12),
				}),
				description: `replay vehicle ${label} · ${reads.length} reads on the tape (${reads
					.map((r) => r.cameraId ?? '—')
					.join(' → ')})${hit ? ` · ${hit}` : ''} · EMULATED ANPR (run ${next.runId})`,
			});
		}

		// One track per agency unit.
		const byUnit = new Map<string, typeof next.units>();
		for (const u of next.units) {
			const list = byUnit.get(u.unitId);
			if (list) list.push(u);
			else byUnit.set(u.unitId, [u]);
		}
		let m = 0;
		for (const [unitId, frames] of byUnit) {
			frames.sort((a, b) => a.t - b.t);
			const prop = new cesium.SampledPositionProperty();
			for (const f of frames) prop.addSample(at(f.t), ground(cesium, f.lon, f.lat));
			prop.backwardExtrapolationType = cesium.ExtrapolationType.NONE;
			prop.forwardExtrapolationType = cesium.ExtrapolationType.HOLD;
			const color = agencyColor(frames[0].agency, cesium);
			add({
				id: `${REPLAY_PICK_PREFIX}unit-${m++}`,
				position: prop,
				billboard: clampedDot(cesium, { pixelSize: 9, color, outlineColor: canvasInk, outlineWidth: 2 }),
				label: clampedLabel(cesium, {
					text: `${unitId} ·EMULATED`,
					font: '11px monospace',
					fillColor: color,
					outlineColor: canvasInk,
					outlineWidth: 2,
					style: 2,
					verticalOrigin: -1,
					pixelOffset: new cesium.Cartesian2(0, 10),
				}),
				description: `replay unit ${unitId} (${frames[0].agency ?? 'agency'}) · ${frames.length} frames · EMULATED`,
			});
		}
		setTime(keepCursor);
	}

	function setTime(sec: number) {
		if (!tape || !t0) return;
		cursor = Math.max(0, Math.min(tape.durationSec, sec));
		viewer.clock.currentTime = at(cursor);
	}

	function markers(): Record<string, ReplayMarker> {
		const out: Record<string, ReplayMarker> = {};
		if (!tape || !t0) return out;
		const now = at(cursor);
		for (const tr of tracks) {
			const p = tr.prop.getValue(now);
			const carto = p ? cesium.Cartographic.fromCartesian(p) : undefined;
			if (!carto) continue;
			let camera: string | null = null;
			for (const r of tr.reads) if (r.t <= cursor) camera = r.camera;
			out[tr.id] = {
				label: tr.label,
				lon: cesium.Math.toDegrees(carto.longitude),
				lat: cesium.Math.toDegrees(carto.latitude),
				camera,
				hit: tr.hit,
			};
		}
		return out;
	}

	return {
		show,
		setTime,
		clear,
		markers,
		count: () => tracks.length,
		destroy: clear,
	};
}
