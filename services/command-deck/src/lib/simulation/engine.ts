/**
 * engine.ts — Main simulation orchestrator for the WebGPU microscopic traffic
 * simulation.  Ties together the GPU compute context, road geometry projection,
 * diversion plan application, and the Svelte store updates.
 *
 * Usage:
 *   const gpu = new SimulationGPU();
 *   await gpu.initialize(MAX_VEHICLES, MAX_SEGMENTS);
 *   const sim = new TrafficSimulation(gpu, config);
 *   sim.initializeVehicles(30);  // 30 vehicles/km
 *   sim.start();
 *   // ... later
 *   sim.pause();
 *   sim.destroy();
 */

import type {
	SimulationConfig,
	DiversionEntry,
	RoadSegment,
} from '$lib/types';

import {
	updateSimulationState,
	simulationSpeed,
} from '$lib/stores/simulation';

import { roadSegments, liveTraffic } from '$lib/stores/traffic';
import { get } from 'svelte/store';

import { SimulationGPU, type IDMParams } from './gpu_context';
import {
	lonLatToMeters,
	computeCentroid,
	projectPolyline,
	polylineLength,
	DEFAULT_REF_LON,
	DEFAULT_REF_LAT,
} from './projection';

// ─── Constants ─────────────────────────────────────────────────────────────

/** Maximum vehicles the simulation will allocate for */
const MAX_VEHICLES = 8192;

/** Maximum road segments */
const MAX_SEGMENTS = 512;

/** Floats per vehicle (must match WGSL struct) */
const FLOATS_PER_VEHICLE = 8;

/** Floats per road segment (must match WGSL struct) */
const FLOATS_PER_SEGMENT = 12;

/** How often (in frames) to read back stats from the GPU */
const STATS_READ_INTERVAL = 15;

/** Default lane width in meters */
const LANE_WIDTH = 3.5;

// ─── Flags bitfield constants ──────────────────────────────────────────────

const FLAG_IS_CONVOY = 1;
const FLAG_IS_DIVERTED = 2;
const FLAG_IS_STOPPED = 4;
const FLAG_IS_EMERGENCY = 8;

// ─── Projected segment cache ───────────────────────────────────────────────

interface ProjectedSegment {
	segmentId: number;
	startX: number;
	startY: number;
	endX: number;
	endY: number;
	length: number;
	speedLimitMs: number;
	lanes: number;
	congestionIdx: number;
	signalState: number;   // 0=red, 1=green, 2=yellow
	closedLanes: number;   // bitmask
}

// ─── Class ─────────────────────────────────────────────────────────────────

export class TrafficSimulation {
	private gpu: SimulationGPU;
	private config: SimulationConfig;
	private running = false;
	private animFrame = 0;
	private frameCount = 0;
	private currentTimeSec = 0;
	private lastFrameTs = 0;
	private fpsAccum = 0;
	private fpsSamples = 0;
	private currentFps = 0;

	// Projected road data
	private projectedSegments: ProjectedSegment[] = [];
	private refLon = DEFAULT_REF_LON;
	private refLat = DEFAULT_REF_LAT;

	// Simulation speed multiplier (subscribed from store)
	private speedMultiplier = 1.0;
	private speedUnsub: (() => void) | null = null;

	// Vehicle count currently in the simulation
	private vehicleCount = 0;

	constructor(gpu: SimulationGPU, config: SimulationConfig) {
		this.gpu = gpu;
		this.config = config;

		// Subscribe to the simulation speed store
		this.speedUnsub = simulationSpeed.subscribe((v) => {
			this.speedMultiplier = v;
		});

		// Compute reference point from convoy route
		if (config.convoyRoute.length > 0) {
			const centroid = computeCentroid(config.convoyRoute);
			this.refLon = centroid.lon;
			this.refLat = centroid.lat;
		}

		// Project road segments and upload to GPU
		this.prepareRoadSegments();

		// Upload default IDM parameters
		this.gpu.updateParams(this.defaultIDMParams());
	}

	// ─── Projection ──────────────────────────────────────────────────────────

	/**
	 * Convert lon/lat road geometry to local meter coordinates using the
	 * equirectangular Mercator approximation.
	 */
	private projectToMeters(
		geometry: [number, number][],
	): { x: number; y: number }[] {
		return projectPolyline(geometry, this.refLon, this.refLat);
	}

	// ─── Road segment preparation ────────────────────────────────────────────

	/**
	 * Load corridor road segments from the traffic store, project them, and
	 * upload the packed Float32Array to the GPU segment buffer.
	 */
	private prepareRoadSegments(): void {
		const segMap = get(roadSegments);
		const trafficMap = get(liveTraffic);
		const corridorIds = new Set(this.config.corridorSegmentIds);

		this.projectedSegments = [];

		segMap.forEach((seg: RoadSegment, segId: number) => {
			if (!corridorIds.has(segId) && corridorIds.size > 0) return;

			// Project the geometry to meters — use first and last point as segment
			// endpoints (the shader models each segment as a straight line).
			const pts = this.projectToMeters(seg.geometry);
			if (pts.length < 2) return;

			const start = pts[0];
			const end = pts[pts.length - 1];
			const len = polylineLength(pts);

			const traffic = trafficMap.get(segId);
			const congestion = traffic ? traffic.congestionIdx : 0;

			this.projectedSegments.push({
				segmentId: segId,
				startX: start.x,
				startY: start.y,
				endX: end.x,
				endY: end.y,
				length: len,
				speedLimitMs: (seg.speedLimitKmh / 3.6),
				lanes: seg.lanes,
				congestionIdx: congestion,
				signalState: 1, // default green
				closedLanes: 0,
			});
		});

		// If no road segments loaded from store, synthesise from convoy route
		if (this.projectedSegments.length === 0 && this.config.convoyRoute.length >= 2) {
			const pts = this.projectToMeters(this.config.convoyRoute);
			for (let i = 0; i < pts.length - 1; i++) {
				const dx = pts[i + 1].x - pts[i].x;
				const dy = pts[i + 1].y - pts[i].y;
				const len = Math.sqrt(dx * dx + dy * dy);
				this.projectedSegments.push({
					segmentId: i,
					startX: pts[i].x,
					startY: pts[i].y,
					endX: pts[i + 1].x,
					endY: pts[i + 1].y,
					length: len,
					speedLimitMs: 13.9, // 50 km/h default
					lanes: 3,
					congestionIdx: 0,
					signalState: 1,
					closedLanes: 0,
				});
			}
		}

		this.uploadSegments();
	}

	private uploadSegments(): void {
		const count = Math.min(this.projectedSegments.length, MAX_SEGMENTS);
		const data = new Float32Array(count * FLOATS_PER_SEGMENT);

		for (let i = 0; i < count; i++) {
			const s = this.projectedSegments[i];
			const off = i * FLOATS_PER_SEGMENT;
			data[off + 0] = s.startX;
			data[off + 1] = s.startY;
			data[off + 2] = s.endX;
			data[off + 3] = s.endY;
			data[off + 4] = s.speedLimitMs;
			data[off + 5] = s.length;
			data[off + 6] = s.lanes;
			data[off + 7] = s.congestionIdx;
			data[off + 8] = s.signalState;
			data[off + 9] = s.closedLanes;
			data[off + 10] = 0; // pad
			data[off + 11] = 0; // pad
		}

		this.gpu.updateSegments(data);
	}

	// ─── Default IDM parameters ──────────────────────────────────────────────

	private defaultIDMParams(): IDMParams {
		return {
			desiredSpeed: 13.9,      // 50 km/h
			timeHeadway: 1.5,        // seconds
			minGap: 2.0,             // meters
			maxAccel: 2.0,           // m/s^2
			comfortDecel: 3.0,       // m/s^2
			delta: 4.0,              // acceleration exponent
			vehicleLength: 4.5,      // meters
			dt: this.config.timeStepSec,
		};
	}

	// ─── Vehicle initialization ──────────────────────────────────────────────

	/**
	 * Scatter vehicles along road segments at a given density (vehicles per km).
	 *
	 * Regular traffic is distributed across all lanes on all segments.
	 * A convoy platoon is placed at the start of the route.
	 *
	 * @param density  Vehicles per kilometre of road
	 */
	initializeVehicles(density: number): void {
		const vehicles: number[] = [];
		const convoyRoutePts = this.projectToMeters(this.config.convoyRoute);

		// --- Scatter ambient traffic ---
		for (const seg of this.projectedSegments) {
			const segLenKm = seg.length / 1000;
			const vehiclesOnSeg = Math.max(1, Math.round(density * segLenKm * seg.lanes));

			const dx = seg.endX - seg.startX;
			const dy = seg.endY - seg.startY;
			const dirX = dx / Math.max(seg.length, 0.001);
			const dirY = dy / Math.max(seg.length, 0.001);

			for (let v = 0; v < vehiclesOnSeg; v++) {
				if (vehicles.length / FLOATS_PER_VEHICLE >= MAX_VEHICLES - 20) break; // reserve 20 for convoy

				const t = (v + 0.5) / vehiclesOnSeg; // progress [0..1]
				const lane = v % seg.lanes;
				const laneOffset =
					lane * LANE_WIDTH + LANE_WIDTH * 0.5 - (seg.lanes * LANE_WIDTH * 0.5);

				const posX = seg.startX + t * dx;
				const posY = seg.startY + t * dy;

				// Initial speed: some fraction of the speed limit with noise
				const baseSpeed = seg.speedLimitMs * (0.6 + Math.random() * 0.3);
				const velX = baseSpeed * dirX;
				const velY = baseSpeed * dirY;

				// Pack vehicle: posX, posY, velX, velY, accelX, accelY, laneOffset, flags
				vehicles.push(posX, posY, velX, velY, 0, 0, laneOffset, 0);
			}
		}

		// --- Place convoy vehicles at the start of the route ---
		if (convoyRoutePts.length >= 2) {
			const convoySpeedMs = this.config.convoySpeedKmh / 3.6;
			const dx = convoyRoutePts[1].x - convoyRoutePts[0].x;
			const dy = convoyRoutePts[1].y - convoyRoutePts[0].y;
			const len = Math.sqrt(dx * dx + dy * dy);
			const dirX = dx / Math.max(len, 0.001);
			const dirY = dy / Math.max(len, 0.001);

			const convoySize = 5; // 5 vehicles in the convoy platoon
			for (let c = 0; c < convoySize; c++) {
				if (vehicles.length / FLOATS_PER_VEHICLE >= MAX_VEHICLES) break;

				const spacing = 8; // 8m between convoy vehicles
				const posX = convoyRoutePts[0].x + dirX * c * spacing;
				const posY = convoyRoutePts[0].y + dirY * c * spacing;
				const velX = convoySpeedMs * dirX;
				const velY = convoySpeedMs * dirY;

				// Flags: isConvoy(1) | isEmergency(8) = 9
				const flagsU32 = FLAG_IS_CONVOY | FLAG_IS_EMERGENCY;
				// Store as f32 via DataView trick
				const flagBuf = new ArrayBuffer(4);
				new Uint32Array(flagBuf)[0] = flagsU32;
				const flagsF32 = new Float32Array(flagBuf)[0];

				vehicles.push(posX, posY, velX, velY, 0, 0, 0, flagsF32);
			}
		}

		this.vehicleCount = vehicles.length / FLOATS_PER_VEHICLE;
		this.gpu.spawnVehicles(new Float32Array(vehicles));

		console.log(
			`[TrafficSimulation] Spawned ${this.vehicleCount} vehicles ` +
			`(density=${density}/km, segments=${this.projectedSegments.length})`,
		);
	}

	// ─── Diversion application ───────────────────────────────────────────────

	/**
	 * Apply diversion entries from the plan that are active at the current
	 * simulation time.  Modifies segment data (closedLanes, speedLimit, signal)
	 * and re-uploads to the GPU.
	 */
	private applyDiversions(currentTimeSec: number): void {
		const plan = this.config.diversionPlan;
		if (!plan || plan.length === 0) return;

		// Reset all segments to default
		for (const seg of this.projectedSegments) {
			seg.closedLanes = 0;
			seg.signalState = 1; // green
		}

		// Build a lookup for segment index by segmentId
		const segIdxMap = new Map<number, number>();
		for (let i = 0; i < this.projectedSegments.length; i++) {
			segIdxMap.set(this.projectedSegments[i].segmentId, i);
		}

		for (const div of plan) {
			// Check if diversion is active at this sim time
			const activateEpoch = new Date(div.activateAt).getTime() / 1000;
			const deactivateEpoch = new Date(div.deactivateAt).getTime() / 1000;
			// Map to relative sim time: assume sim time 0 = plannedDeparture
			// Use relative offsets from first diversion activateAt
			const relativeActivate = activateEpoch - (new Date(plan[0].activateAt).getTime() / 1000);
			const relativeDeactivate = deactivateEpoch - (new Date(plan[0].activateAt).getTime() / 1000);

			if (currentTimeSec < relativeActivate || currentTimeSec > relativeDeactivate) {
				continue; // not active yet or expired
			}

			const segIdx = segIdxMap.get(div.segmentId);
			if (segIdx === undefined) continue;

			const seg = this.projectedSegments[segIdx];

			switch (div.diversionType) {
				case 'full_closure':
					// Close all lanes
					seg.closedLanes = (1 << seg.lanes) - 1;
					break;
				case 'partial_closure':
					// Close the rightmost lane (lane 0)
					seg.closedLanes = 1;
					break;
				case 'speed_restriction':
					// Halve the speed limit
					seg.speedLimitMs = seg.speedLimitMs * 0.5;
					break;
				case 'signal_override':
					// Force red signal for all non-convoy traffic
					seg.signalState = 0;
					break;
			}
		}

		this.uploadSegments();
	}

	// ─── Simulation loop ─────────────────────────────────────────────────────

	/**
	 * Single simulation tick — called from requestAnimationFrame.
	 * Orchestrates diversion application, GPU dispatch, stats readback,
	 * and store updates.
	 */
	private tick = (): void => {
		if (!this.running) return;

		const now = performance.now();
		if (this.lastFrameTs > 0) {
			const dtReal = (now - this.lastFrameTs) / 1000;
			this.fpsAccum += dtReal;
			this.fpsSamples++;
			if (this.fpsAccum >= 0.5) {
				this.currentFps = this.fpsSamples / this.fpsAccum;
				this.fpsAccum = 0;
				this.fpsSamples = 0;
			}
		}
		this.lastFrameTs = now;

		// 1. Apply diversions for the current sim time
		this.applyDiversions(this.currentTimeSec);

		// 2. Update sim time on GPU
		this.gpu.updateSimTime(this.currentTimeSec);

		// 3. GPU compute step (physics + lane changes)
		this.gpu.step();

		// 4. Read back aggregate stats every N frames
		if (this.frameCount % STATS_READ_INTERVAL === 0) {
			this.gpu.computeStats();
			this.gpu.readStats().then((stats) => {
				updateSimulationState({
					running: this.running,
					currentTimeSec: this.currentTimeSec,
					totalVehicles: this.vehicleCount,
					avgSpeedKmh: stats.avgSpeedMs * 3.6,
					avgDelayPerVehicleSec: this.estimateAvgDelay(stats.avgSpeedMs),
					queueLengthM: stats.maxQueueLength * 6, // approx 6m per queued vehicle
					frameCount: this.frameCount,
					fps: Math.round(this.currentFps),
				});
			});
		}

		// 5. Advance simulation time
		this.currentTimeSec += this.config.timeStepSec * this.speedMultiplier;
		this.frameCount++;

		// 6. Check end condition
		if (this.currentTimeSec >= this.config.totalDurationSec) {
			this.pause();
			updateSimulationState({ running: false });
			return;
		}

		// 7. Schedule next frame
		this.animFrame = requestAnimationFrame(this.tick);
	};

	/**
	 * Estimate average delay per vehicle given the current average speed and
	 * the "free-flow" desired speed.
	 */
	private estimateAvgDelay(avgSpeedMs: number): number {
		const freeFlowSpeed = this.defaultIDMParams().desiredSpeed;
		if (avgSpeedMs <= 0 || freeFlowSpeed <= 0) return 0;
		// Delay is the difference in traversal time per unit distance
		const delayPerMeter = (1 / avgSpeedMs) - (1 / freeFlowSpeed);
		// Approximate with a 500m reference distance
		return Math.max(0, delayPerMeter * 500);
	}

	// ─── Public controls ─────────────────────────────────────────────────────

	/**
	 * Start the simulation loop.
	 */
	start(): void {
		if (this.running) return;
		this.running = true;
		this.lastFrameTs = 0;
		this.fpsAccum = 0;
		this.fpsSamples = 0;

		updateSimulationState({ running: true });
		this.animFrame = requestAnimationFrame(this.tick);

		console.log('[TrafficSimulation] Started.');
	}

	/**
	 * Pause the simulation loop (can be resumed with `start()`).
	 */
	pause(): void {
		this.running = false;
		if (this.animFrame) {
			cancelAnimationFrame(this.animFrame);
			this.animFrame = 0;
		}
		updateSimulationState({ running: false });
		console.log('[TrafficSimulation] Paused.');
	}

	/**
	 * Reset the simulation to time 0.  Vehicles are cleared; call
	 * `initializeVehicles()` again before starting.
	 */
	reset(): void {
		this.pause();
		this.currentTimeSec = 0;
		this.frameCount = 0;
		this.vehicleCount = 0;
		this.currentFps = 0;

		updateSimulationState({
			running: false,
			currentTimeSec: 0,
			totalVehicles: 0,
			avgSpeedKmh: 0,
			avgDelayPerVehicleSec: 0,
			queueLengthM: 0,
			frameCount: 0,
			fps: 0,
		});

		// Clear vehicle buffer
		this.gpu.spawnVehicles(new Float32Array(0));

		console.log('[TrafficSimulation] Reset.');
	}

	/**
	 * Destroy the simulation and release all resources.
	 */
	destroy(): void {
		this.pause();
		this.speedUnsub?.();
		this.speedUnsub = null;
		this.gpu.destroy();
		console.log('[TrafficSimulation] Destroyed.');
	}

	// ─── Accessors for the render layer ──────────────────────────────────────

	/** Returns the GPU vehicle buffer for direct render-pipeline consumption. */
	getVehicleBuffer(): GPUBuffer | null {
		return this.gpu.getVehicleBuffer();
	}

	/** Returns the underlying GPU device. */
	getDevice(): GPUDevice | null {
		return this.gpu.getDevice();
	}

	/** Returns the current vehicle count. */
	getVehicleCount(): number {
		return this.vehicleCount;
	}

	/** Returns projected road segments for overlay rendering. */
	getProjectedSegments(): ProjectedSegment[] {
		return this.projectedSegments;
	}

	/** Returns the projection reference point. */
	getReferencePoint(): { lon: number; lat: number } {
		return { lon: this.refLon, lat: this.refLat };
	}

	/** Whether the simulation is currently running. */
	isRunning(): boolean {
		return this.running;
	}
}
