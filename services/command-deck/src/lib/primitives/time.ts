/**
 * IST clock — the ONE wall-time formatter every operator surface uses.
 *
 * The control room runs on India Standard Time whatever the browser's own
 * zone is (a deck opened on a laptop set to UTC must still print the same
 * 20:30:04 the dossier, the board and the audit stream print). Every
 * formatter here pins timeZone 'Asia/Kolkata' through Intl, so equal
 * instants always render as equal strings on every panel.
 *
 * Lives under primitives so the design-shell rows (FeedItem/AuditRow) can
 * use it without importing $lib/ops (R4 boundary); ops/format.ts re-exports
 * it for the board.
 */

export const IST_ZONE = 'Asia/Kolkata';

const clockFmt = new Intl.DateTimeFormat('en-GB', {
	timeZone: IST_ZONE,
	hour: '2-digit',
	minute: '2-digit',
	second: '2-digit',
	hourCycle: 'h23',
});

const hmFmt = new Intl.DateTimeFormat('en-GB', {
	timeZone: IST_ZONE,
	hour: '2-digit',
	minute: '2-digit',
	hourCycle: 'h23',
});

const dayFmt = new Intl.DateTimeFormat('en-GB', {
	timeZone: IST_ZONE,
	day: '2-digit',
	month: 'short',
});

const ymdFmt = new Intl.DateTimeFormat('en-CA', {
	timeZone: IST_ZONE,
	year: 'numeric',
	month: '2-digit',
	day: '2-digit',
});

/** Epoch ms of a number or an ISO string; NaN when neither parses. */
export function toEpochMs(v: number | string | null | undefined): number {
	if (typeof v === 'number') return v;
	if (typeof v === 'string' && v) return Date.parse(v);
	return NaN;
}

/** HH:MM:SS in IST (24 h); '—' for an unparseable moment. */
export function istClock(v: number | string | null | undefined): string {
	const ms = toEpochMs(v);
	return Number.isFinite(ms) ? clockFmt.format(ms) : '—';
}

/** HH:MM in IST (24 h); '—' for an unparseable moment. */
export function istHm(v: number | string | null | undefined): string {
	const ms = toEpochMs(v);
	return Number.isFinite(ms) ? hmFmt.format(ms) : '—';
}

/** The IST calendar day of a moment as YYYY-MM-DD ('' when unparseable). */
export function istDay(v: number | string | null | undefined): string {
	const ms = toEpochMs(v);
	return Number.isFinite(ms) ? ymdFmt.format(ms) : '';
}

/**
 * HH:MM in IST, prefixed with the IST date ('27 Jun 09:20') when the moment
 * is not on today's IST date — a window three days out must not read as
 * this morning.
 */
export function istHmDated(v: number | string | null | undefined, now = Date.now()): string {
	const ms = toEpochMs(v);
	if (!Number.isFinite(ms)) return '—';
	const hm = hmFmt.format(ms);
	return ymdFmt.format(ms) === ymdFmt.format(now) ? hm : `${dayFmt.format(ms)} ${hm}`;
}
