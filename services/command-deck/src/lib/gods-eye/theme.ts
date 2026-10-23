/**
 * Canvas theme access — the design-token bridge for Cesium code.
 *
 * Cesium/deck.gl/SVG cannot read CSS custom properties, and the design
 * system's typed mirror (`$lib/design/tokens.ts`, cssVar()/bandColor()) is a
 * design-shell export. Canvas modules here consume the SAME token contract
 * through its other documented half: the runtime CSS custom properties that
 * tokens.css defines under [data-theme] — no second palette is invented
 * (that would fork the design system, which this stream must not do).
 *
 * Rules honoured:
 *   - ZERO hex literals in this stream's files (design-tokens hex sweep):
 *     every colour is read from a --v-* custom property at runtime.
 *   - When a token is absent (design shell not yet mounted), the reader
 *     returns null and callers degrade to Cesium defaults or skip the
 *     styling — the canvas stays functional, never wrongly coloured.
 *   - Values are cached per read; use `readThemeFresh()` after a theme swap.
 */

/** Read one --v-* token from the current [data-theme] scope. Null if unset. */
export function cssVar(name: string): string | null {
	if (typeof document === 'undefined') return null;
	const prop = name.startsWith('--') ? name : `--${name}`;
	const value = getComputedStyle(document.documentElement).getPropertyValue(prop).trim();
	return value || null;
}

/** Band ladder colour for city state (command theme tokens). */
export function bandColor(band: string): string | null {
	return cssVar(`--v-band-${band.toLowerCase()}`);
}

/** The human-act orange (pursuit traces, decision edges). */
export const ACT = () => cssVar('--v-act');
export const ACT_2 = () => cssVar('--v-act-2');

/** Data accent (identity/live entities). */
export const DATA = () => cssVar('--v-data');

/** Ink ramp for labels (2 = secondary text on canvas). */
export const INK_1 = () => cssVar('--v-ink-1');
export const INK_3 = () => cssVar('--v-ink-3');

/** Canvas ground (for point outlines that must sit on the dark table). */
export const CANVAS = () => cssVar('--v-canvas');

/**
 * A token colour at reduced alpha (CSS string). Token values that are not
 * #rrggbb (rgba()/keywords) pass through unchanged — the caller's veil then
 * runs at full token alpha instead of inventing a second palette.
 */
export function withAlpha(css: string, alpha: number): string {
	if (/^#[0-9a-f]{6}$/i.test(css)) {
		const r = parseInt(css.slice(1, 3), 16);
		const g = parseInt(css.slice(3, 5), 16);
		const b = parseInt(css.slice(5, 7), 16);
		return `rgba(${r},${g},${b},${alpha})`;
	}
	return css;
}

/**
 * Resolve a token with a caller-supplied Cesium fallback colour object —
 * used only where Cesium requires a Color instance and null is impossible
 * (e.g. entity outlines). The fallback is a Cesium default, never a new
 * palette entry.
 */
export function cssColor(cesium: CesiumGlobal, token: string, fallback?: unknown): unknown {
	const value = cssVar(token);
	if (value) {
		try {
			return cesium.Color.fromCssColorString(value);
		} catch {
			// malformed token value — fall through to the fallback
		}
	}
	return fallback ?? cesium.Color.WHITE;
}
