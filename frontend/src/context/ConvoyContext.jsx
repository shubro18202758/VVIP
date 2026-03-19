import React, { createContext, useContext, useState, useCallback, useRef, useEffect } from 'react';
import * as api from '../services/api';

const ConvoyContext = createContext(null);

export function useConvoy() {
  const ctx = useContext(ConvoyContext);
  if (!ctx) throw new Error('useConvoy must be used within ConvoyProvider');
  return ctx;
}

/**
 * Convoy lifecycle states: idle → planning → approved → active → completed
 */
export function ConvoyProvider({ children }) {
  // ─── Convoy lifecycle ──────────────────────────────────────────
  const [movementId, setMovementId] = useState(null);
  const [lifecycle, setLifecycle] = useState('idle'); // idle | planning | approved | active | completed
  const [planResult, setPlanResult] = useState(null);
  const [escortResult, setEscortResult] = useState(null);
  const [clearResult, setClearResult] = useState(null);
  const [lifecycleError, setLifecycleError] = useState(null);

  // ─── Global data ───────────────────────────────────────────────
  const [corridorSummary, setCorridorSummary] = useState(null);
  const [activeMovements, setActiveMovements] = useState([]);
  const [anomalies, setAnomalies] = useState([]);
  const [gpuHealth, setGpuHealth] = useState(null);
  const [backendHealth, setBackendHealth] = useState(null);

  // ─── AI reasoning log ──────────────────────────────────────────
  const [aiReasoning, setAiReasoning] = useState([]);
  const [chatMessages, setChatMessages] = useState([]);
  const [chatStreaming, setChatStreaming] = useState(false);

  // ─── Map interaction state (shared between map ↔ sidebars) ─────
  const [mapSegments, setMapSegments] = useState([]);
  const [selectedSegmentId, setSelectedSegmentId] = useState(null);
  const [mapFlyTarget, setMapFlyTarget] = useState(null);
  const [tempOriginCoords, setTempOriginCoords] = useState(null);   // [lon, lat]
  const [tempDestCoords, setTempDestCoords] = useState(null);       // [lon, lat]
  const [highlightedSegments, setHighlightedSegments] = useState([]); // segment IDs to pulse-highlight

  // ─── Convoy simulation engine ──────────────────────────────────
  const [convoySimulation, setConvoySimulation] = useState(null);
  // Shape: { active, routeCoords:[[lat,lng],...], currentIndex, progress(0-1),
  //   position:{lat,lng}, heading, speed, startTime, totalDistanceM,
  //   distanceTraveledM, etaSeconds, segmentIds:[], currentSegmentIdx,
  //   speedHistory:[], congestionHistory:[], etaHistory:[], paused }
  const simIntervalRef = useRef(null);
  const simStateRef = useRef(null); // for mutable access inside interval
  const demoStartedRef = useRef(false);

  // Haversine distance in meters
  const haversine = useCallback((lat1, lon1, lat2, lon2) => {
    const R = 6371000;
    const dLat = (lat2 - lat1) * Math.PI / 180;
    const dLon = (lon2 - lon1) * Math.PI / 180;
    const a = Math.sin(dLat / 2) ** 2 + Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) * Math.sin(dLon / 2) ** 2;
    return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  }, []);

  // Bearing between two points in degrees
  const bearing = useCallback((lat1, lon1, lat2, lon2) => {
    const dLon = (lon2 - lon1) * Math.PI / 180;
    const y = Math.sin(dLon) * Math.cos(lat2 * Math.PI / 180);
    const x = Math.cos(lat1 * Math.PI / 180) * Math.sin(lat2 * Math.PI / 180) -
              Math.sin(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) * Math.cos(dLon);
    return ((Math.atan2(y, x) * 180 / Math.PI) + 360) % 360;
  }, []);

  const addReasoning = useCallback((entry) => {
    setAiReasoning((prev) => [{ ...entry, timestamp: Date.now() }, ...prev].slice(0, 50));
  }, []);

  const startConvoySimulation = useCallback((routeSegmentIds) => {
    // Build ordered polyline from segment geometries
    const segs = routeSegmentIds
      .map(sid => mapSegments.find(s => String(s.segment_id) === String(sid)))
      .filter(s => s?.geometry?.coordinates?.length);
    if (segs.length === 0) return;

    const routeCoords = [];
    for (const seg of segs) {
      for (const c of seg.geometry.coordinates) {
        routeCoords.push([c[1], c[0]]); // [lat, lng]
      }
    }
    if (routeCoords.length < 2) return;

    // Compute cumulative distances
    const cumDist = [0];
    for (let i = 1; i < routeCoords.length; i++) {
      cumDist.push(cumDist[i - 1] + haversine(routeCoords[i - 1][0], routeCoords[i - 1][1], routeCoords[i][0], routeCoords[i][1]));
    }
    const totalDistanceM = cumDist[cumDist.length - 1];

    const initState = {
      active: true,
      paused: false,
      routeCoords,
      cumDist,
      currentIndex: 0,
      progress: 0,
      position: { lat: routeCoords[0][0], lng: routeCoords[0][1] },
      heading: bearing(routeCoords[0][0], routeCoords[0][1], routeCoords[1][0], routeCoords[1][1]),
      speed: 35 + Math.random() * 15, // 35-50 km/h initial
      startTime: Date.now(),
      totalDistanceM,
      distanceTraveledM: 0,
      etaSeconds: totalDistanceM / (40 * 1000 / 3600), // initial estimate at 40km/h
      segmentIds: routeSegmentIds,
      currentSegmentIdx: 0,
      speedHistory: [],
      congestionHistory: [],
      etaHistory: [],
      trailCoords: [{ lat: routeCoords[0][0], lng: routeCoords[0][1], speed: 40, congestion: 0.2 }],
      // Enhanced tracking
      maxSpeed: 0,
      minSpeed: 999,
      avgSpeed: 0,
      speedSamples: 0,
      acceleration: 0,        // km/h per tick (+accel, -decel)
      prevSpeed: 40,
      originCoord: { lat: routeCoords[0][0], lng: routeCoords[0][1] },
      destCoord: { lat: routeCoords[routeCoords.length - 1][0], lng: routeCoords[routeCoords.length - 1][1] },
      elapsedSeconds: 0,
      segmentsTraversed: 0,
      // R2 enhancements
      fuelPct: 100,                 // simulated fuel 100→0
      distanceRemainingM: totalDistanceM,
      currentZone: 'primary',       // road class of current segment
      threatLevel: 'nominal',       // nominal | guarded | moderate | elevated | critical
      gForce: 0,                    // lateral G-force from heading delta
      prevHeading: 0,
      zoneLog: [],                  // [{time, fromZone, toZone, segId}]
      signalsRemaining: Math.max(2, Math.floor(segs.length * 0.3)), // ~30% of segments have signals
      routeSegments: segs,
    };

    simStateRef.current = initState;
    setConvoySimulation({ ...initState });

    addReasoning({ type: 'system', title: 'Convoy Simulation Started', detail: `${routeSegmentIds.length} segments, ${(totalDistanceM / 1000).toFixed(1)} km route` });

    // Clear any existing interval
    if (simIntervalRef.current) clearInterval(simIntervalRef.current);

    // Animation tick — every 200ms, advance convoy position
    simIntervalRef.current = setInterval(() => {
      const st = simStateRef.current;
      if (!st || !st.active || st.paused) return;

      // Realistic speed variation: ±5 km/h oscillation + random micro-jitter
      const baseSpeed = 35 + 15 * Math.sin(Date.now() / 8000); // oscillate 20-50 km/h
      const jitter = (Math.random() - 0.5) * 6;
      const speed = Math.max(5, Math.min(65, baseSpeed + jitter));
      const speedMps = speed * 1000 / 3600;
      const dt = 0.2; // 200ms intervals
      const advanceM = speedMps * dt * 12; // 12x time multiplier for demo visibility

      let newDist = st.distanceTraveledM + advanceM;
      if (newDist >= st.totalDistanceM) {
        // Convoy reached destination
        const last = st.routeCoords[st.routeCoords.length - 1];
        const finalState = {
          ...st,
          active: false,
          progress: 1,
          position: { lat: last[0], lng: last[1] },
          distanceTraveledM: st.totalDistanceM,
          etaSeconds: 0,
          speed: 0,
        };
        simStateRef.current = finalState;
        setConvoySimulation({ ...finalState });
        clearInterval(simIntervalRef.current);
        simIntervalRef.current = null;
        return;
      }

      // Find position on polyline for newDist
      let segIdx = 0;
      for (let i = 1; i < st.cumDist.length; i++) {
        if (st.cumDist[i] >= newDist) { segIdx = i - 1; break; }
        if (i === st.cumDist.length - 1) segIdx = i - 1;
      }
      const localT = (newDist - st.cumDist[segIdx]) / Math.max(0.001, st.cumDist[segIdx + 1] - st.cumDist[segIdx]);
      const p1 = st.routeCoords[segIdx];
      const p2 = st.routeCoords[segIdx + 1] || p1;
      const lat = p1[0] + (p2[0] - p1[0]) * localT;
      const lng = p1[1] + (p2[1] - p1[1]) * localT;
      const hdg = bearing(p1[0], p1[1], p2[0], p2[1]);

      const now = Date.now();
      const elapsed = (now - st.startTime) / 1000;
      const remaining = st.totalDistanceM - newDist;
      const estimatedEta = speedMps > 0 ? remaining / speedMps / 12 : 9999; // account for 12x multiplier

      // Synthetic congestion at current position (based on road segment)
      const segProgress = newDist / st.totalDistanceM;
      const currentSegIdx = Math.min(Math.floor(segProgress * st.segmentIds.length), st.segmentIds.length - 1);
      const congestion = 0.15 + 0.35 * Math.sin(now / 5000) + 0.1 * Math.random(); // 0.15-0.6 range

      // Record history (keep last 60 points = ~12 seconds of demo time)
      const speedHist = [...st.speedHistory, { t: elapsed, speed: Math.round(speed), ts: now }].slice(-60);
      const congHist = [...st.congestionHistory, { t: elapsed, congestion: parseFloat(congestion.toFixed(3)), ts: now }].slice(-60);
      const etaHist = [...st.etaHistory, { t: elapsed, eta: Math.round(estimatedEta), ts: now }].slice(-60);
      const trail = [...st.trailCoords, { lat, lng, speed: Math.round(speed), congestion: parseFloat(congestion.toFixed(3)) }].slice(-200);

      // Enhanced tracking
      const newMax = Math.max(st.maxSpeed, speed);
      const newMin = Math.min(st.minSpeed, speed);
      const newSamples = st.speedSamples + 1;
      const newAvg = (st.avgSpeed * st.speedSamples + speed) / newSamples;
      const accel = speed - (st.prevSpeed || speed);
      const newSegTraversed = currentSegIdx > st.segmentsTraversed ? currentSegIdx : st.segmentsTraversed;

      // R2: Fuel — burns proportional to speed (faster = more fuel)
      const fuelBurnRate = 0.008 + (speed / 60) * 0.015; // 0.8–2.3% per tick at max speed
      const newFuel = Math.max(0, (st.fuelPct || 100) - fuelBurnRate);

      // R2: Distance remaining
      const distRemaining = st.totalDistanceM - newDist;

      // R2: Current road zone from segment data
      const currentSeg = st.routeSegments?.[currentSegIdx];
      const currentZone = currentSeg?.road_class || currentSeg?.properties?.road_class || st.currentZone || 'primary';

      // R2: Zone transition detection
      let zoneLog = st.zoneLog || [];
      if (currentZone !== st.currentZone && st.currentZone) {
        zoneLog = [...zoneLog, {
          time: elapsed,
          fromZone: st.currentZone,
          toZone: currentZone,
          segId: st.segmentIds?.[currentSegIdx],
        }].slice(-20);
      }

      // R2: Threat level — derived from congestion + speed (slow+congested = bad)
      const threatScore = congestion * 0.6 + (1 - Math.min(speed / 50, 1)) * 0.4;
      const threatLevel = threatScore > 0.75 ? 'critical' : threatScore > 0.6 ? 'elevated' : threatScore > 0.45 ? 'moderate' : threatScore > 0.25 ? 'guarded' : 'nominal';

      // R2: G-force from heading change (lateral)
      const headingDelta = Math.abs(hdg - (st.prevHeading || hdg));
      const normalizedDelta = headingDelta > 180 ? 360 - headingDelta : headingDelta;
      const gForce = Math.min(2.0, (normalizedDelta / 45) * (speed / 40)); // scale by turn severity and speed

      // R2: Signals remaining (decrement when crossing signal segments)
      const signalsRemaining = Math.max(0, (st.signalsRemaining || 0) - (currentSegIdx > st.segmentsTraversed ? 0.3 : 0));

      const updated = {
        ...st,
        currentIndex: segIdx,
        progress: newDist / st.totalDistanceM,
        position: { lat, lng },
        heading: hdg,
        speed,
        congestion,
        distanceTraveledM: newDist,
        etaSeconds: estimatedEta,
        currentSegmentIdx: currentSegIdx,
        speedHistory: speedHist,
        congestionHistory: congHist,
        etaHistory: etaHist,
        trailCoords: trail,
        maxSpeed: newMax,
        minSpeed: newMin === 999 ? speed : newMin,
        avgSpeed: newAvg,
        speedSamples: newSamples,
        acceleration: accel,
        prevSpeed: speed,
        elapsedSeconds: elapsed,
        segmentsTraversed: newSegTraversed,
        // R2 fields
        fuelPct: newFuel,
        distanceRemainingM: distRemaining,
        currentZone,
        threatLevel,
        gForce,
        prevHeading: hdg,
        zoneLog,
        signalsRemaining: Math.round(signalsRemaining),
      };
      simStateRef.current = updated;
      setConvoySimulation({ ...updated });
    }, 200);
  }, [mapSegments, haversine, bearing, addReasoning]);

  const stopConvoySimulation = useCallback(() => {
    if (simIntervalRef.current) { clearInterval(simIntervalRef.current); simIntervalRef.current = null; }
    if (simStateRef.current) {
      simStateRef.current = { ...simStateRef.current, active: false, paused: false };
      setConvoySimulation(prev => prev ? { ...prev, active: false, paused: false } : null);
    }
  }, []);

  const pauseConvoySimulation = useCallback(() => {
    if (simStateRef.current) {
      simStateRef.current = { ...simStateRef.current, paused: !simStateRef.current.paused };
      setConvoySimulation(prev => prev ? { ...prev, paused: !prev.paused } : null);
    }
  }, []);

  // ═══════════════════════════════════════════════════════════════════════════
  // REALISTIC DEMO: Raj Bhavan → SVPI Airport, Ahmedabad
  // East-bank corridor — follows actual roads, no river crossings
  // Road-class-aware speed / congestion / threat derived from ground conditions
  // ═══════════════════════════════════════════════════════════════════════════
  const startDemoSimulation = useCallback(() => {
    if (simIntervalRef.current) { clearInterval(simIntervalRef.current); simIntervalRef.current = null; }

    /* ─── Route phases: each section of the corridor with road properties ─── */
    const ROUTE_PHASES = [
      {
        road_class: 'primary', speed_limit: 40, zone: 'Shahibag', congestion_base: 0.22,
        points: [
          [23.0453, 72.5866],[23.0455, 72.5872],[23.0457, 72.5879],[23.0460, 72.5886],
          [23.0462, 72.5893],[23.0464, 72.5901],[23.0465, 72.5909],[23.0464, 72.5917],
          [23.0463, 72.5924],
        ],
      },
      {
        road_class: 'secondary', speed_limit: 30, zone: 'Raipur-Darwaza', congestion_base: 0.44,
        points: [
          [23.0459, 72.5930],[23.0454, 72.5935],[23.0449, 72.5940],[23.0444, 72.5944],
          [23.0439, 72.5948],[23.0434, 72.5952],[23.0429, 72.5956],[23.0424, 72.5960],
          [23.0420, 72.5965],
        ],
      },
      {
        road_class: 'primary', speed_limit: 35, zone: 'Saraspur', congestion_base: 0.36,
        points: [
          [23.0418, 72.5974],[23.0417, 72.5984],[23.0416, 72.5994],[23.0415, 72.6004],
          [23.0414, 72.6014],[23.0413, 72.6024],[23.0412, 72.6034],[23.0412, 72.6044],
        ],
      },
      {
        road_class: 'trunk', speed_limit: 45, zone: 'Rakhial', congestion_base: 0.28,
        points: [
          [23.0414, 72.6052],[23.0418, 72.6059],[23.0423, 72.6066],[23.0429, 72.6073],
          [23.0435, 72.6079],[23.0441, 72.6085],[23.0448, 72.6090],[23.0455, 72.6095],
        ],
      },
      {
        road_class: 'trunk', speed_limit: 50, zone: 'Meghaninagar', congestion_base: 0.20,
        points: [
          [23.0464, 72.6100],[23.0474, 72.6104],[23.0484, 72.6108],[23.0494, 72.6112],
          [23.0504, 72.6116],[23.0515, 72.6120],[23.0526, 72.6124],[23.0537, 72.6128],
        ],
      },
      {
        road_class: 'motorway', speed_limit: 80, zone: 'Airport-Expressway', congestion_base: 0.10,
        points: [
          [23.0550, 72.6133],[23.0562, 72.6139],[23.0574, 72.6145],[23.0586, 72.6151],
          [23.0598, 72.6158],[23.0610, 72.6165],[23.0622, 72.6173],[23.0634, 72.6181],
          [23.0646, 72.6189],[23.0658, 72.6198],[23.0670, 72.6207],[23.0682, 72.6216],
        ],
      },
      {
        road_class: 'trunk', speed_limit: 40, zone: 'Airport-Approach', congestion_base: 0.16,
        points: [
          [23.0692, 72.6226],[23.0700, 72.6237],[23.0707, 72.6249],[23.0713, 72.6261],
          [23.0718, 72.6274],[23.0722, 72.6287],[23.0726, 72.6300],[23.0729, 72.6312],
          [23.0732, 72.6322],
        ],
      },
    ];

    // Flatten all phase waypoints into a single polyline + per-point road metadata
    const routeCoords = [];
    const pointMeta = [];
    for (const phase of ROUTE_PHASES) {
      for (const pt of phase.points) {
        routeCoords.push(pt);
        pointMeta.push({ road_class: phase.road_class, speed_limit: phase.speed_limit, zone: phase.zone, congestion_base: phase.congestion_base });
      }
    }
    if (routeCoords.length < 2) return;

    // Cumulative distance array
    const cumDist = [0];
    for (let i = 1; i < routeCoords.length; i++) {
      cumDist.push(cumDist[i - 1] + haversine(routeCoords[i - 1][0], routeCoords[i - 1][1], routeCoords[i][0], routeCoords[i][1]));
    }
    const totalDistanceM = cumDist[cumDist.length - 1];

    // Pre-compute phase boundary distances for intersection slowdown
    const phaseBoundaryDists = [];
    let ptIdx = 0;
    for (let pi = 0; pi < ROUTE_PHASES.length - 1; pi++) {
      ptIdx += ROUTE_PHASES[pi].points.length;
      phaseBoundaryDists.push(cumDist[ptIdx]);
    }

    const segmentIds = routeCoords.map((_, i) => `seg-${i + 1}`);
    const initSpeed = ROUTE_PHASES[0].speed_limit * 0.72;

    const initState = {
      active: true, paused: false, routeCoords, cumDist, pointMeta, phaseBoundaryDists,
      currentIndex: 0, progress: 0,
      position: { lat: routeCoords[0][0], lng: routeCoords[0][1] },
      heading: bearing(routeCoords[0][0], routeCoords[0][1], routeCoords[1][0], routeCoords[1][1]),
      speed: initSpeed, congestion: ROUTE_PHASES[0].congestion_base,
      startTime: Date.now(), totalDistanceM, distanceTraveledM: 0,
      etaSeconds: totalDistanceM / (45 * 1000 / 3600),
      segmentIds, currentSegmentIdx: 0,
      speedHistory: [], congestionHistory: [], etaHistory: [],
      trailCoords: [{ lat: routeCoords[0][0], lng: routeCoords[0][1], speed: initSpeed, congestion: ROUTE_PHASES[0].congestion_base }],
      maxSpeed: 0, minSpeed: 999, avgSpeed: 0, speedSamples: 0,
      acceleration: 0, prevSpeed: initSpeed,
      originCoord: { lat: routeCoords[0][0], lng: routeCoords[0][1] },
      destCoord: { lat: routeCoords[routeCoords.length - 1][0], lng: routeCoords[routeCoords.length - 1][1] },
      elapsedSeconds: 0, segmentsTraversed: 0,
      fuelPct: 100, distanceRemainingM: totalDistanceM,
      currentZone: ROUTE_PHASES[0].zone, threatLevel: 'nominal',
      gForce: 0, prevHeading: 0, zoneLog: [],
      signalsRemaining: ROUTE_PHASES.length * 2, routeSegments: [],
    };

    simStateRef.current = initState;
    setConvoySimulation({ ...initState });
    addReasoning({ type: 'system', title: 'VVIP Demo Route Activated', detail: `Raj Bhavan → SVPI Airport · ${(totalDistanceM / 1000).toFixed(1)} km · ${ROUTE_PHASES.length} road phases` });

    // ─── Road-aware animation tick (200 ms) ─────────────────────────────────
    simIntervalRef.current = setInterval(() => {
      const st = simStateRef.current;
      if (!st || !st.active || st.paused) return;

      const now = Date.now();
      const elapsed = (now - st.startTime) / 1000;

      // Current road metadata
      const metaIdx = Math.min(st.currentIndex, st.pointMeta.length - 1);
      const meta = st.pointMeta[metaIdx];

      // Intersection slowdown near phase boundaries (±250 m)
      let intersectionFactor = 1.0;
      for (const bd of st.phaseBoundaryDists) {
        const gap = Math.abs(st.distanceTraveledM - bd);
        if (gap < 250) { intersectionFactor = 0.45 + 0.55 * (gap / 250); break; }
      }

      // Road-class-aware speed: oscillates around speed_limit instead of fixed sine
      const speedCycle = meta.speed_limit * (0.68 + 0.22 * Math.sin(now / 6000));
      const jitter = (Math.random() - 0.5) * 3;
      const speed = Math.max(8, (speedCycle + jitter) * intersectionFactor);
      const speedMps = speed * 1000 / 3600;
      const advanceM = speedMps * 0.2 * 5; // 5× time multiplier (slower than 12× for longer demo)

      let newDist = st.distanceTraveledM + advanceM;
      if (newDist >= st.totalDistanceM) {
        const last = st.routeCoords[st.routeCoords.length - 1];
        const fin = { ...st, active: false, progress: 1, position: { lat: last[0], lng: last[1] }, distanceTraveledM: st.totalDistanceM, etaSeconds: 0, speed: 0 };
        simStateRef.current = fin; setConvoySimulation({ ...fin });
        clearInterval(simIntervalRef.current); simIntervalRef.current = null;
        return;
      }

      // Locate point on polyline
      let segIdx = 0;
      for (let i = 1; i < st.cumDist.length; i++) { if (st.cumDist[i] >= newDist) { segIdx = i - 1; break; } if (i === st.cumDist.length - 1) segIdx = i - 1; }
      const localT = (newDist - st.cumDist[segIdx]) / Math.max(0.001, st.cumDist[segIdx + 1] - st.cumDist[segIdx]);
      const p1 = st.routeCoords[segIdx], p2 = st.routeCoords[segIdx + 1] || p1;
      const lat = p1[0] + (p2[0] - p1[0]) * localT;
      const lng = p1[1] + (p2[1] - p1[1]) * localT;
      const hdg = bearing(p1[0], p1[1], p2[0], p2[1]);

      // Road-class-aware congestion
      const congestion = Math.max(0, Math.min(1, meta.congestion_base + 0.12 * Math.sin(now / 7000) + (Math.random() - 0.5) * 0.08));

      const remaining = st.totalDistanceM - newDist;
      const estimatedEta = speedMps > 0 ? remaining / speedMps / 5 : 9999;

      // History buffers (80 points ≈ 16 s real-time for rich charts)
      const speedHist = [...st.speedHistory, { t: elapsed, speed: Math.round(speed), ts: now }].slice(-80);
      const congHist = [...st.congestionHistory, { t: elapsed, congestion: parseFloat(congestion.toFixed(3)), ts: now }].slice(-80);
      const etaHist = [...st.etaHistory, { t: elapsed, eta: Math.round(estimatedEta), ts: now }].slice(-80);
      const trail = [...st.trailCoords, { lat, lng, speed: Math.round(speed), congestion: parseFloat(congestion.toFixed(3)) }].slice(-200);

      // Enhanced statistics
      const newMax = Math.max(st.maxSpeed, speed);
      const newMin = Math.min(st.minSpeed, speed);
      const newSamples = st.speedSamples + 1;
      const newAvg = (st.avgSpeed * st.speedSamples + speed) / newSamples;
      const accel = speed - (st.prevSpeed || speed);
      const newSegTraversed = segIdx > st.segmentsTraversed ? segIdx : st.segmentsTraversed;

      // Fuel — burns proportional to speed (motorway burns more)
      const fuelBurnRate = 0.005 + (speed / 80) * 0.012;
      const newFuel = Math.max(0, (st.fuelPct || 100) - fuelBurnRate);

      // Zone transition detection
      const currentZone = meta.zone;
      let zoneLog = st.zoneLog || [];
      if (currentZone !== st.currentZone && st.currentZone) {
        zoneLog = [...zoneLog, { time: elapsed, fromZone: st.currentZone, toZone: currentZone, segId: segIdx }].slice(-20);
      }

      // Threat level — compound score: congestion + speed deficit + road risk
      const roadRisk = meta.road_class === 'secondary' ? 0.30 : meta.road_class === 'primary' ? 0.15 : meta.road_class === 'trunk' ? 0.08 : 0.03;
      const threatScore = congestion * 0.45 + (1 - Math.min(speed / meta.speed_limit, 1)) * 0.30 + roadRisk;
      const threatLevel = threatScore > 0.72 ? 'critical' : threatScore > 0.58 ? 'elevated' : threatScore > 0.40 ? 'moderate' : threatScore > 0.22 ? 'guarded' : 'nominal';

      // G-force from heading change (lateral)
      const headingDelta = Math.abs(hdg - (st.prevHeading || hdg));
      const normalizedDelta = headingDelta > 180 ? 360 - headingDelta : headingDelta;
      const gForce = Math.min(2.0, (normalizedDelta / 45) * (speed / 40));

      const signalsRemaining = Math.max(0, (st.signalsRemaining || 0) - (segIdx > st.segmentsTraversed ? 0.3 : 0));

      const updated = {
        ...st,
        currentIndex: segIdx, progress: newDist / st.totalDistanceM,
        position: { lat, lng }, heading: hdg, speed, congestion,
        distanceTraveledM: newDist, etaSeconds: estimatedEta, currentSegmentIdx: segIdx,
        speedHistory: speedHist, congestionHistory: congHist, etaHistory: etaHist, trailCoords: trail,
        maxSpeed: newMax, minSpeed: newMin === 999 ? speed : newMin, avgSpeed: newAvg, speedSamples: newSamples,
        acceleration: accel, prevSpeed: speed, elapsedSeconds: elapsed, segmentsTraversed: newSegTraversed,
        fuelPct: newFuel, distanceRemainingM: remaining, currentZone, threatLevel, gForce, prevHeading: hdg,
        zoneLog, signalsRemaining: Math.round(signalsRemaining),
      };
      simStateRef.current = updated;
      setConvoySimulation({ ...updated });
    }, 200);
  }, [haversine, bearing, addReasoning]);

  // Ref kept for potential programmatic use
  const startDemoRef = useRef(startDemoSimulation);
  startDemoRef.current = startDemoSimulation;

  // Cleanup on unmount
  useEffect(() => {
    return () => { if (simIntervalRef.current) clearInterval(simIntervalRef.current); };
  }, []);

  const flyToLocation = useCallback((lat, lng, zoom = 15) => {
    setMapFlyTarget({ lat, lng, zoom, _ts: Date.now() });
  }, []);

  const flyToSegment = useCallback((segmentId) => {
    const seg = mapSegments.find((s) => String(s.segment_id) === String(segmentId));
    if (seg?.geom?.coordinates?.length) {
      const coords = seg.geom.coordinates;
      const mid = coords[Math.floor(coords.length / 2)];
      setSelectedSegmentId(segmentId);
      setMapFlyTarget({ lat: mid[1], lng: mid[0], zoom: 16, _ts: Date.now() });
    }
  }, [mapSegments]);

  const selectSegment = useCallback((segmentId) => {
    setSelectedSegmentId((prev) => (prev === segmentId ? null : segmentId));
  }, []);

  // ─── WebSocket refs ────────────────────────────────────────────
  const wsRefs = useRef(new Map());

  // ─── Polling: corridor, movements, anomalies ──────────────────
  useEffect(() => {
    let active = true;
    const poll = async () => {
      try {
        const [sum, mov, anm] = await Promise.all([
          api.getCorridorSummary().catch(() => null),
          api.getActiveMovements().catch(() => ({ data: { movements: [] } })),
          api.getRecentAnomalies(15).catch(() => ({ data: { anomalies: [] } })),
        ]);
        if (!active) return;
        if (sum?.data) setCorridorSummary(sum.data);
        setActiveMovements(mov.data?.movements || []);
        if (anm.data?.anomalies) {
          setAnomalies(
            anm.data.anomalies.map((a, idx) => ({
              id: a.anomaly_id || idx,
              segment_id: a.segment_id,
              type: a.anomaly_type,
              severity: a.severity,
              tag: a.anomaly_type.toUpperCase().replace(/_/g, ' '),
              message: a.details?.description || `Severity ${a.severity} at segment ${a.segment_id}`,
              time: new Date(a.timestamp_utc).toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }),
              timestamp: a.timestamp_utc,
              read: false,
            }))
          );
        }
      } catch { /* silent */ }
    };
    poll();
    const id = setInterval(poll, 12000);
    return () => { active = false; clearInterval(id); };
  }, []);

  // ─── Polling: GPU health ───────────────────────────────────────
  useEffect(() => {
    let active = true;
    const poll = async () => {
      try {
        const res = await api.getGpuHealth();
        if (active) setGpuHealth(res.data);
      } catch { /* silent */ }
    };
    poll();
    const id = setInterval(poll, 10000);
    return () => { active = false; clearInterval(id); };
  }, []);

  // ─── Polling: backend health ───────────────────────────────────
  useEffect(() => {
    let active = true;
    const poll = async () => {
      try {
        const res = await api.getHealth();
        if (active) setBackendHealth(res.data);
      } catch { /* silent */ }
    };
    poll();
    const id = setInterval(poll, 30000);
    return () => { active = false; clearInterval(id); };
  }, []);

  // ─── WebSocket tracking for movements ─────────────────────────
  useEffect(() => {
    activeMovements.forEach((mov) => {
      const mid = mov.movement_id;
      if (!mid || wsRefs.current.has(mid)) return;
      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      const ws = new WebSocket(`${protocol}//${window.location.host}/ws/convoy/${mid}`);
      ws.onmessage = (e) => {
        try {
          const data = JSON.parse(e.data);
          if (data.type === 'convoy.position') {
            setActiveMovements((prev) =>
              prev.map((m) =>
                m.movement_id === mid
                  ? { ...m, current_position: { lat: data.payload.position[1], lng: data.payload.position[0] } }
                  : m
              )
            );
          }
        } catch { /* silent */ }
      };
      wsRefs.current.set(mid, ws);
    });
  }, [activeMovements]);

  useEffect(() => {
    return () => {
      wsRefs.current.forEach((ws) => { try { ws.close(); } catch {} });
      wsRefs.current.clear();
    };
  }, []);

  // ─── Lifecycle actions ─────────────────────────────────────────

  const deployMovement = useCallback(async ({ origin, destination, vvipClass, plannedDeparture }) => {
    setLifecycleError(null);
    setLifecycle('planning');
    try {
      // Step 1: Create movement
      const createRes = await api.createMovement({
        origin,
        destination,
        vvip_class: vvipClass,
        planned_departure: plannedDeparture || new Date().toISOString(),
      });
      const mid = createRes.data?.movement_id;
      if (!mid) throw new Error('No movement_id returned');
      setMovementId(mid);

      addReasoning({
        type: 'system',
        title: 'Movement Created',
        detail: `Movement ${mid} created for ${vvipClass} class`,
      });

      // Step 2: Plan movement (7-node LangGraph pre-movement workflow)
      const planRes = await api.planMovement(mid, {
        origin,
        destination,
        vvip_class: vvipClass,
        planned_departure: plannedDeparture || new Date().toISOString(),
      });
      setPlanResult(planRes.data);
      setLifecycle('approved');

      addReasoning({
        type: 'plan',
        title: 'Pre-Movement Plan Complete',
        detail: `Status: ${planRes.data?.status}, Confidence: ${planRes.data?.confidence}`,
        data: planRes.data,
      });

      return planRes.data;
    } catch (err) {
      setLifecycleError(err.message || 'Planning failed');
      setLifecycle('idle');
      throw err;
    }
  }, [addReasoning]);

  const startEscort = useCallback(async (destination) => {
    if (!movementId) return;
    setLifecycleError(null);
    try {
      setLifecycle('active');
      const res = await api.startEscort(movementId, { destination });
      setEscortResult(res.data);

      addReasoning({
        type: 'escort',
        title: 'Live Escort Complete',
        detail: `${res.data?.total_iterations} iterations, status: ${res.data?.final_status}`,
        data: res.data,
      });

      return res.data;
    } catch (err) {
      setLifecycleError(err.message || 'Escort failed');
      throw err;
    }
  }, [movementId, addReasoning]);

  const clearMovement = useCallback(async () => {
    if (!movementId) return;
    setLifecycleError(null);
    try {
      const res = await api.clearMovement(movementId);
      setClearResult(res.data);
      setLifecycle('completed');

      addReasoning({
        type: 'clear',
        title: 'Post-Clearance Report',
        detail: `${res.data?.segments_recovered}/${res.data?.total_affected_segments} segments recovered`,
        data: res.data,
      });

      return res.data;
    } catch (err) {
      setLifecycleError(err.message || 'Clearance failed');
      throw err;
    }
  }, [movementId, addReasoning]);

  const resetLifecycle = useCallback(() => {
    setMovementId(null);
    setLifecycle('idle');
    setPlanResult(null);
    setEscortResult(null);
    setClearResult(null);
    setLifecycleError(null);
    setTempOriginCoords(null);
    setTempDestCoords(null);
    setHighlightedSegments([]);
    stopConvoySimulation();
    setConvoySimulation(null);
  }, [stopConvoySimulation]);

  const highlightSegments = useCallback((segIds, flyToFirst = true) => {
    setHighlightedSegments(segIds || []);
    if (flyToFirst && segIds?.length) flyToSegment(segIds[0]);
  }, [flyToSegment]);

  // ─── Streaming AI chat ─────────────────────────────────────────
  const sendChatMessage = useCallback(async (message, vvipClass = 'Z+') => {
    setChatMessages((prev) => [...prev, { role: 'user', content: message, timestamp: Date.now() }]);
    addReasoning({ type: 'thought', title: 'User Query', detail: message });
    setChatStreaming(true);

    try {
      const response = await api.streamChat({
        message,
        movement_id: movementId || undefined,
        vvip_class: vvipClass,
      });

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let fullResponse = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop();

        for (const line of lines) {
          if (!line.trim()) continue;
          try {
            const event = JSON.parse(line);
            switch (event.type) {
              case 'token':
                fullResponse += event.data;
                setChatMessages((prev) => {
                  const last = prev[prev.length - 1];
                  if (last?.role === 'assistant' && last?.streaming) {
                    return [...prev.slice(0, -1), { ...last, content: fullResponse }];
                  }
                  return [...prev, { role: 'assistant', content: fullResponse, streaming: true, timestamp: Date.now() }];
                });
                break;
              case 'thought':
                addReasoning({ type: 'thought', title: 'Reasoning', detail: event.data?.text || event.data });
                break;
              case 'tool_call':
                addReasoning({
                  type: 'tool',
                  title: `Tool: ${event.data?.toolName}`,
                  detail: JSON.stringify(event.data?.arguments || {}).slice(0, 200),
                  data: event.data,
                });
                break;
              case 'tool_result':
                addReasoning({
                  type: 'tool',
                  title: `Result: ${event.data?.callId}`,
                  detail: `${event.data?.state} (${event.data?.durationMs}ms)`,
                  data: event.data,
                });
                break;
              case 'done':
                if (fullResponse) {
                  setChatMessages((prev) => {
                    const last = prev[prev.length - 1];
                    if (last?.role === 'assistant') {
                      return [...prev.slice(0, -1), { ...last, streaming: false }];
                    }
                    return prev;
                  });
                  addReasoning({ type: 'decision', title: 'AI Response', detail: fullResponse.slice(0, 300) });
                }
                break;
              case 'error':
                addReasoning({ type: 'error', title: 'Stream Error', detail: event.data?.message || 'Unknown error' });
                break;
            }
          } catch { /* skip malformed line */ }
        }
      }

      // If no streaming tokens came, fall back to non-streaming chat
      if (!fullResponse) {
        try {
          const fallback = await api.sendChat({ message, movement_id: movementId || undefined, vvip_class: vvipClass });
          fullResponse = fallback.data?.response?.reasoning || fallback.data?.response?.action || 'AI response received';
          setChatMessages((prev) => [...prev, { role: 'assistant', content: fullResponse, timestamp: Date.now() }]);
          addReasoning({ type: 'decision', title: 'AI Response', detail: fullResponse.slice(0, 300) });
        } catch { /* silent */ }
      }

      return fullResponse;
    } catch (err) {
      addReasoning({ type: 'error', title: 'Chat Failed', detail: err.message });
      setChatMessages((prev) => [...prev, { role: 'assistant', content: `Error: ${err.message}`, error: true, timestamp: Date.now() }]);
      throw err;
    } finally {
      setChatStreaming(false);
    }
  }, [movementId, addReasoning]);

  const value = {
    // Lifecycle
    movementId,
    lifecycle,
    planResult,
    escortResult,
    clearResult,
    lifecycleError,
    deployMovement,
    startEscort,
    clearMovement,
    resetLifecycle,
    // Global data
    corridorSummary,
    activeMovements,
    anomalies,
    gpuHealth,
    backendHealth,
    // AI
    aiReasoning,
    addReasoning,
    chatMessages,
    chatStreaming,
    sendChatMessage,
    // Map interaction
    mapSegments,
    setMapSegments,
    selectedSegmentId,
    selectSegment,
    mapFlyTarget,
    flyToLocation,
    flyToSegment,
    tempOriginCoords,
    setTempOriginCoords,
    tempDestCoords,
    setTempDestCoords,
    highlightedSegments,
    highlightSegments,
    // Convoy simulation
    convoySimulation,
    startConvoySimulation,
    startDemoSimulation,
    stopConvoySimulation,
    pauseConvoySimulation,
  };

  return <ConvoyContext.Provider value={value}>{children}</ConvoyContext.Provider>;
}
