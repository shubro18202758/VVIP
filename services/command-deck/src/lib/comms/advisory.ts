/**
 * Advisory writer — deterministic, trilingual, one fact base.
 *
 * Given an event's RESOLVED facts (comms/facts.ts for the operator desk,
 * the public projection for /citizen) the writer produces, per language
 * (en/hi/gu) and per audience (citizens / zone officers / 108 control /
 * municipal), the exact text that audience would receive: the SAME facts
 * (event kind, hold minutes, stretch, alternate, window clock) rendered
 * with per-audience consequence phrasing. Nothing is sent — the comms
 * desk shows every draft as DRAFTED-NOT-DELIVERED because no gateway
 * exists (by design, shown honestly), and drafts never leave the tab.
 *
 * Public-kind guard: a citizen-audience draft for an operator-only kind
 * (vehicle_pursuit, crime_incident, or any kind not on holds.ts
 * PUBLIC_EVENT_KINDS) is WITHHELD, never composed — the hold advisory AND
 * the all-clear: tactical police operations are not broadcast, not even
 * as "has cleared".
 *
 * Unreadable facts are not absent facts: when the hold length or the
 * alternate could not be READ (AdvisoryFacts.unavailable), the text says
 * "unavailable right now" / makes no alternate claim, instead of "not
 * published" / "no signed alternate".
 *
 * Pure: same facts + same wall-clock minute ⇒ same text on every call, so
 * the EN/HI/GU columns can be diffed for fact parity in the smoke test.
 */

import { SUPPORTED, kindLabel, localizeNumber } from '$lib/i18n/strings';
import { citizenT } from '$lib/i18n/strings.citizen';
import { clockAtMs, holdAnchorMs, isPublicKind, type HoldSource } from '$lib/citizen/holds';
import type { PublicCorridorEvent } from '$lib/citizen/public-model';

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

/**
 * The i18n key of a hold's provenance line — the oracle's own label kept
 * verbatim in meaning: a closure plan, a live adaptation, or the doctrine
 * baseline (never presented as live). Shared by /citizen and /comms.
 */
export function holdSourceKey(source: HoldSource | null): string {
	if (source === 'adaptive_params') return 'card_hold_from_adaptive';
	if (source === 'adaptive_baseline') return 'card_hold_from_baseline';
	return 'card_hold_from_policy';
}

/** The language-independent facts every rendering of an advisory shares. */
export interface AdvisoryFacts {
	eventId: string;
	eventKind: string;
	/** The first held stretch: signage name when the graph has one. */
	stretch: { segmentId: number | null; roadName: string | null };
	holdMinutes: number | null;
	holdSource: HoldSource | null;
	/** Alternate signage roads from the road graph; null = none offered. */
	alternate: string[] | null;
	/** When the first stretch holds (epoch ms, never before "now"). */
	anchorMs: number;
	/** Facts whose read failed this pass (absent = everything answered). */
	unavailable?: { hold: boolean; alternate: boolean };
	/**
	 * The event-emulator staged this event: every draft carries the
	 * SIMULATED FEED · EMULATED marker IN ITS TEXT, so a copied draft can
	 * never pass for a real road hold.
	 */
	emulated?: boolean;
}

export interface AdvisoryDraft {
	/** Key identifying the generation (event + audience). */
	key: string;
	lang: string;
	audience: Audience;
	text: string;
	/** True when the audience must not receive this kind (no text composed). */
	withheld: boolean;
	/** Facts shared by every language rendering — for parity checks. */
	facts: {
		kind: string;
		segment: number | null;
		road: string | null;
		minutes: number | null;
		alternate: string | null;
		anchorMs: number;
	};
}

/** Facts straight from the public projection (/citizen preview). */
export function factsFromPublicEvent(ev: PublicCorridorEvent, nowMs = Date.now()): AdvisoryFacts {
	const first = ev.stretches[0];
	return {
		eventId: ev.eventId,
		eventKind: ev.eventKind,
		stretch: { segmentId: first?.segmentId ?? null, roadName: first?.roadName ?? null },
		holdMinutes: ev.hold.minutes,
		holdSource: ev.hold.source,
		alternate: ev.alternate?.roadNames ?? null,
		anchorMs: holdAnchorMs(ev, nowMs),
		unavailable: { hold: ev.unavailable.hold, alternate: ev.unavailable.alternate },
		emulated: ev.source === 'emulated',
	};
}

/** The stretch as an audience reads it: signage name, else the localized id. */
function stretchLabel(facts: AdvisoryFacts, lang: string): string {
	if (facts.stretch.roadName) return facts.stretch.roadName;
	return facts.stretch.segmentId == null ? '—' : localizeNumber(facts.stretch.segmentId, lang);
}

/**
 * Compose one advisory. `lang` selects the rendering; the facts (localized
 * numbers per script, identical stretch/alternate) are the same across
 * languages so the side-by-side columns differ only in wording.
 */
export function composeAdvisory(facts: AdvisoryFacts, audience: Audience, lang: string): AdvisoryDraft {
	const kind = kindLabel(lang, facts.eventKind);
	const clock = clockAtMs(facts.anchorMs, lang);
	const alt = facts.alternate?.length ? facts.alternate.join(' / ') : null;
	const withheld = audience === 'citizens' && !isPublicKind(facts.eventKind);

	let text: string;
	if (withheld) {
		text = citizenT(lang, 'comms_withheld');
	} else if (facts.holdMinutes == null) {
		text = `${kind}: ${citizenT(lang, facts.unavailable?.hold ? 'card_hold_unavailable' : 'card_hold_unknown')}`;
	} else {
		const minutes = localizeNumber(facts.holdMinutes, lang);
		const seg = stretchLabel(facts, lang);
		switch (audience) {
			case 'citizens':
				text = alt
					? citizenT(lang, 'adv_citizen_hold', kind, minutes, clock, alt)
					: facts.unavailable?.alternate
						? citizenT(lang, 'adv_citizen_hold_altunknown', kind, minutes, clock)
						: citizenT(lang, 'adv_citizen_hold_noalt', kind, minutes, clock);
				break;
			case 'zones':
				text = citizenT(lang, 'adv_zone', kind, seg, minutes);
				break;
			case 'ambulance108':
				text = citizenT(lang, 'adv_108', kind, seg);
				break;
			case 'municipal':
				text = citizenT(lang, 'adv_municipal', kind, seg, minutes);
				break;
		}
	}

	if (facts.emulated && !withheld) text = `${citizenT(lang, 'adv_emulated_prefix')} ${text}`;

	return {
		key: `${facts.eventId}:${audience}`,
		lang,
		audience,
		text,
		withheld,
		facts: {
			kind: facts.eventKind,
			segment: facts.stretch.segmentId,
			road: facts.stretch.roadName,
			minutes: facts.holdMinutes,
			alternate: alt,
			anchorMs: facts.anchorMs,
		},
	};
}

/** All three languages for one audience (the desk's side-by-side columns). */
export function composeAdvisoryTrilingual(facts: AdvisoryFacts, audience: Audience): AdvisoryDraft[] {
	return SUPPORTED.map((s) => composeAdvisory(facts, audience, s.code));
}

/**
 * The closing all-clear (storyboard's final beat) in one language — a
 * CITIZEN draft, so an operator-only kind gets the withheld notice.
 */
export function composeAllClear(eventKind: string, lang: string, emulated = false): string {
	if (!isPublicKind(eventKind)) return citizenT(lang, 'comms_withheld');
	const text = citizenT(lang, 'adv_citizen_clear', kindLabel(lang, eventKind));
	return emulated ? `${citizenT(lang, 'adv_emulated_prefix')} ${text}` : text;
}

/** The all-clear in every supported language (en · hi · gu). */
export function composeAllClearTrilingual(eventKind: string, emulated = false): Array<{ lang: string; text: string }> {
	return SUPPORTED.map((s) => ({ lang: s.code, text: composeAllClear(eventKind, s.code, emulated) }));
}

/**
 * Template library by event kind — the desk's per-kind starting copy for
 * the citizen audience, rendered on illustrative facts (one stretch, the
 * 3-minute rolling hold the template library documents). Operator-only
 * kinds have no citizen template: they render the withheld notice.
 */
export function templateForKind(kind: string, lang: string, nowMs = Date.now()): string {
	return composeAdvisory(
		{
			eventId: `template:${kind}`,
			eventKind: kind,
			stretch: { segmentId: 1, roadName: null },
			holdMinutes: 3,
			holdSource: 'closure_policy',
			alternate: null,
			anchorMs: nowMs,
		},
		'citizens',
		lang,
	).text;
}
