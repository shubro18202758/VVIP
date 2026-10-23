/**
 * REPLAY — an emulator cause tape played back on the canvas clock.
 *
 * LIVE ◉ is the default; REPLAY loads ONE recorded run through the deck's
 * emulator proxy (GET /api/emulator/api/v1/emulator/runs/{id} — the run's
 * persisted tape, contract C-G) and hands its frames to the canvas
 * (replay.ts), which maps them onto the viewer clock:
 *
 *   anpr_read    → one SampledPositionProperty track per plate (the vehicle
 *                  marker moves camera hop to camera hop as the clock moves)
 *   agency_unit  → one track per unit
 *   inputs_doc   → the sparkline under the scrubber (predicted congestion,
 *                  crowd score, weather severity)
 *   event_row / scheduled_event → narrative rows on the fusion rail
 *
 * A tape is a recording of EMULATED causes — every replay surface carries a
 * permanent EMULATED pill, and nothing here writes anything anywhere (a
 * replay never re-delivers frames to the stack; that is the emulator's own
 * POST /replay, which this module never calls). The scrubber position is
 * tape seconds (sim time); play advances it at `speed` tape-seconds per
 * wall-second.
 */

import { EMULATOR_PROXY_PATH } from '$lib/ops/endpoints';
import { getJson, NotDeployed } from '../data';
import { fetchEmulatorRuns, type EmulatorRunRow } from '../city-api';

export const EMULATOR_RUN_API = `${EMULATOR_PROXY_PATH}/api/v1/emulator/runs`;

export interface TapeAnprRead {
	t: number;
	plate: string;
	cameraId: string | null;
	lon: number;
	lat: number;
	speedKmh: number | null;
	heading: number | null;
	hit: string | null;
}

export interface TapeUnitFrame {
	t: number;
	unitId: string;
	agency: string | null;
	status: string | null;
	lon: number;
	lat: number;
}

export interface TapeInputs {
	t: number;
	predictedCongestion: number | null;
	crowdScore: number | null;
	weatherSeverity: number | null;
	eventActive: boolean | null;
	anomalyActive: boolean | null;
}

export interface TapeNarrative {
	t: number;
	frame: number;
	kind: 'event' | 'scheduled' | 'anpr' | 'unit';
	text: string;
	/** Hit reads and incidents are the rows worth a second look. */
	severity: 'info' | 'warn' | 'critical';
}

export interface ReplayTape {
	runId: number;
	useCaseId: string | null;
	/** Epoch ms the tape's t = 0 maps to (the run's t0 anchor). */
	t0: number;
	clockScale: number | null;
	durationSec: number;
	frames: number;
	anpr: TapeAnprRead[];
	units: TapeUnitFrame[];
	inputs: TapeInputs[];
	narrative: TapeNarrative[];
}

function num(v: unknown): number | null {
	return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

function str(v: unknown): string | null {
	return typeof v === 'string' && v.length ? v : null;
}

function rec(v: unknown): Record<string, unknown> {
	return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
}

function pos(p: Record<string, unknown>): [number, number] | null {
	const v = p.position;
	if (Array.isArray(v) && typeof v[0] === 'number' && typeof v[1] === 'number') return [v[0], v[1]];
	if (typeof p.lon === 'number' && typeof p.lat === 'number') return [p.lon, p.lat];
	return null;
}

/** GET /runs/{id} body → tape (pure; exported for tests/city-command-smoke.mjs). */
export function parseTape(resp: unknown): ReplayTape | null {
	const run = rec(resp);
	const tape = rec(run.tape);
	const frames = Array.isArray(tape.frames) ? (tape.frames as unknown[]) : [];
	const runId = num(run.run_id);
	if (runId == null || !frames.length) return null;
	const anchor = Date.parse(str(tape.t0_anchor) ?? str(run.t0_anchor) ?? '');
	const out: ReplayTape = {
		runId,
		useCaseId: str(tape.use_case_id) ?? str(rec(run.spec).use_case_id),
		t0: Number.isFinite(anchor) ? anchor : Date.now(),
		clockScale: num(tape.clock_scale) ?? num(run.clock_scale),
		durationSec: 0,
		frames: frames.length,
		anpr: [],
		units: [],
		inputs: [],
		narrative: [],
	};
	const lastUnitStatus = new Map<string, string | null>();
	for (const raw of frames) {
		const f = rec(raw);
		const t = num(f.t_sec);
		const frame = num(f.frame) ?? 0;
		if (t == null) continue;
		out.durationSec = Math.max(out.durationSec, t);
		const p = rec(f.payload);
		switch (f.kind) {
			case 'anpr_read': {
				const at = pos(p);
				const plate = str(p.plate);
				if (!at || !plate) break;
				const hit = str(p.matched_plate_hit);
				out.anpr.push({
					t,
					plate,
					cameraId: str(p.camera_id),
					lon: at[0],
					lat: at[1],
					speedKmh: num(p.speed_kmh),
					heading: num(p.heading),
					hit,
				});
				if (hit) {
					out.narrative.push({
						t,
						frame,
						kind: 'anpr',
						text: `ANPR ${hit.replace(/_/g, ' ')} · ${plate} at ${str(p.camera_id) ?? 'camera'}${
							num(p.speed_kmh) != null ? ` (${num(p.speed_kmh)} km/h)` : ''
						}`,
						severity: hit === 'stolen_vehicle' ? 'critical' : 'warn',
					});
				}
				break;
			}
			case 'agency_unit': {
				const at = pos(p);
				const unitId = str(p.unit_id);
				if (!at || !unitId) break;
				const status = str(p.status);
				out.units.push({ t, unitId, agency: str(p.agency), status, lon: at[0], lat: at[1] });
				if (lastUnitStatus.get(unitId) !== status) {
					lastUnitStatus.set(unitId, status);
					out.narrative.push({
						t,
						frame,
						kind: 'unit',
						text: `${unitId} (${str(p.agency) ?? 'agency'}) ${(status ?? 'reporting').replace(/_/g, ' ')}`,
						severity: 'info',
					});
				}
				break;
			}
			case 'inputs_doc':
				out.inputs.push({
					t,
					predictedCongestion: num(p.predicted_congestion_idx),
					crowdScore: num(p.crowd_score),
					weatherSeverity: num(p.weather_severity),
					eventActive: typeof p.event_active === 'boolean' ? p.event_active : null,
					anomalyActive: typeof p.anomaly_active === 'boolean' ? p.anomaly_active : null,
				});
				break;
			case 'event_row':
				out.narrative.push({
					t,
					frame,
					kind: 'event',
					text: `event ${str(p.action) ?? 'row'} · ${(str(p.event_kind) ?? 'corridor event').replace(/_/g, ' ')}${
						Array.isArray(p.route_segment_ids) ? ` · ${p.route_segment_ids.length} segments` : ''
					}`,
					severity: 'info',
				});
				break;
			case 'scheduled_event':
				out.narrative.push({
					t,
					frame,
					kind: 'scheduled',
					text: `${(str(p.type) ?? 'incident').replace(/_/g, ' ')} · ${str(p.label) ?? ''}${
						num(p.segment_id) != null ? ` · segment ${p.segment_id}` : ''
					}`,
					severity: (num(p.severity) ?? 0) >= 0.5 ? 'warn' : 'info',
				});
				break;
			default:
				break;
		}
	}
	out.narrative.sort((a, b) => a.t - b.t || a.frame - b.frame);
	return out;
}

/** Ticks of the play loop (ms) — the cursor moves `speed × tick` tape-seconds per tick. */
const PLAY_TICK_MS = 100;

class ReplayStore {
	mode = $state<'live' | 'replay'>('live');
	status = $state<'idle' | 'loading' | 'ready' | 'error'>('idle');
	note = $state<string | null>(null);
	runs = $state.raw<EmulatorRunRow[]>([]);
	runId = $state<number | null>(null);
	tape = $state.raw<ReplayTape | null>(null);
	/** Tape seconds (sim time) the canvas clock shows. */
	cursorSec = $state(0);
	playing = $state(false);
	/** Tape seconds per wall second while playing. */
	speed = $state(60);

	private loop: ReturnType<typeof setInterval> | null = null;
	private token = 0;

	/**
	 * Enter REPLAY: list the newest runs and load `runId` — else the run
	 * already loaded, else the newest recorded (non-replay) run; the run
	 * picker switches tapes.
	 */
	async enter(runId: number | null = null) {
		this.mode = 'replay';
		this.note = null;
		try {
			this.runs = await fetchEmulatorRuns(12, AbortSignal.timeout(8000));
		} catch (err) {
			this.runs = [];
			this.note =
				err instanceof NotDeployed
					? 'emulator runs ledger not deployed (404)'
					: `emulator unreachable (${err instanceof Error ? err.message : String(err)})`;
		}
		const target = runId ?? this.runId ?? this.runs.find((r) => !r.isReplay)?.runId ?? null;
		if (target != null) await this.load(target);
		else if (!this.note) this.note = 'no recorded emulator run to replay';
	}

	exit() {
		this.pause();
		this.token++;
		this.mode = 'live';
		this.status = 'idle';
		this.tape = null;
		this.cursorSec = 0;
		this.note = null;
	}

	async load(runId: number) {
		if (!Number.isInteger(runId) || runId <= 0) return; // the emulator's run ids are positive integers
		const token = ++this.token;
		this.pause();
		this.runId = runId;
		this.status = 'loading';
		this.note = null;
		try {
			const resp = await getJson<unknown>(`${EMULATOR_RUN_API}/${Math.trunc(runId)}`, 15_000);
			if (token !== this.token) return;
			const tape = parseTape(resp);
			if (!tape) throw new Error('the run carries no tape frames');
			this.tape = tape;
			this.cursorSec = 0;
			this.status = 'ready';
		} catch (err) {
			if (token !== this.token) return;
			this.tape = null;
			this.status = 'error';
			this.note =
				err instanceof NotDeployed
					? `run ${runId} not found (404)`
					: `tape load failed (${err instanceof Error ? err.message : String(err)})`;
		}
	}

	setCursor(sec: number) {
		const max = this.tape?.durationSec ?? 0;
		this.cursorSec = Math.max(0, Math.min(max, Number.isFinite(sec) ? sec : 0));
	}

	play() {
		if (!this.tape || this.playing) return;
		if (this.cursorSec >= this.tape.durationSec) this.cursorSec = 0;
		this.playing = true;
		this.loop = setInterval(() => {
			if (!this.tape) return this.pause();
			const next = this.cursorSec + (this.speed * PLAY_TICK_MS) / 1000;
			if (next >= this.tape.durationSec) {
				this.cursorSec = this.tape.durationSec;
				this.pause();
			} else {
				this.cursorSec = next;
			}
		}, PLAY_TICK_MS);
	}

	pause() {
		this.playing = false;
		if (this.loop) clearInterval(this.loop);
		this.loop = null;
	}

	/** Narrative rows up to the cursor, newest first. */
	narrativeUpTo(sec: number): TapeNarrative[] {
		const rows = this.tape?.narrative ?? [];
		const out: TapeNarrative[] = [];
		for (let i = rows.length - 1; i >= 0; i--) if (rows[i].t <= sec) out.push(rows[i]);
		return out;
	}
}

export const replay = new ReplayStore();
