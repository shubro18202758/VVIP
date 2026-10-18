/**
 * Citizen-screens headless smoke — the public/comms stream's render gate.
 *
 * Builds nothing itself: serve the adapter-node `build/` output (or pass
 * an already-running origin) and verify under SwiftShader software WebGL
 * (same flags the red-team render proof validated):
 *
 *   1. /citizen renders the Daylight advisory; the shell language switch
 *      flips to Gujarati (Gujarati codepoints on the public surface),
 *      event cards carry their live/demo badge, and the driver link lives.
 *   2. /driver runs the pipeline idle → evaluating → proposal → open with
 *      the SCRIPTED FALLBACK badge default-on and a golden-hour counter
 *      that actually ticks (mono).
 *   3. /chat (the takeover route) renders ChatConsole with the selected-
 *      event context header and shows its labelled degradation chip when
 *      the agent stream is unreachable (chat/stream requests aborted —
 *      deterministic regardless of convoy-brain's state).
 *   4. /comms generates the advisory in all three languages side-by-side:
 *      every column non-empty, EN/HI/GU in the right scripts, and the
 *      SAME facts (the transliterated road name appears verbatim in all
 *      three); the DRAFTED-NOT-DELIVERED honesty is on screen.
 *   5. grep gate: no $lib/stores or data-service import under the
 *      citizen/comms paths (the stream's import boundary).
 *
 * Usage:
 *   node tests/citizen-smoke.mjs [origin]        # default http://127.0.0.1:5199
 *
 * Exits 0 with `ok <name> — <detail>` lines (showcase style); exit 1 on any
 * assertion failure. Screenshot artifacts: tests/artifacts/citizen-*.png
 */

import { createRequire } from 'node:module';
import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, readdirSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DECK = path.resolve(HERE, '..');

const require2 = createRequire('/home/sandy050405/Desktop/Veer/VVIP/frontend/package.json');
const puppeteer = require2('puppeteer').default ?? require2('puppeteer');

const CHROME_CANDIDATES = [
	process.env['CHROME_PATH'],
	'/home/sandy050405/.cache/puppeteer/chrome/linux_arm-153.0.8010.36/chrome-linux-arm64/chrome',
].filter(Boolean);
const executablePath = CHROME_CANDIDATES.find(existsSync);
if (!executablePath) throw new Error('no Chrome binary found (set CHROME_PATH)');

const SWIFTSHADER_ARGS = [
	'--no-sandbox',
	'--disable-setuid-sandbox',
	'--disable-dev-shm-usage',
	'--enable-unsafe-swiftshader',
	'--use-gl=angle',
	'--use-angle=swiftshader',
];

let failures = 0;
function ok(name, cond, detail = '') {
	if (cond) {
		console.log(`ok   ${name}${detail ? ` — ${detail}` : ''}`);
	} else {
		failures++;
		console.error(`FAIL ${name}${detail ? ` — ${detail}` : ''}`);
	}
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const GU = /[\u0A80-\u0AFF]/;
const HI = /[\u0900-\u097F]/;

async function originAlive(origin) {
	try {
		const res = await fetch(`${origin}/citizen`, { signal: AbortSignal.timeout(3000) });
		return res.ok;
	} catch {
		return false;
	}
}

/** Spawn `node build` on the smoke port (adapter-node output). */
async function ensureOrigin(origin, port) {
	if (await originAlive(origin)) return null;
	if (!existsSync(path.join(DECK, 'build', 'index.js'))) {
		throw new Error('no build/ output — run `npx vite build` first');
	}
	const proc = spawn('node', ['build'], {
		cwd: DECK,
		env: { ...process.env, PORT: String(port), HOST: '127.0.0.1' },
		stdio: ['ignore', 'pipe', 'pipe'],
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

/** Gate 5 — the stream's import boundary, checked on the source tree. */
function importBoundaryGate() {
	const roots = [
		'src/lib/citizen',
		'src/lib/comms',
		'src/routes/(public)',
		'src/routes/(deck)/chat',
		'src/routes/(deck)/comms',
	];
	const offenders = [];
	for (const root of roots) {
		const abs = path.join(DECK, root);
		if (!existsSync(abs)) continue;
		const walk = (dir) => {
			for (const entry of readdirSync(dir, { withFileTypes: true })) {
				const p = path.join(dir, entry.name);
				if (entry.isDirectory()) walk(p);
				else if (/\.(svelte|ts)$/.test(entry.name)) {
					const src = readFileSync(p, 'utf8');
					if (src.includes('$lib/stores') || src.includes('$lib/api/data-service')) {
						offenders.push(path.relative(DECK, p));
					}
				}
			}
		};
		walk(abs);
	}
	ok('import boundary (no $lib/stores, no data-service)', offenders.length === 0, offenders.join(', ') || 'clean');
}

async function text(page, selector) {
	return page
		.$eval(selector, (el) => el.textContent ?? '')
		.catch(() => '');
}

const browser = await puppeteer.launch({ executablePath, headless: 'new', args: SWIFTSHADER_ARGS });
let deckProc = null;
mkdirSync(path.join(HERE, 'artifacts'), { recursive: true });

try {
	const portArg = process.argv[2] ? Number(new URL(process.argv[2]).port) : 5199;
	const origin = process.argv[2] ?? `http://127.0.0.1:${portArg}`;
	deckProc = await ensureOrigin(origin, portArg);
	ok('deck-origin up', true, origin);

	const page = await browser.newPage();
	await page.setViewport({ width: 1280, height: 900, isMobile: false });

	// ── 1. /citizen — daylight advisory + Gujarati switch ────────────────
	await page.goto(`${origin}/citizen`, { waitUntil: 'networkidle2', timeout: 60000 });
	await page.waitForSelector('[data-screen="citizen"]', { timeout: 30000 });
	ok('/citizen advisory mounts', true);

	const theme = await page.evaluate(() => document.documentElement.dataset.theme ?? '');
	ok('/citizen daylight theme', theme === 'daylight', `data-theme=${theme}`);

	await page.waitForSelector('[data-strip]', { timeout: 20000 });
	const stripTxt = await text(page, '[data-strip]');
	ok('/citizen plain-language strip', stripTxt.trim().length > 0, stripTxt.trim().slice(0, 60));

	await page.waitForSelector('.event-card', { timeout: 20000 });
	const cardCount = await page.$$eval('.event-card', (els) => els.length);
	ok('/citizen event cards', cardCount > 0, `${cardCount} card(s)`);

	const badgeTxt = (await text(page, '.event-card [data-prim="badge"]')).toLowerCase();
	ok(
		'/citizen source badge honest',
		badgeTxt.includes('live') || badgeTxt.includes('offline') || badgeTxt.includes('preview'),
		badgeTxt.trim(),
	);

	const driverLink = await page.$('[data-testid="driver-link"]');
	ok('/citizen driver link', !!driverLink);

	// Language switch → Gujarati codepoints on the public surface.
	const guButton = await page.evaluateHandle(() => {
		const btns = [...document.querySelectorAll('[data-shell="command-bar"] .lang button')];
		return btns.find((b) => b.textContent?.includes('ગુજરાતી')) ?? null;
	});
	ok('shell Gujarati switch present', !!guButton && guButton.asElement() !== null);
	if (guButton && guButton.asElement()) {
		await guButton.asElement().click();
		await sleep(600);
		const guText = await text(page, '[data-screen="citizen"]');
		const guKicker = await text(page, '.kicker');
		ok(
			'/citizen Gujarati codepoints',
			GU.test(guKicker) && GU.test(guText),
			`kicker="${guKicker.trim().slice(0, 40)}"`,
		);
	}
	await page.screenshot({ path: path.join(HERE, 'artifacts', 'citizen-gu.png') });

	// ── 2. /driver — the pipeline with SCRIPTED badge + ticking counter ──
	await page.goto(`${origin}/driver`, { waitUntil: 'networkidle2', timeout: 60000 });
	await page.waitForSelector('[data-screen="driver"]', { timeout: 30000 });
	ok('/driver window mounts', true);

	const driverTheme = await page.evaluate(() => document.documentElement.dataset.theme ?? '');
	ok('/driver daylight theme', driverTheme === 'daylight', `data-theme=${driverTheme}`);

	await page.waitForSelector('[data-testid="golden-hour"]', { timeout: 20000 });
	const tick1 = Number(await page.$eval('[data-golden-seconds]', (el) => el.getAttribute('data-golden-seconds')));
	await sleep(2300);
	const tick2 = Number(await page.$eval('[data-golden-seconds]', (el) => el.getAttribute('data-golden-seconds')));
	ok('/driver golden-hour ticks', tick2 > tick1, `${tick1} → ${tick2}`);

	const clockMono = await page.$eval('[data-testid="golden-hour"] .golden-clock', (el) => {
		const ff = getComputedStyle(el).fontFamily ?? '';
		return ff.toLowerCase().includes('mono') || ff.toLowerCase().includes('jetbrains');
	});
	ok('/driver golden-hour is mono', clockMono);

	const scriptedBadge = await text(page, '[data-screen="driver"]');
	ok(
		'/driver SCRIPTED badge default-on',
		scriptedBadge.includes('SCRIPTED') || scriptedBadge.includes('સ્ક્રિપ્ટેડ') || scriptedBadge.includes('स्क्रिप्टेड'),
		'badge text found',
	);

	// Pipeline: idle → (pick stretch + request) → evaluating → proposal → open.
	ok('/driver phase idle', (await page.$eval('[data-screen="driver"]', (el) => el.dataset.phase)) === 'idle');

	// Pick the first NUMERIC stretch option — waiting for the board poll
	// to populate the event's route spine first.
	await page.waitForFunction(
		() =>
			!![...(document.querySelector('[data-testid="stretch-select"]')?.querySelectorAll('option') ?? [])].find(
				(o) => /^\d+$/.test(o.value),
			),
		{ timeout: 20000 },
	);
	const firstStretch = await page.$eval('[data-testid="stretch-select"]', (el) => {
		const opt = [...el.querySelectorAll('option')].find((o) => /^\d+$/.test(o.value));
		return opt?.value ?? '';
	});
	ok('/driver stretch options present', firstStretch !== '', `segment ${firstStretch}`);
	await page.select('[data-testid="stretch-select"]', firstStretch);
	await sleep(250); // let bind:value propagate
	const requestBtn = await page.evaluateHandle(() =>
		[...document.querySelectorAll('[data-prim="button"]')].find((b) =>
			(b.textContent ?? '').toLowerCase().includes('corridor') || (b.textContent ?? '').includes('કોરિડો'),
		),
	);
	ok('/driver request button present', requestBtn && requestBtn.asElement() !== null);
	const requestEnabled = await page.evaluate((b) => !b.disabled, requestBtn.asElement());
	ok('/driver request enabled by named actor', requestEnabled);
	await requestBtn.asElement().click();

	await page.waitForFunction(
		() => document.querySelector('[data-screen="driver"]')?.dataset.phase === 'evaluating',
		{ timeout: 5000 },
	).catch(() => undefined);
	const sawEvaluating = await page.$eval('[data-screen="driver"]', (el) => el.dataset.phase);
	ok('/driver phase evaluating', sawEvaluating === 'evaluating', `phase=${sawEvaluating}`);

	await page.waitForSelector('[data-testid="proposal"]', { timeout: 10000 });
	ok('/driver phase proposal', true, 'proposal card mounted');

	const approveBtn = await page.evaluateHandle(() =>
		[...document.querySelectorAll('[data-testid="proposal"] [data-prim="button"]')].find((b) => !b.disabled),
	);
	ok('/driver approve present', approveBtn && approveBtn.asElement() !== null);
	await approveBtn.asElement().click();

	await page.waitForSelector('[data-testid="window-open"]', { timeout: 10000 });
	ok('/driver phase open', true, 'window open card mounted');

	const cd1 = Number(await page.$eval('[data-countdown]', (el) => el.getAttribute('data-countdown')));
	await sleep(2300);
	const cd2 = Number(await page.$eval('[data-countdown]', (el) => el.getAttribute('data-countdown')));
	ok('/driver countdown ticks', cd2 < cd1, `${cd1} → ${cd2}`);
	await page.screenshot({ path: path.join(HERE, 'artifacts', 'driver-open.png') });

	// ── 3. /chat — takeover route, degradation labelled ──────────────────
	await page.setRequestInterception(true);
	page.on('request', (req) => {
		if (req.url().includes('/api/convoy/chat/stream')) req.abort('failed').catch(() => undefined);
		else req.continue().catch(() => undefined);
	});
	await page.goto(`${origin}/chat`, { waitUntil: 'networkidle2', timeout: 60000 });
	await page.waitForSelector('[data-screen="chat"]', { timeout: 30000 });
	ok('/chat ChatConsole mounts (takeover live)', true);

	const ctxTxt = await text(page, '[data-testid="chat-context"]');
	ok('/chat context header present', ctxTxt.trim().length > 0, ctxTxt.trim().slice(0, 50));

	// Send a message with the stream unreachable → labelled degradation.
	await page.waitForSelector('[data-testid="chat-input"]', { timeout: 10000 });
	await page.type('[data-testid="chat-input"]', 'corridor status?');
	await page.keyboard.press('Enter');
	await page.waitForSelector('[data-testid="chat-degraded"]', { timeout: 15000 });
	ok('/chat labelled degradation when stream down', true, 'degradation chip rendered');
	await page.setRequestInterception(false);
	await page.screenshot({ path: path.join(HERE, 'artifacts', 'chat-degraded.png') });

	// ── 4. /comms — identical advisory facts in all three languages ─────
	await page.goto(`${origin}/comms`, { waitUntil: 'networkidle2', timeout: 60000 });
	await page.waitForSelector('[data-screen="comms"]', { timeout: 30000 });
	ok('/comms desk mounts', true);

	const commsTheme = await page.evaluate(() => document.documentElement.dataset.theme ?? '');
	ok('/comms command theme', commsTheme === 'command', `data-theme=${commsTheme}`);

	await page.waitForSelector('[data-testid="comms-trilingual"]', { timeout: 20000 });
	const genBtn = await page.evaluateHandle(() =>
		[...document.querySelectorAll('[data-prim="button"]')].find((b) => !b.disabled),
	);
	if (genBtn && genBtn.asElement()) await genBtn.asElement().click();
	await sleep(500);

	const cols = await page.$$eval('[data-adv-lang]', (els) =>
		els.map((el) => ({ lang: el.closest('[data-lang]')?.dataset.lang ?? '', text: el.textContent ?? '' })),
	);
	const en = cols.find((c) => c.lang === 'en');
	const hi = cols.find((c) => c.lang === 'hi');
	const gu = cols.find((c) => c.lang === 'gu');
	ok(
		'/comms three language columns',
		!!en && !!hi && !!gu && en.text.trim() && hi.text.trim() && gu.text.trim(),
		`${[en, hi, gu].map((c) => c?.text.trim().length ?? 0).join('/')} chars`,
	);
	ok('/comms EN column script', !!en && /^[A-Za-z0-9 :~.·—-]+$/.test(en.text.trim().slice(0, 24)), en?.text.slice(0, 40));
	ok('/comms HI column Devanagari', !!hi && HI.test(hi.text));
	ok('/comms GU column Gujarati', !!gu && GU.test(gu.text));
	const ROAD = 'SP Ring Road';
	ok(
		'/comms identical facts across languages',
		!!en && !!hi && !!gu && en.text.includes(ROAD) && hi.text.includes(ROAD) && gu.text.includes(ROAD),
		`"${ROAD}" verbatim in all three`,
	);
	const drafted = await text(page, '[data-screen="comms"]');
	ok('/comms drafted-not-delivered honesty', drafted.toUpperCase().includes('NOT DELIVERED') || drafted.includes('नहीं भेजा') || drafted.includes('મોકલાયું નથી'));
	await page.screenshot({ path: path.join(HERE, 'artifacts', 'comms-trilingual.png') });

	// ── 5. Import boundary (source gate) ─────────────────────────────────
	importBoundaryGate();
} catch (err) {
	failures++;
	console.error(`FAIL harness — ${String(err).slice(0, 400)}`);
} finally {
	await browser.close().catch(() => undefined);
	if (deckProc) deckProc.kill();
}

if (failures > 0) {
	console.error(`\ncitizen-smoke: ${failures} failure(s)`);
	process.exit(1);
}
console.log('\ncitizen-smoke: all green');
process.exit(0);
