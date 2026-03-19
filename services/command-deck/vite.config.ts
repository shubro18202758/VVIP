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
		// Chunk strategy: keep vendor libs separate for caching
		rollupOptions: {
			output: {
				manualChunks: {
					'maplibre': ['maplibre-gl'],
					'deckgl': [
						'@deck.gl/core',
						'@deck.gl/layers',
						'@deck.gl/aggregation-layers',
						'@deck.gl/geo-layers',
						'@deck.gl/mapbox',
					],
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
