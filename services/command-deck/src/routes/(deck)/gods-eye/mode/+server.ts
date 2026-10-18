/**
 * GET /gods-eye/mode — which basemap the canvas may use.
 *
 * The ion token is read SERVER-SIDE from the environment (VEER_CESIUM_ION_TOKEN
 * on the gods-eye-deck compose service) and never appears in source, examples
 * or tests (Mimosa credential rule). Cesium ion access tokens are client
 * credentials by design (they gate tile access, not account access), so the
 * same-origin page receives it only when the upgrade is active. Default
 * without the env: keyless OSM + ellipsoid terrain — zero cesium.com calls.
 */

import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';

export const GET: RequestHandler = () => {
	const token = process.env['VEER_CESIUM_ION_TOKEN'];
	if (token && token.trim().length > 0) {
		return json({
			mode: 'ion-photoreal',
			token: token.trim(),
			note: 'env-keyed upgrade active — ion imagery + world terrain',
		});
	}
	return json({
		mode: 'keyless-osm',
		note: 'OSM raster + ellipsoid terrain — set VEER_CESIUM_ION_TOKEN to upgrade',
	});
};
