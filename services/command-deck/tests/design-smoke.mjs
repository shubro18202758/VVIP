/**
 * Design-shell headless smoke — the living style sheet (/design) plus the
 * route-topology tripwire. Builds nothing itself: serves the adapter-node
 * build/ output (spawning `PORT=5199 node build` when nothing answers,
 * gods-eye-smoke pattern) and asserts under SwiftShader:
 *
 *   1. `/` IS the restored Convoy Command cockpit (the VVIP template
 *      home): 200 + the template root in the server render (the frame
 *      scripts/showcase_run.sh greps for), and the cockpit MOUNTS in the
 *      browser once the operator gate's state is known, as /ops does —
 *      the smoke signs in when the origin's gate is on (tests/lib/
 *      operator-session.mjs: one attempt, PIN read at run time, never
 *      logged, cookie set before navigation, revoked on exit), so the
 *      cockpit itself must mount; only a sign-in card counts as a failure
 *   2. the live URLs return 200 (/, /ops, /gods-eye, /comms, /chat,
 *      /citizen, /driver) — a URL that resolves twice or zero times fails
 *      sync; this is the tripwire
 *   3. /design: every primitive mounts ([data-prim])
 *   4. token parity BOTH themes via getComputedStyle on the two
 *      [data-theme-preview] boxes, values compared against tokens.css
 *   5. the BLACK chip is an inverted chip: 1px hairline + TRANSLUCENT
 *      fill (never an opaque void)
 *   6. modal opens and closes on Escape (focus trap mounts)
 *   7. toasts push and render (max-3 region)
 *   8. reduced motion calms the deck: --v-dur-wipe → 64ms and BLACK's
 *      breathing animation is removed (static hairline)
 *   9. '/' focuses the command-bar search; the band indicator renders
 *      its honest 'band —' hollow chip when no page assigns state (R3)
 *
 * Usage: node tests/design-smoke.mjs [origin]    # default http://127.0.0.1:5199
 * Env:   VEER_OPERATOR_PIN, else VEER_OPERATOR_PIN_FILE, else veer.env — read
 *        only when the origin's operator gate is on
 * Exit 0 with `ok <name> — <detail>` lines; exit 1 on any failure.
 */

import { createRequire } from 'node:module';
import { spawn } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { puppeteerCookies, signIn, signOut } from './lib/operator-session.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DECK = path.resolve(HERE, '..');

// Puppeteer + cached Chrome live in the repo's frontend/ tree (the e2e
// harness pattern; the same candidates as tests/gods-eye-smoke.mjs): this
// checkout's frontend first, then the argus tree — never the retired one.
const PUPPETEER_CANDIDATES = [
	process.env['PUPPETEER_FROM'],
	path.resolve(DECK, '../../frontend/package.json'),
	'/home/sandy050405/argus/veer/VVIP/frontend/package.json'
].filter(Boolean);
let puppeteer = null;
for (const from of PUPPETEER_CANDIDATES) {
	try {
		const req = createRequire(from);
		puppeteer = req('puppeteer').default ?? req('puppeteer');
		break;
	} catch {
		// next candidate
	}
}
if (!puppeteer) throw new Error(`puppeteer not resolvable from ${PUPPETEER_CANDIDATES.join(', ')}`);

const CHROME_CANDIDATES = [
	process.env['CHROME_PATH'],
	path.join(os.homedir(), '.cache/puppeteer/chrome/linux_arm-153.0.8010.36/chrome-linux-arm64/chrome'),
	path.join(os.homedir(), '.cache/puppeteer/chrome/linux_arm-146.0.7680.76/chrome-linux-arm64/chrome')
].filter(Boolean);
const executablePath = CHROME_CANDIDATES.find(existsSync);
if (!executablePath) throw new Error('no Chrome binary found (set CHROME_PATH)');

const SWIFTSHADER_ARGS = [
	'--no-sandbox',
	'--disable-setuid-sandbox',
	'--disable-dev-shm-usage',
	'--enable-unsafe-swiftshader',
	'--use-gl=angle',
	'--use-angle=swiftshader'
];

let failures = 0;
function ok(name, cond, detail = '') {
	if (cond) console.log(`ok   ${name}${detail ? ` — ${detail}` : ''}`);
	else {
		failures++;
		console.error(`FAIL ${name}${detail ? ` — ${detail}` : ''}`);
	}
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function originAlive(origin) {
	try {
		const res = await fetch(`${origin}/design`, { redirect: 'manual', signal: AbortSignal.timeout(3000) });
		return res.status < 500;
	} catch {
		return false;
	}
}

async function ensureOrigin(origin, port) {
	if (await originAlive(origin)) return null;
	if (!existsSync(path.join(DECK, 'build', 'index.js'))) {
		throw new Error('no build/ output — run `npx vite build` first');
	}
	// A preview this smoke spawns is private: gate off and keyless, whatever
	// this shell exported (the PIN, the session secret and the ion token stay
	// with the deployed deck).
	const env = { ...process.env, PORT: String(port), HOST: '127.0.0.1' };
	for (const k of ['VEER_OPERATOR_PIN', 'VEER_OPERATOR_PIN_FILE', 'VEER_SESSION_SECRET', 'VEER_SESSION_SECURE', 'VEER_CESIUM_ION_TOKEN']) delete env[k];
	const proc = spawn('node', ['build'], {
		cwd: DECK,
		env,
		stdio: ['ignore', 'pipe', 'pipe']
	});
	proc.stdout.on('data', () => {});
	proc.stderr.on('data', (d) => process.stderr.write(`[deck] ${d}`));
	for (let i = 0; i < 40; i++) {
		if (await originAlive(origin)) return proc;
		await sleep(500);
	}
	proc.kill();
	throw new Error(`deck origin did not come up on ${origin}`);
}

/** tokens.css declared values, whitespace-normalised (the parity source). */
function declaredTokens() {
	const css = readFileSync(path.join(DECK, 'src/lib/design/tokens.css'), 'utf8');
	const grab = (from, to) => {
		const a = css.indexOf(from);
		const b = css.indexOf(to, a);
		const map = {};
		for (const m of css.slice(a, b).matchAll(/(--v-[a-z0-9-]+)\s*:\s*([^;]+);/g)) {
			map[m[1]] = m[2].replace(/\s+/g, ' ').trim();
		}
		return map;
	};
	return {
		command: grab("[data-theme='command'] {", "[data-theme='daylight']"),
		daylight: grab("[data-theme='daylight'] {", '@media')
	};
}

const DECLARED = declaredTokens();
const normColor = (s) =>
	s
		.replace(/\s+/g, '')
		.toLowerCase();

const browser = await puppeteer.launch({ executablePath, headless: 'new', args: SWIFTSHADER_ARGS });
let deckProc = null;
let session = null;

try {
	const portArg = process.argv[2] ? Number(new URL(process.argv[2]).port) : 5199;
	const origin = (process.argv[2] ?? `http://127.0.0.1:${portArg}`).replace(/\/+$/, '');
	deckProc = await ensureOrigin(origin, portArg);
	ok('deck-origin up', true, origin);
	session = await signIn(origin, 'Design smoke (automated check)');
	ok('operator gate', true, session.gate ? 'on — signed in' : 'off');

	// ── 1: `/` IS the restored Convoy Command cockpit (the VVIP template
	// home, rebuilt on the live stack) ──────────────────────────────────
	const root = await fetch(`${origin}/`, { redirect: 'manual' });
	const rootHtml = root.ok ? await root.text() : '';
	{
		const cockpit = await browser.newPage();
		await cockpit.setViewport({ width: 1440, height: 900 });
		if (session.gate) await cockpit.setCookie(...puppeteerCookies(session));
		await cockpit.goto(`${origin}/`, { waitUntil: 'domcontentloaded', timeout: 60000 });
		const mountedAs = await cockpit
			.waitForSelector('[data-template="convoy-command"]:not([data-pending]), [data-testid="cockpit-signin-required"]', { timeout: 20000 })
			.then((el) => el?.evaluate((e) => (e.dataset.template ? 'cockpit' : 'gate sign-in')))
			.catch(() => null);
		ok(
			'/ is the Convoy Command cockpit (200, template mounted)',
			root.status === 200 && rootHtml.includes('data-template="convoy-command"') && (session.gate ? mountedAs === 'cockpit' : !!mountedAs),
			`status=${root.status} server-render template=${rootHtml.includes('data-template="convoy-command"')} · mounted=${mountedAs ?? 'nothing'}`
		);
		await cockpit.close();
	}

	// ── 2: the live URLs ─────────────────────────────────────────────────
	const EIGHT = ['/', '/ops', '/gods-eye', '/comms', '/chat', '/citizen', '/driver'];
	for (const url of EIGHT) {
		const res = await fetch(`${origin}${url}`, { redirect: 'manual' });
		ok(`url 200 ${url}`, res.status === 200, `status=${res.status}`);
	}
	const designRes = await fetch(`${origin}/design`, { redirect: 'manual' });
	ok('url 200 /design', designRes.status === 200, `status=${designRes.status}`);

	// ── /design: primitives, parity, BLACK chip, modal, toast, motion ────
	const page = await browser.newPage();
	await page.setViewport({ width: 1440, height: 1000 });
	if (session.gate) await page.setCookie(...puppeteerCookies(session));
	const consoleErrors = [];
	page.on('pageerror', (e) => consoleErrors.push(`pageerror: ${String(e).slice(0, 200)}`));

	await page.goto(`${origin}/design`, { waitUntil: 'networkidle2', timeout: 60000 });

	const PRIMS = [
		'panel',
		'band-chip',
		'badge',
		'button',
		'data-tile',
		'feed-item',
		'audit-row',
		'map-legend',
		'layer-rail',
		'empty-state',
		'band-masthead',
		'decision-card',
		'event-dossier'
	];
	const mounted = await page.evaluate((ids) => {
		const out = {};
		for (const id of ids) out[id] = document.querySelectorAll(`[data-prim="${id}"]`).length;
		out['panel-shell-via-panel'] = document.querySelectorAll('[data-prim="panel"]').length >= 2;
		out['command-bar'] = document.querySelectorAll('[data-shell="command-bar"]').length;
		out['band-pending-chip'] = document.querySelectorAll('[data-band="pending"]').length;
		return out;
	}, PRIMS);
	for (const id of PRIMS) ok(`primitive mounts: ${id}`, mounted[id] > 0, `count=${mounted[id]}`);
	ok('PanelShell mounts (alias of Panel)', mounted['panel-shell-via-panel']);
	ok('command bar mounts', mounted['command-bar'] === 1);
	ok(
		"R3 hollow state is honest: 'band —' chip renders when nothing assigns band",
		mounted['band-pending-chip'] >= 1,
		`count=${mounted['band-pending-chip']}`
	);

	// ── 4: token parity BOTH themes via getComputedStyle ─────────────────
	const parity = await page.evaluate(async () => {
		const read = (sel) => {
			const el = document.querySelector(sel);
			const cs = getComputedStyle(el);
			return {
				canvas: cs.getPropertyValue('--v-canvas').trim(),
				ink1: cs.getPropertyValue('--v-ink-1').trim(),
				textBase: cs.getPropertyValue('--v-text-base').trim(),
				bandGreen: cs.getPropertyValue('--v-band-green').trim(),
				fontMono: cs.getPropertyValue('--v-font-mono').replace(/\s+/g, ' ').trim()
			};
		};
		return {
			command: read('[data-theme-preview="command"]'),
			daylight: read('[data-theme-preview="daylight"]'),
			root: read('html')
		};
	}, DECLARED);

	const themesDiffer =
		normColor(parity.command.canvas) !== normColor(parity.daylight.canvas) &&
		normColor(parity.command.bandGreen) !== normColor(parity.daylight.bandGreen) &&
		parity.command.textBase !== parity.daylight.textBase;
	ok('themes differ (command vs daylight)', themesDiffer, JSON.stringify(parity.command));
	ok(
		'command tokens computed === declared',
		normColor(parity.command.canvas) === normColor(DECLARED.command['--v-canvas']) &&
			normColor(parity.command.bandGreen) === normColor(DECLARED.command['--v-band-green']) &&
			parity.command.textBase === DECLARED.command['--v-text-base'],
		`canvas=${parity.command.canvas} base=${parity.command.textBase}`
	);
	ok(
		'daylight tokens computed === declared (16px citizen base)',
		normColor(parity.daylight.canvas) === normColor(DECLARED.daylight['--v-canvas']) &&
			normColor(parity.daylight.bandGreen) === normColor(DECLARED.daylight['--v-band-green']) &&
			parity.daylight.textBase === DECLARED.daylight['--v-text-base'],
		`canvas=${parity.daylight.canvas} base=${parity.daylight.textBase}`
	);
	ok(
		'root defaults to command (first paint, no flash)',
		normColor(parity.root.canvas) === normColor(DECLARED.command['--v-canvas']),
		`root canvas=${parity.root.canvas}`
	);

	// ── 5: the BLACK chip — 1px hairline + translucent fill ──────────────
	const blackChip = await page.evaluate(() => {
		const el = document.querySelector('[data-prim="band-chip"][data-band="BLACK"]');
		if (!el) return null;
		const cs = getComputedStyle(el);
		const fill = cs.backgroundColor;
		let alpha = 1;
		const m = fill.match(/rgba?\([^)]*,\s*([\d.]+)\s*\)/);
		if (m) alpha = parseFloat(m[1]);
		return {
			borderTopWidth: cs.borderTopWidth,
			borderTopStyle: cs.borderTopStyle,
			borderTopColor: norm0(cs.borderTopColor),
			fill,
			alpha
		};
		function norm0(c) {
			return c.replace(/\s+/g, '').toLowerCase();
		}
	});
	ok(
		'BLACK chip: 1px hairline + translucent fill (never an opaque void)',
		blackChip !== null &&
			blackChip.borderTopWidth === '1px' &&
			blackChip.borderTopStyle === 'solid' &&
			blackChip.alpha < 1,
		blackChip
			? `border=${blackChip.borderTopWidth} ${blackChip.borderTopStyle} fill=${blackChip.fill} alpha=${blackChip.alpha}`
			: 'chip not found'
	);

	// ── 6: modal open + Escape close (focus trap mounts) ────────────────
	await page.evaluate(() => {
		const btns = Array.from(document.querySelectorAll('button'));
		btns.find((b) => b.textContent?.includes('open evidence modal'))?.click();
	});
	await sleep(400);
	let modalCount = await page.evaluate(() => document.querySelectorAll('[data-prim="modal"]').length);
	ok('modal opens', modalCount === 1, `count=${modalCount}`);
	await page.keyboard.press('Escape');
	await sleep(300);
	modalCount = await page.evaluate(() => document.querySelectorAll('[data-prim="modal"]').length);
	ok('modal closes on Escape', modalCount === 0, `count=${modalCount}`);

	// ── 7: toast push renders in the region ─────────────────────────────
	await page.evaluate(() => {
		const btns = Array.from(document.querySelectorAll('button'));
		btns.find((b) => b.textContent?.includes('toast · act'))?.click();
	});
	await sleep(400);
	const toastCount = await page.evaluate(() => document.querySelectorAll('[data-prim="toast"]').length);
	ok('toast pushes and renders', toastCount === 1, `count=${toastCount}`);

	// ── 8: reduced motion calms the deck (doctrine) ─────────────────────
	await page.emulateMediaFeatures([{ name: 'prefers-reduced-motion', value: 'reduce' }]);
	const reduced = await page.evaluate(() => {
		const root = getComputedStyle(document.documentElement);
		// switch the masthead to BLACK so the breathing hairline exists
		const btns = Array.from(document.querySelectorAll('button'));
		const black = btns.find((b) => b.textContent?.trim() === 'BLACK');
		black?.click();
		const mast = document.querySelector('[data-prim="band-masthead"]');
		const hair = mast ? getComputedStyle(mast.querySelector('.hairline')) : null;
		return {
			wipe: root.getPropertyValue('--v-dur-wipe').trim(),
			breathe: root.getPropertyValue('--v-dur-breathe').trim(),
			hairAnimation: hair ? hair.animationName : 'none'
		};
	});
	await page.emulateMediaFeatures([{ name: 'prefers-reduced-motion', value: 'no-preference' }]);
	ok(
		'reduced motion: wipe → 64ms, breathing → static hairline',
		reduced.wipe === '64ms' && reduced.breathe === '0ms' && reduced.hairAnimation === 'none',
		JSON.stringify(reduced)
	);
	const full = await page.evaluate(() => {
		const root = getComputedStyle(document.documentElement);
		const mast = document.querySelector('[data-prim="band-masthead"]');
		const hair = getComputedStyle(mast.querySelector('.hairline'));
		return { wipe: root.getPropertyValue('--v-dur-wipe').trim(), hairAnimation: hair.animationName };
	});
	// Chrome serializes 600ms as ".6s" and prefixes scoped keyframes
	// ("svelte-xxxx-breathe") — compare semantically, not textually.
	const wipeMs = full.wipe.endsWith('ms')
		? parseFloat(full.wipe)
		: Math.round(parseFloat(full.wipe) * 1000);
	ok(
		'motion restored: 600ms wipe + BLACK breathing (2s, the only breathing)',
		wipeMs === 600 && full.hairAnimation.includes('breathe'),
		JSON.stringify(full)
	);

	// ── 9: '/' focuses the command-bar search ────────────────────────────
	await page.keyboard.press('/');
	const searchFocused = await page.evaluate(
		() => document.activeElement?.matches('input[data-shell="command-search"]') ?? false
	);
	ok("keyboard: '/' focuses global search", searchFocused);

	ok(
		'no page errors on /design',
		consoleErrors.length === 0,
		consoleErrors.slice(0, 3).join(' | ') || 'none'
	);
} finally {
	await signOut(session);
	await browser.close().catch(() => {});
	if (deckProc) deckProc.kill();
}

if (failures > 0) {
	console.error(`design-smoke: ${failures} FAILURE(S)`);
	process.exit(1);
}
console.log('ok   design-smoke — all assertions passed');
process.exit(0);
