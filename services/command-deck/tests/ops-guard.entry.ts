/**
 * Guard self-test entry — bundled with esbuild and executed by node (node
 * cannot import .ts directly; esbuild is vendored in node_modules as a vite
 * dependency). Run from the deck root (services/command-deck):
 *
 *   node_modules/.bin/esbuild tests/ops-guard.entry.ts --bundle \
 *     --platform=node --format=esm --alias:\$lib=./src/lib \
 *     --outfile=tests/artifacts/ops-guard-bundle.mjs \
 *     && node tests/artifacts/ops-guard-bundle.mjs
 *
 * (tests/test_deck_proxy_guard.py runs exactly this for the pytest ladder.)
 *
 * Asserts, hermetically (fetch is stubbed — no request ever leaves):
 *   1. the outbound-URL security contract ($lib/ops/endpoints): http/https
 *      only, loopback/private/reserved hosts rejected, strict path ids;
 *   2. the deck proxy's authority pinning (src/hooks.server.ts): paths
 *      that would override the upstream authority are refused 400 BEFORE
 *      fetch; ordinary paths proxy to their rule's pinned loopback target;
 *   3. edge hardening: WebSocket upgrades answered 501 at once (no fetch),
 *      hop-by-hop + cookie + spoofed officer headers never travel
 *      upstream, decoded-body headers never travel back, security headers
 *      and the nonce CSP on pages (strict on public routes), per-route
 *      theme/lang in the server render;
 *   4. the opt-in operator gate (VEER_OPERATOR_PIN): DEFAULT-DENY for
 *      anonymous callers on the canonical (once-decoded) path — %-encoded
 *      spellings of operator feeds, the interception plan, movements, the
 *      raw events feed all 403; only the road geometry, the liveness
 *      probes and the 108 ambulance request pass (the latter capped per
 *      client, 429); sub-requests pass; the /api/session sign-in issues a
 *      signed HttpOnly cookie that opens the gate (wrong PIN 401,
 *      tampering/expiry refused, malformed cookies never 500, throttle 429);
 *   5. the public projection ($lib/citizen/public-api.server): pursuits and
 *      crime incidents never projected, no titles/metadata/classes, the
 *      primary spine only, hold provenance as the oracle labels it
 *      (baseline never shown as live; params not asked when a closure plan
 *      decides), failed reads flagged `unavailable`, the graph alternate,
 *      the driver status cache + eta_at;
 *   6. the read-model and writer: doctrine window timing (arrival- and
 *      approval-anchored), only active events hold "now", operator-only
 *      kinds get no citizen all-clear, unreadable facts are not "none";
 *   7. the chat stream contract C-D ($lib/api/client): both the C-D and the
 *      legacy brain event shapes normalise to the same callbacks.
 */

import { assertSafeExternalUrl, safePathId } from '../src/lib/ops/endpoints';
import { handle } from '../src/hooks.server';
import * as sessionRoute from '../src/routes/api/session/+server';
import {
	canonicalGatePath,
	issueToken,
	PUBLIC_DISPATCH_MAX,
	resetPublicDispatches,
	SESSION_COOKIE,
} from '../src/lib/shell/operator-session.server';
import {
	buildPublicFeed,
	isEmulatedRow,
	projectEvent,
	projectLater,
	PUBLIC_CACHE_MAX_KEYS,
	publicCacheSizes,
	publicDispatchStatus,
	resetPublicCaches,
	withinHorizon,
} from '../src/lib/citizen/public-api.server';
import {
	adaptiveHoldSource,
	chooseAlternate,
	clockAtMs,
	doctrineWindowSec,
	stretchHolds,
	windowState,
	windowTiming,
} from '../src/lib/citizen/holds';
import { composeAdvisory, composeAllClear, holdSourceKey, type AdvisoryFacts } from '../src/lib/comms/advisory';
import { dispatchChatEvent, type ChatStreamCallbacks } from '../src/lib/api/client';

const REJECT = [
	['file:///etc/passwd', 'non-http scheme'],
	['ftp://example.com/x', 'non-http scheme'],
	['ws://example.com/stream', 'ws is not a fetch scheme for external URLs'],
	['http://localhost:8090/health', 'localhost'],
	['http://api.localhost/v1', 'subdomain of localhost'],
	['http://127.0.0.1:8080/health', 'IPv4 loopback'],
	['http://127.8.8.8/', 'loopback /8'],
	['http://0.0.0.0/', 'reserved 0/8'],
	['http://10.1.2.3/api', 'private 10/8'],
	['http://172.16.0.1/api', 'private 172.16/12 lower edge'],
	['http://172.31.255.255/api', 'private 172.16/12 upper edge'],
	['http://192.168.1.10/api', 'private 192.168/16'],
	['http://169.254.169.254/latest/meta-data', 'link-local metadata endpoint'],
	['http://100.64.0.1/', 'CGNAT 100.64/10'],
	['http://192.0.0.9/', 'reserved 192.0.0/24'],
	['http://198.18.0.5/', 'benchmark 198.18/15'],
	['http://224.0.0.1/', 'multicast 224/4'],
	['http://255.255.255.255/', 'broadcast'],
	['http://[::1]:8080/', 'IPv6 loopback'],
	['http://[fe80::1]/', 'IPv6 link-local'],
	['http://[fc00::1]/', 'IPv6 unique-local'],
	['http://[::ffff:127.0.0.1]/', 'IPv4-mapped loopback (dotted form)'],
	['http://[::ffff:7f00:1]/', 'IPv4-mapped loopback (hex form)'],
	['http://[::ffff:10.0.0.1]/', 'IPv4-mapped private (dotted)'],
	['http://[::ffff:a00:1]/', 'IPv4-mapped private (hex 10.0.0.1)'],
	['http://user:pass@example.com/', 'embedded credentials'],
	['not a url at all', 'malformed'],
];

const ACCEPT = [
	['https://example.com/roadworks', 'plain public host'],
	['http://203.0.113.10/api', 'public IP (TEST-NET-3 is documentation range but not in the reject list contract)'],
	['https://maps.example.in/tiles?x=1&y=2', 'public host with query'],
	['http://[2001:db8::1]/', 'global IPv6 documentation address (not loopback/ULA/link-local)'],
];

const SAFE_IDS = ['rath_yatra_ahmedabad', '4817922f-e531-4b64-8c46-39cf257bacbe', 'evt-42.v2'];
const UNSAFE_IDS = ['../etc/passwd', 'a b c', 'id;rm -rf', 'x'.repeat(65), 'id\nwith-newline', ''];

let failures = 0;
for (const [url, why] of REJECT) {
	const res = assertSafeExternalUrl(url);
	if (res.ok) {
		console.error(`FAIL (should reject): ${url} — ${why}`);
		failures++;
	} else {
		console.log(`ok reject: ${url} → ${res.reason}`);
	}
}
for (const [url, why] of ACCEPT) {
	const res = assertSafeExternalUrl(url);
	if (!res.ok) {
		console.error(`FAIL (should accept): ${url} — ${why} → ${res.reason}`);
		failures++;
	} else {
		console.log(`ok accept: ${url} — ${why}`);
	}
}
for (const id of SAFE_IDS) {
	if (safePathId(id) !== id) {
		console.error(`FAIL safePathId should accept: ${JSON.stringify(id)}`);
		failures++;
	} else {
		console.log(`ok id: ${id}`);
	}
}
for (const id of UNSAFE_IDS) {
	if (safePathId(id) !== null) {
		console.error(`FAIL safePathId should reject: ${JSON.stringify(id)}`);
		failures++;
	} else {
		console.log(`ok id rejected: ${id}`);
	}
}

// ── deck proxy authority pinning (src/hooks.server.ts) ─────────────────────
// Regression for the live-confirmed open proxy: /api/convoy//127.0.0.1:8091/
// health once fetched the traffic-oracle through the brain rule because
// `new URL('//host/...', base)` lets a protocol-relative path override the
// authority. fetch is stubbed so the guard is exercised hermetically: what
// matters is which upstream URL the hook computes, never real network I/O.
const BRAIN_TARGET = `http://127.0.0.1:${process.env.BRAIN_PORT ?? '8092'}`;
const ORACLE_TARGET = `http://127.0.0.1:${process.env.ORACLE_PORT ?? '8091'}`;
const EMULATOR_TARGET = `http://127.0.0.1:${process.env.EMULATOR_PORT ?? '8090'}`;

const capturedFetches: string[] = [];
const capturedInits: Array<RequestInit | undefined> = [];
const stubFetch = (async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
	capturedFetches.push(String(input instanceof URL ? input.href : input));
	capturedInits.push(init);
	return new Response('{"status":"ok","stub":true}', {
		status: 200,
		headers: {
			'content-type': 'application/json',
			// What an upstream may send; fetch already DECODED the body.
			'content-encoding': 'gzip',
			'x-upstream': 'kept',
		},
	});
}) as typeof fetch;
globalThis.fetch = stubFetch;

/** Request paths that MUST be refused (400) with fetch never invoked. */
const PROXY_REFUSE: Array<[string, string]> = [
	['/api/convoy//127.0.0.1:8091/health', 'live-confirmed exploit: oracle fetched via brain rule'],
	['/api/emulator//127.0.0.1:8083/v1/models', 'protocol-relative override, emulator rule'],
	['/api/convoy/\\evil.example.com/x', 'backslash spelling (special schemes treat \\ as /)'],
	['/api/convoy//evil.example.com:8092/movement/plan', 'override targeting an external host:port'],
];

/** [path, expected absolute upstream URL] — MUST forward, origin pinned. */
const PROXY_FORWARD: Array<[string, string]> = [
	['/api/convoy/health', `${BRAIN_TARGET}/health`],
	['/api/v1/health', `${ORACLE_TARGET}/api/v1/health`],
	['/api/traffic/health', `${ORACLE_TARGET}/api/traffic/health`],
	['/api/emulator/health', `${EMULATOR_TARGET}/health`],
	// %2F stays encoded path data under WHATWG parsing — no authority
	// override, so it must NOT be refused (guard must not over-block).
	['/api/v1/%2F%2Fevil.example.com/x', `${ORACLE_TARGET}/api/v1/%2F%2Fevil.example.com/x`],
	// Non-strip rules keep the prefix in the suffix, so the reference is
	// path-absolute and the `//host` can only ever land mid-path — origin
	// structurally pinned. Asserted as FORWARD so a future refactor that
	// flips this into an authority override fails loudly here.
	['/api/v1//evil.example.com/x', `${ORACLE_TARGET}/api/v1//evil.example.com/x`],
	['/api/traffic//evil.example.com/x', `${ORACLE_TARGET}/api/traffic//evil.example.com/x`],
];

interface ProxyOptions {
	body?: string;
	headers?: Record<string, string>;
	subRequest?: boolean;
	clientAddress?: string;
}

async function runProxy(rawPath: string, method = 'GET', opts: ProxyOptions = {}): Promise<Response> {
	capturedFetches.length = 0;
	capturedInits.length = 0;
	const url = new URL(rawPath, 'http://deck.internal');
	const init: RequestInit = { method, headers: opts.headers };
	if (method !== 'GET' && method !== 'HEAD') init.body = opts.body ?? 'payload';
	const event = {
		url,
		request: new Request(url, init),
		route: { id: null },
		isSubRequest: opts.subRequest === true,
		getClientAddress: () => opts.clientAddress ?? '10.0.0.9',
		resolve: async () => {
			throw new Error('resolve must not run for proxied paths');
		},
	};
	// biome-ignore lint/suspicious/noExplicitAny: test double for the SvelteKit event
	return await handle({ event: event as any, resolve: event.resolve as any });
}

function check(name: string, cond: boolean, detail = ''): void {
	if (cond) {
		console.log(`ok ${name}${detail ? ` — ${detail}` : ''}`);
	} else {
		console.error(`FAIL ${name}${detail ? ` — ${detail}` : ''}`);
		failures++;
	}
}

for (const [rawPath, why] of PROXY_REFUSE) {
	for (const method of ['GET', 'POST'] as const) {
		const res = await runProxy(rawPath, method);
		if (res.status !== 400 || capturedFetches.length !== 0) {
			console.error(
				`FAIL proxy-refuse ${method} ${rawPath} — ${why} (status ${res.status}, fetches ${capturedFetches.length})`,
			);
			failures++;
		} else {
			console.log(`ok proxy-refuse ${method} ${rawPath} — ${why}`);
		}
	}
}

for (const [rawPath, expected] of PROXY_FORWARD) {
	const res = await runProxy(rawPath);
	if (res.status !== 200 || capturedFetches.length !== 1 || capturedFetches[0] !== expected) {
		console.error(
			`FAIL proxy-forward ${rawPath} — expected ${expected} (status ${res.status}, fetches ${JSON.stringify(capturedFetches)})`,
		);
		failures++;
	} else {
		console.log(`ok proxy-forward ${rawPath} → ${expected}`);
	}
}

// ── edge: WebSocket upgrade → 501, at once, never proxied ────────────────────
{
	// Best of three: "immediately" is a property of the code path (no
	// upstream wait), not of one cold call on a loaded shared host.
	let res: Response | null = null;
	let ms = Number.POSITIVE_INFINITY;
	let fetches = 0;
	for (let i = 0; i < 3; i++) {
		const t0 = performance.now();
		res = await runProxy('/api/convoy/ws/event/x', 'GET', {
			headers: { connection: 'Upgrade', upgrade: 'websocket', 'sec-websocket-version': '13' },
		});
		ms = Math.min(ms, performance.now() - t0);
		fetches += capturedFetches.length;
	}
	check('ws-upgrade answered 501', res?.status === 501, `status ${res?.status}`);
	check('ws-upgrade never reaches fetch', fetches === 0, `${fetches} fetches`);
	check('ws-upgrade answered immediately', ms < 50, `${ms.toFixed(1)} ms (best of 3)`);
	check('ws-upgrade 501 carries nosniff', res?.headers.get('x-content-type-options') === 'nosniff');
}

// ── edge: header hygiene both ways + security headers on proxied responses ──
{
	const res = await runProxy('/api/v1/health', 'GET', {
		headers: {
			cookie: `${SESSION_COOKIE}=forged; other=1`,
			'x-veer-officer': 'spoofed',
			'keep-alive': 'timeout=5',
			te: 'trailers',
		},
	});
	const sent = new Headers(capturedInits[0]?.headers as HeadersInit);
	check('proxy: cookie never travels upstream', !sent.has('cookie'));
	check('proxy: client cannot assert an officer upstream', !sent.has('x-veer-officer'));
	check('proxy: hop-by-hop request headers stripped', !sent.has('keep-alive') && !sent.has('te'));
	check('proxy: content-encoding not relayed (body already decoded)', !res.headers.has('content-encoding'));
	check('proxy: content-length not relayed', !res.headers.has('content-length'));
	check('proxy: other upstream headers relayed', res.headers.get('x-upstream') === 'kept');
	check(
		'proxy: security headers on proxied responses',
		res.headers.get('x-content-type-options') === 'nosniff' &&
			res.headers.get('x-frame-options') === 'DENY' &&
			!!res.headers.get('referrer-policy'),
	);
}

// ── edge: pages — nonce CSP, per-route theme/lang, strict public policy ─────
async function runPage(routeId: string | null, title = '<title>Citizen · Veer</title>'): Promise<{ res: Response; html: string }> {
	const url = new URL('/x', 'http://deck.internal');
	const template =
		`<html lang="%veer.lang%" data-theme="%veer.theme%"><head>${title}<script>boot()</script>` +
		'<script type="module" src="/_app/x.js"></script></head><body></body></html>';
	const event = { url, request: new Request(url), route: { id: routeId }, isSubRequest: false };
	const resolve = async (_e: unknown, opts?: { transformPageChunk?: (a: { html: string; done: boolean }) => string }) => {
		const html = opts?.transformPageChunk ? opts.transformPageChunk({ html: template, done: true }) : template;
		return new Response(html, { headers: { 'content-type': 'text/html' } });
	};
	// biome-ignore lint/suspicious/noExplicitAny: test double for the SvelteKit event
	const res = await handle({ event: event as any, resolve: resolve as any });
	return { res, html: await res.text() };
}
{
	const { res, html } = await runPage('/(public)/citizen');
	const csp = res.headers.get('content-security-policy') ?? '';
	const nonce = /'nonce-([^']+)'/.exec(csp)?.[1] ?? '';
	check('page: CSP present', csp.length > 0);
	check('page: frame-ancestors none', csp.includes("frame-ancestors 'none'"));
	check('page: OSM tiles + Google Fonts + cesium ion allowed', csp.includes('tile.openstreetmap.org') && csp.includes('fonts.googleapis.com') && csp.includes('cesium.com'));
	check('page: no unsafe-inline scripts', !/script-src[^;]*'unsafe-inline'/.test(csp));
	check('page: public route has NO eval', !csp.includes("'unsafe-eval'"));
	check('page: every script tag carries the CSP nonce', nonce.length > 10 && (html.match(/<script/g) ?? []).length === (html.match(new RegExp(`<script nonce="${nonce.replace(/[+/=]/g, (c) => `\\${c}`)}"`, 'g')) ?? []).length);
	check('page: public route renders daylight', html.includes('data-theme="daylight"'));
	check('page: citizen route renders lang=en', html.includes('lang="en"'));
	const driver = await runPage('/(public)/driver');
	check('page: /driver renders lang=gu', driver.html.includes('lang="gu"'));
	const deck = await runPage('/(deck)/gods-eye');
	const deckCsp = deck.res.headers.get('content-security-policy') ?? '';
	check('page: operator route renders command theme', deck.html.includes('data-theme="command"'));
	check("page: operator route allows Cesium's eval + blob task workers", deckCsp.includes("'unsafe-eval'") && /script-src[^;]* blob:/.test(deckCsp));
	check('page: public route allows no blob scripts', !/script-src[^;]* blob:/.test(csp));
	const csp2 = /'nonce-([^']+)'/.exec(deckCsp)?.[1];
	check('page: nonce is per response', !!csp2 && csp2 !== nonce);
	check('page: a routed page keeps exactly its own <title>', (html.match(/<title>/g) ?? []).length === 1 && html.includes('Citizen · Veer'));
	const notFound = await runPage(null, '');
	check('page: an untitled document (built-in error page) gets the brand <title>', (notFound.html.match(/<title>/g) ?? []).length === 1 && notFound.html.includes('<title>Veer</title>'));
}

// ── operator gate (opt-in) ───────────────────────────────────────────────────
const AMB_EVENT = '/api/v1/events/4508d86d-566b-438f-8414-b3d67fd7e6e4/dispatch';
{
	delete process.env.VEER_OPERATOR_PIN;
	const res = await runProxy('/api/v1/enforcement/recommendations/x/decision', 'POST', { body: '{}' });
	check('gate off: writes proxy as before', res.status === 200 && capturedFetches.length === 1);
}

process.env.VEER_OPERATOR_PIN = '1234';
process.env.VEER_SESSION_SECRET = 'guard-suite-secret';

// The canonical path the gate judges = what uvicorn routes on (one decode).
for (const [raw, want] of [
	['/api/v1/%76ehicles', '/api/v1/vehicles'],
	['/api/v1//vehicles', '/api/v1/vehicles'],
	['/api/v1/spatial/%73egments', '/api/v1/spatial/segments'],
	['/api/v1/%2576ehicles', null], // double encoding: '%' survives one decode
	['/api/v1/%E0%A4%A', null], // malformed escape
	['/api/v1/spatial%2F..%2Fvehicles', null], // %2F mints a dot segment
	['/api/v1/%5Cvehicles', null], // encoded backslash
	['/api/v1/x%00y', null], // control character
] as Array<[string, string | null]>) {
	check(`canonical gate path ${raw} → ${want}`, canonicalGatePath(raw) === want, String(canonicalGatePath(raw)));
}

const PURSUIT_ID = '4508d86d-566b-438f-8414-b3d67fd7e6e4';
const DENY: Array<[string, string, string?]> = [
	['POST', '/api/v1/enforcement/recommendations/x/decision', '{"decision":"approve","officer":"X"}'],
	['POST', '/api/v1/dispatches/x/decision', '{}'],
	['POST', '/api/v1/signals/plans/apply', '{}'],
	['POST', '/api/convoy/chat/stream', '{"message":"hi"}'],
	['POST', '/api/emulator/runs', '{}'],
	['PUT', '/api/traffic/x', '{}'],
	['GET', '/api/v1/vehicles'],
	['GET', '/api/v1/enforcement/recommendations?status=pending'],
	['GET', '/api/v1/dispatches?limit=40'],
	['GET', '/api/v1/city/entities'],
	// Reviewer repros: %-encoded spellings uvicorn decodes back to the feeds.
	['GET', '/api/v1/%76ehicles'],
	['GET', '/api/v1/%64ispatches?limit=40'],
	['GET', '/api/v1/%65nforcement/recommendations'],
	['GET', '/api/v1/%63ity/entities'],
	['GET', '/api/v1/%2576ehicles'],
	['GET', '/api/v1/%E0%A4%A'],
	['GET', '/api/v1//vehicles'],
	['GET', '/api/v1/spatial%2F..%2Fvehicles'],
	['HEAD', '/api/v1/%76ehicles'],
	// Default-deny: operator reads a deny list once missed.
	['GET', `/api/v1/events/${PURSUIT_ID}/interception-plan`],
	['GET', `/api/v1/events/${PURSUIT_ID}/%69nterception-plan`],
	['GET', '/api/v1/movements/active'],
	['GET', '/api/convoy/movements/mv-1/protocol'],
	['GET', '/api/v1/units'],
	['GET', '/api/v1/cameras'],
	['GET', '/api/emulator/runs'],
	// The raw events feed carries the pursuit's plate/FIR/officer and the
	// ids that unlock its interception plan; the public pages read the
	// deck's server-side projection instead.
	['GET', '/api/v1/events/active'],
	['GET', '/api/v1/events/%61ctive'],
	['POST', AMB_EVENT, '{"agency":"police","unit_id":"P-1"}'],
	['POST', AMB_EVENT, 'not json'],
	['POST', `${AMB_EVENT}/`, '{"agency":"ambulance","unit_id":"108-14"}'],
];
for (const [method, path, body] of DENY) {
	const res = await runProxy(path, method, { body });
	check(`gate on, anonymous ${method} ${path} → 403`, res.status === 403 && capturedFetches.length === 0, `status ${res.status}`);
}

resetPublicDispatches();
const ALLOW: Array<[string, string, string?]> = [
	['GET', '/api/v1/spatial/segments?min_lon=1'],
	// Same resource upstream (uvicorn decodes once) — judged canonically.
	['GET', '/api/v1/spatial/%73egments?min_lon=1'],
	['GET', '/api/v1/health'],
	['GET', '/api/convoy/health'],
	['GET', '/api/emulator/health'],
	['POST', AMB_EVENT, '{"agency":"ambulance","unit_id":"108-14","requested_by":"108-driver"}'],
	['POST', AMB_EVENT.replace(/dispatch$/, '%64ispatch'), '{"agency":"ambulance","unit_id":"108-14","requested_by":"108-driver"}'],
];
for (const [method, path, body] of ALLOW) {
	const res = await runProxy(path, method, { body, clientAddress: '10.0.0.21' });
	check(`gate on, anonymous ${method} ${path} passes`, res.status === 200 && capturedFetches.length === 1, `status ${res.status}`);
}
{
	// Each anonymous ambulance request is a row on the duty officer's wall:
	// capped per client (PUBLIC_DISPATCH_MAX per 10 min), others unaffected.
	resetPublicDispatches();
	const amb = '{"agency":"ambulance","unit_id":"108-14","requested_by":"108-driver"}';
	const statuses: number[] = [];
	for (let i = 0; i <= PUBLIC_DISPATCH_MAX; i++) {
		statuses.push((await runProxy(AMB_EVENT, 'POST', { body: amb, clientAddress: '10.0.0.30' })).status);
	}
	const capped = statuses[statuses.length - 1] === 429 && capturedFetches.length === 0;
	check(`gate on: anonymous ambulance requests capped per client (${PUBLIC_DISPATCH_MAX}, then 429)`, capped && statuses.slice(0, -1).every((st) => st === 200), statuses.join(','));
	const other = await runProxy(AMB_EVENT, 'POST', { body: amb, clientAddress: '10.0.0.31' });
	check('gate on: the cap is per client', other.status === 200);
	resetPublicDispatches();
}
{
	const res = await runProxy('/api/v1/dispatches?event_id=x', 'GET', { subRequest: true });
	check('gate on: server-side sub-requests pass (public projection)', res.status === 200 && capturedFetches.length === 1);
}

// The sign-in route (POST/GET/DELETE /api/session) with a minimal event double.
function sessionEvent(method: string, body?: unknown, cookieHeader?: string) {
	const url = new URL('/api/session', 'http://deck.internal');
	const jar: Array<{ name: string; value: string; opts: Record<string, unknown> }> = [];
	const request = new Request(url, {
		method,
		headers: { 'content-type': 'application/json', ...(cookieHeader ? { cookie: cookieHeader } : {}) },
		body: body === undefined ? undefined : JSON.stringify(body),
	});
	const cookies = {
		set: (name: string, value: string, opts: Record<string, unknown>) => jar.push({ name, value, opts }),
		delete: (name: string, opts: Record<string, unknown>) => jar.push({ name, value: '', opts: { ...opts, deleted: true } }),
		get: () => undefined,
	};
	return { event: { url, request, cookies, getClientAddress: () => '10.0.0.7' }, jar };
}
type Handler = (e: unknown) => Promise<Response> | Response;
const POST_SESSION = sessionRoute.POST as unknown as Handler;
const GET_SESSION = sessionRoute.GET as unknown as Handler;
const DELETE_SESSION = sessionRoute.DELETE as unknown as Handler;
{
	const bad = sessionEvent('POST', { officer: 'Duty Officer R. Mehta', pin: '9999' });
	const r = await POST_SESSION(bad.event);
	check('session: wrong PIN → 401, no cookie', r.status === 401 && bad.jar.length === 0, `status ${r.status}`);

	const noName = sessionEvent('POST', { officer: ' ', pin: '1234' });
	check('session: blank officer refused', (await POST_SESSION(noName.event)).status === 401);
	// A name with a bidirectional override / isolate control renders reversed in
	// the audit trail: refused at sign-in. ZWJ / ZWNJ (Indic names) stay legal.
	const bidi = sessionEvent('POST', { officer: 'Mehta \u202ereversed', pin: '1234' });
	check('session: officer name with U+202E refused (no cookie)', (await POST_SESSION(bidi.event)).status === 401 && bidi.jar.length === 0);
	const isolate = sessionEvent('POST', { officer: 'Officer \u2067X', pin: '1234' });
	check('session: officer name with U+2067 refused', (await POST_SESSION(isolate.event)).status === 401);
	const mark = sessionEvent('POST', { officer: 'Officer \u200eX', pin: '1234' });
	check('session: officer name with an LRM mark refused (the brain refuses it on escort writes)', (await POST_SESSION(mark.event)).status === 401);
	const indic = sessionEvent('POST', { officer: 'ક્\u200dષ \u200cમહેતા', pin: '1234' });
	check('session: ZWJ / ZWNJ in an Indic name accepted', (await POST_SESSION(indic.event)).status === 200);

	const ok = sessionEvent('POST', { officer: 'Duty Officer R. Mehta', pin: '1234' });
	const r2 = await POST_SESSION(ok.event);
	const set = ok.jar.find((c) => c.name === SESSION_COOKIE);
	check('session: right PIN → 200 + cookie', r2.status === 200 && !!set?.value, `status ${r2.status}`);
	check('session: cookie is HttpOnly + SameSite=Strict + path /', set?.opts.httpOnly === true && set?.opts.sameSite === 'strict' && set?.opts.path === '/');
	check('session: cookie not Secure by default (plain-http LAN would never return it)', set?.opts.secure === false);
	process.env.VEER_SESSION_SECURE = '1';
	const tls = sessionEvent('POST', { officer: 'Duty Officer R. Mehta', pin: '1234' });
	await POST_SESSION(tls.event);
	check('session: VEER_SESSION_SECURE=1 → Secure cookie (TLS deployments)', tls.jar.find((c) => c.name === SESSION_COOKIE)?.opts.secure === true);
	delete process.env.VEER_SESSION_SECURE;

	const cookieHeader = `${SESSION_COOKIE}=${set?.value ?? ''}`;
	const who = await GET_SESSION(sessionEvent('GET', undefined, cookieHeader).event);
	const whoBody = (await who.json()) as { gate?: boolean; officer?: string };
	check('session: GET reports the officer', whoBody.gate === true && whoBody.officer === 'Duty Officer R. Mehta');

	const res = await runProxy('/api/v1/enforcement/recommendations/x/decision', 'POST', {
		body: '{"decision":"approve"}',
		headers: { cookie: cookieHeader },
	});
	const sent = new Headers(capturedInits[0]?.headers as HeadersInit);
	check('gate on, signed in: write passes', res.status === 200 && capturedFetches.length === 1, `status ${res.status}`);
	check('gate on, signed in: officer travels upstream (verified)', decodeURIComponent(sent.get('x-veer-officer') ?? '') === 'Duty Officer R. Mehta');
	check('gate on, signed in: session cookie still never upstream', !sent.has('cookie'));
	const feed = await runProxy('/api/v1/vehicles', 'GET', { headers: { cookie: cookieHeader } });
	check('gate on, signed in: operator feed readable', feed.status === 200);
	const plan = await runProxy(`/api/v1/events/${PURSUIT_ID}/interception-plan`, 'GET', { headers: { cookie: cookieHeader } });
	check('gate on, signed in: interception plan readable', plan.status === 200);

	// Cookies are shared by every port on a host: a malformed or foreign
	// same-named cookie must neither 500 nor shadow the real session.
	const malformed = await runProxy('/api/v1/vehicles', 'GET', { headers: { cookie: `${SESSION_COOKIE}=%E0%A4%A` } });
	check('gate on: malformed session cookie → 403, never 500', malformed.status === 403, `status ${malformed.status}`);
	const badWho = await GET_SESSION(sessionEvent('GET', undefined, `${SESSION_COOKIE}=%E0%A4%A`).event);
	check('session: GET with a malformed cookie answers (no 500)', badWho.status === 200);
	const shadowed = await runProxy('/api/v1/vehicles', 'GET', {
		headers: { cookie: `${SESSION_COOKIE}=%E0%A4%A; ${SESSION_COOKIE}=forged.x; ${cookieHeader}` },
	});
	check('gate on: a valid session behind malformed/foreign same-named cookies still passes', shadowed.status === 200, `status ${shadowed.status}`);

	// Tamper with a signature character that carries data bits only (mid-MAC).
	const token = set?.value ?? '';
	const mid = token.indexOf('.') + 10;
	const tampered = token.slice(0, mid) + (token[mid] === 'A' ? 'B' : 'A') + token.slice(mid + 1);
	const t = await runProxy('/api/v1/vehicles', 'GET', { headers: { cookie: `${SESSION_COOKIE}=${tampered}` } });
	check('gate on: tampered cookie refused', t.status === 403);
	// A non-canonical spelling of the SAME signature (last base64url char's
	// padding bit flipped — identical bytes) is refused too: one token, one spelling.
	const B64 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_';
	const lastChar = token[token.length - 1];
	const respelled = token.slice(0, -1) + B64[B64.indexOf(lastChar) ^ 1];
	const respelledRes = await runProxy('/api/v1/vehicles', 'GET', { headers: { cookie: `${SESSION_COOKIE}=${respelled}` } });
	check('gate on: non-canonical signature spelling refused', respelledRes.status === 403, `${lastChar} → ${respelled.slice(-1)}`);
	const expired = issueToken('Duty Officer R. Mehta', Math.floor(Date.now() / 1000) - 13 * 3600).token;
	const e = await runProxy('/api/v1/vehicles', 'GET', { headers: { cookie: `${SESSION_COOKIE}=${expired}` } });
	check('gate on: expired cookie refused', e.status === 403);

	const out = sessionEvent('DELETE', undefined, cookieHeader);
	const r3 = await DELETE_SESSION(out.event);
	check('session: DELETE clears the cookie', r3.status === 200 && out.jar.some((c) => c.name === SESSION_COOKIE && c.opts.deleted === true));
	// Sign-out revokes the TOKEN, not only the browser's cookie: a copied
	// cookie stops working too.
	const replayed = await runProxy('/api/v1/vehicles', 'GET', { headers: { cookie: cookieHeader } });
	check('session: a signed-out token is revoked server-side (copied cookie → 403)', replayed.status === 403, `status ${replayed.status}`);
	const whoAfter = (await (await GET_SESSION(sessionEvent('GET', undefined, cookieHeader).event)).json()) as { officer?: string | null };
	check('session: GET after sign-out reports nobody', whoAfter.officer === null);
	// …while another officer's session (its own token id) is untouched.
	const other = sessionEvent('POST', { officer: 'Duty Officer S. Patel', pin: '1234' });
	await POST_SESSION(other.event);
	const otherCookie = `${SESSION_COOKIE}=${other.jar.find((c) => c.name === SESSION_COOKIE)?.value ?? ''}`;
	check('session: another session survives that sign-out', (await runProxy('/api/v1/vehicles', 'GET', { headers: { cookie: otherCookie } })).status === 200);

	let last = 0;
	for (let i = 0; i < 9; i++) {
		last = (await POST_SESSION(sessionEvent('POST', { officer: 'Brute Force', pin: String(1000 + i) }).event)).status;
	}
	check('session: PIN attempts throttled (429)', last === 429, `last status ${last}`);
}
delete process.env.VEER_OPERATOR_PIN;
delete process.env.VEER_SESSION_SECRET;
{
	const r = await POST_SESSION(sessionEvent('POST', { officer: 'Any One', pin: '1234' }).event);
	check('session: gate off → 409, nothing to sign in to', r.status === 409);
}

// ── public projection: what an anonymous browser may receive ────────────────
const NOW = Date.parse('2026-10-22T12:00:00Z');
const PURSUIT = {
	event_id: '4508d86d-566b-438f-8414-b3d67fd7e6e4',
	event_kind: 'vehicle_pursuit',
	protection_class: 'none',
	title: 'PURSUIT — stolen GJ27EM9001 (FIR 4401/2026, Kalupur PS)',
	status: 'planning',
	metadata: { fir: 'FIR 4401/2026', vehicle_id: 'v-1' },
	routes: [{ label: 'sweep', segment_ids: [1012, 1015, 1005, 1013] }],
	phases: [],
};
const CRIME = { ...PURSUIT, event_id: '5e44d9f6-941d-5e08-80a9-0251127216ba', event_kind: 'crime_incident' };
const CONVOY = {
	event_id: '713174a8-7e0e-48b4-a01b-83132012f27f',
	event_kind: 'protected_movement',
	protection_class: 'Z+',
	title: 'Z+ convoy — Airport to Stadium',
	description: 'secret',
	status: 'approved',
	movement_id: 'mv-9',
	planned_start: '2026-10-22T12:30:00Z',
	planned_end: '2026-10-22T13:30:00Z',
	metadata: { source: 'convoy-brain' },
	routes: [
		{ label: 'primary', segment_ids: [1003, 1004, 1001, 1015] },
		{ label: 'alt-1', segment_ids: [1028, 1027] },
	],
	phases: [],
};
const PROCESSION = {
	event_id: '33d188d7-d459-458f-a0c3-5e66eb2ace99',
	event_kind: 'procession',
	protection_class: 'none',
	title: 'Rath Yatra',
	status: 'active',
	routes: [{ label: 'route', segment_ids: [1012, 1015] }],
	phases: [{ label: 'w', window_start: '2026-10-22T11:00:00Z', window_end: '2026-10-22T14:00:00Z', closure_policy: { advance_closure_sec: 150 } }],
};
const SEGMENTS = {
	segments: [
		{ segment_id: 1003, road_name: 'CG Road (West)', geometry: { coordinates: [[72.55, 23.03], [72.56, 23.03]] } },
		{ segment_id: 1039, road_name: 'Ellis Bridge Road', geometry: { coordinates: [[72.57, 23.02], [72.58, 23.02]] } },
		{ segment_id: 1012, road_name: 'Gandhi Road', geometry: { coordinates: [[72.58, 23.025], [72.583, 23.031]] } },
	],
};
function oracleStub(routes: Record<string, unknown>, status: Record<string, number> = {}) {
	const seen: string[] = [];
	const fn = async (input: string) => {
		seen.push(input);
		const key = Object.keys(routes).find((k) => input.startsWith(k));
		const code = Object.entries(status).find(([k]) => input.startsWith(k))?.[1];
		if (code) return new Response('{"detail":"x"}', { status: code });
		if (!key) return new Response('{"detail":"not found"}', { status: 404 });
		return new Response(JSON.stringify(routes[key]), { status: 200, headers: { 'content-type': 'application/json' } });
	};
	return { fn, seen };
}
{
	const facts = {
		segments: new Map(),
		junctions: [],
		advanceFromParams: { sec: 120, source: 'adaptive_baseline' as const },
		alternate: null,
		band: null,
	};
	check('projection: vehicle_pursuit never projected', projectEvent(PURSUIT, facts, NOW) === null);
	check('projection: crime_incident never projected', projectEvent(CRIME, facts, NOW) === null);
	check('projection: completed events never projected', projectEvent({ ...PROCESSION, status: 'completed' }, facts, NOW) === null);
	const c = projectEvent(CONVOY, facts, NOW) as unknown as Record<string, unknown>;
	const leaked = ['title', 'description', 'metadata', 'protection_class', 'movement_id', 'routes'].filter((k) => k in (c ?? {}));
	check('projection: no title/description/metadata/class/movement/routes', !!c && leaked.length === 0, leaked.join(','));
	const stretchIds = ((c?.stretches as Array<{ segment_id: number }>) ?? []).map((x) => x.segment_id);
	check('projection: primary spine only (no contingency spine)', stretchIds.join(',') === '1003,1004,1001,1015', stretchIds.join(','));
	const hold = c?.hold as { minutes: number; source: string };
	check('projection: no phase policy + oracle baseline → labelled adaptive_baseline, never "live"', hold?.minutes === 2 && hold?.source === 'adaptive_baseline');
	const live = projectEvent(CONVOY, { ...facts, advanceFromParams: { sec: 180, source: 'adaptive_params' } }, NOW);
	check('projection: a live adaptation is labelled adaptive_params', live?.hold.minutes === 3 && live?.hold.source === 'adaptive_params');
	check('adaptive provenance: only retrieved/learned are live', adaptiveHoldSource('retrieved') === 'adaptive_params' && adaptiveHoldSource('learned') === 'adaptive_params' && adaptiveHoldSource('baseline') === 'adaptive_baseline' && adaptiveHoldSource(undefined) === 'adaptive_baseline' && adaptiveHoldSource('weird') === 'adaptive_baseline');
	const failed = projectEvent(CONVOY, { ...facts, advanceFromParams: null, unavailable: ['hold', 'alternate'] }, NOW);
	check('projection: a failed params read is "unavailable", not "not published"', failed?.hold.minutes === null && failed?.unavailable.includes('hold') === true && failed?.unavailable.includes('alternate') === true);
	const policyFailed = projectEvent(PROCESSION, { ...facts, unavailable: ['hold'] }, NOW);
	check('projection: a closure-plan hold is never "unavailable"', policyFailed?.hold.source === 'closure_policy' && !policyFailed?.unavailable.includes('hold'));
	const p = projectEvent(PROCESSION, facts, NOW) as unknown as { hold: { minutes: number; source: string }; tier: string };
	check('projection: phase closure policy wins, labelled', p?.hold.minutes === 3 && p?.hold.source === 'closure_policy');
	check('projection: live-now event with no band → watch', p?.tier === 'watch');
	const none = projectEvent(PROCESSION, { ...facts, advanceFromParams: null }, NOW);
	check('projection: hold never defaulted', projectEvent({ ...CONVOY }, { ...facts, advanceFromParams: null }, NOW)?.hold.minutes === null && none?.hold.source === 'closure_policy');

	const alt = chooseAlternate([1003, 1004, 1001, 1015], [[1003, 1004, 1001, 1015], [1003, 1039, 1001, 1015], [1003, 1004, 1001, 1006, 1015]], (sid) => (sid === 1039 ? 'Ellis Bridge Road' : sid === 1006 ? 'Riverfront Road (West)' : null));
	check('alternate: fewest inner-spine overlaps wins, named', alt?.roadNames.join('|') === 'Ellis Bridge Road' && alt?.segmentIds.join(',') === '1039');
	check('alternate: spine-only graph → none (never invented)', chooseAlternate([1012, 1015], [[1012, 1015]], () => 'X') === null);
}
{
	resetPublicCaches();
	const stub = oracleStub({
		'/api/v1/events/active': { events: [PURSUIT, CRIME, CONVOY, PROCESSION] },
		'/api/v1/spatial/segments': SEGMENTS,
		'/api/v1/signals/state': { junctions: [{ junction_id: 6, lon: 72.555, lat: 23.03 }] },
		'/api/v1/graph/k-shortest-paths': { paths: [[{ node: 1003 }, { node: 1004 }, { node: 1001 }, { node: 1015 }], [{ node: 1003 }, { node: 1039 }, { node: 1001 }, { node: 1015 }]] },
		'/api/v1/adaptive/params': { params: { advance_closure_sec: 120 }, source: 'baseline', shadow_eval: 'unavailable' },
		'/api/v1/adaptive/situation_log': { entries: [{ situation_class: 'RED', timestamp_utc: new Date(NOW - 60_000).toISOString() }] },
	});
	const r = await buildPublicFeed(stub.fn, NOW);
	const body = r.body as unknown as { events: Array<Record<string, unknown>>; tier: string };
	const kinds = (body.events ?? []).map((e) => e.event_kind);
	const text = JSON.stringify(body);
	check('feed: 200 with public kinds only', r.status === 200 && kinds.join(',') === 'protected_movement,procession', kinds.join(','));
	check('feed: no plate / FIR / title anywhere in the body', !/GJ27EM9001|FIR|PURSUIT|Rath Yatra|Z\+/.test(text));
	check('feed: signage names + nearest signalised junction', text.includes('CG Road (West)') && text.includes('"junction_id":6'));
	check('feed: graph alternate named', text.includes('Ellis Bridge Road'));
	check('feed: band maps to a plain tier, never the code', body.tier === 'hold' && !text.includes('"RED"'));
	check('feed: no operator endpoint touched', !stub.seen.some((u) => /vehicles|enforcement|dispatches|city/.test(u)));
	const convoyWire = (body.events ?? []).find((e) => e.event_kind === 'protected_movement') as { hold?: { source?: string; minutes?: number } } | undefined;
	check('feed: oracle "baseline" params stay labelled baseline (the reviewer repro)', convoyWire?.hold?.source === 'adaptive_baseline' && convoyWire?.hold?.minutes === 2, JSON.stringify(convoyWire?.hold));
	check('feed: params NOT read for an event whose closure plan decides', !stub.seen.some((u) => u.startsWith('/api/v1/adaptive/params') && u.includes(PROCESSION.event_id)));
	check('feed: every event says which facts were unreadable (none here)', (body.events ?? []).every((e) => Array.isArray(e.unavailable) && (e.unavailable as unknown[]).length === 0));
	const again = await buildPublicFeed(stub.fn, NOW + 1000);
	check('feed: cached within 5 s (one oracle read for many citizens)', again === r || (again.status === 200 && stub.seen.filter((u) => u.startsWith('/api/v1/events/active')).length === 1));

	resetPublicCaches();
	const flaky = oracleStub(
		{
			'/api/v1/events/active': { events: [CONVOY, PROCESSION] },
			'/api/v1/spatial/segments': SEGMENTS,
			'/api/v1/signals/state': { junctions: [] },
		},
		{ '/api/v1/adaptive/params': 500, '/api/v1/graph/k-shortest-paths': 500 },
	);
	const fr = await buildPublicFeed(flaky.fn, NOW + 60_000);
	const fe = ((fr.body as unknown as { events: Array<{ event_kind: string; unavailable: string[]; hold: { minutes: number | null } }> }).events ?? []);
	const fc = fe.find((e) => e.event_kind === 'protected_movement');
	const fp = fe.find((e) => e.event_kind === 'procession');
	check('feed: params + graph down → convoy hold and alternate flagged unavailable', fc?.hold.minutes === null && fc?.unavailable.join(',') === 'hold,alternate', fc?.unavailable.join(','));
	check('feed: graph down → procession alternate unavailable, its plan hold intact', fp?.hold.minutes === 3 && fp?.unavailable.join(',') === 'alternate', fp?.unavailable.join(','));

	resetPublicCaches();
	const missing = await buildPublicFeed(oracleStub({}).fn, NOW);
	check('feed: events API 404 → 404 (labelled not deployed)', missing.status === 404);
	resetPublicCaches();
	const down = await buildPublicFeed(oracleStub({}, { '/api/v1/events/active': 502 }).fn, NOW);
	check('feed: events API down → 503', down.status === 503);
	resetPublicCaches();

	// C-M / WP-13: an event-emulator row is projected as emulated (a boolean —
	// never its metadata), never drives the strip, and the feed says it labels them.
	const EMULATED = { ...PROCESSION, event_id: '66666666-1111-4222-8333-944455556666', metadata: { source: 'event-emulator', mode: 'living_city', run_id: 22 } };
	const LATER = {
		...CONVOY,
		event_id: '77777777-1111-4222-8333-944455556666',
		status: 'approved',
		planned_start: new Date(NOW + 5 * 3600_000).toISOString(),
		planned_end: new Date(NOW + 6 * 3600_000).toISOString(),
	};
	const emu = oracleStub({
		'/api/v1/events/active': { events: [EMULATED, LATER] },
		'/api/v1/spatial/segments': SEGMENTS,
		'/api/v1/signals/state': { junctions: [] },
		'/api/v1/adaptive/situation_log': { entries: [{ situation_class: 'RED', timestamp_utc: new Date(NOW - 60_000).toISOString() }] },
	});
	const er = await buildPublicFeed(emu.fn, NOW);
	const eb = er.body as unknown as {
		events: Array<Record<string, unknown>>;
		later: Array<Record<string, unknown>>;
		tier: string;
		emulated_labelling?: boolean;
	};
	const etext = JSON.stringify(eb);
	check('feed: an event-emulator row is projected emulated:true', eb.events?.[0]?.emulated === true);
	check('feed: …its metadata never leaves the server', !/event-emulator|living_city|run_id/.test(etext));
	check('feed: an emulated RED event never turns the citizen strip (real events only)', eb.tier === 'calm', eb.tier);
	check('feed: carries emulated_labelling:true (C-M)', eb.emulated_labelling === true);
	check('feed: an event starting beyond 2 h is NOT projected as a hold card', !(eb.events ?? []).some((e) => e.event_id === LATER.event_id));
	check(
		'feed: …it sits in the dated Later list (kind, start, signage names only)',
		eb.later?.length === 1 && eb.later[0].event_id === LATER.event_id && eb.later[0].starts_at === LATER.planned_start && !('hold' in eb.later[0]) && !('title' in eb.later[0]),
		JSON.stringify(eb.later?.[0]),
	);
	check('feed: no params / graph / band read for a Later event', !emu.seen.some((u) => u.includes(LATER.event_id)));
	check(
		'feed: a protected (VVIP) movement in Later carries its date and NO roads (its route is announced inside 2 h)',
		eb.later?.[0]?.roads_withheld === true && Array.isArray(eb.later?.[0]?.road_names) && (eb.later[0].road_names as unknown[]).length === 0 && !/CG Road/.test(JSON.stringify(eb.later)),
		JSON.stringify(eb.later?.[0]),
	);
	const procLater = projectLater(
		{ ...PROCESSION, event_id: '88888888-1111-4222-8333-944455556666', status: 'approved', phases: [], planned_start: new Date(NOW + 5 * 3600_000).toISOString() },
		new Map([[1012, { roadName: 'Gandhi Road', mid: null }]]),
		NOW,
	);
	check('feed: …a procession in Later keeps its signage roads', procLater?.roads_withheld === false && procLater.road_names.join(',') === 'Gandhi Road', JSON.stringify(procLater));
	check('horizon: active, in-window or ≤ 2 h starts are "now"', withinHorizon(PROCESSION, NOW) && withinHorizon({ ...LATER, planned_start: new Date(NOW + 90 * 60_000).toISOString() }, NOW));
	check('horizon: a start beyond 2 h is later', !withinHorizon(LATER, NOW));
	check('projection: emulated flag from metadata.source only', isEmulatedRow(EMULATED) && !isEmulatedRow(PROCESSION) && !isEmulatedRow({ ...PROCESSION, metadata: { source: 'convoy-brain' } }));
	resetPublicCaches();
}
{
	const D = '9b2f3c1e-1111-4222-8333-944455556666';
	const E = '33d188d7-d459-458f-a0c3-5e66eb2ace99';
	const POLICE = 'aaaaaaaa-1111-4222-8333-944455556666';
	const ROW_D = { dispatch_id: D, event_id: E, agency: 'ambulance', unit_id: '108-14', status: 'approved', decided_by: 'Officer X', decision_note: 'n', approved_at: '2026-10-22T12:00:00Z', audit: [{ actor: 'Officer X' }] };
	const ROW_P = { dispatch_id: POLICE, event_id: E, agency: 'police', unit_id: 'P-1', status: 'approved' };
	const UNITS = { units: [{ unit_id: '108-14', dispatch_id: D, status: 'en_route', position: [72.57, 23.03], eta_sec: 41.6, arrived: false, emulated: true, source: 'emulated_dispatch_projection' }] };
	// The oracle's GET /dispatches/{id} (the by-id read the driver page follows).
	const stub = oracleStub({
		[`/api/v1/dispatches/${D}`]: ROW_D,
		[`/api/v1/dispatches/${POLICE}`]: ROW_P,
		'/api/v1/units': UNITS,
	});
	resetPublicCaches();
	const r = await publicDispatchStatus(stub.fn, D, E, NOW);
	const b = r.body as { dispatch: Record<string, unknown>; unit: Record<string, unknown> };
	const text = JSON.stringify(r.body);
	check('driver status: own ambulance request readable', r.status === 200 && b.dispatch.status === 'approved' && b.dispatch.approved_at === '2026-10-22T12:00:00Z');
	check('driver status: bound unit tracked', (b.unit?.position as number[])?.[0] === 72.57 && b.unit?.eta_sec === 41.6);
	check('driver status: eta carries the instant it was measured', typeof b.unit?.eta_at === 'string' && Number.isFinite(Date.parse(String(b.unit?.eta_at))));
	const reads = stub.seen.length;
	await publicDispatchStatus(stub.fn, D, E, NOW);
	await publicDispatchStatus(stub.fn, D, E, NOW);
	check('driver status: polling drivers share one upstream read pair per 2 s', stub.seen.length === reads, `${stub.seen.length - reads} extra reads`);
	check('driver status: no officer names, notes or audit', !/Officer X|decision_note|audit/.test(text));
	check('driver status: read by id (GET /dispatches/{id}), not the event list', stub.seen.includes(`/api/v1/dispatches/${D}`) && !stub.seen.some((u) => u.includes('/dispatches?')));
	const police = await publicDispatchStatus(stub.fn, POLICE, E, NOW);
	check('driver status: police dispatch never readable', police.status === 404);
	const otherEvent = await publicDispatchStatus(stub.fn, D, '44444444-1111-4222-8333-944455556666', NOW);
	check('driver status: a dispatch of ANOTHER event is not readable with this event', otherEvent.status === 404);
	const bad = await publicDispatchStatus(stub.fn, '../x', E, NOW);
	check('driver status: non-uuid refused 400', bad.status === 400);

	// 404 'dispatch not found' (the oracle's own words) → no such request.
	resetPublicCaches();
	const gone = 'bbbbbbbb-1111-4222-8333-944455556666';
	const goneStub = async (input: string) =>
		input.startsWith(`/api/v1/dispatches/${gone}`)
			? new Response('{"detail":"dispatch not found"}', { status: 404 })
			: new Response('{"detail":"not found"}', { status: 404 });
	const g = await publicDispatchStatus(goneStub, gone, E, NOW);
	check("driver status: 404 'dispatch not found' → 'no such ambulance request'", g.status === 404 && /no such ambulance request/.test(JSON.stringify(g.body)));

	// The uuid comes from the public URL: invented ids never grow the per-id cache past its cap.
	resetPublicCaches();
	const notFound = async () => new Response('{"detail":"dispatch not found"}', { status: 404 });
	for (let i = 0; i < PUBLIC_CACHE_MAX_KEYS + 64; i++) {
		await publicDispatchStatus(notFound, `cccccccc-${String(i).padStart(4, '0')}-4222-8333-944455556666`, E, NOW);
	}
	const sizes = publicCacheSizes();
	check('driver status: invented dispatch ids never grow the cache past its cap', sizes.dispatch > 0 && sizes.dispatch <= PUBLIC_CACHE_MAX_KEYS, `${sizes.dispatch} keys (cap ${PUBLIC_CACHE_MAX_KEYS})`);

	// An oracle without the by-id route (a plain 404) falls back to the event's ambulance list.
	resetPublicCaches();
	const older = oracleStub({
		'/api/v1/dispatches?': { dispatches: [ROW_D, ROW_P] },
		'/api/v1/units': UNITS,
	});
	const fb = await publicDispatchStatus(older.fn, D, E, NOW);
	check('driver status: no by-id route → the ambulance list fallback answers', fb.status === 200 && (fb.body as { dispatch: { status: string } }).dispatch.status === 'approved');
	check('driver status: the fallback queries the ambulance agency only', older.seen.some((u) => u.includes('agency=ambulance')));
}

// ── read-model + writer: window timing, holds, provenance, withholding ─────
{
	const T0 = Date.parse('2026-10-22T12:00:00Z');
	check('window: advance closure clamped to the 60–180 s doctrine', doctrineWindowSec(30) === 60 && doctrineWindowSec(150) === 150 && doctrineWindowSec(300) === 180 && doctrineWindowSec(null) === null);
	// The sandbox repro: window 120 s, unit ~263 s from the junction.
	const far = windowTiming(T0, T0 + 263_000, 120);
	check('window: unit beyond one window → opens window-length before arrival, closes on arrival', far?.anchor === 'eta' && far.opensAt === T0 + 143_000 && far.closesAt === T0 + 263_000);
	check('window: primed → open → closed on the server clock', !!far && windowState(far, T0 + 60_000) === 'primed' && windowState(far, T0 + 200_000) === 'open' && windowState(far, T0 + 263_000) === 'closed');
	const near = windowTiming(T0, T0 + 40_000, 150);
	check('window: unit within one window → opens at approval, closes on arrival', near?.anchor === 'eta' && near.opensAt === T0 && near.closesAt === T0 + 40_000);
	const blind = windowTiming(T0, null, 120);
	check('window: no projected arrival → runs window-length from approved_at', blind?.anchor === 'approval' && blind.opensAt === T0 && blind.closesAt === T0 + 120_000);
	check('window: arrival not after approval → approval-anchored', windowTiming(T0, T0, 90)?.anchor === 'approval');
	check('window: unknown approval or length → untimed', windowTiming(null, T0, 120) === null && windowTiming(T0, T0 + 1000, null) === null);

	const st = [{ segmentId: 1003, roadName: 'CG Road (West)', junctionId: 6 }, { segmentId: 1004, roadName: null, junctionId: null }];
	const planned = stretchHolds(st, 3, T0, 'en', false, T0 + 30_000);
	const active = stretchHolds(st, 3, T0, 'en', true, T0 + 30_000);
	check('holds: a planned event whose window started never reads "holding now"', planned[0]?.state === 'next' && planned[1]?.state === 'later');
	check('holds: an active event holds now', active[0]?.state === 'now' && active[1]?.state === 'next');
	const farOff = stretchHolds(st, 3, T0 + 5 * 3600_000, 'en', false, T0);
	check('holds: a planned hold beyond 2 h is "later", never "next"', farOff[0]?.state === 'later', farOff[0]?.state);
	// Public clocks are IST (whatever the host zone) and carry the day when it is not today.
	const istNoon = Date.parse('2026-10-22T06:30:00Z'); // 12:00 IST
	check('clock: IST HH:MM on the same IST day', clockAtMs(istNoon, 'en', istNoon) === '12:00', clockAtMs(istNoon, 'en', istNoon));
	const tomorrow = clockAtMs(istNoon + 24 * 3600_000, 'en', istNoon);
	check('clock: another IST day carries its date', /23 Oct/.test(tomorrow) && /12:00$/.test(tomorrow), tomorrow);
	check('clock: Gujarati digits for a GU reader', /૧૨:૦૦/.test(clockAtMs(istNoon, 'gu', istNoon)), clockAtMs(istNoon, 'gu', istNoon));

	check('provenance keys: plan / live adaptation / baseline', holdSourceKey('closure_policy') === 'card_hold_from_policy' && holdSourceKey('adaptive_params') === 'card_hold_from_adaptive' && holdSourceKey('adaptive_baseline') === 'card_hold_from_baseline');
	const base: AdvisoryFacts = {
		eventId: 'e',
		eventKind: 'procession',
		stretch: { segmentId: 1003, roadName: 'CG Road (West)' },
		holdMinutes: 3,
		holdSource: 'closure_policy',
		alternate: null,
		anchorMs: T0,
	};
	const noAlt = composeAdvisory(base, 'citizens', 'en').text;
	const altDown = composeAdvisory({ ...base, unavailable: { hold: false, alternate: true } }, 'citizens', 'en').text;
	const holdDown = composeAdvisory({ ...base, holdMinutes: null, holdSource: null, unavailable: { hold: true, alternate: false } }, 'citizens', 'en').text;
	check('writer: "no signed alternate" only when the graph really offers none', /No signed alternate/.test(noAlt) && !/alternate/i.test(altDown), altDown);
	check('writer: a failed hold read says unavailable, not "not published"', /unavailable/i.test(holdDown) && !/not published/i.test(holdDown), holdDown);
	check('writer: no citizen all-clear for a pursuit (withheld)', composeAllClear('vehicle_pursuit', 'en') === composeAdvisory({ ...base, eventKind: 'vehicle_pursuit' }, 'citizens', 'en').text);
	check('writer: no citizen all-clear for an unknown kind', /Operator-only/.test(composeAllClear('hostage_rescue', 'en')));
	check('writer: public kinds still get the all-clear (en/hi/gu)', /cleared/.test(composeAllClear('procession', 'en')) && /समाप्त/.test(composeAllClear('procession', 'hi')) && /પૂર્ણ/.test(composeAllClear('procession', 'gu')));
	// An event-emulator run: every draft carries SIMULATED FEED · EMULATED in its TEXT (a copy can never pass for a real hold).
	const emu = { ...base, emulated: true };
	const emuTexts = (['citizens', 'zones', 'ambulance108', 'municipal'] as const).flatMap((a) => ['en', 'hi', 'gu'].map((l) => composeAdvisory(emu, a, l).text));
	check(
		'writer: every draft of an emulated event is labelled EMULATED (4 audiences × en/hi/gu)',
		emuTexts.length === 12 && emuTexts.every((x) => x.startsWith('[') && x.includes('EMULATED')),
		emuTexts.find((x) => !x.includes('EMULATED')) ?? emuTexts[2],
	);
	check(
		'writer: …in each language’s own words (SIMULATED FEED · सिम्युलेटेड फ़ीड · સિમ્યુલેટેડ ફીડ)',
		composeAdvisory(emu, 'citizens', 'en').text.startsWith('[SIMULATED FEED · EMULATED') &&
			composeAdvisory(emu, 'citizens', 'hi').text.startsWith('[सिम्युलेटेड फ़ीड · EMULATED') &&
			composeAdvisory(emu, 'citizens', 'gu').text.startsWith('[સિમ્યુલેટેડ ફીડ · EMULATED'),
	);
	check('writer: a real event’s draft carries no EMULATED marker', !/EMULATED/.test(composeAdvisory(base, 'citizens', 'en').text));
	check('writer: an emulated all-clear is labelled too; a pursuit stays withheld', /EMULATED/.test(composeAllClear('procession', 'gu', true)) && !/EMULATED/.test(composeAllClear('vehicle_pursuit', 'en', true)));
}

// ── chat stream contract C-D (and the legacy brain shape) ───────────────────
{
	const seen: Record<string, unknown[]> = { token: [], thought: [], call: [], result: [], done: [], error: [] };
	const cb: ChatStreamCallbacks = {
		onToken: (x) => seen.token.push(x),
		onThought: (x) => seen.thought.push(x),
		onToolCall: (x) => seen.call.push(x),
		onToolResult: (x) => seen.result.push(x),
		onDone: (x) => seen.done.push(x),
		onError: (x) => seen.error.push(x.message),
	};
	const cd = [
		{ type: 'thought', data: { stepIndex: 1, text: 'route_strategy → get_live_traffic', tier: 'decider' } },
		{ type: 'tool_call', data: { id: 'c1', name: 'get_live_traffic', args: { segment_ids: [1012] } } },
		{ type: 'tool_result', data: { id: 'c1', name: 'get_live_traffic', ok: true, summary: '3 segments, avg 31 km/h' } },
		{ type: 'token', data: 'Corridor is ' },
		{ type: 'directive', data: { verb: 'focus' } },
		{ type: 'done', data: { session_id: 's-1', tiers_used: ['decider:pplx-decider-v1-27b', 'gemma:aditi-ps18-v7h-12b'], operator_alerts: [], degraded: false } },
	];
	const ends = cd.map((e) => dispatchChatEvent(e, cb));
	const call = seen.call[0] as { callId: string; toolName: string; state: string };
	const result = seen.result[0] as { callId: string; state: string; summary: string; durationMs: number | null };
	const done = seen.done[0] as { sessionId: string; tiersUsed: string[] };
	const thought = seen.thought[0] as { tier: string };
	check('C-D: tool_call {id,name,args} normalised', call?.callId === 'c1' && call.toolName === 'get_live_traffic' && call.state === 'running');
	check('C-D: tool_result {ok,summary} normalised, no invented duration', result?.state === 'success' && result.summary === '3 segments, avg 31 km/h' && result.durationMs === null);
	check('C-D: thought tier carried', thought?.tier === 'decider');
	check('C-D: done.data → session + tiers', done?.sessionId === 's-1' && done.tiersUsed.length === 2);
	check('C-D: unknown types ignored, done is terminal', ends.join(',') === ',,,,,end');

	for (const k of Object.keys(seen)) seen[k] = [];
	const legacy = [
		{ type: 'tool_call', data: { callId: 'tc_1_x', toolName: 'x', arguments: {}, state: 'running' } },
		{ type: 'tool_result', data: { callId: 'tc_1_x', state: 'error', result: { error: 'boom' }, durationMs: 12 } },
		{ type: 'done' },
	];
	legacy.forEach((e) => dispatchChatEvent(e, cb));
	const lr = seen.result[0] as { state: string; durationMs: number };
	const ld = seen.done[0] as { sessionId: string | null; tiersUsed: string[] };
	check('legacy: tool_result error + server duration kept', lr?.state === 'error' && lr.durationMs === 12);
	check('legacy: bare done tolerated', ld?.sessionId === null && ld.tiersUsed.length === 0);
	check('error event is terminal', dispatchChatEvent({ type: 'error', data: 'x' }, cb) === 'end' && seen.error[0] === 'x');
}

if (failures > 0) {
	console.error(`\n${failures} guard failures`);
	process.exit(1);
}
console.log('\nall guard assertions passed');
