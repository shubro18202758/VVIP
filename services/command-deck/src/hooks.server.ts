/**
 * Deck edge — the adapter-node reverse proxy (counterpart of the vite dev
 * proxy, vite.config.ts server.proxy, mirroring frontend/nginx.gb10.conf),
 * the security headers of every response, and the opt-in operator gate.
 *
 * The gods-eye-deck runs host-networked like traffic-oracle and convoy-brain
 * (compose service names do not resolve off the bridge and bridge→host-
 * gateway traffic is firewall-dropped — verified in the gb10 override
 * header), so every upstream is a FIXED LOOPBACK CONFIG CONSTANT (the C5
 * carve-out; none of these is external input). The HOST is pinned to
 * 127.0.0.1 unconditionally; only the PORTS are env-overridable so a
 * parallel clone of the stack (compose.argus-override.yml) can proxy to its
 * own peers without touching the loopback-only security contract:
 *
 *   /api/v1      → http://127.0.0.1:${ORACLE_PORT:-8091}     (traffic-oracle)
 *   /api/traffic → http://127.0.0.1:${ORACLE_PORT:-8091}     (traffic-oracle)
 *   /api/convoy  → http://127.0.0.1:${BRAIN_PORT:-8092}      (convoy-brain, strip)
 *   /api/emulator→ http://127.0.0.1:${EMULATOR_PORT:-8090}   (event-emulator, strip)
 *
 * WebSockets: adapter-node cannot upgrade, so an upgrade request is answered
 * 501 IMMEDIATELY (the /ops audit ladder falls straight to its poll rung
 * instead of hanging on a handshake that never completes). In dev the vite
 * proxy intercepts these prefixes before SvelteKit sees them, so the proxy
 * and gate below are the prod path only.
 *
 * AUTHORITY PINNING (security contract): WHATWG URL parsing lets a
 * protocol-relative path (`//host/...`, or the backslash spelling `\/host/`
 * — special schemes treat `\` as `/`) OVERRIDE the base's authority, which
 * would turn this proxy into an open relay. The request path is therefore
 * untrusted input: after building the upstream URL we assert its origin is
 * still the rule's fixed loopback target and refuse with 400 otherwise
 * (regression: /api/convoy//127.0.0.1:8091/health once fetched the oracle
 * through the brain rule — live-confirmed, fixed 2026-10-19; covered by
 * tests/ops-guard.entry.ts).
 *
 * HEADERS: every response gets nosniff, a referrer policy (origin only
 * cross-site — OSM's tile policy needs the Referer), DENY framing and a
 * permissions policy (no geolocation: the citizen page promises it
 * collects no location). Pages also get a nonce-based CSP: self, Google
 * Fonts, OSM tiles and Cesium ion (+ its Bing imagery), frame-ancestors
 * 'none'. Public pages get no eval and no blob scripts at all; operator
 * pages allow what Cesium needs (knockout's eval, blob task workers),
 * Google's Map Tiles API (the ion-keyed photoreal 3D tiles stream only
 * into the operator canvas) and the Bing aerial tiles behind ion asset 2
 * over plain http: Cesium asks Bing for tile URLs in the PAGE's scheme and
 * the demo deck is served as http://127.0.0.1:5273 (a user decision), so
 * those tiles arrive as http://ecn.t{0-3}.tiles.virtualearth.net — refused,
 * they flooded the console and tipped the canvas into its offline-schematic
 * fallback while the 3D city rendered. A CSP is per document, and the
 * public shell links no operator route, so a citizen never client-navigates
 * into Cesium under the strict policy.
 * Relayed upstream bodies are already decoded by fetch, so
 * content-encoding / content-length are never
 * relayed with them.
 *
 * OPERATOR GATE (opt-in, VEER_OPERATOR_PIN — see
 * $lib/shell/operator-session.server.ts): without the signed session
 * cookie POST /api/session issues, every proxied request is DEFAULT-DENIED
 * (403) on its canonical (once-decoded) path, except the public road
 * geometry, the liveness probes and the 108 driver's ambulance request
 * (capped per client: 429 beyond PUBLIC_DISPATCH_MAX). The deck's own
 * server-side reads (the public projection, /api/public/*) are
 * sub-requests and pass.
 *
 * DOCUMENTS: every route sets its own <title>; a document that rendered
 * none (SvelteKit's built-in error page for an unknown path) gets the
 * brand title here, so no page ships untitled. <html lang> is the reader's
 * chosen language — the veer_lang cookie (en | hi | gu, written by the
 * language switch, contract C-M) — else the route's default (/driver is
 * Gujarati-first), so a choice survives the sign-in reload.
 */

import { randomBytes } from 'node:crypto';
import type { Handle, RequestEvent } from '@sveltejs/kit';
import {
	PUBLIC_DISPATCH_MAX,
	gateDecision,
	gateEnabled,
	sessionFromCookieHeader,
	takePublicDispatch,
} from './lib/shell/operator-session.server';
import { langFromCookieHeader } from './lib/i18n/lang-cookie';

interface ProxyRule {
	prefix: string;
	target: string;
	strip: boolean;
}

const ORACLE_TARGET = `http://127.0.0.1:${process.env.ORACLE_PORT ?? '8091'}`;
const BRAIN_TARGET = `http://127.0.0.1:${process.env.BRAIN_PORT ?? '8092'}`;
const EMULATOR_TARGET = `http://127.0.0.1:${process.env.EMULATOR_PORT ?? '8090'}`;

const PROXY_RULES: readonly ProxyRule[] = Object.freeze([
	{ prefix: '/api/v1', target: ORACLE_TARGET, strip: false },
	{ prefix: '/api/traffic', target: ORACLE_TARGET, strip: false },
	{ prefix: '/api/convoy', target: BRAIN_TARGET, strip: true },
	{ prefix: '/api/emulator', target: EMULATOR_TARGET, strip: true },
]);

/** Hop-by-hop headers that must not be forwarded verbatim (either way). */
const HOP_BY_HOP = new Set([
	'connection',
	'keep-alive',
	'transfer-encoding',
	'upgrade',
	'proxy-authenticate',
	'proxy-authorization',
	'proxy-connection',
	'te',
	'trailer',
]);

/** fetch() decodes upstream bodies, so these would describe the wrong bytes. */
const DECODED_BODY_HEADERS = new Set(['content-encoding', 'content-length']);

/** Upstream-only identity header: set from a verified session, never from the client. */
const OFFICER_HEADER = 'x-veer-officer';

/** vite dev injects its own scripts/HMR socket; the CSP is a production contract. */
const DEV = process.env.NODE_ENV === 'development';

const CSP_SOURCES = {
	tiles: 'https://tile.openstreetmap.org https://*.tile.openstreetmap.org',
	// Cesium ion: api/assets/tile.cesium.com; ion asset 2 imagery is Bing.
	ion: 'https://*.cesium.com https://*.virtualearth.net',
	// Google Photorealistic 3D Tiles (ion asset 2275207) stream from Google's
	// Map Tiles API once ion hands out the session (photoreal-3d layer,
	// operator canvas only — public pages never load them)
	tiles3d: 'https://tile.googleapis.com',
	// ion asset 2's Bing tiles in the page's scheme: on the plain-http demo
	// origin Cesium requests http://ecn.t{0-3}.tiles.virtualearth.net (the ion
	// imagery factory gives no way to ask for https) — operator canvas only
	ionHttpImagery: 'http://*.virtualearth.net',
	fontsCss: 'https://fonts.googleapis.com',
	fontFiles: 'https://fonts.gstatic.com',
};

/**
 * The page CSP. `cesium` relaxes script-src for the operator routes only:
 * the Cesium build bundles knockout, which runs `(0,eval)("this")` while
 * loading ('unsafe-eval'), and Cesium's task workers are blob workers that
 * importScripts() blob URLs (workers inherit this policy, so 'blob:').
 * Without either the God's-eye canvas never starts or never loads a tile.
 * Public pages (no Cesium) keep the strict policy — and never admit the
 * photoreal 3D tile host or the plain-http Bing tiles, which only the
 * operator canvas streams from.
 */
function contentSecurityPolicy(nonce: string, cesium: boolean): string {
	const operatorTiles = cesium ? ` ${CSP_SOURCES.tiles3d} ${CSP_SOURCES.ionHttpImagery}` : '';
	return [
		"default-src 'self'",
		`script-src 'self' 'nonce-${nonce}' 'wasm-unsafe-eval'${cesium ? " 'unsafe-eval' blob:" : ''}`,
		`style-src 'self' 'unsafe-inline' ${CSP_SOURCES.fontsCss}`,
		`font-src 'self' data: ${CSP_SOURCES.fontFiles}`,
		`img-src 'self' data: blob: ${CSP_SOURCES.tiles} ${CSP_SOURCES.ion}${operatorTiles}`,
		`connect-src 'self' ${CSP_SOURCES.tiles} ${CSP_SOURCES.ion}${operatorTiles}`,
		"worker-src 'self' blob:",
		"child-src 'self' blob:",
		"object-src 'none'",
		"base-uri 'self'",
		"form-action 'self'",
		"frame-ancestors 'none'",
	].join('; ');
}

/** Security headers on every response this hook returns. */
function harden(response: Response, nonce: string | null, cesium = false): Response {
	let res = response;
	try {
		res.headers.set('x-content-type-options', 'nosniff');
	} catch {
		// Immutable headers (e.g. a Response.redirect from an endpoint): copy.
		res = new Response(response.body, response);
	}
	const h = res.headers;
	h.set('x-content-type-options', 'nosniff');
	h.set('referrer-policy', 'strict-origin-when-cross-origin');
	h.set('x-frame-options', 'DENY');
	h.set('permissions-policy', 'geolocation=(), camera=(), microphone=(), payment=()');
	if (nonce && !DEV && (h.get('content-type') ?? '').includes('text/html')) {
		h.set('content-security-policy', contentSecurityPolicy(nonce, cesium));
	}
	return res;
}

function jsonResponse(status: number, body: Record<string, unknown>): Response {
	return new Response(JSON.stringify(body), {
		status,
		headers: { 'content-type': 'application/json', 'cache-control': 'no-store' },
	});
}

function matchRule(pathname: string): ProxyRule | null {
	for (const rule of PROXY_RULES) {
		if (pathname === rule.prefix || pathname.startsWith(`${rule.prefix}/`)) return rule;
	}
	return null;
}

function isUpgrade(request: Request): boolean {
	return Boolean(request.headers.get('upgrade'));
}

/**
 * Per-route document defaults: public pages render daylight; the language
 * is the reader's veer_lang choice — read by the same pure parser the
 * browser uses (lib/i18n/lang-cookie langFromCookieHeader: only en | hi |
 * gu count, a malformed escape is no choice, never a 500) — else the
 * route's (/driver is Gujarati-first); only operator routes (which embed
 * Cesium) relax eval.
 */
function documentDefaults(event: RequestEvent): { theme: string; lang: string; operator: boolean } {
	const id = event.route.id ?? '';
	return {
		theme: id.startsWith('/(public)') ? 'daylight' : 'command',
		lang: langFromCookieHeader(event.request.headers.get('cookie')) ?? (id === '/(public)/driver' ? 'gu' : 'en'),
		operator: id.startsWith('/(deck)'),
	};
}

/** The client address for per-client caps ('unknown' off adapter-node). */
function clientKey(event: RequestEvent): string {
	try {
		return event.getClientAddress();
	} catch {
		return 'unknown';
	}
}

/** Fallback for a document that rendered no <title> (the built-in error page). */
function ensureTitle(html: string): string {
	const headEnd = html.indexOf('</head>');
	if (headEnd < 0 || /<title[\s>]/i.test(html.slice(0, headEnd))) return html;
	return `${html.slice(0, headEnd)}<title>Veer</title>${html.slice(headEnd)}`;
}

/** Anonymous dispatch requests are the 108 flow only: agency 'ambulance'. */
async function isAmbulanceRequest(request: Request): Promise<boolean> {
	try {
		const body = (await request.clone().json()) as { agency?: unknown } | null;
		return body?.agency === 'ambulance';
	} catch {
		return false;
	}
}

async function proxy(event: RequestEvent, rule: ProxyRule, officer: string | null): Promise<Response> {
	const suffix = rule.strip ? event.url.pathname.slice(rule.prefix.length) : event.url.pathname;
	const base = new URL(rule.target);
	const upstream = new URL(`${suffix}${event.url.search}`, base);
	// The request path is untrusted input: a protocol-relative suffix
	// (`//host/...`) overrides the base authority under WHATWG URL parsing.
	// Whatever parsing did with it, the upstream origin must still be the
	// rule's fixed loopback target — refuse anything that escaped it.
	if (upstream.origin !== base.origin) {
		return jsonResponse(400, { detail: 'deck proxy: refused authority-override path' });
	}

	const headers = new Headers(event.request.headers);
	headers.delete('host'); // fetch sets the upstream host
	headers.delete('cookie'); // deck sessions never travel upstream
	headers.delete(OFFICER_HEADER); // only a verified session may assert an officer
	for (const name of HOP_BY_HOP) headers.delete(name);
	// Percent-encoded: officer names may be Devanagari/Gujarati (header
	// values must be ByteStrings). Advisory for upstream audit trails.
	if (officer) headers.set(OFFICER_HEADER, encodeURIComponent(officer));

	const method = event.request.method.toUpperCase();
	// Brain workflows are heavyweight by design (the pre-movement plan runs
	// the decider checkpoint + corridor assessment + Gemma narration — the
	// original template budgeted a 5-minute axios timeout for exactly this
	// call). Everything else keeps the 60 s budget.
	const budgetMs = rule.prefix === '/api/convoy' ? 300_000 : 60_000;
	const init: RequestInit & { duplex?: 'half' } = {
		method: event.request.method,
		headers,
		redirect: 'manual',
		signal: AbortSignal.timeout(budgetMs),
	};
	if (method !== 'GET' && method !== 'HEAD') {
		init.body = event.request.body;
		init.duplex = 'half';
	}

	try {
		const res = await fetch(upstream, init);
		const outHeaders = new Headers();
		res.headers.forEach((value, key) => {
			const k = key.toLowerCase();
			if (!HOP_BY_HOP.has(k) && !DECODED_BODY_HEADERS.has(k)) outHeaders.set(key, value);
		});
		return new Response(res.body, {
			status: res.status,
			statusText: res.statusText,
			headers: outHeaders,
		});
	} catch (err) {
		return jsonResponse(502, {
			detail: `deck proxy: upstream ${rule.target} unreachable (${
				err instanceof Error ? err.message : String(err)
			})`,
		});
	}
}

export const handle: Handle = async ({ event, resolve }) => {
	// adapter-node cannot complete a WebSocket handshake: say so at once.
	if (isUpgrade(event.request)) {
		return harden(
			jsonResponse(501, {
				detail: 'deck proxy: WebSocket upgrade is not supported on this origin — use the polling rung',
			}),
			null,
		);
	}

	const rule = matchRule(event.url.pathname);
	if (rule) {
		const gated = gateEnabled();
		const session = gated ? sessionFromCookieHeader(event.request.headers.get('cookie')) : null;
		if (gated && !session && !event.isSubRequest) {
			let verdict = gateDecision(event.request.method, event.url.pathname);
			if (verdict === 'check-body') {
				verdict = (await isAmbulanceRequest(event.request)) ? 'allow' : 'deny';
				if (verdict === 'allow' && !takePublicDispatch(clientKey(event))) {
					return harden(
						jsonResponse(429, {
							detail: `too many ambulance requests from this device (max ${PUBLIC_DISPATCH_MAX} per 10 minutes) — call 108 control`,
						}),
						null,
					);
				}
			}
			if (verdict === 'deny') {
				return harden(
					jsonResponse(403, {
						detail: 'operator session required — sign in as the duty officer (POST /api/session)',
						gate: 'VEER_OPERATOR_PIN',
					}),
					null,
				);
			}
		}
		return harden(await proxy(event, rule, session?.officer ?? null), null);
	}

	const nonce = randomBytes(16).toString('base64');
	const { theme, lang, operator } = documentDefaults(event);
	const response = await resolve(event, {
		transformPageChunk: ({ html }) =>
			ensureTitle(
				html
					.replace('%veer.theme%', theme)
					.replace('%veer.lang%', lang)
					// Every inline/bootstrap script SvelteKit renders carries the
					// per-response nonce the CSP allows (no 'unsafe-inline').
					.replace(/<script(?=[\s>])(?![^>]*\snonce=)/g, `<script nonce="${nonce}"`),
			),
	});
	return harden(response, nonce, operator);
};
