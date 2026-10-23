/**
 * Basemap providers — the keyless default and the env-keyed upgrade.
 *
 * The KEYLESS DEFAULT (provider id 'osm-keyless', snapshot imageryMode
 * 'keyless-osm') is the proven render path: OSM standard raster tiles via
 * UrlTemplateImageryProvider, dark re-graded on the layer, + ellipsoid
 * terrain + our own 3D entities — ZERO calls to cesium.com/ion or Google.
 * (CARTO's POI-free dark styles were the planned keyless ground, but every
 * basemaps.cartocdn.com style now answers an 'API KEY REQUIRED' placeholder
 * tile — not keyless any more; verified 2026-10-23.)
 *
 * The ION UPGRADE (provider id 'ion-photoreal', requiresKeyId 'ion') is a
 * provider switch, capability-gated the way gev gates keyed sources
 * (src/sources/capability.js): it activates ONLY when the operator set
 * VEER_CESIUM_ION_TOKEN on the gods-eye-deck service (read server-side by
 * routes/gods-eye/mode/+server.ts). Cesium 1.146 removed the synchronous
 * ion constructors (`new IonImageryProvider({assetId})` throws on the
 * missing tiling scheme and took the whole viewer down), so the upgrade is
 * built from the async factories — `ImageryLayer.fromProviderAsync(
 * IonImageryProvider.fromAssetId(2))` + `Terrain.fromWorldTerrain()` — whose
 * failures (bad token, ion unreachable) surface on `errorEvent` AFTER the
 * viewer exists. The canvas listens and swaps in the keyless basemap, so a
 * bad token degrades to keyless imagery instead of an unavailable canvas.
 * On the same key, the photoreal-3d layer (layers/photoreal-3d.ts, contract
 * C-L) adds Google Photorealistic 3D Tiles (ion asset 2275207).
 * Attribution to gods-eye-view applies to the provider-registry pattern, not
 * the tile data (OSM raster is ODbL, credited ON SCREEN — osmScreenCredit:
 * '© OpenStreetMap contributors' on the canvas's credit plate, both modes).
 *
 * HONEST MODE (the chip and the snapshot's imageryMode): imageryModeOf() /
 * imageryChip() name what the canvas actually shows. Once the photoreal
 * layer's tiles cover the view the city IS Google's 3D mesh (ion asset
 * 2275207) — the chip says PHOTOREAL 3D · GOOGLE and the mode is
 * 'ion-photoreal' even if the imagery under the mesh stumbled or fell back
 * to the keyless raster (the canvas's note still says so): the schematic
 * fallback describes a flat dark globe, which is not what is on screen.
 * Without the tiles the old ladder holds: offline schematic → ion aerial →
 * keyless OSM (+ the fallback note). The chip text is registered EN/HI/GU
 * below (registerPack, the additive i18n contract); operational names (OSM,
 * ION, GOOGLE, 3D) stay Latin in every language, like the deck's other
 * codes.
 */

import { registerPack } from '$lib/i18n/strings';

export interface BasemapProvider {
	id: string;
	name: string;
	/** Key-setup id the provider needs, or null when keyless (gev pattern). */
	requiresKeyId: string | null;
}

export const BASEMAP_PROVIDERS: ReadonlyArray<BasemapProvider> = Object.freeze([
	{ id: 'osm-keyless', name: 'OpenStreetMap raster (dark re-grade) + ellipsoid terrain (keyless)', requiresKeyId: null },
	{ id: 'ion-photoreal', name: 'Cesium ion aerial imagery + world terrain (+ Google Photorealistic 3D Tiles)', requiresKeyId: 'ion' },
]);

export const OSM_TILE_URL = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png';
export const OSM_CREDIT = '© OpenStreetMap contributors (ODbL) · Veer God\'s-eye canvas';

/**
 * The OSM attribution ON SCREEN (OSMF attribution guidelines: 'OpenStreetMap'
 * spelled out, visible where the map is, linked to the copyright page) — on
 * the canvas's credit plate on BOTH canvases: the keyless raster is OSM, and
 * the keyed canvas still draws OSM-derived road geometry (the flow network,
 * the spines). Abbreviations ('© OSM' on the legend chip) and the lightbox
 * alone are not enough at 1920×1080.
 */
export const OSM_COPYRIGHT_URL = 'https://www.openstreetmap.org/copyright';
export const OSM_SCREEN_CREDIT_TEXT = '© OpenStreetMap contributors';
export const OSM_SCREEN_CREDIT_HTML = `<a href="${OSM_COPYRIGHT_URL}" target="_blank" rel="noopener noreferrer">${OSM_SCREEN_CREDIT_TEXT}</a>`;

/**
 * The on-screen OSM credit (Cesium Credit, showOnScreen) — one html, so the
 * keyless raster's credit and the canvas's static credit share Cesium's
 * credit id and print once. Null on a build without Credit (the string
 * credit then lands in the lightbox as before).
 */
export function osmScreenCredit(cesium: CesiumGlobal): unknown | null {
	const Credit = cesium.Credit;
	if (typeof Credit !== 'function') return null;
	try {
		return new Credit(OSM_SCREEN_CREDIT_HTML, true);
	} catch {
		return null;
	}
}

/** Cesium ion asset 2 = Bing Maps aerial (the ion default imagery). */
export const ION_IMAGERY_ASSET_ID = 2;
/** Cesium ion asset 2275207 = Google Photorealistic 3D Tiles (photoreal-3d layer). */
export const PHOTOREAL_3D_ASSET_ID = 2275207;

/**
 * How long the ion upgrade may take to report ready before the canvas
 * falls back to keyless (ion unreachable can hang rather than fail).
 */
export const ION_READY_TIMEOUT_MS = 12_000;

/**
 * COMMAND RE-GRADE — the dark basemap adjustment. The first grade
 * (brightness 0.32, gamma 0.9) measured a mean frame luma of 63/255 in RAW
 * and left the road skeleton illegible; this lift (measured ≈86/255 over
 * the home view) keeps the ground clearly darker than the entities
 * (spines, markers, the congestion ramp) while street names, the river and
 * the rail lines stay readable: brightness ≈0.42, contrast ≈1.22 (pushes
 * the dark label ink and the road casings apart from the ground),
 * saturation ≈0.62, neutral gamma. Applied to the imagery LAYER (not a
 * post-process stage) so the FLIR sensor shader keeps grading the
 * re-graded raster: sensor modes and the dark ground compose instead of
 * fighting. Pure numbers — no colour literals here; the palette lives in
 * the design tokens.
 */
export const DARK_REGRADE = { brightness: 0.42, contrast: 1.22, saturation: 0.62, gamma: 1.0 } as const;

/** Gentle dim for ion aerial imagery (already dark; keeps entities on top). */
export const AERIAL_GRADE = { brightness: 0.85, contrast: 1.05, saturation: 0.85, gamma: 1.0 } as const;

type Grade = { brightness: number; contrast: number; saturation: number; gamma: number };

function applyGrade(layer: unknown, grade: Grade): void {
	const l = layer as { brightness?: number; contrast?: number; saturation?: number; gamma?: number } | null;
	if (!l) return;
	l.brightness = grade.brightness;
	l.contrast = grade.contrast;
	l.saturation = grade.saturation;
	l.gamma = grade.gamma;
}

/** Apply the command re-grade to an imagery layer (idempotent, null-safe). */
export function applyDarkRegrade(layer: unknown): void {
	applyGrade(layer, DARK_REGRADE);
}

/** What the canvas hands the Viewer: a base layer + terrain in one of two forms. */
export interface Basemap {
	id: 'osm-keyless' | 'ion-photoreal';
	baseLayer: unknown;
	/** Synchronous terrain provider (keyless path). */
	terrainProvider?: unknown;
	/** Async terrain wrapper (Viewer option `terrain`; ion path). */
	terrain?: CesiumTerrain;
	/** Async readiness hooks — present only on the ion path. */
	imageryLayer?: CesiumImageryLayer;
}

/** Imagery layer + terrain for the keyless default (verified render path). */
export function createKeylessBasemap(cesium: CesiumGlobal): Basemap {
	const provider = new cesium.UrlTemplateImageryProvider({
		url: OSM_TILE_URL,
		// on the credit plate itself, not only behind 'Data attribution'
		credit: osmScreenCredit(cesium) ?? OSM_CREDIT,
		maximumLevel: 19,
	});
	const layer = new cesium.ImageryLayer(provider);
	applyDarkRegrade(layer);
	return {
		id: 'osm-keyless',
		baseLayer: layer,
		terrainProvider: new cesium.EllipsoidTerrainProvider(),
	};
}

/**
 * Imagery + terrain for the ion upgrade. The token is handed to the page by
 * the same-origin mode endpoint (env-only; never in source) — Cesium ion
 * access tokens are client credentials by design.
 *
 * Throws synchronously only if this Cesium build lacks the async factories;
 * the caller catches that and uses createKeylessBasemap(). Asynchronous
 * failures (a bad or expired token answers 401) arrive on
 * `imageryLayer.errorEvent` / `terrain.errorEvent`.
 */
export function createIonBasemap(cesium: CesiumGlobal, token: string): Basemap {
	cesium.Ion.defaultAccessToken = token;
	const imageryLayer = cesium.ImageryLayer.fromProviderAsync(
		cesium.IonImageryProvider.fromAssetId(ION_IMAGERY_ASSET_ID),
	);
	applyGrade(imageryLayer, AERIAL_GRADE);
	return {
		id: 'ion-photoreal',
		baseLayer: imageryLayer,
		terrain: cesium.Terrain.fromWorldTerrain(),
		imageryLayer,
	};
}

/** The photoreal-3d layer's coverage as the canvas holds it (stats['photoreal-3d']). */
export interface PhotorealCoverage {
	via?: string;
	detail?: Record<string, number>;
}

/** The 3D tiles cover the view (the photoreal layer is ready on the ion key). */
export function photorealReady(p3d: PhotorealCoverage | null | undefined): boolean {
	return p3d?.via === 'ion' && p3d.detail?.initialTilesLoaded === 1;
}

/** What the canvas shows, as the snapshot names it (tests and share links read it). */
export function imageryModeOf(state: {
	offline: boolean;
	basemapId: Basemap['id'];
	photoreal?: PhotorealCoverage | null;
}): 'keyless-osm' | 'ion-photoreal' | 'offline-schematic' {
	if (photorealReady(state.photoreal)) return 'ion-photoreal';
	if (state.offline) return 'offline-schematic';
	return state.basemapId === 'ion-photoreal' ? 'ion-photoreal' : 'keyless-osm';
}

/** The imagery chip's i18n key — one per state the canvas can be in. */
export function imageryChip(state: {
	offline: boolean;
	basemapId: Basemap['id'];
	/** Why the ion upgrade fell back to keyless (null when it did not). */
	fallbackNote: string | null;
	photoreal?: PhotorealCoverage | null;
}): string {
	if (photorealReady(state.photoreal)) return 'ge_imagery_photoreal';
	if (state.offline) return 'ge_imagery_offline';
	if (state.basemapId === 'ion-photoreal') {
		return state.photoreal?.via === 'ion' ? 'ge_imagery_photoreal_loading' : 'ge_imagery_ion';
	}
	return state.fallbackNote ? 'ge_imagery_osm_fallback' : 'ge_imagery_osm';
}

// The chip's words (en + hi + gu, mandatory for every user-facing string).
registerPack('en', {
	ge_imagery_photoreal: 'PHOTOREAL 3D · GOOGLE',
	ge_imagery_photoreal_loading: 'PHOTOREAL 3D · LOADING',
	ge_imagery_ion: 'ION AERIAL · 3D TERRAIN',
	ge_imagery_osm: 'OSM · DARK',
	ge_imagery_osm_fallback: 'OSM · DARK · FALLBACK',
	ge_imagery_offline: 'OFFLINE SCHEMATIC MODE',
});
registerPack('hi', {
	ge_imagery_photoreal: 'यथार्थ 3D शहर · GOOGLE',
	ge_imagery_photoreal_loading: 'यथार्थ 3D · लोड हो रहा है',
	ge_imagery_ion: 'ION हवाई चित्र · 3D भू-भाग',
	ge_imagery_osm: 'OSM · डार्क',
	ge_imagery_osm_fallback: 'OSM · डार्क · वैकल्पिक',
	ge_imagery_offline: 'ऑफ़लाइन योजनाबद्ध मोड',
});
registerPack('gu', {
	ge_imagery_photoreal: 'વાસ્તવિક 3D શહેર · GOOGLE',
	ge_imagery_photoreal_loading: 'વાસ્તવિક 3D · લોડ થાય છે',
	ge_imagery_ion: 'ION હવાઈ ચિત્ર · 3D ભૂપ્રદેશ',
	ge_imagery_osm: 'OSM · ડાર્ક',
	ge_imagery_osm_fallback: 'OSM · ડાર્ક · વૈકલ્પિક',
	ge_imagery_offline: 'ઑફલાઇન યોજનાકીય મોડ',
});
