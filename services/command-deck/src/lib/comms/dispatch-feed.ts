/**
 * Dispatch feed — the comms desk's read path into the inter-agency
 * dispatch lifecycle (units/router.py): GET /api/v1/dispatches and
 * GET /api/v1/units, both read through the deck-origin proxy prefix
 * built from the frozen ORACLE_V1 constant (endpoints.ts security
 * contract: fixed constants only; no caller-supplied URLs anywhere).
 *
 * Degradation is honest per the deck ladder: 404 ⇒ ApiNotDeployed
 * (feed shows its not-deployed chip), network/timeout ⇒ ApiDown (feed
 * shows the labelled fallback). No writes live here — dispatch
 * decisions/status are user-commit actions owned by the ops stream.
 */

import { ORACLE_V1, UNITS_API } from '$lib/ops/endpoints';
import { ApiDown, ApiNotDeployed } from '$lib/ops/client';

/** The closed agency registry mirrored from units/agencies.py (migration 011). */
export interface AgencyInfo {
	code: string;
	label: string;
	label_native: string;
	role: string;
}

export const AGENCIES: AgencyInfo[] = [
	{
		code: 'jan_rakshak',
		label: 'Jan Rakshak',
		label_native: 'જન રક્ષક',
		role: 'citizen volunteer coordination, first responder support',
	},
	{ code: 'police', label: 'Police Control', label_native: 'પોલીસ કંટ્રોલ', role: 'law & order' },
	{ code: 'traffic', label: 'Traffic Police', label_native: 'ટ્રાફિક પોલીસ', role: 'traffic management' },
	{ code: 'ambulance', label: '108 Ambulance', label_native: '૧૦૮ એમ્બ્યુલન્સ', role: 'emergency medical' },
	{ code: 'fire', label: 'Fire & Emergency', label_native: 'ફાયર એન્ડ ઇમરજન્સી', role: 'fire & rescue' },
];

export function agencyByCode(code: string): AgencyInfo {
	return AGENCIES.find((a) => a.code === code) ?? { code, label: code, label_native: code, role: '' };
}

/** Dispatch statuses the lifecycle walks (CAD grammar, units/router.py:83). */
export type DispatchStatus =
	| 'requested'
	| 'approved'
	| 'declined'
	| 'en_route'
	| 'on_scene'
	| 'standing_down'
	| (string & {});

export interface DispatchAuditStep {
	from_status: string | null;
	to_status: string;
	actor: string;
	note: string | null;
	at: string;
}

export interface DispatchRecord {
	dispatch_id: string;
	event_id: string;
	agency: string;
	agency_label: string;
	unit_id: string;
	waypoint_junction: number | null;
	status: DispatchStatus;
	requested_by: string;
	decided_by: string | null;
	decision_note: string | null;
	approved_at: string | null;
	created_at: string | null;
	updated_at: string | null;
	audit?: DispatchAuditStep[];
}

export interface DispatchFeedState {
	dispatches: DispatchRecord[];
	source: 'live' | 'unavailable' | 'not-deployed';
	note: string | null;
	lastUpdated: number;
}

async function getJson(url: string, timeoutMs = 8000): Promise<unknown> {
	const ctrl = new AbortController();
	const timer = setTimeout(() => ctrl.abort(), timeoutMs);
	try {
		const res = await fetch(url, { signal: ctrl.signal });
		if (res.status === 404) {
			throw new ApiNotDeployed(404, `endpoint not deployed: ${url}`);
		}
		if (!res.ok) {
			const body = await res.text().catch(() => '');
			throw new ApiDown(`${res.status} ${res.statusText}: ${body.slice(0, 160)}`);
		}
		return await res.json();
	} catch (err) {
		if (err instanceof ApiNotDeployed || err instanceof ApiDown) throw err;
		if (err instanceof DOMException && err.name === 'AbortError') {
			throw new ApiDown(`timeout: ${url}`);
		}
		throw new ApiDown(`${url}: ${err instanceof Error ? err.message : String(err)}`);
	} finally {
		clearTimeout(timer);
	}
}

/**
 * GET /api/v1/dispatches — the inter-agency dispatch list with per-dispatch
 * audit tails. Single fetch, honest degradation; the caller decides cadence.
 */
export async function fetchDispatches(): Promise<DispatchRecord[]> {
	const data = (await getJson(`${ORACLE_V1}/dispatches?limit=50`)) as {
		dispatches?: DispatchRecord[];
	};
	return Array.isArray(data?.dispatches) ? data.dispatches : [];
}

/**
 * GET /api/v1/units — emulated agency units (deterministic projections from
 * approved dispatches; every unit is emulated by design and carries that
 * source label).
 */
export async function fetchAgencyUnits(): Promise<Array<Record<string, unknown>>> {
	const data = (await getJson(UNITS_API)) as { units?: Array<Record<string, unknown>> };
	return Array.isArray(data?.units) ? data.units : [];
}
