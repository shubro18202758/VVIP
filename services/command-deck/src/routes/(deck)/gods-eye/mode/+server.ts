/**
 * GET /gods-eye/mode — which basemap the canvas may use.
 *
 * The ion token is read SERVER-SIDE from the environment (VEER_CESIUM_ION_TOKEN
 * on the gods-eye-deck compose service) and never appears in source, examples
 * or tests (Mimosa credential rule). Cesium ion access tokens are client
 * credentials by design (they gate tile access, not account access), so the
 * same-origin page receives it only when the upgrade is active. Default
 * without the env: keyless OSM + ellipsoid terrain — zero cesium.com and
 * zero Google tile calls.
 *
 * Operator gate (VEER_OPERATOR_PIN, $lib/shell/operator-session.server.ts):
 * while the gate is on, the token goes only to a request carrying a valid
 * operator session — an anonymous LAN client gets the keyless basemap, so
 * the deck's ion quota is never handed to whoever can reach the venue LAN.
 *
 * Additive (contract C-L): a keyed answer names `tiles3d_asset_id`, the ion
 * asset the photoreal-3d layer streams (Google Photorealistic 3D Tiles).
 *
 * Every answer is `cache-control: no-store` and varies on the session
 * cookie: a keyed answer carries a credential, and the answer itself
 * depends on who asks — no cache (browser or proxy) may keep or share it.
 */

import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';
import { gateEnabled, sessionFromCookieHeader } from '$lib/shell/operator-session.server';
import { PHOTOREAL_3D_ASSET_ID } from '$lib/gods-eye/layers/basemap';

/** Never stored, never shared between sessions (the keyed answer is a credential). */
const PRIVATE = { headers: { 'cache-control': 'no-store', vary: 'cookie' } };

export const GET: RequestHandler = ({ request }) => {
	const token = process.env['VEER_CESIUM_ION_TOKEN']?.trim();
	if (token) {
		if (gateEnabled() && !sessionFromCookieHeader(request.headers.get('cookie'))) {
			return json(
				{
					mode: 'keyless-osm',
					note: 'ion upgrade withheld — sign in as the duty officer to load photoreal imagery (operator gate on)',
				},
				PRIVATE,
			);
		}
		return json(
			{
				mode: 'ion-photoreal',
				token,
				tiles3d_asset_id: PHOTOREAL_3D_ASSET_ID,
				note: 'env-keyed upgrade active — ion imagery + world terrain + Google Photorealistic 3D Tiles',
			},
			PRIVATE,
		);
	}
	return json(
		{
			mode: 'keyless-osm',
			note: 'OSM raster + ellipsoid terrain — set VEER_CESIUM_ION_TOKEN to upgrade (ion imagery, world terrain, photoreal 3D)',
		},
		PRIVATE,
	);
};
