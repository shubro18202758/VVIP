/**
 * City Command formatting — IST everywhere (the city's own clock, never the
 * browser's zone), compact ages, entity-kind words. Pure helpers.
 */

const IST_TIME = new Intl.DateTimeFormat('en-GB', {
	timeZone: 'Asia/Kolkata',
	hour: '2-digit',
	minute: '2-digit',
	second: '2-digit',
	hour12: false,
});

const IST_DAY_TIME = new Intl.DateTimeFormat('en-GB', {
	timeZone: 'Asia/Kolkata',
	day: '2-digit',
	month: 'short',
	hour: '2-digit',
	minute: '2-digit',
	hour12: false,
});

/** HH:MM:SS in IST. */
export function istTime(ms: number | null | undefined): string {
	return ms != null && Number.isFinite(ms) ? IST_TIME.format(new Date(ms)) : '—';
}

/** '22 Oct 21:40' in IST (older rows carry their day). */
export function istDayTime(ms: number | null | undefined): string {
	return ms != null && Number.isFinite(ms) ? IST_DAY_TIME.format(new Date(ms)) : '—';
}

/** Row time: HH:MM:SS today, '22 Oct 21:40' on an older day. */
export function istStamp(ms: number | null | undefined, now = Date.now()): string {
	if (ms == null || !Number.isFinite(ms)) return '—';
	return now - ms < 20 * 3600 * 1000 ? istTime(ms) : istDayTime(ms);
}

/** Compact age: 42s · 4m · 3h12m · 2d05h. */
export function fmtAge(sec: number | null | undefined): string {
	if (sec == null || !Number.isFinite(sec)) return '—';
	const s = Math.max(0, Math.floor(sec));
	if (s < 120) return `${s}s`;
	if (s < 3600) return `${Math.floor(s / 60)}m`;
	if (s < 172_800) return `${Math.floor(s / 3600)}h${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}m`;
	return `${Math.floor(s / 86_400)}d${String(Math.floor((s % 86_400) / 3600)).padStart(2, '0')}h`;
}

/** Seconds since an epoch-ms instant (null-safe). */
export function ageOf(ms: number | null | undefined, now = Date.now()): number | null {
	return ms != null && Number.isFinite(ms) ? Math.max(0, (now - ms) / 1000) : null;
}

const KIND_WORD: Record<string, string> = {
	event: 'EVENT',
	vehicle: 'VEHICLE',
	sighting: 'ANPR READ',
	camera: 'CAMERA',
	junction: 'JUNCTION',
	unit: 'UNIT',
	convoy: 'CONVOY',
	dispatch: 'DISPATCH',
	recommendation: 'E-CHALLAN',
	anomaly: 'ANOMALY',
	segment: 'ROAD SEGMENT',
};

/**
 * The operator word for an entity kind (operational codes stay English). A
 * recommendation under the evidence-only R_STOLEN_SIGHTING rule is a
 * sighting on file, never a fine: it reads STOLEN SIGHTING, not E-CHALLAN.
 */
export function kindWord(kind: string | null | undefined, ruleId?: string | null): string {
	if (kind === 'recommendation' && ruleId === 'R_STOLEN_SIGHTING') return 'STOLEN SIGHTING';
	return KIND_WORD[kind ?? ''] ?? (kind ?? 'ENTITY').toUpperCase();
}

/** 'requested_by' → 'requested by'. */
export function humanKey(key: string): string {
	return key.replace(/_/g, ' ');
}
