/**
 * God's-eye layer registry — the extension contract for canvas layers.
 *
 * PORTED FROM gods-eye-view (https://github.com/bilawalsidhu/gods-eye-view),
 * MIT License © bilawalsidhu, specifically src/layers/streetLevel/registry.js
 * (StreetLevelProvider/ProviderInstance typedefs + validateProviders: id
 * grammar, duplicate ids, pick-prefix overlap). Source-level port, not a
 * fork; the Veer contract is narrower (data layers over fixed same-origin
 * endpoints) but the shape — id/name/requiresKeyId/pickPrefix/create with
 * status/init/activate/deactivate/refresh/handlePick/coverageStats — is the
 * gev pattern so future layers read the same.
 *
 * Veer deviations (documented so the port stays auditable):
 *   - providers fetch ONLY through the deck origin (fixed constants from
 *     src/lib/ops/endpoints.ts); gev providers hit public tile APIs.
 *   - `colors`/`credit` become `color`/`credit` on a per-layer basis in the
 *     layer module itself; the registry keeps the id/prefix integrity rules.
 *   - added `defaultOn` so a share link can restore layer toggles.
 *   - added `urnOf` (VEER CITY COMMAND): every picked primitive maps back to
 *     the C-C entity URN it draws (`event:<id>`, `vehicle:<id>`, …), so a
 *     click on any layer opens the same dossier the fusion rail and the
 *     board open — the pick prefix says WHICH layer, the URN says WHAT.
 */

/** Handed to `create()` once by the canvas controller. */
export interface GodsEyeLayerContext {
	/** The global Cesium loaded from /cesium/Cesium.js. */
	cesium: CesiumGlobal;
	viewer: CesiumViewer;
	/**
	 * Same-origin JSON fetch through the deck's fixed endpoint constants.
	 * Layer code must never assemble URLs from external input — ids go
	 * through `safePathId` inside the data module.
	 */
	fetchJson: (path: string, signal?: AbortSignal) => Promise<unknown>;
	/** Ask the controller to publish a new UI snapshot (HUD chip counts). */
	notify: () => void;
}

/** Per-layer stats surfaced in the HUD (coverageStats in gev terms). */
export interface LayerCoverageStats {
	count: number;
	loading: boolean;
	/** e.g. 'live', 'fixtures', 'not-deployed (404)', 'offline'. */
	hint: string;
	error: string | null;
	/**
	 * Named sub-counts behind `count` (e.g. events {live, preview}, flow
	 * {coloured, segments, simulated}) — published on the test snapshot's
	 * layerStats and the legend, never invented: each is a count of drawn rows.
	 */
	detail?: Record<string, number>;
	/** Where the layer's rows came from right now ('city-api' | 'fallback' | …). */
	via?: string;
}

/** One provider instance, created per canvas mount. */
export interface GodsEyeLayerInstance {
	status(): Promise<{ configured: boolean }>;
	/** Create entity collections (hidden) — receives the layer context. */
	init(ctx: GodsEyeLayerContext): void;
	/** Start drawing + polling. */
	activate(): void;
	/** Detach, clear entities and timers, hide. */
	deactivate(): void;
	/** One-shot data refresh (called by activate and on demand). */
	refresh(): void;
	/** The picked entity id carries this layer's own prefix. */
	handlePick(pickId: string): boolean;
	/** The C-C entity URN a picked primitive id draws (null when it draws none). */
	urnOf?(pickId: string): string | null;
	coverageStats(): LayerCoverageStats;
	destroy(): void;
}

/** A registered layer (the provider object; gev StreetLevelProvider). */
export interface GodsEyeLayer {
	/** Stable slug id — also the share-link layer key. */
	id: string;
	/** Human name for captions and legends. */
	name: string;
	/** Chip text (HUD). */
	label: string;
	/** Key-setup id the layer needs ('ion'), or null when keyless. */
	requiresKeyId: string | null;
	/** Every primitive id the layer creates starts with it. */
	pickPrefix: string;
	/** Layers start on (share links can turn them off). */
	defaultOn: boolean;
	create(ctx: GodsEyeLayerContext): GodsEyeLayerInstance;
}

const REQUIRED = Object.freeze(['id', 'name', 'label', 'pickPrefix', 'create'] as const);

const ID_GRAMMAR = /^[a-z][a-z0-9-]*$/;

/**
 * Validate layers and freeze their order (also the chip order) — the
 * validateProviders mechanics from gev registry.js, ported.
 */
export function validateLayers(layers: GodsEyeLayer[]): ReadonlyArray<GodsEyeLayer> {
	if (!Array.isArray(layers)) throw new TypeError('Gods-eye layers must be an array');
	const ids = new Set<string>();
	const prefixes: string[] = [];
	for (const layer of layers) {
		const label = layer?.id ?? '(unnamed)';
		for (const key of REQUIRED) {
			if (layer?.[key] === undefined || layer?.[key] === null) {
				throw new TypeError(`Gods-eye layer ${label} lacks ${key}`);
			}
		}
		if (typeof layer.id !== 'string' || !ID_GRAMMAR.test(layer.id)) {
			throw new TypeError(`Gods-eye layer id ${label} is not a slug`);
		}
		if (ids.has(layer.id)) throw new TypeError(`Duplicate Gods-eye layer ${layer.id}`);
		if (typeof layer.create !== 'function') {
			throw new TypeError(`Gods-eye layer ${label}: create is not a function`);
		}
		if (typeof layer.pickPrefix !== 'string' || !layer.pickPrefix) {
			throw new TypeError(`Gods-eye layer ${label} needs a pick prefix`);
		}
		for (const other of prefixes) {
			if (other.startsWith(layer.pickPrefix) || layer.pickPrefix.startsWith(other)) {
				throw new TypeError(
					`Gods-eye layer ${label}: pick prefix ${layer.pickPrefix} overlaps ${other}`,
				);
			}
		}
		ids.add(layer.id);
		prefixes.push(layer.pickPrefix);
	}
	return Object.freeze([...layers]);
}

/** The key id every layer shares, or null (gev requiresKeyIdFor, ported). */
export function requiresKeyIdFor(layers: ReadonlyArray<GodsEyeLayer>): string | null {
	const keys = new Set(layers.map((l) => l.requiresKeyId ?? null));
	if (keys.size !== 1) return null;
	return [...keys][0];
}
