/**
 * Shareable camera URL — encode/parse the /gods-eye view state into the
 * location hash so an operator can hand the exact view to a Jan Rakshak
 * unit or a police control room.
 *
 * PORTED FROM gods-eye-view (https://github.com/bilawalsidhu/gods-eye-view),
 * MIT License © bilawalsidhu, src/sharelink.js — the ShareLinkManager's
 * v=2 camera encoding (lat/lon/alt/heading/pitch/roll + style + toggles)
 * and its parse-on-load non-finite guards (`parseFloat('Infinity')` is not
 * NaN and must be rejected before the values reach Cesium). Source-level
 * port, not a fork; Veer carries sensor-style + layer toggles and drops the
 * bloom/detection fields it does not have.
 *
 * Format (hash, no leading '#'):
 *   v=2&lat=..&lon=..&alt=..&heading=..&pitch=..&roll=..
 *   &style=flir&bhot=0|1[&palette=iron]&layers=events,flow,traces,…
 *   [&sel=<urn>]
 *
 * `palette=iron` rides only with style=flir (Ironbow); older receivers
 * ignore the key and fall back to White-Hot. `sel` hands over the selected
 * entity (a C-C URN — ids only, never a plate) so the receiver's City
 * Command opens the same dossier.
 */

export interface ShareCameraState {
	lat: number;
	lon: number;
	alt: number;
	/** Degrees. */
	heading: number;
	/** Degrees. */
	pitch: number;
	/** Degrees. */
	roll: number;
}

export interface ShareViewState extends ShareCameraState {
	style: 'flir' | 'raw';
	/** White-Hot (false) / Black-Hot (true) when style is flir. */
	bhot: boolean;
	/** FLIR colour map: monochrome (White/Black-Hot) or Ironbow. Only meaningful with style flir. */
	palette?: 'mono' | 'iron';
	/** Layer ids to switch on (missing ids switch off). */
	layers: string[];
	/** Selected entity URN (`<kind>:<id>`), when the sender had one. */
	selected?: string | null;
}

/** URN grammar a share link may carry (kind slug + the oracle's id alphabet). */
const SEL_RE = /^[a-z]{3,16}:[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$/;

/** Ranges a parsed camera value must land in — hostile hashes never fly. */
const LAT_MAX = 90;
const LON_MAX = 180;
const ALT_MIN = 1;
const ALT_MAX = 50_000_000;
const ANGLE_MAX = 360;

function fmt(n: number): string {
	// 6 decimals ≈ 0.1 m — plenty for a camera handoff and stays short.
	return Number(n.toFixed(6)).toString();
}

/** Build the hash body for the current view (gev _buildHashParams, ported). */
export function buildShareHash(state: ShareViewState, layerIds: readonly string[]): string {
	const params = new URLSearchParams();
	params.set('v', '2');
	params.set('lat', fmt(state.lat));
	params.set('lon', fmt(state.lon));
	params.set('alt', fmt(state.alt));
	params.set('heading', fmt(state.heading));
	params.set('pitch', fmt(state.pitch));
	params.set('roll', fmt(state.roll));
	params.set('style', state.style);
	if (state.style === 'flir') {
		params.set('bhot', state.bhot ? '1' : '0');
		if (state.palette === 'iron') params.set('palette', 'iron');
	}
	const known = new Set(layerIds);
	const on = state.layers.filter((l) => known.has(l));
	params.set('layers', (on.length ? on : layerIds).join(','));
	if (state.selected && SEL_RE.test(state.selected)) params.set('sel', state.selected);
	return params.toString();
}

/** Full shareable URL (page origin + path + hash). */
export function buildShareUrl(state: ShareViewState, layerIds: readonly string[]): string {
	return `${window.location.origin}${window.location.pathname}#${buildShareHash(state, layerIds)}`;
}

/**
 * Parse a hash on page load. Returns null for absent/malformed/hostile
 * hashes — the gev non-finite guards plus Veer range clamps, so a crafted
 * link can never produce NaN camera coordinates or absurd altitudes.
 */
export function parseShareHash(hash: string, layerIds: readonly string[]): ShareViewState | null {
	const body = hash.startsWith('#') ? hash.slice(1) : hash;
	if (!body) return null;
	let params: URLSearchParams;
	try {
		params = new URLSearchParams(body);
	} catch {
		return null;
	}
	if (params.get('v') !== '2') return null;

	const lat = Number.parseFloat(params.get('lat') ?? '');
	const lon = Number.parseFloat(params.get('lon') ?? '');
	// Non-finite guards BEFORE any Cesium use (gev sharelink.js:153-165).
	if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null;
	if (Math.abs(lat) > LAT_MAX || Math.abs(lon) > LON_MAX) return null;

	const parseOr = (raw: string | null, fallback: number, max: number): number => {
		const n = Number.parseFloat(raw ?? '');
		if (!Number.isFinite(n)) return fallback;
		return Math.max(-max, Math.min(max, n));
	};

	const style = params.get('style') === 'raw' ? 'raw' : 'flir';
	const known = new Set(layerIds);
	const layers = (params.get('layers') ?? '')
		.split(',')
		.map((s) => s.trim())
		.filter((s) => s.length > 0 && known.has(s));
	const fallbackLayers = layerIds.filter((id) => id !== '');

	const sel = params.get('sel');
	return {
		lat,
		lon,
		alt: parseOr(params.get('alt'), 800, ALT_MAX),
		heading: parseOr(params.get('heading'), 0, ANGLE_MAX),
		pitch: parseOr(params.get('pitch'), -35, ANGLE_MAX),
		roll: parseOr(params.get('roll'), 0, ANGLE_MAX),
		style,
		bhot: params.get('bhot') === '1',
		palette: style === 'flir' && params.get('palette') === 'iron' ? 'iron' : 'mono',
		layers: layers.length ? layers : fallbackLayers,
		selected: sel && SEL_RE.test(sel) ? sel : null,
	};
}
