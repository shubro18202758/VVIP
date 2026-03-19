/**
 * gpu_context.ts — WebGPU device and compute pipeline manager for the
 * microscopic traffic simulation.
 *
 * Key design decisions:
 *  - Requests the "low-power" adapter first so we run on the integrated GPU
 *    and avoid competing with Qwen + ONNX for discrete VRAM.
 *  - Falls back to "high-performance" only if integrated is unavailable, but
 *    caps maxBufferSize to 64 MB to stay inside the ~1.5 GB VRAM headroom.
 *  - All buffers are pre-allocated at init time for deterministic memory use.
 */

import vehiclePhysicsWGSL from './shaders/vehicle_physics.wgsl?raw';
import reduceStatsWGSL from './shaders/reduce_stats.wgsl?raw';

// ─── Public Types ──────────────────────────────────────────────────────────

export interface IDMParams {
	desiredSpeed: number;   // m/s (default 13.9 ~ 50 km/h)
	timeHeadway: number;    // s   (default 1.5)
	minGap: number;         // m   (default 2.0)
	maxAccel: number;       // m/s^2 (default 2.0)
	comfortDecel: number;   // m/s^2 (default 3.0)
	delta: number;          // exponent (default 4.0)
	vehicleLength: number;  // m   (default 4.5)
	dt: number;             // simulation timestep in seconds
}

export interface SimulationStats {
	avgSpeedMs: number;
	stoppedCount: number;
	maxQueueLength: number;
	processedCount: number;
}

// ─── Constants ─────────────────────────────────────────────────────────────

/** Bytes per vehicle in the GPU storage buffer (8 x f32) */
const VEHICLE_STRIDE = 32;

/** Bytes per road segment in the GPU storage buffer (12 x f32) */
const SEGMENT_STRIDE = 48;

/** Bytes for the IDM params uniform (8 x f32) */
const PARAMS_SIZE = 32;

/** Bytes for the SimMeta uniform (vehicleCount:u32, segmentCount:u32, simTime:f32, pad:f32) */
const META_SIZE = 16;

/** Stats buffer: 8 x u32 = 32 bytes */
const STATS_SIZE = 32;

/** Maximum buffer size requested from the device (64 MB) */
const MAX_BUFFER_SIZE = 64 * 1024 * 1024;

/** Compute workgroup size — must match the WGSL @workgroup_size */
const WORKGROUP_SIZE = 256;

// ─── Class ─────────────────────────────────────────────────────────────────

export class SimulationGPU {
	private adapter: GPUAdapter | null = null;
	private device: GPUDevice | null = null;

	// Compute pipelines
	private physicsPipeline: GPUComputePipeline | null = null;
	private laneChangePipeline: GPUComputePipeline | null = null;
	private clearStatsPipeline: GPUComputePipeline | null = null;
	private reduceStatsPipeline: GPUComputePipeline | null = null;

	// Storage / uniform buffers
	private vehicleBuffer: GPUBuffer | null = null;
	private segmentBuffer: GPUBuffer | null = null;
	private paramsBuffer: GPUBuffer | null = null;
	private metaBuffer: GPUBuffer | null = null;
	private statsBuffer: GPUBuffer | null = null;
	private readbackBuffer: GPUBuffer | null = null;

	// Bind groups
	private physicsBindGroup: GPUBindGroup | null = null;
	private statsBindGroup: GPUBindGroup | null = null;
	private clearStatsBindGroup: GPUBindGroup | null = null;

	// Capacities
	private maxVehicles = 0;
	private maxSegments = 0;
	private currentVehicleCount = 0;
	private currentSegmentCount = 0;
	private simTime = 0;

	/** Human-readable adapter description (for the UI health panel) */
	public adapterInfo = '';

	// ─── Initialization ──────────────────────────────────────────────────────

	/**
	 * Request a WebGPU adapter and device, compile shaders, allocate buffers.
	 *
	 * @param maxVehicles  Maximum number of vehicles to allocate for
	 * @param maxSegments  Maximum number of road segments
	 * @returns `true` if initialization succeeded
	 */
	async initialize(maxVehicles: number, maxSegments: number): Promise<boolean> {
		if (!navigator.gpu) {
			console.warn('[SimulationGPU] WebGPU not available in this browser.');
			return false;
		}

		// 1. Request adapter — prefer integrated GPU to avoid VRAM competition
		this.adapter = await navigator.gpu.requestAdapter({
			powerPreference: 'low-power',
		});

		if (!this.adapter) {
			console.warn(
				'[SimulationGPU] No low-power adapter found, trying high-performance fallback.',
			);
			this.adapter = await navigator.gpu.requestAdapter({
				powerPreference: 'high-performance',
			});
		}

		if (!this.adapter) {
			console.error('[SimulationGPU] No WebGPU adapter available.');
			return false;
		}

		// Store adapter info for diagnostics
		const info = await this.adapter.requestAdapterInfo();
		this.adapterInfo = `${info.vendor} — ${info.device} (${info.architecture})`;
		console.log(`[SimulationGPU] Using adapter: ${this.adapterInfo}`);

		// 2. Request device with limited buffer size
		try {
			this.device = await this.adapter.requestDevice({
				requiredLimits: {
					maxBufferSize: MAX_BUFFER_SIZE,
					maxStorageBufferBindingSize: MAX_BUFFER_SIZE,
					maxComputeWorkgroupsPerDimension: 65535,
				},
			});
		} catch {
			// If the limit request fails, try with defaults
			console.warn('[SimulationGPU] Could not apply custom limits, using adapter defaults.');
			this.device = await this.adapter.requestDevice();
		}

		if (!this.device) {
			console.error('[SimulationGPU] Failed to create GPUDevice.');
			return false;
		}

		// Handle device loss
		this.device.lost.then((info) => {
			console.error(`[SimulationGPU] Device lost: ${info.message}`);
			this.destroy();
		});

		this.maxVehicles = maxVehicles;
		this.maxSegments = maxSegments;

		// 3. Allocate GPU buffers
		this.allocateBuffers();

		// 4. Compile shaders and create pipelines
		this.createPipelines();

		// 5. Create initial bind groups
		this.createBindGroups();

		console.log(
			`[SimulationGPU] Initialized: ${maxVehicles} vehicles, ${maxSegments} segments ` +
			`(${((maxVehicles * VEHICLE_STRIDE + maxSegments * SEGMENT_STRIDE) / 1024).toFixed(0)} KB GPU memory)`,
		);

		return true;
	}

	// ─── Buffer allocation ───────────────────────────────────────────────────

	private allocateBuffers(): void {
		const d = this.device!;

		this.vehicleBuffer = d.createBuffer({
			label: 'vehicle-storage',
			size: this.maxVehicles * VEHICLE_STRIDE,
			usage: GPUBufferUsage.STORAGE | GPUBufferUsage.COPY_DST | GPUBufferUsage.COPY_SRC,
		});

		this.segmentBuffer = d.createBuffer({
			label: 'segment-storage',
			size: this.maxSegments * SEGMENT_STRIDE,
			usage: GPUBufferUsage.STORAGE | GPUBufferUsage.COPY_DST,
		});

		this.paramsBuffer = d.createBuffer({
			label: 'idm-params-uniform',
			size: PARAMS_SIZE,
			usage: GPUBufferUsage.UNIFORM | GPUBufferUsage.COPY_DST,
		});

		this.metaBuffer = d.createBuffer({
			label: 'sim-meta-uniform',
			size: META_SIZE,
			usage: GPUBufferUsage.UNIFORM | GPUBufferUsage.COPY_DST,
		});

		this.statsBuffer = d.createBuffer({
			label: 'stats-storage',
			size: STATS_SIZE,
			usage: GPUBufferUsage.STORAGE | GPUBufferUsage.COPY_SRC,
		});

		this.readbackBuffer = d.createBuffer({
			label: 'stats-readback',
			size: STATS_SIZE,
			usage: GPUBufferUsage.MAP_READ | GPUBufferUsage.COPY_DST,
		});
	}

	// ─── Pipeline creation ───────────────────────────────────────────────────

	private createPipelines(): void {
		const d = this.device!;

		// --- Physics shader module (IDM + lane change) ---
		const physicsModule = d.createShaderModule({
			label: 'vehicle-physics',
			code: vehiclePhysicsWGSL,
		});

		// Physics bind group layout: vehicles(rw), segments(r), params(u), meta(u)
		const physicsBindGroupLayout = d.createBindGroupLayout({
			label: 'physics-bgl',
			entries: [
				{ binding: 0, visibility: GPUShaderStage.COMPUTE, buffer: { type: 'storage' } },
				{ binding: 1, visibility: GPUShaderStage.COMPUTE, buffer: { type: 'read-only-storage' } },
				{ binding: 2, visibility: GPUShaderStage.COMPUTE, buffer: { type: 'uniform' } },
				{ binding: 3, visibility: GPUShaderStage.COMPUTE, buffer: { type: 'uniform' } },
			],
		});

		const physicsPipelineLayout = d.createPipelineLayout({
			label: 'physics-pl',
			bindGroupLayouts: [physicsBindGroupLayout],
		});

		this.physicsPipeline = d.createComputePipeline({
			label: 'idm-physics',
			layout: physicsPipelineLayout,
			compute: { module: physicsModule, entryPoint: 'physics_main' },
		});

		this.laneChangePipeline = d.createComputePipeline({
			label: 'mobil-lane-change',
			layout: physicsPipelineLayout,
			compute: { module: physicsModule, entryPoint: 'lane_change_main' },
		});

		// --- Stats reduction shader module ---
		const statsModule = d.createShaderModule({
			label: 'reduce-stats',
			code: reduceStatsWGSL,
		});

		// Stats bind group layout: vehicles(r), stats(rw), meta(u)
		const statsBindGroupLayout = d.createBindGroupLayout({
			label: 'stats-bgl',
			entries: [
				{ binding: 0, visibility: GPUShaderStage.COMPUTE, buffer: { type: 'read-only-storage' } },
				{ binding: 1, visibility: GPUShaderStage.COMPUTE, buffer: { type: 'storage' } },
				{ binding: 2, visibility: GPUShaderStage.COMPUTE, buffer: { type: 'uniform' } },
			],
		});

		const statsPipelineLayout = d.createPipelineLayout({
			label: 'stats-pl',
			bindGroupLayouts: [statsBindGroupLayout],
		});

		this.reduceStatsPipeline = d.createComputePipeline({
			label: 'reduce-stats',
			layout: statsPipelineLayout,
			compute: { module: statsModule, entryPoint: 'reduce_main' },
		});

		// Clear-stats pipeline reuses the same bind group layout
		// (only touches binding 1 = stats, but layout must match)
		this.clearStatsPipeline = d.createComputePipeline({
			label: 'clear-stats',
			layout: statsPipelineLayout,
			compute: { module: statsModule, entryPoint: 'clear_stats' },
		});
	}

	// ─── Bind group creation ─────────────────────────────────────────────────

	private createBindGroups(): void {
		const d = this.device!;

		// Physics bind group
		this.physicsBindGroup = d.createBindGroup({
			label: 'physics-bg',
			layout: this.physicsPipeline!.getBindGroupLayout(0),
			entries: [
				{ binding: 0, resource: { buffer: this.vehicleBuffer! } },
				{ binding: 1, resource: { buffer: this.segmentBuffer! } },
				{ binding: 2, resource: { buffer: this.paramsBuffer! } },
				{ binding: 3, resource: { buffer: this.metaBuffer! } },
			],
		});

		// Stats bind group
		this.statsBindGroup = d.createBindGroup({
			label: 'stats-bg',
			layout: this.reduceStatsPipeline!.getBindGroupLayout(0),
			entries: [
				{ binding: 0, resource: { buffer: this.vehicleBuffer! } },
				{ binding: 1, resource: { buffer: this.statsBuffer! } },
				{ binding: 2, resource: { buffer: this.metaBuffer! } },
			],
		});

		// Clear stats reuses the same layout
		this.clearStatsBindGroup = d.createBindGroup({
			label: 'clear-stats-bg',
			layout: this.clearStatsPipeline!.getBindGroupLayout(0),
			entries: [
				{ binding: 0, resource: { buffer: this.vehicleBuffer! } },
				{ binding: 1, resource: { buffer: this.statsBuffer! } },
				{ binding: 2, resource: { buffer: this.metaBuffer! } },
			],
		});
	}

	// ─── Data upload ─────────────────────────────────────────────────────────

	/**
	 * Upload road segment data to the GPU.
	 * @param segments Float32Array of packed segment data (12 floats per segment)
	 */
	updateSegments(segments: Float32Array): void {
		if (!this.device || !this.segmentBuffer) return;
		const count = segments.length / 12;
		if (count > this.maxSegments) {
			console.error(`[SimulationGPU] Segment count ${count} exceeds max ${this.maxSegments}`);
			return;
		}
		this.currentSegmentCount = count;
		this.device.queue.writeBuffer(this.segmentBuffer, 0, segments);
		this.writeMetaBuffer();
	}

	/**
	 * Upload IDM parameters to the GPU uniform buffer.
	 */
	updateParams(params: IDMParams): void {
		if (!this.device || !this.paramsBuffer) return;
		const data = new Float32Array([
			params.desiredSpeed,
			params.timeHeadway,
			params.minGap,
			params.maxAccel,
			params.comfortDecel,
			params.delta,
			params.vehicleLength,
			params.dt,
		]);
		this.device.queue.writeBuffer(this.paramsBuffer, 0, data);
	}

	/**
	 * Upload initial vehicle positions and states to the GPU.
	 * @param vehicles Float32Array of packed vehicle data (8 floats per vehicle)
	 */
	spawnVehicles(vehicles: Float32Array): void {
		if (!this.device || !this.vehicleBuffer) return;
		const count = vehicles.length / 8;
		if (count > this.maxVehicles) {
			console.error(`[SimulationGPU] Vehicle count ${count} exceeds max ${this.maxVehicles}`);
			return;
		}
		this.currentVehicleCount = count;
		this.device.queue.writeBuffer(this.vehicleBuffer, 0, vehicles);
		this.writeMetaBuffer();
	}

	/**
	 * Update the simulation time in the meta buffer (called each tick).
	 */
	updateSimTime(timeSec: number): void {
		this.simTime = timeSec;
		this.writeMetaBuffer();
	}

	private writeMetaBuffer(): void {
		if (!this.device || !this.metaBuffer) return;
		const buf = new ArrayBuffer(META_SIZE);
		const u32 = new Uint32Array(buf);
		const f32 = new Float32Array(buf);
		u32[0] = this.currentVehicleCount;
		u32[1] = this.currentSegmentCount;
		f32[2] = this.simTime;
		f32[3] = 0; // padding
		this.device.queue.writeBuffer(this.metaBuffer, 0, new Uint8Array(buf));
	}

	// ─── Compute dispatch ────────────────────────────────────────────────────

	/**
	 * Run one simulation step: physics + lane change compute passes.
	 */
	async step(): Promise<void> {
		if (
			!this.device ||
			!this.physicsPipeline ||
			!this.laneChangePipeline ||
			!this.physicsBindGroup ||
			this.currentVehicleCount === 0
		) {
			return;
		}

		const workgroups = Math.ceil(this.currentVehicleCount / WORKGROUP_SIZE);

		const encoder = this.device.createCommandEncoder({ label: 'sim-step' });

		// Pass 1: IDM physics
		const physicsPass = encoder.beginComputePass({ label: 'idm-physics' });
		physicsPass.setPipeline(this.physicsPipeline);
		physicsPass.setBindGroup(0, this.physicsBindGroup);
		physicsPass.dispatchWorkgroups(workgroups);
		physicsPass.end();

		// Pass 2: MOBIL lane change
		const lanePass = encoder.beginComputePass({ label: 'lane-change' });
		lanePass.setPipeline(this.laneChangePipeline);
		lanePass.setBindGroup(0, this.physicsBindGroup);
		lanePass.dispatchWorkgroups(workgroups);
		lanePass.end();

		this.device.queue.submit([encoder.finish()]);
	}

	/**
	 * Run the stats reduction pipeline and copy results to the readback buffer.
	 * Call `readStats()` after this to retrieve the values on the CPU.
	 */
	async computeStats(): Promise<void> {
		if (
			!this.device ||
			!this.clearStatsPipeline ||
			!this.reduceStatsPipeline ||
			!this.clearStatsBindGroup ||
			!this.statsBindGroup ||
			!this.statsBuffer ||
			!this.readbackBuffer ||
			this.currentVehicleCount === 0
		) {
			return;
		}

		const workgroups = Math.ceil(this.currentVehicleCount / WORKGROUP_SIZE);

		const encoder = this.device.createCommandEncoder({ label: 'stats' });

		// Clear stats buffer
		const clearPass = encoder.beginComputePass({ label: 'clear-stats' });
		clearPass.setPipeline(this.clearStatsPipeline);
		clearPass.setBindGroup(0, this.clearStatsBindGroup);
		clearPass.dispatchWorkgroups(1);
		clearPass.end();

		// Reduce
		const reducePass = encoder.beginComputePass({ label: 'reduce-stats' });
		reducePass.setPipeline(this.reduceStatsPipeline);
		reducePass.setBindGroup(0, this.statsBindGroup);
		reducePass.dispatchWorkgroups(workgroups);
		reducePass.end();

		// Copy stats buffer to readback buffer
		encoder.copyBufferToBuffer(this.statsBuffer, 0, this.readbackBuffer, 0, STATS_SIZE);

		this.device.queue.submit([encoder.finish()]);
	}

	/**
	 * Map the readback buffer and parse aggregate simulation statistics.
	 * Must be called after `computeStats()` and the GPU submission has completed.
	 */
	async readStats(): Promise<SimulationStats> {
		const empty: SimulationStats = {
			avgSpeedMs: 0,
			stoppedCount: 0,
			maxQueueLength: 0,
			processedCount: 0,
		};

		if (!this.readbackBuffer) return empty;

		try {
			await this.readbackBuffer.mapAsync(GPUMapMode.READ);
			const data = new Uint32Array(this.readbackBuffer.getMappedRange().slice(0));
			this.readbackBuffer.unmap();

			const totalSpeedFixed = data[0]; // sum of speeds * 1000
			const stoppedCount = data[1];
			const maxQueueLength = data[2];
			const processedCount = data[3];

			const avgSpeedMs =
				processedCount > 0 ? totalSpeedFixed / 1000.0 / processedCount : 0;

			return {
				avgSpeedMs,
				stoppedCount,
				maxQueueLength,
				processedCount,
			};
		} catch (e) {
			console.warn('[SimulationGPU] readStats failed:', e);
			return empty;
		}
	}

	// ─── Accessors ───────────────────────────────────────────────────────────

	/**
	 * Returns the vehicle GPU buffer so the render pipeline can consume it
	 * directly (e.g. as a vertex/instance buffer in a WebGPU render pass).
	 */
	getVehicleBuffer(): GPUBuffer | null {
		return this.vehicleBuffer;
	}

	/** Returns the underlying GPUDevice (for the render pipeline). */
	getDevice(): GPUDevice | null {
		return this.device;
	}

	/** Current number of active vehicles in the simulation. */
	getVehicleCount(): number {
		return this.currentVehicleCount;
	}

	/** Whether the GPU context is initialized and ready. */
	isReady(): boolean {
		return this.device !== null && this.vehicleBuffer !== null;
	}

	// ─── Cleanup ─────────────────────────────────────────────────────────────

	/**
	 * Release all GPU resources.
	 */
	destroy(): void {
		this.vehicleBuffer?.destroy();
		this.segmentBuffer?.destroy();
		this.paramsBuffer?.destroy();
		this.metaBuffer?.destroy();
		this.statsBuffer?.destroy();
		this.readbackBuffer?.destroy();

		this.vehicleBuffer = null;
		this.segmentBuffer = null;
		this.paramsBuffer = null;
		this.metaBuffer = null;
		this.statsBuffer = null;
		this.readbackBuffer = null;

		this.physicsPipeline = null;
		this.laneChangePipeline = null;
		this.clearStatsPipeline = null;
		this.reduceStatsPipeline = null;

		this.physicsBindGroup = null;
		this.statsBindGroup = null;
		this.clearStatsBindGroup = null;

		this.device?.destroy();
		this.device = null;
		this.adapter = null;

		console.log('[SimulationGPU] Destroyed all GPU resources.');
	}
}
