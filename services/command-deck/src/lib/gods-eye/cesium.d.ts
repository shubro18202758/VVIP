/**
 * Ambient typings for the PREBUILT Cesium bundle.
 *
 * The deck never imports the 'cesium' npm package in application code —
 * pages load the global `Cesium` from `/cesium/Cesium.js` (the 23 MB
 * Build/Cesium directory vendored into static/ at build time; see
 * infra/docker/Dockerfile.command-deck). This file declares just the surface
 * the God's-eye canvas uses, so svelte-check stays strict without a
 * 79 MB type dependency.
 *
 * Verified against cesium@1.146.0 (the version pinned in package.json):
 *   - `Viewer` option `baseLayer` (imageryProvider is deprecated) and
 *     `terrain` (a `Terrain` wrapper around an async terrain provider)
 *   - `SceneTransforms.worldToWindowCoordinates` (wgs84ToWindowCoordinates
 *     was renamed ~1.121)
 *   - the ion constructors are gone: `IonImageryProvider.fromAssetId()` and
 *     `Terrain.fromWorldTerrain()` are the async factories, wrapped with
 *     `ImageryLayer.fromProviderAsync()` (errors surface on `errorEvent`)
 *   - `flyTo` keeps a non-identity camera transform unless `endTransform`
 *     says otherwise, so orbit frames are reset before every flight
 *   - the viewer clock (`startTime/stopTime/currentTime/multiplier/
 *     shouldAnimate/clockRange`) drives time-dynamic entities: REPLAY maps an
 *     emulator tape's frames onto `SampledPositionProperty` tracks
 *     (ExtrapolationType NONE=0, HOLD=1; ClockRange CLAMPED=1)
 *   - `createGooglePhotorealistic3DTileset(apiOptions?, tilesetOptions?)` is
 *     async; without a Google key it loads ion asset 2275207 with
 *     `Ion.defaultAccessToken` (`Cesium3DTileset.fromIonAssetId` is the
 *     generic factory). A tileset is a scene PRIMITIVE (`scene.primitives`);
 *     `initialTilesLoaded` fires once the first view is covered
 *   - DRAPING (layers/draping.ts): `polyline.clampToGround` + `classificationType`
 *     (ClassificationType TERRAIN 0 · CESIUM_3D_TILE 1 · BOTH 2) + `zIndex`
 *     build a GroundPolylinePrimitive; `heightReference` (HeightReference
 *     NONE 0 · CLAMP_TO_GROUND 1 · RELATIVE_TO_GROUND 2 · CLAMP_TO_TERRAIN 3
 *     · RELATIVE_TO_TERRAIN 4 · CLAMP_TO_3D_TILE 5 · RELATIVE_TO_3D_TILE 6)
 *     clamps points / billboards / labels — to a 3D tileset only when it was
 *     built with `enableCollision` (Scene.getHeight skips other tilesets);
 *     a clamped POINT is drawn as the entity's billboard (PointVisualizer),
 *     so an entity must not carry both
 *   - Bing imagery behind ion asset 2 is requested in the PAGE's scheme
 *     (`BingMapsImageryProvider.fromUrl` reads `document.location.protocol`;
 *     the ion factory passes no `tileProtocol`), so a plain-http deck asks
 *     http://ecn.t{0-3}.tiles.virtualearth.net — the operator CSP admits it
 */

declare global {
	/** Cesium.Event surface (raiseEvent stays internal to Cesium). */
	interface CesiumEvent<A extends unknown[] = unknown[]> {
		addEventListener(cb: (...args: A) => void): () => void;
		numberOfListeners?: number;
	}

	/** A time-varying entity property (ConstantProperty / CallbackProperty). */
	interface CesiumProperty<T = unknown> {
		getValue(time?: unknown): T;
	}

	/** Minimal viewer-camera surface used by the canvas + share links. */
	interface CesiumCamera {
		setView(opts: {
			destination?: unknown;
			orientation?: { heading?: number; pitch?: number; roll?: number };
			endTransform?: unknown;
		}): void;
		flyTo(opts: {
			destination: unknown;
			orientation?: { heading?: number; pitch?: number; roll?: number };
			duration?: number;
			complete?: () => void;
			cancel?: () => void;
			endTransform?: unknown;
		}): void;
		/**
		 * Frame a sphere from a heading/pitch/range offset (resets the camera
		 * transform to IDENTITY first, then flies in the world frame).
		 */
		flyToBoundingSphere(
			sphere: unknown,
			opts?: { offset?: unknown; duration?: number; complete?: () => void; cancel?: () => void },
		): void;
		cancelFlight(): void;
		/**
		 * Set the camera reference frame (keeps the world pose when no offset
		 * is given). Matrix4.IDENTITY hands the camera back to the world frame.
		 */
		lookAtTransform(transform: unknown, offset?: unknown): void;
		/** Fires when the view changes enough (share-hash updates). */
		changed: CesiumEvent;
		/** Fraction of view change that raises `changed` (default 0.5). */
		percentageChanged: number;
		/**
		 * Rotate about the reference frame's constrained axis — inside an
		 * east-north-up frame at a pivot this is a true orbit of the pivot.
		 */
		rotateRight(angleRad: number): void;
		/** Ray-cast a window position onto the ellipsoid (undefined = sky). */
		pickEllipsoid(windowPosition: unknown, ellipsoid?: unknown): unknown | undefined;
		/** Current camera pose — all radians, world frame. */
		heading: number;
		pitch: number;
		roll: number;
		positionCartographic: { longitude: number; latitude: number; height: number };
		/** World-frame position (Cartesian3). */
		positionWC: unknown;
		/**
		 * PerspectiveFrustum: `fov` (rad) is the horizontal angle when the
		 * canvas is wider than tall, else the vertical one.
		 */
		frustum: { fov?: number; aspectRatio?: number };
	}

	/** One entity as the canvas reads it back (pick card + hover + locate). */
	interface CesiumEntity {
		id: string;
		description?: CesiumProperty<string> | string;
		position?: CesiumProperty<unknown>;
		polyline?: {
			width?: CesiumProperty<number> | number;
			positions?: CesiumProperty<unknown[]>;
			/** Draped (GroundPolylinePrimitive) — read back by the drape audit. */
			clampToGround?: CesiumProperty<boolean> | boolean;
		};
		point?: { pixelSize?: CesiumProperty<number> | number; outlineWidth?: CesiumProperty<number> | number };
		polygon?: unknown;
		/** Pixel-sized image (e-challan pins, signal glyphs) — highlight scales it. */
		billboard?: { scale?: CesiumProperty<number> | number };
		/** Text; a highlighted entity drops its distance condition so its label always shows. */
		label?: { distanceDisplayCondition?: unknown };
	}

	interface CesiumEntityCollection {
		add(entity: Record<string, unknown>): CesiumEntity;
		removeById(id: string): boolean;
		removeAll(): void;
		getById(id: string): CesiumEntity | undefined;
		values: CesiumEntity[];
	}

	interface CesiumImageryLayerCollection {
		add(layer: unknown, index?: number): unknown;
		remove(layer: unknown, destroy?: boolean): boolean;
		removeAll(): void;
		get(index: number): unknown;
		length: number;
	}

	/** A 3D Tiles tileset (Google Photorealistic 3D Tiles, the photoreal-3d layer). */
	interface CesiumTileset {
		show: boolean;
		maximumScreenSpaceError: number;
		/** Clamped entities and the camera collide with (read heights from) the tiles. */
		enableCollision?: boolean;
		/** Bytes of tile content + textures held right now (the cache in use). */
		totalMemoryUsageInBytes?: number;
		/** Once: the tiles that cover the first view have loaded. */
		initialTilesLoaded: CesiumEvent<[]>;
		/** Per failed tile request ({url, message}). */
		tileFailed: CesiumEvent<[unknown]>;
		destroy(): void;
		isDestroyed(): boolean;
	}

	/** scene.primitives — the collection a tileset is added to (and destroyed by on remove). */
	interface CesiumPrimitiveCollection {
		add<T>(primitive: T): T;
		remove(primitive: unknown): boolean;
		contains(primitive: unknown): boolean;
	}

	interface CesiumGlobe {
		show: boolean;
		tilesLoaded: boolean;
		terrainProvider: unknown;
		ellipsoid: unknown;
		/** Surface colour where no imagery has landed yet (Cesium default: blue). */
		baseColor?: unknown;
		/** Queue-depth event: 0 once every tile the view needs has loaded. */
		tileLoadProgressEvent?: CesiumEvent<[number]>;
		/** Terrain/imagery detail (default 2) — coarsened while the photoreal tiles cover it. */
		maximumScreenSpaceError?: number;
	}

	interface CesiumPostProcessStageCollection {
		add(stage: unknown): unknown;
		remove(stage: unknown): void;
		contains(stage: unknown): boolean;
	}

	interface CesiumScene {
		globe: CesiumGlobe;
		canvas: HTMLCanvasElement;
		/** Clear colour behind the globe (shows where the sky box does not). */
		backgroundColor?: unknown;
		/** The star field (undefined on a build or scene without one). */
		skyBox?: { show: boolean } | undefined;
		imageryLayers: CesiumImageryLayerCollection;
		postProcessStages: CesiumPostProcessStageCollection;
		primitives: CesiumPrimitiveCollection;
		/** `id` is the picked Entity for entity-backed primitives. */
		pick(position: { x: number; y: number }): { id?: CesiumEntity | string } | undefined;
		/** Every primitive under the point, front to back (at most `limit`). */
		drillPick(position: { x: number; y: number }, limit?: number): Array<{ id?: CesiumEntity | string }>;
		requestRenderMode: boolean;
		requestRender(): void;
		/** Raised before every frame update (orbit + guard tick). */
		preUpdate: CesiumEvent<[unknown, unknown]>;
		/** Mouse/touch camera input (zoom limits in metres; default max is +∞). */
		screenSpaceCameraController: {
			minimumZoomDistance: number;
			maximumZoomDistance: number;
		};
	}

	/** A Cesium JulianDate (opaque; built and compared through CesiumGlobal.JulianDate). */
	type CesiumJulianDate = { dayNumber: number; secondsOfDay: number };

	/** The viewer clock — REPLAY scrubs it; live mode leaves it alone. */
	interface CesiumClock {
		startTime: CesiumJulianDate;
		stopTime: CesiumJulianDate;
		currentTime: CesiumJulianDate;
		multiplier: number;
		shouldAnimate: boolean;
		/** ClockRange: UNBOUNDED 0, CLAMPED 1, LOOP_STOP 2. */
		clockRange: number;
		onTick: CesiumEvent<[unknown]>;
	}

	interface CesiumViewer {
		container: HTMLElement;
		canvas: HTMLCanvasElement;
		camera: CesiumCamera;
		entities: CesiumEntityCollection;
		scene: CesiumScene;
		clock: CesiumClock;
		/** The data-credit bar: static credits ride every frame (a showOnScreen credit on the bar itself). */
		creditDisplay?: { addStaticCredit(credit: unknown): void; removeStaticCredit?(credit: unknown): void };
		destroy(): void;
		resize(): void;
	}

	/** An imagery layer as created by `ImageryLayer.fromProviderAsync`. */
	interface CesiumImageryLayer {
		readyEvent: CesiumEvent<[unknown]>;
		errorEvent: CesiumEvent<[unknown]>;
		brightness?: number;
		contrast?: number;
		saturation?: number;
		gamma?: number;
	}

	/** A `Terrain` wrapper (Viewer option `terrain`). */
	interface CesiumTerrain {
		readyEvent: CesiumEvent<[unknown]>;
		errorEvent: CesiumEvent<[unknown]>;
	}

	/** The subset of the Cesium global the canvas touches. */
	interface CesiumGlobal {
		Viewer: new (container: HTMLElement, opts?: Record<string, unknown>) => CesiumViewer;
		Cartesian2: { new (x: number, y: number): { x: number; y: number } };
		Cartesian3: { new (x: number, y: number, z: number): unknown } & {
			fromDegrees(lon: number, lat: number, height?: number): unknown;
			fromDegreesArray(coords: number[]): unknown[];
		};
		Cartographic: {
			fromCartesian(cartesian: unknown): { longitude: number; latitude: number; height: number } | undefined;
		};
		Color: { new (r: number, g: number, b: number, a?: number): unknown } & {
			fromCssColorString(css: string): unknown;
			TRANSPARENT?: unknown;
			WHITE?: unknown;
			BLACK?: unknown;
			GRAY?: unknown;
			LIGHTGRAY?: unknown;
			DARKGRAY?: unknown;
			ORANGE?: unknown;
			RED?: unknown;
			CYAN?: unknown;
			LIME?: unknown;
		};
		UrlTemplateImageryProvider: new (
			opts: { url: string; credit?: string | unknown; maximumLevel?: number },
		) => {
			errorEvent: CesiumEvent<[unknown]>;
		};
		ImageryLayer: (new (provider: unknown, opts?: Record<string, unknown>) => CesiumImageryLayer) & {
			/** Wrap an async provider promise; failures raise `errorEvent`. */
			fromProviderAsync(providerPromise: Promise<unknown>, opts?: Record<string, unknown>): CesiumImageryLayer;
		};
		SingleTileImageryProvider: new (opts: { url: string; rectangle?: unknown }) => unknown;
		/** A data credit: html (DOMPurify-sanitised by Cesium), on the on-screen bar when showOnScreen (else the lightbox). */
		Credit?: new (html: string, showOnScreen?: boolean) => unknown;
		EllipsoidTerrainProvider: new () => unknown;
		Terrain: (new (providerPromise: Promise<unknown>) => CesiumTerrain) & {
			fromWorldTerrain(opts?: Record<string, unknown>): CesiumTerrain;
		};
		PostProcessStage: new (opts: {
			fragmentShader: string;
			uniforms?: Record<string, number>;
			name?: string;
		}) => { uniforms?: Record<string, number> };
		ScreenSpaceEventHandler: new (canvas: HTMLCanvasElement) => {
			setInputAction(
				cb: (movement: { position?: { x: number; y: number }; endPosition?: { x: number; y: number } }) => void,
				type: number,
			): void;
			destroy(): void;
		};
		ScreenSpaceEventType: { LEFT_CLICK: number; MOUSE_MOVE: number };
		/** Time-varying property for animated entity fields (pursuit overlay). */
		CallbackProperty: new (callback: () => unknown, isConstant: boolean) => unknown;
		/** Dashed polyline material (the marching future arc, alternate spines). */
		PolylineDashMaterialProperty: new (opts: {
			color?: unknown;
			/** Colour between the dashes (default transparent). */
			gapColor?: unknown;
			/** Pixels per dash cycle — a number, or a time-varying property (the marching arc). */
			dashLength?: unknown;
			dashPattern?: number;
		}) => unknown;
		/** Polyline in a casing colour (the flow ramp's dark outline). Absent on stripped builds. */
		PolylineOutlineMaterialProperty?: new (opts: { color?: unknown; outlineColor?: unknown; outlineWidth?: number }) => unknown;
		/** HeightReference enum (see the header for the 1.146 values). */
		HeightReference?: {
			NONE: number;
			CLAMP_TO_GROUND: number;
			RELATIVE_TO_GROUND: number;
			CLAMP_TO_TERRAIN?: number;
			CLAMP_TO_3D_TILE?: number;
		};
		/** ClassificationType enum: what a draped primitive paints onto. */
		ClassificationType?: { TERRAIN: number; CESIUM_3D_TILE: number; BOTH: number };
		/** Entity statics — ground polylines need depth textures. */
		Entity?: { supportsPolylinesOnTerrain(scene: CesiumScene): boolean };
		/** Glowing polyline material (the RECOMMENDED primary spine). */
		PolylineGlowMaterialProperty: new (opts: { color?: unknown; glowPower?: number; taperPower?: number }) => unknown;
		/** Arrow-headed polyline material (dispatch lines, convoy heading). */
		PolylineArrowMaterialProperty: new (color?: unknown) => unknown;
		JulianDate: {
			fromIso8601(iso: string): CesiumJulianDate;
			fromDate(date: Date): CesiumJulianDate;
			now(): CesiumJulianDate;
			addSeconds(julian: CesiumJulianDate, seconds: number, result: CesiumJulianDate): CesiumJulianDate;
			secondsDifference(left: CesiumJulianDate, right: CesiumJulianDate): number;
			clone(julian: CesiumJulianDate): CesiumJulianDate;
			toDate(julian: CesiumJulianDate): Date;
		} & (new () => CesiumJulianDate);
		/** Time-sampled position (REPLAY tracks); interpolates linearly between samples. */
		SampledPositionProperty: new () => {
			addSample(time: CesiumJulianDate, position: unknown): void;
			getValue(time: CesiumJulianDate): unknown;
			forwardExtrapolationType: number;
			backwardExtrapolationType: number;
		};
		ExtrapolationType: { NONE: number; HOLD: number; EXTRAPOLATE: number };
		ClockRange: { UNBOUNDED: number; CLAMPED: number; LOOP_STOP: number };
		/** Solid colour material — the constructor takes the colour (or a Property) itself. */
		ColorMaterialProperty: new (color?: unknown) => unknown;
		/** Show a graphic only between near/far camera distances (metres). */
		DistanceDisplayCondition: new (near: number, far: number) => unknown;
		/** Scale/translucency by camera distance (near, nearValue, far, farValue). */
		NearFarScalar: new (near: number, nearValue: number, far: number, farValue: number) => unknown;
		HeadingPitchRange: new (heading: number, pitch: number, range: number) => unknown;
		BoundingSphere: new (center: unknown, radius: number) => unknown;
		Matrix4: { IDENTITY: unknown };
		Transforms: { eastNorthUpToFixedFrame(origin: unknown): unknown };
		SceneTransforms: {
			worldToWindowCoordinates(scene: CesiumScene, position: unknown): { x: number; y: number } | undefined;
		};
		Math: {
			toRadians(deg: number): number;
			toDegrees(rad: number): number;
		};
		Rectangle: { fromDegrees(w: number, s: number, e: number, n: number): unknown };
		IonImageryProvider: {
			/** Async factory (the 1.146 replacement for the removed constructor). */
			fromAssetId(assetId: number, opts?: Record<string, unknown>): Promise<unknown>;
		};
		Ion: { defaultAccessToken: string };
		IonResource: { fromAssetId(id: number, opts?: Record<string, unknown>): Promise<unknown> };
		/**
		 * Google Photorealistic 3D Tiles (1.146: async; `apiOptions.key` absent →
		 * ion asset 2275207 with Ion.defaultAccessToken). Absent on old builds.
		 */
		createGooglePhotorealistic3DTileset?: (
			apiOptions?: { key?: string; onlyUsingWithGoogleGeocoder?: boolean },
			tilesetOptions?: Record<string, unknown>,
		) => Promise<CesiumTileset>;
		Cesium3DTileset: {
			fromIonAssetId(assetId: number, opts?: Record<string, unknown>): Promise<CesiumTileset>;
		};
	}

	/** Sensor modes: COMMAND (raw) is the default; the FLIR looks are opt-in. */
	type GodsEyeSensorMode = 'raw' | 'flir-whot' | 'flir-bhot' | 'flir-iron';

	interface Window {
		Cesium?: CesiumGlobal;
		/** Canvas smoke-test hook (tests/gods-eye-smoke.mjs, tests/canvas-ux-smoke.mjs). */
		__GODS_EYE__?: {
			phase: 'loading' | 'ready' | 'unavailable';
			imageryMode: 'keyless-osm' | 'ion-photoreal' | 'offline-schematic';
			/** Why the ion upgrade fell back to keyless (null = no fallback). */
			imageryNote: string | null;
			tilesLoaded: boolean;
			entityCount: number;
			distinctLayerIds: string[];
			/** Per-layer HUD chip counts ({layerId: count}; events = events, not entities). */
			stats: Record<string, number>;
			offlineDetected: boolean;
			sensorMode: GodsEyeSensorMode;
			/** World-frame camera position (degrees / metres), refreshed every second. */
			camera: { lon: number; lat: number; alt: number; heading: number; pitch: number } | null;
			/** True while the wall's auto-orbit holds the camera frame. */
			orbiting: boolean;
			selectedEventId: string | null;
			/** Event the camera is held on (LOCKED chip), set only when the flight to it lands. */
			subjectLockId: string | null;
			/** The pick card currently shown (null when dismissed). */
			picked: { id: string; layer: string; text: string; eventId: string | null } | null;
			/** AMA chip age in seconds (now − ops.bandUpdated), ticking 1 Hz. */
			amaAgeSec: number | null;
			/** Pursuit overlay entity count (trace + chokes + arc + markers). */
			pursuitEntities: number;
			/**
			 * Per-layer coverage with its named sub-counts (VEER CITY COMMAND):
			 * {layerId: {count, via, ...detail}} — e.g. flow.coloured,
			 * events.live, enforcement.pending.
			 */
			layerStats: Record<string, Record<string, number | string>>;
			/**
			 * The selected entity URN (dossier subject), hovered URN, last fly
			 * outcome, where the selection halo stands (degrees) and how many of
			 * the selection's own primitives wear the highlight.
			 */
			selection: {
				urn: string | null;
				hover: string | null;
				fly: string | null;
				flyOutcome: string | null;
				/** Request → landing of the newest fly-to (ms, page clock). */
				flyMs: number | null;
				halo: { lon: number; lat: number } | null;
				highlighted: number;
			};
			/** City registry: status, entity count, per-kind counts, convoy GPS fixes (degrees). */
			registry: {
				status: string;
				entities: number;
				kinds: Record<string, number>;
				updatedAt: number;
				convoys: Array<{ id: string; lon: number; lat: number; t: number | null; source: string }>;
			};
			/** What the CITY VITALS rail shows (hud/vitals.svelte.ts VitalsShown); null without the rail. */
			vitals: object | null;
			/** REPLAY state (null in live mode). */
			replay: {
				runId: number | null;
				status: string;
				cursorSec: number;
				durationSec: number;
				tracks: number;
				/** Replay vehicle markers at the clock (degrees), keyed by track id. */
				markers: Record<string, { label: string; lon: number; lat: number; camera: string | null; hit: string | null }>;
			} | null;
			/**
			 * Client (viewport CSS px) coordinates of the entities whose id starts
			 * with `prefix` — polylines report their middle vertex, points their
			 * position. Off-screen entities are omitted. Test hook only.
			 */
			locate?: (prefix: string) => Array<{ id: string; x: number; y: number }>;
			ready: Promise<{ ok: boolean; reason?: string }>;
		};
	}
}

export {};
