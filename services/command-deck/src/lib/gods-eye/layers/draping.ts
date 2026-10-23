/**
 * Draping — how every God's-eye overlay meets the ground it describes.
 *
 * The keyed canvas stands on REAL 3D Ahmedabad (Cesium World Terrain +
 * Google Photorealistic 3D Tiles, layers/photoreal-3d.ts). The city's ground
 * lies within a few metres of the WGS84 ellipsoid (Cesium World Terrain
 * samples −4.6 m in the old city, +0.7 m at the airport), but the tiles'
 * mesh — buildings, trees, flyovers, its own ground — rises through any
 * geometry drawn at a fixed height: the keyless-era 2–45 m lines and
 * markers ran INSIDE the city, so the flow network and the spines vanished
 * under the rooftops and the markers floated off their roads the moment the
 * city turned 3D. The same overlays must still read on the keyless canvas
 * (ellipsoid terrain, the OSM raster), so nothing here is mode-specific: the
 * GROUND decides.
 *
 *   LINES   (flow ramp, event spines, alternates, traces, dispatch arrows,
 *            the convoy track, detours, the pursuit trace / fan / arc, rings)
 *            are DRAPED: `clampToGround` + classificationType BOTH — Cesium's
 *            GroundPolylinePrimitive classifies terrain AND 3D tiles, so a
 *            line lies on the road surface (and over whatever stands on it)
 *            at its pixel width. Draw order on the ground is `zIndex`
 *            (DRAPE_Z), not height: the congestion network at the bottom,
 *            the RECOMMENDED corridor above the spines, the pursuit on top.
 *            Outlines of ground ellipses are unsupported by Cesium, so a
 *            ring is a draped closed polyline (ringCoords) and a fill is a
 *            ground ellipse/polygon (classification, no height).
 *   MARKERS (points, billboards, labels) are CLAMPED to the TERRAIN
 *            (HeightReference CLAMP_TO_TERRAIN): street level — the photoreal
 *            layer keeps the globe's terrain beneath its tiles for exactly
 *            this — with the depth test disabled at every distance, so a
 *            building between the camera and the marker never hides it.
 *            Not the tiles' own surface: a 3D-tile height query reads tile
 *            geometry back from the GPU per marker per tile load in Cesium
 *            1.146 / WebGL2 (measured under SwiftShader: ~92 ms each, frames
 *            of 4.9 s — layers/photoreal-3d.ts), while a terrain query is
 *            CPU-cheap and on Ahmedabad's flat ground within metres of the
 *            street. Labels add a small eye offset toward the camera; their
 *            pixel offsets and distance declutter stay the layer's own.
 *            A DOT is a billboard (clampedDot: a disc drawn once per size and
 *            colour into a cached data URL), never a clamped `point`: Cesium
 *            1.146 draws a clamped point as a POOLED billboard whose width /
 *            height it never resets, so a dot that inherits a ring's or a
 *            pin's billboard renders as that glyph's size — a blurred disc
 *            (reproduced in isolation) — and on one entity a clamped point
 *            and a billboard fight over the same primitive.
 *
 * Every position handed to these helpers is a GROUND position (height 0):
 * the surface supplies the height. The audit (drapeAudit) reads the entities
 * back and counts any graphic that still floats — each layer publishes it
 * on its coverage stats (layerStats.<id>.floating), which is what the 3D
 * smoke asserts is zero.
 *
 * Enum values are the Cesium 1.146 build's own (verified against the
 * vendored static/cesium/Cesium.js: HeightReference NONE 0 · CLAMP_TO_GROUND 1
 * · RELATIVE_TO_GROUND 2 · CLAMP_TO_TERRAIN 3 · RELATIVE_TO_TERRAIN 4 ·
 * CLAMP_TO_3D_TILE 5 · RELATIVE_TO_3D_TILE 6; ClassificationType TERRAIN 0 ·
 * CESIUM_3D_TILE 1 · BOTH 2) and are read from the global when it has them.
 */

/** HeightReference values of the vendored build (fallback when the global lacks the enum). */
export const HEIGHT_REFERENCE = Object.freeze({
	NONE: 0,
	CLAMP_TO_GROUND: 1,
	RELATIVE_TO_GROUND: 2,
	CLAMP_TO_TERRAIN: 3,
	RELATIVE_TO_TERRAIN: 4,
	CLAMP_TO_3D_TILE: 5,
	RELATIVE_TO_3D_TILE: 6,
});

/** ClassificationType values of the vendored build. */
export const CLASSIFICATION_TYPE = Object.freeze({ TERRAIN: 0, CESIUM_3D_TILE: 1, BOTH: 2 });

/**
 * Ground draw order (zIndex of draped geometry): a higher tier paints over a
 * lower one where they overlap — the job height used to do above the
 * ellipsoid. Fixture (PREVIEW) geometry sits below live geometry.
 */
export const DRAPE_Z = Object.freeze({
	flow: 0,
	zone: 1,
	superseded: 2,
	fixtureSpine: 3,
	spine: 4,
	alternate: 5,
	recommended: 6,
	trace: 7,
	dispatch: 8,
	detour: 9,
	convoy: 10,
	pursuit: 11,
});

/**
 * Eye-space nudge (m) toward the camera for clamped labels: a label never
 * z-fights the surface it names, even where a host re-enables depth tests.
 */
export const LABEL_EYE_OFFSET_M = 4;

/** Segments of a draped ring (a closed polyline — ground ellipses have no outline). */
export const RING_SEGMENTS = 48;

/** Metres per degree of latitude (and of longitude at the equator). */
const M_PER_DEG = 111_320;

/** Markers clamp to the terrain (street level) — see the header for why not the tiles. */
function heightRef(cesium: CesiumGlobal): number {
	return cesium.HeightReference?.CLAMP_TO_TERRAIN ?? HEIGHT_REFERENCE.CLAMP_TO_TERRAIN;
}

function classifyBoth(cesium: CesiumGlobal): number {
	return cesium.ClassificationType?.BOTH ?? CLASSIFICATION_TYPE.BOTH;
}

/** A ground position: the surface (terrain or tiles) supplies the height. */
export function ground(cesium: CesiumGlobal, lon: number, lat: number): unknown {
	return cesium.Cartesian3.fromDegrees(lon, lat, 0);
}

/** Ground positions for a lon/lat path. */
export function groundPath(cesium: CesiumGlobal, coords: ReadonlyArray<readonly [number, number]>): unknown[] {
	return coords.map(([lon, lat]) => ground(cesium, lon, lat));
}

/**
 * Polyline graphics DRAPED on terrain and 3D tiles (GroundPolylinePrimitive):
 * the caller's width/material/positions (ground positions, or a
 * CallbackProperty yielding them) plus the clamp, the classification and the
 * ground draw order.
 */
export function drapedPolyline(
	cesium: CesiumGlobal,
	opts: { positions: unknown; width: number; material: unknown; z: number; distanceDisplayCondition?: unknown },
): Record<string, unknown> {
	const { z, ...rest } = opts;
	return { ...rest, clampToGround: true, classificationType: classifyBoth(cesium), zIndex: z };
}

/**
 * Ground FILL (ellipse veil) draped on terrain and tiles: no height, so
 * Cesium builds a classification primitive; no outline (unsupported on the
 * ground — draw the rim with ringCoords + drapedPolyline).
 */
export function drapedEllipse(
	cesium: CesiumGlobal,
	opts: { radiusM: unknown; material: unknown; z: number },
): Record<string, unknown> {
	return {
		semiMinorAxis: opts.radiusM,
		semiMajorAxis: opts.radiusM,
		material: opts.material,
		fill: true,
		outline: false,
		classificationType: classifyBoth(cesium),
		zIndex: opts.z,
	};
}

/** Ground FILL polygon (a zone) draped on terrain and tiles. */
export function drapedPolygon(
	cesium: CesiumGlobal,
	opts: { positions: unknown[]; material: unknown; z: number },
): Record<string, unknown> {
	return {
		hierarchy: { positions: opts.positions },
		material: opts.material,
		fill: true,
		outline: false,
		classificationType: classifyBoth(cesium),
		zIndex: opts.z,
	};
}

/** data-URL cache: one disc image per (size, fill, rim, rim width). */
const dotCache = new Map<string, string>();

/** A Cesium Color (or a CSS string) as a CSS colour string; null when it cannot say. */
function cssOf(c: unknown): string | null {
	if (typeof c === 'string') return c;
	const color = c as { toCssColorString?: () => string; css?: string } | null;
	return color?.toCssColorString?.() ?? color?.css ?? null;
}

/** Render (once) a filled disc with a rim; null outside a DOM. */
function dotImage(px: number, fill: string, rim: string | null, rimPx: number): string | null {
	if (typeof document === 'undefined') return null;
	const key = `${px}|${fill}|${rim ?? ''}|${rimPx}`;
	const hit = dotCache.get(key);
	if (hit) return hit;
	const size = px + 2 * rimPx;
	const scale = 2; // crisp on HiDPI walls
	const canvas = document.createElement('canvas');
	canvas.width = canvas.height = Math.ceil(size * scale);
	const g = canvas.getContext('2d');
	if (!g) return null;
	g.scale(scale, scale);
	g.beginPath();
	g.arc(size / 2, size / 2, px / 2 + rimPx / 2, 0, Math.PI * 2);
	g.fillStyle = fill;
	g.fill();
	if (rim && rimPx > 0) {
		g.lineWidth = rimPx;
		g.strokeStyle = rim;
		g.stroke();
	}
	const url = canvas.toDataURL('image/png');
	dotCache.set(key, url);
	return url;
}

/**
 * A DOT clamped to the ground, never hidden by a building — as BILLBOARD
 * graphics (header: never a clamped `point`). Takes the point vocabulary
 * (pixelSize, color, outlineColor, outlineWidth) and keeps any other
 * billboard option (scaleByDistance, distanceDisplayCondition, …); a
 * time-varying size (a pulse) is `pulse` × pixelSize through the billboard
 * scale. Explicit width/height every time, so a pooled billboard never
 * carries another glyph's size. Without a DOM (unit tests) the image is
 * omitted and the graphics still carry the size and clamp.
 */
export function clampedDot(
	cesium: CesiumGlobal,
	opts: { pixelSize: number; color: unknown; outlineColor?: unknown; outlineWidth?: number; pulse?: unknown } & Record<string, unknown>,
): Record<string, unknown> {
	const { pixelSize, color, outlineColor, outlineWidth = 0, pulse, ...rest } = opts;
	const rimPx = Math.max(0, Math.round(outlineWidth));
	const size = pixelSize + 2 * rimPx;
	const fill = cssOf(color) ?? 'white';
	const image = dotImage(pixelSize, fill, cssOf(outlineColor), rimPx);
	return clampedBillboard(cesium, {
		...rest,
		...(image ? { image } : {}),
		width: size,
		height: size,
		...(pulse !== undefined ? { scale: pulse } : {}),
	});
}

/** Billboard graphics clamped to the ground, never hidden by a building. */
export function clampedBillboard(cesium: CesiumGlobal, opts: Record<string, unknown>): Record<string, unknown> {
	return { ...opts, heightReference: heightRef(cesium), disableDepthTestDistance: Number.POSITIVE_INFINITY };
}

/**
 * Label graphics clamped to the ground, never hidden by a building, nudged
 * toward the camera (LABEL_EYE_OFFSET_M). The caller keeps its pixel offset,
 * vertical origin and distance declutter.
 */
export function clampedLabel(cesium: CesiumGlobal, opts: Record<string, unknown>): Record<string, unknown> {
	const nudge = typeof cesium.Cartesian3 === 'function' ? new cesium.Cartesian3(0, 0, -LABEL_EYE_OFFSET_M) : undefined;
	return {
		...(nudge ? { eyeOffset: nudge } : {}),
		...opts,
		heightReference: heightRef(cesium),
		disableDepthTestDistance: Number.POSITIVE_INFINITY,
	};
}

/**
 * A closed lon/lat loop of `radiusM` around a point (first vertex repeated
 * last) — the rim of a draped ring. Pure; `segments` ≥ 8.
 */
export function ringCoords(lon: number, lat: number, radiusM: number, segments = RING_SEGMENTS): Array<[number, number]> {
	const n = Math.max(8, Math.round(segments));
	const kx = M_PER_DEG * Math.cos((lat * Math.PI) / 180);
	const out: Array<[number, number]> = [];
	for (let i = 0; i <= n; i++) {
		const a = ((i % n) / n) * 2 * Math.PI;
		out.push([lon + (radiusM * Math.sin(a)) / kx, lat + (radiusM * Math.cos(a)) / M_PER_DEG]);
	}
	return out;
}

/** A draped ring (closed ground polyline) around lon/lat. */
export function drapedRing(
	cesium: CesiumGlobal,
	opts: { lon: number; lat: number; radiusM: number; width: number; material: unknown; z: number; distanceDisplayCondition?: unknown },
): Record<string, unknown> {
	const { lon, lat, radiusM, ...rest } = opts;
	return drapedPolyline(cesium, { ...rest, positions: groundPath(cesium, ringCoords(lon, lat, radiusM)) });
}

/** Ground polylines need depth textures (Cesium falls back to an undraped line without them). */
export function groundLinesSupported(cesium: CesiumGlobal, viewer: CesiumViewer): boolean {
	const probe = cesium.Entity?.supportsPolylinesOnTerrain;
	if (typeof probe !== 'function') return true;
	try {
		return probe(viewer.scene) !== false;
	} catch {
		return true;
	}
}

/** How a layer's entities meet the ground (the 3D smoke asserts floating === 0). */
export interface DrapeAudit {
	/** Entities whose lines / fills are draped (clampToGround, or ground geometry). */
	draped: number;
	/** Entities whose markers (point / billboard / label) are clamped. */
	clamped: number;
	/** Entities with ANY graphic left at a fixed height (buried on the 3D city). */
	floating: number;
}

function valueOf(p: unknown): unknown {
	if (p != null && typeof p === 'object' && typeof (p as { getValue?: unknown }).getValue === 'function') {
		try {
			return (p as { getValue: (t?: unknown) => unknown }).getValue();
		} catch {
			return undefined;
		}
	}
	return p;
}

function isClampRef(v: unknown): boolean {
	return (
		v === HEIGHT_REFERENCE.CLAMP_TO_GROUND ||
		v === HEIGHT_REFERENCE.RELATIVE_TO_GROUND ||
		v === HEIGHT_REFERENCE.CLAMP_TO_TERRAIN ||
		v === HEIGHT_REFERENCE.RELATIVE_TO_TERRAIN ||
		v === HEIGHT_REFERENCE.CLAMP_TO_3D_TILE ||
		v === HEIGHT_REFERENCE.RELATIVE_TO_3D_TILE
	);
}

/**
 * Read a layer's entities back (by id) and classify each: draped lines /
 * fills, clamped markers, or floating (any graphic at a fixed height).
 * Works on Cesium entities (Property-wrapped values) and on plain test fakes.
 */
export function drapeAudit(viewer: Pick<CesiumViewer, 'entities'>, ids: Iterable<string>): DrapeAudit {
	const out: DrapeAudit = { draped: 0, clamped: 0, floating: 0 };
	for (const id of ids) {
		const e = viewer.entities.getById(id) as Record<string, unknown> | undefined;
		if (!e) continue;
		let floats = false;
		let drapes = false;
		let clamps = false;
		const polyline = e.polyline as Record<string, unknown> | undefined;
		if (polyline) {
			if (valueOf(polyline.clampToGround) === true) drapes = true;
			else floats = true;
		}
		for (const kind of ['ellipse', 'polygon'] as const) {
			const g = e[kind] as Record<string, unknown> | undefined;
			if (!g) continue;
			const fixed = valueOf(g.height) != null || valueOf(g.extrudedHeight) != null;
			if (fixed && !isClampRef(valueOf(g.heightReference))) floats = true;
			else drapes = true;
		}
		for (const kind of ['point', 'billboard', 'label'] as const) {
			const g = e[kind] as Record<string, unknown> | undefined;
			if (!g) continue;
			if (isClampRef(valueOf(g.heightReference)) && valueOf(g.disableDepthTestDistance) === Number.POSITIVE_INFINITY) clamps = true;
			else floats = true;
		}
		if (floats) out.floating++;
		else if (drapes) out.draped++;
		else if (clamps) out.clamped++;
	}
	return out;
}
