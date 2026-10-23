/**
 * Agency colours — one key for every layer that draws an agency (unit
 * markers, dispatch lines), read from the design tokens at runtime
 * (theme.ts; no hex literals):
 *
 *   jan_rakshak  act orange   citizen volunteers — coordinated, never automated
 *   police       data accent  law & order
 *   traffic      tool accent  traffic management
 *   ambulance    ink-1        108 emergency medical (the white of the vehicle)
 *   fire         act-2        fire & rescue
 *   other        ink-3
 */

import { cssColor, cssVar } from '../theme';

const AGENCY_TOKEN: Record<string, string> = {
	jan_rakshak: '--v-act',
	police: '--v-data',
	traffic: '--v-tool',
	ambulance: '--v-ink-1',
	fire: '--v-act-2',
};

/** The token name for an agency (legend + CSS surfaces). */
export function agencyToken(agency: string | null | undefined): string {
	return AGENCY_TOKEN[(agency ?? '').toLowerCase()] ?? '--v-ink-3';
}

/**
 * Text ink per agency where the fill token is too dark to read as 12 px text
 * on the dark canvas (act orange → act-text); the fill token otherwise.
 */
const AGENCY_TEXT_TOKEN: Record<string, string> = {
	jan_rakshak: '--v-act-text',
};

/** The token an agency's LABEL text uses (legible on the canvas plate). */
export function agencyTextToken(agency: string | null | undefined): string {
	return AGENCY_TEXT_TOKEN[(agency ?? '').toLowerCase()] ?? agencyToken(agency);
}

/** Cesium colour for an agency's label text (token value, Cesium fallback). */
export function agencyTextColor(agency: string | null | undefined, cesium: CesiumGlobal): unknown {
	const fallback = (agency ?? '').toLowerCase() === 'jan_rakshak' ? cesium.Color.ORANGE : cesium.Color.CYAN;
	return cssColor(cesium, agencyTextToken(agency), fallback);
}

/** Cesium colour for an agency (token value, Cesium fallback). */
export function agencyColor(agency: string | null | undefined, cesium: CesiumGlobal): unknown {
	const fallback = (agency ?? '').toLowerCase() === 'jan_rakshak' ? cesium.Color.ORANGE : cesium.Color.CYAN;
	return cssColor(cesium, agencyToken(agency), fallback);
}

/** CSS colour string for an agency (null before the design shell mounts). */
export function agencyCss(agency: string | null | undefined): string | null {
	return cssVar(agencyToken(agency));
}

/** Human agency label (the closed registry, migration 011). */
export function agencyLabel(agency: string | null | undefined): string {
	switch ((agency ?? '').toLowerCase()) {
		case 'jan_rakshak':
			return 'Jan Rakshak';
		case 'police':
			return 'Police';
		case 'traffic':
			return 'Traffic Police';
		case 'ambulance':
			return '108 Ambulance';
		case 'fire':
			return 'Fire & Emergency';
		default:
			return agency ?? 'agency';
	}
}

/**
 * ONE partition of a unit's lifecycle for every count on the surface — the
 * vitals rail, the UNITS chip hint and the tour all read it, so a unit is
 * never counted twice: arrived (or status on_scene) → on scene; otherwise
 * status en_route → en route; anything else (patrol, available, approved,
 * returning, …) → neither.
 */
export function unitPhase(u: { status: string | null; arrived?: boolean | null }): 'on_scene' | 'en_route' | 'other' {
	if (u.arrived === true || u.status === 'on_scene') return 'on_scene';
	if (u.status === 'en_route') return 'en_route';
	return 'other';
}
