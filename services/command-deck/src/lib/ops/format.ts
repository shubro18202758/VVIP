/**
 * Mono formatting helpers — every mutable number (timestamps, countdowns,
 * retention days, ETAs, speeds) renders mono per the design contract; these
 * helpers produce the strings, components apply the mono font token.
 */

/** HH:MM:SS wall clock (operator-local, 24h). */
export function fmtClock(ts: number): string {
	const d = new Date(ts);
	return [d.getHours(), d.getMinutes(), d.getSeconds()].map((n) => String(n).padStart(2, '0')).join(':');
}

/** Elapsed since an epoch/ISO moment as compact "1h 04m 12s". */
export function fmtAgeSince(since: number | string | null | undefined, now = Date.now()): string {
	if (since == null) return '—';
	const t = typeof since === 'number' ? since : Date.parse(since);
	if (!Number.isFinite(t)) return '—';
	let s = Math.max(0, Math.round((now - t) / 1000));
	const h = Math.floor(s / 3600);
	s -= h * 3600;
	const m = Math.floor(s / 60);
	s -= m * 60;
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

/** ISO moment → operator-local short "13:04" (or HH:MM:SS when today). */
export function fmtTimeIso(iso: string | null | undefined): string {
	if (!iso) return '—';
	const t = Date.parse(iso);
	if (!Number.isFinite(t)) return '—';
	return fmtClock(t);
}
