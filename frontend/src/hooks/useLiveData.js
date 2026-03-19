import { useState, useEffect, useRef, useCallback } from 'react';
import * as api from '../services/api';

/**
 * Generic polling hook – fetches data at interval, with loading/error states.
 */
export function usePolling(fetchFn, intervalMs = 15000, deps = []) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const mountedRef = useRef(true);

  const doFetch = useCallback(async () => {
    try {
      const result = await fetchFn();
      if (mountedRef.current) {
        setData(result);
        setError(null);
      }
    } catch (err) {
      if (mountedRef.current) setError(err);
    } finally {
      if (mountedRef.current) setLoading(false);
    }
  }, [fetchFn]);

  useEffect(() => {
    mountedRef.current = true;
    doFetch();
    const id = setInterval(doFetch, intervalMs);
    return () => {
      mountedRef.current = false;
      clearInterval(id);
    };
  }, [doFetch, intervalMs, ...deps]);

  return { data, loading, error, refetch: doFetch };
}

/**
 * Live corridor summary — polls every 10s
 */
export function useCorridorSummary() {
  return usePolling(
    async () => {
      const res = await api.getCorridorSummary();
      return res.data;
    },
    10000
  );
}

/**
 * Active movements with WebSocket position tracking
 */
export function useActiveMovements() {
  const [movements, setMovements] = useState([]);
  const wsRefs = useRef(new Map());

  useEffect(() => {
    let active = true;
    const poll = async () => {
      try {
        const res = await api.getActiveMovements();
        if (active) setMovements(res.data.movements || []);
      } catch {
        // silent
      }
    };
    poll();
    const id = setInterval(poll, 15000);
    return () => { active = false; clearInterval(id); };
  }, []);

  // WebSocket tracking for live positions
  useEffect(() => {
    movements.forEach((mov) => {
      const mid = mov.movement_id;
      if (!mid || wsRefs.current.has(mid)) return;
      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      const ws = new WebSocket(`${protocol}//${window.location.host}/api/agent/ws/convoy/${mid}`);
      ws.onmessage = (e) => {
        try {
          const data = JSON.parse(e.data);
          if (data.type === 'convoy.position') {
            setMovements((prev) =>
              prev.map((m) =>
                m.movement_id === mid
                  ? { ...m, current_position: { lat: data.payload.position[1], lng: data.payload.position[0] } }
                  : m
              )
            );
          }
        } catch { /* ignore parse errors */ }
      };
      wsRefs.current.set(mid, ws);
    });
  }, [movements]);

  // Cleanup sockets on unmount
  useEffect(() => {
    return () => {
      wsRefs.current.forEach((ws) => { try { ws.close(); } catch {} });
      wsRefs.current.clear();
    };
  }, []);

  return movements;
}

/**
 * Recent anomalies — polls every 15s
 */
export function useAnomalies(limit = 10) {
  return usePolling(
    async () => {
      const res = await api.getRecentAnomalies(limit);
      return (res.data?.anomalies || []).map((a, idx) => ({
        id: a.anomaly_id || idx,
        segment_id: a.segment_id,
        type: a.anomaly_type,
        severity: a.severity,
        tag: a.anomaly_type.toUpperCase().replace(/_/g, ' '),
        message: a.details?.description || `Severity ${a.severity} at segment ${a.segment_id}`,
        time: new Date(a.timestamp_utc).toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }),
        timestamp: a.timestamp_utc,
        read: false,
      }));
    },
    15000
  );
}

/**
 * GPU health status — polls every 10s
 */
export function useGpuHealth() {
  return usePolling(
    async () => {
      const res = await api.getGpuHealth();
      return res.data;
    },
    10000
  );
}

/**
 * Backend health — polls every 30s
 */
export function useBackendHealth() {
  return usePolling(
    async () => {
      const res = await api.getHealth();
      return res.data;
    },
    30000
  );
}

/**
 * Traffic flow prediction for given segments
 */
export function useFlowPrediction(segmentIds) {
  const [predictions, setPredictions] = useState(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!segmentIds || segmentIds.length === 0) return;
    let active = true;
    const fetch = async () => {
      setLoading(true);
      try {
        const res = await api.predictFlow(segmentIds);
        if (active) setPredictions(res.data);
      } catch { /* silent */ }
      finally { if (active) setLoading(false); }
    };
    fetch();
    const id = setInterval(fetch, 60000); // refresh every 60s
    return () => { active = false; clearInterval(id); };
  }, [JSON.stringify(segmentIds)]);

  return { predictions, loading };
}

/**
 * ETA prediction hook
 */
export function useEtaPrediction(routeData) {
  const [eta, setEta] = useState(null);
  const [loading, setLoading] = useState(false);

  const predict = useCallback(async (data) => {
    setLoading(true);
    try {
      const res = await api.predictEta(data || routeData);
      setEta(res.data);
    } catch { /* silent */ }
    finally { setLoading(false); }
  }, [routeData]);

  return { eta, loading, predict };
}

/**
 * Live traffic for given segments — polls every 30s
 */
export function useLiveTraffic(segmentIds) {
  const [traffic, setTraffic] = useState(null);

  useEffect(() => {
    if (!segmentIds || segmentIds.length === 0) return;
    let active = true;
    const fetch = async () => {
      try {
        const res = await api.getLiveTraffic(segmentIds);
        if (active) setTraffic(res.data);
      } catch { /* silent */ }
    };
    fetch();
    const id = setInterval(fetch, 30000);
    return () => { active = false; clearInterval(id); };
  }, [JSON.stringify(segmentIds)]);

  return traffic;
}
