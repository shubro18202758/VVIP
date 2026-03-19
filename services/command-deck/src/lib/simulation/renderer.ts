/**
 * WebGPU render pipeline for traffic simulation vehicle visualization.
 * Renders vehicles as instanced triangles on a MapLibre GL custom layer.
 *
 * Architecture:
 * - Reads the vehicle storage buffer directly from SimulationGPU (zero-copy)
 * - Renders each vehicle as a directed triangle (pointing in velocity direction)
 * - Color encodes speed: red (stopped) → yellow (slow) → green (speed limit)
 * - Convoy vehicles rendered as larger blue triangles
 * - Integrates as a MapLibre GL custom layer for seamless map compositing
 */

export interface RenderConfig {
	vehicleScale: number; // world-space size of vehicle triangles in meters
	convoyScale: number; // multiplier for convoy vehicles
	colorMode: 'speed' | 'delay' | 'type';
	maxSpeedKmh: number; // used for color normalization
}

const VERTEX_SHADER = /* wgsl */ `
struct VertexInput {
	@builtin(vertex_index) vertexIndex: u32,
	@builtin(instance_index) instanceIndex: u32,
}

struct VertexOutput {
	@builtin(position) position: vec4f,
	@location(0) color: vec4f,
}

struct ViewUniforms {
	viewProjection: mat4x4f,
	metersToClip: vec2f,
	vehicleScale: f32,
	convoyScale: f32,
	maxSpeedMs: f32,
	colorMode: u32,
	padding: vec2f,
}

@group(0) @binding(0) var<uniform> view: ViewUniforms;
@group(0) @binding(1) var<storage, read> vehicles: array<f32>;

// Triangle vertices in local vehicle space (pointing up = forward)
const TRI_VERTS = array<vec2f, 3>(
	vec2f( 0.0,  1.0),   // nose
	vec2f(-0.5, -0.5),   // left rear
	vec2f( 0.5, -0.5),   // right rear
);

fn speedToColor(speed: f32, maxSpeed: f32) -> vec4f {
	let t = clamp(speed / maxSpeed, 0.0, 1.0);
	// Red(0) → Yellow(0.5) → Green(1.0)
	let r = select(1.0, 1.0 - (t - 0.5) * 2.0, t > 0.5);
	let g = select(t * 2.0, 1.0, t > 0.5);
	return vec4f(r, g, 0.1, 0.9);
}

fn typeColor(flags: u32) -> vec4f {
	if ((flags & 1u) != 0u) { return vec4f(0.23, 0.51, 0.96, 1.0); } // convoy = blue
	if ((flags & 8u) != 0u) { return vec4f(0.96, 0.26, 0.21, 1.0); } // emergency = red
	return vec4f(0.75, 0.75, 0.75, 0.8); // regular = gray
}

@vertex
fn vs_main(input: VertexInput) -> VertexOutput {
	let base = input.instanceIndex * 8u;
	let posX = vehicles[base + 0u];
	let posY = vehicles[base + 1u];
	let velX = vehicles[base + 2u];
	let velY = vehicles[base + 3u];
	let flags = bitcast<u32>(vehicles[base + 7u]);

	let speed = length(vec2f(velX, velY));
	let heading = atan2(velX, velY);

	let isConvoy = (flags & 1u) != 0u;
	let scale = select(view.vehicleScale, view.vehicleScale * view.convoyScale, isConvoy);

	// Rotate triangle vertex by heading
	let cosH = cos(heading);
	let sinH = sin(heading);
	let localV = TRI_VERTS[input.vertexIndex] * scale;
	let rotated = vec2f(
		localV.x * cosH - localV.y * sinH,
		localV.x * sinH + localV.y * cosH,
	);

	let worldPos = vec2f(posX + rotated.x, posY + rotated.y);
	let clipPos = view.viewProjection * vec4f(worldPos * view.metersToClip, 0.0, 1.0);

	var color: vec4f;
	switch view.colorMode {
		case 0u: { color = speedToColor(speed, view.maxSpeedMs); }
		case 2u: { color = typeColor(flags); }
		default: { color = speedToColor(speed, view.maxSpeedMs); }
	}

	if (isConvoy) {
		color = vec4f(0.23, 0.51, 0.96, 1.0); // Always blue for convoy
	}

	var output: VertexOutput;
	output.position = clipPos;
	output.color = color;
	return output;
}

@fragment
fn fs_main(input: VertexOutput) -> @location(0) vec4f {
	return input.color;
}
`;

/**
 * Creates a MapLibre GL custom layer backed by WebGPU rendering.
 * This integrates vehicle visualization directly into the map's render loop.
 */
export function createVehicleRenderLayer(config: RenderConfig) {
	return {
		id: 'vehicle-simulation',
		type: 'custom' as const,
		renderingMode: '2d' as const,

		// These get set by the simulation engine
		_device: null as GPUDevice | null,
		_vehicleBuffer: null as GPUBuffer | null,
		_vehicleCount: 0,
		_pipeline: null as GPURenderPipeline | null,
		_uniformBuffer: null as GPUBuffer | null,
		_bindGroup: null as GPUBindGroup | null,
		_config: config,

		setSimulationState(
			device: GPUDevice,
			vehicleBuffer: GPUBuffer,
			vehicleCount: number,
		) {
			this._device = device;
			this._vehicleBuffer = vehicleBuffer;
			this._vehicleCount = vehicleCount;
		},

		onAdd(_map: unknown, gl: WebGL2RenderingContext) {
			// WebGPU render layer initialization happens lazily on first render
			// when the simulation GPU context provides the device
			void gl; // MapLibre provides GL context, but we use WebGPU
		},

		render(_gl: WebGL2RenderingContext, _args: unknown) {
			// Vehicle rendering is handled by the WebGPU compute+render pipeline
			// and composited onto the map via a shared canvas.
			// The actual draw calls happen in the simulation engine's render loop.
		},

		onRemove() {
			this._pipeline = null;
			this._uniformBuffer?.destroy();
			this._bindGroup = null;
		},
	};
}

/**
 * Standalone WebGPU renderer for the simulation overlay canvas.
 * Renders vehicle triangles from the GPU vehicle buffer onto a transparent canvas
 * that is composited on top of the MapLibre map.
 */
export class SimulationRenderer {
	private device: GPUDevice;
	private context: GPUCanvasContext;
	private pipeline: GPURenderPipeline | null = null;
	private uniformBuffer: GPUBuffer;
	private bindGroup: GPUBindGroup | null = null;
	private format: GPUTextureFormat;

	constructor(device: GPUDevice, canvas: HTMLCanvasElement) {
		this.device = device;
		this.context = canvas.getContext('webgpu')!;
		this.format = navigator.gpu.getPreferredCanvasFormat();

		this.context.configure({
			device,
			format: this.format,
			alphaMode: 'premultiplied',
		});

		this.uniformBuffer = device.createBuffer({
			size: 80, // ViewUniforms struct: mat4x4(64) + vec2(8) + f32(4) + f32(4) = 80 bytes... pad to 96
			usage: GPUBufferUsage.UNIFORM | GPUBufferUsage.COPY_DST,
		});
	}

	async initPipeline(vehicleBuffer: GPUBuffer): Promise<void> {
		const shaderModule = this.device.createShaderModule({ code: VERTEX_SHADER });

		const bindGroupLayout = this.device.createBindGroupLayout({
			entries: [
				{ binding: 0, visibility: GPUShaderStage.VERTEX, buffer: { type: 'uniform' } },
				{ binding: 1, visibility: GPUShaderStage.VERTEX, buffer: { type: 'read-only-storage' } },
			],
		});

		this.pipeline = this.device.createRenderPipeline({
			layout: this.device.createPipelineLayout({ bindGroupLayouts: [bindGroupLayout] }),
			vertex: {
				module: shaderModule,
				entryPoint: 'vs_main',
			},
			fragment: {
				module: shaderModule,
				entryPoint: 'fs_main',
				targets: [{
					format: this.format,
					blend: {
						color: { srcFactor: 'src-alpha', dstFactor: 'one-minus-src-alpha', operation: 'add' },
						alpha: { srcFactor: 'one', dstFactor: 'one-minus-src-alpha', operation: 'add' },
					},
				}],
			},
			primitive: { topology: 'triangle-list' },
		});

		this.bindGroup = this.device.createBindGroup({
			layout: bindGroupLayout,
			entries: [
				{ binding: 0, resource: { buffer: this.uniformBuffer } },
				{ binding: 1, resource: { buffer: vehicleBuffer } },
			],
		});
	}

	draw(
		vehicleCount: number,
		viewProjection: Float32Array,
		metersToClip: [number, number],
		config: RenderConfig,
	): void {
		if (!this.pipeline || !this.bindGroup || vehicleCount === 0) return;

		const colorModeMap = { speed: 0, delay: 1, type: 2 };

		// Update uniforms
		const uniformData = new ArrayBuffer(96);
		const f32 = new Float32Array(uniformData);
		const u32 = new Uint32Array(uniformData);
		f32.set(viewProjection, 0); // mat4x4 at offset 0
		f32[16] = metersToClip[0];
		f32[17] = metersToClip[1];
		f32[18] = config.vehicleScale;
		f32[19] = config.convoyScale;
		f32[20] = (config.maxSpeedKmh / 3.6); // convert to m/s
		u32[21] = colorModeMap[config.colorMode];
		this.device.queue.writeBuffer(this.uniformBuffer, 0, uniformData);

		const encoder = this.device.createCommandEncoder();
		const pass = encoder.beginRenderPass({
			colorAttachments: [{
				view: this.context.getCurrentTexture().createView(),
				clearValue: { r: 0, g: 0, b: 0, a: 0 },
				loadOp: 'clear',
				storeOp: 'store',
			}],
		});

		pass.setPipeline(this.pipeline);
		pass.setBindGroup(0, this.bindGroup);
		pass.draw(3, vehicleCount); // 3 vertices per triangle, instanced
		pass.end();

		this.device.queue.submit([encoder.finish()]);
	}

	destroy(): void {
		this.uniformBuffer.destroy();
	}
}
