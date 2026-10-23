/**
 * Overlay label vocabulary shared by the God's-eye layers — pure helpers
 * (unit-tested in tests/canvas-layers.unit.mjs).
 *
 *   PLATE   every overlay label stands on the same translucent canvas plate
 *           (the enforcement, choke and earmarked-vehicle look): a 12 px
 *           label is then legible over the photoreal city, the dark OSM
 *           raster and the draped flow / trace lines that run through it.
 *           labelPlate() returns the Cesium label options for it.
 *   WORDS   a feed anomaly reads as an operator says it, in the console's
 *           language — 'Speed jump ×6', not the wire id 'speed_jump ×6' (the
 *           raw anomaly_type stays in the pick card and the dossier). The
 *           EN/HI/GU words are registered on the i18n core (registerPack,
 *           the additive contract); an id outside the C-H vocabulary reads
 *           humanised ('Some new flag'), never as snake_case.
 *   LINES   one site label carries one line per DISTINCT reading: identical
 *           lines (the same sweep filed twice at one camera) fold into one
 *           with a count — 'STOLEN · NO FINE · PENDING ×2' — instead of
 *           printing the same text twice like a rendering stutter.
 */

import { registerPack, t } from '$lib/i18n/strings';
import { cssVar, withAlpha } from '../theme';

/** Opacity of the canvas plate behind overlay labels. */
export const LABEL_PLATE_ALPHA = 0.72;

/**
 * Label options for the translucent canvas plate (showBackground +
 * backgroundColor + padding) — spread into a label's graphics. `padX`/`padY`
 * in px (Cesium's backgroundPadding).
 */
export function labelPlate(cesium: CesiumGlobal, padX = 4, padY = 2): Record<string, unknown> {
	const css = withAlpha(cssVar('--v-canvas') ?? 'black', LABEL_PLATE_ALPHA);
	let backgroundColor: unknown;
	try {
		backgroundColor = cesium.Color.fromCssColorString(css);
	} catch {
		backgroundColor = undefined;
	}
	return {
		showBackground: true,
		...(backgroundColor ? { backgroundColor } : {}),
		...(typeof cesium.Cartesian2 === 'function' ? { backgroundPadding: new cesium.Cartesian2(padX, padY) } : {}),
	};
}

/** The C-H anomaly_type vocabulary (signal-ingress AnomalyFlag::name). */
export const ANOMALY_TYPES = [
	'future_timestamp',
	'stale_data',
	'speed_jump',
	'phantom_reading',
	'speed_zscore',
	'congestion_outlier',
	'road_class_mismatch',
	'spatial_mismatch',
] as const;

registerPack('en', {
	anm_future_timestamp: 'Future timestamp',
	anm_stale_data: 'Stale data',
	anm_speed_jump: 'Speed jump',
	anm_phantom_reading: 'Phantom reading',
	anm_speed_zscore: 'Speed outlier',
	anm_congestion_outlier: 'Congestion outlier',
	anm_road_class_mismatch: 'Road-class mismatch',
	anm_spatial_mismatch: 'Off-road position',
	anm_unknown: 'Feed anomaly',
});
registerPack('hi', {
	anm_future_timestamp: 'भविष्य का समय-चिह्न',
	anm_stale_data: 'पुराना डेटा',
	anm_speed_jump: 'गति में उछाल',
	anm_phantom_reading: 'आभासी रीडिंग',
	anm_speed_zscore: 'असामान्य गति',
	anm_congestion_outlier: 'असामान्य भीड़भाड़',
	anm_road_class_mismatch: 'सड़क-श्रेणी बेमेल',
	anm_spatial_mismatch: 'सड़क से बाहर स्थिति',
	anm_unknown: 'फ़ीड विसंगति',
});
registerPack('gu', {
	anm_future_timestamp: 'ભવિષ્યનો સમય-ચિહ્ન',
	anm_stale_data: 'જૂનો ડેટા',
	anm_speed_jump: 'ઝડપમાં ઉછાળો',
	anm_phantom_reading: 'આભાસી રીડિંગ',
	anm_speed_zscore: 'અસામાન્ય ઝડપ',
	anm_congestion_outlier: 'અસામાન્ય ટ્રાફિક ભીડ',
	anm_road_class_mismatch: 'રસ્તા-વર્ગ મેળ નથી',
	anm_spatial_mismatch: 'રસ્તાની બહાર સ્થાન',
	anm_unknown: 'ફીડ વિસંગતિ',
});

/** 'some_new_flag' → 'Some new flag' (an id outside the vocabulary, never snake_case on the canvas). */
function humanise(id: string): string {
	const words = id.replace(/[_-]+/g, ' ').trim();
	return words ? words.charAt(0).toUpperCase() + words.slice(1) : '';
}

/** A feed anomaly's type as the operator reads it, in `lang` (EN fallback). */
export function anomalyWord(type: string | null | undefined, lang = 'en'): string {
	const id = (type ?? '').trim().toLowerCase();
	if (!id) return t(lang, 'anm_unknown');
	const key = `anm_${id}`;
	const word = t(lang, key);
	return word === key ? humanise(id) || t(lang, 'anm_unknown') : word;
}

/** The anomaly pulse's label: the word, and how often it fired in the window. */
export function anomalyLabel(type: string | null | undefined, count: number | null, lang = 'en'): string {
	return `${anomalyWord(type, lang)}${count != null && count > 1 ? ` ×${count}` : ''}`;
}

/**
 * Fold identical lines into one with a count, first occurrence first:
 * ['A', 'A', 'B'] → [{line:'A', count:2}, {line:'B', count:1}].
 */
export function collapseLines(lines: readonly string[]): Array<{ line: string; count: number }> {
	const out: Array<{ line: string; count: number }> = [];
	const at = new Map<string, number>();
	for (const line of lines) {
		const i = at.get(line);
		if (i != null) out[i].count++;
		else {
			at.set(line, out.length);
			out.push({ line, count: 1 });
		}
	}
	return out;
}

/**
 * A site's ONE label text: its distinct lines (identical ones counted, ×n),
 * at most `max` of them, then '+n more' for the readings left over.
 */
export function siteLabelText(lines: readonly string[], max: number): string {
	const groups = collapseLines(lines);
	const shown = groups.slice(0, Math.max(1, max));
	const rest = groups.slice(shown.length).reduce((n, g) => n + g.count, 0);
	return shown.map((g) => (g.count > 1 ? `${g.line} ×${g.count}` : g.line)).join('\n') + (rest > 0 ? `\n+${rest} more` : '');
}
