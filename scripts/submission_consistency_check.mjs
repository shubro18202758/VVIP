#!/usr/bin/env node
/**
 * submission_consistency_check.mjs — the submission-consistency oracle.
 *
 * Proves the submission material (submission/DECK.md, DEMO_VIDEO_SCRIPT.md,
 * make_deck.py, Gujarat_Veer_Deck.pptx) agrees with the live tree and the
 * on-disk measurement artifacts it cites — the grounding-audit registry as
 * code:
 *
 *   root-serves-cockpit-200              routes and design-smoke agree on
 *                                        the cockpit topology (GET / = 200,
 *                                        data-template="convoy-command");
 *                                        no route rewrite anywhere in the
 *                                        command-deck src
 *   no-stale-root-redirect-claims        none of the interface-of-record
 *                                        documents still describes the old
 *                                        301-to-/ops behaviour
 *   doc-paths-exist                      every path referenced by DECK.md /
 *                                        DEMO_VIDEO_SCRIPT.md / make_deck.py
 *                                        resolves on the tree
 *   impact-json-single-source            impact_results.json pins + the
 *                                        slide-5 figures (incl. recomputed
 *                                        fuel/CO2) chain to it alone
 *   bench-figures-match-artifacts        every bench figure quoted by the
 *                                        deck equals bench_results.json /
 *                                        bench_adversarial_report.json
 *   showcase-pursuit-figures-derivable   every pursuit/showcase figure
 *                                        quoted by the docs equals the
 *                                        current artifacts
 *   pixel-proofs-match-shot-logs         every colour/count proof equals
 *                                        shots_city.log / shots_manifest.json
 *                                        (probes are MANDATORY — a wording
 *                                        drift in the artifact is a failure,
 *                                        not a silent skip); any colour/
 *                                        entity/card figure with no artifact
 *                                        backing fails across deck+adjacent
 *                                        docs
 *   adjacent-docs-match-artifacts        the adjacent interface-of-record
 *                                        docs (VVIP/TECHNICAL_DOCS.md,
 *                                        VVIP/README.md) carry no figure that
 *                                        contradicts the showcase/bench
 *                                        artifacts they cite
 *   screens-verify-40of40                the screens manifest records
 *                                        40/40 ok incl. the driver countdown
 *   showcase-artifacts-single-run        the docs cannot present two
 *                                        showcase runs as one — the split
 *                                        must be cited with both stamps
 *   pytest-counts-match-deck             live gate-venv collection equals
 *                                        the deck's AND the adjacent docs'
 *                                        quoted counts
 *   e2e-11-checks-gate                   the citizen e2e keeps its 11 boolean
 *                                        checks and stays wired into
 *                                        verify_all.sh
 *   migrations-007-011-exist             migrations 001..011 on disk match
 *                                        the deck's "007–011" claim
 *   smoke-models-ports-env-overridable   the model-tier endpoints keep the
 *                                        ${VAR:-loopback} pattern
 *   showcase-endpoints-env-overridable   showcase_run.sh keeps bash -n, the
 *                                        ${VAR:-loopback} constants and the
 *                                        cockpit health contract
 *   pptx-claims-fresh                    the rendered PPTX carries the
 *                                        corrected figures, not the stale
 *                                        ones
 *   pdf-claims-fresh                     the rendered PDF (pdftotext) carries
 *                                        the same corrected figures and is
 *                                        not older than the PPTX
 *   verify-all-tier-inventory            tier functions AND call sites
 *                                        intact in order; warns (does not
 *                                        fail) that design-smoke is not a
 *                                        verify_all tier — verify_all.sh is
 *                                        out of this stream's edit scope
 *
 * Zero dependencies: node stdlib only (fs, path, child_process, zlib).
 * Every path resolves from this file's own location — any cwd works.
 *
 * Usage:
 *   node VVIP/scripts/submission_consistency_check.mjs            # the gate
 *   node VVIP/scripts/submission_consistency_check.mjs --selftest # prove
 *        the failure path: copies the read set into a temp fixture, injects
 *        known-bad mutations, and requires the oracle to FAIL on each.
 *
 * Exit 0 = all green (selftest: failure path proven); exit 1 = failures,
 * each printed as file:line [check-id] what.
 */

import {
	existsSync,
	mkdirSync,
	mkdtempSync,
	readFileSync,
	readdirSync,
	rmSync,
	writeFileSync,
	copyFileSync,
	statSync,
	symlinkSync
} from 'node:fs';
import { spawnSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import zlib from 'node:zlib';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..', '..'); // the workspace holding VVIP/ + submission/

/* ─────────────────────────── small helpers ─────────────────────────── */

const fmt = (n) => new Intl.NumberFormat('en-US').format(n);
const roundTo = (v, step) => Math.round(v / step) * step;
const EN = '\u2013'; // en dash, the docs' range separator

class Ctx {
	constructor(root, opts = {}) {
		this.root = root;
		this.opts = opts;
		this.failures = [];
		this.warnings = [];
		this.details = [];
	}
	read(rel) {
		try {
			return readFileSync(path.join(this.root, rel), 'utf8');
		} catch {
			return null;
		}
	}
	exists(rel) {
		return existsSync(path.join(this.root, rel));
	}
	json(rel) {
		try {
			return JSON.parse(readFileSync(path.join(this.root, rel), 'utf8'));
		} catch (e) {
			this.fail(rel, 1, 'unreadable JSON', String(e.message || e));
			return null;
		}
	}
	fail(rel, line, check, msg) {
		this.failures.push({ check, file: rel, line, msg });
	}
	warn(msg) {
		this.warnings.push(msg);
	}
	ok(check, detail) {
		this.details.push({ check, detail });
	}
	/** first line of `text` containing `needle` (1-based), else fallback */
	lineOf(rel, text, needle, fallback = 1) {
		const i = text.indexOf(needle);
		if (i < 0) return fallback;
		return text.slice(0, i).split('\n').length;
	}
	/** assert `needle` occurs in `docRel`'s text; fail pointing at anchor.
	 *  The hay is whitespace-normalised first — markdown wraps lines mid-phrase,
	 *  and the figures must match regardless of where the wrap falls. */
	expectIn(docRel, text, needle, check, msg, anchor) {
		const hay = text.replace(/\s+/g, ' ');
		const want = needle.replace(/\s+/g, ' ');
		if (!hay.includes(want)) {
			const line = anchor && text.indexOf(anchor) >= 0
				? this.lineOf(docRel, text, anchor)
				: 1;
			this.fail(docRel, line, check, `${msg} (expected "${needle}")`);
			return false;
		}
		return true;
	}
}

/* ─────────────────── minimal ZIP reader (for the PPTX) ───────────────────
 * OOXML is a zip; node's zlib inflates raw deflate, so a ~70-line central-
 * directory walker is enough to pull ppt/slides/slideN.xml — no deps.     */

function zipEntries(buf) {
	// locate the End Of Central Directory record (scan back — comment size varies)
	let eocd = -1;
	for (let i = buf.length - 22; i >= Math.max(0, buf.length - 22 - 65536); i--) {
		if (buf.readUInt32LE(i) === 0x06054b50) { eocd = i; break; }
	}
	if (eocd < 0) throw new Error('zip EOCD not found');
	const count = buf.readUInt16LE(eocd + 10);
	let ptr = buf.readUInt32LE(eocd + 16); // central directory offset
	const out = [];
	for (let n = 0; n < count; n++) {
		if (buf.readUInt32LE(ptr) !== 0x02014b50) throw new Error('bad central directory entry');
		const method = buf.readUInt16LE(ptr + 10);
		const compSize = buf.readUInt32LE(ptr + 20);
		const nameLen = buf.readUInt16LE(ptr + 28);
		const extraLen = buf.readUInt16LE(ptr + 30);
		const commentLen = buf.readUInt16LE(ptr + 32);
		const localOff = buf.readUInt32LE(ptr + 42);
		const name = buf.slice(ptr + 46, ptr + 46 + nameLen).toString('utf8');
		// local header carries its OWN name/extra lengths — use those for data offset
		const lNameLen = buf.readUInt16LE(localOff + 26);
		const lExtraLen = buf.readUInt16LE(localOff + 28);
		const dataStart = localOff + 30 + lNameLen + lExtraLen;
		out.push({ name, method, data: buf.slice(dataStart, dataStart + compSize) });
		ptr += 46 + nameLen + extraLen + commentLen;
	}
	return out;
}

function zipFileText(buf, nameRe) {
	const entries = zipEntries(buf);
	return entries
		.filter((e) => nameRe.test(e.name))
		.map((e) => (e.method === 8 ? zlib.inflateRawSync(e.data) : e.data).toString('utf8'))
		.join('\n');
}

/* ─────────────────── path-reference extraction (doc paths) ─────────────── */

const PATH_BASES = ['.', 'VVIP', 'submission', 'VVIP/services/command-deck'];
const BARE_BASES = ['.', 'VVIP', 'VVIP/scripts', 'VVIP/showcase', 'submission',
	'VVIP/services/command-deck/tests/artifacts/screens'];
const BARE_EXT = /\.(md|json|py|sh|mjs|sql|png|pptx|pdf|log|yml|yaml|ts|svelte|js|txt)$/;
const KNOWN_PREFIX = /^(VVIP|submission|services|scripts|showcase|frontend|tests|docs|infra)\//;
const EXT_FINAL = /\.(md|json|py|sh|mjs|sql|png|pptx|pdf|log|yml|yaml|ts|svelte|js|txt)$/;

/** expand one level of `{a,b,c}` brace alternatives (screenshots naming) */
function expandBraces(token) {
	const m = token.match(/^(.*)\{([^{}]*)\}(.*)$/);
	if (!m) return [token];
	return m[2].split(',').flatMap((alt) => expandBraces(`${m[1]}${alt}${m[3]}`));
}

const PATH_TOKEN_RE = /[A-Za-z0-9_.-]+(?:\/[A-Za-z0-9_.{}(),-]+)+/g;

function cleanToken(tok) {
	return tok.replace(/[),.;:'"]+$/, '').replace(/\/+$/, ''); // trailing prose punctuation / slashes
}

function tokenIsPathlike(tok) {
	const t = cleanToken(tok);
	if (!t || t.includes(' ') || t.includes('\n')) return false;
	if (/[<>]/.test(t)) return false; // "<State>_<Team>_Deck.pdf" placeholders
	if (/^[A-Za-z]+:/.test(t)) return false; // scheme or host:port
	if (t.startsWith('/')) return false; // route, not file
	if (/[*+]/.test(t)) return false; // "Z+/Z/Y" prose
	const first = t.split('/')[0];
	if (/^\d+$/.test(first) || first.length < 2) return false; // "1/6", "40/40", "tok/s", "L/yr", "Z/Y"
	// a slashed token is a path only when it names a rooted tree prefix or a file extension
	return KNOWN_PREFIX.test(t) || EXT_FINAL.test(t.slice(t.lastIndexOf('/') + 1));
}

function bareIsFilename(s) {
	const t = s.trim().replace(/[),.;:'"]+$/, '');
	return /^[A-Za-z0-9_][A-Za-z0-9_.-]*\.(md|json|py|sh|mjs|sql|png|pptx|pdf|log|yml|yaml|ts|svelte|js|txt)$/.test(t)
		&& !t.includes('/') && !/^[A-Za-z]+:/.test(t) && !t.includes(' ') && !/[<>]/.test(t);
}

/** collect {token, docRel, line} path references from a markdown doc (backticks) */
function mdPathRefs(text, docRel) {
	const out = [];
	const lines = text.split('\n');
	lines.forEach((line, idx) => {
		for (const bt of line.matchAll(/`([^`]+)`/g)) {
			for (const tok of bt[1].match(PATH_TOKEN_RE) ?? []) {
				if (tokenIsPathlike(tok)) out.push({ token: tok, docRel, line: idx + 1 });
			}
			// bare filenames inside backticks (e.g. `verify_all.sh`)
			if (bareIsFilename(bt[1])) out.push({ token: bt[1].trim(), docRel, line: idx + 1, bare: true });
		}
	});
	return out;
}

/** path references from a python source (whole-text tokens + bare filenames in strings) */
function pyPathRefs(text, docRel) {
	const out = [];
	const lines = text.split('\n');
	lines.forEach((line, idx) => {
		const inString = [...line.matchAll(/["']([^"'\n]+)["']/g)].map((m) => m[1]);
		const hay = inString.join(' ');
		for (const tok of hay.match(PATH_TOKEN_RE) ?? []) {
			if (tokenIsPathlike(tok)) out.push({ token: tok, docRel, line: idx + 1 });
		}
		for (const s of inString) {
			if (bareIsFilename(s)) out.push({ token: s.trim(), docRel, line: idx + 1, bare: true });
		}
	});
	return out;
}

function resolveToken(c, ref) {
	const bases = ref.bare ? BARE_BASES : PATH_BASES;
	for (const b of bases) {
		for (const t of expandBraces(cleanToken(ref.token))) {
			if (c.exists(path.join(b, t))) return true;
		}
	}
	return false;
}

/* ─────────────────────────── the check registry ────────────────────────── */

const DECK = 'submission/DECK.md';
const DEMO = 'submission/DEMO_VIDEO_SCRIPT.md';
const MAKER = 'submission/make_deck.py';
const PPTX = 'submission/Gujarat_Veer_Deck.pptx';
const PDF = 'submission/Gujarat_Veer_Deck.pdf';
const TECH_DOCS = 'VVIP/TECHNICAL_DOCS.md';
const README = 'VVIP/README.md';
const ROUTE_ROOT = 'VVIP/services/command-deck/src/routes/(deck)/+page.svelte';
const DESIGN_SMOKE = 'VVIP/services/command-deck/tests/design-smoke.mjs';
const SCREENS_MANIFEST = 'VVIP/services/command-deck/tests/artifacts/screens/screens_manifest.json';
const E2E = 'VVIP/frontend/tests/e2e/citizen-green-corridor.e2e.mjs';
const VERIFY_ALL = 'VVIP/scripts/verify_all.sh';
const SHOWCASE_RUN = 'VVIP/scripts/showcase_run.sh';
const FLEET_SMOKE = 'VVIP/scripts/fleet_smoke.sh';
const SMOKE_MODELS = 'VVIP/scripts/smoke_models.sh';
const CLAIM_FILES = [DECK, DEMO, MAKER, README, TECH_DOCS, FLEET_SMOKE, SHOWCASE_RUN];

const STALE_PATTERNS = [
	[/\b301\b/, 'describes the retired 301-to-/ops behaviour'],
	[/\bredirect(s|ed|ing)?\b/i, 'describes a redirecting deck root (the tree serves / directly)'],
	[/lands on [`*]?\/ops/, 'describes `/` landing on /ops (the tree serves the cockpit at /)']
];

function walkFiles(dirRel, c, out = []) {
	const abs = path.join(c.root, dirRel);
	let entries;
	try { entries = readdirSync(abs, { withFileTypes: true }); } catch { return out; }
	for (const e of entries) {
		const rel = `${dirRel}/${e.name}`;
		if (e.isDirectory()) {
			if (['node_modules', '.svelte-kit', 'build', '.git'].includes(e.name)) continue;
			walkFiles(rel, c, out);
		} else out.push(rel);
	}
	return out;
}

/** gate-venv pytest collection exactly as verify_all.sh discovers suites */
function collectPytest(c) {
	if (c.opts.pytestStub) return c.opts.pytestStub;
	const py = path.join(c.root, 'VVIP/.venvs/gate/bin/python');
	if (!existsSync(py)) {
		c.fail('VVIP/.venvs/gate/bin/python', 1, 'pytest-counts-match-deck',
			'gate venv missing — run verify_all.sh once to bootstrap it, then re-run the oracle');
		return null;
	}
	const suites = [];
	(function find(dirRel) {
		let entries;
		try { entries = readdirSync(path.join(c.root, dirRel), { withFileTypes: true }); } catch { return; }
		for (const e of entries) {
			const rel = `${dirRel}/${e.name}`;
			if (e.isDirectory()) {
				if (['node_modules', '.venvs', '.venv', 'build', '.git'].includes(e.name)) continue;
				find(rel);
			} else if (e.isFile() && /^test_.*\.py$/.test(e.name)) suites.push(path.dirname(rel));
		}
	})('VVIP');
	suites.sort();
	const perSuite = {};
	let total = 0;
	for (const suite of [...new Set(suites)]) {
		const cwd = path.join(c.root, suite);
		const srcDir = path.join(path.dirname(cwd), 'src');
		const env = { ...process.env };
		if (existsSync(srcDir)) env.PYTHONPATH = srcDir;
		const r = spawnSync(py, ['-m', 'pytest', '.', '--collect-only', '-q'], { cwd, env, encoding: 'utf8', timeout: 120000 });
		const m = (r.stdout || '').match(/(\d+) tests? collected(?:, (\d+) errors?)?/);
		if (!m) {
			c.fail(suite, 1, 'pytest-counts-match-deck',
				`pytest --collect-only did not report a count (exit ${r.status}): ${String((r.stdout || '') + (r.stderr || '')).slice(-200)}`);
			continue;
		}
		const n = Number(m[1]);
		const errs = Number(m[2] || 0);
		if (errs > 0) c.warn(`pytest collection: ${suite} — ${n} collected, ${errs} error(s) (deck counts quote collected tests)`);
		perSuite[suite] = n;
		total += n;
	}
	return { total, perSuite };
}

const CHECKS = [
	{
		id: 'root-serves-cockpit-200',
		run(c) {
			const route = c.read(ROUTE_ROOT) ?? '';
			c.expectIn(ROUTE_ROOT, route, 'data-template="convoy-command"', c.id,
				'the root route must mount the Convoy Command cockpit template');
			const smoke = c.read(DESIGN_SMOKE) ?? '';
			let good = true;
			good &= c.expectIn(DESIGN_SMOKE, smoke, 'data-template="convoy-command"', c.id,
				'design-smoke must assert the cockpit marker');
			good &= c.expectIn(DESIGN_SMOKE, smoke, '/ is the Convoy Command cockpit', c.id,
				'design-smoke must name the cockpit contract');
			good &= c.expectIn(DESIGN_SMOKE, smoke, "const EIGHT = ['/', '/ops'", c.id,
				'design-smoke must probe / AND /ops as live 200 URLs');
			// no route rewrite anywhere in the deck src — the only tolerated
			// mention is fetch()'s redirect:'manual' in hooks.server.ts
			const srcFiles = walkFiles('VVIP/services/command-deck/src', c);
			let clean = true;
			for (const f of srcFiles) {
				const t = c.read(f) ?? '';
				t.split('\n').forEach((line, i) => {
					if (/\bredirect(s|ed|ing)?\b/i.test(line) || /\b301\b/.test(line)) {
						if (f === 'VVIP/services/command-deck/src/hooks.server.ts' && /redirect:\s*'manual'/.test(line)) return;
						c.fail(f, i + 1, c.id, `route-rewrite language in deck src: ${line.trim().slice(0, 90)}`);
						clean = false;
					}
				});
			}
			if (clean && srcFiles.length) c.ok(c.id, `cockpit marker in route + design-smoke; no route rewrite across ${srcFiles.length} src files`);
			// NOTE (not failed here): the LIVE halves — `node tests/design-smoke.mjs`
			// against a built deck, screens-verify re-run — belong to the browser
			// tiers, not a static oracle.
		}
	},
	{
		id: 'no-stale-root-redirect-claims',
		run(c) {
			let hits = 0;
			for (const rel of CLAIM_FILES) {
				const t = c.read(rel);
				if (t == null) { c.fail(rel, 1, c.id, 'file missing — cannot scan for stale claims'); hits++; continue; }
				t.split('\n').forEach((line, i) => {
					for (const [re, why] of STALE_PATTERNS) {
						if (re.test(line)) {
							c.fail(rel, i + 1, c.id, `${why}: ${line.trim().slice(0, 100)}`);
							hits++;
						}
					}
				});
			}
			if (!hits) c.ok(c.id, `no 301/redirect claims across ${CLAIM_FILES.length} interface-of-record files`);
		}
	},
	{
		id: 'doc-paths-exist',
		run(c) {
			const refs = [];
			for (const [rel, fn] of [[DECK, mdPathRefs], [DEMO, mdPathRefs], [MAKER, pyPathRefs]]) {
				const t = c.read(rel);
				if (t == null) { c.fail(rel, 1, c.id, 'doc missing — cannot extract path references'); continue; }
				refs.push(...fn(t, rel));
			}
			let missing = 0;
			for (const ref of refs) {
				if (!resolveToken(c, ref)) {
					c.fail(ref.docRel, ref.line, c.id, `referenced path does not exist on the tree: ${ref.token}`);
					missing++;
				}
			}
			if (!missing) c.ok(c.id, `${refs.length} referenced paths all resolve`);
		}
	},
	{
		id: 'impact-json-single-source',
		run(c) {
			const imp = c.json('VVIP/scripts/impact_results.json');
			if (!imp) return;
			const deck = c.read(DECK) ?? '';
			const maker = c.read(MAKER) ?? '';
			// the audit's pins — the artifact this revision's deck was built from
			const pins = [
				['scenarios.central.commuter_minutes_baseline', imp.scenarios?.central?.commuter_minutes_baseline, 570937.5],
				['scenarios.central.commuter_minutes_platform', imp.scenarios?.central?.commuter_minutes_platform, 5709.4],
				['scenarios.central.vehicles_delayed_baseline', imp.scenarios?.central?.vehicles_delayed_baseline, 30450],
				['scenarios.central.vehicles_delayed_platform', imp.scenarios?.central?.vehicles_delayed_platform, 3045],
				['scenarios.conservative.reduction_factor', imp.scenarios?.conservative?.reduction_factor, 25.0],
				['scenarios.central.reduction_factor', imp.scenarios?.central?.reduction_factor, 100.0],
				['scenarios.optimistic.reduction_factor', imp.scenarios?.optimistic?.reduction_factor, 506.2],
				['ambulance.trips_crossing_per_movement_day', imp.ambulance?.trips_crossing_per_movement_day, 12.0],
				['ambulance.minutes_returned_per_movement_day', imp.ambulance?.minutes_returned_per_movement_day, 132],
				['annualization.commuter_hours_returned_per_year_central', imp.annualization?.commuter_hours_returned_per_year_central, 2355117]
			];
			for (const [key, got, want] of pins) {
				if (got !== want) c.fail('VVIP/scripts/impact_results.json', 1, c.id,
					`${key} = ${got}, expected ${want} (the deck revision was built from these values — regenerate the run and the deck together)`);
			}
			// make_deck's annualisation inputs must stay the artifact + the 250 constant
			c.expectIn(MAKER, maker, 'IMPACT_A = 250', c.id, 'make_deck.py must keep the 250 movement-days constant');
			c.expectIn(MAKER, maker, 'c["idle_liters_baseline"] - c["idle_liters_platform"]', c.id,
				'make_deck.py must derive fuel from the artifact litres');
			// recompute fuel/CO2 exactly as make_deck does and require the deck text
			const fuelL = (imp.scenarios.central.idle_liters_baseline - imp.scenarios.central.idle_liters_platform) * 250;
			const co2T = (fuelL * 2.5) / 1000;
			const deckFigures = [
				[`~${fmt(roundTo(imp.scenarios.central.commuter_minutes_baseline, 1000))}`, 'central baseline commuter-minutes'],
				[`~${fmt(roundTo(imp.scenarios.central.commuter_minutes_platform, 100))}`, 'central platform commuter-minutes'],
				[`~${fmt(imp.scenarios.central.vehicles_delayed_baseline)}`, 'vehicles baseline'],
				[`~${fmt(imp.scenarios.central.vehicles_delayed_platform)}`, 'vehicles platform'],
				[`${fmt(Math.round(imp.scenarios.conservative.reduction_factor))}\u00d7\u2013${fmt(Math.round(imp.scenarios.optimistic.reduction_factor))}\u00d7 (central ${fmt(Math.round(imp.scenarios.central.reduction_factor))}\u00d7)`, 'reduction-factor range'],
				[`${(imp.annualization.commuter_hours_returned_per_year_central / 1e6).toFixed(2)} million commuter-hours`, 'annual hours'],
				[`${(fuelL / 1e6).toFixed(2)} million litres`, 'annual idle fuel (recomputed from the artifact)'],
				[`~${fmt(Math.round(co2T))} tonnes`, 'annual CO2 (recomputed from the artifact)'],
				[`~${fmt(imp.ambulance.minutes_returned_per_year)} golden-hour minutes/year`, 'golden-hour minutes'],
				[`${fmt(imp.ambulance.trips_crossing_per_movement_day)} intersecting trips/day \u00d7 ${fmt(imp.ambulance.minutes_returned_per_movement_day / imp.ambulance.trips_crossing_per_movement_day)} min`, 'ambulance arithmetic']
			];
			for (const [needle, what] of deckFigures) {
				c.expectIn(DECK, deck, needle, c.id, `slide-5 figure "${what}" does not chain to impact_results.json`, 'Per movement');
			}
			c.ok(c.id, `slide-5 chains to impact_results.json (fuel ${fmt(fuelL)} L, CO2 ${co2T} t recomputed)`);
		}
	},
	{
		id: 'bench-figures-match-artifacts',
		run(c) {
			const b = c.json('VVIP/scripts/bench_results.json');
			const a = c.json('VVIP/scripts/bench_adversarial_report.json');
			if (!b || !a) return;
			const deck = c.read(DECK) ?? '';
			const maker = c.read(MAKER) ?? '';
			const ss = a.score_summary ?? {};
			const deckNeedles = [
				[`${b.decisionTokSec} tok/s`, 'decider tok/s'],
				[`p99 ${b.decisionP99Ms} ms`, 'decider p99'],
				[`${b.gemmaTokSec} tok/s`, 'gemma tok/s'],
				[`${b.embedMs} ms`, 'embed latency'],
				[`${b.memGB} GiB resident`, 'resident memory'],
				[`${b.scenariosPassed}/${b.scenariosTotal} passed (score ${b.scenarioScore})`, 'adversarial suite'],
				[`avg ${a.avg_latency_sec} s / max ${a.max_latency_sec} s`, 'adversarial latencies'],
				[`tool coverage **${ss.tool_validity}**`, 'tool coverage'],
				[`tool coverage ${ss.tool_validity}`, 'roadmap tool coverage'],
				[`keyword coverage **${ss.keyword_coverage}**`, 'keyword coverage (bench artifact says ' + ss.keyword_coverage + ')'],
				[`keyword coverage ${ss.keyword_coverage}`, 'roadmap keyword coverage'],
				[`${a.timestamp.slice(0, 16)}Z`, 'bench artifact timestamp']
			];
			for (const [needle, what] of deckNeedles) {
				c.expectIn(DECK, deck, needle, c.id, `deck bench figure "${what}" does not match the artifact`, 'Bench re-executed');
			}
			for (const needle of [String(b.decisionTokSec), String(b.decisionP99Ms), String(b.gemmaTokSec),
				String(b.embedMs), String(b.memGB), `${b.scenariosPassed}/${b.scenariosTotal}`,
				String(a.avg_latency_sec), String(a.max_latency_sec),
				`tool coverage ${ss.tool_validity}`, `keyword coverage ${ss.keyword_coverage}`]) {
				c.expectIn(MAKER, maker, needle, c.id, `make_deck.py bench figure does not match the artifact (expected "${needle}")`);
			}
			c.ok(c.id, `deck + make_deck bench figures equal the artifacts (keyword coverage ${ss.keyword_coverage})`);
		}
	},
	{
		id: 'showcase-pursuit-figures-derivable',
		run(c) {
			const chain = c.json('VVIP/showcase/pursuit_chain.json');
			const twin = c.json('VVIP/showcase/twin_audit.json');
			const sig = c.json('VVIP/showcase/signal_policy.json');
			const res = c.json('VVIP/showcase/showcase_results.json');
			if (!chain || !twin || !sig || !res) return;
			const deck = c.read(DECK) ?? '';
			const demo = c.read(DEMO) ?? '';
			const maker = c.read(MAKER) ?? '';

			const fired = chain.recommendation?.fired_rules ?? [];
			const count = (r) => fired.filter((x) => x === r).length;
			const chosen = chain.recommendation?.chosen?.text ?? '';
			const cm = chosen.match(/read at (\d+) km\/h in a (\d+) km\/h segment/);
			const top = chain.interception?.top ?? {};
			const tracePts = chain.trace?.points;
			const speeds = chain.sightings?.speeds_kmh ?? [];
			const counts = res.counts ?? {};

			const deckNeedles = [
				[`\`R_SPD_SEGMENT\` \u00d7${count('R_SPD_SEGMENT')}`, 'R_SPD_SEGMENT fired count'],
				[`\`R_SIGNAL_VIOLATION\` \u00d7${count('R_SIGNAL_VIOLATION')}`, 'R_SIGNAL_VIOLATION fired count'],
				[cm ? `read at ${cm[1]} km/h in a ${cm[2]} km/h segment` : 'read at ? km/h in a ? km/h segment', 'chosen e-challan text'],
				[`${chain.interception?.n_junctions} choke junctions`, 'interception junction count'],
				[`junction ${top.junction_id}, signal-controlled, score ${top.score}`, 'top choke junction'],
				[`${tracePts}-position trace`, 'live trace positions'],
				[`${Math.min(...speeds)}${EN}${Math.max(...speeds)} km/h`, 'ANPR speed range this run'],
				[`${counts.decisions_logged_audit_rows_this_run} audited decisions`, 'decisions this run'],
				[`(${counts.situation_log_rows_this_run} situation + ${counts.param_override_rows_this_run} parameter)`, 'decision split'],
				[`${counts.band_transitions_this_run} band transitions`, 'band transitions'],
				[`${counts.green_corridor_windows} green-corridor windows`, 'green-corridor windows'],
				[`${twin.replications}-replication twin`, 'twin replications'],
				[`${twin.ambulance_delay_sec_mean.toFixed(2)} s`, 'twin ambulance delay'],
				[`${sig.replay.speed_mae_kmh} km/h`, 'twin replay MAE'],
				[`vs the ${sig.replay.mae_tolerance_kmh.toFixed(1)} gate`, 'MAE gate']
			];
			for (const [needle, what] of deckNeedles) {
				c.expectIn(DECK, deck, needle, c.id, `deck pursuit/showcase figure "${what}" does not match the current artifact`, 'flagship showcase');
			}
			const demoNeedles = [
				[cm ? `${cm[1]} km/h in a ${cm[2]} km/h segment` : '? km/h in a ? km/h segment', 'chosen e-challan text'],
				[`${tracePts} timed positions`, 'trace positions'],
				[`junction ${top.junction_id}`, 'top choke junction']
			];
			for (const [needle, what] of demoNeedles) {
				c.expectIn(DEMO, demo, needle, c.id, `video-script figure "${what}" does not match the current artifact`, 'BEAT 4');
			}
			const makerNeedles = [
				[`R_SPD_SEGMENT \u00d7${count('R_SPD_SEGMENT')}`, 'fired count'],
				[`R_SIGNAL_VIOLATION \u00d7${count('R_SIGNAL_VIOLATION')}`, 'fired count'],
				[cm ? `read at ${cm[1]} km/h in a ${cm[2]} km/h segment` : '?', 'chosen text'],
				[`junction ${top.junction_id}, signal-controlled, score ${top.score}`, 'top junction'],
				[`${tracePts}-position trace`, 'trace positions'],
				[`${Math.min(...speeds)}${EN}${Math.max(...speeds)} km/h`, 'speed range']
			];
			for (const [needle, what] of makerNeedles) {
				c.expectIn(MAKER, maker, needle, c.id, `make_deck.py figure "${what}" does not match the current artifact`);
			}
			// band easing: the transition row the video script cites
			const eased = (c.json('VVIP/showcase/band_easing.json') ?? []).find((o) => o.transitioned);
			if (eased) c.expectIn(DEMO, demo, `n=${eased.n}`, c.id, 'band-easing transition count', 'band_easing.json');
			c.ok(c.id, `pursuit figures derivable (fired ${fired.length}, chosen "${chosen.slice(0, 40)}…", trace ${tracePts})`);
		}
	},
	{
		id: 'pixel-proofs-match-shot-logs',
		run(c) {
			const cityLog = c.read('VVIP/showcase/shots_city.log') ?? '';
			const deck = c.read(DECK) ?? '';
			const man = c.json('VVIP/showcase/shots_manifest.json');
			if (!man) return;
			// probes are MANDATORY: if the artifact's wording drifts so a probe
			// can't extract the figure, that is a failure — never a silent skip
			// of the corresponding deck claim.
			const probe = (rel, text, re, what) => {
				const m = text.match(re);
				if (!m) {
					c.fail(rel, 1, c.id, `artifact probe failed: cannot extract "${what}" via ${re} — artifact wording drifted?`);
					return null;
				}
				return m;
			};
			const ents = probe('VVIP/showcase/shots_city.log', cityLog, /entities=(\d+)/, 'city entity count');
			const cityCol = probe('VVIP/showcase/shots_city.log', cityLog, /png colours=(\d+)/, 'city PNG colour count');
			const detail = man.shots?.pursuitTrace?.checks?.['png-nonblank']?.detail || '';
			const pCol = probe('VVIP/showcase/shots_manifest.json', detail, /png colours=(\d+)/, 'pursuit PNG colour count');
			const cit = man.shots?.citizen?.checks || {};
			const citCards = probe('VVIP/showcase/shots_manifest.json', cit['live-restriction-shown']?.detail || '', /(\d+) live-source event card/, 'citizen live-source card count');
			const citCol = probe('VVIP/showcase/shots_manifest.json', cit['png-nonblank']?.detail || '', /png colours=(\d+)/, 'citizen PNG colour count');
			const provenColours = new Set();
			const provenEntities = new Set();
			const provenCards = new Set();
			const needles = [];
			if (ents) { needles.push([`${ents[1]} entities`, 'city entities (shots_city.log)']); provenEntities.add(ents[1]); }
			if (cityCol) { needles.push([`${fmt(Number(cityCol[1]))}-colour`, 'city PNG colour proof (shots_city.log)']); provenColours.add(fmt(Number(cityCol[1]))); }
			if (pCol) { needles.push([`${fmt(Number(pCol[1]))}-colour`, 'pursuit PNG colour proof (shots_manifest.json)']); provenColours.add(fmt(Number(pCol[1]))); }
			if (citCards) { needles.push([`${citCards[1]} live-source event cards`, 'citizen card count (shots_manifest.json)']); provenCards.add(citCards[1]); }
			if (citCol) { needles.push([`${fmt(Number(citCol[1]))}-colour`, 'citizen PNG colour proof (shots_manifest.json)']); provenColours.add(fmt(Number(citCol[1]))); }
			for (const [needle, what] of needles) {
				c.expectIn(DECK, deck, needle, c.id, `screenshot figure "${what}" does not match the artifact`, 'Screenshots');
			}
			// backstop across the deck + adjacent docs: every quoted colour /
			// entity / card figure must be one of the proven counts — this is
			// the guard that fires on figures with no artifact backing (the
			// retired "17,801-colour decode proof" class)
			for (const rel of [DECK, TECH_DOCS, README]) {
				const t = c.read(rel);
				if (t == null) { c.fail(rel, 1, c.id, 'doc missing — cannot backstop quoted figures'); continue; }
				t.split('\n').forEach((line, i) => {
					for (const m of line.matchAll(/(\d[\d,]*)-colour/g)) {
						if (!provenColours.has(m[1])) c.fail(rel, i + 1, c.id, `"${m[1]}-colour" claim has no backing colour count in shots_city.log / shots_manifest.json`);
					}
					for (const m of line.matchAll(/PNG (\d[\d,]*)(?: distinct)? colou?rs/g)) {
						if (!provenColours.has(m[1])) c.fail(rel, i + 1, c.id, `"PNG ${m[1]} colours" claim has no backing colour count in shots_city.log / shots_manifest.json`);
					}
					for (const m of line.matchAll(/(\d[\d,]*) distinct colou?rs/g)) {
						if (!provenColours.has(m[1])) c.fail(rel, i + 1, c.id, `"${m[1]} distinct colours" claim has no backing colour count in the shot logs`);
					}
					for (const m of line.matchAll(/(\d+) entities/g)) {
						if (!provenEntities.has(m[1])) c.fail(rel, i + 1, c.id, `"${m[1]} entities" claim does not match the shots_city.log entity count`);
					}
					for (const m of line.matchAll(/(\d+)\/(\d+) live-source/g)) {
						if (!provenCards.has(m[1]) || m[1] !== m[2]) c.fail(rel, i + 1, c.id, `"${m[1]}/${m[2]} live-source" claim does not match the shots_manifest card count`);
					}
					for (const m of line.matchAll(/(\d+) live-source event cards/g)) {
						if (!provenCards.has(m[1])) c.fail(rel, i + 1, c.id, `"${m[1]} live-source event cards" claim does not match the shots_manifest card count`);
					}
				});
			}
			c.ok(c.id, `colour/count proofs match the shot logs (colours ${[...provenColours].join(', ') || '—'}, entities ${[...provenEntities].join(',') || '—'}, cards ${[...provenCards].join(',') || '—'})`);
		}
	},
	{
		id: 'adjacent-docs-match-artifacts',
		run(c) {
			const chain = c.json('VVIP/showcase/pursuit_chain.json');
			const sig = c.json('VVIP/showcase/signal_policy.json');
			const bench = c.json('VVIP/scripts/bench_adversarial_report.json');
			if (!chain || !sig || !bench) return;
			const fired = chain.recommendation?.fired_rules ?? [];
			const cnt = (r) => fired.filter((x) => x === r).length;
			const cm = (chain.recommendation?.chosen?.text || '').match(/read at (\d+) km\/h in a (\d+) km\/h segment/);
			const top = chain.interception?.top ?? {};
			const pts = chain.trace?.points;
			const speeds = chain.sightings?.speeds_kmh ?? [];
			const planned = sig.apply?.planned_junctions ?? [];
			const kc = bench.score_summary?.keyword_coverage;
			/** contradiction rules: pattern → (groups) must equal the artifact value */
			const rules = [
				[/R_SPD_SEGMENT[`*]*\s*(?:\u00d7|x)\s*(\d+)/g, () => String(cnt('R_SPD_SEGMENT')), 'R_SPD_SEGMENT fired count'],
				[/R_SIGNAL_VIOLATION[`*]*\s*(?:\u00d7|x)\s*(\d+)/g, () => String(cnt('R_SIGNAL_VIOLATION')), 'R_SIGNAL_VIOLATION fired count'],
				[/read at (\d+) km\/h in a (\d+) km\/h segment/g, () => (cm ? `${cm[1]}|${cm[2]}` : '?|?'), 'chosen e-challan segment'],
				[/(\d+)-position(?: live)? trace/g, () => String(pts), 'trace positions'],
				[/\((\d+) (?:timed )?positions/g, () => String(pts), 'trace positions'],
				[/top:?\s*junction (\d+)/g, () => String(top.junction_id), 'top choke junction'],
				[/(\d+) choke junctions/g, () => String(chain.interception?.n_junctions), 'choke-junction count'],
				[/proposed for (\d+) junctions/g, () => String(planned.length), 'signal-plan junction count']
			];
			for (const rel of [DECK, DEMO, TECH_DOCS, README]) {
				const t = c.read(rel);
				if (t == null) { c.fail(rel, 1, c.id, 'doc missing — cannot scan figures'); continue; }
				t.split('\n').forEach((line, i) => {
					for (const [re, want, what] of rules) {
						for (const m of line.matchAll(re)) {
							const got = m.slice(1).join('|');
							const exp = want();
							if (got !== exp) c.fail(rel, i + 1, c.id, `"${what}" quoted as ${got} but the artifact says ${exp} (${m[0]})`);
						}
					}
					// 'score X' only judged on junction lines (the choke-score slot)
					if (/junction/.test(line)) {
						for (const m of line.matchAll(/score (\d+(?:\.\d+)?)/g)) {
							if (String(m[1]) !== String(top.score)) c.fail(rel, i + 1, c.id, `choke score quoted as ${m[1]} but the artifact says ${top.score}`);
						}
					}
					// ANPR speed range this run, only on camera/sighting lines
					if (/EM-CAM|ANPR/.test(line)) {
						for (const m of line.matchAll(new RegExp(`at (\\d+)${EN}(\\d+) km\\/h`, 'g'))) {
							const exp = `${Math.min(...speeds)}|${Math.max(...speeds)}`;
							if (`${m[1]}|${m[2]}` !== exp) c.fail(rel, i + 1, c.id, `ANPR speed range quoted as ${m[1]}\u2013${m[2]} but the artifact says ${Math.min(...speeds)}\u2013${Math.max(...speeds)}`);
						}
					}
					// keyword-coverage: the figure ATTACHED to the token is the
					// claimed-current value and must equal the artifact; other
					// numbers on the line (tool_validity, documented prior-run
					// history) are not keyword-coverage claims.
					for (const m of line.matchAll(/keyword[ _]coverage\D{0,10}?(\d\.\d+)/gi)) {
						if (String(m[1]) !== String(kc)) {
							c.fail(rel, i + 1, c.id, `keyword_coverage quoted as ${m[1]} but the bench artifact says ${kc}`);
						}
					}
					// pursuit event id prefix (truncated citations included)
					if (/pursuit/i.test(line)) {
						for (const m of line.matchAll(/\b([0-9a-f]{8})-(?:\u2026|\.\.\.|[0-9a-f]{4})/g)) {
							if (m[1] !== chain.pursuit_event?.event_id?.slice(0, 8)) {
								c.fail(rel, i + 1, c.id, `pursuit event id quoted as ${m[1]}-\u2026 but the artifact says ${chain.pursuit_event?.event_id?.slice(0, 8)}-\u2026`);
							}
						}
					}
				});
			}
			c.ok(c.id, 'no figure in DECK/DEMO/TECHNICAL_DOCS/README contradicts the cited artifacts');
		}
	},
	{
		id: 'screens-verify-40of40',
		run(c) {
			const man = c.json(SCREENS_MANIFEST);
			if (!man) return;
			const screens = man.screens ?? {};
			let total = 0;
			let okCount = 0;
			for (const [name, s] of Object.entries(screens)) {
				const ch = s.checks ?? {};
				total += Object.keys(ch).length;
				okCount += Object.values(ch).filter((x) => x.ok).length;
				for (const [k, x] of Object.entries(ch)) {
					if (!x.ok) c.fail(SCREENS_MANIFEST, 1, c.id, `screen "${name}" check "${k}" not ok: ${x.detail ?? ''}`);
				}
			}
			if (total !== 40) c.fail(SCREENS_MANIFEST, 1, c.id, `screens manifest records ${total} checks, expected 40`);
			const drv = screens.driver?.checks ?? {};
			if (Object.keys(drv).length !== 7) c.fail(SCREENS_MANIFEST, 1, c.id, `driver screen records ${Object.keys(drv).length} checks, expected 7`);
			const cd = drv['approved open window (live countdown)'];
			if (!cd || !/countdown=90s/.test(cd.detail || '')) {
				c.fail(SCREENS_MANIFEST, 1, c.id, 'driver "approved open window (live countdown)" check missing or its detail lacks countdown=90s');
			}
			const deck = c.read(DECK) ?? '';
			c.expectIn(DECK, deck, '40/40', c.id, 'deck must quote the screens-verify result as 40/40');
			if (total === 40 && okCount === 40) c.ok(c.id, '40/40 checks ok incl. driver countdown=90s');
		}
	},
	{
		id: 'showcase-artifacts-single-run',
		run(c) {
			const res = c.json('VVIP/showcase/showcase_results.json');
			const chain = c.json('VVIP/showcase/pursuit_chain.json');
			const man = c.json('VVIP/showcase/shots_manifest.json');
			if (!res || !chain) return;
			const deck = c.read(DECK) ?? '';
			const demo = c.read(DEMO) ?? '';
			const t1 = Date.parse(res.generated_at_utc);
			const t2 = Date.parse(chain.approval?.audit_tail_last?.at);
			if (!(t1 > 0) || !(t2 > 0)) {
				c.fail('VVIP/showcase/showcase_results.json', 1, c.id, 'run timestamps unparseable — cannot establish single-run consistency');
				return;
			}
			const stamp = (iso) => new Date(iso).toISOString().slice(0, 16) + 'Z';
			const s1 = stamp(res.generated_at_utc);
			const s2 = stamp(chain.approval.audit_tail_last.at);
			const split = Math.abs(t2 - t1) > 60 * 60 * 1000;
			c.expectIn(DECK, deck, s1, c.id, 'deck must cite the showcase_results.json run stamp');
			if (split) {
				c.expectIn(DECK, deck, s2, c.id,
					`showcase artifacts span TWO runs (${s1} summary vs ${s2} pursuit re-run) — the deck must cite both stamps, not blend them`, 'flagship showcase');
				c.expectIn(DEMO, demo, s1.slice(11, 16), c.id, 'video script must cite the summary run stamp (time part)');
				c.expectIn(DEMO, demo, s2.slice(11, 16), c.id, 'video script must cite the pursuit re-run stamp (time part)');
				// adjacent interface-of-record docs must disclose the split too
				for (const [rel, t] of [[TECH_DOCS, c.read(TECH_DOCS)], [README, c.read(README)]]) {
					if (t == null) { c.fail(rel, 1, c.id, 'doc missing — cannot check run-stamp disclosure'); continue; }
					c.expectIn(rel, t, s1.slice(11, 16), c.id, `${rel} must disclose the 13:28Z showcase summary run it cites`);
					c.expectIn(rel, t, s2.slice(11, 16), c.id, `${rel} must disclose the ${s2.slice(11, 16)} pursuit re-run — the on-disk pursuit artifacts are that run`);
				}
			}
			if (man?.pursuitFeed?.vehicleId && man.pursuitFeed.vehicleId !== chain.vehicle_id) {
				c.fail('VVIP/showcase/shots_manifest.json', 1, c.id,
					`pursuitFeed vehicle ${man.pursuitFeed.vehicleId} differs from pursuit_chain vehicle ${chain.vehicle_id} — cross-run blend`);
			}
			if (man?.pursuitFeed?.points && man.pursuitFeed.points !== chain.trace?.points) {
				c.fail('VVIP/showcase/shots_manifest.json', 1, c.id,
					`pursuitFeed points ${man.pursuitFeed.points} differ from pursuit_chain trace points ${chain.trace?.points} — cross-run blend`);
			}
			c.ok(c.id, split
				? `two runs disclosed (${s1} / ${s2}); shared-vehicle and trace-point agreement verified`
				: `single run ${s1}; shared-vehicle and trace-point agreement verified`);
		}
	},
	{
		id: 'pytest-counts-match-deck',
		run(c) {
			const got = collectPytest(c);
			if (!got) return;
			const deck = c.read(DECK) ?? '';
			const find = (frag) => Object.entries(got.perSuite).find(([k]) => k.includes(frag));
			const emu = find('event-emulator');
			const ora = find('traffic-oracle');
			c.expectIn(DECK, deck, `${got.total} pytest`, c.id,
				`deck pytest total does not match live gate-venv collection`, 'verify_all');
			if (emu) c.expectIn(DECK, deck, `emulator's ${emu[1]}`, c.id, `deck emulator count does not match collection (${emu[0]})`, 'verify_all');
			if (ora) c.expectIn(DECK, deck, `traffic-oracle's ${ora[1]}`, c.id, `deck traffic-oracle count does not match collection (${ora[0]})`, 'verify_all');
			if (!emu || !ora) c.fail(DECK, 1, c.id, 'event-emulator / traffic-oracle suites not discovered under VVIP/');
			// the adjacent docs quote the same suite counts — they must track
			// the tree too (historical session records with timings are fine;
			// this only requires the CURRENT collection numbers to appear)
			const brain = Object.entries(got.perSuite).find(([k]) => k.includes('convoy-brain'));
			const tech = c.read(TECH_DOCS) ?? '';
			const readme = c.read(README) ?? '';
			if (brain) {
				c.expectIn(TECH_DOCS, tech, `convoy-brain **${brain[1]}**`, c.id, 'TECHNICAL_DOCS pytest counts do not match live collection');
				c.expectIn(README, readme, `convoy-brain ${brain[1]}`, c.id, 'README pytest counts do not match live collection');
			}
			if (emu) {
				c.expectIn(TECH_DOCS, tech, `event-emulator **${emu[1]}**`, c.id, 'TECHNICAL_DOCS emulator count does not match live collection');
				c.expectIn(README, readme, `event-emulator ${emu[1]}`, c.id, 'README emulator count does not match live collection');
			}
			if (ora) {
				c.expectIn(TECH_DOCS, tech, `traffic-oracle **${ora[1]}**`, c.id, 'TECHNICAL_DOCS traffic-oracle count does not match live collection');
				c.expectIn(README, readme, `traffic-oracle ${ora[1]}`, c.id, 'README traffic-oracle count does not match live collection');
			}
			c.ok(c.id, `deck counts match live collection: ${got.total} total` + (emu ? `, emulator ${emu[1]}` : '') + (ora ? `, traffic-oracle ${ora[1]}` : ''));
		}
	},
	{
		id: 'e2e-11-checks-gate',
		run(c) {
			const e2e = c.read(E2E) ?? '';
			const keys = new Set([...e2e.matchAll(/\bR\.(\w+)\s*=/g)].map((m) => m[1]));
			keys.delete('pageErrors'); // the error array, not a boolean check
			const expected = new Set(['dashboard', 'advisoryIdle', 'gujarati', 'activeStatus', 'freezeSchedule',
				'subscribed', 'quickRequest', 'driverView', 'proposal', 'windowOpen', 'backToAdvisory']);
			const missing = [...expected].filter((k) => !keys.has(k));
			const extra = [...keys].filter((k) => !expected.has(k));
			if (missing.length) c.fail(E2E, 1, c.id, `e2e journey lost boolean check(s): ${missing.join(', ')}`);
			if (extra.length) c.fail(E2E, 1, c.id, `e2e journey gained unexpected boolean check(s): ${extra.join(', ')}`);
			const va = c.read(VERIFY_ALL) ?? '';
			c.expectIn(VERIFY_ALL, va, 'node tests/e2e/citizen-green-corridor.e2e.mjs', c.id,
				'verify_all.sh must keep running the citizen e2e journey (tier_frontend_e2e)');
			const deck = c.read(DECK) ?? '';
			c.expectIn(DECK, deck, '11-check e2e', c.id, 'deck must keep describing the 11-check e2e journey');
			if (!missing.length && !extra.length) c.ok(c.id, 'the 11 boolean e2e checks intact and gated by verify_all.sh');
		}
	},
	{
		id: 'migrations-007-011-exist',
		run(c) {
			const dirRel = 'VVIP/services/corridor-store/migrations';
			let files = [];
			try { files = readdirSync(path.join(c.root, dirRel)).filter((f) => f.endsWith('.sql')).sort(); } catch {
				c.fail(dirRel, 1, c.id, 'migrations directory missing/unreadable');
				return;
			}
			const expected = ['001_spatial_init.sql', '002_graph_extensions.sql', '003_anomaly_tracking.sql',
				'004_memory_embeddings.sql', '005_adaptive.sql', '006_simulation_runs.sql', '007_corridor_events.sql',
				'008_event_profiles.sql', '009_emulation_runs.sql', '010_vehicles_watchlist.sql', '011_city_coordination.sql'];
			const miss = expected.filter((f) => !files.includes(f));
			const extra = files.filter((f) => !expected.includes(f));
			if (miss.length) c.fail(dirRel, 1, c.id, `missing migration file(s): ${miss.join(', ')}`);
			if (extra.length) c.fail(dirRel, 1, c.id, `unexpected migration file(s): ${extra.join(', ')}`);
			const deck = c.read(DECK) ?? '';
			c.expectIn(DECK, deck, 'migrations 007\u2013011', c.id, 'deck must quote the migration range as on disk');
			if (!miss.length && !extra.length) c.ok(c.id, 'migrations 001..011 exactly as the deck quotes (live "applied 11/11" is the fleet tier\'s half)');
		}
	},
	{
		id: 'smoke-models-ports-env-overridable',
		run(c) {
			const t = c.read(SMOKE_MODELS) ?? '';
			for (const [v, url] of [
				['MODEL_HOST_URL', 'http://127.0.0.1:8083'],
				['LLAMA_BASE_URL', 'http://127.0.0.1:8084'],
				['EMBEDDING_SERVICE_URL', 'http://127.0.0.1:8085']
			]) {
				c.expectIn(SMOKE_MODELS, t, `\${${v}:-${url}}`, c.id, `models-tier endpoint ${v} must stay env-overridable with its loopback default`);
			}
			c.expectIn(SMOKE_MODELS, t, 'http://*|https://*', c.id, 'models tier must keep the http/https scheme guard');
			c.ok(c.id, 'smoke_models.sh keeps ${VAR:-loopback} endpoints + scheme guard');
		}
	},
	{
		id: 'showcase-endpoints-env-overridable',
		run(c) {
			const rel = SHOWCASE_RUN;
			const t = c.read(rel);
			if (t == null) { c.fail(rel, 1, c.id, 'showcase_run.sh missing'); return; }
			// bash -n must pass
			const bashn = spawnSync('bash', ['-n', path.join(c.root, rel)], { encoding: 'utf8' });
			if (bashn.status !== 0) c.fail(rel, 1, c.id, `bash -n failed: ${(bashn.stderr || '').trim()}`);
			// the URL constants: full-URL override + port override, loopback defaults
			for (const [line, url, port] of [
				['ORACLE_URL="${ORACLE_URL:-http://127.0.0.1:${ORACLE_PORT:-8081}}"', 'ORACLE_URL', 8081],
				['BRAIN_URL="${BRAIN_URL:-http://127.0.0.1:${BRAIN_PORT:-8080}}"', 'BRAIN_URL', 8080],
				['EMULATOR_URL="${EMULATOR_URL:-http://127.0.0.1:${EMULATOR_PORT:-8090}}"', 'EMULATOR_URL', 8090],
				['DECIDER_URL="${DECIDER_URL:-http://127.0.0.1:${DECIDER_PORT:-8083}}"', 'DECIDER_URL', 8083],
				['LLAMA_URL="${LLAMA_URL:-http://127.0.0.1:${LLAMA_PORT:-8084}}"', 'LLAMA_URL', 8084],
				['EMBED_URL="${EMBED_URL:-http://127.0.0.1:${EMBED_PORT:-8085}}"', 'EMBED_URL', 8085],
				['DECK_CONTAINER_URL="${DECK_URL:-http://127.0.0.1:${DECK_PORT:-5173}}"', 'DECK_CONTAINER_URL', 5173]
			]) {
				c.expectIn(rel, t, line, c.id, `endpoint constant ${url} must be env-overridable (port ${port} preserved as default)`);
			}
			c.expectIn(rel, t, 'DECK_ORIGIN="$DECK_CONTAINER_URL"', c.id, 'the canvas origin must be the ONE deck constant, not a second hardcoded copy');
			for (const needle of [
				'COMPOSE_PROJECT="${COMPOSE_PROJECT:-vvip}"',
				'${POSTGIS_CONTAINER:-${COMPOSE_PROJECT}-postgis}',
				'PSQL="docker exec $PG_CONTAINER psql -U $PG_USER -d $PG_DB -At"'
			]) {
				c.expectIn(rel, t, needle, c.id, 'fleet addressing must be parametrized (project/containers/DB) with defaults preserved');
			}
			// generic: no bare loopback URL outside a ${...:- default}
			t.split('\n').forEach((line, i) => {
				for (const m of line.matchAll(/https?:\/\/127\.0\.0\.1:(\d+)/g)) {
					if (!line.slice(0, m.index).includes(':-')) {
						c.fail(rel, i + 1, c.id, `bare loopback constant http://127.0.0.1:${m[1]} outside a \${VAR:-default}`);
					}
				}
			});
			// no hardcoded container names in docker calls, no hardcoded compose project/files
			t.split('\n').forEach((line, i) => {
				if (/docker (exec|inspect|logs) vvip-/.test(line)) c.fail(rel, i + 1, c.id, `hardcoded container name in docker call: ${line.trim().slice(0, 80)}`);
				if (/docker compose/.test(line) && !line.includes('$COMPOSE_PROJECT')) c.fail(rel, i + 1, c.id, `docker compose call without -p $COMPOSE_PROJECT: ${line.trim().slice(0, 80)}`);
			});
			// the deck health contract must be the cockpit (200 + template), not 301
			c.expectIn(rel, t, 'grep -q \'data-template="convoy-command"\'', c.id, 'deck health must assert the cockpit template marker (design-smoke contract)');
			if (/=\s*"301"\s*\]/.test(t)) c.fail(rel, 1, c.id, 'deck health still asserts the retired 301 status');
			c.ok(c.id, 'showcase_run.sh: bash -n ok, endpoints/containers env-overridable, cockpit health contract');
		}
	},
	{
		id: 'pptx-claims-fresh',
		run(c) {
			const abs = path.join(c.root, PPTX);
			if (!existsSync(abs)) { c.fail(PPTX, 1, c.id, 'rendered deck missing — run submission/make_deck.py'); return; }
			let xml;
			try {
				xml = zipFileText(readFileSync(abs), /^ppt\/slides\/slide\d+\.xml$/);
			} catch (e) {
				c.fail(PPTX, 1, c.id, `unreadable pptx: ${e.message}`);
				return;
			}
			// stale strings from the pre-cockpit / earlier-run revisions
			for (const bad of ['redirects to /ops', '0.548', 'score 0.7', '72-position', `95${EN}114`, 'junction 1, signal-controlled']) {
				if (xml.includes(bad)) c.fail(PPTX, 1, c.id, `rendered pptx still carries the stale figure "${bad}" — re-run submission/make_deck.py`);
			}
			// the current figures, derived from the same artifacts the md deck uses
			const chain = c.json('VVIP/showcase/pursuit_chain.json');
			const bench = c.json('VVIP/scripts/bench_adversarial_report.json');
			if (chain && bench) {
				const fired = chain.recommendation.fired_rules ?? [];
				const cnt = (r) => fired.filter((x) => x === r).length;
				const cm = (chain.recommendation.chosen?.text || '').match(/read at (\d+) km\/h in a (\d+) km\/h segment/);
				const top = chain.interception.top ?? {};
				for (const needle of [
					`R_SPD_SEGMENT \u00d7${cnt('R_SPD_SEGMENT')}`,
					`R_SIGNAL_VIOLATION \u00d7${cnt('R_SIGNAL_VIOLATION')}`,
					cm ? `read at ${cm[1]} km/h in a ${cm[2]} km/h segment` : 'chosen text',
					`junction ${top.junction_id}, signal-controlled, score ${top.score}`,
					`${chain.trace.points}-position trace`,
					String(bench.score_summary.keyword_coverage),
					'Convoy Command cockpit', '40/40'
				]) {
					if (!xml.includes(needle)) c.fail(PPTX, 1, c.id, `rendered pptx lacks the current figure "${needle}" — re-run submission/make_deck.py`);
				}
			}
			c.ok(c.id, 'rendered pptx carries the corrected figures (no stale strings)');
		}
	},
	{
		id: 'verify-all-tier-inventory',
		run(c) {
			const va = c.read(VERIFY_ALL) ?? '';
			const order = ['tier_frontend_bundle', 'tier_frontend_e2e', 'tier_gods_eye_check',
				'tier_gods_eye_canvas', 'tier_pytest_suites', 'tier_models'];
			let last = -1;
			for (const tier of order) {
				// BOTH the function definition AND the main() call site must
				// exist — a deleted definition with the call left in would die
				// at runtime with command-not-found, so it is a failure here,
				// not a passing grep.
				const def = new RegExp(`^${tier}\\(\\)\\s*\\{`, 'm').exec(va);
				const call = new RegExp(`^\\s+${tier}$`, 'm').exec(va);
				if (!def) { c.fail(VERIFY_ALL, defLine(va, tier), c.id, `verify_all.sh lost the ${tier}() function definition (main() would die: command not found)`); continue; }
				if (!call) { c.fail(VERIFY_ALL, 1, c.id, `verify_all.sh lost the main() call to ${tier}`); continue; }
				if (call.index < last) c.fail(VERIFY_ALL, va.slice(0, call.index).split('\n').length, c.id, `verify_all.sh tier ${tier} out of the documented main() order`);
				last = call.index;
			}
			c.expectIn(VERIFY_ALL, va, 'ALL TIERS PASSED', c.id, 'verify_all.sh must keep its terminal pass line');
			if (!/design-smoke/.test(va)) {
				c.warn('drift panel: tests/design-smoke.mjs (the cockpit-topology tripwire) is NOT a tier in verify_all.sh — flagged, not failed (verify_all.sh is out of this stream\'s edit scope)');
			}
			const fleet = c.read(FLEET_SMOKE) ?? '';
			const show = c.read(SHOWCASE_RUN) ?? '';
			if (!/design-smoke/.test(fleet) && !/design-smoke/.test(show)) {
				c.fail(FLEET_SMOKE, 1, c.id, 'no gate script references design-smoke.mjs — the cockpit tripwire would run nowhere');
			}
			c.ok(c.id, 'tier definitions + call sites intact in order; design-smoke referenced by fleet_smoke.sh/showcase_run.sh');
		}
	},
	{
		id: 'pdf-claims-fresh',
		run(c) {
			const pptxAbs = path.join(c.root, PPTX);
			const pdfAbs = path.join(c.root, PDF);
			if (!existsSync(pdfAbs)) { c.fail(PDF, 1, c.id, 'rendered PDF missing — re-convert the pptx (soffice --headless --convert-to pdf)'); return; }
			if (existsSync(pptxAbs) && statSync(pdfAbs).mtimeMs < statSync(pptxAbs).mtimeMs) {
				c.fail(PDF, 1, c.id, 'rendered PDF predates the PPTX — the pdf was not re-converted after the last deck render');
			}
			// pdftotext is a hard requirement: without it the PDF's claims
			// would be checked against nothing (the silent-pass class)
			const probe = spawnSync('pdftotext', ['-v'], { encoding: 'utf8' });
			if (probe.status !== 0 && probe.error) {
				c.fail(PDF, 1, c.id, 'pdftotext (poppler-utils) unavailable — required to gate the rendered PDF');
				return;
			}
			const ex = spawnSync('pdftotext', [pdfAbs, '-'], { encoding: 'utf8', maxBuffer: 32 * 1024 * 1024 });
			if (ex.status !== 0) {
				c.fail(PDF, 1, c.id, `pdftotext failed on the rendered PDF: ${(ex.stderr || '').slice(0, 120)}`);
				return;
			}
			const text = (ex.stdout || '').replace(/\s+/g, ' ');
			for (const bad of ['redirects to /ops', '0.548', 'score 0.7', '72-position', `95${EN}114`, 'junction 1, signal-controlled']) {
				if (text.includes(bad)) c.fail(PDF, 1, c.id, `rendered PDF still carries the stale figure "${bad}" — re-convert from the corrected pptx`);
			}
			const chain = c.json('VVIP/showcase/pursuit_chain.json');
			const bench = c.json('VVIP/scripts/bench_adversarial_report.json');
			if (chain && bench) {
				const fired = chain.recommendation.fired_rules ?? [];
				const cnt = (r) => fired.filter((x) => x === r).length;
				const cm = (chain.recommendation.chosen?.text || '').match(/read at (\d+) km\/h in a (\d+) km\/h segment/);
				const top = chain.interception.top ?? {};
				for (const needle of [
					`R_SPD_SEGMENT \u00d7${cnt('R_SPD_SEGMENT')}`,
					`read at ${cm ? cm[1] : '?'} km/h in a ${cm ? cm[2] : '?'} km/h segment`,
					`junction ${top.junction_id}, signal-controlled, score ${top.score}`,
					`${chain.trace.points}-position trace`,
					String(bench.score_summary.keyword_coverage),
					'Convoy Command cockpit', '40/40'
				]) {
					if (!text.includes(needle.replace(/\s+/g, ' '))) {
						c.fail(PDF, 1, c.id, `rendered PDF lacks the current figure "${needle}" — re-convert from the corrected pptx`);
					}
				}
			}
			c.ok(c.id, 'rendered PDF carries the corrected figures (pdftotext-verified) and postdates the pptx');
		}
	}
];

/** line number of `tier() {` in verify_all.sh (for precise failures) */
function defLine(text, tier) {
	const m = new RegExp(`^${tier}\\(\\)`, 'm').exec(text);
	return m ? text.slice(0, m.index).split('\n').length : 1;
}

function runAllChecks(root, opts = {}) {
	const c = new Ctx(root, opts);
	for (const check of CHECKS) {
		c.id = check.id;
		try {
			check.run(c);
		} catch (e) {
			c.fail('(oracle)', 1, check.id, `check crashed: ${e.message}\n${e.stack?.split('\n').slice(0, 4).join('\n')}`);
		}
	}
	return c;
}

/* ─────────────────────────────── selftest ────────────────────────────────
 * Copies everything the oracle reads into a temp fixture, proves the
 * baseline is green there, then injects one known-bad mutation at a time
 * and requires the named check to FAIL. Exits 0 only if the failure path
 * works for every mutation.                                                        */

const FIXTURE_FILES = [
	DECK, DEMO, MAKER, PPTX, PDF,
	'VVIP/README.md', 'VVIP/TECHNICAL_DOCS.md',
	SHOWCASE_RUN, FLEET_SMOKE, SMOKE_MODELS, VERIFY_ALL, 'VVIP/scripts/bench_stack.sh',
	'VVIP/scripts/impact_results.json', 'VVIP/scripts/bench_results.json',
	'VVIP/scripts/bench_adversarial_report.json', 'VVIP/scripts/impact_run.py',
	'VVIP/IMPACT_REPORT.md', 'PROBLEM_STATEMENT_GUJARAT.md',
	'VVIP/docs/USE_CASES.md', 'VVIP/docs/GODS_EYE_INTEGRATION.md', 'VVIP/docs/ACTIVE_MOVEMENT_ADAPTATION.md',
	'VVIP/showcase/pursuit_chain.json', 'VVIP/showcase/showcase_results.json',
	'VVIP/showcase/twin_audit.json', 'VVIP/showcase/signal_policy.json',
	'VVIP/showcase/band_easing.json', 'VVIP/showcase/ambulance_observe.json',
	'VVIP/showcase/jan_rakshak.json', 'VVIP/showcase/injection_flagship.json',
	'VVIP/showcase/shots_manifest.json', 'VVIP/showcase/shots_city.log', 'VVIP/showcase/shots_rest.log',
	'VVIP/showcase/showcase_gods_eye_city.png', 'VVIP/showcase/showcase_pursuit_trace.png',
	'VVIP/showcase/showcase_citizen_advisory.png', 'VVIP/showcase/showcase_ops_wall.png',
	DESIGN_SMOKE, 'VVIP/services/command-deck/tests/screens-verify.mjs', SCREENS_MANIFEST,
	...['ops', 'gods-eye', 'citizen', 'driver', 'comms', 'chat'].map((s) => `VVIP/services/command-deck/tests/artifacts/screens/screen_${s}.png`),
	ROUTE_ROOT, 'VVIP/services/command-deck/src/hooks.server.ts',
	E2E,
	...['001_spatial_init.sql', '002_graph_extensions.sql', '003_anomaly_tracking.sql', '004_memory_embeddings.sql',
		'005_adaptive.sql', '006_simulation_runs.sql', '007_corridor_events.sql', '008_event_profiles.sql',
		'009_emulation_runs.sql', '010_vehicles_watchlist.sql', '011_city_coordination.sql']
		.map((f) => `VVIP/services/corridor-store/migrations/${f}`)
];
const FIXTURE_DIRS = ['VVIP/services/command-deck/src', 'VVIP/frontend'];

// the pytest check runs against a stub under selftest (the fixture carries
// no service suites); the stub mirrors the live gate-venv collection the
// real run performs: 2+262+22+12+113+94+633 = 1138.
const PYTEST_STUB = {
	total: 1138,
	perSuite: {
		'VVIP/services/command-deck/tests': 2,
		'VVIP/services/convoy-brain/tests': 262,
		'VVIP/services/corridor-store/importer/tests': 22,
		'VVIP/services/corridor-store/tests': 12,
		'VVIP/services/embedding-service/tests': 113,
		'VVIP/services/event-emulator/tests': 94,
		'VVIP/services/traffic-oracle/tests': 633
	}
};

/** build a stored (method-0) zip with one entry — a fake stale pptx */
function makeFakeZip(entries) {
	const chunks = [];
	const central = [];
	let offset = 0;
	for (const [name, data] of entries) {
		const nb = Buffer.from(name, 'utf8');
		const db = Buffer.from(data, 'utf8');
		const lh = Buffer.alloc(30);
		lh.writeUInt32LE(0x04034b50, 0);
		lh.writeUInt16LE(20, 4); // version
		lh.writeUInt16LE(0, 6); // flags
		lh.writeUInt16LE(0, 8); // method 0 = stored
		chunks.push(lh, nb, db);
		const ch = Buffer.alloc(46);
		ch.writeUInt32LE(0x02014b50, 0);
		ch.writeUInt16LE(20, 4);
		ch.writeUInt16LE(20, 6);
		ch.writeUInt16LE(0, 8);
		ch.writeUInt32LE(db.length, 20);
		ch.writeUInt16LE(nb.length, 28);
		ch.writeUInt32LE(offset, 42);
		central.push(Buffer.concat([ch, nb]));
		offset += 30 + nb.length + db.length;
	}
	const cd = Buffer.concat(central);
	const eocd = Buffer.alloc(22);
	eocd.writeUInt32LE(0x06054b50, 0);
	eocd.writeUInt16LE(entries.length, 8);
	eocd.writeUInt16LE(entries.length, 10);
	eocd.writeUInt32LE(cd.length, 12);
	eocd.writeUInt32LE(30, 16); // central dir offset (fixed local part? computed below)
	// recompute: the EOCD's central-directory-offset field must be the real one
	eocd.writeUInt32LE(offset, 16);
	return Buffer.concat([...chunks, cd, eocd]);
}

const MUTATIONS = [
	{
		id: 'no-stale-root-redirect-claims',
		desc: 'DECK.md re-describes the retired 301-to-/ops behaviour',
		apply(c, fx) {
			const p = path.join(fx, DECK);
			writeFileSync(p, readFileSync(p, 'utf8').replace('## Slide 1',
				'## Slide 1\n\nSTALE: `/` redirects to /ops via a 301 — the old front door.\n'));
		}
	},
	{
		id: 'bench-figures-match-artifacts',
		desc: 'DECK.md quotes keyword coverage 0.548 (artifact says 0.561)',
		apply(c, fx) {
			const p = path.join(fx, DECK);
			writeFileSync(p, readFileSync(p, 'utf8').replaceAll('coverage **0.561**', 'coverage **0.548**'));
		}
	},
	{
		id: 'showcase-pursuit-figures-derivable',
		desc: 'DECK.md quotes the earlier run\'s 72-position trace everywhere',
		apply(c, fx) {
			const p = path.join(fx, DECK);
			writeFileSync(p, readFileSync(p, 'utf8').replaceAll('4-position trace', '72-position trace'));
		}
	},
	{
		id: 'pixel-proofs-match-shot-logs',
		desc: 'DECK.md quotes an unbacked 2,776-colour city proof (log says 2,781)',
		apply(c, fx) {
			const p = path.join(fx, DECK);
			writeFileSync(p, readFileSync(p, 'utf8').replace('2,781-colour', '2,776-colour'));
		}
	},
	{
		id: 'doc-paths-exist',
		desc: 'DECK.md cites a nonexistent artifact',
		apply(c, fx) {
			const p = path.join(fx, DECK);
			writeFileSync(p, readFileSync(p, 'utf8').replace('`VVIP/scripts/impact_results.json`', '`VVIP/scripts/impact_results_missing.json`'));
		}
	},
	{
		id: 'root-serves-cockpit-200',
		desc: 'the root route loses the cockpit template marker',
		apply(c, fx) {
			const p = path.join(fx, ROUTE_ROOT);
			writeFileSync(p, readFileSync(p, 'utf8').replace('data-template="convoy-command"', 'data-template="stale-root"'));
		}
	},
	{
		id: 'impact-json-single-source',
		desc: 'impact_results.json central baseline drifts from the pinned value',
		apply(c, fx) {
			const p = path.join(fx, 'VVIP/scripts/impact_results.json');
			writeFileSync(p, readFileSync(p, 'utf8').replace('"commuter_minutes_baseline": 570937.5', '"commuter_minutes_baseline": 570000.0'));
		}
	},
	{
		id: 'screens-verify-40of40',
		desc: 'one screens-manifest check flips to not-ok',
		apply(c, fx) {
			const p = path.join(fx, SCREENS_MANIFEST);
			writeFileSync(p, readFileSync(p, 'utf8').replace('"ok": true', '"ok": false'));
		}
	},
	{
		id: 'e2e-11-checks-gate',
		desc: 'the citizen e2e loses the gujarati check',
		apply(c, fx) {
			const p = path.join(fx, E2E);
			writeFileSync(p, readFileSync(p, 'utf8').replace('R.gujarati =', 'R.gujaratIX ='));
		}
	},
	{
		id: 'migrations-007-011-exist',
		desc: 'migration 011 disappears',
		apply(c, fx) { rmSync(path.join(fx, 'VVIP/services/corridor-store/migrations/011_city_coordination.sql')); }
	},
	{
		id: 'smoke-models-ports-env-overridable',
		desc: 'smoke_models.sh hardcodes the decider endpoint again',
		apply(c, fx) {
			const p = path.join(fx, SMOKE_MODELS);
			writeFileSync(p, readFileSync(p, 'utf8').replace('${MODEL_HOST_URL:-http://127.0.0.1:8083}', 'http://127.0.0.1:8083'));
		}
	},
	{
		id: 'showcase-endpoints-env-overridable',
		desc: 'showcase_run.sh hardcodes ORACLE_URL again',
		apply(c, fx) {
			const p = path.join(fx, SHOWCASE_RUN);
			writeFileSync(p, readFileSync(p, 'utf8').replace('ORACLE_URL="${ORACLE_URL:-http://127.0.0.1:${ORACLE_PORT:-8081}}"', 'ORACLE_URL="http://127.0.0.1:8081"'));
		}
	},
	{
		id: 'pptx-claims-fresh',
		desc: 'the pptx regresses to stale slide text',
		apply(c, fx) {
			writeFileSync(path.join(fx, PPTX), makeFakeZip([
				['ppt/slides/slide4.xml',
					'<a:t>/ redirects to /ops, the situation wall (top: junction 1, signal-controlled, score 0.7) '
					+ `+ live 72-position trace at 95${EN}114 km/h; keyword coverage 0.548</a:t>`]
			]));
		}
	},
	{
		id: 'pytest-counts-match-deck',
		desc: 'DECK.md quotes the old 875-pytest total',
	apply(c, fx) {
		const p = path.join(fx, DECK);
		writeFileSync(p, readFileSync(p, 'utf8').replace('1138 pytest', '875 pytest'));
	}
	},
	{
		id: 'showcase-artifacts-single-run',
		desc: 'DECK.md cites a stale showcase run stamp (the 14:29Z degraded run)',
		apply(c, fx) {
			const p = path.join(fx, DECK);
			writeFileSync(p, readFileSync(p, 'utf8').replace('2026-10-18T15:01Z', '2026-10-18T14:29Z'));
		}
	},
	{
		id: 'verify-all-tier-inventory',
		desc: 'verify_all.sh loses the tier_models() definition (call kept → runtime crash)',
		apply(c, fx) {
			const p = path.join(fx, VERIFY_ALL);
			writeFileSync(p, readFileSync(p, 'utf8').replace('tier_models() {', 'tier_models_GONE() {'));
		}
	},
	{
		id: 'pdf-claims-fresh',
		desc: 'the rendered PDF is replaced with a non-PDF carrying stale figures',
		apply(c, fx) {
			writeFileSync(path.join(fx, PDF), 'stale: / redirects to /ops, 72-position trace, keyword coverage 0.548\n');
		}
	},
	{
		id: 'adjacent-docs-match-artifacts',
		desc: 'TECHNICAL_DOCS.md regresses to the earlier run\'s R_SPD_SEGMENT ×5',
		apply(c, fx) {
			const p = path.join(fx, TECH_DOCS);
			writeFileSync(p, readFileSync(p, 'utf8').replace('`R_SPD_SEGMENT` \u00d74', '`R_SPD_SEGMENT` \u00d75'));
		}
	},
	{
		id: 'pixel-proofs-match-shot-logs',
		desc: 'shots_city.log wording drifts so the entity probe cannot parse (reviewer probe)',
		apply(c, fx) {
			const p = path.join(fx, 'VVIP/showcase/shots_city.log');
			writeFileSync(p, readFileSync(p, 'utf8').replace('entities=38', 'entities=thirty-eight'));
		}
	}
];

async function selftest() {
	const fx = mkdtempSync(path.join(tmpdir(), 'veer-subcons-'));
	let exit = 0;
	try {
		// build the fixture: every file the oracle reads
		for (const rel of FIXTURE_FILES) {
			const src = path.join(ROOT, rel);
			if (!existsSync(src)) { console.error(`selftest: fixture source missing: ${rel}`); exit = 1; continue; }
			const dst = path.join(fx, rel);
			mkdirSync(path.dirname(dst), { recursive: true });
			copyFileSync(src, dst);
		}
		for (const rel of FIXTURE_DIRS) {
			const src = path.join(ROOT, rel);
			const dst = path.join(fx, rel);
			if (existsSync(src)) {
				mkdirSync(path.dirname(dst), { recursive: true });
				try { mkdirSync(dst, { recursive: true }); } catch {}
				(function cp(from, to) {
					for (const e of readdirSync(from, { withFileTypes: true })) {
						const f = path.join(from, e.name), t2 = path.join(to, e.name);
						if (e.isDirectory()) { if (!['node_modules', '.svelte-kit', 'build'].includes(e.name)) { mkdirSync(t2, { recursive: true }); cp(f, t2); } }
						else copyFileSync(f, t2);
					}
				})(src, dst);
			} else { mkdirSync(dst, { recursive: true }); }
		}
		// baseline: the faithful copies must be all green (pytest stubbed)
		const base = runAllChecks(fx, { pytestStub: PYTEST_STUB });
		if (base.failures.length === 0) {
			console.log(`selftest: baseline GREEN on the temp fixture (${CHECKS.length} checks, pytest stubbed)`);
		} else {
			exit = 1;
			console.error('selftest: baseline NOT green — the oracle disagrees with the real tree copies:');
			for (const f of base.failures) console.error(`  ${f.file}:${f.line} [${f.check}] ${f.msg}`);
		}
		// exercise the REAL collectPytest paths (discovery + gate-venv spawn +
		// output parse) that the stubbed comparison runs never touch: a tiny
		// fixture suite through the symlinked gate venv, then a broken
		// conftest to prove the unparseable-output failure branch.
		const realVenv = path.join(ROOT, 'VVIP/.venvs/gate');
		if (!existsSync(path.join(realVenv, 'bin/python'))) {
			exit = 1;
			console.error('selftest: real gate venv missing at VVIP/.venvs/gate — cannot exercise the live collection paths');
		} else {
			mkdirSync(path.join(fx, 'VVIP/.venvs'), { recursive: true });
			symlinkSync(realVenv, path.join(fx, 'VVIP/.venvs/gate'), 'dir');
			const tinyDir = path.join(fx, 'VVIP/services/tiny/tests');
			mkdirSync(tinyDir, { recursive: true });
			writeFileSync(path.join(tinyDir, 'test_two.py'), 'def test_one():\n    assert 1\n\ndef test_two():\n    assert 2\n');
			const okCtx = new Ctx(fx);
			okCtx.id = 'pytest-counts-match-deck';
			const got = collectPytest(okCtx);
			if (got && got.total === 2 && got.perSuite['VVIP/services/tiny/tests'] === 2 && okCtx.failures.length === 0) {
				console.log('selftest: real collectPytest path exercised — fixture suite collected 2 via the symlinked gate venv');
			} else {
				exit = 1;
				console.error(`selftest: real collectPytest path broken: got=${JSON.stringify(got)} failures=${JSON.stringify(okCtx.failures)}`);
			}
			writeFileSync(path.join(tinyDir, 'conftest.py'), 'def (broken syntax — selftest parse-failure probe\n');
			const badCtx = new Ctx(fx);
			badCtx.id = 'pytest-counts-match-deck';
			collectPytest(badCtx);
			if (badCtx.failures.some((f) => f.check === 'pytest-counts-match-deck')) {
				console.log('selftest: collectPytest parse-failure branch exercised — broken conftest fails loudly');
			} else {
				exit = 1;
				console.error('selftest: collectPytest did NOT fail on unparseable collection output (broken conftest probe)');
			}
			rmSync(path.join(fx, 'VVIP/services/tiny'), { recursive: true, force: true });
			rmSync(path.join(fx, 'VVIP/.venvs/gate'), { force: true });
		}
		// snapshot pristine contents so each mutation starts from the baseline
		const pristine = new Map();
		for (const rel of [DECK, ROUTE_ROOT, 'VVIP/scripts/impact_results.json', SCREENS_MANIFEST,
			E2E, SMOKE_MODELS, SHOWCASE_RUN, PPTX, PDF, TECH_DOCS, VERIFY_ALL,
			'VVIP/showcase/shots_city.log',
			'VVIP/services/corridor-store/migrations/011_city_coordination.sql']) {
			const p = path.join(fx, rel);
			pristine.set(rel, existsSync(p) ? readFileSync(p) : null);
		}
		const restore = () => {
			for (const [rel, buf] of pristine) {
				const p = path.join(fx, rel);
				if (buf == null) { try { rmSync(p); } catch {} }
				else writeFileSync(p, buf);
			}
		};
		// each mutation must make the named check FAIL
		for (const mut of MUTATIONS) {
			restore();
			mut.apply(null, fx);
			const r = runAllChecks(fx, { pytestStub: PYTEST_STUB });
			const hit = r.failures.filter((f) => f.check === mut.id);
			if (hit.length > 0) {
				console.log(`selftest: ${mut.id} — FAILS as designed (${hit.length} finding(s)) · ${mut.desc}`);
				console.log(`           e.g. ${hit[0].file}:${hit[0].line} ${hit[0].msg.slice(0, 110)}`);
			} else {
				exit = 1;
				console.error(`selftest: MUTATION NOT CAUGHT — ${mut.id} · ${mut.desc} (oracle stayed green on a known-bad tree)`);
			}
		}
		restore();
	} finally {
		rmSync(fx, { recursive: true, force: true });
	}
	if (exit === 0) console.log('selftest: all mutation paths proven — oracle fails loudly on known-bad input');
	return exit;
}

/* ──────────────────────────────── main ─────────────────────────────────── */

const args = process.argv.slice(2);
if (args.includes('--selftest')) {
	process.exit(await selftest());
}

const c = runAllChecks(ROOT);
console.log(`submission-consistency oracle — root ${ROOT}`);
for (const d of c.details) console.log(`  ok    ${d.check} — ${d.detail}`);
for (const w of c.warnings) console.log(`  WARN  ${w}`);
if (c.failures.length) {
	console.error(`\nFAILURES (${c.failures.length}):`);
	for (const f of c.failures) console.error(`  ${f.file}:${f.line} [${f.check}] ${f.msg}`);
	process.exit(1);
}
console.log('\nSUBMISSION CONSISTENCY: ALL GREEN');
process.exit(0);
