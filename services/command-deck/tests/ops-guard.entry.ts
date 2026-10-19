/**
 * Guard self-test entry — bundled with esbuild and executed by node in the
 * self-test step (node cannot import .ts directly; esbuild is vendored in
 * node_modules as a vite dependency).
 *
 * Bundled + run from the deck root (services/command-deck):
 *   node_modules/.bin/esbuild tests/ops-guard.entry.ts \
 *     --bundle --platform=node --format=esm --outfile=<tmp>.mjs && node <tmp>.mjs
 * (also wrapped by tests/test_deck_proxy_guard.py for the pytest ladder.)
 *
 * Asserts the outbound-URL security contract from the showcase ask:
 *   - scheme http/https only for external-input URLs
 *   - localhost / loopback / private / reserved hosts rejected
 *   - fixed internal endpoints are exempt config constants (documented)
 *   - path ids validated against the strict allowlist
 *
 * And the deck proxy's authority-pinning contract (src/hooks.server.ts):
 *   - request paths that would override the upstream authority
 *     (`//host/...`, backslash spelling) are refused with 400 BEFORE fetch
 *   - ordinary paths (including %2F-encoded ones) still proxy to the
 *     pinned loopback target of their rule
 */

import { assertSafeExternalUrl, safePathId } from '../src/lib/ops/endpoints';
import { handle } from '../src/hooks.server';

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
const BRAIN_TARGET = `http://127.0.0.1:${process.env.BRAIN_PORT ?? '8080'}`;
const ORACLE_TARGET = `http://127.0.0.1:${process.env.ORACLE_PORT ?? '8081'}`;
const EMULATOR_TARGET = `http://127.0.0.1:${process.env.EMULATOR_PORT ?? '8090'}`;

const capturedFetches: string[] = [];
const stubFetch = (async (input: RequestInfo | URL): Promise<Response> => {
	capturedFetches.push(String(input instanceof URL ? input.href : input));
	return new Response('{"status":"ok","stub":true}', {
		status: 200,
		headers: { 'content-type': 'application/json' },
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

async function runProxy(rawPath: string, method = 'GET'): Promise<Response> {
	capturedFetches.length = 0;
	const url = new URL(rawPath, 'http://deck.internal');
	const init: RequestInit = { method };
	if (method !== 'GET' && method !== 'HEAD') init.body = 'payload';
	const event = {
		url,
		request: new Request(url, init),
		resolve: async () => {
			throw new Error('resolve must not run for proxied paths');
		},
	};
	// biome-ignore lint/suspicious/noExplicitAny: test double for the SvelteKit event
	return await handle({ event: event as any, resolve: event.resolve as any });
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

if (failures > 0) {
	console.error(`\n${failures} guard failures`);
	process.exit(1);
}
console.log('\nall guard assertions passed');
