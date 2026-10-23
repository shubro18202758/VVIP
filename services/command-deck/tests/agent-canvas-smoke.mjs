/**
 * Agentic God's-eye smoke (wave 3, contract C-D2) — City Command at
 * 1920×1080 under SOFTWARE WebGL (SwiftShader: --enable-unsafe-swiftshader
 * --use-gl=angle --use-angle=swiftshader). End to end through the deck's
 * own proxy and the brain's real stream (decider + Gemma):
 *
 *   chip        ⌘K 'ask show me the stolen vehicle on the map' streams a
 *               directive that shows as 'AI suggests: …' (never auto-applied)
 *   apply       Apply moves the camera to within 0.01° of the stolen
 *               vehicle's last lawful sighting (the fused registry's
 *               position) and posts exactly ONE ack
 *   dismiss     a second proposal's Dismiss removes its chip (one ack,
 *               'dismissed')
 *   palette     ⌘K 'fly to GJ27EM9001' selects vehicle:<id> and opens its
 *               dossier with the plate MASKED (no officer named)
 *   explain     the dossier's 'Explain this' streams an answer, masked
 *   cameras     clicking each EM-CAM camera that locate('cam') reports
 *               selects camera:<id> (pick priority: a camera wins its spot)
 *
 * Usage:
 *   node tests/agent-canvas-smoke.mjs [origin]      # default http://127.0.0.1:5214
 *   AGENT_SMOKE_ONLY=palette,cameras                 # subset (chip, apply,
 *                                                    # dismiss, palette, explain, cameras)
 *   AGENT_SMOKE_PLATE=GJ27EM9001                     # the stolen-vehicle plate
 *   AGENT_SMOKE_SHOTS=/tmp/claude-1000/veer-wp14     # screenshot directory
 *   PUPPETEER_FROM=…/package.json, CHROME_PATH=…     # tool resolution overrides
 *
 * The deck must already be serving (adapter-node build/, gate OFF — a
 * preview, never the PIN-gated live deck). Requests the page makes are the
 * deck's own read polling, the chat stream and the directive acks (the brain
 * records them; nothing is approved, dispatched or earmarked). Exits 0 with
 * `ok <name> — <detail>` lines, 1 on any FAIL.
 */

import { createRequire } from 'node:module';
import { existsSync, mkdirSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const DECK = path.resolve(HERE, '..');
const ORIGIN = (process.argv[2] ?? 'http://127.0.0.1:5214').replace(/\/$/, '');
const ONLY = new Set((process.env.AGENT_SMOKE_ONLY ?? '').split(',').map((s) => s.trim()).filter(Boolean));
const run = (name) => ONLY.size === 0 || ONLY.has(name);
const PLATE = process.env.AGENT_SMOKE_PLATE ?? 'GJ27EM9001';
const MASKED = `${PLATE.slice(0, -4)}•••${PLATE.slice(-1)}`;
const SHOTS = process.env.AGENT_SMOKE_SHOTS ?? path.join(HERE, 'artifacts');
const APPLY_TOLERANCE_DEG = 0.01;
const STREAM_BUDGET_MS = 180_000;

const PUPPETEER_CANDIDATES = [
	process.env.PUPPETEER_FROM,
	path.resolve(DECK, '../../frontend/package.json'),
	'/home/sandy050405/argus/veer/VVIP/frontend/package.json',
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
const executablePath = [
	process.env.CHROME_PATH,
	path.join(os.homedir(), '.cache/puppeteer/chrome/linux_arm-153.0.8010.36/chrome-linux-arm64/chrome'),
]
	.filter(Boolean)
	.find((p) => existsSync(p));

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
	if (cond) console.log(`ok   ${name}${detail ? ` — ${detail}` : ''}`);
	else {
		failures++;
		console.error(`FAIL ${name}${detail ? ` — ${detail}` : ''}`);
	}
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const fmt = (n, d = 5) => (typeof n === 'number' ? n.toFixed(d) : String(n));

async function getJson(url) {
	const res = await fetch(url, { signal: AbortSignal.timeout(15_000) });
	if (!res.ok) throw new Error(`${res.status} ${url}`);
	return res.json();
}

/** The stolen vehicle from the deck's own proxied fusion API (never from the canvas). */
async function stolenVehicle() {
	const body = await getJson(`${ORIGIN}/api/v1/city/entities?kinds=vehicle`);
	const norm = (s) => String(s ?? '').replace(/[\s-]/g, '').toUpperCase();
	return (body.entities ?? []).find((e) => norm(e.props?.plate) === norm(PLATE)) ?? null;
}

async function snapshot(page) {
	return page.evaluate(() => {
		const s = window.__GODS_EYE__;
		return s
			? { phase: s.phase, camera: s.camera, selection: s.selection, registry: s.registry, agent: s.agent ?? null }
			: null;
	});
}

async function waitFor(page, fn, timeoutMs, stepMs = 250) {
	const deadline = Date.now() + timeoutMs;
	let last = null;
	while (Date.now() < deadline) {
		last = await fn();
		if (last) return last;
		await sleep(stepMs);
	}
	return null;
}

async function shot(page, name) {
	mkdirSync(SHOTS, { recursive: true });
	const file = path.join(SHOTS, `agent-${name}.png`);
	await page.screenshot({ path: file });
	return file;
}

async function openPalette(page) {
	await page.keyboard.down('Control');
	await page.keyboard.press('KeyK');
	await page.keyboard.up('Control');
	return page.waitForSelector('[data-testid="palette-input"]', { visible: true, timeout: 5000 }).then(() => true).catch(() => false);
}

async function paletteRun(page, text) {
	if (!(await openPalette(page))) return false;
	// The palette opens with an empty query; type() focuses the input itself
	// (the palette's own focus waits for an animation frame, slow on SwiftShader).
	await page.type('[data-testid="palette-input"]', text, { delay: 5 });
	await sleep(400);
	await page.keyboard.press('Enter');
	return true;
}

/**
 * Esc on City Command is layered (palette → dossier → EXIT to the wall):
 * press it only when there is a palette or a selection to close.
 */
async function closePalette(page) {
	if (await page.$('[data-testid="palette-input"]')) await page.keyboard.press('Escape');
	await sleep(150);
}

async function closeDossier(page) {
	await closePalette(page);
	if ((await snapshot(page))?.selection?.urn) await page.keyboard.press('Escape');
	await sleep(250);
}

async function chipCount(page) {
	return page.$$eval('[data-testid="agent-chip"]', (els) => els.length);
}

const browser = await puppeteer.launch({
	headless: true,
	executablePath,
	args: SWIFTSHADER_ARGS,
	defaultViewport: { width: 1920, height: 1080 },
});
try {
	const page = await browser.newPage();
	const acks = [];
	page.on('request', (req) => {
		if (req.url().endsWith('/api/convoy/chat/directives/ack') && req.method() === 'POST') {
			try {
				acks.push(JSON.parse(req.postData() ?? '{}'));
			} catch {
				acks.push({});
			}
		}
	});
	page.on('pageerror', (err) => console.error(`page error: ${err.message}`));

	await page.goto(`${ORIGIN}/gods-eye`, { waitUntil: 'domcontentloaded', timeout: 60_000 });
	await page.waitForFunction(() => !!window.__GODS_EYE__, { timeout: 60_000 });
	const ready = await page.evaluate(() => window.__GODS_EYE__.ready);
	ok('canvas ready', ready?.ok === true, ready?.reason ?? 'phase ready');
	const loaded = await waitFor(page, async () => {
		const s = await snapshot(page);
		return s?.registry?.status === 'ok' && (s.registry.kinds.vehicle ?? 0) > 0 && (s.registry.kinds.camera ?? 0) > 0 ? s : null;
	}, 60_000);
	ok('fused registry answered (vehicles + cameras)', !!loaded, loaded ? JSON.stringify(loaded.registry.kinds) : 'no registry');
	await sleep(3000); // layers draw their first frame

	const vehicle = await stolenVehicle();
	ok('the stolen vehicle is in the city picture', !!vehicle && vehicle.lon != null, vehicle ? `${vehicle.urn} @ ${fmt(vehicle.lon)},${fmt(vehicle.lat)}` : PLATE);

	// ── chip + apply ──────────────────────────────────────────────────────
	if ((run('chip') || run('apply')) && vehicle) {
		const asked = await paletteRun(page, 'ask show me the stolen vehicle on the map');
		ok('palette sends the question', asked);
		const chip = await waitFor(page, async () => ((await chipCount(page)) > 0 ? true : null), STREAM_BUDGET_MS, 500);
		const meta = chip
			? await page.$eval('[data-testid="agent-chip"]', (el) => ({ verb: el.dataset.verb, text: el.textContent ?? '' }))
			: null;
		ok('a streamed directive shows the AI-suggests chip', !!chip && /AI suggests/.test(meta?.text ?? ''), meta ? `${meta.verb}: ${meta.text.replace(/\s+/g, ' ').trim()}` : 'no chip');
		ok('the chip masks the plate', !!meta && !meta.text.includes(PLATE));
		await closePalette(page); // the stream keeps running
		const before = await snapshot(page);
		await sleep(1500);
		const still = await snapshot(page);
		ok('never auto-applied (camera idle while the chip waits)', !!before?.camera && !!still?.camera && Math.abs(before.camera.lon - still.camera.lon) < 1e-6 && still.selection.urn !== vehicle.urn);
		if (chip) await shot(page, 'chip');

		if (run('apply') && chip) {
			const acksBefore = acks.length;
			await page.click('[data-testid="agent-apply"]');
			const landed = await waitFor(page, async () => {
				const s = await snapshot(page);
				return s?.camera &&
					Math.abs(s.camera.lon - vehicle.lon) <= APPLY_TOLERANCE_DEG &&
					Math.abs(s.camera.lat - vehicle.lat) <= APPLY_TOLERANCE_DEG &&
					s.selection.flyOutcome === 'landed'
					? s
					: null;
			}, 10_000);
			const s = landed ?? (await snapshot(page));
			ok(
				`Apply moves the camera within ${APPLY_TOLERANCE_DEG}° of the last lawful sighting`,
				!!landed,
				`camera ${fmt(s?.camera?.lon)},${fmt(s?.camera?.lat)} vs ${fmt(vehicle.lon)},${fmt(vehicle.lat)} · ${s?.selection?.flyOutcome}`,
			);
			ok('Apply selected the vehicle', s?.selection?.urn === vehicle.urn, s?.selection?.urn ?? 'none');
			await sleep(1500);
			const posted = acks.slice(acksBefore);
			ok('exactly one ack posted', posted.length === 1 && posted[0].decision === 'applied', JSON.stringify(posted));
			await shot(page, 'applied');
		}
	}

	// ── dismiss ───────────────────────────────────────────────────────────
	if (run('dismiss') && vehicle) {
		await closeDossier(page);
		const before = await chipCount(page);
		const acksBefore = acks.length;
		await paletteRun(page, 'ask highlight the stolen vehicle on the map');
		const appeared = await waitFor(page, async () => ((await chipCount(page)) > before ? true : null), STREAM_BUDGET_MS, 500);
		await closePalette(page);
		ok('a second proposal arrives', !!appeared);
		if (appeared) {
			const n = await chipCount(page);
			await page.click('[data-testid="agent-dismiss"]');
			const gone = await waitFor(page, async () => ((await chipCount(page)) === n - 1 ? true : null), 5000);
			ok('Dismiss removes the chip', !!gone);
			await sleep(1500);
			const posted = acks.slice(acksBefore);
			ok('Dismiss posts one dismissed ack', posted.length === 1 && posted[0].decision === 'dismissed', JSON.stringify(posted));
		}
	}

	// ── palette fly to a plate ────────────────────────────────────────────
	if (run('palette') && vehicle) {
		await closeDossier(page);
		await paletteRun(page, `fly to ${PLATE}`);
		const selected = await waitFor(page, async () => {
			const s = await snapshot(page);
			return s?.selection?.urn === vehicle.urn ? s : null;
		}, 8000);
		ok(`⌘K 'fly to ${PLATE}' selects ${vehicle.urn}`, !!selected, (await snapshot(page))?.selection?.urn ?? 'none');
		const dossier = await page
			.waitForSelector(`[data-testid="entity-dossier"][data-urn="${vehicle.urn}"]`, { visible: true, timeout: 8000 })
			.then(() => true)
			.catch(() => false);
		ok('its dossier opens', dossier);
		const plate = await page
			.$eval('[data-testid="dossier-plate"] .plate', (el) => ({ text: el.textContent?.trim(), masked: el.dataset.masked }))
			.catch(() => null);
		ok('the plate is masked (no officer named)', plate?.masked === 'true' && plate.text === MASKED, JSON.stringify(plate));
		const raw = await page.evaluate((p) => document.body.innerText.includes(p), PLATE);
		ok('the raw plate appears nowhere on the page', !raw);
		await shot(page, 'palette-dossier');
	}

	// ── explain this ─────────────────────────────────────────────────────
	if (run('explain') && vehicle) {
		const btn = await page.$('[data-testid="dossier-explain-btn"]');
		ok('the dossier offers Explain this', !!btn);
		if (btn) {
			await btn.click();
			const answer = await waitFor(page, async () => {
				const a = await page
					.$eval('[data-testid="dossier-explain-answer"]', (el) => ({ status: el.dataset.status, text: el.textContent?.trim() ?? '' }))
					.catch(() => null);
				return a && a.status !== 'streaming' && a.text.length > 0 ? a : null;
			}, STREAM_BUDGET_MS, 500);
			ok("'Explain this' streams an answer", answer?.status === 'done' && answer.text.length > 20, answer ? `${answer.text.slice(0, 160)}…` : 'no answer');
			ok('the answer is masked', !!answer && !answer.text.includes(PLATE));
			await shot(page, 'explain');
		}
	}

	// ── camera picks ─────────────────────────────────────────────────────
	if (run('cameras')) {
		// a fresh City Command (no share hash): the home view frames the city
		await page.goto(`${ORIGIN}/gods-eye`, { waitUntil: 'domcontentloaded', timeout: 60_000 });
		await page.waitForFunction(() => !!window.__GODS_EYE__, { timeout: 60_000 });
		await page.evaluate(() => window.__GODS_EYE__.ready);
		await waitFor(page, async () => ((await snapshot(page))?.registry?.status === 'ok' ? true : null), 60_000);
		await sleep(4000);
		// the city view, so every EM-CAM is on screen and none under a panel
		const spots = await page.evaluate(() => window.__GODS_EYE__.locate('cam:'));
		const emIds = [...new Set(spots.map((s) => s.id).filter((id) => id.includes('EM-CAM')))];
		ok('locate(cam) reports EM-CAM cameras', emIds.length > 0, emIds.join(', '));
		let selectedAll = 0;
		for (const id of emIds) {
			await closeDossier(page); // the next spot is clear of the drawer
			await sleep(400);
			const now = await page.evaluate(() => window.__GODS_EYE__.locate('cam:'));
			const spot = now.find((s) => s.id === id);
			if (!spot) {
				ok(`camera ${id} clickable`, false, 'covered or off screen');
				continue;
			}
			await page.mouse.click(spot.x, spot.y);
			const urn = `camera:${id.slice('cam:'.length)}`;
			const sel = await waitFor(page, async () => ((await snapshot(page))?.selection?.urn === urn ? true : null), 4000, 150);
			if (sel) selectedAll++;
			ok(`click on ${id} selects ${urn}`, !!sel, (await snapshot(page))?.selection?.urn ?? 'none');
		}
		ok('every EM-CAM click selected its camera', emIds.length > 0 && selectedAll === emIds.length, `${selectedAll}/${emIds.length}`);
		ok('still on City Command', new URL(page.url()).pathname === '/gods-eye', page.url());
		await shot(page, 'camera-pick');
	}
} finally {
	await browser.close();
}

if (failures > 0) {
	console.error(`\n${failures} agent-canvas failures`);
	process.exit(1);
}
console.log('\nall agent-canvas assertions passed');
