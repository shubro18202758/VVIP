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
 *   - `Viewer` option `baseLayer` (imageryProvider is deprecated)
 *   - `SceneTransforms.worldToWindowCoordinates` (wgs84ToWindowCoordinates
 *     was renamed ~1.121)
 */

declare global {
	/** Minimal viewer-camera surface used by the canvas + share links. */
	interface CesiumCamera {
		setView(opts: { destination?: unknown; orientation?: { heading?: number; pitch?: number; roll?: number } }): void;
		flyTo(opts: {
			destination: unknown;
			orientation?: { heading?: number; pitch?: number; roll?: number };
			duration?: number;
		}): void;
		/** Fires when the view changes enough (share-hash updates). */
		changed: { addEventListener(cb: () => void): () => void };
		/** Slow auto-orbit step (wall mode). */
		rotateRight(angleRad: number): void;
		/** Current camera pose — all radians. */
		heading: number;
		pitch: number;
		roll: number;
		positionCartographic: { longitude: number; latitude: number; height: number };
	}

	interface CesiumEntityCollection {
		add(entity: Record<string, unknown>): { id: string };
		removeById(id: string): boolean;
		removeAll(): void;
		values: Array<{ id: string }>;
	}

	interface CesiumImageryLayerCollection {
		add(layer: unknown, index?: number): unknown;
		remove(layer: unknown, destroy?: boolean): boolean;
		removeAll(): void;
		get(index: number): unknown;
		length: number;
	}

	interface CesiumGlobe {
		show: boolean;
		tilesLoaded: boolean;
	}

	interface CesiumPostProcessStageCollection {
		add(stage: unknown): unknown;
		remove(stage: unknown): void;
		contains(stage: unknown): boolean;
	}

	interface CesiumScene {
		globe: CesiumGlobe;
		canvas: HTMLCanvasElement;
		imageryLayers: CesiumImageryLayerCollection;
		postProcessStages: CesiumPostProcessStageCollection;
		pick(position: { x: number; y: number }): { id?: { id?: string } } | undefined;
		requestRenderMode: boolean;
		requestRender(): void;
		preUpdate?: { addEventListener(cb: () => void): () => void };
	}

	interface CesiumViewer {
		container: HTMLElement;
		canvas: HTMLCanvasElement;
		camera: CesiumCamera;
		entities: CesiumEntityCollection;
		scene: CesiumScene;
		destroy(): void;
		resize(): void;
	}

	/** The subset of the Cesium global the canvas touches. */
	interface CesiumGlobal {
		Viewer: new (container: HTMLElement, opts?: Record<string, unknown>) => CesiumViewer;
		Cartesian2: { new (x: number, y: number): { x: number; y: number } };
		Cartesian3: { new (x: number, y: number, z: number): unknown } & {
			fromDegrees(lon: number, lat: number, height?: number): unknown;
			fromDegreesArray(coords: number[]): unknown[];
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
			opts: { url: string; credit?: string; maximumLevel?: number },
		) => {
			errorEvent: { addEventListener(cb: (err: unknown) => void): () => void };
		};
		ImageryLayer: new (provider: unknown, opts?: Record<string, unknown>) => unknown;
		SingleTileImageryProvider: new (opts: { url: string; rectangle?: unknown }) => unknown;
		EllipsoidTerrainProvider: new () => unknown;
		PostProcessStage: new (opts: {
			fragmentShader: string;
			uniforms?: Record<string, number>;
			name?: string;
		}) => { uniforms?: Record<string, number> };
		ScreenSpaceEventHandler: new (canvas: HTMLCanvasElement) => {
			setInputAction(cb: (movement: { position: { x: number; y: number } }) => void, type: number): void;
			destroy(): void;
		};
		ScreenSpaceEventType: { LEFT_CLICK: number };
		/** Time-varying property for animated entity fields (pursuit overlay). */
		CallbackProperty: new (callback: () => unknown, isConstant: boolean) => unknown;
		/** Dashed polyline material (the marching future arc). */
		PolylineDashMaterialProperty: new (opts: {
			color?: unknown;
			dashLength?: number;
			dashPattern?: number;
		}) => unknown;
		/** Solid colour material for points/polylines. */
		ColorMaterialProperty: new (opts: { color: unknown }) => unknown;
		Math: {
			toRadians(deg: number): number;
			toDegrees(rad: number): number;
		};
		Rectangle: { fromDegrees(w: number, s: number, e: number, n: number): unknown };
		IonImageryProvider: new (opts?: Record<string, unknown>) => unknown;
		CesiumTerrainProvider: new (opts: { url: string }) => unknown;
		Ion: { defaultAccessToken: string };
		IonResource: { fromAssetId(id: number, opts?: Record<string, unknown>): Promise<unknown> };
	}

	interface Window {
		Cesium?: CesiumGlobal;
		/** Canvas smoke-test hook (tests/gods-eye-smoke.mjs reads this). */
		__GODS_EYE__?: {
			phase: 'loading' | 'ready' | 'unavailable';
			imageryMode: 'keyless-osm' | 'ion-photoreal' | 'offline-schematic';
			tilesLoaded: boolean;
			entityCount: number;
			distinctLayerIds: string[];
			offlineDetected: boolean;
			sensorMode: 'flir-whot' | 'flir-bhot' | 'raw';
			/** Pursuit overlay entity count (trace + chokes + arc + markers). */
			pursuitEntities: number;
			ready: Promise<{ ok: boolean; reason?: string }>;
		};
	}
}

export {};
