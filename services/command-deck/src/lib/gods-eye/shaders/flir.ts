/**
 * FLIR (White-Hot / Black-Hot thermal) sensor post-process stage.
 *
 * PORTED FROM gods-eye-view (https://github.com/bilawalsidhu/gods-eye-view),
 * MIT License © bilawalsidhu, src/styles/thermal.js — the thermal fragment
 * shader's temperature mapping, Ironbow palette, coherent temporal noise,
 * hot-spot bloom, sensor pixelation, lens vignette, crosshair HUD and scale
 * bar. Source-level port, not a fork. Trimmed for Veer's ONE sensor mode:
 * the 7-segment digit renderer and frame counter are dropped (the HUD chip
 * shows the sensor state in DOM, not GLSL) and the bloom kernel shrunk to
 * 3x3 so software WebGL (SwiftShader) stays usable on the demo box.
 *
 * Uniforms (defaults follow gev thermal.js):
 *   intensity    (0-1) — blend of the sensor look over the raw render
 *   sensitivity  (0-1) — contrast/range of the temperature mapping
 *   bloom        (0-1) — hot-spot bloom/bleed intensity
 *   mode         (0-1) — 0 = White-Hot, 1 = Black-Hot (step at 0.5)
 *   pixelation   (1-6) — sensor resolution grid size
 *   palette      (0-1) — 0 = monochrome FLIR, 1 = Ironbow "Predator" ramp
 *   time         (s)   — driven by the canvas for coherent noise drift
 */

export interface SensorShader {
	name: string;
	uniforms: Record<string, { default: number; min: number; max: number; label: string }>;
	fragmentShader: string;
}

export const FLIR_SHADER: SensorShader = {
	name: 'flir',
	uniforms: {
		intensity: { default: 0.9, min: 0, max: 1, label: 'Intensity' },
		sensitivity: { default: 0.75, min: 0, max: 1, label: 'Sensitivity' },
		bloom: { default: 0.65, min: 0, max: 1, label: 'Bloom' },
		mode: { default: 0.0, min: 0, max: 1, label: 'WHOT/BHOT' },
		pixelation: { default: 1.5, min: 1, max: 6, label: 'Pixelation' },
		palette: { default: 0.0, min: 0, max: 1, label: 'Ironbow' },
	},
	fragmentShader: /* glsl */ `
		uniform sampler2D colorTexture;
		uniform vec2 colorTextureDimensions;
		uniform float intensity;
		uniform float time;
		uniform float sensitivity;
		uniform float bloom;
		uniform float mode;
		uniform float pixelation;
		uniform float palette;
		in vec2 v_textureCoordinates;

		// ── Ironbow "Predator" thermal palette (gev thermal.js) ──
		vec3 ironbow(float t) {
			t = clamp(t, 0.0, 1.0);
			const vec3 c0 = vec3(0.0, 0.0, 0.0);
			const vec3 c1 = vec3(0.13, 0.0, 0.30);
			const vec3 c2 = vec3(0.49, 0.0, 0.45);
			const vec3 c3 = vec3(0.86, 0.10, 0.18);
			const vec3 c4 = vec3(1.0, 0.55, 0.0);
			const vec3 c5 = vec3(1.0, 0.91, 0.32);
			const vec3 c6 = vec3(1.0, 1.0, 1.0);
			float s = t * 6.0;
			if (s < 1.0) return mix(c0, c1, s);
			if (s < 2.0) return mix(c1, c2, s - 1.0);
			if (s < 3.0) return mix(c2, c3, s - 2.0);
			if (s < 4.0) return mix(c3, c4, s - 3.0);
			if (s < 5.0) return mix(c4, c5, s - 4.0);
			return mix(c5, c6, s - 5.0);
		}

		// ── Value noise (coherent, smooth, drifting) ──
		float hash(vec2 p) {
			vec3 p3 = fract(vec3(p.xyx) * 0.1031);
			p3 += dot(p3, p3.yzx + 33.33);
			return fract((p3.x + p3.y) * p3.z);
		}

		float valueNoise(vec2 p) {
			vec2 i = floor(p);
			vec2 f = fract(p);
			f = f * f * (3.0 - 2.0 * f);
			float a = hash(i);
			float b = hash(i + vec2(1.0, 0.0));
			float c = hash(i + vec2(0.0, 1.0));
			float d = hash(i + vec2(1.0, 1.0));
			return mix(mix(a, b, f.x), mix(c, d, f.x), f.y);
		}

		float fbm(vec2 p) {
			float v = 0.0;
			float a = 0.5;
			vec2 shift = vec2(100.0);
			for (int i = 0; i < 4; i++) {
				v += a * valueNoise(p);
				p = p * 2.0 + shift;
				a *= 0.5;
			}
			return v;
		}

		// ── Crosshair (gev thermal.js) ──
		float crosshair(vec2 uv) {
			vec2 c = uv - 0.5;
			float h = smoothstep(0.0015, 0.0005, abs(c.y)) *
				step(0.015, abs(c.x)) * step(abs(c.x), 0.04);
			float v = smoothstep(0.0015, 0.0005, abs(c.x)) *
				step(0.015, abs(c.y)) * step(abs(c.y), 0.04);
			return clamp(h + v, 0.0, 1.0);
		}

		void main() {
			vec2 uv = v_textureCoordinates;
			vec2 dims = colorTextureDimensions;
			vec2 texel = 1.0 / dims;
			vec2 hudUV = uv;

			// ── Circular vignette mask (FLIR optics field of view) ──
			vec2 centered = uv * 2.0 - 1.0;
			float aspect = dims.x / dims.y;
			centered.x *= aspect;
			float radius = length(centered);
			float lensMask = pow(1.0 - smoothstep(0.6, 1.05, radius), 0.7);
			float lensShading = max(1.0 - radius * radius * 0.25, 0.0);
			if (lensMask < 0.001) {
				out_FragColor = vec4(vec3(0.0), 1.0);
				return;
			}

			// ── Sensor resolution pixelation ──
			float pixSize = mix(1.0, pixelation, intensity);
			vec2 snappedUV = floor(uv * dims / pixSize) * pixSize / dims;
			uv = mix(uv, snappedUV, intensity);

			// ── Soft IR blur (thermal diffraction) ──
			vec3 blurred = vec3(0.0);
			float totalWeight = 0.0;
			for (int y = -2; y <= 2; y++) {
				for (int x = -2; x <= 2; x++) {
					float w = exp(-0.5 * float(x * x + y * y) / 2.0);
					blurred += texture(colorTexture, uv + vec2(float(x), float(y)) * texel * 1.5).rgb * w;
					totalWeight += w;
				}
			}
			blurred /= totalWeight;
			vec4 original = texture(colorTexture, uv);
			vec3 src = mix(original.rgb, blurred, 0.6 * intensity);

			// ── Luminance → temperature mapping ──
			float luma = dot(src, vec3(0.299, 0.587, 0.114));
			float sens = mix(0.25, 1.0, sensitivity);
			float temp = clamp((luma - (0.5 - sens * 0.5)) / sens, 0.0, 1.0);

			// ── Temperature banding (contour lines) ──
			float bands = 12.0;
			float bandLine = abs(fract(temp * bands) - 0.5);
			float contour = smoothstep(0.04, 0.06, bandLine);
			temp *= mix(1.0, contour * 0.85 + 0.15, 0.3 * intensity);

			// ── White-Hot / Black-Hot ──
			float isBlackHot = step(0.5, mode);
			float thermal = mix(temp, 1.0 - temp, isBlackHot);
			vec3 mono = vec3(thermal);
			vec3 iron = ironbow(temp);
			vec3 thermalColor = mix(mono, iron, palette);

			// ── Hot-spot bloom (3x3 kernel — SwiftShader-friendly) ──
			float bloomSample = 0.0;
			float bloomWeight = 0.0;
			for (int y = -1; y <= 1; y++) {
				for (int x = -1; x <= 1; x++) {
					vec2 offset = vec2(float(x), float(y)) * texel * 3.0;
					float sLuma = dot(texture(colorTexture, uv + offset).rgb, vec3(0.299, 0.587, 0.114));
					float sMapped = clamp((sLuma - (0.5 - sens * 0.5)) / sens, 0.0, 1.0);
					float sFinal = mix(sMapped, 1.0 - sMapped, isBlackHot);
					float w = exp(-0.5 * float(x * x + y * y) / 2.0);
					float hotness = smoothstep(0.6, 1.0, sFinal);
					bloomSample += hotness * w;
					bloomWeight += w;
				}
			}
			bloomSample /= bloomWeight;
			thermalColor += bloomSample * bloom * 0.8;

			// ── Coherent temporal noise (drifting cloud) ──
			vec2 noiseCoord = uv * 80.0 + vec2(time * 0.3, time * 0.2);
			float noise = fbm(noiseCoord);
			thermalColor += (noise - 0.5) * 0.08 * intensity;

			// ── HUD: label marker + crosshair + right-edge scale bar ──
			float hud = 0.0;
			vec2 labelArea = (hudUV - vec2(0.02, 0.92)) / vec2(0.08, 0.04);
			if (labelArea.x >= 0.0 && labelArea.x <= 1.0 && labelArea.y >= 0.0 && labelArea.y <= 1.0) {
				hud += step(0.1, labelArea.x) * step(labelArea.x, 0.9) *
					step(0.3, labelArea.y) * step(labelArea.y, 0.7) * 0.6;
			}
			vec2 modeArea = (hudUV - vec2(0.02, 0.88)) / vec2(0.06, 0.03);
			if (modeArea.x >= 0.0 && modeArea.x <= 1.0 && modeArea.y >= 0.0 && modeArea.y <= 1.0) {
				hud += step(0.1, modeArea.x) * step(modeArea.x, 0.9) *
					step(0.2, modeArea.y) * step(modeArea.y, 0.8) * 0.4;
			}
			hud += crosshair(hudUV) * 0.7;
			float barX = step(0.94, hudUV.x) * step(hudUV.x, 0.955);
			float barY = step(0.15, hudUV.y) * step(hudUV.y, 0.85);
			float bar = barX * barY;
			if (bar > 0.0) {
				float barGrad = (hudUV.y - 0.15) / 0.7;
				float barVal = mix(barGrad, 1.0 - barGrad, isBlackHot);
				thermalColor = mix(thermalColor, vec3(barVal), bar * 0.9);
			}
			thermalColor += hud * 0.6 * intensity;

			// ── Lens shading + vignette + final blend ──
			thermalColor *= lensShading;
			thermalColor *= lensMask;
			thermalColor = clamp(thermalColor, 0.0, 1.0);
			vec3 finalColor = mix(original.rgb, thermalColor, intensity);
			finalColor *= mix(1.0, lensMask, intensity);
			out_FragColor = vec4(finalColor, 1.0);
		}
	`,
};
