/**
 * Basemap providers — the keyless default and the env-keyed upgrade.
 *
 * The KEYLESS DEFAULT is the proven render path (verified live this
 * session): OSM raster tiles via UrlTemplateImageryProvider +
 * EllipsoidTerrainProvider + our own 3D entities — ZERO calls to
 * cesium.com/ion.
 *
 * The ION UPGRADE (provider id 'ion-photoreal', requiresKeyId 'ion') is a
 * provider switch, capability-gated the way gev gates keyed sources
 * (src/sources/capability.js): it activates ONLY when the operator set
 * VEER_CESIUM_ION_TOKEN on the gods-eye-deck service (read server-side by
 * routes/gods-eye/mode/+server.ts). Attribution to gods-eye-view applies to
 * the provider-registry pattern, not the tile data (OSM raster is ODbL,
 * credited on-globe).
 */

export interface BasemapProvider {
	id: string;
	name: string;
	/** Key-setup id the provider needs, or null when keyless (gev pattern). */
	requiresKeyId: string | null;
}

export const BASEMAP_PROVIDERS: ReadonlyArray<BasemapProvider> = Object.freeze([
	{ id: 'osm-keyless', name: 'OpenStreetMap raster + ellipsoid terrain (keyless)', requiresKeyId: null },
	{ id: 'ion-photoreal', name: 'Cesium ion photorealistic (imagery + world terrain)', requiresKeyId: 'ion' },
]);

export const OSM_TILE_URL = 'https://tile.openstreetmap.org/{z}/{x}/{y}.png';
export const OSM_CREDIT = '© OpenStreetMap contributors (ODbL) · Veer God\'s-eye canvas';

/**
 * COMMAND RE-GRADE — the dark basemap adjustment (design contract: brightness
 * ≈0.32, contrast ≈1.12, saturation ≈0.5, plus a 0.9 gamma to keep the road
 * skeleton legible at that brightness). Applied to the imagery LAYER (not a
 * post-process stage) so the FLIR sensor shader keeps grading the re-graded
 * raster: sensor modes and the dark ground compose instead of fighting. This
 * kills the light-map-inside-dark-shell split. Pure numbers — no colour
 * literals here; the palette lives in the design tokens.
 */
export const DARK_REGRADE = { brightness: 0.32, contrast: 1.12, saturation: 0.5, gamma: 0.9 } as const;

/** Apply the command re-grade to an imagery layer (idempotent, null-safe). */
export function applyDarkRegrade(layer: unknown): void {
	const l = layer as { brightness?: number; contrast?: number; saturation?: number; gamma?: number } | null;
	if (!l) return;
	l.brightness = DARK_REGRADE.brightness;
	l.contrast = DARK_REGRADE.contrast;
	l.saturation = DARK_REGRADE.saturation;
	l.gamma = DARK_REGRADE.gamma;
}

/** Imagery layer + terrain for the keyless default (verified render path). */
export function createKeylessBasemap(cesium: CesiumGlobal): { baseLayer: unknown; terrainProvider: unknown } {
	const provider = new cesium.UrlTemplateImageryProvider({
		url: OSM_TILE_URL,
		credit: OSM_CREDIT,
		maximumLevel: 19,
	});
	const layer = new cesium.ImageryLayer(provider);
	applyDarkRegrade(layer);
	return {
		baseLayer: layer,
		terrainProvider: new cesium.EllipsoidTerrainProvider(),
	};
}

/**
 * Imagery + terrain for the ion upgrade. The token is handed to the page by
 * the same-origin mode endpoint (env-only; never in source) — Cesium ion
 * access tokens are client credentials by design.
 */
export function createIonBasemap(
	cesium: CesiumGlobal,
	token: string,
): { baseLayer: unknown; terrainProvider: unknown } {
	cesium.Ion.defaultAccessToken = token;
	return {
		baseLayer: new cesium.ImageryLayer(new cesium.IonImageryProvider({ assetId: 2 })),
		terrainProvider: new cesium.CesiumTerrainProvider({
			url: 'https://tile.cesium.com/v1/terrain/world?access_token=' + token,
		}),
	};
}
