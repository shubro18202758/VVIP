/**
 * Edge self-test for the basemap hosts and the reader's language — bundled
 * with esbuild and executed by node exactly like tests/ops-guard.entry.ts.
 * Run from the deck root (services/command-deck):
 *
 *   node_modules/.bin/esbuild tests/edge-basemap.entry.ts --bundle \
 *     --platform=node --format=esm --alias:\$lib=./src/lib \
 *     --outfile=tests/artifacts/edge-basemap-bundle.mjs \
 *     && node tests/artifacts/edge-basemap-bundle.mjs
 *
 * Asserts, hermetically (no request leaves the process):
 *   1. the operator-page CSP admits the ion-keyed photoreal tiles
 *      (https://tile.googleapis.com) and — WP-22 — the plain-http Bing tiles
 *      behind ion asset 2 (http://*.virtualearth.net: Cesium requests them in
 *      the page's scheme and the demo deck is plain http) in img-src AND
 *      connect-src; a public page (no Cesium, never a 3D tile) admits
 *      neither; every page still admits the OSM tiles (the keyless basemap
 *      and the citizen map), Cesium ion + its https Bing imagery and Google
 *      Fonts (tests/ops-guard.entry.ts pins those too); no CARTO host (its
 *      styles now need an API key); no other plain-http source anywhere;
 *   2. <html lang> follows the veer_lang cookie (en | hi | gu, contract C-M)
 *      on operator and public routes, falls back to the route default
 *      (/driver Gujarati-first, else English) when the cookie is absent,
 *      unknown, malformed or hostile — never echoing it, never a 500 —
 *      and the document reads the SAME choice the browser's parser does
 *      (lib/i18n/lang-cookie langFromCookieHeader: one parser, two sides);
 *   3. GET /gods-eye/mode keeps its semantics and, keyed, names the
 *      photoreal 3D asset (tiles3d_asset_id 2275207); the token is withheld
 *      from an anonymous client while the operator gate is on; every answer
 *      is no-store and varies on the cookie (a keyed answer is a credential).
 */

import { handle } from '../src/hooks.server';
import { langFromCookieHeader } from '../src/lib/i18n/lang-cookie';
import * as modeRoute from '../src/routes/(deck)/gods-eye/mode/+server';

let failures = 0;
function check(name: string, cond: boolean, detail = ''): void {
	if (cond) {
		console.log(`ok   ${name}${detail ? ` — ${detail}` : ''}`);
	} else {
		failures++;
		console.error(`FAIL ${name}${detail ? ` — ${detail}` : ''}`);
	}
}

async function runPage(routeId: string | null, cookie?: string): Promise<{ res: Response; html: string }> {
	const url = new URL('/x', 'http://deck.internal');
	const template =
		'<html lang="%veer.lang%" data-theme="%veer.theme%"><head><title>t</title>' +
		'<script>boot()</script></head><body></body></html>';
	const request = new Request(url, cookie === undefined ? undefined : { headers: { cookie } });
	const event = { url, request, route: { id: routeId }, isSubRequest: false };
	const resolve = async (_e: unknown, opts?: { transformPageChunk?: (a: { html: string; done: boolean }) => string }) => {
		const html = opts?.transformPageChunk ? opts.transformPageChunk({ html: template, done: true }) : template;
		return new Response(html, { headers: { 'content-type': 'text/html' } });
	};
	// biome-ignore lint/suspicious/noExplicitAny: test double for the SvelteKit event
	const res = await handle({ event: event as any, resolve: resolve as any });
	return { res, html: await res.text() };
}

function directive(csp: string, name: string): string {
	return csp.split(';').map((d) => d.trim()).find((d) => d.startsWith(`${name} `)) ?? '';
}

function langOf(html: string): string | null {
	return /<html lang="([^"]*)"/.exec(html)?.[1] ?? null;
}

// ── 1. CSP hosts ─────────────────────────────────────────────────────────────
for (const route of ['/(deck)/gods-eye', '/(deck)/ops', '/(public)/citizen', '/(public)/driver']) {
	const { res } = await runPage(route);
	const csp = res.headers.get('content-security-policy') ?? '';
	const operator = route.startsWith('/(deck)');
	for (const name of ['img-src', 'connect-src']) {
		const d = directive(csp, name);
		if (operator) {
			check(`${route}: ${name} admits the photoreal 3D tiles`, d.includes('https://tile.googleapis.com'), d);
			check(`${route}: ${name} admits the plain-http Bing tiles of ion asset 2`, d.includes('http://*.virtualearth.net'), d);
		} else {
			check(`${route}: ${name} never admits the 3D tile host (operator canvas only)`, !d.includes('googleapis'), d);
			check(`${route}: ${name} never admits plain-http Bing (operator canvas only)`, !d.includes('http://*.virtualearth.net'), d);
		}
		const plainHttp = d.split(/\s+/).filter((src) => src.startsWith('http://'));
		check(
			`${route}: ${name} names no other plain-http source`,
			plainHttp.every((src) => src === 'http://*.virtualearth.net'),
			plainHttp.join(' ') || 'none',
		);
		check(`${route}: ${name} names no unused tile host`, !d.includes('cartocdn'));
		check(
			`${route}: ${name} keeps OSM, Cesium ion and Bing`,
			d.includes('https://tile.openstreetmap.org') &&
				d.includes('https://*.tile.openstreetmap.org') &&
				d.includes('https://*.cesium.com') &&
				d.includes('https://*.virtualearth.net'),
			d,
		);
	}
	check(`${route}: fonts unchanged`, directive(csp, 'style-src').includes('https://fonts.googleapis.com') && directive(csp, 'font-src').includes('https://fonts.gstatic.com'));
	check(`${route}: no wildcard host beyond the tile servers`, !/(?:^|\s)\*(?:\s|$)|https:\/\/\*(?:\s|$)/.test(csp));
}

// ── 2. veer_lang → <html lang> ───────────────────────────────────────────────
const LANG_CASES: Array<[string | null, string | undefined, string, string]> = [
	['/(deck)/ops', 'veer_lang=gu', 'gu', 'operator route follows the cookie'],
	['/(deck)/ops', undefined, 'en', 'operator route default English'],
	['/(deck)/gods-eye', 'veer_lang=hi', 'hi', 'City Command in Hindi'],
	['/(public)/citizen', 'veer_lang=hi', 'hi', 'citizen page follows the cookie'],
	['/(public)/citizen', undefined, 'en', 'citizen default English'],
	['/(public)/driver', undefined, 'gu', '/driver stays Gujarati-first'],
	['/(public)/driver', 'veer_lang=en', 'en', '/driver honours an English choice'],
	['/(deck)/ops', 'session=abc; veer_lang=gu; theme=x', 'gu', 'found among other cookies'],
	['/(deck)/ops', 'veer_lang=xx', 'en', 'unknown language → route default'],
	['/(public)/driver', 'veer_lang=fr', 'gu', 'unknown language on /driver → Gujarati'],
	['/(deck)/ops', 'veer_lang=%E0%A4', 'en', 'malformed escape → route default'],
	['/(deck)/ops', 'veer_lang="><script>alert(1)</script>', 'en', 'hostile value never echoed'],
	['/(deck)/ops', 'veer_lang=GU', 'en', 'only the exact codes count'],
	['/(deck)/ops', 'my_veer_lang=gu', 'en', 'a cookie merely ending in veer_lang is not it'],
	[null, 'veer_lang=gu', 'gu', 'the built-in error page follows the cookie too'],
];
for (const [route, cookie, want, why] of LANG_CASES) {
	const { res, html } = await runPage(route, cookie);
	check(`lang: ${why}`, res.status === 200 && langOf(html) === want, `route=${route} cookie=${cookie ?? '—'} lang=${langOf(html)}`);
	check(`lang: ${why} (nothing injected)`, !html.includes('alert(1)'));
}
// One parser on both sides (C-M): the first paint's <html lang> is exactly the
// choice the ops store / shells read back from document.cookie.
for (const [route, cookie, want, why] of LANG_CASES) {
	const routeDefault = route === '/(public)/driver' ? 'gu' : 'en';
	check(`lang: the browser reads the same choice — ${why}`, (langFromCookieHeader(cookie) ?? routeDefault) === want);
}
{
	const { html } = await runPage('/(deck)/ops', 'veer_lang=gu');
	check('lang: the theme still follows the route', html.includes('data-theme="command"'));
	const pub = await runPage('/(public)/citizen', 'veer_lang=gu');
	check('lang: public pages stay daylight', pub.html.includes('data-theme="daylight"') && langOf(pub.html) === 'gu');
}

// ── 3. the mode endpoint ─────────────────────────────────────────────────────
const modeHeaders: Array<string | null> = [];
async function mode(cookie?: string): Promise<Record<string, unknown>> {
	const request = new Request('http://deck.internal/gods-eye/mode', cookie ? { headers: { cookie } } : undefined);
	// biome-ignore lint/suspicious/noExplicitAny: test double for the SvelteKit event
	const res = await (modeRoute.GET as any)({ request });
	modeHeaders.push(`${res.headers.get('cache-control')}|${res.headers.get('vary')}`);
	return (await res.json()) as Record<string, unknown>;
}
{
	delete process.env.VEER_CESIUM_ION_TOKEN;
	delete process.env.VEER_OPERATOR_PIN;
	const keyless = await mode();
	check('mode: keyless without the env token', keyless.mode === 'keyless-osm' && !('token' in keyless) && !('tiles3d_asset_id' in keyless));
	process.env.VEER_CESIUM_ION_TOKEN = 'edge-test-token';
	const keyed = await mode();
	check('mode: keyed answers ion-photoreal + the token', keyed.mode === 'ion-photoreal' && keyed.token === 'edge-test-token');
	check('mode: keyed names the photoreal 3D asset', keyed.tiles3d_asset_id === 2275207);
	process.env.VEER_OPERATOR_PIN = '13579';
	process.env.VEER_SESSION_SECRET = 'edge-basemap-test-secret';
	const anonymous = await mode();
	check('mode: gate on → anonymous gets keyless, no token', anonymous.mode === 'keyless-osm' && !('token' in anonymous));
	delete process.env.VEER_OPERATOR_PIN;
	delete process.env.VEER_SESSION_SECRET;
	delete process.env.VEER_CESIUM_ION_TOKEN;
	check(
		'mode: every answer is no-store and varies on the cookie (keyless, keyed, withheld)',
		modeHeaders.length === 3 && modeHeaders.every((h) => h === 'no-store|cookie'),
		modeHeaders.join(' · '),
	);
}

if (failures > 0) {
	console.error(`edge-basemap: ${failures} FAILURE(S)`);
	process.exit(1);
}
console.log('ok   edge-basemap — all assertions passed');
