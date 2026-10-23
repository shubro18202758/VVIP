<script lang="ts">
	/**
	 * GodsEyeCanvas — the Cesium God's-eye canvas.
	 *
	 * Keyless by default (re-graded dark OSM raster + ellipsoid terrain + our
	 * own entities — zero calls to cesium.com/ion); env-keyed ion upgrade via
	 * the server-side mode endpoint, built from the async ion factories and
	 * falling back to keyless when the token or ion fails. Sensor modes:
	 * COMMAND (the raw render — the default, because a luminance-mapped scene
	 * erases every colour the layers carry) and the opt-in FLIR looks
	 * (White-Hot / Black-Hot / Ironbow). A share-link camera URL, and the
	 * layer registry (gods-eye-view MIT pattern — see registry.ts).
	 *
	 * HUD modes: 'full' (VEER CITY COMMAND /gods-eye — EXIT chip + Esc back to
	 * the wall; the page hosts the entity dossier, the vitals and the fusion
	 * rail around this canvas) · 'mini' (compact chip row) · 'wall' (the
	 * cockpit '/' and the /ops situation wall):
	 *   - AUTO-ORBIT (wall only): 30 s after the last interaction the camera
	 *     orbits a ground pivot — the locked subject, else the screen-centre
	 *     ground point inside the city, else the city centre — inside an
	 *     east-north-up frame at that pivot (rotateRight about the LOCAL up
	 *     axis). Any interaction, flight, tour or follow hands the camera back
	 *     to the world frame, and an idle camera that leaves the city bbox
	 *     flies home. (The first orbit rotated about Earth's polar axis and
	 *     carried Ahmedabad off-screen in ~40 s.)
	 *   - SUBJECT FOLLOW (/ops): selecting a live event flies to its spine
	 *     once and locks; the follow tracks the selection id only, so board
	 *     polls never re-fly, and RELEASE sticks (across views too — a
	 *     released subject is not re-locked until the selection moves).
	 *   - FOCUS BUS (focus.svelte.ts): board surfaces request a fly + lock;
	 *     LOCKED is shown only when the flight lands (a drag or wheel during
	 *     the flight cancels it), and a canvas serves only requests made
	 *     after it mounted — the next view never replays the last request.
	 *   - SELECTION (entities/selection.svelte.ts): every pick maps through its
	 *     layer to a C-C URN and selects that entity (event URNs also select
	 *     the event on the board); the selected entity wears a halo and its
	 *     primitives stay highlighted. A fly request (fusion-rail row, dossier
	 *     chip, tour) flies to the entity's fused position — the registry's,
	 *     else its dossier's — and reports landed / no-position.
	 *   - PICKING: on the wall views the layer's description opens a pick
	 *     card; on City Command the dossier replaces it (the pick text rides
	 *     into the dossier's MAP line). Hover highlights the entity.
	 *   - PURSUIT OVERLAY (head-first trace + comet + choke cascade + marching
	 *     future arc — pursuit.ts), redrawn only when the plan changes, with
	 *     the plan's stale / likely-passed / direction-of-travel facts.
	 *   - GUIDED TOUR: camera choreography over targets computed from the
	 *     LIVE city picture (posture, the pursuit, the busiest camera, the
	 *     units, the planned convoy, the signal network, the e-challan pins),
	 *     skipping what is empty; on City Command each stop opens its dossier.
	 *   - REPLAY (hud/replay.svelte.ts + replay.ts): an emulator tape on the
	 *     viewer clock — EVERY live layer stands down while it plays (a 'LIVE
	 *     paused' chip says so), so the replayed frames are what is on screen;
	 *     every replayed primitive is badged EMULATED.
	 *   - AGENTIC GOD'S-EYE (canvas-bus.svelte.ts, contract C-D2): the agent's
	 *     canvas directives queue as 'AI suggests: <verb> <label> [Apply]
	 *     [Dismiss]' chips. Apply runs the move LOCALLY (fly / select / halo /
	 *     a TTL note / sensor / layer) and acknowledges it to the brain once;
	 *     nothing is ever auto-applied. ⌘K / Ctrl+K opens the command palette
	 *     (hud full + wall). An 'Open on map' from the agent console is
	 *     served when City Command mounts.
	 *   - PICKING PRIORITY: a click drill-picks up to 8 primitives and takes
	 *     the operator's likely target by layer (unit > camera > vehicle mark
	 *     > signal > pursuit > event > segment); the halo and non-entity
	 *     picks (3D tile features of the photoreal layer, C-L) never win.
	 *     Draped lines share the road's pixels, so a pursuit LINE lying on
	 *     an event's spine ranks just after the event (its markers keep
	 *     their rank).
	 *   - 3D CITY (layers/draping.ts): on the ion key the overlays stand ON
	 *     Google's photoreal mesh — the selection halo is a draped ring, an
	 *     agent note a clamped label; City Command's home is a judge-grade
	 *     oblique over the old city and the Sabarmati (HOME_VIEW_3D, the
	 *     live action framed at ACTION_VIEW_3D); the chip names what is on
	 *     screen (basemap.ts imageryChip — PHOTOREAL 3D · GOOGLE) and the
	 *     data credits ride a glass plate in the region the host's panels
	 *     leave clear (City Command's fusion rail would cover them).
	 *
	 * Ethics rail (C6): every plate this canvas prints is masked unless a
	 * named duty officer is on console (plates.ts) — at the source (layer
	 * descriptions, tour labels, focus notes) AND again at every text sink
	 * (pick card, LOCKED chip, flash line, tour label, test snapshot), so a
	 * layer that forgets to mask a free-text prop still never shows a plate.
	 *
	 * Degradation contract: if WebGL or the prebuilt bundle is unavailable
	 * this panel degrades ALONE. If OSM tiles cannot load, the canvas switches
	 * to OFFLINE SCHEMATIC MODE (entities over a dark globe, labelled).
	 */
	import { onMount, onDestroy, untrack } from 'svelte';
	import { replaceState } from '$app/navigation';
	import { page } from '$app/state';
	import { ops } from '$lib/ops/store.svelte';
	import { CITY_BBOX } from '$lib/ops/endpoints';
	import { t } from '$lib/i18n/strings';
	import type { InterceptionPlan } from '$lib/ops/types';
	import { godsEye } from './store.svelte';
	import { focus } from './focus.svelte';
	import { GODS_EYE_LAYERS, GODS_EYE_LAYER_IDS } from './layers';
	import { eventCentroid, eventIdFromPick } from './layers/corridor-events';
	import { unitPhase } from './layers/agency';
	import {
		ION_READY_TIMEOUT_MS,
		OSM_CREDIT,
		createIonBasemap,
		createKeylessBasemap,
		imageryChip,
		imageryModeOf,
		osmScreenCredit,
		photorealReady,
		type Basemap,
	} from './layers/basemap';
	import { DRAPE_Z, clampedLabel, drapedPolyline, ground, groundPath, ringCoords } from './layers/draping';
	import { createPursuitOverlay, type PursuitOverlay } from './pursuit';
	import { createReplayOverlay, type ReplayOverlay } from './replay';
	import { fetchVehicleTrace } from './data';
	import { FLIR_SHADER } from './shaders/flir';
	import { buildShareUrl, parseShareHash, type ShareViewState } from './share-link';
	import { cssColor, cssVar } from './theme';
	import { displayPlate, displayText } from './plates';
	import { parseUrn } from './city-api';
	import { cityRegistry } from './entities/registry.svelte';
	import { selection } from './entities/selection.svelte';
	import { dossier } from './entities/dossier.svelte';
	import { replay } from './hud/replay.svelte';
	import { cityVitals } from './hud/vitals.svelte';
	import CommandPalette from './hud/CommandPalette.svelte';
	import { canvasBus } from './canvas-bus.svelte';
	import type { CanvasDirective, DirectiveWorld } from './directives';
	import '$lib/i18n/packs.agent';
	import type { GodsEyeLayerInstance, LayerCoverageStats } from './registry';

	type SensorMode = GodsEyeSensorMode;

	let {
		hud = 'full',
		onexit,
		pickCard = hud !== 'full',
		clearInsets = null,
	}: {
		hud?: 'full' | 'mini' | 'wall';
		/** 'full' only: the EXIT chip and Esc (no tour running) call this. */
		onexit?: () => void;
		/**
		 * Show the canvas's own pick card. City Command ('full') hosts the
		 * entity dossier instead, so a pick there only selects.
		 */
		pickCard?: boolean;
		/**
		 * CSS px of the canvas the host's glass panels cover (City Command:
		 * vitals left, dossier right, fusion rail bottom). Every camera flight
		 * then lands its subject in the middle of the CLEAR region instead of
		 * the canvas centre, so a selected entity never lands under a panel.
		 */
		clearInsets?: { left: number; right: number; top: number; bottom: number } | null;
	} = $props();

	let container: HTMLDivElement | null = $state(null);
	/** The HUD chip row — measured for the clear region (it wraps when a host panel narrows it). */
	let hudRowEl: HTMLDivElement | null = $state(null);

	let phase = $state<'loading' | 'ready' | 'unavailable'>('loading');
	let unavailableReason = $state<string | null>(null);
	let offlineDetected = $state(false);
	/** COMMAND (raw) by default on every route; FLIR looks are opt-in. */
	let sensorMode = $state<SensorMode>('raw');
	let activeLayers = $state<Set<string>>(new Set(GODS_EYE_LAYERS.map((l) => l.id)));
	let stats = $state<Record<string, LayerCoverageStats>>({});
	let picked = $state<{
		id: string;
		layer: string;
		label: string;
		text: string;
		eventId: string | null;
		/** The C-C entity the pick selected (null for preview fixtures / replay). */
		urn: string | null;
	} | null>(null);
	let shareFlash = $state<string | null>(null);
	let tourIndex = $state<number | null>(null);
	let tourTotal = $state(0);
	let tourLabel = $state<string>('');
	let legendOpen = $state(false);
	/** Reactive mirror of the pursuit overlay's entity count (HUD chip). */
	let pursuitCount = $state(0);
	/** Basemap actually rendering (the ion upgrade can fall back to keyless). */
	let basemapId = $state<'osm-keyless' | 'ion-photoreal'>('osm-keyless');
	let basemapNote = $state<string | null>(null);
	/** 1 Hz wall clock — the AMA chip age ticks between band polls. */
	let nowMs = $state(Date.now());

	// controller state (non-reactive)
	let viewer: CesiumViewer | null = null;
	/** This canvas's own test hook (window.__GODS_EYE__ points at the newest canvas's). */
	let snapHook: NonNullable<Window['__GODS_EYE__']> | null = null;
	/**
	 * Set by onDestroy. The mount is async (bundle, mode endpoint, layer
	 * probes): a view left while the canvas is loading must not go on to
	 * build a viewer — a WebGL context with a live render loop — on a
	 * detached node.
	 */
	let destroyed = false;
	let instances = new Map<string, GodsEyeLayerInstance>();
	let pursuit: PursuitOverlay | null = null;
	let stage: { uniforms?: Record<string, number> } | null = null;
	let hashTimer: ReturnType<typeof setTimeout> | null = null;
	let hashLast = 0;
	let hudTimer: ReturnType<typeof setInterval> | null = null;
	let clockTimer: ReturnType<typeof setInterval> | null = null;
	let timeTimer: ReturnType<typeof setInterval> | null = null;
	let declutterTimer: ReturnType<typeof setInterval> | null = null;
	let tourTimer: ReturnType<typeof setTimeout> | null = null;
	let ionTimer: ReturnType<typeof setTimeout> | null = null;
	let hoverTimer: ReturnType<typeof setTimeout> | null = null;
	let handler: { destroy(): void } | null = null;
	let removeCameraListener: (() => void) | null = null;
	let removeTileError: (() => void) | null = null;
	let removeTileProgress: (() => void) | null = null;
	let removePreUpdate: (() => void) | null = null;
	let removeIonListeners: Array<() => void> = [];
	let tileErrors = 0;
	/** Set in onMount: a SPA navigation must not start orbiting at once. */
	let lastInteraction = 0;
	/** True while the orbit's east-north-up frame holds the camera. */
	let orbitFrameActive = false;
	let orbitLastTick = 0;
	/** performance.now() until which a camera flight owns the camera. */
	let flightUntil = 0;
	/** Where the current press started (null once it became a drag). */
	let pointerOrigin: { x: number; y: number } | null = null;
	/** A button is held on the canvas — hover picking pauses. */
	let pressing = false;
	let hoverAt: { x: number; y: number } | null = null;
	let hovered: { entity: CesiumEntity; restore: () => void } | null = null;
	/** Selection-follow memory: never re-fly the same (or a released) subject. */
	let lastFollowedId: string | null = null;
	/** Ground anchor of the locked subject (the wall orbit pivots on it). */
	let lockAnchor: { lon: number; lat: number } | null = null;
	/**
	 * The subject flight in progress: LOCKED is set only when it lands. The
	 * token drops callbacks of a flight that a newer one superseded.
	 */
	let lockFlight: { token: number; eventId: string; seq: number | null } | null = null;
	let lockFlightToken = 0;
	/**
	 * Focus requests this canvas has seen. Starts at the bus's current seq:
	 * the bus outlives SPA navigation, and a canvas must not replay a request
	 * made on another view (a released subject re-locked on /gods-eye, a
	 * stale 'no route geometry' flash on /ops). Requests made while this
	 * canvas is still loading are newer, so they are still served.
	 */
	const mountedFocusSeq = focus.seq;
	let lastFocusSeq = mountedFocusSeq;
	let detachFocus: (() => void) | null = null;
	let pendingFocus: { seq: number; eventId: string; deadline: number } | null = null;
	/** Pursuit request token: a stale async trace fetch never draws. */
	let pursuitToken = 0;
	let pursuitShownSig = '';
	/** Signature whose trace fetch is in flight (an identical re-poll waits for it). */
	let pursuitPendingSig = '';
	let preTourSensor: SensorMode = 'raw';
	/** REPLAY overlay (tape tracks on the viewer clock). */
	let replayOverlay: ReplayOverlay | null = null;
	let replayShownSig = '';
	/** Layers stood down while a replay plays (restored on LIVE; the chips show them paused). */
	let replaySuspended = $state<string[]>([]);
	/** Selection halo + the highlight currently applied to the selection's primitives. */
	const HALO_ID = 'sel:halo';
	/** Radius (m) of the halo's draped ring around its subject. */
	const HALO_RADIUS_M = 95;
	/** A moving subject re-lays its ring once it has moved this far (m). */
	const HALO_RELAY_M = 20;
	let haloAt: { lon: number; lat: number } | null = null;
	/** The halo entity, and where its ring was last laid on the ground. */
	let haloEntity: (CesiumEntity & { show: boolean }) | null = null;
	let haloDrawnAt: { lon: number; lat: number } | null = null;
	let selectedHighlights: Array<{ entity: CesiumEntity; restore: () => void }> = [];
	let selectedHighlightSig = '';
	/** Fly requests this canvas has seen (starts at the bus's seq: no replay across views). */
	const mountedFlySeq = selection.flySeq;
	let lastFlySeq = mountedFlySeq;
	/** Agent notes (canvas_annotate) on the canvas: id → removal timer. */
	const AGENT_NOTE_PREFIX = 'agent:note:';
	const AGENT_NOTE_TTL_MS = 60_000;
	/** canvas_highlight emphasis lasts this long. */
	const AGENT_HIGHLIGHT_TTL_MS = 20_000;
	const agentNotes = new Map<string, ReturnType<typeof setTimeout>>();
	let agentHighlights: Array<() => void> = [];
	let agentHighlightTimer: ReturnType<typeof setTimeout> | null = null;
	/** AI-suggests chips shown at once (the rest wait behind '+N more'). */
	const AGENT_CHIPS_SHOWN = 3;

	const CAMERA_HOME = {
		lon: (CITY_BBOX.minLon + CITY_BBOX.maxLon) / 2,
		lat: (CITY_BBOX.minLat + CITY_BBOX.maxLat) / 2,
	};

	/** Home framing: the city centre mid-screen, the old city in view. */
	const HOME_VIEW = { range: 7000, headingDeg: 0, pitchDeg: -45 } as const;

	/**
	 * City Command home: the live action framed (actionFrame). If the fused
	 * picture answers only after the canvas is ready, the camera settles onto
	 * the action once — within `settleMs` of the home and only if nobody has
	 * touched the camera since.
	 */
	const ACTION_VIEW = {
		minRange: 3500,
		maxRange: 16_000,
		/** Extra room around the outermost entity (fraction of the fit, then metres for markers/labels). */
		margin: 1.2,
		padM: 500,
		headingDeg: 0,
		pitchDeg: -52,
		flightSec: 1.6,
		settleMs: 12_000,
	} as const;
	/** performance.now() of the home that is still waiting for the fused picture (0 = none). */
	let actionHomeAt = 0;

	/**
	 * The judge-grade home on the photoreal 3D city (City Command, ion key,
	 * nothing in action): an oblique look NNE over the old city and the
	 * Sabarmati — camera ≈2.06 km up, SSW of Teen Darwaza — where the real
	 * buildings AND the draped overlays read at 1920×1080. The 2D homes
	 * (7 km at −45°, the action frame at −52°) look straight down onto a
	 * mesh that wants an oblique.
	 */
	const HOME_VIEW_3D = { lon: 72.583, lat: 23.0245, range: 3200, headingDeg: 20, pitchDeg: -40 } as const;
	/**
	 * The live action on the photoreal city: the same frame, oblique, its
	 * camera kept 1.5–3 km up (range 3.5–4.4 km at −42°) — a wider action
	 * keeps its centre and lets the edges go rather than climbing to where
	 * the buildings flatten into a map.
	 */
	const ACTION_VIEW_3D = { pitchDeg: -42, maxRange: 4400 } as const;

	/** Orbit resumes 30 s after the last interaction (the wall's idle beat). */
	const ORBIT_RESUME_MS = 30_000;
	/** ~5 minutes per revolution — slow enough to read labels while it turns. */
	const ORBIT_DEG_PER_SEC = 1.2;
	/** An idle wall camera past the city bbox by this much (or this high) flies home. */
	const CITY_MARGIN_DEG = 0.04;
	const CITY_MAX_ALT_M = 40_000;

	/**
	 * Subject framing for follow/focus. The range is the distance at which
	 * the spine's bounding circle (+15 %) fits the canvas's NARROWER field
	 * of view (1.5–30 km), so a long corridor is framed whole on any aspect
	 * ratio; the pitch steepens from the -60° oblique as the range grows so
	 * the camera stays over the subject — the horizontal offset
	 * range·cos(pitch) never exceeds 1.8 km (≈0.016° of latitude). A short
	 * flight lands the lock well inside 3 s even on software WebGL.
	 */
	const SUBJECT_VIEW = {
		margin: 1.15,
		minRange: 1500,
		maxRange: 30_000,
		minPitchDeg: 60,
		maxPitchDeg: 89,
		maxOffsetM: 1800,
		flightSec: 1.2,
	} as const;

	/** Wheel-out ceiling (m): the region stays in view, never orbit-space. */
	const MAX_ZOOM_M = 120_000;

	/** Focus requests wait this long for geometry (segments / board) to land. */
	const FOCUS_WAIT_MS = 15_000;

	/** An AMA band whose newest READING is older than this renders STALE. */
	const AMA_STALE_SEC = 600;

	/** Share-hash writes at most once per second (cinematic route only). */
	const HASH_THROTTLE_MS = 1000;

	const SENSOR_ORDER: SensorMode[] = ['raw', 'flir-whot', 'flir-bhot', 'flir-iron'];
	const SENSOR_LABEL: Record<SensorMode, string> = {
		raw: 'COMMAND',
		'flir-whot': 'FLIR·WHOT',
		'flir-bhot': 'FLIR·BHOT',
		'flir-iron': 'FLIR·IRON',
	};

	/**
	 * The guided tour — camera choreography over the LIVE city picture.
	 * Targets are computed when a step plays, from the fused registry and the
	 * board; a step whose layer is off or empty (or whose subject does not
	 * exist right now) is skipped, so the tour never narrates a layer that
	 * shows nothing. On City Command each stop also selects its subject, so
	 * the dossier narrates real rows while the camera moves.
	 */
	interface TourTarget {
		label: string;
		lon: number;
		lat: number;
		range: number;
		/** The entity the stop is about (selected on City Command). */
		urn?: string | null;
	}

	interface TourStep {
		id: string;
		/** Registry layer the step shows (skipped when off or count 0). */
		layer?: string;
		headingDeg: number;
		pitchDeg: number;
		/** Optional sensor beat; other steps play in the pre-tour sensor. */
		sensor?: SensorMode;
		target: () => TourTarget | null;
	}

	const TOUR: TourStep[] = [
		{
			id: 'city',
			headingDeg: HOME_VIEW.headingDeg,
			pitchDeg: HOME_VIEW.pitchDeg,
			target: () => ({
				label: `Ahmedabad at ${ops.band ?? 'NO BAND'} — one fused picture (${cityRegistry.size} entities · band source ${ops.bandSource})`,
				lon: CAMERA_HOME.lon,
				lat: CAMERA_HOME.lat,
				range: HOME_VIEW.range,
			}),
		},
		{
			id: 'pursuit',
			headingDeg: 60,
			pitchDeg: -48,
			target: () => {
				const pursuitEv = cityRegistry
					.byKind('event')
					.find((e) => e.props.event_kind === 'vehicle_pursuit' && e.state !== 'completed' && e.state !== 'cancelled');
				const meta = (pursuitEv?.props.metadata ?? {}) as Record<string, unknown>;
				const vehicle = typeof meta.vehicle_id === 'string' ? cityRegistry.get(`vehicle:${meta.vehicle_id}`) : null;
				const plan = ops.interception;
				const s = plan?.latestSighting;
				const anchor =
					vehicle && vehicle.lon != null && vehicle.lat != null
						? ([vehicle.lon, vehicle.lat] as [number, number])
						: s
							? s.position
							: null;
				if (!anchor) return null;
				const pts: Array<[number, number]> = [anchor, ...(plan?.chokes ?? []).slice(0, 4).map((c) => c.position)];
				const f = frameOf(pts, 1100);
				const plate = displayPlate(
					typeof vehicle?.props.plate === 'string' ? vehicle.props.plate : (s?.plate ?? null),
					ops.officerOn,
				);
				return f && {
					...f,
					label: `pursuit — ${plate ?? 'vehicle'} (${vehicle?.source === 'emulated' || s?.source === 'emulated_anpr' ? 'EMULATED ANPR' : 'ANPR'}) · ${
						plan?.chokes.length ?? 0
					} ranked chokes · human-initiated`,
					urn: vehicle?.urn ?? pursuitEv?.urn ?? null,
				};
			},
		},
		{
			id: 'camera',
			layer: 'cameras',
			headingDeg: 200,
			pitchDeg: -50,
			target: () => {
				const cams = godsEye.cameras;
				if (!cams.length) return null;
				const busiest = [...cams].sort(
					(a, b) => b.hits15m * 1000 + b.reads15m - (a.hits15m * 1000 + a.reads15m) || (b.lastReadT ?? 0) - (a.lastReadT ?? 0),
				)[0];
				const name = busiest.label ?? busiest.cameraId;
				// A quiet window says so: the stop then shows the newest read, not a 'busiest' zero.
				const ago = busiest.lastReadT != null ? Math.max(0, Math.round((Date.now() - busiest.lastReadT) / 60_000)) : null;
				return {
					label:
						busiest.reads15m > 0
							? `busiest camera — ${name}: ${busiest.reads15m} reads, ${busiest.hits15m} watchlist hits in 15 min (locations + counts only)`
							: `latest ANPR read — ${name}${ago != null ? `, ${ago} min ago` : ''} · no reads in the last 15 min (locations + counts only)`,
					lon: busiest.lon,
					lat: busiest.lat,
					range: 1600,
					urn: `camera:${busiest.cameraId}`,
				};
			},
		},
		{
			id: 'units',
			layer: 'units',
			headingDeg: 150,
			pitchDeg: -42,
			target: () => {
				const units = godsEye.units;
				const pts = units.map((u) => [u.lon, u.lat] as [number, number]);
				const f = frameOf(pts, 1200);
				const moving = units.find((u) => unitPhase(u) === 'en_route') ?? units[0];
				return f && {
					...f,
					label: `units on the ground — ${pts.length} (EMULATED) · ${units.filter((u) => unitPhase(u) === 'en_route').length} en route · ${
						units.filter((u) => unitPhase(u) === 'on_scene').length
					} on scene`,
					urn: moving ? `unit:${moving.unitId}` : null,
				};
			},
		},
		{
			id: 'convoy',
			headingDeg: 0,
			pitchDeg: -55,
			target: () => {
				const convoy = cityRegistry.byKind('convoy')[0];
				if (convoy && convoy.lon != null && convoy.lat != null) {
					return {
						label: `convoy on GPS — ${convoy.label} (${convoy.source === 'emulated' ? 'EMULATED GPS' : 'GPS'})`,
						lon: convoy.lon,
						lat: convoy.lat,
						range: 2400,
						urn: convoy.urn,
					};
				}
				const segs = godsEye.segmentById;
				const planned = ops.events.find(
					(e) => e.source !== 'offline-fixture' && e.eventKind === 'protected_movement' && eventCentroid(e, segs),
				);
				const c = planned ? eventCentroid(planned, segs) : null;
				if (!planned || !c) return null;
				const { range } = subjectFraming(c.radiusM);
				return {
					label: `planned protected movement — ${displayText(planned.name, ops.officerOn)} · RECOMMENDED corridor + dashed alternates`,
					lon: c.lon,
					lat: c.lat,
					range: Math.min(9000, range),
					urn: `event:${planned.eventId}`,
				};
			},
		},
		{
			id: 'signals',
			layer: 'signals',
			headingDeg: 90,
			pitchDeg: -38,
			sensor: 'flir-iron',
			target: () => {
				const pts = godsEye.signals.map((s) => [s.lon, s.lat] as [number, number]);
				const f = frameOf(pts);
				return f && { ...f, label: `signal network — ${pts.length} junction programs at the phase clock (FLIR·IRON beat)` };
			},
		},
		{
			id: 'enforcement',
			layer: 'enforcement',
			headingDeg: 300,
			pitchDeg: -50,
			target: () => {
				const recs = cityRegistry.byKind('recommendation').filter((r) => r.lon != null && r.lat != null);
				const pending = recs.filter((r) => r.state === 'pending');
				const pick = pending[0] ?? recs[0];
				if (!pick) return null;
				// sightings are evidence on file, never fines: the tour says which is which
				const sightings = recs.filter((r) => r.props.rule_id === 'R_STOLEN_SIGHTING').length;
				return {
					label: `enforcement — ${recs.length - sightings} e-challan recommendation(s) · ${sightings} stolen-vehicle sighting(s), evidence only · ${pending.length} pending a named officer (machine proposes, human disposes)`,
					lon: pick.lon as number,
					lat: pick.lat as number,
					range: 1800,
					urn: pick.urn,
				};
			},
		},
	];

	/** Bounding frame (centre + range) for a set of ground points. */
	function frameOf(
		pts: Array<[number, number]>,
		minRange = 1200,
	): { lon: number; lat: number; range: number } | null {
		if (!pts.length) return null;
		let minLon = Infinity;
		let maxLon = -Infinity;
		let minLat = Infinity;
		let maxLat = -Infinity;
		for (const [x, y] of pts) {
			minLon = Math.min(minLon, x);
			maxLon = Math.max(maxLon, x);
			minLat = Math.min(minLat, y);
			maxLat = Math.max(maxLat, y);
		}
		const lon = (minLon + maxLon) / 2;
		const lat = (minLat + maxLat) / 2;
		const halfW = ((maxLon - minLon) / 2) * 111_320 * Math.cos((lat * Math.PI) / 180);
		const halfH = ((maxLat - minLat) / 2) * 111_320;
		const radius = Math.hypot(halfW, halfH);
		return { lon, lat, range: Math.min(9000, Math.max(minRange, radius * 2.4)) };
	}

	function loadScript(src: string): Promise<void> {
		return new Promise((resolve, reject) => {
			const el = document.createElement('script');
			el.src = src;
			el.async = true;
			el.onload = () => resolve();
			el.onerror = () => reject(new Error(`failed to load ${src}`));
			document.head.appendChild(el);
		});
	}

	function webglAvailable(): boolean {
		try {
			const c = document.createElement('canvas');
			const gl = (c.getContext('webgl2') ?? c.getContext('webgl')) as WebGLRenderingContext | null;
			// Release the probe context at once — one fewer live context per mount.
			gl?.getExtension('WEBGL_lose_context')?.loseContext();
			return !!gl;
		} catch {
			return false;
		}
	}

	function fetchJson(path: string, signal?: AbortSignal): Promise<unknown> {
		return fetch(path, signal ? { signal } : undefined).then((res) => {
			if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
			return res.json();
		});
	}

	function makeReadyHook() {
		let resolveReady: (v: { ok: boolean; reason?: string }) => void;
		const ready = new Promise<{ ok: boolean; reason?: string }>((r) => {
			resolveReady = r;
		});
		const hook: NonNullable<Window['__GODS_EYE__']> = {
			phase: 'loading',
			imageryMode: 'keyless-osm',
			imageryNote: null,
			tilesLoaded: false,
			entityCount: 0,
			distinctLayerIds: [],
			stats: {},
			offlineDetected: false,
			sensorMode: 'raw',
			camera: null,
			orbiting: false,
			selectedEventId: null,
			subjectLockId: null,
			picked: null,
			amaAgeSec: null,
			pursuitEntities: 0,
			layerStats: {},
			selection: { urn: null, hover: null, fly: null, flyOutcome: null, flyMs: null, halo: null, highlighted: 0 },
			registry: { status: 'idle', entities: 0, kinds: {}, updatedAt: 0, convoys: [] },
			replay: null,
			vitals: null,
			locate,
			ready,
		};
		window.__GODS_EYE__ = snapHook = hook;
		return (ok: boolean, reason?: string) => {
			hook.phase = ok ? 'ready' : 'unavailable';
			resolveReady({ ok, reason });
		};
	}

	/**
	 * Publish the test snapshot. Reads run untracked: this is called from
	 * effects, and a snapshot must never become an effect dependency.
	 */
	function publishSnapshot() {
		untrack(() => {
			const snap = snapHook;
			if (!snap || !viewer) return;
			// What is on screen: the photoreal mesh once its tiles cover the view
			// (even over a stumbled imagery layer), else the imagery ladder.
			snap.imageryMode = imageryModeOf({ offline: offlineDetected, basemapId, photoreal: stats['photoreal-3d'] ?? null });
			snap.imageryNote = basemapNote;
			snap.tilesLoaded = viewer.scene.globe.tilesLoaded;
			snap.entityCount = viewer.entities.values.length;
			snap.distinctLayerIds = [...instances.keys()].filter((id) => activeLayers.has(id));
			snap.stats = Object.fromEntries(Object.entries(stats).map(([id, s]) => [id, s.count]));
			snap.offlineDetected = offlineDetected;
			snap.sensorMode = sensorMode;
			snap.camera = cameraState();
			snap.orbiting = orbitFrameActive;
			snap.selectedEventId = ops.selectedEventId;
			snap.subjectLockId = godsEye.subjectLockId;
			snap.picked = picked
				? { id: picked.id, layer: picked.layer, text: displayText(picked.text, ops.officerOn), eventId: picked.eventId }
				: null;
			snap.amaAgeSec = amaAgeSec;
			snap.pursuitEntities = pursuit?.count() ?? 0;
			pursuitCount = snap.pursuitEntities;
			snap.layerStats = Object.fromEntries(
				Object.entries(stats).map(([id, st]) => [id, { count: st.count, ...(st.detail ?? {}), ...(st.via ? { via: st.via } : {}) }]),
			);
			// The pursuit overlay is not a registry layer: its drape audit rides beside them (3D smoke).
			if (pursuit && snap.pursuitEntities > 0) snap.layerStats.pursuit = { count: snap.pursuitEntities, ...pursuit.audit() };
			snap.selection = {
				urn: selection.current,
				hover: selection.hover,
				fly: selection.flyUrn,
				flyOutcome: selection.flyOutcome,
				flyMs: selection.flyLandedMs,
				halo: haloAt ? { lon: haloAt.lon, lat: haloAt.lat } : null,
				highlighted: selectedHighlights.length,
			};
			const kinds: Record<string, number> = {};
			for (const k of ['event', 'vehicle', 'sighting', 'camera', 'junction', 'unit', 'convoy', 'dispatch', 'recommendation', 'anomaly'] as const) {
				kinds[k] = cityRegistry.byKind(k).length;
			}
			snap.registry = {
				status: cityRegistry.status,
				entities: cityRegistry.size,
				kinds,
				updatedAt: cityRegistry.updatedAt,
				convoys: cityRegistry
					.byKind('convoy')
					.filter((c) => c.lon != null && c.lat != null)
					.map((c) => ({ id: c.id, lon: c.lon as number, lat: c.lat as number, t: c.t, source: c.source })),
			};
			snap.vitals = cityVitals.shown;
			// AI suggestions (canvas bus) — a local cast: the snapshot typing lives in cesium.d.ts.
			(snap as unknown as Record<string, unknown>).agent = canvasBus.snapshot();
			snap.replay =
				replay.mode === 'replay'
					? {
							runId: replay.runId,
							status: replay.status,
							cursorSec: replay.cursorSec,
							durationSec: replay.tape?.durationSec ?? 0,
							tracks: replayOverlay?.count() ?? 0,
							markers: replayOverlay?.markers() ?? {},
						}
					: null;
		});
	}

	function refreshStats() {
		const next: Record<string, LayerCoverageStats> = {};
		for (const [id, inst] of instances) next[id] = inst.coverageStats();
		stats = next;
		publishSnapshot();
	}

	function radToDeg(rad: number): number {
		const C = window.Cesium;
		if (C?.Math?.toDegrees) return C.Math.toDegrees(rad);
		return (rad * 180) / Math.PI;
	}

	/**
	 * Camera ground position only (degrees / metres) — the per-frame city
	 * guard reads this; the heading/pitch getters each swap the camera
	 * transform, so the full pose stays on the 1 Hz snapshot.
	 */
	function cameraGround(): { lon: number; lat: number; alt: number } | null {
		if (!viewer) return null;
		try {
			const c = viewer.camera.positionCartographic;
			return { lon: radToDeg(c.longitude), lat: radToDeg(c.latitude), alt: c.height };
		} catch {
			return null; // camera mid-teardown
		}
	}

	/** World-frame camera pose (degrees / metres), null before the viewer. */
	function cameraState(): { lon: number; lat: number; alt: number; heading: number; pitch: number } | null {
		if (!viewer) return null;
		try {
			const cam = viewer.camera;
			const c = cam.positionCartographic;
			return {
				lon: radToDeg(c.longitude),
				lat: radToDeg(c.latitude),
				alt: c.height,
				heading: radToDeg(cam.heading),
				pitch: radToDeg(cam.pitch),
			};
		} catch {
			return null; // camera mid-teardown
		}
	}

	function currentViewState(): ShareViewState {
		const cam = viewer!.camera;
		const c = cam.positionCartographic;
		return {
			lon: radToDeg(c.longitude),
			lat: radToDeg(c.latitude),
			alt: c.height,
			heading: radToDeg(cam.heading),
			pitch: radToDeg(cam.pitch),
			roll: radToDeg(cam.roll),
			style: sensorMode === 'raw' ? 'raw' : 'flir',
			bhot: sensorMode === 'flir-bhot',
			palette: sensorMode === 'flir-iron' ? 'iron' : 'mono',
			layers: [...activeLayers],
			// City Command hands the selected entity over with the view.
			selected: hud === 'full' ? selection.current : null,
		};
	}

	/** The share URL for the current view (camera, sensor + palette, layers, selection). */
	function shareUrl(): string {
		return buildShareUrl(currentViewState(), GODS_EYE_LAYER_IDS);
	}

	/** Keep the location hash on the current view — cinematic route only, throttled. */
	function updateHash() {
		if (!viewer || hud !== 'full' || hashTimer) return;
		const wait = Math.max(0, HASH_THROTTLE_MS - (performance.now() - hashLast));
		hashTimer = setTimeout(() => {
			hashTimer = null;
			hashLast = performance.now();
			if (!viewer) return;
			try {
				replaceState(`#${shareUrl().split('#')[1] ?? ''}`, page.state);
			} catch {
				// router not ready / sandboxed iframe — SHARE VIEW still works
			}
		}, wait);
	}

	function readProp<T>(p: CesiumProperty<T> | T | undefined, time: unknown): T | undefined {
		if (p == null) return undefined;
		if (typeof p === 'object' && typeof (p as CesiumProperty<T>).getValue === 'function') {
			return (p as CesiumProperty<T>).getValue(time);
		}
		return p as T;
	}

	/** The human description every layer attaches to its entities. */
	function descriptionOf(entity: CesiumEntity): string | null {
		if (!viewer) return null;
		const d = readProp(entity.description, viewer.clock.currentTime);
		return typeof d === 'string' && d.trim() ? d : null;
	}

	/**
	 * Test hook: client coordinates of the entities whose id starts with
	 * `prefix`, on-screen only — on the canvas AND not under anything the
	 * host floats over it (City Command's vitals / dossier / rail, the HUD
	 * chips), so a reported spot is one an operator can actually click. A
	 * polyline reports the screen midpoint of its middle span (on the line,
	 * clear of the vertices where markers and neighbouring segments meet); a
	 * point reports its position.
	 */
	function locate(prefix: string): Array<{ id: string; x: number; y: number }> {
		const C = window.Cesium;
		const out: Array<{ id: string; x: number; y: number }> = [];
		if (!viewer || !C) return out;
		const v = viewer;
		const rect = v.scene.canvas.getBoundingClientRect();
		const time = v.clock.currentTime;
		const toWindow = (world: unknown) => {
			const win = world ? C.SceneTransforms.worldToWindowCoordinates(v.scene, world) : undefined;
			return win && Number.isFinite(win.x) && Number.isFinite(win.y) ? win : null;
		};
		for (const e of v.entities.values) {
			if (!e.id.startsWith(prefix)) continue;
			const positions = readProp(e.polyline?.positions, time);
			let win: { x: number; y: number } | null = null;
			if (Array.isArray(positions) && positions.length >= 2) {
				const i = Math.max(0, Math.floor(positions.length / 2) - 1);
				const a = toWindow(positions[i]);
				const b = toWindow(positions[i + 1]);
				win = a && b ? { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 } : null;
			} else {
				win = toWindow(readProp(e.position, time));
			}
			if (!win || win.x < 0 || win.y < 0 || win.x > rect.width || win.y > rect.height) continue;
			const x = rect.left + win.x;
			const y = rect.top + win.y;
			const top = document.elementFromPoint(x, y);
			if (top && container && !container.contains(top)) continue; // covered by a panel or chip
			out.push({ id: e.id, x, y });
		}
		return out;
	}

	function enterOfflineSchematic(v: CesiumViewer) {
		if (offlineDetected) return;
		offlineDetected = true;
		try {
			v.scene.globe.show = false;
			v.scene.imageryLayers.removeAll?.();
		} catch {
			// older bundle edge — globe hidden is the meaningful part
		}
	}

	/**
	 * Offline detection: sustained tile errors on the base layer → schematic.
	 * Not while the photoreal tiles cover the view: the city on screen is
	 * Google's 3D mesh, not a flat dark globe, and the globe beneath it is
	 * the overlays' ground (layers/draping.ts) — hiding it would be false.
	 */
	function watchTileErrors(v: CesiumViewer) {
		removeTileError?.();
		removeTileError = null;
		tileErrors = 0;
		const baseProvider = (v.scene.imageryLayers.get(0) as unknown as {
			imageryProvider?: { errorEvent?: CesiumEvent<[unknown]> };
		})?.imageryProvider;
		if (baseProvider?.errorEvent) {
			removeTileError = baseProvider.errorEvent.addEventListener(() => {
				tileErrors++;
				if (tileErrors >= 5 && !offlineDetected && !photorealReady(stats['photoreal-3d'])) enterOfflineSchematic(v);
			});
		}
	}

	/**
	 * The ion upgrade reports failure asynchronously (bad token → 401,
	 * ion unreachable → no answer). Either way the canvas swaps in the
	 * keyless basemap and labels why — a bad token never takes the canvas
	 * down to 'unavailable'.
	 */
	function watchIonBasemap(cesium: CesiumGlobal, v: CesiumViewer, basemap: Basemap) {
		if (basemap.id !== 'ion-photoreal') return;
		let imageryReady = false;
		const why = (err: unknown) => (err instanceof Error ? err.message : String(err)).slice(0, 120);
		const imageryFallback = (reason: string) => {
			if (basemapId !== 'ion-photoreal' || viewer !== v) return;
			if (ionTimer) clearTimeout(ionTimer);
			ionTimer = null;
			try {
				if (basemap.imageryLayer) v.scene.imageryLayers.remove(basemap.imageryLayer, true);
				const keyless = createKeylessBasemap(cesium);
				v.scene.imageryLayers.add(keyless.baseLayer, 0);
				v.scene.globe.terrainProvider = keyless.terrainProvider;
				watchTileErrors(v);
			} catch {
				// a half-built ion layer is still removable on teardown
			}
			basemapId = 'osm-keyless';
			basemapNote = `ion upgrade unavailable (${reason}) — keyless OSM fallback`;
			publishSnapshot();
		};
		if (basemap.imageryLayer) {
			removeIonListeners.push(
				basemap.imageryLayer.readyEvent.addEventListener(() => {
					imageryReady = true;
					if (ionTimer) clearTimeout(ionTimer);
					ionTimer = null;
					// The ion provider exists only now: arm the offline-schematic
					// tile-error watch on it (the mount-time watch found none).
					if (viewer === v && basemapId === 'ion-photoreal') watchTileErrors(v);
				}),
				basemap.imageryLayer.errorEvent.addEventListener((err) => imageryFallback(why(err))),
			);
		}
		if (basemap.terrain) {
			removeIonListeners.push(
				basemap.terrain.errorEvent.addEventListener((err) => {
					if (viewer !== v) return;
					try {
						v.scene.globe.terrainProvider = new cesium.EllipsoidTerrainProvider();
					} catch {
						// keep whatever terrain the globe has
					}
					if (basemapId === 'ion-photoreal') basemapNote = `ion world terrain unavailable (${why(err)}) — ellipsoid terrain`;
					publishSnapshot();
				}),
			);
		}
		ionTimer = setTimeout(() => {
			if (!imageryReady) imageryFallback(`no answer in ${Math.round(ION_READY_TIMEOUT_MS / 1000)} s`);
		}, ION_READY_TIMEOUT_MS);
	}

	/** The star field is held off until the view's first tiles have landed. */
	let starsHeld = false;

	/**
	 * The canvas is a DARK MAP from its first frame, never 'space': the clear
	 * colour and the globe's base colour are the canvas token, and the star
	 * field stays off until the view's first tiles land (a slow tile fetch —
	 * OSM probes have taken 4.9 s — used to leave a fresh City Command on a
	 * black, starry sky with only the markers; the draped lines need the
	 * globe). The stars come back with the first loaded view (only a horizon
	 * view ever shows sky). And the OSM attribution rides the on-screen
	 * credit plate on both canvases — the keyless raster is OSM, and the keyed
	 * city still draws OSM-derived road geometry (basemap.ts osmScreenCredit).
	 */
	function prepareGround(cesium: CesiumGlobal, v: CesiumViewer) {
		const ink = cssColor(cesium, '--v-canvas', cesium.Color.BLACK);
		try {
			v.scene.backgroundColor = ink;
			v.scene.globe.baseColor = ink;
		} catch {
			// a stripped build keeps its defaults
		}
		if (v.scene.skyBox) {
			v.scene.skyBox.show = false;
			starsHeld = true;
		}
		const credit = osmScreenCredit(cesium);
		if (credit) {
			try {
				v.creditDisplay?.addStaticCredit(credit);
			} catch {
				// the lightbox still carries the raster's credit
			}
		}
	}

	/** The first loaded view: the star field may show again (horizon views only). */
	function releaseStars() {
		if (!starsHeld) return;
		starsHeld = false;
		try {
			if (viewer?.scene.skyBox) viewer.scene.skyBox.show = true;
		} catch {
			// viewer mid-teardown
		}
	}

	function applySensorStage(cesium: CesiumGlobal, v: CesiumViewer) {
		if (stage && v.scene.postProcessStages.contains(stage)) {
			v.scene.postProcessStages.remove(stage);
		}
		stage = null;
		if (sensorMode === 'raw') return;
		stage = new cesium.PostProcessStage({
			name: 'veer-flir',
			fragmentShader: FLIR_SHADER.fragmentShader,
			uniforms: {
				intensity: FLIR_SHADER.uniforms.intensity.default,
				sensitivity: FLIR_SHADER.uniforms.sensitivity.default,
				bloom: FLIR_SHADER.uniforms.bloom.default,
				mode: sensorMode === 'flir-bhot' ? 1 : 0,
				pixelation: FLIR_SHADER.uniforms.pixelation.default,
				palette: sensorMode === 'flir-iron' ? 1 : 0,
				// Wall embeds keep the full frame; the optics mask is cinematic-only.
				lens: hud === 'full' ? FLIR_SHADER.uniforms.lens.default : 0,
				time: 0,
			},
		});
		v.scene.postProcessStages.add(stage);
	}

	function setSensor(mode: SensorMode) {
		sensorMode = mode;
		if (viewer && window.Cesium) applySensorStage(window.Cesium, viewer);
		publishSnapshot();
	}

	function cycleSensor() {
		const next = SENSOR_ORDER[(SENSOR_ORDER.indexOf(sensorMode) + 1) % SENSOR_ORDER.length];
		// The operator's choice outlives the tour (stop restores THIS sensor).
		if (tourIndex != null) preTourSensor = next;
		setSensor(next);
		updateHash();
	}

	function setLayerEnabled(id: string, on: boolean) {
		const inst = instances.get(id);
		if (!inst) return;
		if (on === activeLayers.has(id)) return;
		// A layer the replay stood down only changes its toggle now; LIVE
		// re-activates it (activate() is not idempotent — never twice).
		if (!replaySuspended.includes(id)) {
			if (on) inst.activate();
			else inst.deactivate();
		}
		activeLayers = new Set(on ? [...activeLayers, id] : [...activeLayers].filter((x) => x !== id));
		refreshStats();
		updateHash();
	}

	function toggleLayer(id: string) {
		setLayerEnabled(id, !activeLayers.has(id));
	}

	async function share() {
		if (!viewer) return;
		const url = shareUrl();
		try {
			await navigator.clipboard.writeText(url);
			shareFlash = 'view URL copied';
		} catch {
			shareFlash = url.slice(0, 160) + '…';
		}
		setTimeout(() => (shareFlash = null), 4000);
	}

	// ─── camera: frames, flights, home ─────────────────────────────────────────

	/** Hand the camera back to the world frame (orbit frame released). */
	function resetFrame() {
		const C = window.Cesium;
		if (!orbitFrameActive || !viewer || !C) return;
		try {
			viewer.camera.lookAtTransform(C.Matrix4.IDENTITY);
		} catch {
			// camera mid-teardown
		}
		orbitFrameActive = false;
	}

	/**
	 * Fly to frame a ground point from a heading/pitch/range offset. The
	 * orbit frame is released first (flyTo keeps a non-identity transform),
	 * and the orbit stays off while the flight owns the camera. `complete`
	 * runs when the flight lands; `cancel` when a newer flight (or the
	 * operator's gesture) supersedes it.
	 */
	function flyFrame(
		lon: number,
		lat: number,
		range: number,
		headingDeg: number,
		pitchDeg: number,
		duration: number,
		on: { complete?: () => void; cancel?: () => void } = {},
	) {
		const C = window.Cesium;
		if (!viewer || !C) return;
		resetFrame();
		flightUntil = performance.now() + duration * 1000 + 250;
		const at = clearRegionTarget(lon, lat, range, headingDeg, pitchDeg);
		viewer.camera.flyToBoundingSphere(new C.BoundingSphere(C.Cartesian3.fromDegrees(at.lon, at.lat, 0), 0), {
			offset: new C.HeadingPitchRange(C.Math.toRadians(headingDeg), C.Math.toRadians(pitchDeg), range),
			duration,
			...on,
		});
	}

	/**
	 * The look-at point that puts ground point `lon,lat` in the middle of the
	 * canvas region the host's panels leave clear (clearInsets), for a flight
	 * at `range`/`headingDeg`/`pitchDeg`. Without insets (the wall views) it
	 * is the point itself. Small-angle model around the screen centre: one
	 * CSS px covers 2·range·tan(vfov/2)/H metres across the view and that
	 * over sin(|pitch|) along the ground; the shift is capped at 35 % of the
	 * range so a panel layout can never push the subject off the canvas.
	 * The top edge is the lower of the host's inset and the chip row as it
	 * lays out right now: with the dossier open the row wraps to two lines,
	 * and a fixed one-row inset would land the subject under the second.
	 */
	function clearRegionTarget(
		lon: number,
		lat: number,
		range: number,
		headingDeg: number,
		pitchDeg: number,
	): { lon: number; lat: number } {
		const ins = clearInsets;
		const canvas = viewer?.scene.canvas;
		const w = canvas?.clientWidth ?? 0;
		const h = canvas?.clientHeight ?? 0;
		if (!ins || w < 200 || h < 200) return { lon, lat };
		const rowBottom =
			hudRowEl && canvas ? hudRowEl.getBoundingClientRect().bottom - canvas.getBoundingClientRect().top + 8 : 0;
		const top = Math.max(ins.top, rowBottom);
		const clearW = w - ins.left - ins.right;
		const clearH = h - top - ins.bottom;
		if (clearW < 160 || clearH < 120) return { lon, lat };
		const dxPx = ins.left + clearW / 2 - w / 2; // + = the clear centre is right of the canvas centre
		const dyPx = top + clearH / 2 - h / 2; // + = below
		const fov = viewer?.camera.frustum.fov;
		const aspect = w / h;
		const tanHalfV = Math.tan((fov && Number.isFinite(fov) ? fov : Math.PI / 3) / 2) / Math.max(1, aspect);
		const mPerPx = (2 * range * tanHalfV) / h;
		const along = Math.max(0.35, Math.sin(Math.abs((pitchDeg * Math.PI) / 180)));
		let rightM = dxPx * mPerPx;
		let upM = (-dyPx * mPerPx) / along;
		const cap = range * 0.35;
		const norm = Math.hypot(rightM, upM);
		if (norm > cap) {
			rightM *= cap / norm;
			upM *= cap / norm;
		}
		// The look-at moves opposite to where the subject must appear on screen.
		const hd = (headingDeg * Math.PI) / 180;
		const eastM = -rightM * Math.cos(hd) - upM * Math.sin(hd);
		const northM = rightM * Math.sin(hd) - upM * Math.cos(hd);
		return {
			lon: lon + eastM / (111_320 * Math.cos((lat * Math.PI) / 180)),
			lat: lat + northM / 110_540,
		};
	}

	/**
	 * Half-angle (rad) of the narrower field-of-view axis. Cesium's `fov` is
	 * the horizontal angle on a wide canvas and the vertical one on a tall
	 * canvas; the other axis is narrower by the aspect ratio.
	 */
	function narrowHalfFov(): number {
		const f = viewer?.camera.frustum;
		const canvas = viewer?.scene.canvas;
		const fov = f?.fov && Number.isFinite(f.fov) ? f.fov : Math.PI / 3;
		const aspect =
			f?.aspectRatio && Number.isFinite(f.aspectRatio)
				? f.aspectRatio
				: canvas && canvas.clientHeight > 0
					? canvas.clientWidth / canvas.clientHeight
					: 16 / 9;
		return Math.atan(Math.tan(fov / 2) * Math.min(aspect, 1 / aspect));
	}

	/** Range + pitch that frame a spine of `radiusM` whole, with the camera over its centroid. */
	function subjectFraming(radiusM: number): { range: number; pitchDeg: number } {
		const fit = (radiusM * SUBJECT_VIEW.margin) / Math.sin(narrowHalfFov());
		const range = Math.min(SUBJECT_VIEW.maxRange, Math.max(SUBJECT_VIEW.minRange, fit));
		const steepDeg = (Math.acos(Math.min(1, SUBJECT_VIEW.maxOffsetM / range)) * 180) / Math.PI;
		return { range, pitchDeg: -Math.min(SUBJECT_VIEW.maxPitchDeg, Math.max(SUBJECT_VIEW.minPitchDeg, steepDeg)) };
	}

	/**
	 * Home. On City Command the home is the LIVE ACTION — the fused
	 * picture's open events, earmarked vehicles, pending e-challans,
	 * dispatches, convoys and units in motion, framed together in the clear
	 * region — and the city centre only while the picture has none (or has
	 * not answered yet). On the photoreal 3D city both are oblique: the
	 * action at ACTION_VIEW_3D, else the old city over the Sabarmati
	 * (HOME_VIEW_3D). The wall views keep the fixed city-centre home.
	 */
	function flyHome(duration: number) {
		const action = hud === 'full' ? actionFrame() : null;
		if (action) {
			flyFrame(action.lon, action.lat, action.range, ACTION_VIEW.headingDeg, actionPitchDeg(), duration);
			return;
		}
		if (hud === 'full' && photoreal3d()) {
			const h = HOME_VIEW_3D;
			flyFrame(h.lon, h.lat, h.range, h.headingDeg, h.pitchDeg, duration);
			return;
		}
		flyFrame(CAMERA_HOME.lon, CAMERA_HOME.lat, HOME_VIEW.range, HOME_VIEW.headingDeg, HOME_VIEW.pitchDeg, duration);
	}

	/**
	 * The photoreal 3D city is (or is about to be) under the camera: the ion
	 * key with its 3D layer on. Read from the key, not the tiles — the home
	 * flight leaves before the first tile lands.
	 */
	function photoreal3d(): boolean {
		return godsEye.imageryMode === 'ion-photoreal' && !!godsEye.imageryToken && activeLayers.has('photoreal-3d');
	}

	/** The action frame's pitch: oblique over the 3D city, steeper over the flat map. */
	function actionPitchDeg(): number {
		return photoreal3d() ? ACTION_VIEW_3D.pitchDeg : ACTION_VIEW.pitchDeg;
	}

	/** The frame of what is happening in the city now (null: nothing open, or no answer yet). */
	function actionFrame(): { lon: number; lat: number; range: number } | null {
		if (cityRegistry.status !== 'ok') return null;
		const pts: Array<[number, number]> = [];
		const add = (e: { lon: number | null; lat: number | null }) => {
			if (e.lon != null && e.lat != null && inCity(e.lon, e.lat, 0)) pts.push([e.lon, e.lat]);
		};
		for (const e of cityRegistry.byKind('event')) if (e.state !== 'completed' && e.state !== 'cancelled') add(e);
		for (const e of cityRegistry.byKind('vehicle')) add(e);
		for (const e of cityRegistry.byKind('recommendation')) if (e.state === 'pending') add(e);
		for (const e of cityRegistry.byKind('dispatch')) add(e);
		for (const e of cityRegistry.byKind('convoy')) add(e);
		for (const e of cityRegistry.byKind('unit')) if (e.state === 'en_route' || e.state === 'on_scene') add(e);
		if (!pts.length) return null;
		let minLon = Infinity;
		let maxLon = -Infinity;
		let minLat = Infinity;
		let maxLat = -Infinity;
		for (const [x, y] of pts) {
			minLon = Math.min(minLon, x);
			maxLon = Math.max(maxLon, x);
			minLat = Math.min(minLat, y);
			maxLat = Math.max(maxLat, y);
		}
		const lat = (minLat + maxLat) / 2;
		const lon = (minLon + maxLon) / 2;
		// Fit the extent into the region the panels leave clear (the whole
		// canvas on a wall): one CSS px covers 2·range·tan(vfov/2)/H metres
		// across the view, that over sin(|pitch|) along the ground.
		const canvas = viewer?.scene.canvas;
		const w = canvas?.clientWidth || 1920;
		const h = canvas?.clientHeight || 1080;
		const ins = clearInsets;
		const clearW = Math.max(160, w - (ins?.left ?? 0) - (ins?.right ?? 0));
		const clearH = Math.max(120, h - (ins?.top ?? 0) - (ins?.bottom ?? 0));
		const fov = viewer?.camera.frustum.fov;
		const tanHalfV = Math.tan((fov && Number.isFinite(fov) ? fov : Math.PI / 3) / 2) / Math.max(1, w / h);
		const halfE = ((maxLon - minLon) / 2) * 111_320 * Math.cos((lat * Math.PI) / 180);
		const halfN = ((maxLat - minLat) / 2) * 110_540;
		const needE = (halfE * h) / (tanHalfV * clearW);
		const needN = (halfN * Math.sin(Math.abs((actionPitchDeg() * Math.PI) / 180)) * h) / (tanHalfV * clearH);
		const range = Math.min(
			photoreal3d() ? ACTION_VIEW_3D.maxRange : ACTION_VIEW.maxRange,
			Math.max(ACTION_VIEW.minRange, Math.max(needE, needN) * ACTION_VIEW.margin + ACTION_VIEW.padM),
		);
		return { lon, lat, range };
	}

	function inCity(lon: number, lat: number, marginDeg: number): boolean {
		return (
			lon >= CITY_BBOX.minLon - marginDeg &&
			lon <= CITY_BBOX.maxLon + marginDeg &&
			lat >= CITY_BBOX.minLat - marginDeg &&
			lat <= CITY_BBOX.maxLat + marginDeg
		);
	}

	function restoreFromHash(cesium: CesiumGlobal, v: CesiumViewer) {
		const parsed = parseShareHash(window.location.hash, GODS_EYE_LAYER_IDS);
		if (!parsed) {
			flyHome(0);
			// City Command before the fused picture answered: settle onto the action later.
			if (hud === 'full' && !actionFrame()) actionHomeAt = performance.now();
			return;
		}
		sensorMode =
			parsed.style === 'raw' ? 'raw' : parsed.bhot ? 'flir-bhot' : parsed.palette === 'iron' ? 'flir-iron' : 'flir-whot';
		applySensorStage(cesium, v);
		activeLayers = new Set(parsed.layers);
		// A handed-over selection opens the same dossier (the camera comes from the hash).
		if (hud === 'full' && parsed.selected && parseUrn(parsed.selected)) {
			selection.select(parsed.selected, { fly: false, source: 'share' });
		}
		v.camera.flyTo({
			destination: cesium.Cartesian3.fromDegrees(parsed.lon, parsed.lat, parsed.alt),
			orientation: {
				heading: cesium.Math.toRadians(parsed.heading),
				pitch: cesium.Math.toRadians(parsed.pitch),
				roll: cesium.Math.toRadians(parsed.roll),
			},
			duration: 0, // restore instantly — a handoff link must not cinematic-fly
		});
	}

	// ─── wall mode: auto-orbit, subject follow, focus, guided tour ────────────

	/** Pointer/wheel on the canvas: the operator has the camera now. */
	function markInteraction() {
		lastInteraction = performance.now();
		resetFrame();
		if (tourIndex != null) stopTour();
	}

	/**
	 * A camera gesture (drag or wheel) takes the camera: the held or incoming
	 * lock is released, so the LOCKED chip never claims a hold the operator
	 * just broke, and a flight in progress yields — Cesium does not cancel a
	 * flight on input, its tween would keep overriding the gesture.
	 */
	function takeCamera() {
		if (godsEye.subjectLockId || lockFlight) releaseSubject({ why: 'the operator took the camera' });
		if (viewer && performance.now() < flightUntil) {
			try {
				viewer.camera.cancelFlight();
			} catch {
				// camera mid-teardown
			}
			flightUntil = 0;
		}
	}

	function onPointerDown(e: PointerEvent) {
		markInteraction();
		pressing = true;
		pointerOrigin = { x: e.clientX, y: e.clientY };
	}

	function onPointerMove(e: PointerEvent) {
		if (e.buttons === 0) return;
		lastInteraction = performance.now();
		// A drag (not a click) is a camera gesture.
		if (pointerOrigin && Math.hypot(e.clientX - pointerOrigin.x, e.clientY - pointerOrigin.y) > 6) {
			pointerOrigin = null;
			takeCamera();
		}
	}

	function onPointerUp() {
		pressing = false;
		pointerOrigin = null;
		lastInteraction = performance.now();
	}

	function onWheel() {
		markInteraction();
		takeCamera();
	}

	/** The ground point the orbit turns around. */
	function orbitPivot(C: CesiumGlobal): unknown {
		if (godsEye.subjectLockId && lockAnchor) {
			return C.Cartesian3.fromDegrees(lockAnchor.lon, lockAnchor.lat, 0);
		}
		const canvas = viewer!.scene.canvas;
		const hit = viewer!.camera.pickEllipsoid(new C.Cartesian2(canvas.clientWidth / 2, canvas.clientHeight / 2));
		const carto = hit ? C.Cartographic.fromCartesian(hit) : undefined;
		if (carto) {
			const lon = radToDeg(carto.longitude);
			const lat = radToDeg(carto.latitude);
			if (inCity(lon, lat, 0)) return C.Cartesian3.fromDegrees(lon, lat, 0);
		}
		return C.Cartesian3.fromDegrees(CAMERA_HOME.lon, CAMERA_HOME.lat, 0);
	}

	/**
	 * Per-frame orbit + city guard (scene.preUpdate). Wall mode only; time
	 * based, so the turn rate is the same on SwiftShader and on a GPU.
	 */
	function orbitTick() {
		const now = performance.now();
		const dt = orbitLastTick ? Math.min(0.25, (now - orbitLastTick) / 1000) : 0;
		orbitLastTick = now;
		const C = window.Cesium;
		if (!viewer || !C || hud !== 'wall') return;
		const idle = now - lastInteraction >= ORBIT_RESUME_MS;
		if (!idle || tourIndex != null) {
			resetFrame();
			return;
		}
		if (now < flightUntil || document.hidden) return;
		try {
			const cam = cameraGround();
			if (cam && (!inCity(cam.lon, cam.lat, CITY_MARGIN_DEG) || cam.alt > CITY_MAX_ALT_M)) {
				// An idle wall camera off the city (or out in space) flies home.
				if (godsEye.subjectLockId) releaseSubject({ remember: false });
				flyHome(2.5);
				return;
			}
			if (!orbitFrameActive) {
				viewer.camera.lookAtTransform(C.Transforms.eastNorthUpToFixedFrame(orbitPivot(C)));
				orbitFrameActive = true;
			}
			viewer.camera.rotateRight(C.Math.toRadians(ORBIT_DEG_PER_SEC * dt));
		} catch {
			// camera mid-teardown — the next frame retries
		}
	}

	/**
	 * Fly to an event's spine; the camera is held on it (the LOCKED chip)
	 * only when the flight LANDS. `seq` is the focus request being served
	 * (null for the /ops selection follow) — it settles 'locked' on landing,
	 * 'cancelled' when the flight is superseded or the operator takes over.
	 */
	function lockOn(eventId: string, c: { lon: number; lat: number; radiusM: number }, seq: number | null = null) {
		if (tourIndex != null) stopTour();
		// A different subject held now is let go at once (a hand-over, not an
		// operator release); the chip returns when this flight lands.
		if (godsEye.subjectLockId && godsEye.subjectLockId !== eventId) {
			godsEye.subjectLockId = null;
			lockAnchor = null;
		}
		const token = ++lockFlightToken;
		lockFlight = { token, eventId, seq };
		const { range, pitchDeg } = subjectFraming(c.radiusM);
		lastInteraction = performance.now();
		flyFrame(c.lon, c.lat, range, 0, pitchDeg, SUBJECT_VIEW.flightSec, {
			complete: () => {
				if (token !== lockFlightToken || !viewer) return;
				lockFlight = null;
				godsEye.subjectLockId = eventId;
				lockAnchor = { lon: c.lon, lat: c.lat };
				lastInteraction = performance.now();
				if (seq != null) focus.settle(seq, 'locked', eventLabel(eventId));
				publishSnapshot();
			},
			cancel: () => {
				if (token !== lockFlightToken) return;
				// superseded by a flight that is not a lock (tour step, fly-home)
				lockFlight = null;
				if (seq != null) focus.settle(seq, 'cancelled', 'another camera flight took over');
				publishSnapshot();
			},
		});
		publishSnapshot();
	}

	/** Selection follow (wall /ops): live events only, once per selection. */
	function followSubject(eventId: string) {
		if (!viewer || !window.Cesium) return;
		const ev = ops.events.find((e) => e.eventId === eventId);
		if (!ev || ev.source === 'offline-fixture') return;
		const c = eventCentroid(ev, godsEye.segmentById);
		if (!c) return;
		lastFollowedId = eventId;
		lockOn(eventId, c);
	}

	/**
	 * Let go of the subject — the held lock, or a lock flight still in the
	 * air (it will not lock when it lands). An operator release (RELEASE,
	 * drag, wheel, tour) is remembered on the focus bus, so no view's
	 * selection follow re-locks the subject until the selection moves to
	 * another event or a surface asks for it again; the idle city guard's
	 * release is not an operator decision and is not remembered.
	 */
	function releaseSubject({ why = 'released before the flight landed', remember = true } = {}) {
		const subject = godsEye.subjectLockId ?? lockFlight?.eventId ?? null;
		if (lockFlight) {
			if (lockFlight.seq != null) focus.settle(lockFlight.seq, 'cancelled', why);
			lockFlight = null;
			lockFlightToken++; // its complete/cancel callbacks are stale now
		}
		if (subject && remember) focus.released = subject;
		godsEye.subjectLockId = null;
		lockAnchor = null;
		lastInteraction = performance.now(); // orbit stays paused 30 s after release
		resetFrame();
		publishSnapshot();
	}

	/** Serve focus request `seq` (focus bus): fly + lock on landing, or say why not. */
	function runFocus(seq: number, eventId: string) {
		const ev = ops.events.find((e) => e.eventId === eventId);
		const c = ev ? eventCentroid(ev, godsEye.segmentById) : null;
		if (ev && c) {
			pendingFocus = null;
			focus.settle(seq, 'pending', `flying to ${eventLabel(eventId)}`);
			lockOn(eventId, c, seq);
			return;
		}
		const waiting = !ev || godsEye.segmentsFeed.status === 'empty';
		if (waiting && (!pendingFocus || pendingFocus.seq !== seq)) {
			pendingFocus = { seq, eventId, deadline: Date.now() + FOCUS_WAIT_MS };
			focus.settle(seq, 'pending', 'waiting for the board / segment geometry');
			return;
		}
		if (waiting && pendingFocus && Date.now() < pendingFocus.deadline) return;
		pendingFocus = null;
		focusFlash(`${eventLabel(eventId)}: no route geometry on the canvas — nothing to lock`);
		focus.settle(seq, 'no-geometry', 'event has no resolvable route spine');
	}

	/**
	 * A board event's name as this console may show it: a pursuit title
	 * carries the plate ('PURSUIT — stolen GJ27EM9001 …'), so every focus
	 * note, flash and chip title goes through the C6 mask (the event id when
	 * the board does not carry the event).
	 */
	function eventLabel(eventId: string, officer: boolean = ops.officerOn): string {
		const name = ops.events.find((e) => e.eventId === eventId)?.name;
		return name ? displayText(name, officer) : eventId;
	}

	function focusFlash(text: string) {
		shareFlash = text;
		setTimeout(() => {
			if (shareFlash === text) shareFlash = null;
		}, 5000);
	}

	function tourTarget(step: TourStep) {
		if (step.layer && (!activeLayers.has(step.layer) || (stats[step.layer]?.count ?? 0) === 0)) return null;
		return step.target();
	}

	function tourStep(i: number) {
		const C = window.Cesium;
		if (!viewer || !C || tourIndex == null) return;
		const steps = TOUR.filter((s) => tourTarget(s) !== null);
		if (!steps.length) {
			stopTour();
			return;
		}
		const idx = i % steps.length;
		const step = steps[idx];
		const t = tourTarget(step)!;
		tourIndex = idx;
		tourTotal = steps.length;
		tourLabel = t.label;
		const want = step.sensor ?? preTourSensor;
		if (want !== sensorMode) setSensor(want);
		// City Command: the stop's subject opens in the dossier, so the tour
		// narrates real rows (selected first — the clear region then already
		// accounts for the dossier the selection opens).
		if (hud === 'full' && t.urn && parseUrn(t.urn) && selection.current !== t.urn) {
			selection.select(t.urn, { fly: false, source: 'tour' });
		}
		flyFrame(t.lon, t.lat, t.range, step.headingDeg, step.pitchDeg, 2);
		tourTimer = setTimeout(() => {
			if (tourIndex == null) return;
			tourStep(idx + 1);
		}, 6000);
	}

	function startTour() {
		if (!viewer) return;
		if (godsEye.subjectLockId || lockFlight) releaseSubject({ why: 'a guided tour took the camera' });
		preTourSensor = sensorMode;
		picked = null;
		legendOpen = false;
		tourIndex = 0;
		tourStep(0);
	}

	function stopTour(restoreSensor = true) {
		const wasRunning = tourIndex != null;
		tourIndex = null;
		tourTotal = 0;
		tourLabel = '';
		if (tourTimer) clearTimeout(tourTimer);
		tourTimer = null;
		if (wasRunning) lastInteraction = performance.now();
		if (wasRunning && restoreSensor && sensorMode !== preTourSensor) setSensor(preTourSensor);
	}

	// ─── picking + hover ───────────────────────────────────────────────────────

	function layerOf(id: string): { layer: string; label: string } {
		for (const [layerId, inst] of instances) {
			if (inst.handlePick(id)) {
				return { layer: layerId, label: GODS_EYE_LAYERS.find((l) => l.id === layerId)?.label ?? layerId };
			}
		}
		if (id.startsWith('pursuit:')) return { layer: 'pursuit', label: 'PURSUIT' };
		if (id.startsWith('rpl:')) return { layer: 'replay', label: 'REPLAY · EMULATED' };
		if (id === HALO_ID) return { layer: 'selection', label: 'SELECTED' };
		return { layer: 'other', label: 'ENTITY' };
	}

	/**
	 * The C-C URN a picked primitive draws: the owning layer's urnOf, the
	 * pursuit overlay's subjects (the pursued vehicle, a choke junction, an
	 * onward segment), the halo (the selection itself). Replay primitives are
	 * tape records, not city entities — they have none.
	 */
	function urnOfPick(id: string): string | null {
		for (const inst of instances.values()) {
			if (inst.handlePick(id)) return inst.urnOf?.(id) ?? null;
		}
		if (id === HALO_ID) return selection.current;
		if (id.startsWith('pursuit:')) {
			const choke = /^pursuit:choke-(\d+)$/.exec(id);
			if (choke) return `junction:${choke[1]}`;
			const onward = /^pursuit:onward-(\d+)$/.exec(id);
			if (onward) return `segment:${onward[1]}`;
			const plan = ops.interception;
			return plan?.vehicleId ? `vehicle:${plan.vehicleId}` : plan ? `event:${plan.eventId}` : null;
		}
		return null;
	}

	/**
	 * Pick priority by layer — the operator's likely target first: unit >
	 * camera > vehicle mark > signal > pursuit > event > segment (lower
	 * wins). A small marker drawn over a road must win the click over the
	 * road's spine and the signal ring at the same spot.
	 */
	const PICK_RANK: Record<string, number> = {
		units: 0,
		dispatch: 0.5,
		cameras: 1,
		traces: 2,
		convoy: 2,
		replay: 2,
		enforcement: 2.5,
		signals: 3,
		pursuit: 4,
		anomalies: 4.5,
		events: 5,
		flow: 6,
	};

	/** Marker centres within this many px of the nearest one are 'at the click'. */
	const PICK_NEAR_PX = 3;

	/**
	 * Lines of EQUAL rank under the click (two events' spines sharing a
	 * segment) are not left to drill-pick order: the selected event wins,
	 * then a live (active) one over a planned one, then a pursuit, then the
	 * most recent window — and the id, last, so a click at the same pixel
	 * always picks the same entity. Lower is preferred.
	 */
	function lineTieBreak(e: CesiumEntity): [number, number, number, number, string] {
		const eventId = eventIdFromPick(e.id);
		const ev = eventId ? ops.eventById(eventId) : null;
		if (!ev) return [1, 1, 1, 0, e.id];
		const start = ev.windowStart ? Date.parse(ev.windowStart) : Number.NaN;
		return [
			ev.eventId === ops.selectedEventId ? 0 : 1,
			ev.state === 'active' ? 0 : 1,
			ev.eventKind === 'vehicle_pursuit' ? 0 : 1,
			Number.isFinite(start) ? -start : 0,
			e.id,
		];
	}

	function compareTieBreak(a: [number, number, number, number, string], b: [number, number, number, number, string]): number {
		for (let i = 0; i < 4; i++) if (a[i] !== b[i]) return (a[i] as number) - (b[i] as number);
		return a[4] < b[4] ? -1 : a[4] > b[4] ? 1 : 0;
	}

	/**
	 * The entity under a screen point: up to `limit` primitives drill-picked
	 * front to back. A MARKER (an entity with a position) beats a line; among
	 * markers the one whose centre is nearest the click wins, and markers
	 * about as near (within PICK_NEAR_PX) go by PICK_RANK — unit > camera >
	 * vehicle mark > signal > pursuit > event; lines (spines, segments) go
	 * by PICK_RANK alone. Only Cesium ENTITIES count — the selection halo,
	 * the agent's notes and anything that is not an entity (a 3D-tile
	 * feature of the photoreal layer, C-L) are see-through to the pointer.
	 */
	function pickedEntity(position: { x: number; y: number }, limit = 8): CesiumEntity | null {
		const C = window.Cesium;
		if (!viewer || !C) return null;
		const v = viewer;
		const asEntity = (hit: { id?: CesiumEntity | string } | undefined) => {
			const e = hit && typeof hit.id === 'object' ? hit.id : null;
			return e && typeof e.id === 'string' ? e : null;
		};
		let hits: Array<{ id?: CesiumEntity | string }> = [];
		try {
			hits = v.scene.drillPick(position, limit);
		} catch {
			return null; // scene mid-teardown
		}
		const time = v.clock.currentTime;
		const markers: Array<{ e: CesiumEntity; rank: number; d: number }> = [];
		const lines: Array<{ e: CesiumEntity; rank: number }> = [];
		for (const hit of hits) {
			const e = asEntity(hit);
			if (!e || e.id === HALO_ID || e.id.startsWith(AGENT_NOTE_PREFIX)) continue;
			const layer = layerOf(e.id).layer;
			// Draped lines share the road's pixels: a pursuit LINE (trace,
			// onward fan, arc, rim) lies on the very road an event's spine
			// drapes, so a click there means the event — the pursued vehicle
			// and its chokes are markers of their own and keep their rank.
			const rank = layer === 'pursuit' && e.polyline ? PICK_RANK.events + 0.25 : (PICK_RANK[layer] ?? 7);
			const world = e.polyline ? undefined : readProp(e.position, time);
			const win = world ? C.SceneTransforms.worldToWindowCoordinates(v.scene, world) : undefined;
			if (win && Number.isFinite(win.x) && Number.isFinite(win.y)) {
				markers.push({ e, rank, d: Math.hypot(win.x - position.x, win.y - position.y) });
			} else {
				lines.push({ e, rank });
			}
		}
		if (markers.length) {
			const nearest = Math.min(...markers.map((m) => m.d));
			const at = markers.filter((m) => m.d <= nearest + PICK_NEAR_PX);
			at.sort((a, b) => a.rank - b.rank || a.d - b.d);
			return at[0].e;
		}
		lines.sort((a, b) => a.rank - b.rank || compareTieBreak(lineTieBreak(a.e), lineTieBreak(b.e)));
		return lines[0]?.e ?? null;
	}

	// ─── label declutter (screen space) ───────────────────────────────────────

	/**
	 * Overlay labels of nearby markers would overprint each other (a Jan
	 * Rakshak unit's label over 'PURSUIT · GJ27TS•••2', an e-challan pin's
	 * rule over the pursuit event's name). Every DECLUTTER_MS the labels of
	 * these layers are placed in priority order — the pursued vehicle, the
	 * corridor events' names and tags, units, the pursuit's choke
	 * annotations, dispatch waypoints (a unit that arrived stands ON its
	 * waypoint junction), the convoy, e-challan pins, cameras, vehicle traces
	 * (a vehicle's own mark before its numbered camera hops), anomaly rings —
	 * each as the SCREEN BOX it draws (its anchor + pixelOffset, its origin,
	 * its lines of monospace text), and one whose box meets a placed one hides
	 * until they part. The e-challan / sighting PINS themselves (glyphs, not
	 * labels) are obstacles too: a unit's, camera's, hop's or anomaly's label
	 * that would print over a pin gives way to it — the pin's site label names
	 * the place (a hop 'n EM-CAM-04' under the ₹ pin at EM-CAM-04 read as
	 * noise). A label the operator hovers or selects (emphasis) always shows;
	 * a label already outside its distance condition takes no room.
	 */
	const DECLUTTER_RANK: Record<string, number> = {
		pursuit: 0,
		events: 0.75,
		units: 1,
		dispatch: 1.4,
		convoy: 1.5,
		enforcement: 2,
		cameras: 3,
		traces: 3.25,
		anomalies: 3.5,
	};
	/** A numbered camera hop gives way to its vehicle's own mark label. */
	const HOP_RANK = 3.3;
	/**
	 * Labels that give way to an enforcement pin (glyph) they would cover:
	 * units, cameras, anomalies and the numbered camera hops — never the
	 * earmarked vehicle's own mark label, an event's name or the pursuit's.
	 */
	const YIELD_TO_PINS = new Set(['units', 'cameras', 'anomalies']);
	const yieldsToPins = (layer: string, id: string) => YIELD_TO_PINS.has(layer) || id.startsWith('veh:hop-');
	/** Glyph metrics of the canvas's 12–13 px monospace labels (box estimate). */
	const LABEL_CHAR_PX = 7.4;
	const LABEL_LINE_PX = 16;
	const LABEL_PAD_PX = 4;
	const DECLUTTER_MS = 500;
	/** Hidden labels → the show value they had (restored when they part). */
	const declutterHidden = new Map<CesiumEntity, unknown>();

	type LabelBits = {
		show?: unknown;
		distanceDisplayCondition?: unknown;
		text?: unknown;
		pixelOffset?: unknown;
		verticalOrigin?: unknown;
		horizontalOrigin?: unknown;
	};
	type ScreenBox = { l: number; t: number; r: number; b: number };

	/** Metres between two world (Cartesian3) positions. */
	function cartesianDistance(a: unknown, b: unknown): number {
		const p = a as { x?: number; y?: number; z?: number } | null;
		const q = b as { x?: number; y?: number; z?: number } | null;
		if (!p || !q) return Number.POSITIVE_INFINITY;
		return Math.hypot((p.x ?? 0) - (q.x ?? 0), (p.y ?? 0) - (q.y ?? 0), (p.z ?? 0) - (q.z ?? 0));
	}

	function labelVisibleAt(label: LabelBits, distanceM: number, time: unknown): boolean {
		if (readProp(label.show as CesiumProperty<boolean> | boolean | undefined, time) === false) return false;
		const ddc = readProp(label.distanceDisplayCondition as CesiumProperty<unknown> | undefined, time) as
			| { near?: number; far?: number }
			| undefined;
		if (!ddc) return true;
		return distanceM >= (ddc.near ?? 0) && distanceM <= (ddc.far ?? Number.POSITIVE_INFINITY);
	}

	/**
	 * The screen box a label draws: its anchor moved by its pixelOffset,
	 * placed by its origins (Cesium VerticalOrigin CENTER 0 / BOTTOM 1 /
	 * BASELINE 2 / TOP -1; HorizontalOrigin CENTER 0 / LEFT 1 / RIGHT -1),
	 * sized by its lines of text.
	 */
	function labelBox(label: LabelBits, win: { x: number; y: number }, time: unknown): ScreenBox {
		const text = String(readProp(label.text as CesiumProperty<string> | string | undefined, time) ?? '');
		const lines = text.split('\n');
		const w = Math.max(1, ...lines.map((l) => l.length)) * LABEL_CHAR_PX + LABEL_PAD_PX * 2;
		const h = lines.length * LABEL_LINE_PX + LABEL_PAD_PX;
		const off = readProp(label.pixelOffset as CesiumProperty<{ x?: number; y?: number }> | undefined, time);
		const x = win.x + (off?.x ?? 0);
		const y = win.y + (off?.y ?? 0);
		const vo = Number(readProp(label.verticalOrigin as CesiumProperty<number> | number | undefined, time) ?? 0);
		const ho = Number(readProp(label.horizontalOrigin as CesiumProperty<number> | number | undefined, time) ?? 0);
		const t = vo === -1 ? y : vo === 0 ? y - h / 2 : y - h;
		const l = ho === 1 ? x : ho === -1 ? x - w : x - w / 2;
		return { l, t, r: l + w, b: t + h };
	}

	const boxesMeet = (a: ScreenBox, b: ScreenBox) => a.l < b.r && b.l < a.r && a.t < b.b && b.t < a.b;

	type BillboardBits = {
		show?: unknown;
		width?: unknown;
		height?: unknown;
		scale?: unknown;
		pixelOffset?: unknown;
		verticalOrigin?: unknown;
		horizontalOrigin?: unknown;
	};

	/** The screen box a billboard (a pin glyph) draws — same origin rules as labelBox. */
	function billboardBox(bb: BillboardBits, win: { x: number; y: number }, time: unknown): ScreenBox | null {
		if (readProp(bb.show as CesiumProperty<boolean> | boolean | undefined, time) === false) return null;
		const scale = Number(readProp(bb.scale as CesiumProperty<number> | number | undefined, time) ?? 1) || 1;
		const w = Number(readProp(bb.width as CesiumProperty<number> | number | undefined, time) ?? 0) * scale;
		const h = Number(readProp(bb.height as CesiumProperty<number> | number | undefined, time) ?? 0) * scale;
		if (!(w > 0 && h > 0)) return null;
		const off = readProp(bb.pixelOffset as CesiumProperty<{ x?: number; y?: number }> | undefined, time);
		const x = win.x + (off?.x ?? 0);
		const y = win.y + (off?.y ?? 0);
		const vo = Number(readProp(bb.verticalOrigin as CesiumProperty<number> | number | undefined, time) ?? 0);
		const ho = Number(readProp(bb.horizontalOrigin as CesiumProperty<number> | number | undefined, time) ?? 0);
		const t = vo === -1 ? y : vo === 0 ? y - h / 2 : y - h;
		const l = ho === 1 ? x : ho === -1 ? x - w : x - w / 2;
		return { l, t, r: l + w, b: t + h };
	}

	function declutterLabels() {
		const C = window.Cesium;
		const v = viewer;
		if (!v || !C || phase !== 'ready') return;
		const time = v.clock.currentTime;
		const eye = v.camera.positionWC;
		const placed: ScreenBox[] = [];
		/** Enforcement pin glyphs on screen (obstacles for the labels that yield to them). */
		const pins: ScreenBox[] = [];
		const candidates: Array<{ e: CesiumEntity; label: LabelBits; rank: number; box: ScreenBox; yieldsToPins: boolean }> = [];
		for (const e of v.entities.values) {
			if (typeof e.id !== 'string') continue;
			const label = e.label as LabelBits | undefined;
			const layer = label || e.billboard ? layerOf(e.id).layer : null;
			if (layer === 'enforcement' && e.billboard) {
				const world = readProp(e.position, time);
				const win = world ? C.SceneTransforms.worldToWindowCoordinates(v.scene, world) : null;
				const box = win && Number.isFinite(win.x) && Number.isFinite(win.y) ? billboardBox(e.billboard as BillboardBits, win, time) : null;
				if (box) pins.push(box);
			}
			if (!label || layer == null) continue;
			// the pursued vehicle's own label outranks everything; its choke
			// annotations rank after the units sent to them; a camera hop after
			// its vehicle's mark
			const rank = e.id.startsWith('pursuit:choke-')
				? 1.25
				: e.id.startsWith('veh:hop-')
					? HOP_RANK
					: DECLUTTER_RANK[layer];
			if (rank == null) continue;
			const world = readProp(e.position, time);
			if (!world) continue;
			// judge its OWN visibility (the show we forced off does not count)
			const own: LabelBits = declutterHidden.has(e) ? { distanceDisplayCondition: label.distanceDisplayCondition } : label;
			const distance = cartesianDistance(eye, world);
			if (!emphasis.has(e) && !labelVisibleAt(own, distance, time)) continue;
			const win = C.SceneTransforms.worldToWindowCoordinates(v.scene, world);
			if (!win || !Number.isFinite(win.x) || !Number.isFinite(win.y)) continue;
			candidates.push({ e, label, rank, box: labelBox(label, win, time), yieldsToPins: yieldsToPins(layer, e.id) });
		}
		candidates.sort((a, b) => Number(emphasis.has(b.e)) - Number(emphasis.has(a.e)) || a.rank - b.rank || a.box.t - b.box.t);
		for (const c of candidates) {
			const clash = placed.some((p) => boxesMeet(p, c.box)) || (c.yieldsToPins && pins.some((p) => boxesMeet(p, c.box)));
			if (!clash || emphasis.has(c.e)) {
				placed.push(c.box);
				if (declutterHidden.has(c.e)) {
					c.label.show = declutterHidden.get(c.e) ?? true;
					declutterHidden.delete(c.e);
				}
			} else if (!declutterHidden.has(c.e)) {
				declutterHidden.set(c.e, c.label.show);
				c.label.show = false;
			}
		}
		// entities a layer removed are forgotten
		for (const e of [...declutterHidden.keys()]) if (v.entities.getById(e.id) !== e) declutterHidden.delete(e);
	}

	/**
	 * Left click: the picked primitive's URN becomes the selection (the
	 * dossier on City Command; event URNs also select the event on the
	 * board); the wall views show the layer's pick card as well.
	 */
	function onPick(position: { x: number; y: number }) {
		const entity = pickedEntity(position);
		if (!entity) {
			picked = null;
			publishSnapshot();
			return;
		}
		const { layer, label } = layerOf(entity.id);
		const eventId = eventIdFromPick(entity.id);
		const text = descriptionOf(entity) ?? entity.id;
		const urn = entity.id === HALO_ID ? null : urnOfPick(entity.id);
		if (urn && parseUrn(urn)) {
			selection.select(urn, { fly: false, source: 'map', pick: { pickId: entity.id, layer, text } });
		} else if (eventId && eventId !== ops.selectedEventId && ops.events.some((e) => e.eventId === eventId)) {
			// A PREVIEW fixture (slug id — no C-C URN) still selects on the board.
			// selectEvent resets the band and the audit tail, so never re-select.
			ops.selectEvent(eventId);
		}
		picked = { id: entity.id, layer, label, text, eventId, urn };
		legendOpen = false;
		applySelectionHighlight();
		publishSnapshot();
	}

	/**
	 * Emphasis held per entity. Hover, the selection and an applied agent
	 * highlight may overlap on one entity; each enlarge captures the size it
	 * found, so undos run out of order would leave it enlarged. The count
	 * keeps one enlargement per entity, undone when the LAST holder releases.
	 */
	const emphasis = new Map<CesiumEntity, { holders: number; undo: () => void }>();

	/** Emphasise one entity (hover, selection, agent highlight); returns its release. */
	function highlight(entity: CesiumEntity): () => void {
		const held = emphasis.get(entity);
		if (held) held.holders += 1;
		else emphasis.set(entity, { holders: 1, undo: enlarge(entity) });
		let released = false;
		return () => {
			if (released) return;
			released = true;
			const h = emphasis.get(entity);
			if (!h) return;
			h.holders -= 1;
			if (h.holders <= 0) {
				emphasis.delete(entity);
				h.undo();
			}
		};
	}

	/**
	 * Enlarge one entity: a polyline widens, a point grows, a pin (billboard)
	 * scales up, and a label hidden by distance shows at any range — the
	 * decluttered overview never hides the text of what the operator is
	 * looking at. Returns the undo.
	 */
	function enlarge(entity: CesiumEntity): () => void {
		const time = viewer?.clock.currentTime;
		const undo: Array<() => void> = [];
		const line = entity.polyline;
		if (line) {
			const w = Number(readProp(line.width, time) ?? 2);
			line.width = w + 3;
			undo.push(() => {
				line.width = w;
			});
		}
		const pt = entity.point;
		if (pt) {
			const size = Number(readProp(pt.pixelSize, time) ?? 8);
			const outline = Number(readProp(pt.outlineWidth, time) ?? 1);
			pt.pixelSize = size + 6;
			pt.outlineWidth = outline + 1;
			undo.push(() => {
				pt.pixelSize = size;
				pt.outlineWidth = outline;
			});
		}
		const pin = entity.billboard;
		if (pin) {
			// Restore the PROPERTY, not its value: a hot camera's dot pulses
			// through a time-varying scale (layers/cameras.ts, clamped dots).
			const held = pin.scale;
			const scale = Number(readProp(held, time) ?? 1);
			pin.scale = scale * 1.35;
			undo.push(() => {
				pin.scale = held;
			});
		}
		const text = entity.label;
		if (text && text.distanceDisplayCondition) {
			const ddc = text.distanceDisplayCondition;
			text.distanceDisplayCondition = undefined;
			undo.push(() => {
				text.distanceDisplayCondition = ddc;
			});
		}
		return () => {
			for (const u of undo) u();
		};
	}

	function applyHover() {
		hoverTimer = null;
		if (!viewer || !container || !hoverAt || pressing) return;
		const entity = pickedEntity(hoverAt, 4); // hover: a lighter drill (≤12 picks/s)
		if (entity === (hovered?.entity ?? null)) return;
		hovered?.restore();
		hovered = null;
		const urn = entity ? urnOfPick(entity.id) : null;
		selection.hover = urn;
		// Only entities still in the collection (layers redraw on data change);
		// the selection's own primitives already wear the selection highlight.
		if (entity && viewer.entities.getById(entity.id) === entity) {
			const selected = selectedHighlights.some((h) => h.entity === entity);
			hovered = { entity, restore: selected ? () => {} : highlight(entity) };
		}
		container.style.cursor = hovered ? 'pointer' : '';
	}

	function onHover(position: { x: number; y: number }) {
		hoverAt = position;
		if (!hoverTimer) hoverTimer = setTimeout(applyHover, 80); // ≤12 picks/s
	}

	// ─── selection: halo, highlight, fly-to ────────────────────────────────────

	/** Camera framing for a single entity fly-to (offset ≈ 0.007° — well inside 0.02°). */
	const ENTITY_VIEW = { range: 1700, pitchDeg: -62, flightSec: 0.9, maxOffsetM: 900 } as const;
	/**
	 * An applied AI suggestion lands steeper and closer: the camera stands
	 * almost over the subject (offset ≈ 460 m ≈ 0.004°), so 'the agent showed
	 * me the vehicle' reads as the vehicle mid-screen, not a street away.
	 */
	const AGENT_VIEW = { range: 1500, pitchDeg: -72 } as const;
	/** An applied canvas_fly_to's own range_m for its URN (the next agent fly-to reads it once). */
	let agentFlyRange: { urn: string; range: number } | null = null;

	/**
	 * Where an entity is on the ground: the fused registry first; an event's
	 * spine (with its radius, for framing) from the board; a segment or a
	 * junction from the canvas's own geometry; the open dossier last.
	 */
	function knownPosition(urn: string): { lon: number; lat: number; radiusM?: number } | null {
		const parsed = parseUrn(urn);
		const e = cityRegistry.get(urn);
		if (parsed?.kind === 'event') {
			const ev = ops.events.find((x) => x.eventId === parsed.id);
			const c = ev ? eventCentroid(ev, godsEye.segmentById) : null;
			if (e && e.lon != null && e.lat != null) return { lon: e.lon, lat: e.lat, radiusM: c?.radiusM };
			if (c) return c;
		}
		if (e && e.lon != null && e.lat != null) return { lon: e.lon, lat: e.lat };
		if (parsed?.kind === 'segment') {
			const seg = godsEye.segmentById.get(Number(parsed.id));
			const mid = seg?.coordinates[Math.floor((seg?.coordinates.length ?? 0) / 2)];
			if (mid) return { lon: mid[0], lat: mid[1] };
		}
		if (parsed?.kind === 'junction') {
			const j = godsEye.signals.find((s) => s.junctionId === parsed.id);
			if (j) return { lon: j.lon, lat: j.lat };
		}
		const d = dossier.data;
		if (d && d.entity.urn === urn && d.entity.lon != null && d.entity.lat != null) {
			return { lon: d.entity.lon, lat: d.entity.lat };
		}
		return null;
	}

	/** The selection's primitives stay highlighted (re-applied when a layer redraws them). */
	function applySelectionHighlight() {
		if (!viewer) return;
		const urn = selection.current;
		const matches: CesiumEntity[] = [];
		if (urn) {
			for (const e of viewer.entities.values) {
				if (e.id === HALO_ID || e.id.startsWith('rpl:')) continue;
				if (urnOfPick(e.id) === urn) matches.push(e);
			}
		}
		const sig = matches.map((e) => e.id).join('|');
		const v = viewer;
		if (sig === selectedHighlightSig && selectedHighlights.every((h) => v.entities.getById(h.entity.id) === h.entity)) return;
		for (const h of selectedHighlights) h.restore();
		selectedHighlights = matches.map((e) => {
			if (hovered?.entity === e) {
				// already enlarged by the hover: let the hover's restore hand it back
				hovered.restore();
				hovered = { entity: e, restore: () => {} };
			}
			return { entity: e, restore: highlight(e) };
		});
		selectedHighlightSig = sig;
	}

	/** Move the halo onto the selection (or hide it). */
	function placeHalo() {
		const urn = selection.current;
		haloAt = urn ? knownPosition(urn) : null;
		drawHalo();
	}

	/**
	 * Lay the halo's ring on the ground at haloAt — only once the subject
	 * moved HALO_RELAY_M: a draped ring is a GroundPolylinePrimitive that
	 * Cesium rebuilds in a worker, so it is never re-laid per frame, and a
	 * moving subject (a convoy, a pursued vehicle) re-lays it in steps.
	 */
	function drawHalo() {
		const C = window.Cesium;
		const halo = haloEntity;
		if (!C || !halo) return;
		const at = haloAt;
		if (!at) {
			halo.show = false;
			return;
		}
		const kx = 111_320 * Math.cos((at.lat * Math.PI) / 180);
		const moved = !haloDrawnAt || Math.hypot((at.lon - haloDrawnAt.lon) * kx, (at.lat - haloDrawnAt.lat) * 110_540) > HALO_RELAY_M;
		if (moved && halo.polyline) {
			(halo.polyline as { positions?: unknown }).positions = groundPath(C, ringCoords(at.lon, at.lat, HALO_RADIUS_M));
			haloDrawnAt = { lon: at.lon, lat: at.lat };
		}
		halo.show = true;
	}

	/**
	 * The selection halo: a glowing RING around its subject, DRAPED on the
	 * ground (terrain and the photoreal tiles — a ring at a fixed height ran
	 * inside the 3D city) above every other draped line; never a filled
	 * disc (the old 16 %-alpha disc read as an opaque plate over the very
	 * marker it was meant to point at). It breathes through its glow colour
	 * — a material uniform, so the ring itself is re-laid only when its
	 * subject moves (drawHalo); reduced motion holds it steady.
	 */
	function addHalo(cesium: CesiumGlobal, v: CesiumViewer) {
		const ring = cssColor(cesium, '--v-data', cesium.Color.CYAN) as { withAlpha?: (a: number) => unknown };
		const t0 = performance.now();
		const glow = () => {
			if (reducedMotionPref()) return ring.withAlpha?.(0.9) ?? ring;
			const p = ((performance.now() - t0) % 1800) / 1800;
			return ring.withAlpha?.(0.5 + 0.45 * (0.5 - 0.5 * Math.cos(p * 2 * Math.PI))) ?? ring;
		};
		haloEntity = v.entities.add({
			id: HALO_ID,
			show: false,
			polyline: drapedPolyline(cesium, {
				positions: [],
				width: 6,
				material: new cesium.PolylineGlowMaterialProperty({ color: new cesium.CallbackProperty(glow, false), glowPower: 0.28 }),
				z: DRAPE_Z.pursuit + 1,
			}),
			description: 'the selected entity',
		}) as CesiumEntity & { show: boolean };
		haloDrawnAt = null;
		drawHalo();
	}

	function reducedMotionPref(): boolean {
		return typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;
	}

	/** Serve fly request `seq`: bring `urn` into view (or say why not). */
	async function flyToUrn(seq: number, urn: string) {
		let at = knownPosition(urn);
		if (!at) {
			selection.settleFly(seq, 'pending', 'resolving the entity position');
			const d = await dossier.peek(urn);
			if (seq !== selection.flySeq || !viewer || destroyed) return;
			if (d && d.entity.lon != null && d.entity.lat != null) at = { lon: d.entity.lon, lat: d.entity.lat };
		}
		if (!at) {
			selection.settleFly(seq, 'no-position', 'the entity has no known position');
			focusFlash(`${urn}: no position in the city picture — nothing to fly to`);
			return;
		}
		if (tourIndex != null) stopTour();
		if (godsEye.subjectLockId || lockFlight) releaseSubject({ why: 'a fly-to took the camera', remember: false });
		const agent = selection.source === 'agent' && selection.flyUrn === urn;
		const asked = agent && agentFlyRange?.urn === urn ? agentFlyRange.range : null;
		agentFlyRange = null;
		let range: number = agent ? (asked ?? AGENT_VIEW.range) : ENTITY_VIEW.range;
		let pitchDeg: number = agent ? AGENT_VIEW.pitchDeg : ENTITY_VIEW.pitchDeg;
		if (asked != null && asked > AGENT_VIEW.range) {
			// A wider ask still stands over the subject: the pitch steepens so
			// the camera never sits further off it than the agent view's own
			// offset (≈ 460 m) — the entity stays mid-screen, only smaller.
			const maxOffsetM = AGENT_VIEW.range * Math.cos((AGENT_VIEW.pitchDeg * Math.PI) / 180);
			pitchDeg = -Math.max(-AGENT_VIEW.pitchDeg, (Math.acos(Math.min(1, maxOffsetM / range)) * 180) / Math.PI);
		}
		if (at.radiusM != null && at.radiusM > 400) {
			// An area (an event's spines): frame it whole, camera kept over it.
			range = subjectFraming(at.radiusM).range;
			pitchDeg = -Math.max(62, (Math.acos(Math.min(1, ENTITY_VIEW.maxOffsetM / range)) * 180) / Math.PI);
		}
		haloAt = { lon: at.lon, lat: at.lat };
		drawHalo();
		lastInteraction = performance.now();
		selection.settleFly(seq, 'flying');
		flyFrame(at.lon, at.lat, range, 0, pitchDeg, ENTITY_VIEW.flightSec, {
			complete: () => {
				selection.settleFly(seq, 'landed');
				lastInteraction = performance.now();
				publishSnapshot();
			},
			cancel: () => selection.settleFly(seq, 'cancelled', 'another camera flight took over'),
		});
		publishSnapshot();
	}

	// ─── REPLAY: every live layer stands down while a tape plays ──────────────

	/**
	 * Every live layer pauses while a tape plays — the roads' live colours,
	 * the board's events, signals and cameras included — so the replayed
	 * frames are the only data on the canvas (the judge saw live layers
	 * drawing over the replay). Basemap-like layers (3D tiles) stay.
	 */
	const REPLAY_KEEPS = new Set(['photoreal-3d']);

	function suspendLiveLayers() {
		if (replaySuspended.length) return;
		const paused: string[] = [];
		for (const id of GODS_EYE_LAYER_IDS.filter((l) => !REPLAY_KEEPS.has(l))) {
			if (activeLayers.has(id)) {
				instances.get(id)?.deactivate();
				paused.push(id);
			}
		}
		replaySuspended = paused;
		pursuitToken++;
		pursuitShownSig = pursuitPendingSig = '';
		pursuit?.clear();
		refreshStats();
	}

	function resumeLiveLayers() {
		for (const id of replaySuspended) if (activeLayers.has(id)) instances.get(id)?.activate();
		replaySuspended = [];
		pursuitShownSig = '';
		refreshStats();
	}

	// ─── AGENTIC GOD'S-EYE: AI-suggested directives a human applies ──────────

	/** What this canvas can resolve right now (the fused registry + its own network). */
	function directiveWorld(): DirectiveWorld {
		const segments = godsEye.segmentById;
		return {
			hasUrn: (urn) => cityRegistry.get(urn) != null,
			hasSegment: (id) => segments.has(id),
			hasJunction: (id) =>
				cityRegistry.get(`junction:${id}`) != null || godsEye.signals.some((s) => String(s.junctionId) === String(id)),
			layerIds: [...instances.keys()],
		};
	}

	/** The world is worth re-checking queued URNs against (registry answered, network loaded). */
	function worldReady(): boolean {
		return phase === 'ready' && cityRegistry.status === 'ok' && godsEye.segments.length > 0;
	}

	/** The named duty officer (the ack's claimed officer; the deck proxy adds the verified one). */
	function ackOfficer(): string | null {
		return ops.officerOn ? ops.dutyOfficer.trim() : null;
	}

	function clearAgentHighlights() {
		if (agentHighlightTimer) clearTimeout(agentHighlightTimer);
		agentHighlightTimer = null;
		for (const undo of agentHighlights) undo();
		agentHighlights = [];
	}

	/** Emphasise every primitive of `urns` for AGENT_HIGHLIGHT_TTL_MS and frame them. */
	function highlightUrns(urns: string[]) {
		if (!viewer) return;
		clearAgentHighlights();
		const wanted = new Set(urns);
		const pts: Array<[number, number]> = [];
		for (const e of viewer.entities.values) {
			if (e.id === HALO_ID || e.id.startsWith('rpl:') || e.id.startsWith(AGENT_NOTE_PREFIX)) continue;
			const urn = urnOfPick(e.id);
			if (urn && wanted.has(urn)) agentHighlights.push(highlight(e));
		}
		for (const urn of urns) {
			const at = knownPosition(urn);
			if (at) pts.push([at.lon, at.lat]);
		}
		agentHighlightTimer = setTimeout(clearAgentHighlights, AGENT_HIGHLIGHT_TTL_MS);
		const f = frameOf(pts, 1200);
		if (f) flyFrame(f.lon, f.lat, f.range, 0, -55, 1.2);
		publishSnapshot();
	}

	/** A short-lived note pinned to an entity (canvas_annotate), masked at the sink. */
	function addAgentNote(d: CanvasDirective) {
		const C = window.Cesium;
		if (!viewer || !C || d.args.verb !== 'canvas_annotate') return;
		const at = knownPosition(d.args.urn);
		if (!at) return;
		const id = `${AGENT_NOTE_PREFIX}${d.id}`;
		viewer.entities.removeById(id);
		const tone = d.args.severity === 'critical' ? '--v-band-red' : d.args.severity === 'warn' ? '--v-band-amber' : '--v-tool';
		viewer.entities.add({
			id,
			// On the ground beside its entity (a fixed 30 m sat inside the 3D city), never hidden by a building.
			position: ground(C, at.lon, at.lat),
			label: clampedLabel(C, {
				text: `${t(ops.lang, 'agent_note_label')} · ${displayText(d.args.text, ops.officerOn)}`,
				font: '600 13px monospace',
				fillColor: cssColor(C, tone, C.Color.WHITE),
				outlineColor: cssColor(C, '--v-canvas', C.Color.BLACK),
				outlineWidth: 4,
				style: 2, // FILL_AND_OUTLINE
				verticalOrigin: 1, // BOTTOM
				pixelOffset: new C.Cartesian2(0, -18),
			}),
			description: `AI note (proposal applied by the operator) · ${d.args.text}`,
		});
		const old = agentNotes.get(id);
		if (old) clearTimeout(old);
		agentNotes.set(
			id,
			setTimeout(() => {
				agentNotes.delete(id);
				viewer?.entities.removeById(id);
			}, AGENT_NOTE_TTL_MS),
		);
	}

	/**
	 * Apply one AI suggestion — a HUMAN pressed Apply. Local only (camera,
	 * selection, halo, a TTL note, sensor, layer); the brain gets one ack.
	 */
	function applyDirective(d: CanvasDirective) {
		if (!canvasBus.get(d.id)) return; // already settled (double press)
		const a = d.args;
		switch (a.verb) {
			case 'canvas_fly_to':
				if (a.urn) {
					// the proposal's own range frames the entity (else AGENT_VIEW)
					agentFlyRange = a.rangeM != null ? { urn: a.urn, range: a.rangeM } : null;
					selection.select(a.urn, { fly: true, source: 'agent' });
				} else if (a.lon != null && a.lat != null) {
					if (tourIndex != null) stopTour();
					lastInteraction = performance.now();
					flyFrame(a.lon, a.lat, a.rangeM ?? AGENT_VIEW.range, 0, AGENT_VIEW.pitchDeg, ENTITY_VIEW.flightSec);
				}
				break;
			case 'canvas_track_entity':
				agentFlyRange = null;
				selection.select(a.urn, { fly: true, source: 'agent' });
				break;
			case 'canvas_highlight':
				highlightUrns(a.urns);
				break;
			case 'canvas_annotate':
				addAgentNote(d);
				break;
			case 'canvas_set_sensor':
				setSensor(a.mode);
				updateHash();
				break;
			case 'canvas_toggle_layer':
				setLayerEnabled(a.layerId, a.on);
				break;
		}
		canvasBus.settle(d.id, 'applied', ackOfficer());
		publishSnapshot();
	}

	function dismissDirective(d: CanvasDirective) {
		canvasBus.settle(d.id, 'dismissed', ackOfficer());
		publishSnapshot();
	}

	/** 'fly to <road>' / units: a point flight from the palette. */
	function paletteFly(lon: number, lat: number, rangeM: number) {
		if (tourIndex != null) stopTour();
		if (godsEye.subjectLockId || lockFlight) releaseSubject({ why: 'the command palette took the camera', remember: false });
		lastInteraction = performance.now();
		flyFrame(lon, lat, rangeM, 0, -55, 1.2);
	}

	/** The brain's English verb lead ('track …', 'fly to …') — replaced by the localised verb. */
	const EN_VERB_LEAD = /^(track|fly to|highlight|note on|sensor|layer)\s+/i;

	/** '<verb> <label>' for a chip: the localised verb, the label masked at the sink. */
	function chipText(d: CanvasDirective): string {
		const verb = t(ops.lang, `agent_verb_${d.verb}`);
		return `${verb} ${displayText(d.label, ops.officerOn).replace(EN_VERB_LEAD, '')}`;
	}

	/**
	 * A modal dialog is open on screen. Only a VISIBLE one counts: Cesium
	 * keeps its credit lightbox (role=dialog, aria-modal) in the DOM with
	 * display:none for the life of the viewer.
	 */
	function modalOpen(): boolean {
		return Array.from(document.querySelectorAll('[aria-modal="true"]')).some((el) => el.getClientRects().length > 0);
	}

	/**
	 * Layered Esc: tour → legend → pick card → exit (/gods-eye). An Esc that
	 * another surface already handled, typed in a field (the command
	 * search) or pressed under an open modal belongs to that surface.
	 */
	function onKey(e: KeyboardEvent) {
		if (e.key !== 'Escape' || e.defaultPrevented) return;
		const t = e.target as HTMLElement | null;
		const typing = !!t && (t.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(t.tagName));
		if (typing || modalOpen()) return;
		if (tourIndex != null) {
			stopTour();
		} else if (legendOpen) {
			legendOpen = false;
		} else if (picked && showPickCard) {
			picked = null;
			publishSnapshot();
		} else if (hud === 'full' && selection.current) {
			// City Command: Esc closes the dossier before it leaves the view.
			selection.clear();
			picked = null;
			publishSnapshot();
		} else if (hud === 'full' && onexit) {
			onexit();
		} else {
			return;
		}
		e.preventDefault(); // consumed: later listeners see defaultPrevented
	}

	/** 1 Hz: AMA clock, camera snapshot, queued focus retries, the selection's halo + highlight, the AI queue. */
	function tick() {
		nowMs = Date.now();
		if (pendingFocus && phase === 'ready') runFocus(pendingFocus.seq, pendingFocus.eventId);
		if (phase === 'ready') {
			placeHalo();
			applySelectionHighlight();
			// Proposals made off-canvas (the agent console) are resolved here;
			// expired ones leave the queue on their own.
			if (worldReady()) canvasBus.resolve(directiveWorld());
			else canvasBus.expire();
			serveSelectionRequest();
		}
		publishSnapshot();
	}

	/** A cited entity opened from the agent console: City Command selects + flies once it can. */
	function serveSelectionRequest() {
		if (hud !== 'full' || !canvasBus.selectionRequest || cityRegistry.status !== 'ok') return;
		const req = canvasBus.takeSelection();
		if (req && parseUrn(req.urn)) selection.select(req.urn, { fly: true, source: req.source });
	}

	onMount(() => {
		const settle = makeReadyHook();
		detachFocus = focus.attach();
		lastInteraction = performance.now();
		ops.start(); // events + band feed the events layer / AMA chip
		godsEye.start();
		clockTimer = setInterval(tick, 1000);

		(async () => {
			if (!container) return;
			if (!webglAvailable()) {
				unavailableReason = 'WebGL is unavailable in this browser';
				phase = 'unavailable';
				settle(false, unavailableReason);
				return;
			}
			if (!window.Cesium) {
				try {
					const link = document.createElement('link');
					link.rel = 'stylesheet';
					link.href = '/cesium/Widgets/widgets.css';
					document.head.appendChild(link);
					await loadScript('/cesium/Cesium.js');
					if (destroyed) return;
				} catch (err) {
					unavailableReason = `Cesium bundle not served (${
						err instanceof Error ? err.message : String(err)
					}) — dev servers need static/cesium (see infra/docker/Dockerfile.command-deck)`;
					phase = 'unavailable';
					settle(false, unavailableReason);
					return;
				}
			}
			const cesium = window.Cesium!;
			try {
				await godsEye.pollImageryMode();
				if (destroyed) return;
				const viewerOpts = {
					baseLayerPicker: false,
					geocoder: false,
					homeButton: false,
					sceneModePicker: false,
					navigationHelpButton: false,
					animation: false,
					timeline: false,
					fullscreenButton: false,
					infoBox: false,
					selectionIndicator: false,
					requestRenderMode: false,
					contextOptions: { webgl: { preserveDrawingBuffer: true } },
				};
				const viewerWith = (b: Basemap) =>
					new cesium.Viewer(container!, {
						...viewerOpts,
						baseLayer: b.baseLayer,
						...(b.terrain ? { terrain: b.terrain } : { terrainProvider: b.terrainProvider }),
					});

				// The ion upgrade, when keyed — any synchronous failure (API drift,
				// constructor errors) lands on the keyless basemap instead of
				// taking the canvas down; async failures are watched below.
				let basemap: Basemap | null = null;
				if (godsEye.imageryMode === 'ion-photoreal' && godsEye.imageryToken) {
					try {
						const ion = createIonBasemap(cesium, godsEye.imageryToken);
						viewer = viewerWith(ion);
						basemap = ion;
					} catch (err) {
						basemapNote = `ion upgrade failed (${
							err instanceof Error ? err.message : String(err)
						}) — keyless OSM fallback`;
						viewer = null;
						container.replaceChildren(); // drop a half-built widget
					}
				}
				if (!viewer || !basemap) {
					basemap = createKeylessBasemap(cesium);
					viewer = viewerWith(basemap);
				}
				basemapId = basemap.id;
				prepareGround(cesium, viewer);
				watchIonBasemap(cesium, viewer, basemap);
				watchTileErrors(viewer);

				// Real-time tilesLoaded publishing: the queue-depth event fires
				// as imagery loads, so the test snapshot (and any observer) sees
				// the moment the globe settles instead of waiting for the next
				// layer-refresh publish.
				const globeEvents = viewer.scene.globe as unknown as {
					tileLoadProgressEvent?: CesiumEvent<[number]>;
				};
				if (globeEvents.tileLoadProgressEvent) {
					removeTileProgress = globeEvents.tileLoadProgressEvent.addEventListener((remaining) => {
						if (snapHook) snapHook.tilesLoaded = remaining === 0;
						if (remaining === 0) releaseStars();
					});
				}

				applySensorStage(cesium, viewer);
				restoreFromHash(cesium, viewer);

				// Registry lifecycle.
				instances = new Map();
				for (const layer of GODS_EYE_LAYERS) {
					const inst = layer.create({
						cesium,
						viewer,
						fetchJson,
						notify: refreshStats,
					});
					await inst.status();
					if (destroyed) return; // onDestroy already tore down the viewer + the layers so far
					inst.init({ cesium, viewer, fetchJson, notify: refreshStats });
					instances.set(layer.id, inst);
				}
				for (const layer of GODS_EYE_LAYERS) {
					if (activeLayers.has(layer.id)) instances.get(layer.id)?.activate();
				}

				// The shared pursuit overlay (wall + cinematic), the replay
				// overlay (tape tracks on the clock) and the selection halo.
				pursuit = createPursuitOverlay(cesium, viewer);
				replayOverlay = createReplayOverlay(cesium, viewer);
				addHalo(cesium, viewer);

				// Picking (pick card + board selection) and hover highlight.
				const hh = new cesium.ScreenSpaceEventHandler(viewer.scene.canvas);
				hh.setInputAction((movement) => {
					if (movement.position) onPick(movement.position);
				}, cesium.ScreenSpaceEventType.LEFT_CLICK);
				hh.setInputAction((movement) => {
					if (movement.endPosition) onHover(movement.endPosition);
				}, cesium.ScreenSpaceEventType.MOUSE_MOVE);
				handler = hh;

				// Interaction hands the camera back from the orbit/tour (capture:
				// before Cesium's own handlers see the gesture).
				container.addEventListener('pointerdown', onPointerDown, { passive: true, capture: true });
				container.addEventListener('pointermove', onPointerMove, { passive: true, capture: true });
				container.addEventListener('pointerup', onPointerUp, { passive: true, capture: true });
				container.addEventListener('wheel', onWheel, { passive: true, capture: true });

				// Wheel-out ceiling: an unbounded zoom once parked the camera
				// 2.8e13 m out until the idle guard flew it home.
				viewer.scene.screenSpaceCameraController.maximumZoomDistance = MAX_ZOOM_M;
				viewer.camera.percentageChanged = 0.05;
				removeCameraListener = viewer.camera.changed.addEventListener(updateHash);
				removePreUpdate = viewer.scene.preUpdate.addEventListener(orbitTick);

				hudTimer = setInterval(refreshStats, 2000);
				declutterTimer = setInterval(declutterLabels, DECLUTTER_MS);
				// FLIR coherent-noise clock.
				timeTimer = setInterval(() => {
					if (stage?.uniforms) stage.uniforms['time'] = performance.now() / 1000;
				}, 100);

				refreshStats();
				phase = 'ready';
				settle(true);
			} catch (err) {
				if (destroyed) return;
				unavailableReason = `viewer failed: ${err instanceof Error ? err.message : String(err)}`;
				phase = 'unavailable';
				settle(false, unavailableReason);
			}
		})();
	});

	// Selection follow (wall /ops): tracks the selection id (+ readiness and
	// whether its spine geometry exists) — never the 5 s events array — and
	// flies once per selected id. A released subject is not re-flown, here
	// or on the next view's canvas (focus.released), until the selection
	// moves to another event.
	let selectedHasSpine = $derived.by(() => {
		const id = ops.selectedEventId;
		const ev = id ? ops.events.find((e) => e.eventId === id) : undefined;
		return ev ? eventCentroid(ev, godsEye.segmentById) !== null : false;
	});

	$effect(() => {
		const id = ops.selectedEventId;
		const ready = phase === 'ready' && selectedHasSpine;
		untrack(() => {
			// A new selection forgets an older RELEASE: re-selecting that
			// subject later follows it again (a transient null selection
			// while the board reloads does not count as a new one).
			if (id && focus.released && focus.released !== id) focus.released = null;
			if (!ready || !id || !godsEye.followSelection || id === lastFollowedId || id === focus.released) return;
			followSubject(id);
		});
	});

	// City Command home, late: the fused picture answered after the canvas
	// opened on the city centre — settle onto the live action once, unless
	// the operator (or a selection, tour or focus) has the camera already.
	$effect(() => {
		void cityRegistry.version;
		const ready = phase === 'ready';
		untrack(() => {
			if (!actionHomeAt || !ready) return;
			const since = actionHomeAt;
			const untouched = lastInteraction <= since && !selection.current && tourIndex == null && !lockFlight;
			if (!untouched || performance.now() - since > ACTION_VIEW.settleMs) {
				actionHomeAt = 0;
				return;
			}
			const action = actionFrame();
			if (!action) return;
			actionHomeAt = 0;
			flyFrame(action.lon, action.lat, action.range, ACTION_VIEW.headingDeg, actionPitchDeg(), ACTION_VIEW.flightSec);
		});
	});

	// Focus bus: each request made since this canvas mounted flies + locks
	// on landing (or reports why it could not). lastFocusSeq starts at the
	// bus's seq at mount, so the previous view's request is never replayed.
	$effect(() => {
		const seq = focus.seq;
		const current = phase;
		if (seq === 0 || seq === lastFocusSeq || current === 'loading') return;
		lastFocusSeq = seq;
		untrack(() => {
			if (current === 'unavailable') {
				focus.settle(seq, 'no-canvas', unavailableReason);
				return;
			}
			if (focus.eventId) runFocus(seq, focus.eventId);
		});
	});

	function pursuitSignature(plan: InterceptionPlan, officer: boolean): string {
		const s = plan.latestSighting;
		return JSON.stringify([
			plan.eventId,
			plan.vehicleId,
			s?.sightingId,
			s?.t,
			s?.position,
			plan.chokes.map((c) => [c.junctionId, c.rank]),
			officer,
		]);
	}

	// The pursuit overlay rides the interception plan (both wall + full). A
	// re-polled identical plan keeps the drawn overlay (no replayed cinema)
	// and never restarts its own reads (a 6 s re-poll against a slow trace
	// endpoint would otherwise starve the overlay); the request token drops
	// reads that a newer plan (or a clear) superseded, so a stale show() can
	// never land after clear(). Each new plan also re-reads the plan's
	// honesty fields (stale / likely passed / direction) for the overlay and
	// the dossier. A replay owns the canvas while it plays.
	$effect(() => {
		const plan = ops.interception;
		const officer = ops.officerOn;
		const replaying = replay.mode === 'replay';
		if (phase !== 'ready' || !pursuit) return;
		if (!plan?.latestSighting || !plan.vehicleId || replaying) {
			pursuitToken++;
			pursuitShownSig = pursuitPendingSig = '';
			pursuit.clear();
			if (!plan) void godsEye.loadInterception(null);
			publishSnapshot();
			return;
		}
		const sig = pursuitSignature(plan, officer);
		if (sig === pursuitShownSig || sig === pursuitPendingSig) return;
		const token = ++pursuitToken;
		pursuitPendingSig = sig;
		const vehicleId: string = plan.vehicleId;
		void (async () => {
			let trace: Array<[number, number]> = [];
			const [t, detail] = await Promise.all([
				fetchVehicleTrace(vehicleId, 900, AbortSignal.timeout(6000)).catch(() => null),
				godsEye.loadInterception(plan.eventId),
			]);
			// trace endpoint absent — the sighting + chokes still draw
			if (t) trace = t.coordinates;
			if (token !== pursuitToken || !pursuit) return;
			pursuitPendingSig = '';
			pursuit.show({ plan, trace, detail: detail ?? godsEye.interception, segments: godsEye.segmentById, officer });
			pursuitShownSig = sig;
			publishSnapshot();
		})();
	});

	// Fly bus (selection): each request made since this canvas mounted flies
	// to the entity's fused position and reports landed / no-position.
	$effect(() => {
		const seq = selection.flySeq;
		const current = phase;
		const urn = selection.flyUrn;
		if (seq === 0 || seq === lastFlySeq || current === 'loading') return;
		lastFlySeq = seq;
		untrack(() => {
			if (current === 'unavailable') {
				selection.settleFly(seq, 'no-canvas', unavailableReason);
				return;
			}
			if (urn) void flyToUrn(seq, urn);
		});
	});

	// The selection moved: halo, highlight and (City Command) the share hash follow.
	$effect(() => {
		const urn = selection.current;
		const ready = phase === 'ready';
		untrack(() => {
			if (!ready) return;
			placeHalo();
			applySelectionHighlight();
			if (!urn && picked?.urn) picked = null;
			updateHash();
			publishSnapshot();
		});
	});

	// REPLAY: the tape on the clock; live dynamic layers stand down meanwhile.
	$effect(() => {
		const mode = replay.mode;
		const tape = replay.tape;
		const cursor = replay.cursorSec;
		const officer = ops.officerOn;
		if (phase !== 'ready' || !replayOverlay) return;
		untrack(() => {
			if (mode !== 'replay' || !tape) {
				if (replayShownSig) {
					replayOverlay?.clear();
					replayShownSig = '';
				}
				if (replaySuspended.length) resumeLiveLayers();
				publishSnapshot();
				return;
			}
			const sig = `${tape.runId}|${officer}`;
			if (sig !== replayShownSig) {
				suspendLiveLayers();
				replayOverlay?.show(tape, officer);
				replayShownSig = sig;
			}
			replayOverlay?.setTime(cursor);
			publishSnapshot();
		});
	});

	onDestroy(() => {
		destroyed = true;
		stopTour(false);
		// A request this canvas took on but never finished (queued for
		// geometry, mid-flight, or still loading) is answered, so no surface
		// waits on 'locating…' for a canvas that is gone; the next canvas
		// does not replay it.
		lockFlightToken++;
		lockFlight = null;
		pendingFocus = null;
		if (focus.seq > mountedFocusSeq && focus.outcome === 'pending') {
			focus.settle(focus.seq, 'cancelled', 'the canvas closed before the request was served');
		}
		detachFocus?.();
		detachFocus = null;
		if (hudTimer) clearInterval(hudTimer);
		if (clockTimer) clearInterval(clockTimer);
		if (timeTimer) clearInterval(timeTimer);
		if (declutterTimer) clearInterval(declutterTimer);
		declutterTimer = null;
		declutterHidden.clear();
		if (hashTimer) clearTimeout(hashTimer);
		if (ionTimer) clearTimeout(ionTimer);
		if (hoverTimer) clearTimeout(hoverTimer);
		hudTimer = clockTimer = timeTimer = null;
		hashTimer = ionTimer = hoverTimer = null;
		pursuitToken++;
		handler?.destroy();
		handler = null;
		if (container) {
			container.removeEventListener('pointerdown', onPointerDown, { capture: true });
			container.removeEventListener('pointermove', onPointerMove, { capture: true });
			container.removeEventListener('pointerup', onPointerUp, { capture: true });
			container.removeEventListener('wheel', onWheel, { capture: true });
		}
		removeCameraListener?.();
		removeCameraListener = null;
		removePreUpdate?.();
		removePreUpdate = null;
		removeTileError?.();
		removeTileError = null;
		removeTileProgress?.();
		removeTileProgress = null;
		for (const off of removeIonListeners) off();
		removeIonListeners = [];
		hovered = null;
		selectedHighlights = [];
		haloAt = null;
		haloEntity = null;
		haloDrawnAt = null;
		clearAgentHighlights();
		for (const timer of agentNotes.values()) clearTimeout(timer);
		agentNotes.clear();
		pursuit?.destroy();
		pursuit = null;
		replayOverlay?.destroy();
		replayOverlay = null;
		for (const inst of instances.values()) inst.destroy();
		instances = new Map();
		try {
			viewer?.destroy();
		} catch {
			// viewer already torn down by navigation
		}
		viewer = null;
		if (godsEye.subjectLockId) godsEye.subjectLockId = null;
		godsEye.stop();
		ops.stop();
	});

	/** Compact age: 42s · 4m07s · 3h12m · 2d05h. */
	function fmtAge(sec: number): string {
		if (sec < 120) return `${sec}s`;
		if (sec < 7200) return `${Math.floor(sec / 60)}m${String(sec % 60).padStart(2, '0')}s`;
		if (sec < 172_800) return `${Math.floor(sec / 3600)}h${String(Math.floor((sec % 3600) / 60)).padStart(2, '0')}m`;
		return `${Math.floor(sec / 86_400)}d${String(Math.floor((sec % 86_400) / 3600)).padStart(2, '0')}h`;
	}

	// AMA chip (R9): band + age of the newest band READING (now −
	// ops.bandUpdated: the event's newest situation_log row, or the city
	// band's latest engine evaluation — every rung reports the same 'newest
	// reading', never the band's entry time) on a 1 s clock; the source label
	// prints as the store reports it. STALE keys on that reading too, so a
	// band that has HELD for hours but is still being read is never STALE —
	// only one whose engine stopped reading it (a failing city-band poll
	// already reads UNKNOWN in the store).
	let amaBand = $derived(ops.band ?? '—');
	let amaAgeSec = $derived(ops.bandUpdated ? Math.max(0, Math.floor((nowMs - ops.bandUpdated) / 1000)) : null);
	let amaStale = $derived(amaAgeSec != null && amaAgeSec > AMA_STALE_SEC);
	let feedWarnings = $derived(GODS_EYE_LAYERS.filter((l) => !!stats[l.id]?.error));
	/** The layer's own pick card: wall views always; City Command only for picks with no entity URN. */
	let showPickCard = $derived(pickCard || (picked != null && !picked.urn));
	let replaying = $derived(replay.mode === 'replay');
	/** The locked subject's name as this console may show it (C6: masked unless an officer is named). */
	let lockedEventName = $derived(godsEye.subjectLockId ? eventLabel(godsEye.subjectLockId, ops.officerOn) : null);
	// Chip text only (basemap.ts imageryChip, EN/HI/GU) — the snapshot's
	// imageryMode stays 'keyless-osm' / 'ion-photoreal' / 'offline-schematic'
	// (tests and share links read it). Once the photoreal tiles cover the
	// view the chip names Google's 3D city, whatever the imagery under it did.
	let photoreal = $derived(stats['photoreal-3d'] ?? null);
	let photorealOn = $derived(photorealReady(photoreal));
	let modeLabel = $derived(
		t(ops.lang, imageryChip({ offline: offlineDetected, basemapId, fallbackNote: basemapNote, photoreal })),
	);
	let modeTitle = $derived(
		photorealOn
			? `Google Photorealistic 3D Tiles via Cesium ion — every overlay draped on the 3D city${basemapNote ? ` · ${basemapNote}` : ''}`
			: (basemapNote ?? 'Basemap mode (dark re-grade applied to the keyless raster)'),
	);
	/** The data credits ride the clear region's bottom-left corner (City Command's rail covers the canvas's own). */
	let creditStyle = $derived(
		clearInsets
			? `--ge-credit-left:${Math.max(0, Math.round(clearInsets.left))}px;--ge-credit-bottom:${Math.max(0, Math.round(clearInsets.bottom))}px`
			: undefined,
	);
	/** AI suggestions waiting on a human (the chips) — the bus's queue. */
	let agentPending = $derived(canvasBus.pending);
</script>

<svelte:window onkeydown={onKey} />

<div class="ge-wrap" class:mini={hud === 'mini'} data-hud={hud}>
	{#if phase === 'loading'}
		<div class="ge-status"><span class="pulse"></span> initialising God's-eye canvas…</div>
	{/if}

	{#if phase === 'unavailable'}
		<div class="ge-degrade">
			<h3>3D God's-eye canvas unavailable</h3>
			<p>{unavailableReason}</p>
			<p class="dim">
				This panel degrades alone — the board keeps working on the situation wall at
				<a href="/ops">/ops</a>. A WebGL-capable browser (or the served build with
				static/cesium) renders the Cesium canvas.
			</p>
		</div>
	{/if}

	<div class="ge-canvas" bind:this={container} style={creditStyle}></div>

	{#if phase === 'ready'}
		<div class="ge-hud" class:mini={hud === 'mini'}>
			{#if hud === 'full' || hud === 'wall'}
				<!-- one wrapping chip row: controls · layers · AMA · basemap · legend -->
				<div class="hud-row" bind:this={hudRowEl}>
					{#if hud === 'full' && onexit}
						<button class="chip exit" onclick={onexit} title="Leave the cinematic view — back to the situation wall (Esc)">
							⤡ EXIT
						</button>
					{/if}
					<button
						class="chip sensor"
						class:flir={sensorMode !== 'raw'}
						onclick={cycleSensor}
						title="Sensor: COMMAND (true colours, default) → FLIR White-Hot → Black-Hot → Ironbow"
					>
						▙ {SENSOR_LABEL[sensorMode]}
					</button>
					<button class="chip share" onclick={share} title="Copy this exact view for a Jan Rakshak unit / police control room">
						⇪ SHARE
					</button>
					<button
						class="chip tour"
						onclick={() => (tourIndex == null ? startTour() : stopTour())}
						title="Guided tour over the live city — empty layers are skipped (Esc stops)"
					>
						{tourIndex == null ? '▶ TOUR' : `■ ${tourIndex + 1}/${tourTotal}`}
					</button>
					{#if godsEye.subjectLockId}
						<button
							class="chip lock"
							onclick={() => releaseSubject()}
							title={`camera held on ${lockedEventName ?? godsEye.subjectLockId} — click to release`}
						>
							⊕ LOCKED · RELEASE
						</button>
					{/if}
					{#each GODS_EYE_LAYERS as layer (layer.id)}
						<button
							class="chip layer"
							class:off={!activeLayers.has(layer.id)}
							class:paused={replaying && replaySuspended.includes(layer.id)}
							class:warn={!!stats[layer.id]?.error}
							title={replaying && replaySuspended.includes(layer.id)
								? `${layer.name} — paused while the replay owns the canvas`
								: (stats[layer.id]?.hint ?? layer.name)}
							data-layer={layer.id}
							onclick={() => toggleLayer(layer.id)}
						>
							{layer.label}
							<span class="n">{stats[layer.id]?.count ?? 0}</span>
							{#if (stats[layer.id]?.detail?.simulated ?? 0) > 0}
								<!-- provenance, never 'live': the integrated ingest feed is synthetic -->
								<span class="sim" data-testid="chip-simulated" title={t(ops.lang, 'cc_simulated_feed')}>SIM</span>
							{/if}
						</button>
					{/each}
					{#if replaying}
						<span class="chip replay" data-testid="replay-pill" title="A recorded emulator tape is playing on the canvas clock — every replayed primitive is EMULATED">
							◐ REPLAY{replay.runId != null ? ` · run ${replay.runId}` : ''} · EMULATED
						</span>
						<span class="chip live-paused" data-testid="live-paused" title={t(ops.lang, 'canvas_live_paused_hint')}>
							❚❚ {t(ops.lang, 'canvas_live_paused')}
						</span>
					{/if}
					<span
						class="chip ama"
						class:stale={amaStale}
						data-age-sec={amaAgeSec ?? ''}
						title={t(ops.lang, 'canvas_ama_title')}
					>
						AMA {amaBand}
						{#if amaAgeSec != null}<span class="n">·{fmtAge(amaAgeSec)}</span>{/if}
						<span class="n dim">[{ops.bandSource}]</span>
						{#if amaStale}<span class="stale-tag">STALE</span>{/if}
					</span>
					<span class="chip mode" class:photoreal={photorealOn} title={modeTitle}>
						{modeLabel}
					</span>
					{#if pursuitCount > 0}
						<span class="chip pursuit" title="pursuit overlay — trace + ranked chokes + future arc">
							PURSUIT <span class="n">{pursuitCount}</span>
						</span>
					{/if}
					<button
						class="chip legend"
						class:warn={feedWarnings.length > 0}
						onclick={() => (legendOpen = !legendOpen)}
						aria-expanded={legendOpen}
						title="Legend — layer feeds, colour key and credits"
					>
						ⓘ © OSM{#if feedWarnings.length}<span class="warn-n">⚠ {feedWarnings.length}</span>{/if}
					</button>
					{#if shareFlash}<span class="flash">{displayText(shareFlash, ops.officerOn)}</span>{/if}
				</div>
				{#if tourIndex != null && tourLabel}
					<span class="tour-label">{displayText(tourLabel, ops.officerOn)}</span>
				{/if}
				{#if agentPending.length}
					<div class="agent-chips" data-testid="agent-suggestions" aria-live="polite">
						{#each agentPending.slice(0, AGENT_CHIPS_SHOWN) as d (d.id)}
							<div class="agent-chip" data-testid="agent-chip" data-directive-id={d.id} data-verb={d.verb} title={t(ops.lang, 'agent_proposal')}>
								<span class="agent-kicker">✦ {t(ops.lang, 'agent_suggests')}</span>
								<span class="agent-text">{chipText(d)}</span>
								{#if d.tier}<span class="agent-tier" title={t(ops.lang, 'agent_from', d.tier)}>{d.tier}</span>{/if}
								<button class="agent-apply" data-testid="agent-apply" onclick={() => applyDirective(d)}>{t(ops.lang, 'agent_apply')}</button>
								<button class="agent-dismiss" data-testid="agent-dismiss" onclick={() => dismissDirective(d)}>{t(ops.lang, 'agent_dismiss')}</button>
							</div>
						{/each}
						{#if agentPending.length > AGENT_CHIPS_SHOWN}
							<span class="agent-more">{t(ops.lang, 'agent_more', agentPending.length - AGENT_CHIPS_SHOWN)}</span>
						{/if}
					</div>
				{/if}
				{#if legendOpen}
					<div class="ge-legend" role="dialog" aria-label="God's-eye legend">
						<ul class="lg-layers">
							{#each GODS_EYE_LAYERS as layer (layer.id)}
								<li>
									<b>{layer.label}</b>
									<span class="n">{stats[layer.id]?.count ?? 0}</span>
									<span class="lg-hint">{stats[layer.id]?.hint ?? layer.name}</span>
									{#if stats[layer.id]?.error}<span class="feed-warn">{stats[layer.id].error}</span>{/if}
								</li>
							{/each}
						</ul>
						<p class="lg-key">
							<i class="sw sw-data"></i> planned · <i class="sw sw-act"></i> active / flagged / pending a human ·
							<i class="sw sw-dim"></i> closed · glow = RECOMMENDED corridor · dashes = alternates ·
							·PREVIEW = offline fixture · ·EMULATED = emulator source
						</p>
						<p class="lg-key">
							flow (draped on the ground): <i class="sw sw-green"></i> free → <i class="sw sw-red"></i> severe, hairline = NO DATA,
							dashed = schematic chord (not the road's true shape) ·
							signals: arc = green share, <i class="sw sw-amber"></i> amber, <i class="sw sw-red"></i> all-red ·
							₹ pins = e-challan recommendations (a named officer decides) · eye pins = stolen-vehicle sightings (evidence, never a fine) ·
							plates masked until a duty officer is named
						</p>
						{#if basemapNote}<p class="lg-note">{basemapNote}</p>{/if}
						<p class="credit">
							{OSM_CREDIT} · canvas patterns from
							<a href="https://github.com/bilawalsidhu/gods-eye-view" target="_blank" rel="noreferrer">gods-eye-view (MIT)</a>
						</p>
					</div>
				{/if}
				{#if picked && showPickCard}
					<div class="ge-pick" data-pick-layer={picked.layer} role="status">
						<header>
							<span class="pick-layer">{picked.label}</span>
							{#if picked.eventId && ops.selectedEventId === picked.eventId}<span class="pick-sel">SELECTED</span>{/if}
							<button class="pick-x" onclick={() => (picked = null)} aria-label="dismiss pick card">✕</button>
						</header>
						<p class="pick-text">{displayText(picked.text, ops.officerOn)}</p>
						{#if picked.eventId}
							<div class="pick-actions">
								<button class="chip lock" onclick={() => focus.request(picked!.eventId!)} title="fly to this event and hold the camera on it">
									⊕ FOCUS
								</button>
							</div>
						{/if}
					</div>
				{/if}
			{:else}
				<span class="chip mode" title={modeTitle}>
					{photorealOn ? '3D · GOOGLE' : offlineDetected ? 'OFFLINE SCHEMATIC' : basemapId === 'ion-photoreal' ? 'ION' : 'OSM·DARK'}
				</span>
				<span
					class="chip ama"
					class:stale={amaStale}
					style={`border-color:${ops.band ? (cssVar(`--v-band-${ops.band.toLowerCase()}`) ?? 'var(--v-line-2)') : 'var(--v-line-2)'}`}
				>
					AMA {amaBand}{amaAgeSec != null ? ` ·${fmtAge(amaAgeSec)}` : ''}
				</span>
				<a class="chip layer" href="/gods-eye">FULLSCREEN ⤢</a>
			{/if}
		</div>
		{#if hud === 'full' || hud === 'wall'}
			<CommandPalette
				layerIds={GODS_EYE_LAYER_IDS}
				world={directiveWorld}
				onfly={paletteFly}
				onhighlight={highlightUrns}
				onlayer={(id, on) => setLayerEnabled(id, on)}
				onsensor={(mode) => {
					setSensor(mode);
					updateHash();
				}}
			/>
		{/if}
	{/if}
</div>

<style>
	.ge-wrap {
		position: relative;
		width: 100%;
		height: 100%;
		min-height: 240px;
		background: var(--v-canvas);
		overflow: hidden;
		border-radius: var(--v-radius-panel);
	}

	.ge-canvas {
		position: absolute;
		inset: 0;
	}

	.ge-canvas :global(.cesium-viewer) {
		width: 100%;
		height: 100%;
	}

	/*
	 * The data credits (Google, Cesium ion, OSM — Google's terms want them
	 * legible) on a glass plate, lifted into the corner the host's panels
	 * leave clear (--ge-credit-left / --ge-credit-bottom from clearInsets:
	 * City Command's fusion rail covers the canvas's own bottom-left).
	 * A transform, because Cesium writes the bar's left/bottom inline on
	 * every resize. A host that floats an instrument along the bottom edge
	 * (the cockpit's tool dock) caps the bar's width (--ge-credit-max-w):
	 * the credits wrap onto another line instead of running under it.
	 */
	.ge-canvas :global(.cesium-viewer-bottom) {
		transform: translate(var(--ge-credit-left, 0px), calc(-1 * var(--ge-credit-bottom, 0px)));
		width: fit-content;
		max-width: var(--ge-credit-max-w, none);
		padding: 2px 6px;
		border-radius: var(--v-radius-chip);
		background: var(--v-glass-bg);
	}

	.ge-status {
		position: absolute;
		inset: 0;
		display: flex;
		align-items: center;
		justify-content: center;
		gap: var(--v-space-3);
		color: var(--v-ink-2);
		font-family: var(--v-font-mono);
		font-size: var(--v-text-xs);
		z-index: 3;
	}

	.pulse {
		width: 8px;
		height: 8px;
		border-radius: var(--v-radius-pill);
		background: var(--v-data);
		animation: gepulse 1.2s ease-in-out infinite;
	}

	@keyframes gepulse {
		50% {
			opacity: 0.2;
		}
	}

	.ge-degrade {
		position: absolute;
		inset: 0;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: var(--v-space-2);
		padding: var(--v-space-5);
		text-align: center;
		color: var(--v-ink-2);
		background: var(--v-surface-sink);
		z-index: 3;
	}

	.ge-degrade h3 {
		margin: 0;
		font-size: var(--v-text-base);
		letter-spacing: 0.04em;
		color: var(--v-ink-1);
	}

	.ge-degrade p {
		margin: 0;
		font-size: var(--v-text-xs);
		max-width: 46ch;
	}

	.ge-degrade .dim {
		color: var(--v-ink-3);
	}

	.ge-degrade a {
		color: var(--v-data);
	}

	/* The HUD column. Hosts that float chrome over the canvas (the cockpit's
	 * rails and stats pill) inset it with --ge-hud-left/right/top. */
	.ge-hud {
		position: absolute;
		left: var(--ge-hud-left, var(--v-space-3));
		right: var(--ge-hud-right, var(--v-space-3));
		top: var(--ge-hud-top, var(--v-space-3));
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: var(--v-space-1);
		z-index: 4;
		pointer-events: none;
	}

	.ge-hud .hud-row,
	.ge-hud .chip,
	.ge-hud .ge-pick,
	.ge-hud .ge-legend,
	.ge-hud .flash,
	.ge-hud .tour-label {
		pointer-events: auto;
	}

	.hud-row {
		display: flex;
		flex-wrap: wrap;
		gap: var(--v-space-1);
		align-items: center;
	}

	.chip {
		font-family: var(--v-font-mono);
		font-size: var(--v-text-2xs);
		letter-spacing: 0.05em;
		color: var(--v-ink-1);
		background: color-mix(in srgb, var(--v-canvas) 82%, transparent);
		border: 1px solid var(--v-line-2);
		border-radius: var(--v-radius-chip);
		padding: 2px var(--v-space-2);
		cursor: default;
		text-decoration: none;
		display: inline-flex;
		align-items: center;
		gap: var(--v-space-1);
		white-space: nowrap;
	}

	button.chip {
		cursor: pointer;
	}

	button.chip:hover {
		border-color: var(--v-line-select);
	}

	button.chip:focus-visible {
		outline: 1px solid var(--v-act-focus);
		outline-offset: 1px;
	}

	.chip.exit {
		border-color: var(--v-ink-2);
		color: var(--v-ink-1);
	}

	.chip.sensor {
		border-color: var(--v-data);
		color: var(--v-data);
	}

	.chip.sensor.flir {
		border-color: var(--v-band-amber);
		color: var(--v-band-amber);
	}

	.chip.share {
		border-color: var(--v-band-green);
		color: var(--v-band-green);
	}

	.chip.tour {
		border-color: var(--v-act);
		color: var(--v-act-text);
	}

	.chip.lock {
		border-color: var(--v-data);
		color: var(--v-data);
	}

	.chip.pursuit {
		border-color: var(--v-act);
		color: var(--v-act-text);
	}

	.chip.mode.photoreal {
		border-color: var(--v-data);
		color: var(--v-data);
	}

	.chip.layer.off {
		opacity: 0.45;
	}

	.chip.layer.paused {
		opacity: 0.35;
		border-style: dotted;
	}

	.chip.replay {
		border-color: var(--v-tool);
		color: var(--v-tool);
		border-style: dashed;
		letter-spacing: 0.08em;
	}

	.chip.layer.warn {
		border-style: dashed;
	}

	.chip .n {
		color: var(--v-data);
	}

	/* SIMULATED FEED provenance tag (synthetic ingest rows) — the tool accent, dashed, never a band colour. */
	.chip .sim {
		margin-left: 2px;
		padding: 0 4px;
		font-size: 9px;
		letter-spacing: 0.08em;
		color: var(--v-tool);
		border: 1px dashed var(--v-tool);
		border-radius: var(--v-radius-pill);
	}

	.chip .dim {
		color: var(--v-ink-3);
	}

	.chip.ama {
		border-color: var(--v-data);
		color: var(--v-data);
	}

	.chip.ama.stale {
		border-style: dashed;
		border-color: var(--v-line-2);
		color: var(--v-ink-3);
	}

	.chip.ama.stale .n {
		color: var(--v-ink-3);
	}

	.stale-tag {
		color: var(--v-band-amber);
		letter-spacing: 0.08em;
	}

	.chip.legend.warn {
		border-color: var(--v-band-red);
	}

	.warn-n {
		color: var(--v-band-red);
		margin-left: var(--v-space-1);
	}

	.tour-label {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-2);
		background: color-mix(in srgb, var(--v-canvas) 82%, transparent);
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-chip);
		padding: 2px var(--v-space-2);
		max-width: min(64ch, 100%);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.flash {
		font-family: var(--v-font-mono);
		font-size: var(--v-text-2xs);
		color: var(--v-band-green);
		background: color-mix(in srgb, var(--v-canvas) 85%, transparent);
		border-radius: var(--v-radius-chip);
		padding: 2px var(--v-space-2);
		max-width: 48ch;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.ge-legend,
	.ge-pick {
		font-family: var(--v-font-mono);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-2);
		background: color-mix(in srgb, var(--v-canvas) 90%, transparent);
		border: 1px solid var(--v-line-2);
		border-radius: var(--v-radius-chip);
		padding: var(--v-space-2);
		max-width: min(58ch, 100%);
	}

	.lg-layers {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.lg-layers b {
		color: var(--v-ink-1);
		font-weight: 600;
	}

	.lg-layers .n {
		color: var(--v-data);
		margin: 0 var(--v-space-1);
	}

	.lg-hint {
		color: var(--v-ink-3);
	}

	.lg-key,
	.lg-note,
	.credit {
		margin: var(--v-space-2) 0 0;
	}

	.lg-note {
		color: var(--v-band-amber);
	}

	.sw {
		display: inline-block;
		width: 10px;
		height: 3px;
		vertical-align: middle;
		border-radius: 1px;
	}

	.sw-data {
		background: var(--v-data);
	}

	.sw-act {
		background: var(--v-act);
	}

	.sw-dim {
		background: var(--v-ink-4);
	}

	.sw-green {
		background: var(--v-band-green);
	}

	.sw-amber {
		background: var(--v-band-amber);
	}

	.sw-red {
		background: var(--v-band-red);
	}

	.credit {
		color: var(--v-ink-3);
	}

	.credit a {
		color: var(--v-ink-2);
	}

	.feed-warn {
		display: block;
		font-family: var(--v-font-mono);
		font-size: var(--v-text-2xs);
		color: var(--v-band-red);
	}

	.ge-pick {
		border-color: var(--v-data);
	}

	.ge-pick header {
		display: flex;
		align-items: center;
		gap: var(--v-space-2);
		margin-bottom: var(--v-space-1);
	}

	.pick-layer {
		color: var(--v-data);
		letter-spacing: 0.08em;
	}

	.pick-sel {
		color: var(--v-act-text);
		letter-spacing: 0.08em;
	}

	.pick-x {
		margin-left: auto;
		background: transparent;
		border: none;
		color: var(--v-ink-3);
		cursor: pointer;
		font: inherit;
	}

	.pick-x:hover {
		color: var(--v-ink-1);
	}

	.pick-text {
		margin: 0;
		color: var(--v-ink-1);
		white-space: pre-line;
		line-height: 1.45;
	}

	.pick-actions {
		margin-top: var(--v-space-2);
		display: flex;
		gap: var(--v-space-1);
	}

	.ge-hud.mini {
		flex-direction: row;
		gap: var(--v-space-1);
		align-items: flex-start;
	}

	/* 'LIVE paused' — the live layers stand down while a replay owns the canvas */
	.chip.live-paused {
		border-color: var(--v-band-amber);
		color: var(--v-band-amber);
		border-style: dashed;
	}

	/* AI suggestions: the machine proposes (tool accent), a human presses Apply */
	.agent-chips {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: var(--v-space-1);
		pointer-events: auto;
		max-width: min(64ch, 100%);
	}

	.agent-chip {
		display: flex;
		align-items: center;
		gap: var(--v-space-2);
		padding: 3px var(--v-space-2);
		font-family: var(--v-font-mono);
		font-size: var(--v-text-xs);
		color: var(--v-ink-1);
		background: color-mix(in srgb, var(--v-canvas) 88%, transparent);
		border: 1px solid var(--v-tool);
		border-left: 3px solid var(--v-tool);
		border-radius: var(--v-radius-chip);
		box-shadow: 0 0 18px color-mix(in srgb, var(--v-tool) 25%, transparent);
		max-width: 100%;
	}

	.agent-kicker {
		color: var(--v-tool);
		letter-spacing: 0.08em;
		white-space: nowrap;
	}

	.agent-text {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		min-width: 0;
	}

	.agent-tier {
		font-size: 9px;
		letter-spacing: 0.08em;
		color: var(--v-ink-3);
		border: 1px dashed var(--v-line-2);
		border-radius: var(--v-radius-pill);
		padding: 0 5px;
		white-space: nowrap;
	}

	.agent-chip button {
		font: inherit;
		font-size: var(--v-text-2xs);
		padding: 1px var(--v-space-2);
		border-radius: var(--v-radius-btn);
		cursor: pointer;
		white-space: nowrap;
	}

	.agent-apply {
		color: var(--v-act-text);
		background: var(--v-act-veil);
		border: 1px solid var(--v-act);
	}

	.agent-apply:hover {
		background: color-mix(in srgb, var(--v-act) 30%, transparent);
	}

	.agent-dismiss {
		color: var(--v-ink-2);
		background: transparent;
		border: 1px solid var(--v-line-2);
	}

	.agent-dismiss:hover {
		color: var(--v-ink-1);
		border-color: var(--v-line-select);
	}

	.agent-chip button:focus-visible {
		outline: 1px solid var(--v-act-focus);
		outline-offset: 1px;
	}

	.agent-more {
		font-family: var(--v-font-mono);
		font-size: var(--v-text-2xs);
		color: var(--v-ink-3);
	}

	@media (prefers-reduced-motion: reduce) {
		.pulse {
			animation: none;
			opacity: 0.5;
		}
	}
</style>
