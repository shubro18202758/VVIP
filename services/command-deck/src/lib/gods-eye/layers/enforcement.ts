/**
 * enforcement layer — e-challan RECOMMENDATION pins and stolen-vehicle
 * SIGHTING pins (rec:). The chip reads ENFORCEMENT (the fusion rail's word)
 * and its hint splits the count: penal e-challans an officer can approve,
 * withheld ones, and evidence-only stolen-vehicle sightings — so the chip
 * never claims six 'e-challans' while CITY VITALS counts zero fines.
 *
 * Feed: the city registry's recommendation entities (GET /api/v1/city/
 * entities, contract C-C — pending and approved rows, each located at its
 * evidence sighting). There is no per-layer fallback: without the city
 * picture the chip says so instead of guessing positions.
 *
 * Glyphs: a penal recommendation is a pin with the rupee mark (₹ — a fine a
 * named officer may approve); an evidence-only R_STOLEN_SIGHTING is the same
 * pin with an EYE (a sighting on file — never a fine, never a ₹).
 *
 * A pin reads RULE · km/h over the limit · STATUS:
 *   PENDING   act orange — the deterministic rule PROPOSED it; a named duty
 *             officer must decide (penalties are recommendations, C6);
 *   APPROVED  data accent — decided; the dossier names the officer.
 * Pins are pixel-sized billboards (a map pin drawn once per colour into a
 * cached data URL — identical URLs share one atlas slot) whose tip stands
 * on the read's position — CLAMPED to the ground with the depth test off
 * (layers/draping.ts), so on the photoreal 3D city a pin stands on its
 * street, never buried in a building or floating over it; the coverage
 * detail carries the drape audit (floating 0).
 *
 * One label per camera SITE: several recommendations at one read position
 * (two plates read at EM-CAM-03, a plate with two rules) used to stack
 * identical labels into garble ('STOLENG NO FINE · PENDING'). Their pins
 * fan out sideways (each stays pickable) and the site carries ONE label —
 * a line per DISTINCT reading, the ones an officer must act on first;
 * identical lines (the same sweep filed twice) fold into one with a count
 * ('STOLEN · NO FINE · PENDING ×2', layers/labels.ts siteLabelText); at
 * most SITE_LINES and then '+n more'.
 *
 * Vehicles only: the label carries the rule and the excess speed, never the
 * plate; the pick description masks the plate unless a duty officer is
 * named on console. The rule reads in plain words (SPEEDING, RED LIGHT,
 * CLOSED CORRIDOR — the raw rule id stays in the pick card). A penal rule
 * on a vehicle REPORTED STOLEN (watchlist basis stolen_vehicle_fir, C-N)
 * never reads as a pending fine: its pin is dim and says WITHHELD ·
 * STOLEN; an R_STOLEN_SIGHTING pin says STOLEN · NO FINE.
 *
 * Pick ids `rec:<recommendationId>` → URN `recommendation:<id>`.
 */

import { ops } from '$lib/ops/store.svelte';
import { cityRegistry } from '../entities/registry.svelte';
import type { CityEntity } from '../city-api';
import { displayPlate } from '../plates';
import type { GodsEyeLayer, GodsEyeLayerContext, GodsEyeLayerInstance } from '../registry';
import { cssColor, cssVar, withAlpha } from '../theme';
import { clampedBillboard, clampedDot, clampedLabel, drapeAudit, ground } from './draping';
import { siteLabelText } from './labels';

export const ENFORCEMENT_PICK_PREFIX = 'rec:';

/**
 * Rule text shows within this camera distance (m); the pin itself always
 * shows. A PENDING recommendation (an officer must act) reads from the wall's
 * overview range; decided / withheld ones only up close — the judge view of
 * a locked pursuit stays legible instead of a stack of plates.
 */
const LABEL_NEAR_M = 7000;
const LABEL_DECIDED_M = 4500;

/** Lines one site label carries before '+n more'. */
const SITE_LINES = 3;
/** Read positions this close (degrees, ~1 m) are one camera site. */
const SITE_KEY_DECIMALS = 5;

/** The rule as an operator says it (the raw id stays in the pick card). */
const RULE_WORD: Record<string, string> = {
	R_SPD_SEGMENT: 'SPEEDING',
	R_SIGNAL_VIOLATION: 'RED LIGHT',
	R_CLOSED_CORRIDOR_ENTRY: 'CLOSED CORRIDOR',
	R_STOLEN_SIGHTING: 'STOLEN · NO FINE',
};

/** The vehicle a recommendation entity is about (its prop, else its vehicle link). */
function vehicleOf(r: CityEntity): string | null {
	if (typeof r.props.vehicle_id === 'string') return r.props.vehicle_id;
	const link = r.links.find((l) => l.startsWith('vehicle:'));
	return link ? link.slice('vehicle:'.length) : null;
}

const PIN_W = 22;
const PIN_H = 30;
const pinCache = new Map<string, string>();

/** What the pin's disc carries: a fine (₹) or a sighting on file (an eye — never a fine). */
export type PinMark = 'rupee' | 'eye';

/** The mark a recommendation's pin carries: evidence-only rules never wear the ₹. */
export function pinMarkOf(ruleId: string | null | undefined): PinMark {
	return ruleId === 'R_STOLEN_SIGHTING' ? 'eye' : 'rupee';
}

/** A map pin (disc on a stem; a rupee mark, or an eye for a sighting) in `fill`, rendered once per colour + mark. */
function pinGlyph(fill: string, ink: string, mark: PinMark = 'rupee'): string | null {
	if (typeof document === 'undefined') return null;
	const key = `${fill}|${ink}|${mark}`;
	const hit = pinCache.get(key);
	if (hit) return hit;
	const scale = 2;
	const canvas = document.createElement('canvas');
	canvas.width = PIN_W * scale;
	canvas.height = PIN_H * scale;
	const g = canvas.getContext('2d');
	if (!g) return null;
	g.scale(scale, scale);
	const cx = PIN_W / 2;
	const r = PIN_W / 2 - 2;
	g.fillStyle = fill;
	g.strokeStyle = ink;
	g.lineWidth = 1.5;
	g.beginPath();
	g.moveTo(cx, PIN_H - 1);
	g.lineTo(cx - r * 0.62, r + 6);
	g.arc(cx, r + 1, r, Math.PI * 0.82, Math.PI * 0.18);
	g.closePath();
	g.fill();
	g.stroke();
	g.fillStyle = ink;
	if (mark === 'eye') {
		// an eye (almond + pupil): a sighting on file — evidence, never a fine
		const cy = r + 1;
		g.lineWidth = 1.6;
		g.beginPath();
		g.moveTo(cx - 6.5, cy);
		g.quadraticCurveTo(cx, cy - 6.5, cx + 6.5, cy);
		g.quadraticCurveTo(cx, cy + 6.5, cx - 6.5, cy);
		g.closePath();
		g.stroke();
		g.beginPath();
		g.arc(cx, cy, 2.3, 0, Math.PI * 2);
		g.fill();
	} else {
		g.font = '700 12px sans-serif';
		g.textAlign = 'center';
		g.textBaseline = 'middle';
		g.fillText('₹', cx, r + 1.5);
	}
	const url = canvas.toDataURL('image/png');
	pinCache.set(key, url);
	return url;
}

function num(v: unknown): number | null {
	return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

/** km/h over the posted limit (evidence.excess_kmh, else speed − limit). */
export function excessKmh(rec: CityEntity): number | null {
	const ev = (rec.props.evidence ?? {}) as Record<string, unknown>;
	const excess = num(ev.excess_kmh);
	if (excess != null) return excess;
	const speed = num(ev.speed_kmh);
	const limit = num(ev.speed_limit_kmh);
	return speed != null && limit != null ? speed - limit : null;
}

function createEnforcementLayer(): GodsEyeLayerInstance {
	let ctx: GodsEyeLayerContext | null = null;
	let active = false;
	let timer: ReturnType<typeof setInterval> | null = null;
	const entityIds: string[] = [];
	let lastSignature = '';
	/** Penal e-challans pending a named officer / approved; withheld + superseded (stolen, C-N); evidence-only sightings. */
	let pending = 0;
	let approved = 0;
	let withheldCount = 0;
	let supersededCount = 0;
	let sightings = 0;

	function draw() {
		if (!ctx || !active) return;
		const { cesium, viewer } = ctx;
		const recs = cityRegistry.byKind('recommendation').filter((r) => r.lon != null && r.lat != null);
		const officer = ops.officerOn;
		const stolen = new Set(ops.watchlist.filter((w) => w.lawfulBasis === 'stolen_vehicle_fir').map((w) => w.vehicleId));
		const sig = JSON.stringify([
			recs.map((r) => [r.id, r.state, r.lon, r.lat, r.props.decided_by ?? null]),
			officer,
			[...stolen].sort(),
		]);
		if (sig === lastSignature) return;
		lastSignature = sig;

		for (const id of entityIds) viewer.entities.removeById(id);
		entityIds.length = 0;
		pending = 0;
		approved = 0;
		withheldCount = 0;
		supersededCount = 0;
		sightings = 0;

		const act = cssVar('--v-act') ?? 'orange';
		const data = cssVar('--v-data') ?? 'cyan';
		const ink = cssVar('--v-canvas') ?? 'black';
		const canvasInk = cssColor(cesium, '--v-canvas', cesium.Color.BLACK);

		const dim = cssVar('--v-ink-3') ?? 'gray';
		const plateBg = cesium.Color.fromCssColorString(withAlpha(cssVar('--v-canvas') ?? 'black', 0.72));

		// One label per camera site: group the reads standing on one spot.
		const rows = recs.map((r) => {
			const rule = typeof r.props.rule_id === 'string' ? r.props.rule_id : 'rule';
			const evidenceOnly = rule === 'R_STOLEN_SIGHTING';
			// C-N: a penal rule on a reported-stolen vehicle is never a pending fine.
			const withheld = !evidenceOnly && r.state === 'pending' && stolen.has(vehicleOf(r) ?? '');
			// …and one APPROVED before the stolen-vehicle rule stays approved as
			// audit history (decisions are final) but never reads as a live fine:
			// the theft report supersedes it
			const superseded = !evidenceOnly && r.state === 'approved' && stolen.has(vehicleOf(r) ?? '');
			const isPending = r.state === 'pending' && !withheld;
			const status = withheld
				? 'WITHHELD · STOLEN'
				: superseded
					? 'SUPERSEDED · STOLEN'
					: (r.state ?? 'pending').toUpperCase();
			const word = RULE_WORD[rule] ?? rule.replace(/^R_/, '').replace(/_/g, ' ');
			const excess = evidenceOnly ? null : excessKmh(r);
			const line = `${word}${excess != null ? ` +${Math.round(excess)} km/h` : ''} · ${status}`;
			const site = `${(r.lon as number).toFixed(SITE_KEY_DECIMALS)},${(r.lat as number).toFixed(SITE_KEY_DECIMALS)}`;
			return { r, rule, evidenceOnly, withheld, superseded, isPending, status, word, excess, line, site };
		});
		const sites = new Map<string, typeof rows>();
		for (const row of rows) {
			const list = sites.get(row.site) ?? [];
			list.push(row);
			sites.set(row.site, list);
		}
		// an officer's work first (pending), then withheld, then decided; newest first
		const order = (x: (typeof rows)[number]) => (x.isPending ? 0 : x.withheld || x.superseded ? 1 : 2);
		for (const list of sites.values()) {
			list.sort(
				(a, b) =>
					order(a) - order(b) ||
					String(b.r.props.created_at ?? '').localeCompare(String(a.r.props.created_at ?? '')) ||
					a.r.id.localeCompare(b.r.id),
			);
		}

		for (const row of rows) {
			const { r, rule, evidenceOnly, withheld, superseded, isPending, status, word, excess } = row;
			const site = sites.get(row.site) ?? [row];
			const index = site.indexOf(row);
			// the chip's split: sightings are evidence, never counted as fines
			if (evidenceOnly) sightings++;
			else if (withheld) withheldCount++;
			else if (superseded) supersededCount++;
			else if (isPending) pending++;
			else approved++;
			// the site's lead pin carries the site's ONE label (identical lines folded, ×n)
			const lead = index === 0;
			const siteText = siteLabelText(
				site.map((x) => x.line),
				SITE_LINES,
			);
			const sitePending = site.some((x) => x.isPending);
			const ev = (r.props.evidence ?? {}) as Record<string, unknown>;
			const plate = displayPlate(typeof r.props.plate === 'string' ? r.props.plate : null, officer) ?? 'vehicle';
			const decidedBy = typeof r.props.decided_by === 'string' ? r.props.decided_by : null;
			const image = pinGlyph(withheld || superseded ? dim : isPending ? act : data, ink, pinMarkOf(rule));
			// a site's pins fan out sideways (centred on the read), each pickable
			const fanX = (index - (site.length - 1) / 2) * (PIN_W * 0.6);
			const entity = viewer.entities.add({
				id: `${ENFORCEMENT_PICK_PREFIX}${r.id}`,
				position: ground(cesium, r.lon as number, r.lat as number),
				// No DOM (tests): a clamped DOT billboard — never a clamped `point`,
				// which Cesium pools with other billboards and draws at their size.
				billboard: image
					? clampedBillboard(cesium, {
							image,
							width: PIN_W,
							height: PIN_H,
							verticalOrigin: 1, // BOTTOM: the pin's tip stands on the read
							pixelOffset: new cesium.Cartesian2(fanX, 0),
						})
					: clampedDot(cesium, {
							pixelSize: 10,
							color: cssColor(cesium, isPending ? '--v-act' : '--v-data', cesium.Color.ORANGE),
							outlineColor: canvasInk,
							outlineWidth: 2,
							pixelOffset: new cesium.Cartesian2(fanX, 0),
						}),
				// the site's ONE label rides its lead pin (every line of the site,
				// coloured by its most urgent row); the other pins carry none
				...(lead
					? {
							label: clampedLabel(cesium, {
								text: siteText,
								font: sitePending ? '600 12px monospace' : '12px monospace',
								fillColor: cssColor(
									cesium,
									withheld || superseded ? '--v-ink-2' : isPending ? '--v-act-text' : '--v-data',
									cesium.Color.ORANGE,
								),
								outlineColor: canvasInk,
								outlineWidth: 3,
								style: 2,
								showBackground: true,
								backgroundColor: plateBg,
								backgroundPadding: new cesium.Cartesian2(4, 2),
								verticalOrigin: 1,
								pixelOffset: new cesium.Cartesian2(0, -PIN_H - 2),
								// Overview declutter: the pin always shows, its rule text up close
								// (a hovered / selected pin shows it at any range).
								distanceDisplayCondition: new cesium.DistanceDisplayCondition(
									0,
									sitePending ? LABEL_NEAR_M : LABEL_DECIDED_M,
								),
							}),
						}
					: {}),
				description: `${evidenceOnly ? 'stolen-vehicle sighting (evidence, never a fine)' : 'e-challan recommendation'} · ${word} (${rule}) · ${status}${decidedBy ? (superseded ? ` (approved by ${decidedBy})` : ` by ${decidedBy}`) : ''}${
					num(ev.speed_kmh) != null && num(ev.speed_limit_kmh) != null
						? ` · ${num(ev.speed_kmh)} km/h in a ${num(ev.speed_limit_kmh)} km/h zone${excess != null ? ` (+${Math.round(excess)})` : ''}`
						: ''
				}${typeof ev.road_name === 'string' ? ` on ${ev.road_name}` : ''} · camera ${r.props.camera_id ?? '—'} · plate ${plate} · ${
					withheld
						? 'e-challan WITHHELD — the vehicle is reported stolen (FIR); its owner is the theft victim'
						: superseded
							? 'approved before the stolen-vehicle rule, SUPERSEDED — the vehicle is reported stolen (FIR); its owner is the theft victim; kept as audit history (decisions are final) and never a pursuit basis; no fine is approved on a reported-stolen vehicle (409)'
							: isPending
							? 'HUMAN DECISION REQUIRED (no automated penalty)'
							: 'decided by a named officer'
				} · source ${r.source === 'emulated' ? 'EMULATED' : 'LIVE'}`,
			});
			entityIds.push(entity.id);
		}
	}

	return {
		async status() {
			return { configured: true };
		},
		init(layerCtx) {
			ctx = layerCtx;
		},
		activate() {
			active = true;
			draw();
			timer = setInterval(draw, 2000);
		},
		deactivate() {
			active = false;
			if (timer) clearInterval(timer);
			timer = null;
			if (ctx) for (const id of entityIds) ctx.viewer.entities.removeById(id);
			entityIds.length = 0;
			lastSignature = '';
		},
		refresh() {
			lastSignature = '';
			void cityRegistry.refresh();
			draw();
		},
		handlePick(pickId) {
			return pickId.startsWith(ENFORCEMENT_PICK_PREFIX);
		},
		urnOf(pickId) {
			const id = pickId.slice(ENFORCEMENT_PICK_PREFIX.length);
			return id ? `recommendation:${id}` : null;
		},
		coverageStats() {
			const served = cityRegistry.served('recommendation');
			const down = cityRegistry.status !== 'idle' && !served;
			return {
				count: entityIds.length,
				loading: cityRegistry.status === 'idle',
				hint: down
					? 'city fusion API unavailable — no enforcement pins'
					: `e-challans (₹): ${pending} pending a named officer · ${approved} approved${
							withheldCount ? ` · ${withheldCount} withheld (vehicle reported stolen)` : ''
						}${
							supersededCount ? ` · ${supersededCount} superseded (approved before the vehicle's theft report)` : ''
						} · stolen-vehicle sightings (eye): ${sightings} — evidence, never a fine · pins stand on the evidence read`,
				error: down ? (cityRegistry.note ?? 'recommendation source not ok') : null,
				// + how the pins meet the ground (the 3D smoke asserts floating 0)
				detail:
					ctx && entityIds.length
						? { pending, approved, withheld: withheldCount, superseded: supersededCount, sightings, ...drapeAudit(ctx.viewer, entityIds) }
						: { pending, approved, withheld: withheldCount, superseded: supersededCount, sightings },
				via: 'city-api',
			};
		},
		destroy() {
			this.deactivate();
		},
	};
}

export const enforcementLayer: GodsEyeLayer = {
	id: 'enforcement',
	name: 'Enforcement — e-challan recommendations (₹) + stolen-vehicle sightings (evidence); a named officer decides',
	label: 'ENFORCEMENT',
	requiresKeyId: null,
	pickPrefix: ENFORCEMENT_PICK_PREFIX,
	defaultOn: true,
	create: createEnforcementLayer,
};
