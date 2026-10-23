/**
 * GET /api/public/dispatches/{dispatch_id}?event={event_id} — the /driver
 * page following its OWN green-corridor request: status, approval time and
 * the bound (emulated) unit. Ambulance dispatches only; no officer names,
 * notes or audit tails (see $lib/citizen/public-api.server.ts).
 */
import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';
import { publicDispatchStatus } from '$lib/citizen/public-api.server';

export const GET: RequestHandler = async ({ fetch, params, url }) => {
	const { status, body } = await publicDispatchStatus(
		fetch,
		params.id,
		url.searchParams.get('event') ?? '',
	);
	return json(body, { status, headers: { 'cache-control': 'no-store' } });
};
