<script lang="ts">
	/**
	 * GodsEyeCanvas — the Cesium God's-eye canvas.
	 *
	 * Keyless by default (re-graded dark OSM raster + ellipsoid terrain + our
	 * own entities — zero calls to cesium.com/ion); env-keyed ion upgrade via
	 * the server-side mode endpoint. Sensor shader (FLIR White-Hot default,
	 * Black-Hot toggle), a share-link camera URL, and the layer registry
	 * (gods-eye-view MIT pattern — see registry.ts).
	 *
	 * HUD modes: 'full' (cinematic /gods-eye) · 'mini' (compact chip row) ·
	 * 'wall' (the /ops situation wall): slow AUTO-ORBIT that pauses 30 s on
	 * interaction, SUBJECT FOLLOW (the wall locks the camera to the selected
	 * event's spine until released), the shared PURSUIT OVERLAY (head-first
	 * trace + comet + choke cascade + marching future arc — pursuit.ts), and
	 * the GUIDED TOUR (camera + sensor choreography for the demo).
	 *
	 * Degradation contract: if WebGL or the prebuilt bundle is unavailable
	 * this panel degrades ALONE. If OSM tiles cannot load, the canvas switches
	 * to OFFLINE SCHEMATIC MODE (entities over a dark globe, labelled).
	 */
	import { onMount, onDestroy } from 'svelte';
	import { ops } from '$lib/ops/store.svelte';
	import { CITY_BBOX } from '$lib/ops/endpoints';
	import { godsEye } from './store.svelte';
	import { GODS_EYE_LAYERS, GODS_EYE_LAYER_IDS } from './layers';
	import { createIonBasemap, createKeylessBasemap } from './layers/basemap';
	import { createPursuitOverlay, type PursuitOverlay } from './pursuit';
	import { fetchVehicleTrace } from './data';
	import { FLIR_SHADER } from './shaders/flir';
	import { buildShareUrl, parseShareHash, type ShareViewState } from './share-link';
	import { cssVar } from './theme';
	import type { GodsEyeLayerInstance, LayerCoverageStats } from './registry';

	let { hud = 'full' }: { hud?: 'full' | 'mini' | 'wall' } = $props();

	let container: HTMLDivElement | null = $state(null);

	let phase = $state<'loading' | 'ready' | 'unavailable'>('loading');
	let unavailableReason = $state<string | null>(null);
	let offlineDetected = $state(false);
	let sensorMode = $state<'flir-whot' | 'flir-bhot' | 'raw'>('flir-whot');
	let activeLayers = $state<Set<string>>(new Set(GODS_EYE_LAYERS.map((l) => l.id)));
	let stats = $state<Record<string, LayerCoverageStats>>({});
	let pickedInfo = $state<string | null>(null);
	let shareFlash = $state<string | null>(null);
	let tourIndex = $state<number | null>(null);
	let tourLabel = $state<string>('');
	/** Reactive mirror of the pursuit overlay's entity count (HUD chip). */
	let pursuitCount = $state(0);

	// controller state (non-reactive)
	let viewer: CesiumViewer | null = null;
	let instances = new Map<string, GodsEyeLayerInstance>();
	let pursuit: PursuitOverlay | null = null;
	let stage: { uniforms?: Record<string, number> } | null = null;
	let hashTimer: ReturnType<typeof setTimeout> | null = null;
	let hudTimer: ReturnType<typeof setInterval> | null = null;
	let timeTimer: ReturnType<typeof setInterval> | null = null;
	let orbitTimer: ReturnType<typeof setInterval> | null = null;
	let tourTimer: ReturnType<typeof setTimeout> | null = null;
	let handler: { destroy(): void } | null = null;
	let removeCameraListener: (() => void) | null = null;
	let removeTileError: (() => void) | null = null;
	let removeTileProgress: (() => void) | null = null;
	let tileErrors = 0;
	let lastInteraction = 0;

	const CAMERA_HOME = {
		lon: (CITY_BBOX.minLon + CITY_BBOX.maxLon) / 2,
		lat: (CITY_BBOX.minLat + CITY_BBOX.maxLat) / 2,
	};

	/** Orbit resumes 30 s after the last interaction (the wall's idle beat). */
	const ORBIT_RESUME_MS = 30_000;

	/** The guided tour — camera + sensor choreography over the live city. */
	const TOUR: Array<{ label: string; lon: number; lat: number; alt: number; heading: number; pitch: number; sensor?: 'flir-whot' | 'flir-bhot' | 'raw' }> = [
		{ label: 'the city at AMBER — posture in one view', ...CAMERA_HOME, alt: 3200, heading: 0, pitch: -42 },
		{ label: 'corridor spines — the events layer', lon: 72.585, lat: 23.022, alt: 1800, heading: 20, pitch: -38 },
		{ label: 'signal network — the gated junctions', lon: 72.578, lat: 23.025, alt: 1200, heading: 90, pitch: -35, sensor: 'flir-bhot' },
		{ label: 'watchlist traces — earmarked plates', lon: 72.5846, lat: 23.0213, alt: 900, heading: 78, pitch: -40 },
		{ label: 'units + choke plan — humans on the ground', lon: 72.5798, lat: 23.0254, alt: 800, heading: 140, pitch: -38, sensor: 'flir-whot' },
	];

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
			return !!(c.getContext('webgl2') ?? c.getContext('webgl'));
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
		window.__GODS_EYE__ = {
			phase: 'loading',
			imageryMode: 'keyless-osm',
			tilesLoaded: false,
			entityCount: 0,
			distinctLayerIds: [],
			offlineDetected: false,
			sensorMode: 'flir-whot',
			pursuitEntities: 0,
			ready,
		};
		return (ok: boolean, reason?: string) => {
			window.__GODS_EYE__!.phase = ok ? 'ready' : 'unavailable';
			resolveReady({ ok, reason });
		};
	}

	function publishSnapshot() {
		if (!window.__GODS_EYE__ || !viewer) return;
		window.__GODS_EYE__.imageryMode = offlineDetected
			? 'offline-schematic'
			: godsEye.imageryMode === 'ion-photoreal'
				? 'ion-photoreal'
				: 'keyless-osm';
		window.__GODS_EYE__.tilesLoaded = viewer.scene.globe.tilesLoaded;
		window.__GODS_EYE__.entityCount = viewer.entities.values.length;
		window.__GODS_EYE__.distinctLayerIds = [...instances.keys()].filter((id) =>
			activeLayers.has(id),
		);
		window.__GODS_EYE__.offlineDetected = offlineDetected;
		window.__GODS_EYE__.sensorMode = sensorMode;
		window.__GODS_EYE__.pursuitEntities = pursuit?.count() ?? 0;
		pursuitCount = window.__GODS_EYE__.pursuitEntities;
	}

	function refreshStats() {
		const next: Record<string, LayerCoverageStats> = {};
		for (const [id, inst] of instances) next[id] = inst.coverageStats();
		stats = next;
		publishSnapshot();
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
			layers: [...activeLayers],
		};
	}

	function radToDeg(rad: number): number {
		const C = window.Cesium;
		if (C?.Math?.toDegrees) return C.Math.toDegrees(rad);
		return (rad * 180) / Math.PI;
	}

	function updateHash() {
		if (!viewer) return;
		if (hashTimer) clearTimeout(hashTimer);
		hashTimer = setTimeout(() => {
			try {
				const hash = `#${buildShareUrl(currentViewState(), GODS_EYE_LAYER_IDS).split('#')[1] ?? ''}`;
				history.replaceState(null, '', hash);
			} catch {
				// history unavailable (sandboxed iframe) — share button still works
			}
		}, 800);
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
				palette: FLIR_SHADER.uniforms.palette.default,
				time: 0,
			},
		});
		v.scene.postProcessStages.add(stage);
	}

	function setLayerEnabled(id: string, on: boolean) {
		const inst = instances.get(id);
		if (!inst) return;
		if (on) inst.activate();
		else inst.deactivate();
		activeLayers = new Set(on ? [...activeLayers, id] : [...activeLayers].filter((x) => x !== id));
		refreshStats();
	}

	function toggleLayer(id: string) {
		setLayerEnabled(id, !activeLayers.has(id));
	}

	function cycleSensor() {
		sensorMode = sensorMode === 'flir-whot' ? 'flir-bhot' : sensorMode === 'flir-bhot' ? 'raw' : 'flir-whot';
		if (viewer && window.Cesium) applySensorStage(window.Cesium, viewer);
		publishSnapshot();
	}

	async function share() {
		if (!viewer) return;
		try {
			const url = buildShareUrl(currentViewState(), GODS_EYE_LAYER_IDS);
			await navigator.clipboard.writeText(url);
			shareFlash = 'view URL copied';
		} catch {
			shareFlash = buildShareUrl(currentViewState(), GODS_EYE_LAYER_IDS).slice(0, 160) + '…';
		}
		setTimeout(() => (shareFlash = null), 4000);
	}

	function restoreFromHash(cesium: CesiumGlobal, v: CesiumViewer) {
		const parsed = parseShareHash(window.location.hash, GODS_EYE_LAYER_IDS);
		if (!parsed) {
			v.camera.setView({
				destination: cesium.Cartesian3.fromDegrees(CAMERA_HOME.lon, CAMERA_HOME.lat, 2600),
				orientation: {
					heading: cesium.Math.toRadians(0),
					pitch: cesium.Math.toRadians(-35),
					roll: 0,
				},
			});
			return;
		}
		sensorMode = parsed.style === 'raw' ? 'raw' : parsed.bhot ? 'flir-bhot' : 'flir-whot';
		applySensorStage(cesium, v);
		activeLayers = new Set(parsed.layers);
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

	// ─── wall mode: auto-orbit, subject follow, guided tour ────────────────────

	function markInteraction() {
		lastInteraction = performance.now();
	}

	function startOrbit() {
		if (orbitTimer || !viewer || window.Cesium == null) return;
		const cesium = window.Cesium;
		orbitTimer = setInterval(() => {
			if (!viewer || tourIndex != null || godsEye.subjectLockId) return;
			if (document.hidden) return;
			if (performance.now() - lastInteraction < ORBIT_RESUME_MS) return;
			try {
				viewer.camera.rotateRight(cesium.Math.toRadians(0.035));
			} catch {
				// camera mid-teardown — next tick retries
			}
		}, 33);
	}

	function stopOrbit() {
		if (orbitTimer) clearInterval(orbitTimer);
		orbitTimer = null;
	}

	/** Fly to the selected event's spine centroid and hold (subject lock). */
	function followSubject(eventId: string) {
		if (!viewer || !window.Cesium) return;
		const ev = ops.events.find((e) => e.eventId === eventId);
		if (!ev || ev.source === 'offline-fixture') return;
		const segs = ev.routeSegmentIds
			.map((id) => godsEye.segmentById.get(id))
			.filter((s): s is NonNullable<typeof s> => Boolean(s))
			.flatMap((s) => s.coordinates);
		if (!segs.length) return;
		let lon = 0;
		let lat = 0;
		for (const [x, y] of segs) {
			lon += x;
			lat += y;
		}
		lon /= segs.length;
		lat /= segs.length;
		const cesium = window.Cesium;
		godsEye.subjectLockId = eventId;
		viewer.camera.flyTo({
			destination: cesium.Cartesian3.fromDegrees(lon, lat, 1700),
			orientation: { heading: cesium.Math.toRadians(0), pitch: cesium.Math.toRadians(-38), roll: 0 },
			duration: 2,
		});
	}

	function releaseSubject() {
		godsEye.subjectLockId = null;
		lastInteraction = performance.now(); // orbit stays paused 30 s after release
	}

	function tourStep(i: number) {
		const cesium = window.Cesium;
		if (!viewer || !cesium || tourIndex == null) return;
		const step = TOUR[i];
		tourLabel = step.label;
		if (step.sensor && step.sensor !== sensorMode) {
			sensorMode = step.sensor;
			applySensorStage(cesium, viewer);
			publishSnapshot();
		}
		viewer.camera.flyTo({
			destination: cesium.Cartesian3.fromDegrees(step.lon, step.lat, step.alt),
			orientation: { heading: cesium.Math.toRadians(step.heading), pitch: cesium.Math.toRadians(step.pitch), roll: 0 },
			duration: 2,
		});
		tourTimer = setTimeout(() => {
			if (tourIndex == null) return;
			const next = (tourIndex + 1) % TOUR.length;
			tourIndex = next;
			tourStep(next);
		}, 5500);
	}

	function startTour() {
		if (!viewer) return;
		releaseSubject();
		tourIndex = 0;
		tourStep(0);
	}

	function stopTour() {
		tourIndex = null;
		tourLabel = '';
		if (tourTimer) clearTimeout(tourTimer);
		tourTimer = null;
	}

	onMount(() => {
		const settle = makeReadyHook();
		ops.start(); // events + band feed the events layer / AMA chip
		godsEye.start();

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
				const useIon = godsEye.imageryMode === 'ion-photoreal' && godsEye.imageryToken;
				const basemap = useIon
					? createIonBasemap(cesium, godsEye.imageryToken!)
					: createKeylessBasemap(cesium);

				viewer = new cesium.Viewer(container, {
					baseLayer: basemap.baseLayer,
					terrainProvider: basemap.terrainProvider,
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
				});

				// Offline detection: sustained tile errors → schematic mode.
				const baseProvider = (viewer.scene.imageryLayers.get(0) as unknown as {
					imageryProvider?: { errorEvent?: { addEventListener(cb: (e: unknown) => void): () => void } };
				})?.imageryProvider;
				if (baseProvider?.errorEvent) {
					removeTileError = baseProvider.errorEvent.addEventListener(() => {
						tileErrors++;
						if (tileErrors >= 5 && !offlineDetected) enterOfflineSchematic(viewer!);
					});
				}

				// Real-time tilesLoaded publishing: the queue-depth event fires
				// as imagery loads, so the test snapshot (and any observer) sees
				// the moment the globe settles instead of waiting for the next
				// layer-refresh publish.
				const globeEvents = viewer.scene.globe as unknown as {
					tileLoadProgressEvent?: { addEventListener(cb: (remaining: number) => void): () => void };
				};
				if (globeEvents.tileLoadProgressEvent) {
					removeTileProgress = globeEvents.tileLoadProgressEvent.addEventListener((remaining) => {
						if (window.__GODS_EYE__) window.__GODS_EYE__.tilesLoaded = remaining === 0;
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
					inst.init({ cesium, viewer, fetchJson, notify: refreshStats });
					instances.set(layer.id, inst);
				}
				for (const layer of GODS_EYE_LAYERS) {
					if (activeLayers.has(layer.id)) instances.get(layer.id)?.activate();
				}

				// The shared pursuit overlay (wall + cinematic).
				pursuit = createPursuitOverlay(cesium, viewer);

				// Picking: route the entity id to its owning layer.
				const hh = new cesium.ScreenSpaceEventHandler(viewer.scene.canvas);
				hh.setInputAction((movement) => {
					if (!viewer) return;
					markInteraction();
					const picked = viewer.scene.pick(movement.position);
					const id = (picked as { id?: { id?: string } } | undefined)?.id?.id;
					if (!id) {
						pickedInfo = null;
						return;
					}
					for (const [layerId, inst] of instances) {
						if (inst.handlePick(id)) {
							const label = GODS_EYE_LAYERS.find((l) => l.id === layerId)?.label ?? layerId;
							pickedInfo = `${label} · ${id}`;
							return;
						}
					}
					pickedInfo = id;
				}, cesium.ScreenSpaceEventType.LEFT_CLICK);
				handler = hh;

				// Interaction pauses the wall's auto-orbit for 30 s.
				container.addEventListener('pointerdown', markInteraction, { passive: true });
				container.addEventListener('wheel', markInteraction, { passive: true });

				removeCameraListener = viewer.camera.changed.addEventListener(updateHash);

				if (hud !== 'mini') startOrbit();

				hudTimer = setInterval(refreshStats, 2000);
				// FLIR coherent-noise clock.
				timeTimer = setInterval(() => {
					if (stage?.uniforms) stage.uniforms['time'] = performance.now() / 1000;
				}, 100);

				refreshStats();
				phase = 'ready';
				settle(true);
			} catch (err) {
				unavailableReason = `viewer failed: ${err instanceof Error ? err.message : String(err)}`;
				phase = 'unavailable';
				settle(false, unavailableReason);
			}
		})();
	});

	// Wall mode: the selected event locks the camera to its spine.
	$effect(() => {
		const id = ops.selectedEventId;
		if (!godsEye.followSelection || phase !== 'ready' || !id) return;
		followSubject(id);
	});

	// The pursuit overlay rides the interception plan (both wall + full).
	$effect(() => {
		const plan = ops.interception;
		if (phase !== 'ready' || !pursuit) return;
		if (!plan?.latestSighting || !plan.vehicleId) {
			pursuit.clear();
			publishSnapshot();
			return;
		}
		const vehicleId: string = plan.vehicleId;
		void (async () => {
			let trace: Array<[number, number]> = [];
			try {
				const t = await fetchVehicleTrace(vehicleId, 900, AbortSignal.timeout(6000));
				trace = t.coordinates;
			} catch {
				// trace endpoint absent — the sighting + chokes still draw
			}
			pursuit?.show({ plan, trace });
			publishSnapshot();
		})();
	});

	onDestroy(() => {
		stopTour();
		stopOrbit();
		if (hudTimer) clearInterval(hudTimer);
		if (timeTimer) clearInterval(timeTimer);
		if (hashTimer) clearTimeout(hashTimer);
		hudTimer = timeTimer = null;
		handler?.destroy();
		handler = null;
		removeCameraListener?.();
		removeCameraListener = null;
		removeTileError?.();
		removeTileError = null;
		removeTileProgress?.();
		removeTileProgress = null;
		pursuit?.destroy();
		pursuit = null;
		for (const inst of instances.values()) inst.destroy();
		instances = new Map();
		try {
			viewer?.destroy();
		} catch {
			// viewer already torn down by navigation
		}
		viewer = null;
		godsEye.stop();
		ops.stop();
	});

	// AMA chip (R9): band + last-transition age from the ops store ladder.
	let amaBand = $derived(ops.band ?? '—');
	let amaAgeSec = $derived(ops.bandUpdated ? Math.max(0, Math.round((Date.now() - ops.bandUpdated) / 1000)) : null);
	let amaSource = $derived(
		ops.bandSource === 'situation_log'
			? 'situation_log'
			: ops.bandSource === 'adaptive-status'
				? 'adaptive/status'
				: '—',
	);
</script>

<svelte:window onkeydown={(e) => { if (e.key === 'Escape' && tourIndex != null) stopTour(); }} />

<div class="ge-wrap" class:mini={hud === 'mini'}>
	{#if phase === 'loading'}
		<div class="ge-status"><span class="pulse"></span> initialising God's-eye canvas…</div>
	{/if}

	{#if phase === 'unavailable'}
		<div class="ge-degrade">
			<h3>3D God's-eye canvas unavailable</h3>
			<p>{unavailableReason}</p>
			<p class="dim">
				This panel degrades alone — the board keeps working. The 2D corridor map
				stays on <a href="/map">/map</a>. A WebGL-capable browser (or the served
				build with static/cesium) renders the Cesium canvas.
			</p>
		</div>
	{/if}

	<div class="ge-canvas" bind:this={container}></div>

	{#if phase === 'ready'}
		<div class="ge-hud" class:mini={hud === 'mini'}>
			{#if hud === 'full' || hud === 'wall'}
				<div class="hud-row">
					<button class="chip sensor" onclick={cycleSensor} title="Sensor mode: FLIR White-Hot / Black-Hot / raw">
						▙ SENSOR {sensorMode === 'flir-whot' ? 'FLIR·WHOT' : sensorMode === 'flir-bhot' ? 'FLIR·BHOT' : 'RAW'}
					</button>
					<button class="chip share" onclick={share} title="Copy this exact view for a Jan Rakshak unit / police control room">
						⇪ SHARE VIEW
					</button>
					<button
						class="chip tour"
						onclick={() => (tourIndex == null ? startTour() : stopTour())}
						title="Guided tour — camera + sensor choreography over the live city (Esc stops)"
					>
						{tourIndex == null ? '▶ TOUR' : `■ ${tourIndex + 1}/${TOUR.length}`}
					</button>
					{#if godsEye.subjectLockId}
						<button class="chip lock" onclick={releaseSubject} title="camera locked to the selected event's spine">
							⊕ LOCKED · RELEASE
						</button>
					{/if}
					{#if shareFlash}<span class="flash">{shareFlash}</span>{/if}
					{#if tourIndex != null && tourLabel}<span class="tour-label">{tourLabel}</span>{/if}
				</div>
				<div class="hud-row layers">
					{#each GODS_EYE_LAYERS as layer (layer.id)}
						<button
							class="chip layer"
							class:off={!activeLayers.has(layer.id)}
							title={stats[layer.id]?.hint ?? layer.name}
							onclick={() => toggleLayer(layer.id)}
						>
							{layer.label}
							<span class="n">{stats[layer.id]?.count ?? 0}</span>
						</button>
					{/each}
				</div>
				<div class="hud-row meta">
					<span
						class="chip ama"
						title="Active Movement Adaptation — situation → hysteresis → policy floors → twin shadow-eval (fail-closed) → audited persistence"
					>
						AMA {amaBand}
						{#if amaAgeSec != null}<span class="n">·{amaAgeSec}s</span>{/if}
						<span class="n dim">[{amaSource}]</span>
					</span>
					<span class="chip mode" title="Basemap mode (dark re-grade applied to the keyless raster)">
						{offlineDetected
							? 'OFFLINE SCHEMATIC MODE'
							: godsEye.imageryMode === 'ion-photoreal'
								? 'ION PHOTOREAL'
								: 'KEYLESS OSM · DARK'}
					</span>
					{#if pursuitCount > 0}
						<span class="chip pursuit" title="pursuit overlay — trace + ranked chokes + future arc">
							PURSUIT <span class="n">{pursuitCount}</span>
						</span>
					{/if}
					<span class="credit">
						© OpenStreetMap contributors · canvas patterns from
						<a href="https://github.com/bilawalsidhu/gods-eye-view" target="_blank" rel="noreferrer">gods-eye-view (MIT)</a>
					</span>
				</div>
				{#if pickedInfo}
					<div class="ge-pick">{pickedInfo}</div>
				{/if}
				<div class="ge-feeds">
					{#each GODS_EYE_LAYERS as layer (layer.id)}
						{#if stats[layer.id]?.error}
							<span class="feed-warn">{layer.label}: {stats[layer.id].error}</span>
						{/if}
					{/each}
				</div>
			{:else}
				<span class="chip mode">
					{offlineDetected ? 'OFFLINE SCHEMATIC' : godsEye.imageryMode === 'ion-photoreal' ? 'ION' : 'OSM·DARK'}
				</span>
			<span class="chip ama" style={`border-color:${ops.band ? (cssVar(`--v-band-${ops.band.toLowerCase()}`) ?? 'var(--v-line-2)') : 'var(--v-line-2)'}`}>
				AMA {amaBand}{amaAgeSec != null ? ` ·${amaAgeSec}s` : ''}
			</span>
				<a class="chip layer" href="/gods-eye">FULLSCREEN ⤢</a>
			{/if}
		</div>
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

	.ge-hud {
		position: absolute;
		left: var(--v-space-3);
		right: var(--v-space-3);
		top: var(--v-space-3);
		display: flex;
		flex-direction: column;
		gap: var(--v-space-2);
		z-index: 4;
		pointer-events: none;
	}

	.ge-hud .hud-row,
	.ge-hud .chip,
	.ge-hud .ge-pick,
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
	}

	button.chip {
		cursor: pointer;
	}

	button.chip:hover {
		border-color: var(--v-line-select);
	}

	.chip.sensor {
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

	.chip.layer.off {
		opacity: 0.45;
	}

	.chip .n {
		color: var(--v-data);
	}

	.chip .dim {
		color: var(--v-ink-3);
	}

	.chip.ama {
		border-color: var(--v-data);
		color: var(--v-data);
	}

	.tour-label {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-2);
		background: color-mix(in srgb, var(--v-canvas) 82%, transparent);
		border: 1px solid var(--v-line-1);
		border-radius: var(--v-radius-chip);
		padding: 2px var(--v-space-2);
		max-width: 46ch;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.credit {
		font-size: var(--v-text-2xs);
		color: var(--v-ink-4);
		margin-left: auto;
	}

	.credit a {
		color: var(--v-ink-3);
	}

	.flash {
		font-family: var(--v-font-mono);
		font-size: var(--v-text-2xs);
		color: var(--v-band-green);
		background: color-mix(in srgb, var(--v-canvas) 85%, transparent);
		border-radius: var(--v-radius-chip);
		padding: 2px var(--v-space-2);
		max-width: 40ch;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.ge-pick {
		align-self: flex-start;
		font-family: var(--v-font-mono);
		font-size: var(--v-text-xs);
		color: var(--v-band-amber);
		background: color-mix(in srgb, var(--v-canvas) 85%, transparent);
		border: 1px dashed var(--v-band-amber);
		border-radius: var(--v-radius-chip);
		padding: 2px var(--v-space-2);
		max-width: 60ch;
	}

	.ge-feeds {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.feed-warn {
		font-family: var(--v-font-mono);
		font-size: var(--v-text-2xs);
		color: var(--v-band-red);
	}

	.ge-hud.mini {
		flex-direction: row;
		gap: var(--v-space-1);
		align-items: flex-start;
	}

	@media (prefers-reduced-motion: reduce) {
		.pulse {
			animation: none;
			opacity: 0.5;
		}
	}
</style>
