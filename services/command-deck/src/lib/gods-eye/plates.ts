/**
 * Plate display rail (C6) — vehicles are identified by ANPR plate only,
 * and a plate is shown MASKED (GJ27EM•••1) on every canvas, dossier and
 * feed surface unless a named duty officer is on console.
 *
 * The masking mirrors the oracle's city/common.py exactly (the server
 * masks every label and feed text it sends; the raw plate rides only in
 * props.plate for the officer-named deck), so a plate the deck renders
 * from its own data (ops board titles, interception plans, emulator
 * tapes) reads the same as one the server masked.
 */

const MASK = '•••';

/**
 * Indian registration marks inside free text: state code, RTO number,
 * optional series letters, 4-digit number (GJ27EM9001, GJ 27 EM 9001,
 * GJ-01-AB-1234). Only the 4-digit number is masked, last digit kept.
 */
const PLATE_IN_TEXT_RE = /\b([A-Z]{2}[ -]?\d{1,2}[ -]?(?:[A-Z]{1,3}[ -]?)?)(\d{4})\b/g;

/** Mask every registration mark inside a free-text string. */
export function maskPlatesInText(text: string | null | undefined): string {
	if (!text) return '';
	return text.replace(PLATE_IN_TEXT_RE, (_m, head: string, num: string) => `${head}${MASK}${num.slice(-1)}`);
}

/** `GJ27EM9001` → `GJ27EM•••1` (null-safe; short plates keep their last char only). */
export function maskPlate(plate: string | null | undefined): string | null {
	if (!plate) return null;
	const masked = maskPlatesInText(plate);
	if (masked !== plate) return masked;
	return plate.length >= 6 ? `${plate.slice(0, -4)}${MASK}${plate.slice(-1)}` : `${MASK}${plate.slice(-1)}`;
}

/** The plate as this console may show it: raw only for a named duty officer. */
export function displayPlate(plate: string | null | undefined, officerNamed: boolean): string | null {
	if (!plate) return null;
	return officerNamed ? plate : maskPlate(plate);
}

/** Free text (titles, descriptions) as this console may show it. */
export function displayText(text: string | null | undefined, officerNamed: boolean): string {
	if (!text) return '';
	return officerNamed ? text : maskPlatesInText(text);
}

/**
 * A structured evidence payload (an audit row's `detail`, shown as JSON in
 * the evidence modal) as this console may show it: every registration mark
 * in any string value masked unless a named duty officer is on console.
 * The mask never touches JSON syntax (it matches letters, digits, spaces
 * and dashes only), so the round trip is lossless otherwise.
 */
export function displayDetail(detail: unknown, officerNamed: boolean): unknown {
	if (officerNamed || detail === undefined || detail === null) return detail;
	if (typeof detail === 'string') return maskPlatesInText(detail);
	if (typeof detail !== 'object') return detail;
	try {
		return JSON.parse(maskPlatesInText(JSON.stringify(detail)));
	} catch {
		return detail;
	}
}
