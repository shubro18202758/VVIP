/**
 * Public advisory derivation — the shared read-model behind /citizen, the
 * /driver proposal and the comms advisory writer.
 *
 * Everything here is a PURE function of the CorridorEvent the ops board
 * already holds (live events first, labelled fixtures otherwise): the hold
 * minutes come from the phase closure policy exactly as the legacy advisory
 * derived them, stretch windows advance with the event, and alternates stay
 * transliterated road names as they appear on city signage.
 */

import type { CorridorEvent } from '$lib/ops/types';
import { localizeNumber } from '$lib/i18n/strings';
import { USE_CASES } from '$lib/ops/usecases';

/** Alternates as on signage (transliterated; identical in every language). */
export const ALTERNATE_ROADS = 'SP Ring Road / Riverfront Road';

export interface StretchHold {
	segmentId: number;
	/** Minutes the stretch is expected to hold. */
	minutes: number;
	/** Wall-clock start of the hold window, localized per language. */
	clock: string;
	state: 'now' | 'next' | 'later';
}

/** Planned hold minutes for an event (max advance-closure across phases). */
export function holdMinutes(event: CorridorEvent): number {
	const advance = event.phases
		.map((p) => p.closurePolicy?.advanceClosureSec ?? 0)
		.reduce((a, b) => Math.max(a, b), 0);
	return Math.max(1, Math.round(advance / 60) || 3);
}

function hhmm(d: Date): string {
	return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

/** Localized HH:MM clock for `offsetSec` seconds from now. */
export function clockAt(offsetSec: number, lang: string): string {
	return localizeNumber(hhmm(new Date(Date.now() + offsetSec * 1000)), lang);
}

/**
 * Stretch-by-stretch hold plan for the next two hours: the first stretch
 * holds when the event reaches it, later stretches advance by the hold
 * length (the documented rolling-hold model, kept verbatim from the
 * accepted advisory).
 */
export function stretchHolds(event: CorridorEvent, lang: string): StretchHold[] {
	const minutes = holdMinutes(event);
	return event.routeSegmentIds.slice(0, 6).map((segmentId, i) => ({
		segmentId,
		minutes,
		clock: clockAt(i * minutes * 60, lang),
		state: i === 0 ? 'now' : i === 1 ? 'next' : 'later',
	}));
}

/** Plain-language strip tier for the citizen band strip (never the code). */
export type StripTier = 'calm' | 'watch' | 'hold' | 'severe';

/**
 * Citizens never see GREEN/AMBER/RED/BLACK — the ops band maps onto
 * four plain-language postures. No band / GREEN ⇒ calm; AMBER ⇒ watch;
 * RED ⇒ hold; BLACK maps to "priority corridor traffic" wording, never
 * the internal code.
 */
export function stripTier(band: string | null | undefined, anyActive: boolean): StripTier {
	if (!anyActive) return 'calm';
	switch (band) {
		case 'BLACK':
			return 'severe';
		case 'RED':
			return 'hold';
		case 'AMBER':
			return 'watch';
		default:
			return anyActive ? 'watch' : 'calm';
	}
}

/** Live events first; fixtures only as the labelled fallback (board ladder). */
export function advisoryEvents(events: CorridorEvent[]): CorridorEvent[] {
	const live = events.filter((e) => e.source !== 'offline-fixture');
	if (live.length) return live.filter((e) => e.state !== 'completed');
	const active = events.filter((e) => e.state !== 'completed');
	return active.length ? active : events.slice(0, 2);
}

export function isDemoEvent(event: CorridorEvent): boolean {
	return event.source === 'offline-fixture';
}

/**
 * Driver stretch options — live spine when the selected event reports one,
 * else the LABELLED fixture spine. Honest ladder for a flow that is
 * scripted-demo by design (SCRIPTED FALLBACK badge default-on): today's
 * live events API carries its route as nested `routes[].segment_ids`,
 * which the ops board's event mapping does not flatten, so live rows can
 * arrive spineless — the driver then falls back to the use-case library's
 * real graph segment ids, visibly badged as the demo spine.
 */
export function driverSpine(
	event: CorridorEvent | null,
): { segmentIds: number[]; source: 'live' | 'fixture' } {
	if (event && event.routeSegmentIds.length > 0) {
		return { segmentIds: event.routeSegmentIds, source: 'live' };
	}
	const fixture = USE_CASES.find((u) => u.routeSegmentIds.length > 0) ?? USE_CASES[0];
	return { segmentIds: fixture.routeSegmentIds, source: 'fixture' };
}
