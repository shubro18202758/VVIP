/**
 * Centralized API service for all backend endpoints.
 * convoy-brain: /api/agent/* (port 8082)
 * traffic-oracle: /api/v1/*   (port 8081)
 * Vite proxy handles routing — see vite.config.js
 */
import axios from 'axios';

const api = axios.create({ timeout: 30000 });

// ─── convoy-brain endpoints (port 8082) ───────────────────────────

export const getHealth = () => api.get('/api/agent/health');
export const getGpuHealth = () => api.get('/api/agent/health/gpu');

export const createMovement = (data) =>
  api.post('/api/agent/movements', data);

export const planMovement = (movementId, data) =>
  api.post(`/api/agent/movements/${movementId}/plan`, data, { timeout: 300000 });

export const startEscort = (movementId, data) =>
  api.post(`/api/agent/movements/${movementId}/escort`, data, { timeout: 300000 });

export const clearMovement = (movementId) =>
  api.post(`/api/agent/movements/${movementId}/clear`);

// Blue Book protocol management
export const getProtocolState = (movementId) =>
  api.get(`/api/agent/movements/${movementId}/protocol`);

export const updateProtocolState = (movementId, data) =>
  api.post(`/api/agent/movements/${movementId}/protocol`, data);

export const assessProtocol = (movementId) =>
  api.post(`/api/agent/movements/${movementId}/protocol/assess`, {}, { timeout: 300000 });

export const generateDossier = (movementId, data) =>
  api.post(`/api/agent/movements/${movementId}/dossier`, data, { timeout: 300000 });

export const getThreatAssessment = (movementId) =>
  api.post(`/api/agent/movements/${movementId}/threat-assessment`, {}, { timeout: 300000 });

export const sendChat = (data) =>
  api.post('/api/agent/chat', data, { timeout: 300000 });

// Streaming chat — returns raw fetch Response for NDJSON
export const streamChat = (data) =>
  fetch('/chat/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });

// ─── traffic-oracle endpoints (port 8081) ─────────────────────────

// Corridor & monitoring
export const getCorridorSummary = () => api.get('/api/v1/corridor/summary');
export const getActiveMovements = () =>
  api.get('/api/v1/movements/active').catch(() => ({ data: { movements: [] } }));
export const getRecentAnomalies = (limit = 10) =>
  api.get(`/api/v1/anomalies/recent?limit=${limit}`).catch(() => ({ data: { anomalies: [] } }));

// Predictions
export const predictFlow = (segmentIds, horizons = [5, 10, 15, 30]) =>
  api.post('/api/v1/predict/flow', { segment_ids: segmentIds, horizons_min: horizons });
export const predictEta = (data) =>
  api.post('/api/v1/predict/eta', data);

// Routing
export const findRoutes = (data) =>
  api.post('/api/v1/optimize/routes', data);
export const planDiversions = (data) =>
  api.post('/api/v1/optimize/diversions', data);
export const evaluateScenarios = (data) =>
  api.post('/api/v1/evaluate/scenarios', data);

// Spatial queries
export const getShortestPath = (source, target) =>
  api.get(`/api/v1/graph/shortest-path?source=${source}&target=${target}`);
export const getKShortestPaths = (source, target, k = 3) =>
  api.get(`/api/v1/graph/k-shortest-paths?source=${source}&target=${target}&k=${k}`);
export const getSegmentsInBbox = (minLon, minLat, maxLon, maxLat) =>
  api.get(`/api/v1/spatial/segments?min_lon=${minLon}&min_lat=${minLat}&max_lon=${maxLon}&max_lat=${maxLat}`);
export const getSegmentDetails = (id) =>
  api.get(`/api/v1/spatial/segments/${id}`);

// Traffic data
export const getLiveTraffic = (segmentIds) =>
  api.post('/api/v1/traffic/live', { segment_ids: segmentIds });
export const getHistoricalPattern = (segmentId, patternType = 'daily_profile') =>
  api.get(`/api/v1/traffic/historical?segment_id=${segmentId}&pattern_type=${patternType}`);
export const getSegmentHistory = (segmentId) =>
  api.get(`/api/v1/traffic/history/${segmentId}`);

// Deep-dive reasoning (Qwen 3.5 powered) — extended timeout for LLM inference
// 5-min timeout: LLM inference (~30-60s) + potential asyncio.Lock queueing behind other requests
export const analyticsReasoning = (metricName, metricValue, metricContext = {}, vvipClass = 'Z') =>
  api.post('/api/agent/analytics/reasoning', {
    metric_name: metricName,
    metric_value: metricValue,
    metric_context: metricContext,
    vvip_class: vvipClass,
  }, { timeout: 300000 });
export const recommendationReasoning = (statement, category = 'general', groundData = {}, vvipClass = 'Z') =>
  api.post('/api/agent/recommendations/reasoning', {
    statement,
    category,
    ground_data: groundData,
    vvip_class: vvipClass,
  }, { timeout: 300000 });

export default api;
