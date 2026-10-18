/**
 * Advisory writer — deterministic, trilingual, one fact base.
 *
 * Given a CorridorEvent the writer produces, per language (en/hi/gu) and
 * per audience (citizens / zone officers / 108 control / municipal), the
 * exact text that audience would receive: the SAME facts (event kind,
 * minutes, stretch, alternate, window clock) rendered with per-audience
 * consequence phrasing. Nothing is sent — the comms desk logs every draft
 * as DRAFTED-NOT-DELIVERED because no gateway exists (by design, shown
 * honestly).
 *
 * Pure: same event + same wall-clock minute ⇒ same text on every call, so
 * the EN/HI/GU columns can be diffed for fact parity in the smoke test.
 */

import type { CorridorEvent } from '$lib/ops/types';
import { SUPPORTED, localizeNumber, t } from '$lib/i18n/strings';
import { citizenT } from '$lib/i18n/strings.citizen';
import { ALTERNATE_ROADS, clockAt, holdMinutes } from '$lib/citizen/holds';

export type Audience = 'citizens' | 'zones' | 'ambulance108' | 'municipal';

export const AUDIENCES: Audience[] = ['citizens', 'zones', 'ambulance108', 'municipal'];

export function audienceLabel(audience: Audience, lang: string): string {
	switch (audience) {
		case 'citizens':
			return citizenT(lang, 'audience_citizens');
		case 'zones':
			return citizenT(lang, 'audience_zones');
		case 'ambulance108':
			return citizenT(lang, 'audience_108');
		case 'municipal':
			return citizenT(lang, 'audience_municipal');
	}
}

export interface AdvisoryDraft {
	/** ISO-ish key identifying the generation (event + audience). */
	key: string;
	lang: string;
	audience: Audience;
	text: string;
	/** Facts shared by every language rendering — for parity checks. */
	facts: {
		kind: string;
		segment: number;
		minutes: string;
		alternate: string;
		clock: string;
	};
}

function kindLabel(event: CorridorEvent, lang: string): string {
	return t(lang, `kind_${event.eventKind}`);
}

/**
 * Compose one advisory. `lang` selects the rendering; the facts (localized
 * numbers per script, identical segment/alternate) are the same across
 * languages so the side-by-side columns differ only in wording.
 */
export function composeAdvisory(event: CorridorEvent, audience: Audience, lang: string): AdvisoryDraft {
	const minutes = Math.max(1, holdMinutes(event));
	const seg = event.routeSegmentIds[0] ?? 0;
	const clock = clockAt(0, lang);
	const kind = kindLabel(event, lang);
	const minutesLoc = localizeNumber(minutes, lang);

	let text: string;
	switch (audience) {
		case 'citizens':
			text = citizenT(lang, 'adv_citizen_hold', kind, minutesLoc, clock, ALTERNATE_ROADS);
			break;
		case 'zones':
			text = citizenT(lang, 'adv_zone', kind, localizeNumber(seg, lang), minutesLoc);
			break;
		case 'ambulance108':
			text = citizenT(lang, 'adv_108', kind, localizeNumber(seg, lang));
			break;
		case 'municipal':
			text = citizenT(
				lang,
				'adv_municipal',
				kind,
				localizeNumber(seg, lang),
				minutesLoc,
			);
			break;
	}

	return {
		key: `${event.eventId}:${audience}`,
		lang,
		audience,
		text,
		facts: { kind: event.eventKind, segment: seg, minutes: String(minutes), alternate: ALTERNATE_ROADS, clock },
	};
}

/** All three languages for one audience (the desk's side-by-side columns). */
export function composeAdvisoryTrilingual(event: CorridorEvent, audience: Audience): AdvisoryDraft[] {
	return SUPPORTED.map((s) => composeAdvisory(event, audience, s.code));
}

/** The closing all-clear (storyboard's final beat), trilingual. */
export function composeAllClear(event: CorridorEvent, lang: string): string {
	return citizenT(lang, 'adv_citizen_clear', kindLabel(event, lang));
}

/**
 * Template library by event kind — the desk's per-kind starting copy for
 * the citizen audience. Same derivation as composeAdvisory so opening a
 * template and generating produce the same text.
 */
export function templateForKind(kind: string, lang: string): string {
	const fixture: CorridorEvent = {
		eventId: `template:${kind}`,
		name: kind,
		eventKind: kind as CorridorEvent['eventKind'],
		protectionClass: 'none',
		crowdClass: 'LOW',
		state: 'active',
		routeSegmentIds: [1],
		phases: [{ label: 'active', startTime: null, endTime: null, closurePolicy: { advanceClosureSec: 180, closureType: 'partial_closure' }, state: 'active' }],
		currentPhase: 'active',
		band: null,
		windowStart: null,
		windowEnd: null,
		description: null,
		source: 'offline-fixture',
	};
	return composeAdvisory(fixture, 'citizens', lang).text;
}
