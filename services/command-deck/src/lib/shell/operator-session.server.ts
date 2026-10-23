/**
 * Opt-in operator gate (server-only) — VEER_OPERATOR_PIN.
 *
 * OFF unless VEER_OPERATOR_PIN is set to a non-empty value in the deck's
 * environment (then every behaviour below is inert and the deck works as
 * before). When ON:
 *
 *   POST /api/session {officer, pin}  → HttpOnly, SameSite=Strict cookie
 *        `veer_operator` = base64url(payload) "." base64url(HMAC-SHA256),
 *        payload {o: officer, exp: epoch-sec, jti: random id}, 12 h
 *        lifetime. DELETE /api/session (sign out) revokes the token's jti
 *        on the server, so a copied cookie stops working at sign-out — not
 *        only in the browser that signed out. The revocation set lives in
 *        this deck process (bounded, pruned at each token's own expiry):
 *        a deck restart forgets it, so rotate VEER_SESSION_SECRET to end
 *        every session at once.
 *   hooks.server.ts → gateDecision(): a request WITHOUT a valid cookie is
 *        DEFAULT-DENIED (403) on every proxied prefix (/api/v1,
 *        /api/traffic, /api/convoy, /api/emulator). Exactly two things pass
 *        anonymously:
 *          - PUBLIC_READS: the road geometry the public maps draw and the
 *            three liveness probes — nothing that names a plate, an
 *            e-challan, a dispatch, an event or a movement;
 *          - the 108 driver's POST /api/v1/events/{uuid}/dispatch (it only
 *            creates a 'requested' row a named officer must still approve;
 *            the hook additionally requires agency 'ambulance' and caps
 *            each client at PUBLIC_DISPATCH_MAX requests per window).
 *        The public pages' advisory feed is the deck's own server-side
 *        projection (/api/public/*, a sub-request), so the raw events feed
 *        — pursuit plate, FIR, officer, the ids that unlock an
 *        interception plan — never needs to be anonymous.
 *
 * The decision is taken on the CANONICAL path (canonicalGatePath): the
 * upstreams (uvicorn) percent-decode the path once before routing, so the
 * gate must judge that decoded string — never the raw pathname, where
 * `/api/v1/%76ehicles` would not look like `/api/v1/vehicles` yet route to
 * it upstream.
 *
 * Key: VEER_SESSION_SECRET when set, else 32 random bytes per process —
 * sessions then end when the deck restarts (stated, not hidden). PIN
 * attempts are throttled per client address (8 failures per 5 minutes) so
 * a short PIN cannot be walked. Comparisons are constant-time. Officer
 * names refuse control characters, the bidirectional override / isolate
 * controls (U+202A–U+202E, U+2066–U+2069) and the LRM / RLM marks
 * (U+200E / U+200F, which the brain refuses on escort writes) — a name
 * that renders reversed in the audit trail is not a name; ZWJ / ZWNJ stay
 * legal for Indic names.
 */

import { createHash, createHmac, randomBytes, timingSafeEqual } from 'node:crypto';

export const SESSION_COOKIE = 'veer_operator';
export const SESSION_TTL_SEC = 12 * 3600;

const MAX_FAILURES = 8;
const FAILURE_WINDOW_MS = 5 * 60_000;

/** The configured PIN, or null when the gate is off. Read per call (env may change in tests). */
export function operatorPin(): string | null {
	const pin = process.env.VEER_OPERATOR_PIN?.trim();
	return pin ? pin : null;
}

export function gateEnabled(): boolean {
	return operatorPin() !== null;
}

let processSecret: Buffer | null = null;

function secret(): Buffer {
	const fromEnv = process.env.VEER_SESSION_SECRET?.trim();
	if (fromEnv) return Buffer.from(fromEnv, 'utf8');
	processSecret ??= randomBytes(32);
	return processSecret;
}

function sign(payload: string): string {
	return createHmac('sha256', secret()).update(payload).digest('base64url');
}

/** Bidirectional override / isolate controls and LRM / RLM (never part of a name). */
const BIDI_CONTROLS = /[\u200e\u200f\u202a-\u202e\u2066-\u2069]/;

/** Officer names as the audit trail records them: trimmed, 2–64 printable chars, no bidi controls. */
export function normaliseOfficer(value: unknown): string | null {
	if (typeof value !== 'string') return null;
	const name = value.replace(/\s+/g, ' ').trim();
	// eslint-disable-next-line no-control-regex
	if (name.length < 2 || name.length > 64 || /[\u0000-\u001f\u007f]/.test(name)) return null;
	if (BIDI_CONTROLS.test(name)) return null;
	return name;
}

/** Constant-time PIN check (digests make the lengths equal). */
export function checkPin(pin: unknown): boolean {
	const expected = operatorPin();
	if (expected === null || typeof pin !== 'string') return false;
	const a = createHash('sha256').update(pin).digest();
	const b = createHash('sha256').update(expected).digest();
	return timingSafeEqual(a, b);
}

export function issueToken(officer: string, nowSec = Math.floor(Date.now() / 1000)): { token: string; expiresAt: number } {
	const expiresAt = nowSec + SESSION_TTL_SEC;
	const jti = randomBytes(12).toString('base64url');
	const payload = Buffer.from(JSON.stringify({ o: officer, exp: expiresAt, jti }), 'utf8').toString('base64url');
	return { token: `${payload}.${sign(payload)}`, expiresAt };
}

// ─── Revocation (sign-out ends the token, not just the cookie) ──────────────

/** Revoked token ids → their own expiry (epoch-sec); pruned once expired. */
const revoked = new Map<string, number>();
const REVOKED_MAX = 10_000;

function pruneRevoked(nowSec: number): void {
	for (const [jti, exp] of revoked) if (exp <= nowSec) revoked.delete(jti);
}

/**
 * Revoke the session a cookie value proves (sign-out). Only a VALID token is
 * revoked (a forged one has nothing to end); returns whether one was. The
 * set is bounded: past REVOKED_MAX live entries the oldest-expiring go first.
 */
export function revokeToken(token: string | null | undefined, nowSec = Math.floor(Date.now() / 1000)): boolean {
	const session = verifyToken(token, nowSec);
	if (!session?.jti) return false;
	pruneRevoked(nowSec);
	revoked.set(session.jti, session.expiresAt);
	if (revoked.size > REVOKED_MAX) {
		const oldest = [...revoked.entries()].sort((a, b) => a[1] - b[1]).slice(0, revoked.size - REVOKED_MAX);
		for (const [jti] of oldest) revoked.delete(jti);
	}
	return true;
}

/** Revoke whichever valid session a Cookie header carries (sign-out). */
export function revokeFromCookieHeader(header: string | null): boolean {
	if (!header) return false;
	let any = false;
	for (const part of header.split(';')) {
		const eq = part.indexOf('=');
		if (eq <= 0 || part.slice(0, eq).trim() !== SESSION_COOKIE) continue;
		try {
			any = revokeToken(decodeURIComponent(part.slice(eq + 1).trim())) || any;
		} catch {
			// malformed escape — not ours
		}
	}
	return any;
}

/** Test hook: forget every revocation. */
export function resetRevocations(): void {
	revoked.clear();
}

/** The session a cookie value proves, or null (bad signature, expired, revoked, malformed). */
export function verifyToken(
	token: string | null | undefined,
	nowSec = Math.floor(Date.now() / 1000),
): { officer: string; expiresAt: number; jti: string | null } | null {
	if (!token) return null;
	const dot = token.indexOf('.');
	if (dot <= 0 || dot === token.length - 1) return null;
	const payload = token.slice(0, dot);
	// Compare the canonical base64url STRINGS, not decoded bytes: a 32-byte
	// MAC's last base64 character carries 2 padding bits, so decoding would
	// accept up to four spellings of one signature.
	const given = Buffer.from(token.slice(dot + 1), 'utf8');
	const want = Buffer.from(sign(payload), 'utf8');
	if (given.length !== want.length || !timingSafeEqual(given, want)) return null;
	try {
		const doc = JSON.parse(Buffer.from(payload, 'base64url').toString('utf8')) as { o?: unknown; exp?: unknown; jti?: unknown };
		const officer = normaliseOfficer(doc.o);
		if (!officer || typeof doc.exp !== 'number' || doc.exp <= nowSec) return null;
		const jti = typeof doc.jti === 'string' && doc.jti ? doc.jti : null;
		if (jti && revoked.has(jti)) return null;
		return { officer, expiresAt: doc.exp, jti };
	} catch {
		return null;
	}
}

/**
 * The one cookie the gate reads, parsed straight off the request header.
 * Cookies are shared by every port on a host, so a same-named cookie from
 * another app (or a malformed escape) must neither throw nor shadow a
 * valid session: each candidate is tried, the first verified one wins.
 */
export function sessionFromCookieHeader(header: string | null): { officer: string; expiresAt: number; jti: string | null } | null {
	if (!header) return null;
	for (const part of header.split(';')) {
		const eq = part.indexOf('=');
		if (eq <= 0 || part.slice(0, eq).trim() !== SESSION_COOKIE) continue;
		let value: string;
		try {
			value = decodeURIComponent(part.slice(eq + 1).trim());
		} catch {
			continue; // malformed escape — not ours, never a 500
		}
		const session = verifyToken(value);
		if (session) return session;
	}
	return null;
}

// ─── PIN throttle (per client address) ───────────────────────────────────────

const failures = new Map<string, number[]>();

function recent(key: string, now: number): number[] {
	const list = (failures.get(key) ?? []).filter((t) => now - t < FAILURE_WINDOW_MS);
	if (list.length) failures.set(key, list);
	else failures.delete(key);
	return list;
}

export function throttled(key: string, now = Date.now()): boolean {
	return recent(key, now).length >= MAX_FAILURES;
}

export function noteFailure(key: string, now = Date.now()): void {
	failures.set(key, [...recent(key, now), now]);
}

export function clearFailures(key: string): void {
	failures.delete(key);
}

// ─── Anonymous 108 requests (per client address) ─────────────────────────────

/** Anonymous ambulance requests one client may file per window. */
export const PUBLIC_DISPATCH_MAX = 6;
const PUBLIC_DISPATCH_WINDOW_MS = 10 * 60_000;

const publicDispatches = new Map<string, number[]>();

/**
 * Records one anonymous dispatch request for `key` and says whether it
 * may proceed: every allowed request is a 'requested' row on the duty
 * officer's wall, so a client gets PUBLIC_DISPATCH_MAX per 10 minutes —
 * plenty for an ambulance crew, too few to flood the wall.
 */
export function takePublicDispatch(key: string, now = Date.now()): boolean {
	const list = (publicDispatches.get(key) ?? []).filter((t) => now - t < PUBLIC_DISPATCH_WINDOW_MS);
	if (list.length >= PUBLIC_DISPATCH_MAX) {
		publicDispatches.set(key, list);
		return false;
	}
	list.push(now);
	publicDispatches.set(key, list);
	return true;
}

/** Test hook: forget every recorded anonymous request. */
export function resetPublicDispatches(): void {
	publicDispatches.clear();
}

// ─── Gate decision ───────────────────────────────────────────────────────────

const GATED_PREFIXES = ['/api/v1', '/api/traffic', '/api/convoy', '/api/emulator'];

/**
 * The ONLY proxied reads (GET/HEAD/OPTIONS) an anonymous browser may make
 * while the gate is on. An allowlist on purpose: an operator read added
 * to the oracle, brain or emulator later is gated the day it ships (a
 * deny list fails open — it once missed /events/{id}/interception-plan,
 * /movements and /api/convoy/movements/{id}/protocol).
 */
const PUBLIC_READS: readonly RegExp[] = [
	/^\/api\/v1\/spatial\/segments$/, // road geometry the public maps draw
	/^\/api\/v1\/health$/, // liveness probes only — no data rows
	/^\/api\/convoy\/health$/,
	/^\/api\/emulator\/health$/,
];

/** The 108 driver's request — it only creates a 'requested' dispatch row. */
export const PUBLIC_DISPATCH_RE = /^\/api\/v1\/events\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\/dispatch$/i;

/**
 * The path the upstream will actually route on, or null when an anonymous
 * caller has no business sending it. uvicorn percent-decodes the request
 * path exactly once before Starlette matches a route, so the gate decodes
 * once too and judges THAT string. Refused (null): malformed escapes, a
 * '%' still present after one decode (double encoding), backslashes,
 * control characters and '.'/'..' segments (WHATWG URL parsing already
 * resolved the literal ones; decoding %2F can mint new ones). Repeated
 * slashes collapse.
 */
export function canonicalGatePath(pathname: string): string | null {
	let decoded: string;
	try {
		decoded = decodeURIComponent(pathname);
	} catch {
		return null;
	}
	// eslint-disable-next-line no-control-regex
	if (/[%\\\u0000-\u001f\u007f]/.test(decoded)) return null;
	const collapsed = decoded.replace(/\/{2,}/g, '/');
	if (collapsed.split('/').some((seg) => seg === '.' || seg === '..')) return null;
	return collapsed;
}

function underGatedPrefix(path: string): boolean {
	return GATED_PREFIXES.some((p) => path === p || path.startsWith(`${p}/`));
}

/**
 * Whether an ANONYMOUS request (no valid session) may pass the gate.
 * Signed-in operators and a disabled gate always pass (callers check).
 * 'check-body' = the public dispatch request: allowed only for the
 * ambulance agency (the hook inspects the JSON body and the per-client
 * cap). Everything else on a gated prefix is denied.
 */
export function gateDecision(method: string, pathname: string): 'allow' | 'deny' | 'check-body' {
	const path = canonicalGatePath(pathname);
	if (path === null) return underGatedPrefix(pathname) ? 'deny' : 'allow';
	if (!underGatedPrefix(path) && !underGatedPrefix(pathname)) return 'allow';
	const m = method.toUpperCase();
	if (m === 'GET' || m === 'HEAD' || m === 'OPTIONS') {
		return PUBLIC_READS.some((re) => re.test(path)) ? 'allow' : 'deny';
	}
	if (m === 'POST' && PUBLIC_DISPATCH_RE.test(path)) return 'check-body';
	return 'deny';
}
