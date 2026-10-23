/**
 * Mono formatting helpers — every mutable number (timestamps, countdowns,
 * retention days, ETAs, speeds) renders mono per the design contract; these
 * helpers produce the strings, components apply the mono font token.
 *
 * Wall time is IST everywhere (Intl, timeZone 'Asia/Kolkata' — the shared
 * primitives/time.ts clock), so the board, the dossier and the audit stream
 * print the same instant identically whatever zone the browser runs in.
 */

import { IST_ZONE, istClock, istDay, istHm, istHmDated, toEpochMs } from '$lib/primitives/time';

export { IST_ZONE, istClock, istDay, istHm, istHmDated, toEpochMs };

/** HH:MM:SS wall clock in IST (24 h). */
export function fmtClock(ts: number): string {
	return istClock(ts);
}

/** Elapsed since an epoch/ISO moment as compact "1h 04m 12s". */
export function fmtAgeSince(since: number | string | null | undefined, now = Date.now()): string {
	if (since == null) return '—';
	const t = typeof since === 'number' ? since : Date.parse(since);
	if (!Number.isFinite(t)) return '—';
	return fmtDuration(Math.max(0, Math.round((now - t) / 1000)));
}

/** A duration in seconds as compact "2d 04h" / "1h 04m 12s" / "4m 12s" / "12s". */
export function fmtDuration(totalSec: number | null | undefined): string {
	if (totalSec == null || !Number.isFinite(totalSec)) return '—';
	let s = Math.max(0, Math.round(totalSec));
	const d = Math.floor(s / 86400);
	s -= d * 86400;
	const h = Math.floor(s / 3600);
	s -= h * 3600;
	const m = Math.floor(s / 60);
	s -= m * 60;
	if (d > 0) return `${d}d ${String(h).padStart(2, '0')}h`;
	if (h > 0) return `${h}h ${String(m).padStart(2, '0')}m ${String(s).padStart(2, '0')}s`;
	if (m > 0) return `${m}m ${String(s).padStart(2, '0')}s`;
	return `${s}s`;
}

/** Retention remaining until an ISO horizon as compact "29d 04h" (mono). */
export function fmtRetentionUntil(iso: string | null | undefined, now = Date.now()): string {
	if (!iso) return '—';
	const t = Date.parse(iso);
	if (!Number.isFinite(t)) return '—';
	let s = Math.max(0, Math.round((t - now) / 1000));
	const d = Math.floor(s / 86400);
	s -= d * 86400;
	const h = Math.floor(s / 3600);
	return d > 0 ? `${d}d ${String(h).padStart(2, '0')}h` : `${h}h ${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}m`;
}

/**
 * Golden-hour elapsed since an ISO moment, signed "+MMm SSs" — the 108
 * request counter. Past 60 min it keeps counting honestly (no cap theatre).
 */
export function fmtGoldenHour(iso: string | null | undefined, now = Date.now()): string {
	if (!iso) return '—';
	const t = Date.parse(iso);
	if (!Number.isFinite(t)) return '—';
	const s = Math.max(0, Math.round((now - t) / 1000));
	const m = Math.floor(s / 60);
	return `+${String(m).padStart(2, '0')}m ${String(s % 60).padStart(2, '0')}s`;
}

/** ISO moment → IST "HH:MM:SS" ('—' when absent or unparseable). */
export function fmtTimeIso(iso: string | null | undefined): string {
	return istClock(iso ?? null);
}

/**
 * A phase / event window in IST as "HH:MM → HH:MM" (each end dated when it
 * is not today in IST; an open end reads 'open'). The board and the dossier
 * both print windows through this one function, so they always agree.
 */
export function fmtWindowIst(start: string | null | undefined, end: string | null | undefined, now = Date.now()): string {
	const a = start ? istHmDated(start, now) : '—';
	const b = end ? istHmDated(end, now) : 'open';
	return `${a} → ${b}`;
}

// ─── Plates (C6: masked unless a named duty officer is on console) ──────────

const PLATE_MASK = '•••';

/**
 * Indian registration marks inside free text (state code, RTO number,
 * optional series, 4-digit number) — the oracle's own pattern
 * (traffic_oracle/city/common.py _PLATE_IN_TEXT_RE): only the 4-digit
 * number is masked, its last digit kept, so GJ27EM9001 → GJ27EM•••1.
 */
const PLATE_IN_TEXT_RE = /\b([A-Z]{2}[ -]?\d{1,2}[ -]?(?:[A-Z]{1,3}[ -]?)?)(\d{4})\b/g;

/** Mask every registration mark inside free text (server parity). */
export function maskPlatesInText(text: string | null | undefined): string {
	if (!text) return '';
	return text.replace(PLATE_IN_TEXT_RE, (_m, head: string, digits: string) => `${head}${PLATE_MASK}${digits.slice(-1)}`);
}

/** `GJ27EM9001` → `GJ27EM•••1` (city/common.mask_plate parity); an already-masked plate is kept. */
export function maskPlate(plate: string | null | undefined): string | null {
	if (!plate) return null;
	if (plate.includes(PLATE_MASK)) return plate;
	const masked = maskPlatesInText(plate);
	if (masked !== plate) return masked;
	return plate.length >= 6 ? `${plate.slice(0, -4)}${PLATE_MASK}${plate.slice(-1)}` : `${PLATE_MASK}${plate.slice(-1)}`;
}

/** The plate as a console may print it: raw only for a named officer. */
export function plateFor(plate: string | null | undefined, officerNamed: boolean): string | null {
	if (!plate) return null;
	return officerNamed ? plate : maskPlate(plate);
}

/** Free text as a console may print it: plates raw only for a named officer. */
export function textFor(text: string | null | undefined, officerNamed: boolean): string {
	if (!text) return '';
	return officerNamed ? text : maskPlatesInText(text);
}

/**
 * An evidence object (an audit row's detail, a probe's tool args, a refusal
 * report) with every registration mark masked: strings through the text
 * mask, a `plate` key through maskPlate — deep, bounded, never mutating
 * the original.
 */
export function maskDetail(value: unknown): unknown {
	const walk = (v: unknown, key: string | null, depth: number): unknown => {
		if (typeof v === 'string') return key === 'plate' ? maskPlate(v) : maskPlatesInText(v);
		if (depth > 6 || v === null || typeof v !== 'object') return v;
		if (Array.isArray(v)) return v.map((x) => walk(x, null, depth + 1));
		return Object.fromEntries(Object.entries(v as Record<string, unknown>).map(([k, x]) => [k, walk(x, k, depth + 1)]));
	};
	return walk(value, null, 0);
}

/**
 * Free text cut to about `max` characters WITHOUT splitting a registration
 * mark: a mark straddling the cut is kept whole (it is masked when it is
 * printed). A blind slice can leave 'GJ27EM90' — no longer a whole mark,
 * so no mask would catch it, and more digits than the masked form shows.
 */
export function clipText(text: string, max: number): string {
	if (text.length <= max) return text;
	let cut = max;
	for (const m of text.matchAll(PLATE_IN_TEXT_RE)) {
		const start = m.index ?? 0;
		if (start >= cut) break;
		const end = start + m[0].length;
		if (end > cut) {
			cut = end;
			break;
		}
	}
	return text.slice(0, cut);
}

/**
 * The last `max` characters of free text, masked for a console without a
 * named officer. Masked BEFORE the cut, so the window's leading edge can
 * never expose the tail of a split mark ('7EM9001').
 */
export function tailText(text: string, max: number, officerNamed: boolean): string {
	return textFor(text, officerNamed).slice(-max);
}

// ─── Engine sampling under clock compression ────────────────────────────────

/**
 * The situation engine observes every subject once per ENGINE_TICK_SEC of
 * WALL time (adaptive/engine_loop.py DEFAULT_POLL_INTERVAL_SEC). Under a
 * clock-compressed tape one wall tick covers `tick × scale` simulated
 * seconds — the effective sampling the emulator panel states, e.g. ×60 →
 * '1 decision / 5 sim-min'.
 */
export const ENGINE_TICK_SEC = 5;

/** Simulated seconds one engine observation covers at `clockScale`. */
export function simSecPerDecision(clockScale: number): number {
	return ENGINE_TICK_SEC * Math.max(1, clockScale);
}

/**
 * The sampling at `clockScale` as a value + unit, for the trilingual note
 * (tOps 'emuSamplingNote'): ×60 → {5, 'min'} ('1 decision / 5 sim-min'),
 * ×10 → {50, 's'} ('1 decision / 50 sim-s').
 */
export function samplingOf(clockScale: number): { value: number; unit: 'min' | 's' } {
	const sec = simSecPerDecision(clockScale);
	return sec >= 60 && sec % 60 === 0 ? { value: sec / 60, unit: 'min' } : { value: sec, unit: 's' };
}
