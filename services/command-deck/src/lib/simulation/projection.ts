/**
 * Mercator projection utilities for converting between geographic (lon/lat)
 * and local planar (meter) coordinates used by the simulation.
 *
 * The projection is a transverse Mercator approximation centred on a reference
 * point.  For the New Delhi corridor the default reference is:
 *   lon = 77.2090, lat = 28.6139
 *
 * Error at this latitude is < 0.05 % over a 30 km corridor which is more than
 * acceptable for traffic micro-simulation.
 */

// WGS-84 semi-major axis (equatorial radius) in meters
const EARTH_RADIUS = 6_378_137.0;

/** Degrees to radians */
function deg2rad(deg: number): number {
	return (deg * Math.PI) / 180.0;
}

/** Radians to degrees */
function rad2deg(rad: number): number {
	return (rad * 180.0) / Math.PI;
}

// ─── Public API ─────────────────────────────────────────────────────────────

/**
 * Default reference point: centre of the New Delhi VVIP corridor.
 * All local-meter coordinates are relative to this origin.
 */
export const DEFAULT_REF_LON = 77.209;
export const DEFAULT_REF_LAT = 28.6139;

/**
 * Convert a geographic coordinate (lon, lat) in degrees to local Cartesian
 * coordinates in meters, relative to a reference origin.
 *
 * Uses an equirectangular (plate carree) approximation that accounts for
 * latitude-dependent longitude scaling.  This is equivalent to a local
 * tangent-plane (East-North-Up) projection and is very fast.
 *
 * @param lon  Longitude in degrees
 * @param lat  Latitude in degrees
 * @param refLon  Reference longitude (origin) in degrees
 * @param refLat  Reference latitude (origin) in degrees
 * @returns { x, y } in meters (x = east, y = north)
 */
export function lonLatToMeters(
	lon: number,
	lat: number,
	refLon: number = DEFAULT_REF_LON,
	refLat: number = DEFAULT_REF_LAT,
): { x: number; y: number } {
	const refLatRad = deg2rad(refLat);
	const cosRefLat = Math.cos(refLatRad);

	const x = EARTH_RADIUS * deg2rad(lon - refLon) * cosRefLat;
	const y = EARTH_RADIUS * deg2rad(lat - refLat);

	return { x, y };
}

/**
 * Convert local Cartesian coordinates in meters back to geographic (lon, lat)
 * in degrees, relative to a reference origin.
 *
 * Inverse of `lonLatToMeters`.
 *
 * @param x  Easting in meters from reference
 * @param y  Northing in meters from reference
 * @param refLon  Reference longitude (origin) in degrees
 * @param refLat  Reference latitude (origin) in degrees
 * @returns [lon, lat] in degrees
 */
export function metersToLonLat(
	x: number,
	y: number,
	refLon: number = DEFAULT_REF_LON,
	refLat: number = DEFAULT_REF_LAT,
): [number, number] {
	const refLatRad = deg2rad(refLat);
	const cosRefLat = Math.cos(refLatRad);

	const lon = refLon + rad2deg(x / (EARTH_RADIUS * cosRefLat));
	const lat = refLat + rad2deg(y / EARTH_RADIUS);

	return [lon, lat];
}

/**
 * Compute the centroid of a set of lon/lat points.
 * Useful for deriving the reference origin from a route geometry.
 */
export function computeCentroid(
	points: [number, number][],
): { lon: number; lat: number } {
	if (points.length === 0) {
		return { lon: DEFAULT_REF_LON, lat: DEFAULT_REF_LAT };
	}
	let sumLon = 0;
	let sumLat = 0;
	for (const [lon, lat] of points) {
		sumLon += lon;
		sumLat += lat;
	}
	return {
		lon: sumLon / points.length,
		lat: sumLat / points.length,
	};
}

/**
 * Project an entire polyline from lon/lat to local meters.
 *
 * @param geometry Array of [lon, lat] pairs
 * @param refLon  Reference longitude
 * @param refLat  Reference latitude
 * @returns Array of { x, y } in meters
 */
export function projectPolyline(
	geometry: [number, number][],
	refLon: number = DEFAULT_REF_LON,
	refLat: number = DEFAULT_REF_LAT,
): { x: number; y: number }[] {
	return geometry.map(([lon, lat]) => lonLatToMeters(lon, lat, refLon, refLat));
}

/**
 * Compute the total path length in meters of a projected polyline.
 */
export function polylineLength(points: { x: number; y: number }[]): number {
	let total = 0;
	for (let i = 1; i < points.length; i++) {
		const dx = points[i].x - points[i - 1].x;
		const dy = points[i].y - points[i - 1].y;
		total += Math.sqrt(dx * dx + dy * dy);
	}
	return total;
}
