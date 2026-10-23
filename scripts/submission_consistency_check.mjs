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
 *                                        (scripted fallback) — or, from a
 *                                        live-mode /driver, the idle request
 *                                        card with nothing filed
 *   showcase-artifacts-single-run        the docs cannot present two
 *                                        showcase runs as one — the split
 *                                        must be cited with both stamps; hero
 *                                        shots re-taken apart from the run
 *                                        are disclosed with their own trace
 *                                        figure, never blended into it
 *   pytest-counts-match-deck             live gate-venv collection equals
 *                                        the deck's AND the adjacent docs'
 *                                        quoted counts
 *   verify-all-deck-gates-wired          verify_all.sh wires the deck UI
 *                                        tier (hermetic ops wall, contract,
 *                                        units, edge-basemap, ops-write, the
 *                                        C6 'no officer' groups on a gate-off
 *                                        keyless preview), signs its --live
 *                                        tier in through the shared helper and
 *                                        never skips a whole pytest suite; no
 *                                        doc paragraph about verify_all still
 *                                        claims the retired React tiers
 *   migrations-on-disk-match-docs        the migration list is derived from
 *                                        disk (contiguous 001..N); DECK,
 *                                        README and TECHNICAL_DOCS quote
 *                                        "001–N" and no other full range or
 *                                        "applied n/n" count
 *   smoke-models-ports-env-overridable   the model-tier endpoints keep the
 *                                        ${VAR:-loopback} pattern
 *   showcase-endpoints-env-overridable   showcase_run.sh keeps bash -n, the
 *                                        ${VAR:-loopback} constants with the
 *                                        live (argus) defaults, the cockpit
 *                                        health contract, the shared sign-in
 *                                        helper, and phase 6's stolen-vehicle
 *                                        rule: the pursuit is authorised from
 *                                        an evidence-only R_STOLEN_SIGHTING and
 *                                        a penal approval on the plate must be
 *                                        refused (409) — never a fine (C-N)
 *   pptx-claims-fresh                    the rendered PPTX carries the
 *                                        corrected figures, not the stale
 *                                        ones
 *   verify-all-tier-inventory            tier functions AND call sites
 *                                        intact in the pinned order (the
 *                                        offline ladder and the --live tiers)
 *   pdf-claims-fresh                     the rendered PDF (pdftotext) carries
 *                                        the same corrected figures and is
 *                                        not older than the PPTX
 *   post-push-origin-main                (--post-push only; SKIPPED before the
 *                                        push) HEAD == `git ls-remote origin
 *                                        refs/heads/main` and that tree
 *                                        carries a non-empty LICENSE
 *
 * The pursuit figures follow whichever chain the showcase artifact records:
 * the per-rule fired counts (`R_STOLEN_SIGHTING` ×n, or the pre-C-N
 * `R_SPD_SEGMENT` ×n) and the chosen recommendation's own figure (the
 * evidence-only "e-challan withheld" sighting, or a speed read). A chain
 * that APPROVED a penal rule on a reported-stolen vehicle is flagged (WARN)
 * until the showcase is re-run: C-N forbids fining the theft victim.
 *
 * Zero dependencies: node stdlib only (fs, path, child_process, zlib).
 * Every path resolves from this file's own location — any cwd works.
 * VEER_WORKSPACE_ROOT=<dir> names another workspace holding VVIP/ +
 * submission/ (e.g. one whose VVIP is a git worktree).
 *
 * Usage:
 *   node VVIP/scripts/submission_consistency_check.mjs            # the gate
 *   node VVIP/scripts/submission_consistency_check.mjs --selftest # prove
 *        the failure path: copies the read set into a temp fixture, injects
 *        known-bad mutations, and requires the oracle to FAIL on each (and
 *        to stay green on the benign ones).
 *   --post-push   also run the post-push checks (after `git push`)
 *   --vvip-only   a checkout of the VVIP repository alone (CI): the
 *        workspace documents (../submission, ../PROBLEM_STATEMENT_GUJARAT.md)
 *        and the gitignored local run artifacts (shot logs, screens
 *        manifest) are not in it, so the checks — and selftest mutations —
 *        that judge them are reported as not run; everything in VVIP/ is
 *        judged as usual.
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
	realpathSync,
	rmSync,
	rmdirSync,
	unlinkSync,
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
/** the VVIP checkout this script lives in */
const CHECKOUT = path.resolve(HERE, '..');
/**
 * The workspace holding VVIP/ + submission/ — every check addresses its
 * files as VVIP/… or submission/…. VEER_WORKSPACE_ROOT names one; else the
 * checkout's parent when the checkout is a directory named VVIP (the
 * workspace layout). A checkout under any other name (a git worktree, a
 * clone into another directory) is judged through a private view: a temp
 * dir whose VVIP links to the checkout, beside the workspace documents found
 * next to it (none beside a bare clone: run it --vvip-only). The view holds
 * only symlinks, unlinked one by one at exit — never a recursive delete.
 */
function workspaceRoot() {
	if (process.env.VEER_WORKSPACE_ROOT) return path.resolve(process.env.VEER_WORKSPACE_ROOT);
	const parent = path.dirname(CHECKOUT);
	if (path.basename(CHECKOUT) === 'VVIP') return parent;
	const view = mkdtempSync(path.join(tmpdir(), 'veer-consistency-ws-'));
	const links = [];
	const link = (target, name) => {
		symlinkSync(target, path.join(view, name));
		links.push(path.join(view, name));
	};
	link(CHECKOUT, 'VVIP');
	for (const rel of ['submission', 'PROBLEM_STATEMENT_GUJARAT.md']) {
		if (existsSync(path.join(parent, rel))) link(path.join(parent, rel), rel);
	}
	process.on('exit', () => {
		for (const l of links) {
			try { unlinkSync(l); } catch { /* already gone */ }
		}
		try { rmdirSync(view); } catch { /* not empty or gone: leave it */ }
	});
	return view;
}
const ROOT = workspaceRoot();
const ARGS = process.argv.slice(2);
const VVIP_ONLY = ARGS.includes('--vvip-only');
const POST_PUSH = ARGS.includes('--post-push');

/** a path of the workspace around VVIP/ (absent from a VVIP-only checkout) */
const isWorkspacePath = (rel) => !rel.startsWith('VVIP/') && rel !== '(oracle)';

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
		this.skips = []; // {check, why} — checks not run in this mode
		this.unjudged = {}; // check -> findings on workspace files (VVIP-only checkout)
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
		// a VVIP-only checkout carries no workspace documents: what they would
		// have been judged on is reported as not judged, never as a failure
		if (this.opts.vvipOnly && isWorkspacePath(rel)) {
			this.unjudged[check] = (this.unjudged[check] ?? 0) + 1;
			return;
		}
		this.failures.push({ check, file: rel, line, msg });
	}
	warn(msg) {
		this.warnings.push(msg);
	}
	skip(check, why) {
		this.skips.push({ check, why });
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
const VERIFY_ALL = 'VVIP/scripts/verify_all.sh';
const MIGRATIONS = 'VVIP/services/corridor-store/migrations';
const DECK_SRC = 'VVIP/services/command-deck/src';
// local run artifacts the docs cite, gitignored (absent from a clean checkout)
const LOCAL_ARTIFACTS = ['VVIP/showcase/shots_city.log', 'VVIP/showcase/shots_rest.log'];
const LOCAL_ARTIFACT_DIRS = ['VVIP/services/command-deck/tests/artifacts/'];
const isLocalArtifact = (rel) => LOCAL_ARTIFACTS.includes(rel) || LOCAL_ARTIFACT_DIRS.some((d) => rel.startsWith(d));
// enforcement/rules.py RULE_IDS: the penal rules, and the evidence-only stolen
// sighting (C-N: a reported-stolen vehicle's owner is never fined)
const PENAL_RULES = ['R_SPD_SEGMENT', 'R_SIGNAL_VIOLATION', 'R_CLOSED_CORRIDOR_ENTRY'];
const RULE_IDS = [...PENAL_RULES, 'R_STOLEN_SIGHTING'];
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

/** per line of a TS/JS/Svelte source: true when the WHOLE line is a comment
 *  (`//`, a `/* … *\/` block and its continuation lines, an HTML `<!-- … -->`) */
function commentLines(text) {
	const out = [];
	let block = null; // the closing token of the comment block we are in
	for (const raw of text.split('\n')) {
		const line = raw.trim();
		if (block) {
			out.push(true);
			if (line.includes(block)) block = null;
			continue;
		}
		if (line.startsWith('//')) { out.push(true); continue; }
		if (line.startsWith('/*')) {
			out.push(true);
			if (!line.includes('*/', 2)) block = '*/';
			continue;
		}
		if (line.startsWith('<!--')) {
			out.push(true);
			if (!line.includes('-->', 4)) block = '-->';
			continue;
		}
		out.push(false);
	}
	return out;
}

/** fired-rule counts of a showcase pursuit chain, e.g. {R_STOLEN_SIGHTING: 4} */
function ruleCounts(chain) {
	const counts = {};
	for (const r of chain?.recommendation?.fired_rules ?? []) counts[r] = (counts[r] ?? 0) + 1;
	return counts;
}

/** penal rules the run's reads WOULD have fired, withheld under the FIR
 *  earmark (C-N) — recommendation.withheld_rules, one entry per read and rule */
function withheldCounts(chain) {
	const counts = {};
	for (const r of chain?.recommendation?.withheld_rules ?? []) counts[r] = (counts[r] ?? 0) + 1;
	return counts;
}

const isNum = (v) => typeof v === 'number' && Number.isFinite(v);

/** the figure the docs quote for the chosen recommendation — whichever chain
 *  the artifact records: the evidence-only stolen-vehicle sighting
 *  ("e-challan withheld", C-N) or a pre-C-N speed read */
function chosenFigure(chain) {
	const chosen = chain?.recommendation?.chosen ?? {};
	const text = chosen.text ?? '';
	const speed = text.match(/read at (\d+) km\/h in a (\d+) km\/h segment/);
	if (speed) {
		return { kind: 'speed', needle: `read at ${speed[1]} km/h in a ${speed[2]} km/h segment`,
			short: `${speed[1]} km/h in a ${speed[2]} km/h segment`, speed };
	}
	if (chosen.rule_id === 'R_STOLEN_SIGHTING' || /e-challan withheld/i.test(text)) {
		return { kind: 'withheld', needle: 'e-challan withheld', short: 'e-challan withheld', speed: null };
	}
	return { kind: 'unknown', needle: null, short: null, speed: null, text };
}

/** the chain approved a PENAL rule on a vehicle the run earmarked as stolen
 *  (FIR) — the very thing C-N forbids: the registered owner is the victim */
function penalApprovalOnStolen(chain) {
	const chosen = chain?.recommendation?.chosen ?? {};
	const fir = chain?.pursuit_event?.metadata?.fir ?? chain?.fir;
	return PENAL_RULES.includes(chosen.rule_id) && chain?.approval?.status === 'approved' && !!fir;
}

/** the figures the rendered deck (pptx, and the pdf converted from it) must
 *  carry — derived from the same artifacts the markdown deck uses */
function renderedDeckNeedles(chain, bench) {
	const top = chain.interception?.top ?? null;
	return [
		...Object.entries(ruleCounts(chain)).map(([rule, n]) => `${rule} \u00d7${n}`),
		chosenFigure(chain).needle ?? 'the chosen recommendation (no known figure in the artifact)',
		...(top ? [`junction ${top.junction_id}, ${top.signal_control === false ? 'unsignalised' : 'signal-controlled'}, score ${top.score}`] : []),
		`${chain.trace?.points}-position trace`,
		String(bench.score_summary?.keyword_coverage),
		'Convoy Command cockpit', '40/40'
	];
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
			// no route rewrite anywhere in the deck src: code only — a comment
			// may talk about redirects, and fetch()'s redirect:'manual' (never
			// FOLLOW one) is tolerated in any file
			const srcFiles = walkFiles(DECK_SRC, c);
			let clean = true;
			for (const f of srcFiles) {
				const t = c.read(f) ?? '';
				const comment = commentLines(t);
				t.split('\n').forEach((line, i) => {
					if (comment[i]) return;
					const code = line.replace(/redirect:\s*['"]manual['"]/g, '');
					if (/\bredirect(s|ed|ing)?\b/i.test(code) || /\b301\b/.test(code)) {
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
			// the audit's pins — the artifact this revision's deck is built from (the
			// real-road network's impact run, WP-20; regenerate the run and the deck together)
			const pins = [
				['scenarios.central.commuter_minutes_baseline', imp.scenarios?.central?.commuter_minutes_baseline, 1131887.8],
				['scenarios.central.commuter_minutes_platform', imp.scenarios?.central?.commuter_minutes_platform, 11318.9],
				['scenarios.central.vehicles_delayed_baseline', imp.scenarios?.central?.vehicles_delayed_baseline, 60367],
				['scenarios.central.vehicles_delayed_platform', imp.scenarios?.central?.vehicles_delayed_platform, 6037],
				['scenarios.conservative.reduction_factor', imp.scenarios?.conservative?.reduction_factor, 25.0],
				['scenarios.central.reduction_factor', imp.scenarios?.central?.reduction_factor, 100.0],
				['scenarios.optimistic.reduction_factor', imp.scenarios?.optimistic?.reduction_factor, 506.2],
				['ambulance.trips_crossing_per_movement_day', imp.ambulance?.trips_crossing_per_movement_day, 12.0],
				['ambulance.minutes_returned_per_movement_day', imp.ambulance?.minutes_returned_per_movement_day, 132],
				['annualization.commuter_hours_returned_per_year_central', imp.annualization?.commuter_hours_returned_per_year_central, 4669037]
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

			const fired = ruleCounts(chain);
			const figure = chosenFigure(chain);
			const top = chain.interception?.top ?? null;
			const tracePts = chain.trace?.points;
			const speeds = chain.sightings?.speeds_kmh ?? [];
			const counts = res.counts ?? {};
			if (figure.kind === 'unknown') {
				c.fail('VVIP/showcase/pursuit_chain.json', 1, c.id,
					`the chosen recommendation's text has no known figure (speed read or "e-challan withheld"): "${String(figure.text).slice(0, 90)}" — teach chosenFigure() the new shape`);
			}
			// C-N on the artifact itself: the showcase's probe approving an
			// e-challan on the reported-stolen plate must have been refused
			const probe = chain.penal_approval_probe;
			if (probe?.exercised && probe.http !== 409) {
				c.fail('VVIP/showcase/pursuit_chain.json', 1, c.id,
					`C-N: the e-challan approval probe on the reported-stolen plate answered ${probe.http}, not 409 — a fine on the theft victim's car was NOT refused`);
			}
			if (penalApprovalOnStolen(chain)) {
				c.warn(`ethics (C-N): VVIP/showcase/pursuit_chain.json records an APPROVED penal ${chain.recommendation.chosen.rule_id} `
					+ `(${String(chain.recommendation.chosen.id).slice(0, 8)}) on a vehicle reported stolen — a pre-C-N run; re-run `
					+ 'scripts/showcase_run.sh (phase 6 authorises the pursuit from an evidence-only R_STOLEN_SIGHTING) and regenerate the deck from it');
			}

			const junctionNeedle = top
				? `junction ${top.junction_id}, ${top.signal_control === false ? 'unsignalised' : 'signal-controlled'}, score ${top.score}`
				: null;
			const speedRange = speeds.length ? `${Math.min(...speeds)}${EN}${Math.max(...speeds)} km/h` : null;
			const ruleNeedles = Object.entries(fired).map(([rule, n]) => [rule, n]);
			const deckNeedles = [
				...ruleNeedles.map(([rule, n]) => [`\`${rule}\` ×${n}`, `${rule} fired count`]),
				[figure.needle, 'chosen recommendation figure'],
				[top ? `${chain.interception?.n_junctions} choke junctions` : null, 'interception junction count'],
				[junctionNeedle, 'top choke junction'],
				[`${tracePts}-position trace`, 'live trace positions'],
				[speedRange, 'ANPR speed range this run'],
				[`${counts.decisions_logged_audit_rows_this_run} audited decisions`, 'decisions this run'],
				[`(${counts.situation_log_rows_this_run} situation + ${counts.param_override_rows_this_run} parameter)`, 'decision split'],
				[`${counts.band_transitions_this_run} band transition${counts.band_transitions_this_run === 1 ? '' : 's'}`, 'band transitions'],
				[`${counts.green_corridor_windows} green-corridor windows`, 'green-corridor windows'],
				[`${twin.replications}-replication twin`, 'twin replications'],
				[isNum(twin.ambulance_delay_sec_mean) ? `${twin.ambulance_delay_sec_mean.toFixed(2)} s` : null, 'twin ambulance delay'],
				[isNum(sig.replay?.speed_mae_kmh) ? `${sig.replay.speed_mae_kmh} km/h` : null, 'twin replay MAE'],
				[isNum(sig.replay?.mae_tolerance_kmh) ? `vs the ${sig.replay.mae_tolerance_kmh.toFixed(1)} gate` : null, 'MAE gate']
			];
			// a replay with no MAE (status no_data: hourly_aggregates holds real
			// observations only) leaves nothing a doc may quote as measured
			if (!isNum(sig.replay?.speed_mae_kmh)) {
				for (const [rel, text] of [[DECK, deck], [DEMO, demo], [MAKER, maker]]) {
					const m = text.replace(/\s+/g, ' ').match(/\d+(?:\.\d+)? km\/h vs the \d+(?:\.\d+)? (?:km\/h )?gate/);
					if (m) c.fail(rel, 1, c.id, `quotes a twin replay MAE ("${m[0]}") but signal_policy.json's replay is '${sig.replay?.status ?? 'unknown'}' with no MAE`);
				}
			}
			for (const [needle, what] of deckNeedles) {
				if (needle == null) continue;
				c.expectIn(DECK, deck, needle, c.id, `deck pursuit/showcase figure "${what}" does not match the current artifact`, 'flagship showcase');
			}
			const demoNeedles = [
				[figure.short, 'chosen recommendation figure'],
				[`${tracePts} timed positions`, 'trace positions'],
				[top ? `junction ${top.junction_id}` : null, 'top choke junction']
			];
			for (const [needle, what] of demoNeedles) {
				if (needle == null) continue;
				c.expectIn(DEMO, demo, needle, c.id, `video-script figure "${what}" does not match the current artifact`, 'BEAT 4');
			}
			const makerNeedles = [
				...ruleNeedles.map(([rule, n]) => [`${rule} ×${n}`, `${rule} fired count`]),
				[figure.needle, 'chosen recommendation figure'],
				[junctionNeedle, 'top junction'],
				[`${tracePts}-position trace`, 'trace positions'],
				[speedRange, 'speed range']
			];
			for (const [needle, what] of makerNeedles) {
				if (needle == null) continue;
				c.expectIn(MAKER, maker, needle, c.id, `make_deck.py figure "${what}" does not match the current artifact`);
			}
			// band easing: the transition row the video script cites
			const eased = (c.json('VVIP/showcase/band_easing.json') ?? []).find((o) => o.transitioned);
			if (eased) c.expectIn(DEMO, demo, `n=${eased.n}`, c.id, 'band-easing transition count', 'band_easing.json');
			const firedText = Object.entries(fired).map(([r, n]) => `${r} ×${n}`).join(' + ') || 'none';
			c.ok(c.id, `pursuit figures derivable (fired ${firedText}; chosen: ${figure.short ?? '?'}; trace ${tracePts})`);
		}
	},
	{
		id: 'pixel-proofs-match-shot-logs',
		local: true, // reads gitignored local run artifacts
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
			// the public /citizen shot counts the event cards it saw (by source)
			// on its pursuit-not-public line (showcase_canvas_shots.mjs cardNote)
			const citCards = probe('VVIP/showcase/shots_manifest.json', cit['pursuit-not-public']?.detail || '', /(\d+) card\(s\) by source/, 'citizen event-card count');
			const citCol = probe('VVIP/showcase/shots_manifest.json', cit['png-nonblank']?.detail || '', /png colours=(\d+)/, 'citizen PNG colour count');
			// the /ops wall shot (--ops-only, merged into the manifest) backs the
			// colour figure the deck quotes for it, when the manifest carries one
			const opsCol = (man.shots?.opsWall?.checks?.['png-nonblank']?.detail || '').match(/png colours=(\d+)/);
			const provenColours = new Set();
			const provenEntities = new Set();
			const provenCards = new Set();
			const needles = [];
			if (ents) { needles.push([`${ents[1]} entities`, 'city entities (shots_city.log)']); provenEntities.add(ents[1]); }
			if (cityCol) { needles.push([`${fmt(Number(cityCol[1]))}-colour`, 'city PNG colour proof (shots_city.log)']); provenColours.add(fmt(Number(cityCol[1]))); }
			if (pCol) { needles.push([`${fmt(Number(pCol[1]))}-colour`, 'pursuit PNG colour proof (shots_manifest.json)']); provenColours.add(fmt(Number(pCol[1]))); }
			if (citCards) { needles.push([`${citCards[1]} event cards`, 'citizen event-card count (shots_manifest.json)']); provenCards.add(citCards[1]); }
			if (citCol) { needles.push([`${fmt(Number(citCol[1]))}-colour`, 'citizen PNG colour proof (shots_manifest.json)']); provenColours.add(fmt(Number(citCol[1]))); }
			if (opsCol) { needles.push([`${fmt(Number(opsCol[1]))}-colour`, '/ops wall PNG colour proof (shots_manifest.json)']); provenColours.add(fmt(Number(opsCol[1]))); }
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
			const fired = ruleCounts(chain);
			const withheld = withheldCounts(chain);
			const cm = chosenFigure(chain).speed;
			const top = chain.interception?.top ?? {};
			const pts = chain.trace?.points;
			const speeds = chain.sightings?.speeds_kmh ?? [];
			const planned = sig.apply?.planned_junctions ?? [];
			const kc = bench.score_summary?.keyword_coverage;
			/** contradiction rules: pattern → (groups) must equal the artifact value
			 *  (a rule the run never fired counts 0 — a doc quoting fines the
			 *  current chain did not fire contradicts it) */
			const rules = [
				...RULE_IDS.map((rule) => [new RegExp(`${rule}[\`*]*\\s*(?:\u00d7|x)\\s*(\\d+)`, 'g'),
					() => [String(fired[rule] ?? 0), ...(withheld[rule] ? [String(withheld[rule])] : [])], `${rule} fired (or withheld) count`]),
				[/read at (\d+) km\/h in a (\d+) km\/h segment/g, () => (cm ? `${cm[1]}|${cm[2]}` : 'none in the current chain'), 'chosen e-challan segment'],
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
							const exp = [want()].flat();
							if (!exp.includes(got)) c.fail(rel, i + 1, c.id, `"${what}" quoted as ${got} but the artifact says ${exp.join(' or ')} (${m[0]})`);
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
		local: true, // reads gitignored local run artifacts
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
			// The camera drives /driver end-to-end only in its scripted fallback
			// (the window opens on the scripted approval: countdown=90s). In live
			// mode a request would be a real dispatch row that only a named
			// officer may open, so the run records the idle request card instead
			// (screens-verify.mjs) — judged by those checks, never by a countdown.
			const driverMode = screens.driver?.mode ?? 'scripted'; // manifests before the mode was recorded drove it
			let driverNote;
			if (driverMode === 'live') {
				for (const k of ['live mode: live corridor stretches on offer',
					'live mode: the request must name a 108 unit (button held until one is typed)',
					'live mode: nothing filed by the camera (still idle)']) {
					if (!drv[k]) c.fail(SCREENS_MANIFEST, 1, c.id, `driver (live mode) check "${k}" missing`);
				}
				if (drv['approved open window (live countdown)']) {
					c.fail(SCREENS_MANIFEST, 1, c.id, 'driver in live mode records an opened window — the camera must never file a live request');
				}
				driverNote = 'driver in live mode: idle request card, nothing filed';
			} else {
				// the scripted cardiac window is 90 s; the camera reads it just
				// after it opens, and the countdown can show 91 for its first
				// tick (the page clock lags the opening) — so 85..91 s, never a
				// different window length
				const cd = drv['approved open window (live countdown)'];
				const secs = Number((/countdown=(\d+)s/.exec(cd?.detail || '') || [])[1]);
				if (!cd || !(secs >= 85 && secs <= 91)) {
					c.fail(SCREENS_MANIFEST, 1, c.id, `driver "approved open window (live countdown)" check missing or its countdown (${Number.isFinite(secs) ? `${secs}s` : 'none'}) is not the 90-s scripted window read at its opening (85–91 s)`);
				}
				driverNote = `driver countdown=${secs}s of the 90-s scripted window`;
			}
			const deck = c.read(DECK) ?? '';
			c.expectIn(DECK, deck, '40/40', c.id, 'deck must quote the screens-verify result as 40/40');
			if (total === 40 && okCount === 40) c.ok(c.id, `40/40 checks ok incl. ${driverNote}`);
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
			// the hero shots may be re-taken apart from the run (showcase_canvas_shots.mjs
			// on the staged live state): then the trace they frame is the vehicle's
			// trace at capture time, and the deck must say so with the shot's own
			// figure — never present the shot's trace as the run's
			const recapture = man?.pursuitFeed?.points && man.pursuitFeed.points !== chain.trace?.points;
			if (recapture) {
				for (const [rel, t] of [[DECK, deck], [README, c.read(README) ?? '']]) {
					for (const needle of ['hero shots recaptured', `${man.pursuitFeed.points}-point trace`]) {
						c.expectIn(rel, t, needle, c.id,
							`shots_manifest.json pursuitFeed points ${man.pursuitFeed.points} differ from pursuit_chain trace points ${chain.trace?.points} — `
							+ 'a recapture of the hero shots apart from the run must be disclosed, never blended into the run (cross-run blend)');
					}
				}
			}
			c.ok(c.id, (split
				? `two runs disclosed (${s1} / ${s2})`
				: `single run ${s1}`)
				+ (recapture
					? `; shared vehicle verified; the hero shots' ${man.pursuitFeed.points}-point trace (recaptured apart from the run's ${chain.trace?.points}) disclosed`
					: '; shared-vehicle and trace-point agreement verified'));
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
		id: 'verify-all-deck-gates-wired',
		run(c) {
			const va = c.read(VERIFY_ALL) ?? '';
			// the deck UI tier (WP-15 T2) on the private preview, and the --live
			// tier signing in through the ONE shared helper
			for (const [needle, what] of [
				['node tests/ops-wall-smoke.mjs "$PREVIEW"', 'the hermetic ops-wall smoke (its C6 no-officer masking group) on the preview'],
				['node tests/contract/run.mjs', 'the deck contract replay'],
				['"$DECK"/tests/*.unit.mjs', 'every deck unit test'],
				['tests/edge-basemap.entry.ts', 'the edge-basemap entry'],
				['node tests/ops-write-smoke.mjs "$ORACLE_URL"', 'the ops-write smoke (GET-only probes)'],
				['CC_SMOKE_ONLY=plates', "city-command's C6 'plates' group (no officer named)"],
				['-u VEER_OPERATOR_PIN', 'previews gate-off: no session names an officer (C6)'],
				['-u VEER_CESIUM_ION_TOKEN', 'previews keyless'],
				['. "$VVIP_DIR/scripts/deck_session.sh"', 'the --live tier sourcing the shared sign-in helper'],
				['deck_signin "$deck"', 'the --live tier signing in through deck_signin']
			]) {
				c.expectIn(VERIFY_ALL, va, needle, c.id, `verify_all.sh must wire ${what}`);
			}
			// every pytest suite executes whole — skips are per test, never per suite
			const lines = va.split('\n');
			lines.forEach((line, i) => {
				if (/NOT_PURE_MODULES|\[SKIP\] pytest-|not pure-python/.test(line)) {
					c.fail(VERIFY_ALL, i + 1, c.id, `a whole pytest suite can be skipped again: ${line.trim().slice(0, 90)}`);
				}
			});
			// the legacy React tiers are retired from the gate …
			const RETIRED_TIERS = ['tier_frontend_bundle', 'tier_frontend_e2e', 'citizen-green-corridor.e2e.mjs'];
			for (const r of RETIRED_TIERS) {
				if (va.includes(r)) c.fail(VERIFY_ALL, c.lineOf(VERIFY_ALL, va, r), c.id, `verify_all.sh still runs the retired React frontend tier (${r})`);
			}
			// … so no paragraph about verify_all may still claim them as gate
			// tiers (history that says they were retired is fine)
			const RETIRED_CLAIMS = [/\bfrontend[- ](?:bundle|e2e)\b/i, /\b11-check e2e\b/i, /\bheadless e2e journey\b/i];
			let claims = 0;
			for (const rel of [DECK, MAKER, README, TECH_DOCS]) {
				const t = c.read(rel);
				if (t == null) { c.fail(rel, 1, c.id, 'doc missing — cannot scan the verify_all claims'); continue; }
				let at = 0;
				for (const para of t.split(/\n[ \t]*\n/)) {
					const start = t.indexOf(para, at);
					at = start + para.length;
					if (!/verify_all/.test(para) || /\bretired\b/i.test(para)) continue;
					for (const re of RETIRED_CLAIMS) {
						const m = para.match(re);
						if (!m) continue;
						const line = t.slice(0, start + m.index).split('\n').length;
						c.fail(rel, line, c.id, `describes verify_all with the retired React tier "${m[0]}" — the gate no longer runs it (say what it runs now, or say it was retired)`);
						claims++;
					}
				}
			}
			c.ok(c.id, `verify_all.sh wires the deck UI tier + the helper sign-in, skips no whole pytest suite, runs no React tier; ${claims ? claims + ' stale doc claim(s)' : 'no doc claims a retired tier'}`);
		}
	},
	{
		id: 'migrations-on-disk-match-docs',
		run(c) {
			let files = [];
			try { files = readdirSync(path.join(c.root, MIGRATIONS)).filter((f) => f.endsWith('.sql')).sort(); } catch {
				c.fail(MIGRATIONS, 1, c.id, 'migrations directory missing/unreadable');
				return;
			}
			// the list is DERIVED from disk: NNN_name.sql, contiguous from 001, no repeats
			const nums = [];
			for (const f of files) {
				const m = f.match(/^(\d{3})_[a-z0-9_]+\.sql$/);
				if (!m) { c.fail(MIGRATIONS, 1, c.id, `migration file not named NNN_name.sql: ${f}`); continue; }
				nums.push(Number(m[1]));
			}
			if (!nums.length) { c.fail(MIGRATIONS, 1, c.id, 'no migrations on disk'); return; }
			const max = Math.max(...nums);
			const pad = (n) => String(n).padStart(3, '0');
			for (let n = 1; n <= max; n++) {
				const k = nums.filter((x) => x === n).length;
				if (k === 0) c.fail(MIGRATIONS, 1, c.id, `gap in the ledger: no migration ${pad(n)} (001..${pad(max)} must be contiguous)`);
				if (k > 1) c.fail(MIGRATIONS, 1, c.id, `migration number ${pad(n)} used ${k} times`);
			}
			const top = pad(max);
			const range = `001${EN}${top}`;
			// the docs quote the range as on disk, and no other full range or
			// "applied n/n" count (a subset like "migrations 016–020" is fine, and
			// so is 001–006: the original schema the docs call byte-immutable)
			const SUBSETS_FROM_001 = ['006'];
			for (const rel of [DECK, README, TECH_DOCS]) {
				const t = c.read(rel);
				if (t == null) { c.fail(rel, 1, c.id, 'doc missing — cannot check the migration range'); continue; }
				if (!new RegExp(`\\b001[${EN}-]${top}\\b`).test(t)) {
					c.fail(rel, 1, c.id, `must quote the migration range as on disk: "${range}" (${max} files)`);
				}
				t.split('\n').forEach((line, i) => {
					for (const m of line.matchAll(new RegExp(`\\b001[${EN}-](\\d{3})\\b`, 'g'))) {
						if (m[1] !== top && !SUBSETS_FROM_001.includes(m[1])) c.fail(rel, i + 1, c.id, `quotes migrations 001${EN}${m[1]} but ${range} are on disk`);
					}
					for (const m of line.matchAll(/\bapplied(?: live)? (\d+)\/(\d+)\b/g)) {
						if (Number(m[1]) !== max || Number(m[2]) !== max) c.fail(rel, i + 1, c.id, `quotes "applied ${m[1]}/${m[2]}" but ${max} migrations are on disk`);
					}
				});
			}
			c.ok(c.id, `migrations ${range} on disk (${max} files, contiguous) — the range DECK/README/TECHNICAL_DOCS quote`);
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
			// the URL constants: full-URL override + port override, the live
			// argus stack's ports as defaults (scripts/veer_env.sh)
			for (const [line, url, port] of [
				['ORACLE_URL="${ORACLE_URL:-http://127.0.0.1:${ORACLE_PORT:-8091}}"', 'ORACLE_URL', 8091],
				['BRAIN_URL="${BRAIN_URL:-http://127.0.0.1:${BRAIN_PORT:-8092}}"', 'BRAIN_URL', 8092],
				['EMULATOR_URL="${EMULATOR_URL:-http://127.0.0.1:${EMULATOR_PORT:-8090}}"', 'EMULATOR_URL', 8090],
				['DECIDER_URL="${DECIDER_URL:-http://127.0.0.1:${DECIDER_PORT:-8083}}"', 'DECIDER_URL', 8083],
				['LLAMA_URL="${LLAMA_URL:-http://127.0.0.1:${LLAMA_PORT:-8084}}"', 'LLAMA_URL', 8084],
				['EMBED_URL="${EMBED_URL:-http://127.0.0.1:${EMBED_PORT:-8085}}"', 'EMBED_URL', 8085],
				['DECK_CONTAINER_URL="${DECK_URL:-http://127.0.0.1:${DECK_PORT:-5273}}"', 'DECK_CONTAINER_URL', 5273]
			]) {
				c.expectIn(rel, t, line, c.id, `endpoint constant ${url} must be env-overridable (the live port ${port} as default)`);
			}
			c.expectIn(rel, t, 'DECK_ORIGIN="$DECK_CONTAINER_URL"', c.id, 'the canvas origin must be the ONE deck constant, not a second hardcoded copy');
			for (const needle of [
				'COMPOSE_PROJECT="${COMPOSE_PROJECT:-vvip-argus}"',
				'${POSTGIS_CONTAINER:-${COMPOSE_PROJECT}-postgis}',
				'PSQL="docker exec $PG_CONTAINER psql -U $PG_USER -d $PG_DB -At"'
			]) {
				c.expectIn(rel, t, needle, c.id, 'fleet addressing must be parametrized (project/containers/DB) with the live (argus) defaults');
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
			// the operator gate: ONE shared sign-in helper, never a PIN of its own
			c.expectIn(rel, t, '. "$VVIP_DIR/scripts/deck_session.sh"', c.id, 'the showcase must sign in through the shared helper (scripts/deck_session.sh)');
			c.expectIn(rel, t, 'deck_signin "$DECK_ORIGIN"', c.id, 'the showcase must sign in to its deck origin with deck_signin');
			t.split('\n').forEach((line, i) => {
				if (/VEER_OPERATOR_PIN=\S|"pin"\s*:/.test(line) && !/^\s*#/.test(line)) {
					c.fail(rel, i + 1, c.id, `the showcase handles the operator PIN itself — only scripts/deck_session.sh may: ${line.trim().slice(0, 80)}`);
				}
			});
			// phase 6 (C-N, the user's decision): the pursuit is authorised from
			// an evidence-only R_STOLEN_SIGHTING under the honest scripted-officer
			// label, and an e-challan approval on the stolen plate must be refused
			for (const [needle, what] of [
				['R_STOLEN_SIGHTING', 'the evidence-only stolen-vehicle sighting rule'],
				['pursuit authorised on a reported-stolen vehicle (evidence, no fine)', "phase 6's approval note (the user's wording)"],
				['Scripted demo officer (showcase script)', 'the honest officer label of every unattended approval']
			]) {
				c.expectIn(rel, t, needle, c.id, `showcase phase 6 must carry ${what}`);
			}
			if (!t.split('\n').some((line) => /\b409\b/.test(line) && /e-challan|penal/i.test(line))) {
				c.fail(rel, 1, c.id, 'showcase phase 6 must assert that an e-challan (penal) approval on the stolen plate is refused 409 (C-N)');
			}
			if (/e-challan approved per/.test(t)) {
				c.fail(rel, c.lineOf(rel, t, 'e-challan approved per'), c.id, 'showcase still approves an e-challan on the reported-stolen vehicle (C-N: evidence only, never a fine)');
			}
			c.ok(c.id, 'showcase_run.sh: bash -n ok, endpoints/containers env-overridable (argus defaults), cockpit health contract, shared sign-in, phase 6 evidence-only + 409');
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
				for (const needle of renderedDeckNeedles(chain, bench)) {
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
			// the offline ladder, then the --live tiers: BOTH the function
			// definition AND the main() call site must exist, in this order —
			// a deleted definition with the call left in would die at runtime
			// with command-not-found, so it is a failure here, not a passing grep
			const ladders = [
				['tier_scripts_syntax', 'tier_gods_eye_check', 'tier_gods_eye_canvas', 'tier_design_smoke', 'tier_deck_ui',
					'tier_pytest_suites', 'tier_cargo', 'tier_display_geometry', 'tier_ingress_segments',
					'tier_submission_consistency', 'tier_wave1_hatches', 'tier_wave2_forecaster',
					'tier_wave2_routing', 'tier_wave2_tier0', 'tier_wave3_repetition', 'tier_wave3_ledger'],
				['tier_live', 'tier_demo_reset', 'tier_live_smokes', 'tier_models', 'tier_model_drift']
			];
			for (const order of ladders) {
				let last = -1;
				for (const tier of order) {
					const def = new RegExp(`^${tier}\\(\\)\\s*\\{`, 'm').exec(va);
					const call = new RegExp(`^\\s+${tier}$`, 'm').exec(va);
					if (!def) { c.fail(VERIFY_ALL, defLine(va, tier), c.id, `verify_all.sh lost the ${tier}() function definition (main() would die: command not found)`); continue; }
					if (!call) { c.fail(VERIFY_ALL, 1, c.id, `verify_all.sh lost the main() call to ${tier}`); continue; }
					if (call.index < last) c.fail(VERIFY_ALL, va.slice(0, call.index).split('\n').length, c.id, `verify_all.sh tier ${tier} out of the pinned main() order`);
					last = call.index;
				}
			}
			c.expectIn(VERIFY_ALL, va, 'ALL TIERS PASSED', c.id, 'verify_all.sh must keep its terminal pass line');
			c.expectIn(VERIFY_ALL, va, 'node tests/design-smoke.mjs', c.id, 'verify_all.sh must run the cockpit-topology tripwire (tests/design-smoke.mjs)');
			c.expectIn(VERIFY_ALL, va, 'scripts/gen_display_geometry.py --verify', c.id, 'verify_all.sh must re-measure the OSM network (gen_display_geometry.py --verify)');
			c.expectIn(VERIFY_ALL, va, 'scripts/gen_ingress_segments.py --check', c.id, "verify_all.sh must check signal-ingress's compiled-in network against the store (gen_ingress_segments.py --check)");
			c.expectIn(VERIFY_ALL, va, 'cargo test --locked', c.id, 'verify_all.sh must run the signal-ingress cargo tests');
			// the stack-bound tools that cannot run hermetically are at least
			// parsed offline, and the pre-judging dry-run is a --live tier
			for (const needle of ['node --check scripts/journeys.mjs', 'bash -n scripts/demo_reset.sh',
				'scripts/data/journeys/reboot_evidence.json', 'scripts/demo_reset.sh" 2>&1', 'dry-run: nothing was written']) {
				c.expectIn(VERIFY_ALL, va, needle, c.id, `verify_all.sh must keep the stack-bound tools gated (${needle})`);
			}
			c.ok(c.id, `tier definitions + call sites intact in the pinned order (${ladders[0].length} offline, ${ladders[1].length} --live)`);
		}
	},
	{
		id: 'post-push-origin-main',
		postPush: true,
		run(c) {
			const repo = path.join(c.root, 'VVIP');
			const git = (...a) => spawnSync('git', ['-C', repo, ...a], { encoding: 'utf8', timeout: 60000 });
			const head = git('rev-parse', 'HEAD');
			if (head.status !== 0) { c.fail('VVIP/.git', 1, c.id, `git rev-parse HEAD failed: ${(head.stderr || '').trim().slice(0, 120)}`); return; }
			const local = head.stdout.trim();
			const ls = git('ls-remote', 'origin', 'refs/heads/main');
			if (ls.status !== 0) { c.fail('VVIP/.git', 1, c.id, `git ls-remote origin refs/heads/main failed: ${(ls.stderr || '').trim().slice(0, 120)}`); return; }
			const remote = (ls.stdout.trim().split(/\s+/)[0] || '');
			if (!remote) { c.fail('VVIP/.git', 1, c.id, 'origin has no main branch'); return; }
			if (remote !== local) {
				c.fail('VVIP/.git', 1, c.id, `HEAD ${local.slice(0, 12)} is not origin/main ${remote.slice(0, 12)} — the push did not land (or HEAD moved after it)`);
				return;
			}
			// the pushed tree (== HEAD, so its objects are local) carries the licence
			const lic = git('cat-file', '-p', `${remote}:LICENSE`);
			if (lic.status !== 0 || !lic.stdout.trim()) c.fail('VVIP/LICENSE', 1, c.id, `origin/main ${remote.slice(0, 12)} carries no (or an empty) LICENSE`);
			else c.ok(c.id, `HEAD == origin/main ${remote.slice(0, 12)}; LICENSE on origin (${lic.stdout.trim().split('\n')[0].slice(0, 40)})`);
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
				c.fail(PDF, 1, c.id, `pdftotext failed on the rendered PDF: ${(ex.stderr || '').replace(/\s+/g, ' ').trim().slice(0, 120)}`);
				return;
			}
			const text = (ex.stdout || '').replace(/\s+/g, ' ');
			for (const bad of ['redirects to /ops', '0.548', 'score 0.7', '72-position', `95${EN}114`, 'junction 1, signal-controlled']) {
				if (text.includes(bad)) c.fail(PDF, 1, c.id, `rendered PDF still carries the stale figure "${bad}" — re-convert from the corrected pptx`);
			}
			const chain = c.json('VVIP/showcase/pursuit_chain.json');
			const bench = c.json('VVIP/scripts/bench_adversarial_report.json');
			if (chain && bench) {
				for (const needle of renderedDeckNeedles(chain, bench)) {
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
		if (check.postPush && !opts.postPush) {
			c.skip(check.id, 'before the push — run with --post-push once origin/main is pushed');
			continue;
		}
		if (check.local && opts.vvipOnly) {
			c.skip(check.id, 'judges gitignored local run artifacts (shot logs, screens manifest) a clean checkout does not carry');
			continue;
		}
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
	...['ops', 'gods-eye', 'citizen', 'driver', 'comms', 'chat'].map((s) => `VVIP/services/command-deck/tests/artifacts/screens/screen_${s}.png`)
];
// the deck src (route scan) and the migration ledger (derived from disk) whole
const FIXTURE_DIRS = [DECK_SRC, MIGRATIONS];

// the pytest check runs against a stub under selftest (the fixture carries
// no service suites). Its numbers are the fresh gate-venv collection of the
// deployed tree (2026-10-24, after the live tier-0 fix: convoy-brain 635 =
// 634 passed + 1 skipped; 4+635+22+107+113+328+1500 = 2709); the three suites and the
// total the docs quote are re-read from the pristine docs at fixture-build
// time, so the baseline never pins a collection the docs were since moved to
// (default mode compares the docs with a LIVE collection).
const PYTEST_STUB = {
	total: 2709,
	perSuite: {
		'VVIP/services/command-deck/tests': 4,
		'VVIP/services/convoy-brain/tests': 635,
		'VVIP/services/corridor-store/importer/tests': 22,
		'VVIP/services/corridor-store/tests': 107,
		'VVIP/services/embedding-service/tests': 113,
		'VVIP/services/event-emulator/tests': 328,
		'VVIP/services/traffic-oracle/tests': 1500
	}
};

/** PYTEST_STUB with the counts the (pristine) docs quote, where they quote one */
function pytestStubFromDocs(root) {
	const read = (rel) => { try { return readFileSync(path.join(root, rel), 'utf8').replace(/\s+/g, ' '); } catch { return ''; } };
	const deck = read(DECK);
	const tech = read(TECH_DOCS);
	const readme = read(README);
	const first = (...pairs) => {
		for (const [re, text] of pairs) {
			const m = text.match(re);
			if (m) return Number(m[1]);
		}
		return null;
	};
	const per = { ...PYTEST_STUB.perSuite };
	const set = (suite, n) => { if (n != null) per[suite] = n; };
	set('VVIP/services/convoy-brain/tests', first([/convoy-brain \*\*(\d+)\*\*/, tech], [/convoy-brain (\d+)/, readme]));
	set('VVIP/services/event-emulator/tests', first([/event-emulator \*\*(\d+)\*\*/, tech], [/event-emulator (\d+)/, readme], [/emulator's (\d+)/, deck]));
	set('VVIP/services/traffic-oracle/tests', first([/traffic-oracle \*\*(\d+)\*\*/, tech], [/traffic-oracle (\d+)/, readme], [/traffic-oracle's (\d+)/, deck]));
	const total = first([/(\d+) pytest/, deck]) ?? Object.values(per).reduce((a, b) => a + b, 0);
	return { total, perSuite: per };
}

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

/* Each mutation makes ONE known-bad change through the fixture handle m
 * (m.edit / m.add / m.remove / m.json — every touched path is restored before
 * the next one) and names the check that must FAIL on it. Values come from
 * the fixture's own artifacts, so a regenerated artifact never turns a
 * mutation into a no-op. scope: 'vvip' runs everywhere, 'workspace' needs
 * ../submission, 'local' the gitignored run artifacts (both skipped under
 * --vvip-only). */
const MUTATIONS = [
	{
		id: 'no-stale-root-redirect-claims', scope: 'workspace',
		desc: 'DECK.md re-describes the retired 301-to-/ops behaviour',
		apply(m) { m.edit(DECK, (t) => `${t}\n\nSTALE: \`/\` redirects to /ops via a 301 — the old front door.\n`); }
	},
	{
		id: 'no-stale-root-redirect-claims', scope: 'vvip',
		desc: 'README.md re-describes the retired 301-to-/ops behaviour',
		apply(m) { m.edit(README, (t) => `${t}\n\nThe deck root redirects to /ops (301).\n`); }
	},
	{
		id: 'bench-figures-match-artifacts', scope: 'workspace',
		desc: 'DECK.md quotes a keyword coverage the bench artifact does not say',
		apply(m) {
			const kc = m.json('VVIP/scripts/bench_adversarial_report.json').score_summary.keyword_coverage;
			m.edit(DECK, (t) => t.replaceAll(`coverage **${kc}**`, 'coverage **0.548**'));
		}
	},
	{
		id: 'showcase-pursuit-figures-derivable', scope: 'workspace',
		desc: "DECK.md quotes an earlier run's trace length everywhere",
		apply(m) {
			const pts = m.json('VVIP/showcase/pursuit_chain.json').trace.points;
			m.edit(DECK, (t) => t.replaceAll(`${pts}-position trace`, `${pts + 68}-position trace`));
		}
	},
	{
		id: 'pixel-proofs-match-shot-logs', scope: 'local',
		desc: 'DECK.md adds a colour proof no shot log backs',
		apply(m) { m.edit(DECK, (t) => `${t}\n\nScreenshots: a 1,234,567-colour decode proof of the city.\n`); }
	},
	{
		id: 'pixel-proofs-match-shot-logs', scope: 'local',
		desc: 'shots_city.log wording drifts so the entity probe cannot parse (reviewer probe)',
		apply(m) { m.edit('VVIP/showcase/shots_city.log', (t) => t.replace(/entities=\d+/, 'entities=thirty-eight')); }
	},
	{
		id: 'doc-paths-exist', scope: 'workspace',
		desc: 'DECK.md cites a nonexistent artifact',
		apply(m) { m.edit(DECK, (t) => `${t}\n\nEvidence: \`VVIP/scripts/impact_results_missing.json\`.\n`); }
	},
	{
		id: 'root-serves-cockpit-200', scope: 'vvip',
		desc: 'the root route loses the cockpit template marker',
		apply(m) { m.edit(ROUTE_ROOT, (t) => t.replaceAll('data-template="convoy-command"', 'data-template="stale-root"')); }
	},
	{
		id: 'root-serves-cockpit-200', scope: 'vvip',
		desc: 'a deck route answers with a 301 rewrite again (code, not a comment)',
		apply(m) { m.edit('VVIP/services/command-deck/src/hooks.server.ts', (t) => `${t}\nexport const stale = () => redirect(301, '/ops');\n`); }
	},
	{
		id: 'impact-json-single-source', scope: 'vvip',
		desc: 'impact_results.json central baseline drifts from the pinned value',
		apply(m) {
			m.edit('VVIP/scripts/impact_results.json', (t) => {
				const d = JSON.parse(t);
				d.scenarios.central.commuter_minutes_baseline += 1000;
				return JSON.stringify(d, null, 2);
			});
		}
	},
	{
		id: 'screens-verify-40of40', scope: 'local',
		desc: 'one screens-manifest check flips to not-ok',
		apply(m) { m.edit(SCREENS_MANIFEST, (t) => t.replace('"ok": true', '"ok": false')); }
	},
	{
		id: 'screens-verify-40of40', scope: 'local',
		desc: 'the scripted /driver window is no longer the 90-s cardiac window (countdown 60 s)',
		apply(m) { m.edit(SCREENS_MANIFEST, (t) => t.replace(/countdown=\d+s/, 'countdown=60s')); }
	},
	{
		id: 'screens-verify-40of40', scope: 'local',
		desc: 'a live-mode /driver run records an opened window (the camera filed a live request)',
		apply(m) {
			m.edit(SCREENS_MANIFEST, (t) => {
				const d = JSON.parse(t);
				d.screens.driver.mode = 'live';
				return JSON.stringify(d, null, 2);
			});
		}
	},
	{
		id: 'verify-all-deck-gates-wired', scope: 'vvip',
		desc: 'verify_all.sh drops the hermetic ops-wall smoke from the deck UI tier',
		apply(m) { m.edit(VERIFY_ALL, (t) => t.replace(/^.*node tests\/ops-wall-smoke\.mjs "\$PREVIEW".*$/m, '    :')); }
	},
	{
		id: 'verify-all-deck-gates-wired', scope: 'vvip',
		desc: 'verify_all.sh skips a whole pytest suite on a missing module again',
		apply(m) { m.edit(VERIFY_ALL, (t) => t.replace('tier_pytest_suites() {', 'NOT_PURE_MODULES="torch|onnx"\ntier_pytest_suites() {')); }
	},
	{
		id: 'verify-all-deck-gates-wired', scope: 'vvip',
		desc: 'README.md describes verify_all with the retired React e2e tier',
		apply(m) { m.edit(README, (t) => `${t}\n\n\`scripts/verify_all.sh\` runs the frontend e2e journey first, then the deck tiers.\n`); }
	},
	{
		id: 'migrations-on-disk-match-docs', scope: 'vvip',
		desc: 'migration 011 disappears (a gap in the ledger)',
		apply(m) { m.remove(`${MIGRATIONS}/011_city_coordination.sql`); }
	},
	{
		id: 'migrations-on-disk-match-docs', scope: 'vvip',
		desc: 'a new migration lands while the docs still quote the old range',
		apply(m) {
			const n = m.list(MIGRATIONS).filter((f) => /^\d{3}_/.test(f)).length;
			m.add(`${MIGRATIONS}/${String(n + 1).padStart(3, '0')}_selftest_probe.sql`, '-- selftest\n');
		}
	},
	{
		id: 'migrations-on-disk-match-docs', scope: 'vvip',
		desc: 'TECHNICAL_DOCS.md quotes a stale full migration range',
		apply(m) { m.edit(TECH_DOCS, (t) => `${t}\n\nThe store ledger holds migrations 001${EN}011.\n`); }
	},
	{
		id: 'smoke-models-ports-env-overridable', scope: 'vvip',
		desc: 'smoke_models.sh hardcodes the decider endpoint again',
		apply(m) { m.edit(SMOKE_MODELS, (t) => t.replace('${MODEL_HOST_URL:-http://127.0.0.1:8083}', 'http://127.0.0.1:8083')); }
	},
	{
		id: 'showcase-endpoints-env-overridable', scope: 'vvip',
		desc: 'showcase_run.sh hardcodes ORACLE_URL again',
		apply(m) {
			m.edit(SHOWCASE_RUN, (t) => t.replace('ORACLE_URL="${ORACLE_URL:-http://127.0.0.1:${ORACLE_PORT:-8091}}"', 'ORACLE_URL="http://127.0.0.1:8091"'));
		}
	},
	{
		id: 'showcase-endpoints-env-overridable', scope: 'vvip',
		desc: 'showcase phase 6 approves an e-challan on the stolen plate again (C-N)',
		apply(m) {
			m.edit(SHOWCASE_RUN, (t) => t.replaceAll('pursuit authorised on a reported-stolen vehicle (evidence, no fine)', 'e-challan approved per FIR'));
		}
	},
	{
		id: 'pptx-claims-fresh', scope: 'workspace',
		desc: 'the pptx regresses to stale slide text',
		apply(m) {
			m.add(PPTX, makeFakeZip([
				['ppt/slides/slide4.xml',
					'<a:t>/ redirects to /ops, the situation wall (top: junction 1, signal-controlled, score 0.7) '
					+ `+ live 72-position trace at 95${EN}114 km/h; keyword coverage 0.548</a:t>`]
			]));
		}
	},
	{
		id: 'pytest-counts-match-deck', scope: 'workspace',
		desc: 'DECK.md quotes an old pytest total',
		apply(m) { m.edit(DECK, (t) => t.replace(`${m.stub.total} pytest`, '875 pytest')); }
	},
	{
		id: 'pytest-counts-match-deck', scope: 'vvip',
		desc: 'README.md quotes an old traffic-oracle count',
		apply(m) {
			const n = m.stub.perSuite['VVIP/services/traffic-oracle/tests'];
			m.edit(README, (t) => t.replaceAll(`traffic-oracle ${n}`, `traffic-oracle ${n + 7}`));
		}
	},
	{
		id: 'showcase-artifacts-single-run', scope: 'workspace',
		desc: 'DECK.md cites a stale showcase run stamp',
		apply(m) {
			const s1 = new Date(m.json('VVIP/showcase/showcase_results.json').generated_at_utc).toISOString().slice(0, 16) + 'Z';
			m.edit(DECK, (t) => t.replaceAll(s1, '2026-10-18T14:29Z'));
		}
	},
	{
		id: 'showcase-artifacts-single-run', scope: 'vvip',
		desc: 'the hero pursuit shot frames a trace no doc discloses (recaptured apart from the run, its figure never quoted)',
		apply(m) {
			const pts = m.json('VVIP/showcase/pursuit_chain.json').trace.points;
			m.edit('VVIP/showcase/shots_manifest.json', (t) => {
				const d = JSON.parse(t);
				d.pursuitFeed = { ...(d.pursuitFeed ?? {}), points: pts + 50 };
				return JSON.stringify(d, null, 2);
			});
		}
	},
	{
		id: 'verify-all-tier-inventory', scope: 'vvip',
		desc: 'verify_all.sh loses the tier_models() definition (call kept → runtime crash)',
		apply(m) { m.edit(VERIFY_ALL, (t) => t.replace('tier_models() {', 'tier_models_GONE() {')); }
	},
	{
		id: 'verify-all-tier-inventory', scope: 'vvip',
		desc: 'verify_all.sh runs the cargo tier before the pytest suites (out of the pinned order)',
		apply(m) {
			m.edit(VERIFY_ALL, (t) => t.replace(/^(\s+)tier_pytest_suites\n(\s+)tier_cargo$/m, '$1tier_cargo\n$2tier_pytest_suites'));
		}
	},
	{
		id: 'pdf-claims-fresh', scope: 'workspace',
		desc: 'the rendered PDF is replaced with a non-PDF carrying stale figures',
		apply(m) { m.add(PDF, 'stale: / redirects to /ops, 72-position trace, keyword coverage 0.548\n'); }
	},
	{
		id: 'showcase-pursuit-figures-derivable', scope: 'vvip',
		desc: 'the pursuit artifact records an e-challan approval on the stolen plate that was NOT refused (C-N)',
		apply(m) {
			m.edit('VVIP/showcase/pursuit_chain.json', (t) => {
				const d = JSON.parse(t);
				d.penal_approval_probe = { exercised: true, http: 200, rule_id: 'R_SPD_SEGMENT' };
				return JSON.stringify(d, null, 2);
			});
		}
	},
	{
		id: 'showcase-pursuit-figures-derivable', scope: 'workspace',
		desc: 'the twin replay has no MAE (no_data) while the deck still quotes one',
		apply(m) {
			// the replay forced to no_data (written whole: the live artifact may
			// already be no_data), and a measured-sounding MAE quote put back in
			const d = m.json('VVIP/showcase/signal_policy.json');
			d.replay = { ...d.replay, status: 'no_data', speed_mae_kmh: null, mae_tolerance_kmh: null };
			m.add('VVIP/showcase/signal_policy.json', JSON.stringify(d, null, 2));
			m.edit(DECK, (t) => `${t}\n\nThe guard refused: twin replay MAE 7.2 km/h vs the 5.0 gate.\n`);
		}
	},
	{
		id: 'adjacent-docs-match-artifacts', scope: 'vvip',
		desc: 'TECHNICAL_DOCS.md quotes a fired count the pursuit artifact contradicts',
		apply(m) { m.edit(TECH_DOCS, (t) => `${t}\n\nThe pursuit fired \`R_SPD_SEGMENT\` ×999 this run.\n`); }
	}
];

/* Benign changes the named check must NOT fail on (the false-positive guards). */
const BENIGN = [
	{
		id: 'root-serves-cockpit-200', scope: 'vvip',
		desc: 'deck src comments that talk about redirects, and fetch() with redirect:\'manual\'',
		apply(m) {
			m.edit('VVIP/services/command-deck/src/hooks.server.ts', (t) => `${t}\n// a Response.redirect from an endpoint is copied, never followed\n`
				+ '/* a 301 here would be a redirect —\n * this block only talks about one */\n'
				+ "export const probeHealth = (u) => fetch(u, { redirect: 'manual' });\n");
		}
	},
	{
		id: 'verify-all-deck-gates-wired', scope: 'vvip',
		desc: 'README.md history that says the React e2e tier was retired from verify_all',
		apply(m) { m.edit(README, (t) => `${t}\n\nThe legacy React frontend e2e journey was retired from \`scripts/verify_all.sh\` in wave 4.\n`); }
	},
	{
		id: 'screens-verify-40of40', scope: 'local',
		desc: 'the camera read the 90-s scripted window on its first tick (countdown 91 s), or 5 s in (85 s)',
		// another in-window reading than the manifest's own: 91 s, or 85 s when it already says 91
		apply(m) { m.edit(SCREENS_MANIFEST, (t) => t.replace(/countdown=(\d+)s/, (_, s) => `countdown=${s === '91' ? 85 : 91}s`)); }
	},
	{
		id: 'screens-verify-40of40', scope: 'local',
		desc: 'a live-mode /driver run: the idle request card checked, nothing filed (still 7 driver checks, 40/40)',
		apply(m) {
			m.edit(SCREENS_MANIFEST, (t) => {
				const d = JSON.parse(t);
				const drv = d.screens.driver;
				const keep = ['flow mounts at idle', 'golden-hour counter ticks', 'no-page-errors', 'png-nonblank'];
				const checks = Object.fromEntries(keep.map((k) => [k, drv.checks[k] ?? { ok: true, detail: '' }]));
				checks['live mode: live corridor stretches on offer'] = { ok: true, detail: 'mode=live stretches=4' };
				checks['live mode: the request must name a 108 unit (button held until one is typed)'] = { ok: true, detail: 'held=true' };
				checks['live mode: nothing filed by the camera (still idle)'] = { ok: true, detail: 'phase=idle' };
				d.screens.driver = { ...drv, mode: 'live', checks };
				return JSON.stringify(d, null, 2);
			});
		}
	}
];

/** a fixture handle: mutations go through it so every touched path is restored */
function fixtureHandle(fx, stub) {
	const pristine = new Map(); // rel -> Buffer | null (absent before the mutation)
	const remember = (rel) => {
		if (pristine.has(rel)) return;
		const p = path.join(fx, rel);
		pristine.set(rel, existsSync(p) ? readFileSync(p) : null);
	};
	return {
		stub,
		read: (rel) => readFileSync(path.join(fx, rel), 'utf8'),
		json: (rel) => JSON.parse(readFileSync(path.join(fx, rel), 'utf8')),
		list: (rel) => readdirSync(path.join(fx, rel)),
		edit(rel, fn) {
			remember(rel);
			const p = path.join(fx, rel);
			const before = readFileSync(p, 'utf8');
			const after = fn(before);
			if (after === before) throw new Error(`mutation left ${rel} unchanged (its anchor is gone)`);
			writeFileSync(p, after);
		},
		add(rel, data) {
			remember(rel);
			mkdirSync(path.dirname(path.join(fx, rel)), { recursive: true });
			writeFileSync(path.join(fx, rel), data);
		},
		remove(rel) {
			remember(rel);
			rmSync(path.join(fx, rel));
		},
		restore() {
			for (const [rel, buf] of pristine) {
				const p = path.join(fx, rel);
				if (buf == null) rmSync(p, { force: true });
				else writeFileSync(p, buf);
			}
			pristine.clear();
		}
	};
}

/** copy a tree, skipping dependency/build dirs */
function copyTree(from, to) {
	mkdirSync(to, { recursive: true });
	for (const e of readdirSync(from, { withFileTypes: true })) {
		const f = path.join(from, e.name);
		const t = path.join(to, e.name);
		if (e.isDirectory()) {
			if (!['node_modules', '.svelte-kit', 'build'].includes(e.name)) copyTree(f, t);
		} else if (e.isFile()) copyFileSync(f, t);
	}
}

/** the post-push check against a throwaway origin: green when HEAD == origin/main
 *  with a LICENSE, red when HEAD is ahead, red when the pushed tree lost LICENSE */
function selftestPostPush() {
	const tmp = mkdtempSync(path.join(tmpdir(), 'veer-postpush-'));
	const check = CHECKS.find((x) => x.id === 'post-push-origin-main');
	const git = (cwd, ...a) => spawnSync('git', ['-c', 'user.name=selftest', '-c', 'user.email=selftest@invalid',
		'-c', 'commit.gpgsign=false', '-c', 'init.defaultBranch=main', '-c', 'core.hooksPath=/dev/null', ...a],
	{ cwd, encoding: 'utf8' });
	const run = () => {
		const c = new Ctx(tmp, { postPush: true });
		c.id = check.id;
		check.run(c);
		return c.failures.filter((f) => f.check === check.id);
	};
	let exit = 0;
	try {
		const origin = path.join(tmp, 'origin.git');
		const repo = path.join(tmp, 'VVIP');
		mkdirSync(repo);
		git(tmp, 'init', '-q', '--bare', origin);
		git(repo, 'init', '-q');
		writeFileSync(path.join(repo, 'LICENSE'), 'MIT License\n');
		git(repo, 'add', 'LICENSE');
		git(repo, 'commit', '-q', '-m', 'licence');
		git(repo, 'remote', 'add', 'origin', origin);
		const pushed = git(repo, 'push', '-q', 'origin', 'HEAD:refs/heads/main');
		if (pushed.status !== 0) throw new Error(`git push to the throwaway origin failed: ${pushed.stderr}`);
		const green = run();
		writeFileSync(path.join(repo, 'later.txt'), 'not pushed\n');
		git(repo, 'add', 'later.txt');
		git(repo, 'commit', '-q', '-m', 'not pushed');
		const ahead = run();
		git(repo, 'rm', '-q', 'LICENSE');
		git(repo, 'commit', '-q', '-m', 'licence dropped');
		git(repo, 'push', '-q', 'origin', 'HEAD:refs/heads/main');
		const unlicensed = run();
		if (green.length === 0 && ahead.length > 0 && unlicensed.length > 0) {
			console.log('selftest: post-push-origin-main — green on HEAD == origin/main + LICENSE; FAILS when HEAD is ahead and when the pushed tree has no LICENSE');
		} else {
			exit = 1;
			console.error(`selftest: post-push-origin-main misjudged a throwaway origin (green=${green.length} ahead=${ahead.length} unlicensed=${unlicensed.length} findings): ${JSON.stringify([...green, ...ahead, ...unlicensed].slice(0, 2))}`);
		}
	} catch (e) {
		exit = 1;
		console.error(`selftest: post-push probe could not run: ${e.message}`);
	} finally {
		rmSync(tmp, { recursive: true, force: true });
	}
	return exit;
}

async function selftest() {
	const fx = mkdtempSync(path.join(tmpdir(), 'veer-subcons-'));
	const opts = { vvipOnly: VVIP_ONLY };
	const inScope = (scope) => !VVIP_ONLY || scope === 'vvip';
	let exit = 0;
	try {
		// build the fixture: every file the oracle reads (a VVIP-only checkout
		// carries neither the workspace documents nor the local run artifacts)
		const notHere = [];
		for (const rel of FIXTURE_FILES) {
			const src = path.join(ROOT, rel);
			if (!existsSync(src)) {
				if (VVIP_ONLY && (isWorkspacePath(rel) || isLocalArtifact(rel))) { notHere.push(rel); continue; }
				console.error(`selftest: fixture source missing: ${rel}`);
				exit = 1;
				continue;
			}
			const dst = path.join(fx, rel);
			mkdirSync(path.dirname(dst), { recursive: true });
			copyFileSync(src, dst);
		}
		if (notHere.length) console.log(`selftest: VVIP-only checkout — ${notHere.length} workspace/local file(s) not in it, their checks and mutations not run`);
		for (const rel of FIXTURE_DIRS) {
			const src = path.join(ROOT, rel);
			if (existsSync(src)) copyTree(src, path.join(fx, rel));
			else mkdirSync(path.join(fx, rel), { recursive: true });
		}
		// every path the submission docs cite must exist in the fixture too
		// (doc-paths-exist judges existence; the docs may cite new files)
		for (const [rel, fn] of [[DECK, mdPathRefs], [DEMO, mdPathRefs], [MAKER, pyPathRefs]]) {
			const t = existsSync(path.join(ROOT, rel)) ? readFileSync(path.join(ROOT, rel), 'utf8') : null;
			if (t == null) continue;
			for (const ref of fn(t, rel)) {
				const bases = ref.bare ? BARE_BASES : PATH_BASES;
				for (const b of bases) {
					const hit = expandBraces(cleanToken(ref.token)).map((tok) => path.join(b, tok)).find((r) => existsSync(path.join(ROOT, r)));
					if (!hit) continue;
					const dst = path.join(fx, hit);
					if (!existsSync(dst)) {
						if (statSync(path.join(ROOT, hit)).isDirectory()) mkdirSync(dst, { recursive: true });
						else { mkdirSync(path.dirname(dst), { recursive: true }); copyFileSync(path.join(ROOT, hit), dst); }
					}
					break;
				}
			}
		}
		const stub = pytestStubFromDocs(fx);
		// baseline: the faithful copies must be all green (pytest stubbed)
		const base = runAllChecks(fx, { ...opts, pytestStub: stub });
		if (base.failures.length === 0) {
			console.log(`selftest: baseline GREEN on the temp fixture (${CHECKS.length} checks, pytest stubbed${VVIP_ONLY ? ', VVIP-only' : ''})`);
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
		const m = fixtureHandle(fx, stub);
		// a mutation is judged by the findings it ADDS over the baseline, so a
		// baseline that is not green (docs behind the tree) cannot mask a
		// mutation, nor turn a benign change into a false positive
		const keyOf = (f) => `${f.check}\u0000${f.file}\u0000${f.msg}`;
		const baseKeys = new Map();
		for (const f of base.failures) baseKeys.set(keyOf(f), (baseKeys.get(keyOf(f)) ?? 0) + 1);
		const fresh = (failures, check) => {
			const left = new Map(baseKeys);
			return failures.filter((f) => {
				if (f.check !== check) return false;
				const n = left.get(keyOf(f)) ?? 0;
				if (n > 0) { left.set(keyOf(f), n - 1); return false; }
				return true;
			});
		};
		// each mutation must make the named check FAIL …
		let skipped = 0;
		for (const mut of MUTATIONS) {
			if (!inScope(mut.scope)) { skipped++; continue; }
			try {
				mut.apply(m);
			} catch (e) {
				exit = 1;
				console.error(`selftest: MUTATION NOT APPLIED — ${mut.id} · ${mut.desc}: ${e.message}`);
				m.restore();
				continue;
			}
			const r = runAllChecks(fx, { ...opts, pytestStub: stub });
			m.restore();
			const hit = fresh(r.failures, mut.id);
			if (hit.length > 0) {
				console.log(`selftest: ${mut.id} — FAILS as designed (${hit.length} finding(s)) · ${mut.desc}`);
				console.log(`           e.g. ${hit[0].file}:${hit[0].line} ${hit[0].msg.slice(0, 110)}`);
			} else {
				exit = 1;
				console.error(`selftest: MUTATION NOT CAUGHT — ${mut.id} · ${mut.desc} (oracle stayed green on a known-bad tree)`);
			}
		}
		// … and each benign change must leave it green
		for (const ben of BENIGN) {
			if (!inScope(ben.scope)) { skipped++; continue; }
			ben.apply(m);
			const r = runAllChecks(fx, { ...opts, pytestStub: stub });
			m.restore();
			const hit = fresh(r.failures, ben.id);
			if (hit.length === 0) {
				console.log(`selftest: ${ben.id} — stays GREEN on a benign change · ${ben.desc}`);
			} else {
				exit = 1;
				console.error(`selftest: FALSE POSITIVE — ${ben.id} · ${ben.desc}: ${hit[0].file}:${hit[0].line} ${hit[0].msg.slice(0, 110)}`);
			}
		}
		if (skipped) console.log(`selftest: ${skipped} workspace/local mutation(s) not run (VVIP-only checkout)`);
		exit = selftestPostPush() || exit;
	} finally {
		rmSync(fx, { recursive: true, force: true });
	}
	if (exit === 0) console.log('selftest: all mutation paths proven — oracle fails loudly on known-bad input and stays green on benign input');
	return exit;
}

/* ──────────────────────────────── main ─────────────────────────────────── */

if (ARGS.includes('--selftest')) {
	process.exit(await selftest());
}

const c = runAllChecks(ROOT, { vvipOnly: VVIP_ONLY, postPush: POST_PUSH });
const judgedVvip = (() => { try { return realpathSync(path.join(ROOT, 'VVIP')); } catch { return null; } })();
console.log(`submission-consistency oracle — root ${ROOT}${judgedVvip && judgedVvip !== path.join(ROOT, 'VVIP') ? ` (VVIP → ${judgedVvip})` : ''}${VVIP_ONLY ? ' (VVIP-only checkout)' : ''}`);
for (const d of c.details) console.log(`  ok    ${d.check} — ${d.detail}`);
for (const [check, n] of Object.entries(c.unjudged)) {
	console.log(`  part  ${check} — its workspace half not judged (${n} finding(s) on ../submission or ../PROBLEM_STATEMENT_GUJARAT.md, not in this checkout)`);
}
for (const s of c.skips) console.log(`  SKIP  ${s.check} — ${s.why}`);
for (const w of c.warnings) console.log(`  WARN  ${w}`);
if (c.failures.length) {
	console.error(`\nFAILURES (${c.failures.length}):`);
	for (const f of c.failures) console.error(`  ${f.file}:${f.line} [${f.check}] ${f.msg}`);
	process.exit(1);
}
console.log(`\nSUBMISSION CONSISTENCY: ALL GREEN${VVIP_ONLY ? ' (VVIP-only: the workspace documents were not judged)' : ''}`);
process.exit(0);
