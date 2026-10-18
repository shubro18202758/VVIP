/**
 * Guard self-test entry — bundled with esbuild and executed by node in the
 * self-test step (node cannot import .ts directly; esbuild is vendored in
 * node_modules as a vite dependency).
 *
 * Asserts the outbound-URL security contract from the showcase ask:
 *   - scheme http/https only for external-input URLs
 *   - localhost / loopback / private / reserved hosts rejected
 *   - fixed internal endpoints are exempt config constants (documented)
 *   - path ids validated against the strict allowlist
 */

import { assertSafeExternalUrl, safePathId } from '../src/lib/ops/endpoints';

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
		console.log(`ok id rejected: ${JSON.stringify(id)}`);
	}
}

if (failures > 0) {
	console.error(`\n${failures} guard failures`);
	process.exit(1);
}
console.log('\nall guard assertions passed');
