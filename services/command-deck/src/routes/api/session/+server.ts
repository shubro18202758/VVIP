/**
 * /api/session — the opt-in operator gate's sign-in (VEER_OPERATOR_PIN).
 *
 *   GET    → {gate, officer, expires_at}   (what the shell's sign-in shows)
 *   POST   {officer, pin} → sets the HttpOnly, SameSite=Strict signed
 *          session cookie; 401 on a wrong name/PIN, 429 when throttled,
 *          409 while the gate is off (nothing to sign in to)
 *   DELETE → revokes the session on the server (its token id) and clears
 *            the cookie (sign out) — a copied cookie stops working too
 *
 * Secure flag: set VEER_SESSION_SECURE=1 when the deck is served over TLS.
 * It cannot be inferred — adapter-node reports https: for every request
 * unless ORIGIN is set — and on the plain-http venue LAN a Secure cookie
 * would never come back (the operator could never sign in). Signing and
 * throttling live in $lib/shell/operator-session.server.ts.
 */
import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';
import {
	SESSION_COOKIE,
	SESSION_TTL_SEC,
	checkPin,
	clearFailures,
	gateEnabled,
	issueToken,
	normaliseOfficer,
	noteFailure,
	revokeFromCookieHeader,
	sessionFromCookieHeader,
	throttled,
} from '$lib/shell/operator-session.server';

const NO_STORE = { 'cache-control': 'no-store' };

/** Explicit deployment switch (see header): TLS in front ⇒ Secure cookie. */
function secureCookie(): boolean {
	return process.env.VEER_SESSION_SECURE === '1';
}

export const GET: RequestHandler = ({ request }) => {
	const session = gateEnabled() ? sessionFromCookieHeader(request.headers.get('cookie')) : null;
	return json(
		{
			gate: gateEnabled(),
			officer: session?.officer ?? null,
			expires_at: session ? new Date(session.expiresAt * 1000).toISOString() : null,
		},
		{ headers: NO_STORE },
	);
};

export const POST: RequestHandler = async ({ request, cookies, getClientAddress }) => {
	if (!gateEnabled()) {
		return json({ detail: 'operator gate is off (VEER_OPERATOR_PIN unset)', gate: false }, { status: 409, headers: NO_STORE });
	}
	const client = getClientAddress();
	if (throttled(client)) {
		return json({ detail: 'too many sign-in attempts — wait a few minutes' }, { status: 429, headers: NO_STORE });
	}
	const body = (await request.json().catch(() => null)) as { officer?: unknown; pin?: unknown } | null;
	const officer = normaliseOfficer(body?.officer);
	if (!officer || !checkPin(body?.pin)) {
		noteFailure(client);
		return json({ detail: 'sign-in refused' }, { status: 401, headers: NO_STORE });
	}
	clearFailures(client);
	const { token, expiresAt } = issueToken(officer);
	cookies.set(SESSION_COOKIE, token, {
		path: '/',
		httpOnly: true,
		sameSite: 'strict',
		secure: secureCookie(),
		maxAge: SESSION_TTL_SEC,
	});
	return json(
		{ gate: true, officer, expires_at: new Date(expiresAt * 1000).toISOString() },
		{ headers: NO_STORE },
	);
};

export const DELETE: RequestHandler = ({ cookies, request }) => {
	revokeFromCookieHeader(request.headers.get('cookie'));
	cookies.delete(SESSION_COOKIE, { path: '/', secure: secureCookie() });
	return json({ gate: gateEnabled(), officer: null, expires_at: null }, { headers: NO_STORE });
};
