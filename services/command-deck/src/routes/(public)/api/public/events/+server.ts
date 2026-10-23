/**
 * GET /api/public/events — the public advisory feed (/citizen, /driver).
 *
 * Public-kind events only, projected server-side (see
 * $lib/citizen/public-api.server.ts): no titles, metadata, protection
 * classes or operator feeds ever reach an anonymous browser. 404 / 503
 * pass through as {detail} so the page can label "not deployed" vs
 * "unreachable" honestly.
 *
 * Every answer carries `emulated_labelling: true` (contract C-M): this
 * build badges event-emulator events SIMULATED FEED · EMULATED and never
 * shows them as live. It is a property of the build, not of the data, so
 * it rides the error bodies too — `veer_up.sh --demo` reads it before it
 * lets the living city run.
 */
import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';
import { buildPublicFeed } from '$lib/citizen/public-api.server';

export const GET: RequestHandler = async ({ fetch }) => {
	const { status, body } = await buildPublicFeed(fetch);
	return json({ ...body, emulated_labelling: true }, { status, headers: { 'cache-control': 'no-store' } });
};
