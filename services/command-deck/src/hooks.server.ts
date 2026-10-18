/**
 * Deck-origin reverse proxy — the adapter-node counterpart of the vite dev
 * proxy (vite.config.ts server.proxy), mirroring frontend/nginx.gb10.conf.
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
 *   /api/v1      → http://127.0.0.1:${ORACLE_PORT:-8081}     (traffic-oracle)
 *   /api/traffic → http://127.0.0.1:${ORACLE_PORT:-8081}     (traffic-oracle)
 *   /api/convoy  → http://127.0.0.1:${BRAIN_PORT:-8080}      (convoy-brain, strip)
 *   /api/emulator→ http://127.0.0.1:${EMULATOR_PORT:-8090}   (event-emulator, strip)
 *
 * Limitations, stated honestly: adapter-node cannot upgrade WebSockets, so
 * the /ops audit WS ladder degrades to its 2 s poll rung on this origin (by
 * design — the ladder exists for exactly this); in dev the vite proxy
 * intercepts these prefixes before SvelteKit sees them, so this hook is the
 * prod path only.
 */

import type { Handle } from '@sveltejs/kit';

interface ProxyRule {
	prefix: string;
	target: string;
	strip: boolean;
}

const ORACLE_TARGET = `http://127.0.0.1:${process.env.ORACLE_PORT ?? '8081'}`;
const BRAIN_TARGET = `http://127.0.0.1:${process.env.BRAIN_PORT ?? '8080'}`;
const EMULATOR_TARGET = `http://127.0.0.1:${process.env.EMULATOR_PORT ?? '8090'}`;

const PROXY_RULES: readonly ProxyRule[] = Object.freeze([
	{ prefix: '/api/v1', target: ORACLE_TARGET, strip: false },
	{ prefix: '/api/traffic', target: ORACLE_TARGET, strip: false },
	{ prefix: '/api/convoy', target: BRAIN_TARGET, strip: true },
	{ prefix: '/api/emulator', target: EMULATOR_TARGET, strip: true },
]);

/** Hop-by-hop headers that must not be forwarded verbatim. */
const HOP_BY_HOP = new Set([
	'connection',
	'keep-alive',
	'transfer-encoding',
	'upgrade',
	'proxy-authenticate',
	'proxy-authorization',
	'te',
	'trailer',
]);

function matchRule(pathname: string): ProxyRule | null {
	for (const rule of PROXY_RULES) {
		if (pathname === rule.prefix || pathname.startsWith(`${rule.prefix}/`)) return rule;
	}
	return null;
}

export const handle: Handle = async ({ event, resolve }) => {
	const rule = matchRule(event.url.pathname);
	if (!rule) return resolve(event);

	const suffix = rule.strip ? event.url.pathname.slice(rule.prefix.length) : event.url.pathname;
	const upstream = new URL(`${suffix}${event.url.search}`, rule.target);

	const headers = new Headers(event.request.headers);
	headers.delete('host'); // fetch sets the upstream host

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
			if (!HOP_BY_HOP.has(key.toLowerCase()) && key.toLowerCase() !== 'content-length') {
				outHeaders.set(key, value);
			}
		});
		return new Response(res.body, {
			status: res.status,
			statusText: res.statusText,
			headers: outHeaders,
		});
	} catch (err) {
		return new Response(
			JSON.stringify({
				detail: `deck proxy: upstream ${rule.target} unreachable (${
					err instanceof Error ? err.message : String(err)
				})`,
			}),
			{ status: 502, headers: { 'content-type': 'application/json' } },
		);
	}
};
