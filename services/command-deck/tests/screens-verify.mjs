/**
 * Screens-verify — the interface-revamp UI acceptance camera.
 *
 * Photographs EVERY revamped surface of the ONE deck (the SvelteKit
 * command-deck serving :5273 as container vvip-argus-command-deck) under the
 * red-team-validated SwiftShader software-WebGL flags and proves, per
 * surface:
 *
 *   (a) the page renders its KEY ELEMENTS (each surface's contract below),
 *   (b) the saved PNG is NON-BLANK at the pixel level (Chrome itself
 *       decodes the PNG; ≥8 distinct colours + >90% opaque pixels — the
 *       showcase_canvas_shots.mjs proof, reused verbatim).
 *
 * Surfaces and their key elements:
 *   /ops       the situation wall — band masthead, event board, pursuit
 *              console, enforcement queue, Jan Rakshak dispatch, city
 *              signal plans, AMA module card, decision-audit stream, and
 *              the wall God's-eye canvas (.ge-canvas)
 *   /gods-eye  the cinematic — Cesium viewer ready (window.__GODS_EYE__)
 *   /citizen   the Daylight advisory — band strip + event cards
 *   /driver    the 108 driver flow DRIVEN end-to-end: idle → evaluating →
 *              proposal → approved open window (live countdown)
 *   /comms     the trilingual advisory desk — EN/HI/GU columns
 *   /chat      the takeover chat console
 *
 * Operator gate: the deck surfaces (/ops, /gods-eye, /comms, /chat) are
 * shot SIGNED IN — one sign-in through tests/lib/operator-session.mjs (one
 * attempt, PIN read at run time, never logged; the cookie is set before each
 * page's first navigation; revoked on exit). The public surfaces (/citizen,
 * /driver) are shot anonymously, as the public sees them.
 *
 * Usage: node tests/screens-verify.mjs [origin] [outDir]
 *        defaults: origin http://127.0.0.1:5273 (the container),
 *                  outDir tests/artifacts/screens
 * Env:   CHROME_PATH (defaults to the puppeteer cache path); VEER_OPERATOR_PIN,
 *        else VEER_OPERATOR_PIN_FILE (a sandbox deck's PIN), else veer.env
 * Exit 0 all checks pass; 1 any failure. Manifest:
 *        <outDir>/screens_manifest.json (per-check ok/detail evidence).
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { puppeteerCookies, signIn, signOut } from './lib/operator-session.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DECK = path.resolve(HERE, '..');

const ORIGIN = (process.argv[2] ?? 'http://127.0.0.1:5273').replace(/\/+$/, '');
const OUT = process.argv[3] ?? path.join(DECK, 'tests', 'artifacts', 'screens');

const CHROME =
	process.env.CHROME_PATH ||
	`${process.env.HOME}/.cache/puppeteer/chrome/linux_arm-153.0.8010.36/chrome-linux-arm64/chrome`;

// puppeteer-core resolves from the React frontend's node_modules (the same
// tree the e2e harness and gods-eye-smoke.mjs use); ESM ignores NODE_PATH,
// so import by file URL.
const PUPPETEER_ENTRY = path.join(
	HERE,
	'..',
	'..',
	'..',
	'frontend',
	'node_modules',
	'puppeteer-core',
	'lib',
	'cjs',
	'puppeteer',
	'puppeteer-core.js',
);
if (!fs.existsSync(PUPPETEER_ENTRY)) {
	console.error(`puppeteer-core not found at ${PUPPETEER_ENTRY}`);
	process.exit(2);
}
const puppeteer = await import(pathToFileURL(PUPPETEER_ENTRY).href);

const SWIFTSHADER_ARGS = [
	'--no-sandbox',
	'--disable-setuid-sandbox',
	'--disable-dev-shm-usage',
	'--enable-unsafe-swiftshader',
	'--use-gl=angle',
	'--use-angle=swiftshader',
];

const PALETTE_MIN = 8;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const results = { origin: ORIGIN, chrome: CHROME, screens: {} };
let failures = 0;
/** The operator session for the gated deck surfaces ({gate:false} when the gate is off). */
let session = null;
function record(screen, name, ok, detail = '') {
	const entry = (results.screens[screen] ||= { checks: {} });
	entry.checks[name] = { ok, detail };
	if (!ok) failures++;
}

/** Pixel-level non-blank proof over a SAVED PNG (Chrome as decoder). */
async function pngNonBlank(browser, pngPath) {
	const b64 = fs.readFileSync(pngPath).toString('base64');
	const page = await browser.newPage();
	try {
		await page.setContent(
			`<!doctype html><body style="margin:0"><img id="i" src="data:image/png;base64,${b64}"></body>`,
			{ waitUntil: 'load', timeout: 30000 },
		);
		await page.evaluate(() => document.getElementById('i').decode?.() ?? null);
		return await page.evaluate(() => {
			const img = document.getElementById('i');
			const c = document.createElement('canvas');
			c.width = 160;
			c.height = 100;
			const ctx = c.getContext('2d');
			ctx.drawImage(img, 0, 0, 160, 100);
			const d = ctx.getImageData(0, 0, 160, 100).data;
			const colours = new Set();
			let opaque = 0;
			for (let i = 0; i < d.length; i += 4) {
				colours.add(`${d[i]},${d[i + 1]},${d[i + 2]}`);
				if (d[i + 3] > 0) opaque++;
			}
			return { distinctColours: colours.size, opaqueFrac: opaque / (d.length / 4) };
		});
	} finally {
		await page.close();
	}
}

/** Shoot one surface: key-element probe + PNG + non-blank proof. */
async function shoot(browser, screen, route, probe, { viewport = { width: 1440, height: 900 }, settle = 1200, anonymous = false } = {}) {
	const page = await browser.newPage();
	await page.setViewport(viewport);
	if (!anonymous && session) await page.setCookie(...puppeteerCookies(session));
	const consoleErrors = [];
	page.on('pageerror', (e) => consoleErrors.push(`pageerror: ${String(e).slice(0, 160)}`));
	try {
		await page.goto(`${ORIGIN}${route}`, { waitUntil: 'domcontentloaded', timeout: 60000 });
		const probeOut = (await probe(page)) || {};
		for (const [name, { ok, detail }] of Object.entries(probeOut)) {
			record(screen, name, ok, detail);
		}
		record(screen, 'no-page-errors', consoleErrors.length === 0, consoleErrors.slice(0, 3).join(' | ') || 'none');
		await sleep(settle);
		const file = `screen_${screen.replace(/[^a-z0-9-]/g, '_')}.png`;
		await page.screenshot({ path: path.join(OUT, file), captureBeyondViewport: false });
		const png = await pngNonBlank(browser, path.join(OUT, file));
		record(
			screen,
			'png-nonblank',
			png.distinctColours >= PALETTE_MIN && png.opaqueFrac > 0.9,
			`png colours=${png.distinctColours} opaque=${(png.opaqueFrac * 100).toFixed(1)}%`,
		);
		results.screens[screen].file = file;
	} finally {
		await page.close();
	}
}

/** Count panels on the page whose h2.title exactly matches. */
function panelTitleProbe(page) {
	return page.evaluate(() => {
		const titles = [...document.querySelectorAll('section.panel h2.title')].map((h) => h.textContent.trim());
		return {
			titles,
			masthead: document.querySelectorAll('[data-prim="band-masthead"]').length,
			panels: document.querySelectorAll('section.panel').length,
			eventCards: document.querySelectorAll('.event-card, .board .event, article[data-event]').length,
			auditRows: document.querySelectorAll('[data-prim="audit-row"]').length,
			canvasEl: document.querySelectorAll('.ge-canvas canvas').length,
			band: document.querySelector('[data-prim="band-masthead"]')?.getAttribute('data-band') ?? null,
		};
	});
}

async function main() {
	fs.mkdirSync(OUT, { recursive: true });
	session = await signIn(ORIGIN, 'Screens verify (automated check)');
	results.operatorGate = session.gate ? 'on (deck surfaces signed in)' : 'off';
	const browser = await puppeteer.launch({ executablePath: CHROME, headless: 'new', args: SWIFTSHADER_ARGS });
	try {
		// ── /ops — THE SITUATION WALL ─────────────────────────────────────
		await shoot(
			browser,
			'ops',
			'/ops',
			async (page) => {
				await page.waitForSelector('.wall', { timeout: 30000 });
				await sleep(6000); // board poll rung + canvas first draws under SwiftShader
				const p = await panelTitleProbe(page);
				const title = (t) => p.titles.includes(t);
				return {
					'band-masthead mounts': {
						ok: p.masthead === 1,
						detail: `data-prim=band-masthead band=${p.band} panels=${p.panels}`,
					},
					'event board (Corridor Operations Board)': {
						ok: title('Corridor Operations Board'),
						detail: `titles=${JSON.stringify(p.titles)}`,
					},
					'pursuit console (Pursuit & Watchlist)': {
						ok: title('Pursuit & Watchlist'),
						detail: '',
					},
					'enforcement queue (Decision Queue)': {
						ok: title('Decision Queue'),
						detail: '',
					},
					'Jan Rakshak dispatch (Jan Rakshak Dispatch)': {
						ok: title('Jan Rakshak Dispatch'),
						detail: '',
					},
					'signal panel (City Signal Plans)': {
						ok: title('City Signal Plans'),
						detail: '',
					},
					'AMA module card (Active Movement Adaptation)': {
						ok: title('Active Movement Adaptation'),
						detail: '',
					},
					'audit stream (Decision-Audit Stream)': {
						ok: title('Decision-Audit Stream'),
						detail: `audit rows rendered=${p.auditRows}`,
					},
					'event rows on the board': {
						ok: p.eventCards > 0,
						detail: `event cards=${p.eventCards}`,
					},
					'wall canvas mounts (.ge-canvas)': {
						ok: p.canvasEl > 0,
						detail: `canvas elements=${p.canvasEl}`,
					},
				};
			},
			{ settle: 2500, viewport: { width: 1680, height: 1050 } },
		);

		// ── /gods-eye — THE CINEMATIC ─────────────────────────────────────
		await shoot(
			browser,
			'gods-eye',
			'/gods-eye',
			async (page) => {
				const deadline = Date.now() + 60000;
				let phase = null;
				while (Date.now() < deadline) {
					phase = await page
						.evaluate(() => window.__GODS_EYE__?.phase ?? null)
						.catch(() => null);
					if (phase && phase !== 'loading') break;
					await sleep(500);
				}
				await sleep(9000); // tiles + first layer draws under software GL
				const g = await page.evaluate(() => ({
					phase: window.__GODS_EYE__?.phase ?? null,
					entities: window.__GODS_EYE__?.entityCount ?? 0,
					layers: window.__GODS_EYE__?.distinctLayerIds ?? [],
					hudChips: document.querySelectorAll('.ge-hud .chip').length,
				}));
				return {
					'viewer ready (window.__GODS_EYE__.phase)': { ok: g.phase === 'ready', detail: `phase=${g.phase}` },
					'entities render on the globe': { ok: g.entities > 0, detail: `entities=${g.entities} layers=[${g.layers.join(',')}]` },
					'HUD chips mount': { ok: g.hudChips > 0, detail: `chips=${g.hudChips}` },
				};
			},
			{ settle: 1500 },
		);

		// ── /citizen — THE DAYLIGHT ADVISORY ──────────────────────────────
		await shoot(
			browser,
			'citizen',
			'/citizen',
			async (page) => {
				await page.waitForSelector('[data-screen="citizen"]', { timeout: 30000 });
				await sleep(6500); // board poll rung (5 s) lands the live events
				const p = await page.evaluate(() => ({
					theme: document.documentElement.getAttribute('data-theme'),
					strip: document.querySelector('[data-strip]')?.getAttribute('data-strip') ?? null,
					cards: document.querySelectorAll('.event-card').length,
					holds: document.querySelectorAll('.event-card .hold').length,
					badges: [...document.querySelectorAll('.event-card .badge, [data-variant]')].length,
					kicker: document.querySelector('.kicker')?.textContent?.trim() ?? '',
				}));
				return {
					'daylight theme on public surface': { ok: p.theme === 'daylight', detail: `data-theme=${p.theme}` },
					'plain-language band strip': { ok: !!p.strip, detail: `strip=${p.strip}` },
					'event cards render': { ok: p.cards > 0, detail: `cards=${p.cards} hold rows=${p.holds}` },
					'advisory kicker': { ok: p.kicker.length > 0, detail: p.kicker },
				};
			},
			{ settle: 1500, anonymous: true },
		);

		// ── /driver — the 108 flow (public: anonymous) ───────────────────
		// Driven end-to-end ONLY in its labelled scripted fallback
		// (data-mode="scripted": nothing is sent, the approval is scripted).
		// In live mode a request is a REAL 'requested' dispatch row on the
		// duty officer's wall, and only a named officer opens the window
		// (C6) — so the camera asserts the idle request card and never
		// presses the request button. Seven checks either way.
		{
			const page = await browser.newPage();
			await page.setViewport({ width: 1440, height: 900 });
			const consoleErrors = [];
			page.on('pageerror', (e) => consoleErrors.push(`pageerror: ${String(e).slice(0, 160)}`));
			try {
				await page.goto(`${ORIGIN}/driver`, { waitUntil: 'domcontentloaded', timeout: 60000 });
				await page.waitForSelector('[data-screen="driver"]', { timeout: 30000 });
				// the public feed settles the mode (connecting → live | scripted)
				await page
					.waitForSelector('[data-screen="driver"]:not([data-mode="connecting"])', { timeout: 30000 })
					.catch(() => null);
				const attrOf = (name) => page.$eval('[data-screen="driver"]', (el, n) => el.getAttribute(n), name);
				const phaseOf = () => attrOf('data-phase');
				const mode = await attrOf('data-mode');
				record('driver', 'flow mounts at idle', (await phaseOf()) === 'idle', `phase=${await phaseOf()} mode=${mode}`);
				results.screens.driver.mode = mode; // the checks below depend on it (the consistency oracle reads it)
				// the golden-hour counter must tick (two reads 1.2 s apart)
				const g1 = await page.$eval('[data-golden-seconds]', (el) => Number(el.getAttribute('data-golden-seconds')));
				await sleep(1300);
				const g2 = await page.$eval('[data-golden-seconds]', (el) => Number(el.getAttribute('data-golden-seconds')));
				record('driver', 'golden-hour counter ticks', g2 > g1, `${g1} → ${g2}`);
				if (mode === 'scripted') {
					// pick the first stretch, request, ride evaluating → proposal
					await page.select('[data-testid="stretch-select"]', await page.$eval('[data-testid="stretch-select"] option:not([value=""])', (o) => o.value));
					await page.evaluate(() => {
						const btns = [...document.querySelectorAll('button')];
						btns.find((b) => b.closest('.request-row'))?.click();
					});
					await page.waitForSelector('[data-testid="evaluating"], [data-testid="proposal"]', { timeout: 20000 });
					record('driver', 'evaluating state mounts', true, `phase=${await phaseOf()}`);
					await page.waitForSelector('[data-testid="proposal"]', { timeout: 30000 });
					record('driver', 'proposal mounts (duty-officer approve present)', true, `phase=${await phaseOf()}`);
					await page.evaluate(() => {
						const btns = [...document.querySelectorAll('[data-testid="proposal"] button')];
						btns.find((b) => /approve|મંજૂર/i.test(b.textContent ?? ''))?.click();
					});
					await page.waitForSelector('[data-testid="window-open"]', { timeout: 20000 });
					const cd = await page.$eval('[data-countdown]', (el) => Number(el.getAttribute('data-countdown')));
					record('driver', 'approved open window (live countdown)', cd > 0, `phase=${await phaseOf()} countdown=${cd}s`);
				} else {
					// live (or a feed that never settled): read the idle card, file nothing
					const card = await page.evaluate(() => {
						const btn = [...document.querySelectorAll('.request-row button')][0] ?? null;
						return {
							stretches: document.querySelectorAll('[data-testid="stretch-select"] option:not([value=""])').length,
							unitInput: !!document.querySelector('[data-testid="unit-input"]'),
							requestButton: !!btn,
							requestHeld: !!btn && btn.disabled,
						};
					});
					record('driver', 'live mode: live corridor stretches on offer', mode === 'live' && card.stretches >= 1,
						`mode=${mode} stretches=${card.stretches}`);
					record('driver', 'live mode: the request must name a 108 unit (button held until one is typed)',
						card.unitInput && card.requestButton && card.requestHeld,
						`unit input=${card.unitInput} request button=${card.requestButton} held=${card.requestHeld}`);
					record('driver', 'live mode: nothing filed by the camera (still idle)', (await phaseOf()) === 'idle',
						`phase=${await phaseOf()} — a window opens only on a named duty officer's approval on /ops`);
				}
				record('driver', 'no-page-errors', consoleErrors.length === 0, consoleErrors.slice(0, 3).join(' | ') || 'none');
				await sleep(1200);
				const file = 'screen_driver.png';
				await page.screenshot({ path: path.join(OUT, file), captureBeyondViewport: false });
				const png = await pngNonBlank(browser, path.join(OUT, file));
				record(
					'driver',
					'png-nonblank',
					png.distinctColours >= PALETTE_MIN && png.opaqueFrac > 0.9,
					`png colours=${png.distinctColours} opaque=${(png.opaqueFrac * 100).toFixed(1)}%`,
				);
				results.screens.driver = { ...(results.screens.driver || {}), file };
			} finally {
				await page.close();
			}
		}

		// ── /comms — THE TRILINGUAL DESK ──────────────────────────────────
		await shoot(
			browser,
			'comms',
			'/comms',
			async (page) => {
				await page.waitForSelector('[data-screen="comms"]', { timeout: 30000 });
				await sleep(2500);
				const p = await page.evaluate(() => {
					const cols = [...document.querySelectorAll('.lang-col')];
					const texts = cols.map((c) => c.querySelector('.adv-text')?.textContent?.trim() ?? '');
					// Signed in, the desk drafts from the live event (not the
					// offline fixture): its road names ride verbatim in Latin
					// script inside the Hindi and Gujarati advisories, so they
					// are read from those columns, never hard-coded.
					const latin = (t) => (t.match(/[A-Za-z][A-Za-z0-9.'’()–-]*(?:[ /]+[A-Za-z0-9(][A-Za-z0-9.'’()–-]*)*/g) ?? []).map((x) => x.trim());
					const byLang = (l) => texts[cols.findIndex((c) => c.getAttribute('data-lang') === l)] ?? '';
					const facts = [...new Set([...latin(byLang('hi')), ...latin(byLang('gu'))])];
					return {
						langs: cols.map((c) => c.getAttribute('data-lang')),
						filled: texts.filter((t) => t.length > 10).length,
						facts,
						sameRoad: facts.length > 0 && ['en', 'hi', 'gu'].every((l) => facts.every((f) => byLang(l).includes(f))),
					};
				});
				return {
					'trilingual columns mount (EN/HI/GU)': {
						ok: p.langs.join(',') === 'en,hi,gu',
						detail: `langs=[${p.langs.join(',')}]`,
					},
					'all three advisories non-empty': { ok: p.filled === 3, detail: `filled=${p.filled}/3` },
					'same facts in all three (road name verbatim)': { ok: p.sameRoad, detail: `facts=[${p.facts.join(' | ')}]` },
				};
			},
			{ settle: 1500 },
		);

		// ── /chat — THE TAKEOVER CONSOLE ──────────────────────────────────
		await shoot(
			browser,
			'chat',
			'/chat',
			async (page) => {
				await page.waitForSelector('[data-screen="chat"]', { timeout: 30000 });
				await sleep(2000);
				const p = await page.evaluate(() => ({
					context: document.querySelectorAll('[data-testid="chat-context"]').length,
					stream: document.querySelectorAll('[data-testid="chat-stream"]').length,
					input: document.querySelectorAll('[data-testid="chat-input"]').length,
				}));
				return {
					'context header mounts': { ok: p.context === 1, detail: `count=${p.context}` },
					'stream region mounts': { ok: p.stream === 1, detail: `count=${p.stream}` },
					'input mounts': { ok: p.input >= 1, detail: `count=${p.input}` },
				};
			},
			{ settle: 1200 },
		);
	} finally {
		await browser.close();
		await signOut(session);
		fs.writeFileSync(path.join(OUT, 'screens_manifest.json'), JSON.stringify(results, null, 2));
	}
	console.log(JSON.stringify(results, null, 2));
	console.log(failures === 0 ? 'SCREENS VERIFY: OK' : `SCREENS VERIFY: ${failures} check(s) failed`);
	process.exit(failures === 0 ? 0 : 1);
}

main().catch(async (e) => {
	await signOut(session);
	console.error('HARNESS ERROR', e);
	fs.writeFileSync(path.join(OUT, 'screens_manifest.json'), JSON.stringify({ ...results, error: String(e) }, null, 2));
	process.exit(2);
});
