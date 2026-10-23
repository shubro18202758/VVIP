import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

// Dev proxy targets — the SAME pinned loopback hosts and env-overridable
// ports as the adapter-node proxy (src/hooks.server.ts): oracle 8091,
// brain 8092, emulator 8090 by default (the retired 8081/8080 stack is
// gone). Note the dev proxy answers before SvelteKit, so the hook's
// authority pinning, WebSocket 501 and operator gate apply in prod only.
const ORACLE_TARGET = `http://127.0.0.1:${process.env.ORACLE_PORT ?? '8091'}`;
const BRAIN_TARGET = `http://127.0.0.1:${process.env.BRAIN_PORT ?? '8092'}`;
const EMULATOR_TARGET = `http://127.0.0.1:${process.env.EMULATOR_PORT ?? '8090'}`;

export default defineConfig({
	plugins: [sveltekit()],
	server: {
		port: 5173,
		host: '0.0.0.0', // Allow LAN access for testing
		strictPort: true,
		proxy: {
			// Proxy API calls to convoy-brain orchestration service
			'/api/convoy': {
				target: BRAIN_TARGET,
				changeOrigin: true,
				ws: true, // Enable WebSocket proxying (dev only — prod answers 501)
				rewrite: (path) => path.replace(/^\/api\/convoy/, ''),
			},
			// Proxy traffic data calls to traffic-oracle
			'/api/traffic': {
				target: ORACLE_TARGET,
				changeOrigin: true,
			},
			// Proxy v1 API calls (events, spatial, dispatch, adaptive, …)
			'/api/v1': {
				target: ORACLE_TARGET,
				changeOrigin: true,
			},
			// Event emulator (loopback :8090) behind the same-origin prefix.
			'/api/emulator': {
				target: EMULATOR_TARGET,
				changeOrigin: true,
				rewrite: (path) => path.replace(/^\/api\/emulator/, ''),
			},
		},
	},

	// Allow .wgsl shader imports as raw strings
	assetsInclude: ['**/*.wgsl'],

	// ─── CPU-Only Build Optimization ──────────────────────────────────
	// Preserves GPU VRAM entirely for backend AI logic (Qwen, ONNX, DQN).
	build: {
		// Disable source maps in production — saves ~40% build memory
		sourcemap: false,
		// Limit parallel operations to reduce peak memory
		minify: 'esbuild', // esbuild is CPU-only, faster than terser
		// Chunk strategy: keep vendor libs separate for caching.
		// Function form — the object form (package-name keys) hard-fails on
		// vite/rollup versions that resolve these dynamic-imported packages
		// as external (observed: "maplibre-gl cannot be included in
		// manualChunks because it is resolved as an external module").
		rollupOptions: {
			output: {
				manualChunks(id: string) {
					if (id.includes('maplibre-gl')) return 'maplibre';
					if (id.includes('@deck.gl')) return 'deckgl';
					return undefined;
				},
			},
		},
		// Target modern browsers only — skip polyfill bloat
		target: 'esnext',
		// Warn on chunks > 500KB
		chunkSizeWarningLimit: 500,
	},

	// esbuild runs entirely on CPU — no GPU acceleration possible
	esbuild: {
		// target esnext avoids unnecessary transforms
		target: 'esnext',
	},
});
