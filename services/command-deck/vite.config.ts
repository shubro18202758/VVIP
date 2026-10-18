import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export default defineConfig({
	plugins: [sveltekit()],
	server: {
		port: 5173,
		host: '0.0.0.0', // Allow LAN access for testing
		strictPort: true,
		proxy: {
			// Proxy API calls to convoy-brain orchestration service
			'/api/convoy': {
				target: 'http://localhost:8080',
				changeOrigin: true,
				ws: true, // Enable WebSocket proxying
				rewrite: (path) => path.replace(/^\/api\/convoy/, ''),
			},
			// Proxy traffic data calls to traffic-oracle
			'/api/traffic': {
				target: 'http://localhost:8081',
				changeOrigin: true,
			},
			// Proxy v1 API calls (anomalies, corridor summary, segment history)
			'/api/v1': {
				target: 'http://localhost:8081',
				changeOrigin: true,
			},
			// Optional dev-tidiness entry for the event emulator (loopback
			// :8090): same-origin fallback for decks served from an origin the
			// emulator's CORS allowlist does not cover. The primary path stays
			// the absolute http://localhost:8090 constant (contract C6).
			'/api/emulator': {
				target: 'http://localhost:8090',
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
