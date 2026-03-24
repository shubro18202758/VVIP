import React, { useState, useEffect, useCallback, useMemo } from 'react';
import { User, CheckCircle, Clock as ClockIcon, TrendingUp, ChevronLeft, ChevronRight, Cpu, Brain, Zap, Route, Timer, AlertTriangle, Shield, Radio, Target, Crosshair, Activity, Eye, Lock, Siren, CheckSquare, Square, ChevronDown, ChevronUp } from 'lucide-react';
import { BarChart, Bar, Cell, XAxis, YAxis, Tooltip, ResponsiveContainer, RadarChart, PolarGrid, PolarAngleAxis, Radar, Legend, LineChart, Line, CartesianGrid, AreaChart, Area, ReferenceLine } from 'recharts';
import SHdr from './SHdr';
import { useConvoy } from '../context/ConvoyContext';
import * as api from '../services/api';
import { 
  IconPoliceEscort, 
  IconVVIPShield, 
  IconMedicalSupport, 
  IconTrafficPatrol,
  IconOriginMarker,
  IconDestinationMarker 
} from './CustomIcons';

const SECURITY_SPECS = {
  'SPG': { minLanes: 8, closure: 'Full corridor lockdown', advance: '300s', maxQueue: '3000m', color: '#7f1d1d', personnel: '40-60+', agency: 'SPG (Cabinet Sec.)', profile: 'Prime Minister only' },
  'Z+': { minLanes: 6, closure: 'Full closure', advance: '180s', maxQueue: '2000m', color: '#dc2626', personnel: '55', agency: 'NSG + Police', profile: 'High-risk: top political leaders' },
  'Z':  { minLanes: 4, closure: 'Partial closure', advance: '120s', maxQueue: '1000m', color: '#ea580c', personnel: '22', agency: 'NSG/ITBP/CRPF', profile: 'Moderate-high: ministers' },
  'Y+': { minLanes: 3, closure: 'Partial + speed restriction', advance: '90s', maxQueue: '750m', color: '#9333ea', personnel: '11', agency: 'Commandos + Police', profile: 'Moderate: judges, state ministers' },
  'Y':  { minLanes: 2, closure: 'Speed restriction + signal', advance: '60s', maxQueue: '500m', color: '#2563eb', personnel: '8', agency: 'Commandos + Police', profile: 'Low-moderate: senior bureaucrats' },
  'X':  { minLanes: 0, closure: 'Signal priority only', advance: '0s', maxQueue: 'None', color: '#64748b', personnel: '2', agency: 'Armed Police', profile: 'Minimal threat' },
};

// Known VVIP location coordinates [lon, lat] for Ahmedabad corridor
const LOCATION_COORDS = {
  'raj bhavan':              [72.5609, 23.0337],
  'svpi airport':            [72.6266, 23.0733],
  'sardar vallabhbhai patel airport': [72.6266, 23.0733],
  'sabarmati ashram':        [72.5802, 23.0607],
  'narendra modi stadium':   [72.5957, 23.0916],
  'motera stadium':          [72.5957, 23.0916],
  'lal darwaja':             [72.5798, 23.0254],
  'ahmedabad railway station': [72.6006, 23.0251],
  'iim ahmedabad':           [72.5293, 23.0327],
  'science city':            [72.6588, 23.0684],
  'kankaria lake':           [72.6000, 23.0070],
  'cg road':                 [72.5620, 23.0300],
  'sg highway':              [72.5150, 23.0300],
  'riverfront':              [72.5780, 23.0350],
  'gandhinagar':             [72.6369, 23.2156],
};

const LOCATION_NAMES = Object.keys(LOCATION_COORDS);

function resolveCoords(input, fallback) {
  if (!input) return fallback;
  const key = input.toLowerCase().replace(/,?\s*ahmedabad\s*$/i, '').trim();
  for (const [name, coords] of Object.entries(LOCATION_COORDS)) {
    if (key.includes(name) || name.includes(key)) return coords;
  }
  return fallback;
}

// Find nearest segment to given [lon, lat] via spatial bbox query
async function resolveNearestSegment(coords) {
  const [lon, lat] = coords;
  const delta = 0.015; // ~1.5km bbox radius
  try {
    const res = await api.getSegmentsInBbox(lon - delta, lat - delta, lon + delta, lat + delta);
    const segments = res?.data?.segments;
    if (!segments || segments.length === 0) return null;
    // Pick segment whose geometry centroid is closest to the target coords
    let bestSeg = segments[0].segment_id;
    let bestDist = Infinity;
    for (const seg of segments) {
      if (seg.geometry?.coordinates) {
        const lineCoords = seg.geometry.coordinates;
        // Use midpoint of the LineString as approximate centroid
        const mid = lineCoords[Math.floor(lineCoords.length / 2)];
        if (mid) {
          const dx = mid[0] - lon;
          const dy = mid[1] - lat;
          const dist = dx * dx + dy * dy;
          if (dist < bestDist) {
            bestDist = dist;
            bestSeg = seg.segment_id;
          }
        }
      }
    }
    return bestSeg;
  } catch {
    return null;
  }
}

const LeftPanel = ({ 
  open, 
  onToggle, 
  origin, 
  setOrigin, 
  destination, 
  setDestination, 
  selectedVehicles, 
  setSelectedVehicles,
  navigate,
  vvipClass,
  setVvipClass,
}) => {
  const LWIDTH = 320;
  
  const {
    gpuHealth,
    lifecycle,
    planResult,
    escortResult,
    clearResult,
    deployMovement,
    startEscort,
    clearMovement,
    resetLifecycle,
    lifecycleError,
    addReasoning,
    corridorSummary,
    movementId,
    flyToLocation,
    flyToSegment,
    anomalies,
    activeMovements,
    convoySimulation,
    setTempOriginCoords,
    setTempDestCoords,
    highlightSegments,
    mapSegments,
    // Blue Book ASL & Protocol
    aslChecklist,
    toggleAslItem,
    aslReadiness,
    aslCriticalReady,
    simulateAslCompletion,
    protocolCompliance,
    toggleProtocolRule,
    protocolScore,
    antiSabotage,
    transitStatus,
    planB,
    // AI Protocol Actions
    protocolAssessment,
    assessingProtocol,
    runProtocolAssessment,
    threatBrief,
    assessingThreat,
    runThreatAssessment,
  } = useConvoy();

  const [deploying, setDeploying] = useState(false);
  const [aslExpanded, setAslExpanded] = useState(true);
  const [etaPrediction, setEtaPrediction] = useState(null);
  const [flowPrediction, setFlowPrediction] = useState(null);
  const [routeComparison, setRouteComparison] = useState(null);
  const [predictionLoading, setPredictionLoading] = useState(false);
  const [plannedDeparture, setPlannedDeparture] = useState('');
  const [resolvedSegments, setResolvedSegments] = useState({ origin: null, dest: null });
  const [escortLoading, setEscortLoading] = useState(false);
  const [clearLoading, setClearLoading] = useState(false);
  const [originSuggestions, setOriginSuggestions] = useState([]);
  const [destSuggestions, setDestSuggestions] = useState([]);

  // Fleet composition — Blue Book convoy security box formation
  // Ref: Blue Book §3.1 — The Security Box (8+ vehicles)
  // All vehicles same make/colour to prevent VVIP identification
  const FLEET_TEMPLATES = {
    'SPG': [
      { role: 'pilot_warning',  icon: <IconPoliceEscort color="#7f1d1d" />, name: 'Pilot Warning Car',     status: 'ready',   desc: 'State police pilot · Road clearance · Lights/sirens' },
      { role: 'advance_scout',  icon: <IconPoliceEscort color="#7f1d1d" />, name: 'Advance Recon Unit',    status: 'ready',   desc: 'Forward reconnaissance · Threat scanning' },
      { role: 'ecm_technical',  icon: <IconTrafficPatrol color="#7f1d1d" />, name: 'ECM / Technical Car',   status: 'ready',   desc: 'Signal jamming · IED blocking · Comms relay' },
      { role: 'vvip_primary',   icon: <IconVVIPShield color="#7f1d1d" />,   name: 'VVIP Primary (VR10)',   status: 'ready',   desc: 'Armoured VR10 · Protectee + SPG proximate team' },
      { role: 'escort_1',       icon: <IconPoliceEscort color="#7f1d1d" />, name: 'Escort Car I',          status: 'ready',   desc: 'Armed SPG counter-assault team' },
      { role: 'escort_2',       icon: <IconPoliceEscort color="#7f1d1d" />, name: 'Escort Car II',         status: 'ready',   desc: 'Second armed layer · Same make/colour' },
      { role: 'spare_decoy',    icon: <IconVVIPShield color="#7f1d1d" />,   name: 'Spare / Decoy Car',     status: 'ready',   desc: 'Identical to VVIP car · Emergency switch' },
      { role: 'ambulance',      icon: <IconMedicalSupport color="#7f1d1d" />, name: 'Medical Ambulance',   status: 'standby', desc: 'Medical team on standby · Emergency extraction' },
      { role: 'ssp_dm_ib_1',    icon: <IconTrafficPatrol color="#7f1d1d" />, name: 'SSP / DM Authority',   status: 'ready',   desc: 'Local authority officials · Trailing core box' },
      { role: 'ssp_dm_ib_2',    icon: <IconTrafficPatrol color="#7f1d1d" />, name: 'IB Liaison Vehicle',   status: 'ready',   desc: 'Intelligence Bureau · Real-time threat intel' },
    ],
    'Z+': [
      { role: 'pilot_warning',  icon: <IconPoliceEscort color="#dc2626" />, name: 'Pilot Warning Car',     status: 'ready',   desc: 'Road clearance · Lights/sirens · Crowd control' },
      { role: 'ecm_technical',  icon: <IconTrafficPatrol color="#dc2626" />, name: 'ECM / Technical Car',   status: 'ready',   desc: 'Signal jamming · IED blocking · Comms relay' },
      { role: 'vvip_primary',   icon: <IconVVIPShield color="#dc2626" />,   name: 'VVIP Primary (VR10)',   status: 'ready',   desc: 'Armoured vehicle · Protectee + proximate team' },
      { role: 'escort_1',       icon: <IconPoliceEscort color="#dc2626" />, name: 'Escort Car I',          status: 'ready',   desc: 'Armed NSG counter-assault team' },
      { role: 'escort_2',       icon: <IconPoliceEscort color="#dc2626" />, name: 'Escort Car II',         status: 'ready',   desc: 'Second armed layer · Same make/colour' },
      { role: 'spare_decoy',    icon: <IconVVIPShield color="#dc2626" />,   name: 'Spare / Decoy Car',     status: 'ready',   desc: 'Identical to VVIP car · Emergency switch' },
      { role: 'ambulance',      icon: <IconMedicalSupport color="#dc2626" />, name: 'Medical Ambulance',   status: 'standby', desc: 'Medical team · Emergency extraction' },
      { role: 'ssp_dm_ib',      icon: <IconTrafficPatrol color="#dc2626" />, name: 'SSP / DM / IB Trail',  status: 'ready',   desc: 'Authority officials trailing core box' },
    ],
    'Z': [
      { role: 'pilot_warning',  icon: <IconPoliceEscort color="#ea580c" />, name: 'Pilot Warning Car',     status: 'ready',   desc: 'Road clearance · Crowd control signal' },
      { role: 'vvip_primary',   icon: <IconVVIPShield color="#ea580c" />,   name: 'VVIP Primary Car',      status: 'ready',   desc: 'Protected vehicle · CRPF/ITBP team' },
      { role: 'escort_1',       icon: <IconPoliceEscort color="#ea580c" />, name: 'Escort Car',            status: 'ready',   desc: 'Armed escort · Counter-assault' },
      { role: 'ambulance',      icon: <IconMedicalSupport color="#ea580c" />, name: 'Medical Support',     status: 'standby', desc: 'Medical team on standby' },
      { role: 'ssp_dm',         icon: <IconTrafficPatrol color="#ea580c" />, name: 'Authority Trail',       status: 'ready',   desc: 'SSP/DM officials · Area coordination' },
    ],
    'Y+': [
      { role: 'pilot_warning',  icon: <IconPoliceEscort color="#9333ea" />, name: 'Pilot Car',             status: 'ready',   desc: 'Lead vehicle · Route clearance' },
      { role: 'vvip_primary',   icon: <IconVVIPShield color="#9333ea" />,   name: 'VVIP Primary Car',      status: 'ready',   desc: 'Protected vehicle · Commando team' },
      { role: 'escort_1',       icon: <IconPoliceEscort color="#9333ea" />, name: 'Escort Vehicle',        status: 'ready',   desc: 'Armed commandos + police' },
      { role: 'traffic_control', icon: <IconTrafficPatrol color="#9333ea" />, name: 'Traffic Coordination', status: 'standby', desc: 'Signal priority · Route management' },
    ],
    'Y': [
      { role: 'pilot_warning',  icon: <IconPoliceEscort color="#2563eb" />, name: 'Lead Escort',           status: 'ready',   desc: 'Route advance · Signal coordination' },
      { role: 'vvip_primary',   icon: <IconVVIPShield color="#2563eb" />,   name: 'VVIP Primary Car',      status: 'ready',   desc: 'Protected vehicle · Police escort' },
      { role: 'traffic_control', icon: <IconTrafficPatrol color="#2563eb" />, name: 'Traffic Support',     status: 'standby', desc: 'Signal management · Rear coverage' },
    ],
    'X': [
      { role: 'vvip_primary',   icon: <IconVVIPShield color="#64748b" />,   name: 'VVIP Vehicle',          status: 'ready',   desc: 'Primary vehicle · Armed police escort' },
      { role: 'traffic_control', icon: <IconTrafficPatrol color="#64748b" />, name: 'Traffic Escort',      status: 'standby', desc: 'Signal priority support' },
    ],
  };
  const vehicles = useMemo(() => {
    const templates = FLEET_TEMPLATES[vvipClass] || FLEET_TEMPLATES['Z'];
    // Base fuel/range per vehicle slot (pre-mission values)
    const baseFuel = [94, 88, 91, 85, 78, 82, 90, 86, 92, 87];
    const baseRange = [580, 520, 550, 490, 460, 510, 540, 500, 570, 530];
    // During active simulation, derive from convoy fuelPct + per-vehicle variance
    const sim = convoySimulation;
    const simActive = sim?.active;
    const simFuelPct = sim?.fuelPct ?? 100;
    return templates.map((t, i) => {
      // Per-vehicle variance: VVIP car burns slightly more, ambulance less
      const variance = t.role === 'vvip_primary' ? -3 : t.role === 'ambulance' ? 2 : (i % 3 - 1) * 1.5;
      let fuel, range;
      if (simActive && t.status === 'ready') {
        fuel = Math.round(Math.max(5, simFuelPct + variance));
        range = `${Math.round(baseFuel[i % baseFuel.length] * (fuel / 100) * 6.2)} km`;
      } else {
        fuel = t.status === 'ready' ? baseFuel[i % baseFuel.length] : t.status === 'standby' ? 45 : 12;
        range = t.status === 'ready' ? `${baseRange[i % baseRange.length]} km` : t.status === 'standby' ? '280 km' : '—';
      }
      return {
        id: i + 1,
        icon: t.icon,
        name: t.name,
        role: t.role,
        desc: t.desc,
        regId: `GJ-01-VV-${String(i + 1).padStart(4, '0')}`,
        status: t.status,
        fuel,
        range,
      };
    });
  }, [vvipClass, convoySimulation]);

  // Fetch AI predictions from backend — dynamically resolved segments
  const fetchPredictions = useCallback(async () => {
    setPredictionLoading(true);
    try {
      // Resolve origin/destination to actual segment IDs via spatial query
      const originCoords = resolveCoords(origin, [72.5609, 23.0337]);
      const destCoords = resolveCoords(destination, [72.6266, 23.0733]);
      const [originSeg, destSeg] = await Promise.all([
        resolveNearestSegment(originCoords),
        resolveNearestSegment(destCoords),
      ]);

      const effOrigin = originSeg || 1001;
      const effDest = destSeg || 1040;
      setResolvedSegments({ origin: effOrigin, dest: effDest });

      const routeRes = await api.findRoutes({
        origin_segment: effOrigin,
        destination_segment: effDest,
        max_candidates: 3,
      }).catch(() => null);

      if (routeRes?.data?.routes) {
        setRouteComparison(routeRes.data.routes);
      }

      // Use route segment IDs for flow prediction instead of hardcoded list
      const bestRoute = routeRes?.data?.routes?.[0];
      const flowSegments = bestRoute?.segment_ids?.length
        ? bestRoute.segment_ids.slice(0, 10) // limit to 10 for perf
        : [effOrigin, effDest]; // fallback: just endpoints

      const flowRes = await api.predictFlow(flowSegments, [5, 10, 15, 30]).catch(() => null);

      // Get ETA — use route if available, otherwise standalone estimate
      const now = new Date();
      // Compute num_signals from segment count (roughly 1 signal per 3 segments)
      const routeSegCount = bestRoute?.segment_ids?.length || 10;
      const estimatedSignals = Math.max(1, Math.ceil(routeSegCount / 3));
      // Compute weighted_road_class_score from corridor summary or route data
      const roadClassScore = corridorSummary?.avg_road_class_score || (bestRoute?.security_score ? bestRoute.security_score * 100 : 60.0);
      const etaRes = await api.predictEta({
        route_length_m: bestRoute?.total_distance_m || 15000,
        num_segments: routeSegCount,
        avg_predicted_speed: corridorSummary?.avg_speed_kmh || 40.0,
        avg_predicted_congestion: corridorSummary?.avg_congestion_idx || 0.5,
        hour: now.getHours(),
        dow: (now.getDay() + 6) % 7,
        num_signals: estimatedSignals,
        weighted_road_class_score: roadClassScore,
      }).catch(() => null);
      if (etaRes?.data) setEtaPrediction(etaRes.data);

      if (flowRes?.data) setFlowPrediction(flowRes.data);
    } catch { /* silent */ }
    setPredictionLoading(false);
  }, [origin, destination, vvipClass, corridorSummary]);

  useEffect(() => {
    fetchPredictions();
    const id = setInterval(fetchPredictions, 60000);
    return () => clearInterval(id);
  }, [fetchPredictions]);

  const handleDeploy = async () => {
    setDeploying(true);
    try {
      const originCoords = resolveCoords(origin, [72.5609, 23.0337]);
      const destCoords = resolveCoords(destination, [72.6266, 23.0733]);
      await deployMovement({
        origin: originCoords,
        destination: destCoords,
        vvipClass,
        plannedDeparture: plannedDeparture || new Date().toISOString(),
      });
      setTempOriginCoords(null);
      setTempDestCoords(null);
      navigate('route-planning');
    } catch (err) {
      console.error("Deploy failed:", err);
    } finally {
      setDeploying(false);
    }
  };

  const handleStartEscort = async () => {
    setEscortLoading(true);
    try {
      const destCoords = resolveCoords(destination, [72.6266, 23.0733]);
      await startEscort(destCoords);
    } catch (err) {
      console.error("Escort failed:", err);
    } finally {
      setEscortLoading(false);
    }
  };

  const handleClearRoute = async () => {
    setClearLoading(true);
    try {
      await clearMovement();
    } catch (err) {
      console.error("Clear failed:", err);
    } finally {
      setClearLoading(false);
    }
  };

  const handleReset = () => {
    resetLifecycle();
    setRouteComparison(null);
    setFlowPrediction(null);
    setEtaPrediction(null);
    setResolvedSegments({ origin: null, dest: null });
  };

  // Location autocomplete
  const filterLocations = (input) => {
    if (!input || input.length < 2) return [];
    const key = input.toLowerCase().replace(/,?\s*ahmedabad\s*$/i, '').trim();
    return LOCATION_NAMES.filter(name => name.includes(key) || key.includes(name))
      .map(name => name.split(' ').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' '));
  };

  // Build chart data from route comparison
  const routeChartData = routeComparison?.map((r, i) => ({
    name: `Route ${i + 1}`,
    Security: Math.round((r.security_score || 0) * 100),
    Flow: Math.round((1 - (r.disruption_score || 0) / 100) * 100),
    Composite: Math.round((r.composite_score || r.score || 0) * 100),
  })) || [];

  // Build flow forecast chart — predictions is {segmentId: {horizon: {speed_kmh, congestion_idx}}}
  const flowChartData = (() => {
    const preds = flowPrediction?.predictions;
    if (!preds || typeof preds !== 'object' || Object.keys(preds).length === 0) return [];
    const horizonAcc = {};
    Object.values(preds).forEach(segData => {
      if (segData && typeof segData === 'object') {
        Object.entries(segData).forEach(([horizon, vals]) => {
          if (!horizonAcc[horizon]) horizonAcc[horizon] = { speed: 0, congestion: 0, count: 0 };
          horizonAcc[horizon].speed += vals?.speed_kmh || 0;
          horizonAcc[horizon].congestion += vals?.congestion_idx || 0;
          horizonAcc[horizon].count += 1;
        });
      }
    });
    return Object.entries(horizonAcc)
      .sort(([a], [b]) => Number(a) - Number(b))
      .map(([h, v]) => ({
        horizon: `T+${h}m`,
        Speed: Math.round(v.speed / (v.count || 1)),
        Congestion: Math.round((v.congestion / (v.count || 1)) * 100),
      }));
  })();

  // ─── Blue Book Protocol Computed Data ──────────────────────
  const threatLevel = useMemo(() => {
    if (!anomalies || anomalies.length === 0) return { level: 'NOMINAL', color: '#16a34a', icon: '◈' };
    const high = anomalies.filter(a => a.severity === 'high').length;
    const medium = anomalies.filter(a => a.severity === 'medium').length;
    if (high >= 3) return { level: 'CRITICAL', color: '#dc2626', icon: '◆' };
    if (high >= 1) return { level: 'ELEVATED', color: '#ea580c', icon: '◆' };
    if (medium >= 3) return { level: 'MODERATE', color: '#eab308', icon: '◇' };
    if (medium >= 1) return { level: 'GUARDED', color: '#2563eb', icon: '◈' };
    return { level: 'NOMINAL', color: '#16a34a', icon: '◈' };
  }, [anomalies]);

  const deployedAgencies = useMemo(() => {
    if (!planResult?.diversion_directives) return [];
    const agencyMap = {};
    const AGENCY_LABELS = {
      traffic_police: { name: 'Delhi Traffic Police', short: 'DTP', color: '#2563eb' },
      transport: { name: 'Transport Authority', short: 'TA', color: '#eab308' },
      security: { name: 'SPG/Security', short: 'SPG', color: '#dc2626' },
    };
    planResult.diversion_directives.forEach(d => {
      const key = d.agency || 'security';
      if (!agencyMap[key]) agencyMap[key] = { ...(AGENCY_LABELS[key] || { name: key, short: key.slice(0,3).toUpperCase(), color: '#94a3b8' }), segments: 0, actions: [] };
      agencyMap[key].segments += 1;
      if (!agencyMap[key].actions.includes(d.action)) agencyMap[key].actions.push(d.action);
    });
    return Object.values(agencyMap);
  }, [planResult]);

  const protocolPhase = useMemo(() => {
    const phases = {
      idle: { label: 'STANDBY', color: '#64748b', step: 0 },
      planning: { label: 'ASL IN PROGRESS', color: '#eab308', step: 1 },
      approved: { label: 'ROUTE LOCKED', color: '#ea580c', step: 2 },
      active: { label: 'ESCORT LIVE', color: '#16a34a', step: 3 },
      completed: { label: 'POST-CLEARANCE', color: '#2563eb', step: 4 },
    };
    return phases[lifecycle] || phases.idle;
  }, [lifecycle]);

  const corridorThreatCount = useMemo(() => {
    if (!corridorSummary) return 0;
    return (corridorSummary.critical_segments || 0) + (corridorSummary.anomaly_count || 0);
  }, [corridorSummary]);

  const secSpec = SECURITY_SPECS[vvipClass] || SECURITY_SPECS['Z+'];

  return (
    <>
      <div 
        className={`sp overflow-y-auto ${lifecycle === 'active' ? 'convoy-active-glow' : ''}`}
        style={{
          position: 'absolute',
          top: 0,
          bottom: 0,
          left: 0,
          width: `${LWIDTH}px`,
          backgroundColor: '#0f172a',
          borderRight: '1px solid #334155',
          boxShadow: '4px 0 24px rgba(0,0,0,0.3)',
          zIndex: 1000,
          transform: open ? 'translateX(0)' : `translateX(-${LWIDTH}px)`,
          transition: 'transform 0.3s cubic-bezier(0.4, 0, 0.2, 1)',
          display: 'flex',
          flexDirection: 'column'
        }}
      >
        {/* VVIP Profile Section */}
        <div style={{ 
          padding: '20px', 
          background: 'linear-gradient(180deg, #1e293b 0%, #0f172a 100%)',
          borderBottom: '1px solid #334155',
          marginBottom: '16px'
        }}>
          <div className="flex gap-4 items-center" style={{ marginBottom: '16px' }}>
            <div style={{ 
              width: '48px', height: '48px', borderRadius: '12px', 
              background: 'linear-gradient(135deg, #1e293b, #334155)', 
              border: '1px solid #475569', display: 'flex', alignItems: 'center', justifyContent: 'center',
              boxShadow: '0 2px 8px rgba(234,88,12,0.2)',
            }}>
              <User size={24} color="#ea580c" strokeWidth={2.5} />
            </div>
            <div>
              <h2 style={{ fontSize: '14px', fontWeight: 700, color: '#e2e8f0' }}>VVIP Convoy Command</h2>
              <div className="flex gap-2 mt-1.5">
                <span style={{
                  padding: '2px 8px', borderRadius: '10px', fontSize: '9px', fontWeight: 800,
                  backgroundColor: secSpec.color + '18', color: secSpec.color, border: `1px solid ${secSpec.color}40`,
                }}>
                  {vvipClass} Security
                </span>
                <span className="badge badge-green" style={{ minWidth: 'auto', padding: '2px 8px', fontSize: '9px' }}>
                  {lifecycle === 'idle' ? 'Standby' : lifecycle.toUpperCase()}
                </span>
              </div>
            </div>
          </div>

          {/* Security spec grid */}
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '6px', marginBottom: '12px' }}>
            {[
              { label: 'Min Lanes', value: secSpec.minLanes || 'Any', icon: '🛣️' },
              { label: 'Closure', value: secSpec.closure, icon: '🚧' },
              { label: 'Advance', value: secSpec.advance, icon: '⏱️' },
              { label: 'Max Queue', value: secSpec.maxQueue, icon: '📏' },
            ].map(stat => (
              <div key={stat.label} style={{ backgroundColor: 'rgba(30,41,59,0.85)', backdropFilter: 'blur(8px)', padding: '8px 10px', borderRadius: '8px', border: '1px solid #334155', transition: 'all 0.2s ease', boxShadow: '0 1px 3px rgba(0,0,0,0.2)' }}>
                <div style={{ fontSize: '8px', color: '#94a3b8', textTransform: 'uppercase', marginBottom: '2px', letterSpacing: '0.5px', display: 'flex', alignItems: 'center', gap: '3px' }}><span style={{ fontSize: '9px' }}>{stat.icon}</span> {stat.label}</div>
                <div style={{ fontSize: '11px', fontWeight: 700, fontFamily: 'var(--font-mono)', color: '#e2e8f0' }}>{stat.value}</div>
              </div>
            ))}
          </div>

          {/* ─── Blue Book Protocol Status ─────────────────────────── */}
          <div style={{ 
            marginBottom: '12px', padding: '10px 12px', borderRadius: '8px',
            background: 'linear-gradient(135deg, rgba(15,23,42,0.95), rgba(30,41,59,0.8))',
            border: `1px solid ${protocolPhase.color}40`,
            boxShadow: `0 0 12px ${protocolPhase.color}15, inset 0 1px 0 rgba(255,255,255,0.03)`,
          }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '8px' }}>
              <Shield size={11} color={protocolPhase.color} />
              <span style={{ fontSize: '9px', fontWeight: 800, color: protocolPhase.color, letterSpacing: '0.1em' }}>
                BLUE BOOK — {protocolPhase.label}
              </span>
            </div>
            {/* Phase Progress Bar */}
            <div style={{ display: 'flex', gap: '3px', marginBottom: '8px' }}>
              {['STANDBY', 'ASL', 'LOCKED', 'ESCORT', 'CLEAR'].map((ph, i) => (
                <div key={ph} style={{ 
                  flex: 1, height: '3px', borderRadius: '2px',
                  backgroundColor: i <= protocolPhase.step ? protocolPhase.color : '#334155',
                  transition: 'background-color 0.5s ease',
                  boxShadow: i <= protocolPhase.step ? `0 0 4px ${protocolPhase.color}50` : 'none',
                }} />
              ))}
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between' }}>
              {['STANDBY', 'ASL', 'LOCKED', 'ESCORT', 'CLEAR'].map((ph, i) => (
                <span key={ph} style={{ 
                  fontSize: '6px', fontWeight: 600, letterSpacing: '0.05em',
                  color: i <= protocolPhase.step ? protocolPhase.color : '#475569',
                }}>{ph}</span>
              ))}
            </div>
            {/* Threat Level + Corridor Intel + Plan B */}
            <div style={{ display: 'flex', gap: '6px', marginTop: '8px' }}>
              <div style={{ 
                flex: 1, padding: '6px 8px', borderRadius: '6px',
                backgroundColor: `${threatLevel.color}12`, border: `1px solid ${threatLevel.color}30`,
              }}>
                <div style={{ fontSize: '7px', color: '#94a3b8', letterSpacing: '0.08em', marginBottom: '2px' }}>THREAT</div>
                <div style={{ fontSize: '10px', fontWeight: 800, color: threatLevel.color, fontFamily: 'var(--font-mono)' }}>
                  {threatLevel.icon} {threatLevel.level}
                </div>
              </div>
              <div style={{ 
                flex: 1, padding: '6px 8px', borderRadius: '6px',
                backgroundColor: 'rgba(37,99,235,0.08)', border: '1px solid rgba(37,99,235,0.2)',
              }}>
                <div style={{ fontSize: '7px', color: '#94a3b8', letterSpacing: '0.08em', marginBottom: '2px' }}>CORRIDOR</div>
                <div style={{ fontSize: '10px', fontWeight: 800, color: corridorSummary?.status === 'red' ? '#dc2626' : corridorSummary?.status === 'amber' ? '#eab308' : '#16a34a', fontFamily: 'var(--font-mono)' }}>
                  {(corridorSummary?.status || 'N/A').toUpperCase()}
                </div>
              </div>
              <div style={{ 
                flex: 1, padding: '6px 8px', borderRadius: '6px',
                backgroundColor: planB.active ? 'rgba(220,38,38,0.12)' : 'rgba(234,88,12,0.08)', 
                border: `1px solid ${planB.active ? 'rgba(220,38,38,0.4)' : 'rgba(234,88,12,0.2)'}`,
              }}>
                <div style={{ fontSize: '7px', color: '#94a3b8', letterSpacing: '0.08em', marginBottom: '2px' }}>PLAN B</div>
                <div style={{ fontSize: '10px', fontWeight: 800, color: planB.active ? '#dc2626' : planB.altRouteSanitised ? '#16a34a' : '#64748b', fontFamily: 'var(--font-mono)' }}>
                  {planB.active ? '⚡ ON' : planB.altRouteSanitised ? 'READY' : 'OFF'}
                </div>
              </div>
            </div>
          </div>

          {/* GPU Health Chip */}
          {gpuHealth && (
            <div style={{ padding: '12px', backgroundColor: '#0f172a', borderRadius: '10px', border: '1px solid #334155', color: '#f8fafc', boxShadow: '0 4px 16px rgba(15,23,42,0.4), inset 0 1px 0 rgba(255,255,255,0.05)' }}>
               <div className="flex items-center gap-2 mb-2">
                 <Cpu size={12} color="#3b82f6" />
                 <span style={{ fontSize: '10px', fontWeight: 700, letterSpacing: '0.08em', color: '#94a3b8' }}>AI CO-PROCESSOR</span>
                 <span style={{ fontSize: '7px', fontFamily: 'var(--font-mono)', color: '#3b82f6', backgroundColor: '#1e293b', padding: '1px 5px', borderRadius: '4px', border: '1px solid #334155' }}>RTX 4070</span>
               </div>
               
               <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px', marginBottom: '8px' }}>
                 <div>
                    <div className="flex justify-between items-center mb-1">
                       <span style={{ fontSize: '9px', color: '#cbd5e1' }}>VRAM</span>
                       <span style={{ fontSize: '9px', fontFamily: 'var(--font-mono)', fontWeight: 600 }}>
                         {gpuHealth.vramTotalMb ? ((gpuHealth.vramUsedMb / gpuHealth.vramTotalMb) * 100).toFixed(0) : 0}%
                       </span>
                    </div>
                    <div style={{ height: '3px', backgroundColor: '#334155', borderRadius: '2px', overflow: 'hidden' }}>
                       <div style={{ height: '100%', width: `${(gpuHealth.vramUsedMb / gpuHealth.vramTotalMb) * 100}%`, backgroundColor: '#3b82f6', transition: 'width 0.5s ease' }} />
                    </div>
                    <div style={{ fontSize: '8px', color: '#64748b', marginTop: '2px' }}>{gpuHealth.vramUsedMb}/{gpuHealth.vramTotalMb}MB</div>
                 </div>
                 <div>
                    <div className="flex justify-between items-center mb-1">
                       <span style={{ fontSize: '9px', color: '#cbd5e1' }}>TEMP</span>
                       <span style={{ fontSize: '9px', fontFamily: 'var(--font-mono)', fontWeight: 600, color: gpuHealth.temperature > 80 ? '#ef4444' : '#f8fafc' }}>{gpuHealth.temperature}°C</span>
                    </div>
                    <div style={{ height: '3px', backgroundColor: '#334155', borderRadius: '2px', overflow: 'hidden' }}>
                       <div style={{ height: '100%', width: `${Math.min(100, (gpuHealth.temperature / 90) * 100)}%`, backgroundColor: gpuHealth.temperature > 80 ? '#ef4444' : '#eab308', transition: 'width 0.5s ease' }} />
                    </div>
                    <div style={{ fontSize: '8px', color: '#64748b', marginTop: '2px' }}>GPU-0</div>
                 </div>
               </div>

               <div style={{ display: 'flex', gap: '6px', fontSize: '8px', color: '#cbd5e1', flexWrap: 'wrap' }}>
                 <div style={{ padding: '2px 6px', backgroundColor: '#1e293b', borderRadius: '4px', border: '1px solid #334155' }}>
                   Qwen: <span style={{ color: '#3b82f6', fontFamily: 'var(--font-mono)' }}>{gpuHealth.allocations?.ollamaQwen || 5632}MB</span>
                 </div>
                 <div style={{ padding: '2px 6px', backgroundColor: '#1e293b', borderRadius: '4px', border: '1px solid #334155' }}>
                   ONNX: <span style={{ color: '#10b981', fontFamily: 'var(--font-mono)' }}>{gpuHealth.allocations?.onnxDstgat || 409}MB</span>
                 </div>
                 <div style={{ padding: '2px 6px', backgroundColor: '#1e293b', borderRadius: '4px', border: '1px solid #334155' }}>
                   Util: <span style={{ color: '#10b981', fontFamily: 'var(--font-mono)' }}>{gpuHealth.gpuUtilPercent || 0}%</span>
                 </div>
               </div>
            </div>
          )}
        </div>

        {/* Convoy Planner Section */}
        <div style={{ marginBottom: '16px' }}>
          <SHdr title="Convoy Planner" />
          <div style={{ padding: '12px 14px' }}>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
              <div style={{ position: 'relative' }}>
                <label style={{ fontSize: '10px', fontWeight: 600, color: '#94a3b8', display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '4px' }}>
                  <IconOriginMarker size={12} color="#16a34a" /> ORIGIN
                </label>
                <input value={origin} onChange={e => { setOrigin(e.target.value); setOriginSuggestions(filterLocations(e.target.value)); }} onFocus={() => setOriginSuggestions(filterLocations(origin))} onBlur={() => setTimeout(() => setOriginSuggestions([]), 150)} style={{ width: '100%', padding: '8px 10px', borderRadius: '6px', border: '1px solid #334155', fontSize: '12px', backgroundColor: '#1e293b', color: '#e2e8f0', outline: 'none', boxSizing: 'border-box' }} />
                {originSuggestions.length > 0 && (
                  <div style={{ position: 'absolute', top: '100%', left: 0, right: 0, zIndex: 2000, backgroundColor: '#1e293b', border: '1px solid #334155', borderRadius: '0 0 6px 6px', boxShadow: '0 4px 12px rgba(0,0,0,0.4)', maxHeight: '140px', overflowY: 'auto' }}>
                    {originSuggestions.map(s => (
                      <div key={s} onMouseDown={() => { setOrigin(s); setOriginSuggestions([]); const coords = resolveCoords(s); if (coords) { flyToLocation(coords[1], coords[0]); setTempOriginCoords(coords); } }} style={{ padding: '6px 10px', fontSize: '11px', cursor: 'pointer', borderBottom: '1px solid #334155', color: '#e2e8f0' }} onMouseEnter={e => e.currentTarget.style.backgroundColor = '#334155'} onMouseLeave={e => e.currentTarget.style.backgroundColor = '#1e293b'}>
                        {s}
                      </div>
                    ))}
                  </div>
                )}
              </div>

              <div style={{ position: 'relative' }}>
                <label style={{ fontSize: '10px', fontWeight: 600, color: '#94a3b8', display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '4px' }}>
                  <IconDestinationMarker size={12} color="#dc2626" /> DESTINATION
                </label>
                <input value={destination} onChange={e => { setDestination(e.target.value); setDestSuggestions(filterLocations(e.target.value)); }} onFocus={() => setDestSuggestions(filterLocations(destination))} onBlur={() => setTimeout(() => setDestSuggestions([]), 150)} style={{ width: '100%', padding: '8px 10px', borderRadius: '6px', border: '1px solid #334155', fontSize: '12px', backgroundColor: '#1e293b', color: '#e2e8f0', outline: 'none', boxSizing: 'border-box' }} />
                {destSuggestions.length > 0 && (
                  <div style={{ position: 'absolute', top: '100%', left: 0, right: 0, zIndex: 2000, backgroundColor: '#1e293b', border: '1px solid #334155', borderRadius: '0 0 6px 6px', boxShadow: '0 4px 12px rgba(0,0,0,0.4)', maxHeight: '140px', overflowY: 'auto' }}>
                    {destSuggestions.map(s => (
                      <div key={s} onMouseDown={() => { setDestination(s); setDestSuggestions([]); const coords = resolveCoords(s); if (coords) { flyToLocation(coords[1], coords[0]); setTempDestCoords(coords); } }} style={{ padding: '6px 10px', fontSize: '11px', cursor: 'pointer', borderBottom: '1px solid #334155', color: '#e2e8f0' }} onMouseEnter={e => e.currentTarget.style.backgroundColor = '#334155'} onMouseLeave={e => e.currentTarget.style.backgroundColor = '#1e293b'}>
                        {s}
                      </div>
                    ))}
                  </div>
                )}
              </div>

              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px' }}>
                <div>
                  <label style={{ fontSize: '10px', fontWeight: 600, color: '#94a3b8', marginBottom: '4px', display: 'block' }}>
                    <IconVVIPShield size={12} color="#ea580c" /> TIER
                  </label>
                  <select value={vvipClass} onChange={e => setVvipClass(e.target.value)} style={{ width: '100%', padding: '8px', borderRadius: '6px', border: '1px solid #334155', fontSize: '11px', backgroundColor: '#1e293b', outline: 'none', fontWeight: 600, color: secSpec.color, boxSizing: 'border-box' }}>
                    <option value="SPG">SPG (PM Exclusive)</option>
                    <option value="Z+">Z+ (Maximum)</option>
                    <option value="Z">Z (High)</option>
                    <option value="Y+">Y+ (Moderate+)</option>
                    <option value="Y">Y (Standard)</option>
                    <option value="X">X (Minimal)</option>
                  </select>
                </div>
                <div>
                  <label style={{ fontSize: '10px', fontWeight: 600, color: '#94a3b8', marginBottom: '4px', display: 'block' }}>
                    <Timer size={12} color="#94a3b8" /> DEPARTURE
                  </label>
                  <input type="time" value={plannedDeparture} onChange={e => setPlannedDeparture(e.target.value)} style={{ width: '100%', padding: '8px', borderRadius: '6px', border: '1px solid #334155', fontSize: '11px', backgroundColor: '#1e293b', color: '#e2e8f0', outline: 'none', fontFamily: 'var(--font-mono)', boxSizing: 'border-box' }} />
                </div>
              </div>

              {/* AI Prediction Box — Real Data */}
              <div style={{ 
                background: 'linear-gradient(135deg, #1e293b, #0f172a)', 
                border: '1px solid #475569', 
                borderRadius: '10px',
                padding: '12px',
              }}>
                <div className="flex items-center justify-between mb-2">
                  <div className="flex items-center gap-2">
                    <Brain size={14} color="#ea580c" />
                    <span style={{ fontSize: '10px', fontWeight: 700, color: '#ea580c', letterSpacing: '0.05em' }}>DSTGAT + HistGBT PREDICTION</span>
                  </div>
                  {predictionLoading && (
                    <div className="animate-spin" style={{ width: '10px', height: '10px', border: '2px solid #ea580c', borderTopColor: 'transparent', borderRadius: '50%' }} />
                  )}
                </div>

                {etaPrediction ? (
                  <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '8px' }}>
                    <div>
                      <div style={{ fontSize: '9px', color: '#ea580c', fontWeight: 600 }}>ETA ({etaPrediction.model === 'histgbt' ? 'HistGBT' : 'Estimate'})</div>
                      <div style={{ fontSize: '16px', fontWeight: 700, color: '#c2410c', fontFamily: 'var(--font-mono)' }}>
                        {Math.round((etaPrediction.eta_seconds || 0) / 60)} min
                      </div>
                    </div>
                    <div style={{ textAlign: 'center' }}>
                      <div style={{ fontSize: '9px', color: '#ea580c', fontWeight: 600 }}>Model</div>
                      <div style={{ fontSize: '16px', fontWeight: 700, color: etaPrediction.model === 'histgbt' ? '#16a34a' : '#ea580c', fontFamily: 'var(--font-mono)' }}>
                        {etaPrediction.model === 'histgbt' ? 'ML' : 'FBK'}
                      </div>
                    </div>
                    <div style={{ textAlign: 'right' }}>
                      <div style={{ fontSize: '9px', color: '#ea580c', fontWeight: 600 }}>Distance</div>
                      <div style={{ fontSize: '16px', fontWeight: 700, color: '#c2410c', fontFamily: 'var(--font-mono)' }}>
                        {routeComparison?.[0]?.total_distance_m ? (routeComparison[0].total_distance_m / 1000).toFixed(1) : '--'} km
                      </div>
                    </div>
                  </div>
                ) : (
                  <div style={{ fontSize: '11px', color: '#94a3b8', padding: '8px 0' }}>Awaiting prediction data...</div>
                )}

                {/* Route comparison chart */}
                {routeChartData.length > 0 && (
                  <div style={{ width: '100%', height: '90px', marginTop: '4px' }}>
                    <ResponsiveContainer width="100%" height="100%">
                      <BarChart data={routeChartData} margin={{ top: 0, right: 0, left: 0, bottom: 0 }}>
                        <Tooltip contentStyle={{ fontSize: '10px', padding: '4px 8px', borderRadius: '4px', backgroundColor: '#1e293b', border: '1px solid #334155', color: '#e2e8f0' }} cursor={{ fill: '#475569aa' }} />
                        <XAxis dataKey="name" tick={{ fontSize: 9 }} />
                        <Bar dataKey="Security" fill="#16a34a" radius={[2, 2, 0, 0]} barSize={14} />
                        <Bar dataKey="Flow" fill="#3b82f6" radius={[2, 2, 0, 0]} barSize={14} />
                        <Bar dataKey="Composite" fill="#ea580c" radius={[2, 2, 0, 0]} barSize={14} />
                      </BarChart>
                    </ResponsiveContainer>
                    <div style={{ display: 'flex', justifyContent: 'center', gap: '10px', marginTop: '2px' }}>
                      <span style={{ fontSize: '8px', fontWeight: 600, color: '#16a34a' }}>● Security</span>
                      <span style={{ fontSize: '8px', fontWeight: 600, color: '#3b82f6' }}>● Flow</span>
                      <span style={{ fontSize: '8px', fontWeight: 600, color: '#ea580c' }}>● Composite</span>
                    </div>
                  </div>
                )}

                {/* Flow forecast mini-chart */}
                {flowChartData.length > 0 && (
                  <div style={{ marginTop: '8px', paddingTop: '8px', borderTop: '1px solid #475569' }}>
                    <div style={{ fontSize: '9px', fontWeight: 700, color: '#ea580c', marginBottom: '4px' }}>TRAFFIC FLOW FORECAST (DSTGAT)</div>
                    <div style={{ width: '100%', height: '60px' }}>
                      <ResponsiveContainer width="100%" height="100%">
                        <LineChart data={flowChartData} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
                          <CartesianGrid strokeDasharray="3 3" stroke="#47556933" />
                          <XAxis dataKey="horizon" tick={{ fontSize: 8, fill: '#94a3b8' }} />
                          <Line type="monotone" dataKey="Speed" stroke="#2563eb" strokeWidth={2} dot={{ r: 2 }} />
                          <Line type="monotone" dataKey="Congestion" stroke="#dc2626" strokeWidth={2} dot={{ r: 2 }} />
                          <Tooltip contentStyle={{ fontSize: '9px', padding: '3px 6px', borderRadius: '4px', backgroundColor: '#1e293b', border: '1px solid #334155', color: '#e2e8f0' }} />
                        </LineChart>
                      </ResponsiveContainer>
                      <div style={{ display: 'flex', justifyContent: 'center', gap: '10px' }}>
                        <span style={{ fontSize: '8px', fontWeight: 600, color: '#2563eb' }}>● Speed km/h</span>
                        <span style={{ fontSize: '8px', fontWeight: 600, color: '#dc2626' }}>● Congestion %</span>
                      </div>
                    </div>
                  </div>
                )}
              </div>

              {/* Plan result summary */}
              {planResult && (
                <div style={{ padding: '10px', borderRadius: '8px', border: '1px solid #16a34a55', backgroundColor: 'rgba(22,163,74,0.08)' }}>
                  <div className="flex items-center gap-2 mb-2">
                    <Zap size={12} color="#16a34a" />
                    <span style={{ fontSize: '10px', fontWeight: 700, color: '#16a34a' }}>PLAN: {planResult.status?.toUpperCase()}</span>
                    <span style={{ fontSize: '9px', color: '#94a3b8', marginLeft: 'auto' }}>Confidence: {planResult.confidence}</span>
                  </div>
                  {planResult.primary_route && (
                    <div style={{ fontSize: '10px', color: '#e2e8f0' }}>
                      Primary route score: <strong>{planResult.primary_route.score?.toFixed(2)}</strong> — {planResult.primary_route.reason}
                    </div>
                  )}
                  {planResult.security_violations?.length > 0 && (
                    <div style={{ marginTop: '6px', padding: '6px', backgroundColor: 'rgba(220,38,38,0.1)', borderRadius: '4px', fontSize: '9px', color: '#fca5a5' }}>
                      <AlertTriangle size={10} style={{ display: 'inline', verticalAlign: 'middle' }} /> {planResult.security_violations.length} security violation(s)
                    </div>
                  )}
                </div>
              )}

              {/* ─── LIVE CONVOY ANALYTICS ───────────────────────── */}
              {convoySimulation?.active && (
                <div style={{
                  padding: '12px', borderRadius: '10px',
                  background: 'linear-gradient(135deg, rgba(234,88,12,0.06), rgba(15,23,42,0.95))',
                  border: '1px solid rgba(234,88,12,0.3)',
                  boxShadow: '0 0 12px rgba(234,88,12,0.08)',
                }}>
                  <div className="flex items-center gap-2" style={{ marginBottom: '10px' }}>
                    <Activity size={12} color="#ea580c" />
                    <span style={{ fontSize: '9px', fontWeight: 800, color: '#ea580c', letterSpacing: '0.08em' }}>LIVE CONVOY ANALYTICS</span>
                    <div className="live-data-dot" style={{ marginLeft: 'auto' }} />
                  </div>

                  {/* Speed over time — enhanced AreaChart with gradient + reference lines */}
                  <div style={{ marginBottom: '10px' }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: '4px' }}>
                      <span style={{ fontSize: '8px', fontWeight: 700, color: '#94a3b8', letterSpacing: '0.06em' }}>SPEED (km/h)</span>
                      <span style={{ fontSize: '8px', color: '#64748b', fontFamily: 'var(--font-mono)' }}>
                        avg {convoySimulation.avgSpeed?.toFixed(0) ?? '—'} · max {convoySimulation.maxSpeed?.toFixed(0) ?? '—'}
                      </span>
                    </div>
                    <div style={{ height: '85px' }}>
                      <ResponsiveContainer width="100%" height="100%">
                        <AreaChart data={convoySimulation.speedHistory.map((d, i) => ({ i, v: d.speed }))}>
                          <defs>
                            <linearGradient id="speedGradL" x1="0" y1="0" x2="0" y2="1">
                              <stop offset="5%" stopColor="#ea580c" stopOpacity={0.35} />
                              <stop offset="95%" stopColor="#ea580c" stopOpacity={0.02} />
                            </linearGradient>
                          </defs>
                          <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                          <XAxis dataKey="i" hide />
                          <YAxis domain={[0, 60]} tick={{ fontSize: 8, fill: '#64748b' }} width={24} />
                          <Tooltip
                            contentStyle={{ backgroundColor: '#1e293b', border: '1px solid #475569', borderRadius: '6px', fontSize: '9px' }}
                            labelFormatter={() => ''}
                            formatter={(v) => [`${v.toFixed(1)} km/h`, 'Speed']}
                          />
                          {convoySimulation.avgSpeed > 0 && (
                            <ReferenceLine y={convoySimulation.avgSpeed} stroke="#fbbf24" strokeDasharray="4 4" strokeWidth={1} strokeOpacity={0.6} />
                          )}
                          <Area type="monotone" dataKey="v" stroke="#ea580c" fill="url(#speedGradL)" strokeWidth={1.5} dot={false} isAnimationActive={false} />
                        </AreaChart>
                      </ResponsiveContainer>
                    </div>
                  </div>

                  {/* Congestion timeline — enhanced with danger zone */}
                  <div style={{ marginBottom: '10px' }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: '4px' }}>
                      <span style={{ fontSize: '8px', fontWeight: 700, color: '#94a3b8', letterSpacing: '0.06em' }}>CONGESTION INDEX</span>
                      <span style={{ fontSize: '8px', color: (convoySimulation.congestion ?? 0) > 0.6 ? '#dc2626' : (convoySimulation.congestion ?? 0) > 0.3 ? '#eab308' : '#22c55e', fontFamily: 'var(--font-mono)', fontWeight: 700 }}>
                        {((convoySimulation.congestion ?? 0) * 100).toFixed(0)}%
                      </span>
                    </div>
                    <div style={{ height: '85px' }}>
                      <ResponsiveContainer width="100%" height="100%">
                        <AreaChart data={convoySimulation.congestionHistory.map((d, i) => ({ i, v: d.congestion }))}>
                          <defs>
                            <linearGradient id="congGradL" x1="0" y1="0" x2="0" y2="1">
                              <stop offset="5%" stopColor="#dc2626" stopOpacity={0.35} />
                              <stop offset="50%" stopColor="#f97316" stopOpacity={0.15} />
                              <stop offset="95%" stopColor="#22c55e" stopOpacity={0.02} />
                            </linearGradient>
                          </defs>
                          <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                          <XAxis dataKey="i" hide />
                          <YAxis domain={[0, 1]} tick={{ fontSize: 8, fill: '#64748b' }} width={24} tickFormatter={v => `${(v * 100).toFixed(0)}%`} />
                          <Tooltip
                            contentStyle={{ backgroundColor: '#1e293b', border: '1px solid #475569', borderRadius: '6px', fontSize: '9px' }}
                            labelFormatter={() => ''}
                            formatter={(v) => [`${(v * 100).toFixed(0)}%`, 'Congestion']}
                          />
                          <ReferenceLine y={0.8} stroke="#dc2626" strokeDasharray="3 3" strokeWidth={1} strokeOpacity={0.5} label={{ value: 'SPIKE', fill: '#dc2626', fontSize: 7, position: 'left' }} />
                          <Area type="monotone" dataKey="v" stroke="#dc2626" fill="url(#congGradL)" strokeWidth={1.5} dot={false} isAnimationActive={false} />
                        </AreaChart>
                      </ResponsiveContainer>
                    </div>
                  </div>

                  {/* ETA trend — enhanced with gradient */}
                  <div style={{ marginBottom: '10px' }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: '4px' }}>
                      <span style={{ fontSize: '8px', fontWeight: 700, color: '#94a3b8', letterSpacing: '0.06em' }}>ETA (seconds)</span>
                      <span style={{ fontSize: '8px', color: '#3b82f6', fontFamily: 'var(--font-mono)', fontWeight: 700 }}>
                        {convoySimulation.etaSeconds != null ? `${Math.floor(convoySimulation.etaSeconds / 60)}:${String(Math.floor(convoySimulation.etaSeconds % 60)).padStart(2, '0')}` : '--:--'}
                      </span>
                    </div>
                    <div style={{ height: '75px' }}>
                      <ResponsiveContainer width="100%" height="100%">
                        <AreaChart data={convoySimulation.etaHistory.map((d, i) => ({ i, v: d.eta }))}>
                          <defs>
                            <linearGradient id="etaGradL" x1="0" y1="0" x2="0" y2="1">
                              <stop offset="5%" stopColor="#3b82f6" stopOpacity={0.25} />
                              <stop offset="95%" stopColor="#3b82f6" stopOpacity={0.02} />
                            </linearGradient>
                          </defs>
                          <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                          <XAxis dataKey="i" hide />
                          <YAxis tick={{ fontSize: 8, fill: '#64748b' }} width={30} />
                          <Tooltip
                            contentStyle={{ backgroundColor: '#1e293b', border: '1px solid #475569', borderRadius: '6px', fontSize: '9px' }}
                            labelFormatter={() => ''}
                            formatter={(v) => [`${v.toFixed(0)}s`, 'ETA']}
                          />
                          <Area type="monotone" dataKey="v" stroke="#3b82f6" fill="url(#etaGradL)" strokeWidth={1.5} dot={false} isAnimationActive={false} />
                        </AreaChart>
                      </ResponsiveContainer>
                    </div>
                  </div>

                  {/* Speed vs Congestion Correlation */}
                  <div style={{ marginBottom: '10px' }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: '4px' }}>
                      <span style={{ fontSize: '8px', fontWeight: 700, color: '#94a3b8', letterSpacing: '0.06em' }}>SPEED vs CONGESTION</span>
                      <div style={{ display: 'flex', gap: '8px' }}>
                        <span style={{ fontSize: '7px', color: '#ea580c' }}>● Spd</span>
                        <span style={{ fontSize: '7px', color: '#dc2626' }}>● Cng</span>
                      </div>
                    </div>
                    <div style={{ height: '90px' }}>
                      <ResponsiveContainer width="100%" height="100%">
                        <AreaChart data={convoySimulation.speedHistory.map((d, i) => ({
                          i,
                          speed: d.speed,
                          congestion: (convoySimulation.congestionHistory?.[i]?.congestion ?? 0) * 60
                        }))} margin={{ top: 2, right: 0, left: 0, bottom: 0 }}>
                          <defs>
                            <linearGradient id="spdGradComp" x1="0" y1="0" x2="0" y2="1">
                              <stop offset="0%" stopColor="#ea580c" stopOpacity={0.3} />
                              <stop offset="100%" stopColor="#ea580c" stopOpacity={0} />
                            </linearGradient>
                            <linearGradient id="congGradComp" x1="0" y1="0" x2="0" y2="1">
                              <stop offset="0%" stopColor="#dc2626" stopOpacity={0.2} />
                              <stop offset="100%" stopColor="#dc2626" stopOpacity={0} />
                            </linearGradient>
                          </defs>
                          <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                          <XAxis dataKey="i" hide />
                          <YAxis domain={[0, 60]} tick={{ fontSize: 7, fill: '#64748b' }} width={22} />
                          <Tooltip
                            contentStyle={{ backgroundColor: '#1e293b', border: '1px solid #475569', borderRadius: '6px', fontSize: '9px' }}
                            formatter={(v, name) => [`${name === 'speed' ? v.toFixed(1) + ' km/h' : ((v / 60) * 100).toFixed(0) + '%'}`, name === 'speed' ? 'Speed' : 'Congestion']}
                          />
                          <Area type="monotone" dataKey="speed" stroke="#ea580c" fill="url(#spdGradComp)" strokeWidth={1.5} dot={false} isAnimationActive={false} />
                          <Area type="monotone" dataKey="congestion" stroke="#dc2626" fill="url(#congGradComp)" strokeWidth={1} dot={false} isAnimationActive={false} strokeDasharray="4 2" />
                        </AreaChart>
                      </ResponsiveContainer>
                    </div>
                  </div>

                  {/* Acceleration Timeline */}
                  {convoySimulation.speedHistory.length > 2 && (
                  <div style={{ marginBottom: '10px' }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: '4px' }}>
                      <span style={{ fontSize: '8px', fontWeight: 700, color: '#94a3b8', letterSpacing: '0.06em' }}>ACCELERATION (Δv)</span>
                      <span style={{ fontSize: '8px', color: (convoySimulation.acceleration ?? 0) >= 0 ? '#22c55e' : '#dc2626', fontFamily: 'var(--font-mono)', fontWeight: 700 }}>
                        {(convoySimulation.acceleration ?? 0) > 0 ? '+' : ''}{(convoySimulation.acceleration ?? 0).toFixed(1)} km/h/s
                      </span>
                    </div>
                    <div style={{ height: '70px' }}>
                      <ResponsiveContainer width="100%" height="100%">
                        <AreaChart data={convoySimulation.speedHistory.slice(1).map((d, i) => ({
                          i, v: Number((d.speed - convoySimulation.speedHistory[i].speed).toFixed(2))
                        }))}>
                          <defs>
                            <linearGradient id="accelGradL" x1="0" y1="0" x2="0" y2="1">
                              <stop offset="5%" stopColor="#f97316" stopOpacity={0.25} />
                              <stop offset="95%" stopColor="#f97316" stopOpacity={0} />
                            </linearGradient>
                          </defs>
                          <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                          <XAxis dataKey="i" hide />
                          <YAxis tick={{ fontSize: 7, fill: '#64748b' }} width={24} />
                          <ReferenceLine y={0} stroke="#475569" strokeWidth={1} />
                          <Tooltip
                            contentStyle={{ backgroundColor: '#1e293b', border: '1px solid #475569', borderRadius: '6px', fontSize: '9px' }}
                            labelFormatter={() => ''}
                            formatter={(v) => [`${v > 0 ? '+' : ''}${Number(v).toFixed(2)}`, 'Δv']}
                          />
                          <Area type="monotone" dataKey="v" stroke="#f97316" fill="url(#accelGradL)" strokeWidth={1.5} dot={false} isAnimationActive={false} />
                        </AreaChart>
                      </ResponsiveContainer>
                    </div>
                  </div>
                  )}

                  {/* Speed Distribution Histogram */}
                  {convoySimulation.speedHistory.length > 5 && (() => {
                    const sh = convoySimulation.speedHistory;
                    const bins = [
                      { range: '0-15', count: sh.filter(s => s.speed <= 15).length, fill: '#dc2626' },
                      { range: '16-30', count: sh.filter(s => s.speed > 15 && s.speed <= 30).length, fill: '#f97316' },
                      { range: '31-45', count: sh.filter(s => s.speed > 30 && s.speed <= 45).length, fill: '#eab308' },
                      { range: '46+', count: sh.filter(s => s.speed > 45).length, fill: '#22c55e' },
                    ];
                    return (
                    <div style={{ marginBottom: '10px' }}>
                      <div style={{ fontSize: '8px', fontWeight: 700, color: '#94a3b8', letterSpacing: '0.06em', marginBottom: '4px' }}>SPEED DISTRIBUTION (km/h)</div>
                      <div style={{ height: '72px' }}>
                        <ResponsiveContainer width="100%" height="100%">
                          <BarChart data={bins} layout="vertical" margin={{ top: 0, right: 4, left: 0, bottom: 0 }}>
                            <XAxis type="number" tick={{ fontSize: 7, fill: '#64748b' }} />
                            <YAxis dataKey="range" type="category" tick={{ fontSize: 7, fill: '#94a3b8' }} width={32} />
                            <Tooltip contentStyle={{ backgroundColor: '#1e293b', border: '1px solid #475569', borderRadius: '6px', fontSize: '9px' }} />
                            <Bar dataKey="count" radius={[0, 3, 3, 0]}>
                              {bins.map((b, i) => <Cell key={i} fill={b.fill} />)}
                            </Bar>
                          </BarChart>
                        </ResponsiveContainer>
                      </div>
                    </div>
                    );
                  })()}

                  {/* Enhanced live metrics strip — 2 rows */}
                  <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '5px', marginTop: '10px' }}>
                    {[
                      { label: 'SPEED', value: `${convoySimulation.speed?.toFixed(0) || '0'}`, unit: 'km/h', color: convoySimulation.speed > 40 ? '#22c55e' : convoySimulation.speed > 20 ? '#eab308' : '#dc2626' },
                      { label: 'PROGRESS', value: `${((convoySimulation.progress || 0) * 100).toFixed(1)}`, unit: '%', color: '#f97316' },
                      { label: 'ETA', value: convoySimulation.etaSeconds != null ? `${Math.floor(convoySimulation.etaSeconds / 60)}:${String(Math.floor(convoySimulation.etaSeconds % 60)).padStart(2, '0')}` : '--', unit: '', color: '#3b82f6' },
                      { label: 'HEADING', value: `${convoySimulation.heading?.toFixed(0) || '0'}`, unit: '°', color: '#e2e8f0' },
                      { label: 'ACCEL', value: `${(convoySimulation.acceleration ?? 0) > 0 ? '+' : ''}${(convoySimulation.acceleration ?? 0).toFixed(1)}`, unit: 'km/h²', color: (convoySimulation.acceleration ?? 0) > 0 ? '#22c55e' : '#dc2626' },
                      { label: 'SEGMENTS', value: `${convoySimulation.segmentsTraversed ?? 0}/${convoySimulation.segmentIds?.length ?? 0}`, unit: '', color: '#a78bfa' },
                    ].map(m => (
                      <div key={m.label} style={{ textAlign: 'center', padding: '5px 3px', borderRadius: '6px', backgroundColor: '#0f172a', border: '1px solid #1e293b' }}>
                        <div style={{ fontSize: '7px', color: '#64748b', letterSpacing: '0.05em', marginBottom: '2px' }}>{m.label}</div>
                        <div style={{ fontSize: '12px', fontWeight: 800, fontFamily: 'var(--font-mono)', color: m.color }}>
                          {m.value}<span style={{ fontSize: '7px', fontWeight: 500, color: '#64748b', marginLeft: '1px' }}>{m.unit}</span>
                        </div>
                      </div>
                    ))}
                  </div>

                  {/* ─── R2: Convoy Health Radar ─────────────────────── */}
                  <div style={{ marginTop: '10px', padding: '8px', borderRadius: '8px', background: 'rgba(15,23,42,0.8)', border: '1px solid #334155' }}>
                    <div style={{ fontSize: '8px', fontWeight: 700, color: '#94a3b8', letterSpacing: '0.08em', marginBottom: '4px' }}>CONVOY HEALTH RADAR</div>
                    <div style={{ height: '140px' }}>
                      <ResponsiveContainer width="100%" height="100%">
                        <RadarChart data={[
                          { axis: 'Speed', v: Math.min(100, (convoySimulation.speed / 50) * 100) },
                          { axis: 'Safety', v: Math.max(0, 100 - (convoySimulation.congestion ?? 0) * 100) },
                          { axis: 'ETA', v: Math.min(100, convoySimulation.etaSeconds > 0 ? Math.max(0, 100 - (convoySimulation.etaSeconds / 600) * 100) : 50) },
                          { axis: 'Fuel', v: convoySimulation.fuelPct ?? 100 },
                          { axis: 'Progress', v: (convoySimulation.progress ?? 0) * 100 },
                          { axis: 'Stability', v: Math.max(0, 100 - (convoySimulation.gForce ?? 0) * 200) },
                        ]} cx="50%" cy="50%" outerRadius="70%">
                          <PolarGrid stroke="#334155" />
                          <PolarAngleAxis dataKey="axis" tick={{ fontSize: 7, fill: '#94a3b8' }} />
                          <Radar dataKey="v" stroke="#ea580c" fill="#ea580c" fillOpacity={0.2} strokeWidth={1.5} />
                        </RadarChart>
                      </ResponsiveContainer>
                    </div>
                  </div>

                  {/* ─── R2: Fuel + Threat + Zone + G-Force Strip ────── */}
                  <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '5px', marginTop: '8px' }}>
                    {/* Fuel gauge */}
                    <div style={{ padding: '6px 8px', borderRadius: '6px', backgroundColor: '#0f172a', border: '1px solid #1e293b' }}>
                      <div style={{ fontSize: '7px', color: '#64748b', letterSpacing: '0.05em', marginBottom: '3px', display: 'flex', justifyContent: 'space-between' }}>
                        <span>FUEL</span>
                        <span style={{ color: (convoySimulation.fuelPct ?? 100) > 50 ? '#22c55e' : (convoySimulation.fuelPct ?? 100) > 20 ? '#eab308' : '#ef4444', fontFamily: 'var(--font-mono)', fontWeight: 700 }}>
                          {(convoySimulation.fuelPct ?? 100).toFixed(0)}%
                        </span>
                      </div>
                      <div style={{ height: '4px', background: '#1e293b', borderRadius: '2px', overflow: 'hidden' }}>
                        <div style={{ height: '100%', width: `${convoySimulation.fuelPct ?? 100}%`, background: 'linear-gradient(90deg, #ef4444, #f59e0b, #22c55e)', borderRadius: '2px', transition: 'width 0.3s' }} />
                      </div>
                    </div>
                    {/* G-Force */}
                    <div style={{ padding: '6px 8px', borderRadius: '6px', backgroundColor: '#0f172a', border: '1px solid #1e293b' }}>
                      <div style={{ fontSize: '7px', color: '#64748b', letterSpacing: '0.05em', marginBottom: '3px' }}>G-FORCE</div>
                      <div style={{ fontSize: '14px', fontWeight: 800, fontFamily: 'var(--font-mono)', color: (convoySimulation.gForce ?? 0) > 0.5 ? '#f97316' : (convoySimulation.gForce ?? 0) > 0.2 ? '#eab308' : '#64748b' }}>
                        {(convoySimulation.gForce ?? 0).toFixed(2)}<span style={{ fontSize: '8px', color: '#64748b' }}>G</span>
                      </div>
                    </div>
                    {/* Zone */}
                    <div style={{ padding: '6px 8px', borderRadius: '6px', backgroundColor: '#0f172a', border: '1px solid #1e293b' }}>
                      <div style={{ fontSize: '7px', color: '#64748b', letterSpacing: '0.05em', marginBottom: '3px' }}>ZONE</div>
                      <div style={{ fontSize: '10px', fontWeight: 700, color: '#a78bfa', textTransform: 'capitalize', fontFamily: 'var(--font-mono)' }}>
                        {convoySimulation.currentZone || '—'}
                      </div>
                    </div>
                    {/* Threat Level */}
                    <div style={{
                      padding: '6px 8px', borderRadius: '6px', backgroundColor: '#0f172a',
                      border: `1px solid ${convoySimulation.threatLevel === 'critical' ? '#dc262650' : convoySimulation.threatLevel === 'elevated' ? '#ea580c40' : '#1e293b'}`,
                    }}>
                      <div style={{ fontSize: '7px', color: '#64748b', letterSpacing: '0.05em', marginBottom: '3px' }}>THREAT</div>
                      <div style={{
                        fontSize: '10px', fontWeight: 800, fontFamily: 'var(--font-mono)', textTransform: 'uppercase',
                        color: convoySimulation.threatLevel === 'critical' ? '#ef4444' : convoySimulation.threatLevel === 'elevated' ? '#f97316' : convoySimulation.threatLevel === 'moderate' ? '#eab308' : convoySimulation.threatLevel === 'guarded' ? '#38bdf8' : '#22c55e',
                      }}>
                        {convoySimulation.threatLevel || 'nominal'}
                      </div>
                    </div>
                  </div>

                  {/* ─── R2: Zone Transition Log ─────────────────────── */}
                  {(convoySimulation.zoneLog?.length ?? 0) > 0 && (
                    <div style={{ marginTop: '8px', padding: '6px 8px', borderRadius: '6px', backgroundColor: '#0f172a', border: '1px solid #1e293b' }}>
                      <div style={{ fontSize: '7px', color: '#64748b', letterSpacing: '0.05em', marginBottom: '4px' }}>ZONE TRANSITIONS</div>
                      <div style={{ maxHeight: '60px', overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: '2px' }}>
                        {convoySimulation.zoneLog.slice(-5).map((z, i) => (
                          <div key={i} style={{ display: 'flex', alignItems: 'center', gap: '4px', fontSize: '8px' }}>
                            <span style={{ color: '#64748b', fontFamily: 'var(--font-mono)', width: '28px' }}>{Math.floor((z.time || 0) / 60)}:{String(Math.floor((z.time || 0) % 60)).padStart(2, '0')}</span>
                            <span style={{ color: '#94a3b8', textTransform: 'capitalize' }}>{z.from}</span>
                            <span style={{ color: '#475569' }}>→</span>
                            <span style={{ color: '#a78bfa', fontWeight: 600, textTransform: 'capitalize' }}>{z.to}</span>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  {/* Distance progress bar */}
                  <div style={{ marginTop: '8px' }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '3px' }}>
                      <span style={{ fontSize: '7px', color: '#64748b', letterSpacing: '0.05em' }}>DISTANCE</span>
                      <span style={{ fontSize: '8px', fontFamily: 'var(--font-mono)', color: '#f97316', fontWeight: 700 }}>
                        {(convoySimulation.distanceTraveledM / 1000).toFixed(1)} / {(convoySimulation.totalDistanceM / 1000).toFixed(1)} km
                      </span>
                    </div>
                    <div style={{ height: '6px', background: '#1e293b', borderRadius: '3px', overflow: 'hidden', position: 'relative' }}>
                      <div style={{
                        height: '100%', borderRadius: '3px',
                        width: `${(convoySimulation.progress || 0) * 100}%`,
                        background: 'linear-gradient(90deg, #ea580c, #f97316, #fbbf24)',
                        transition: 'width 0.2s',
                        boxShadow: '0 0 8px rgba(249,115,22,0.4)',
                      }} />
                    </div>
                  </div>
                </div>
              )}

              {/* ─── Blue Book: Security Compliance Panel ──────────── */}
              {planResult && (
                <div style={{ 
                  padding: '10px 12px', borderRadius: '8px',
                  background: 'linear-gradient(135deg, rgba(15,23,42,0.95), rgba(30,41,59,0.7))',
                  border: `1px solid ${planResult.security_compliant ? 'rgba(22,163,74,0.4)' : 'rgba(220,38,38,0.4)'}`,
                  boxShadow: `0 0 8px ${planResult.security_compliant ? 'rgba(22,163,74,0.1)' : 'rgba(220,38,38,0.1)'}`,
                }}>
                  {/* Security Score Header */}
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '8px' }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                      <Lock size={11} color={planResult.security_compliant ? '#16a34a' : '#dc2626'} />
                      <span style={{ fontSize: '9px', fontWeight: 800, color: planResult.security_compliant ? '#16a34a' : '#dc2626', letterSpacing: '0.08em' }}>
                        SECURITY {planResult.security_compliant ? 'COMPLIANT' : 'NON-COMPLIANT'}
                      </span>
                    </div>
                    {planResult.security_score != null && (
                      <div style={{ 
                        padding: '2px 8px', borderRadius: '10px', fontSize: '10px', fontWeight: 800,
                        fontFamily: 'var(--font-mono)',
                        backgroundColor: planResult.security_score >= 0.8 ? 'rgba(22,163,74,0.15)' : planResult.security_score >= 0.5 ? 'rgba(234,88,12,0.15)' : 'rgba(220,38,38,0.15)',
                        color: planResult.security_score >= 0.8 ? '#4ade80' : planResult.security_score >= 0.5 ? '#fb923c' : '#fca5a5',
                        border: `1px solid ${planResult.security_score >= 0.8 ? 'rgba(22,163,74,0.3)' : planResult.security_score >= 0.5 ? 'rgba(234,88,12,0.3)' : 'rgba(220,38,38,0.3)'}`,
                      }}>
                        {(planResult.security_score * 100).toFixed(0)}%
                      </div>
                    )}
                  </div>

                  {/* Security Score Bar */}
                  {planResult.security_score != null && (
                    <div style={{ marginBottom: '8px' }}>
                      <div style={{ height: '4px', backgroundColor: '#334155', borderRadius: '4px', overflow: 'hidden' }}>
                        <div style={{
                          height: '100%', borderRadius: '4px',
                          width: `${Math.min(planResult.security_score * 100, 100)}%`,
                          background: planResult.security_score >= 0.8
                            ? 'linear-gradient(90deg, #16a34a, #4ade80)'
                            : planResult.security_score >= 0.5
                              ? 'linear-gradient(90deg, #ea580c, #fb923c)'
                              : 'linear-gradient(90deg, #dc2626, #fca5a5)',
                          transition: 'width 0.8s cubic-bezier(0.4, 0, 0.2, 1)',
                          boxShadow: `0 0 6px ${planResult.security_score >= 0.8 ? 'rgba(22,163,74,0.4)' : 'rgba(220,38,38,0.4)'}`,
                        }} />
                      </div>
                    </div>
                  )}

                  {/* Violations & Warnings Summary */}
                  <div style={{ display: 'flex', gap: '6px', marginBottom: '8px' }}>
                    <div style={{ 
                      flex: 1, padding: '5px 8px', borderRadius: '6px',
                      backgroundColor: (planResult.security_violations?.length || 0) > 0 ? 'rgba(220,38,38,0.1)' : 'rgba(22,163,74,0.06)',
                      border: `1px solid ${(planResult.security_violations?.length || 0) > 0 ? 'rgba(220,38,38,0.25)' : 'rgba(22,163,74,0.15)'}`,
                    }}>
                      <div style={{ fontSize: '7px', color: '#94a3b8', letterSpacing: '0.08em' }}>VIOLATIONS</div>
                      <div style={{ fontSize: '12px', fontWeight: 800, fontFamily: 'var(--font-mono)', color: (planResult.security_violations?.length || 0) > 0 ? '#fca5a5' : '#4ade80' }}>
                        {planResult.security_violations?.length || 0}
                      </div>
                    </div>
                    <div style={{ 
                      flex: 1, padding: '5px 8px', borderRadius: '6px',
                      backgroundColor: (planResult.security_warnings?.length || 0) > 0 ? 'rgba(234,179,8,0.08)' : 'rgba(22,163,74,0.06)',
                      border: `1px solid ${(planResult.security_warnings?.length || 0) > 0 ? 'rgba(234,179,8,0.2)' : 'rgba(22,163,74,0.15)'}`,
                    }}>
                      <div style={{ fontSize: '7px', color: '#94a3b8', letterSpacing: '0.08em' }}>WARNINGS</div>
                      <div style={{ fontSize: '12px', fontWeight: 800, fontFamily: 'var(--font-mono)', color: (planResult.security_warnings?.length || 0) > 0 ? '#fde68a' : '#4ade80' }}>
                        {planResult.security_warnings?.length || 0}
                      </div>
                    </div>
                    <div style={{ 
                      flex: 1, padding: '5px 8px', borderRadius: '6px',
                      backgroundColor: 'rgba(37,99,235,0.08)', border: '1px solid rgba(37,99,235,0.2)',
                    }}>
                      <div style={{ fontSize: '7px', color: '#94a3b8', letterSpacing: '0.08em' }}>ALT ROUTES</div>
                      <div style={{ fontSize: '12px', fontWeight: 800, fontFamily: 'var(--font-mono)', color: '#93c5fd' }}>
                        {planResult.alternate_routes?.length || 0}
                      </div>
                    </div>
                  </div>

                  {/* Violation Details */}
                  {planResult.security_violations?.length > 0 && (
                    <div style={{ marginBottom: '8px' }}>
                      {planResult.security_violations.slice(0, 3).map((v, i) => (
                        <div key={i} style={{ 
                          display: 'flex', alignItems: 'flex-start', gap: '6px', padding: '4px 6px', marginBottom: '3px',
                          borderRadius: '4px', backgroundColor: 'rgba(220,38,38,0.06)', fontSize: '8px', color: '#fca5a5',
                          cursor: v.segment_id ? 'pointer' : 'default',
                          transition: 'background-color 0.15s',
                        }}
                        onClick={() => v.segment_id && flyToSegment(v.segment_id)}
                        onMouseEnter={e => v.segment_id && (e.currentTarget.style.backgroundColor = 'rgba(220,38,38,0.15)')}
                        onMouseLeave={e => v.segment_id && (e.currentTarget.style.backgroundColor = 'rgba(220,38,38,0.06)')}
                        >
                          <AlertTriangle size={9} style={{ flexShrink: 0, marginTop: '1px' }} color="#dc2626" />
                          <div>
                            <span style={{ fontWeight: 700 }}>Seg {v.segment_id}</span>
                            {v.rule && <span style={{ color: '#94a3b8' }}> — {v.rule}</span>}
                            {v.severity && <span style={{ marginLeft: '4px', padding: '0 4px', borderRadius: '3px', fontSize: '7px', fontWeight: 700, backgroundColor: v.severity === 'critical' ? 'rgba(220,38,38,0.2)' : 'rgba(234,88,12,0.2)', color: v.severity === 'critical' ? '#fca5a5' : '#fb923c' }}>{v.severity}</span>}
                          </div>
                        </div>
                      ))}
                      {planResult.security_violations.length > 3 && (
                        <div style={{ fontSize: '8px', color: '#94a3b8', textAlign: 'center', marginTop: '2px' }}>
                          +{planResult.security_violations.length - 3} more violations
                        </div>
                      )}
                    </div>
                  )}

                  {/* Agency Deployment Status */}
                  {deployedAgencies.length > 0 && (
                    <div style={{ borderTop: '1px solid #334155', paddingTop: '8px' }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '4px', marginBottom: '6px' }}>
                        <Radio size={9} color="#ea580c" />
                        <span style={{ fontSize: '8px', fontWeight: 700, color: '#ea580c', letterSpacing: '0.08em' }}>AGENCIES DEPLOYED</span>
                      </div>
                      <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                        {deployedAgencies.map((ag, i) => (
                          <div key={i} style={{ 
                            display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                            padding: '5px 8px', borderRadius: '6px',
                            backgroundColor: `${ag.color}08`, border: `1px solid ${ag.color}25`,
                          }}>
                            <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                              <div style={{ 
                                width: '6px', height: '6px', borderRadius: '50%', 
                                backgroundColor: ag.color, boxShadow: `0 0 4px ${ag.color}60`,
                              }} />
                              <span style={{ fontSize: '9px', fontWeight: 700, color: '#e2e8f0' }}>{ag.short}</span>
                              <span style={{ fontSize: '8px', color: '#94a3b8' }}>{ag.name}</span>
                            </div>
                            <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                              <span style={{ fontSize: '8px', fontFamily: 'var(--font-mono)', color: ag.color, fontWeight: 600 }}>
                                {ag.segments} seg{ag.segments !== 1 ? 's' : ''}
                              </span>
                              {ag.actions.map(a => (
                                <span key={a} style={{ 
                                  fontSize: '7px', padding: '1px 4px', borderRadius: '3px',
                                  backgroundColor: `${ag.color}15`, color: ag.color, fontWeight: 600,
                                }}>{a}</span>
                              ))}
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              )}

              {/* ─── Corridor Intelligence Strip ─────────────────── */}
              {corridorSummary && (
                <div style={{ 
                  padding: '8px 10px', borderRadius: '8px',
                  backgroundColor: 'rgba(15,23,42,0.9)', border: '1px solid #33415580',
                }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '4px', marginBottom: '6px' }}>
                    <Activity size={9} color="#3b82f6" />
                    <span style={{ fontSize: '8px', fontWeight: 700, color: '#3b82f6', letterSpacing: '0.08em' }}>CORRIDOR INTEL</span>
                  </div>
                  <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '4px' }}>
                    <div style={{ textAlign: 'center' }}>
                      <div style={{ fontSize: '7px', color: '#64748b', letterSpacing: '0.05em' }}>AVG SPEED</div>
                      <div style={{ fontSize: '11px', fontWeight: 800, fontFamily: 'var(--font-mono)', color: '#e2e8f0' }}>
                        {corridorSummary.avg_speed_kmh?.toFixed(0) || '—'}<span style={{ fontSize: '7px', color: '#94a3b8' }}> km/h</span>
                      </div>
                    </div>
                    <div style={{ textAlign: 'center' }}>
                      <div style={{ fontSize: '7px', color: '#64748b', letterSpacing: '0.05em' }}>CONGESTION</div>
                      <div style={{ fontSize: '11px', fontWeight: 800, fontFamily: 'var(--font-mono)', color: (corridorSummary.avg_congestion_idx || 0) > 0.7 ? '#dc2626' : (corridorSummary.avg_congestion_idx || 0) > 0.4 ? '#ea580c' : '#16a34a' }}>
                        {corridorSummary.avg_congestion_idx != null ? (corridorSummary.avg_congestion_idx * 100).toFixed(0) : '—'}<span style={{ fontSize: '7px', color: '#94a3b8' }}>%</span>
                      </div>
                    </div>
                    <div style={{ textAlign: 'center' }}>
                      <div style={{ fontSize: '7px', color: '#64748b', letterSpacing: '0.05em' }}>CRITICAL</div>
                      <div style={{ fontSize: '11px', fontWeight: 800, fontFamily: 'var(--font-mono)', color: (corridorSummary.critical_segments || 0) > 0 ? '#dc2626' : '#4ade80' }}>
                        {corridorSummary.critical_segments || 0}<span style={{ fontSize: '7px', color: '#94a3b8' }}>/{corridorSummary.total_segments || 0}</span>
                      </div>
                    </div>
                  </div>
                </div>
              )}

              {lifecycleError && (
                <div style={{ padding: '8px', borderRadius: '6px', backgroundColor: 'rgba(220,38,38,0.1)', border: '1px solid rgba(220,38,38,0.3)', fontSize: '10px', color: '#fca5a5' }}>
                  {lifecycleError}
                </div>
              )}

              {/* ─── ASL Pre-Deployment Checklist (Blue Book §3.2) ─── */}
              {(lifecycle === 'idle' || lifecycle === 'approved') && (
                <div style={{ 
                  borderRadius: '10px', overflow: 'hidden',
                  border: aslReadiness.ready ? '1px solid rgba(34,197,94,0.4)' : '1px solid rgba(234,179,8,0.3)',
                  backgroundColor: aslReadiness.ready ? 'rgba(22,163,74,0.06)' : 'rgba(234,179,8,0.04)',
                }}>
                  {/* Header */}
                  <button
                    onClick={() => setAslExpanded(!aslExpanded)}
                    style={{ 
                      width: '100%', display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                      padding: '8px 10px', border: 'none', cursor: 'pointer',
                      backgroundColor: 'rgba(15,23,42,0.7)', color: '#e2e8f0',
                    }}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                      <Shield size={10} color={aslReadiness.ready ? '#22c55e' : '#eab308'} />
                      <span style={{ fontSize: '8px', fontWeight: 700, letterSpacing: '0.08em', color: aslReadiness.ready ? '#4ade80' : '#fbbf24' }}>
                        ASL PRE-DEPLOYMENT
                      </span>
                      <span style={{ 
                        fontSize: '7px', fontWeight: 800, fontFamily: 'var(--font-mono)',
                        padding: '1px 5px', borderRadius: '4px',
                        backgroundColor: aslReadiness.ready ? 'rgba(34,197,94,0.2)' : 'rgba(234,179,8,0.15)',
                        color: aslReadiness.ready ? '#4ade80' : '#fbbf24',
                      }}>
                        {aslReadiness.checked}/{aslReadiness.total} · {aslReadiness.pct}%
                      </span>
                    </div>
                    {aslExpanded ? <ChevronUp size={10} color="#64748b" /> : <ChevronDown size={10} color="#64748b" />}
                  </button>

                  {/* Progress bar */}
                  <div style={{ height: '2px', backgroundColor: 'rgba(100,116,139,0.2)' }}>
                    <div style={{ 
                      height: '100%', width: `${aslReadiness.pct}%`,
                      background: aslReadiness.ready ? '#22c55e' : 'linear-gradient(90deg, #eab308, #f59e0b)',
                      transition: 'width 0.4s ease',
                    }} />
                  </div>

                  {aslExpanded && (
                    <div style={{ padding: '6px 8px 8px', display: 'flex', flexDirection: 'column', gap: '2px' }}>
                      {[
                        { key: 'asl_meeting', label: 'ASL Meeting Convened', critical: true, desc: 'SSP + DM + IB + SPG' },
                        { key: 'route_finalised', label: 'Route Finalised & Locked', critical: true, desc: 'Primary + Plan B' },
                        { key: 'route_survey', label: 'Route Physical Survey', critical: true, desc: 'Bridge/culvert/underpass' },
                        { key: 'vuln_points', label: 'Vulnerability Points Mapped', critical: false, desc: 'Chokepoints identified' },
                        { key: 'contingency_route', label: 'Plan B Route Identified', critical: false, desc: 'Alternate + hospitals' },
                        { key: 'threat_briefing', label: 'Threat Intel Brief Done', critical: false, desc: 'IB + State Intel input' },
                        { key: 'comms_protocol', label: 'Comms Protocol Confirmed', critical: false, desc: 'Frequencies locked' },
                        { key: 'antisab_sweep', label: 'Anti-Sabotage Sweep', critical: true, desc: 'Physical + tech + K9' },
                        { key: 'vehicles_cleared', label: 'Vehicles Sanitised', critical: false, desc: 'Same make/colour/VR' },
                        { key: 'vehicle_checks', label: 'Vehicle Mechanical Check', critical: true, desc: 'Fuel/tyre/brakes/comms' },
                        { key: 'driver_vetting', label: 'Driver Vetting Complete', critical: true, desc: 'Background verified' },
                        { key: 'flag_mounted', label: 'National Flag Mounted', critical: false, desc: 'Only on VVIP car' },
                      ].map(item => (
                        <button
                          key={item.key}
                          onClick={() => toggleAslItem(item.key)}
                          style={{ 
                            width: '100%', display: 'flex', alignItems: 'center', gap: '6px',
                            padding: '4px 6px', border: 'none', borderRadius: '4px', cursor: 'pointer',
                            backgroundColor: aslChecklist[item.key] ? 'rgba(34,197,94,0.08)' : 'transparent',
                            transition: 'background-color 0.2s',
                          }}
                        >
                          {aslChecklist[item.key] 
                            ? <CheckSquare size={11} color="#22c55e" /> 
                            : <Square size={11} color={item.critical ? '#eab308' : '#475569'} />
                          }
                          <div style={{ flex: 1, textAlign: 'left' }}>
                            <span style={{ 
                              fontSize: '8px', fontWeight: 600,
                              color: aslChecklist[item.key] ? '#86efac' : '#cbd5e1',
                              textDecoration: aslChecklist[item.key] ? 'line-through' : 'none',
                              opacity: aslChecklist[item.key] ? 0.7 : 1,
                            }}>
                              {item.label}
                              {item.critical && !aslChecklist[item.key] && (
                                <span style={{ fontSize: '6px', color: '#ef4444', marginLeft: '3px', fontWeight: 800 }}>★ CRITICAL</span>
                              )}
                            </span>
                            <div style={{ fontSize: '6px', color: '#64748b', marginTop: '-1px' }}>{item.desc}</div>
                          </div>
                        </button>
                      ))}

                      {/* Simulate ASL button */}
                      {!aslReadiness.ready && (
                        <button
                          onClick={simulateAslCompletion}
                          style={{ 
                            marginTop: '4px', width: '100%', padding: '6px 8px',
                            background: 'linear-gradient(135deg, rgba(99,102,241,0.15), rgba(139,92,246,0.1))',
                            border: '1px solid rgba(139,92,246,0.3)', borderRadius: '6px',
                            cursor: 'pointer', color: '#a78bfa', fontSize: '8px', fontWeight: 700,
                            letterSpacing: '0.06em',
                          }}
                        >
                          ⚡ SIMULATE ASL COMPLETION (DEMO)
                        </button>
                      )}

                      {/* Critical readiness gate */}
                      <div style={{ 
                        marginTop: '4px', padding: '4px 8px', borderRadius: '5px', fontSize: '7px', fontWeight: 700,
                        textAlign: 'center', letterSpacing: '0.06em',
                        backgroundColor: aslCriticalReady ? 'rgba(34,197,94,0.12)' : 'rgba(220,38,38,0.08)',
                        color: aslCriticalReady ? '#4ade80' : '#fca5a5',
                        border: aslCriticalReady ? '1px solid rgba(34,197,94,0.25)' : '1px solid rgba(220,38,38,0.2)',
                      }}>
                        {aslCriticalReady ? '✓ ALL CRITICAL CHECKS PASSED — DEPLOY READY' : '✗ CRITICAL CHECKS INCOMPLETE — DEPLOY BLOCKED'}
                      </div>
                    </div>
                  )}
                </div>
              )}

              {/* ─── Protocol Compliance + Anti-Sab + Transit Strip ─── */}
              {(lifecycle === 'idle' || lifecycle === 'approved' || lifecycle === 'active') && (
                <div style={{ 
                  display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '4px',
                }}>
                  {/* Protocol Score */}
                  <div style={{ 
                    padding: '6px', borderRadius: '6px', textAlign: 'center',
                    backgroundColor: 'rgba(15,23,42,0.8)', border: '1px solid #33415550',
                  }}>
                    <div style={{ fontSize: '6px', color: '#64748b', letterSpacing: '0.06em', marginBottom: '2px' }}>PROTOCOL</div>
                    <div style={{ 
                      fontSize: '12px', fontWeight: 800, fontFamily: 'var(--font-mono)',
                      color: protocolScore.pct >= 80 ? '#4ade80' : protocolScore.pct >= 50 ? '#fbbf24' : '#f87171',
                    }}>
                      {protocolScore.pct}%
                    </div>
                    <div style={{ fontSize: '6px', color: '#475569' }}>{protocolScore.checked}/{protocolScore.total} rules</div>
                  </div>

                  {/* Anti-Sabotage */}
                  <div style={{ 
                    padding: '6px', borderRadius: '6px', textAlign: 'center',
                    backgroundColor: 'rgba(15,23,42,0.8)', border: '1px solid #33415550',
                  }}>
                    <div style={{ fontSize: '6px', color: '#64748b', letterSpacing: '0.06em', marginBottom: '2px' }}>ANTI-SAB</div>
                    <div style={{ display: 'flex', justifyContent: 'center', gap: '3px', marginBottom: '2px' }}>
                      {[
                        { key: 'physical_search', label: 'P' },
                        { key: 'technical_gadgets', label: 'T' },
                        { key: 'sniffer_dogs', label: 'K9' },
                      ].map(m => (
                        <span key={m.key} style={{ 
                          fontSize: '7px', fontWeight: 800, width: '14px', height: '14px',
                          display: 'flex', alignItems: 'center', justifyContent: 'center',
                          borderRadius: '3px', fontFamily: 'var(--font-mono)',
                          backgroundColor: antiSabotage[m.key] ? 'rgba(34,197,94,0.2)' : 'rgba(100,116,139,0.15)',
                          color: antiSabotage[m.key] ? '#4ade80' : '#475569',
                        }}>{m.label}</span>
                      ))}
                    </div>
                    <div style={{ fontSize: '6px', color: '#475569' }}>
                      {Object.values(antiSabotage).filter(Boolean).length}/3
                    </div>
                  </div>

                  {/* Transit Status */}
                  <div style={{ 
                    padding: '6px', borderRadius: '6px', textAlign: 'center',
                    backgroundColor: 'rgba(15,23,42,0.8)', border: '1px solid #33415550',
                  }}>
                    <div style={{ fontSize: '6px', color: '#64748b', letterSpacing: '0.06em', marginBottom: '2px' }}>TRANSIT</div>
                    <div style={{ display: 'flex', justifyContent: 'center', gap: '2px', marginBottom: '2px' }}>
                      {[
                        { key: 'ecm_active', label: 'ECM', col: '#818cf8' },
                        { key: 'spg_clearance', label: 'SPG', col: '#f472b6' },
                        { key: 'route_sanitised', label: 'SAN', col: '#34d399' },
                        { key: 'formation_intact', label: 'FRM', col: '#fbbf24' },
                      ].map(t => (
                        <span key={t.key} style={{ 
                          fontSize: '5.5px', fontWeight: 800, padding: '1px 3px',
                          borderRadius: '2px', fontFamily: 'var(--font-mono)',
                          backgroundColor: transitStatus[t.key] ? `${t.col}20` : 'rgba(100,116,139,0.1)',
                          color: transitStatus[t.key] ? t.col : '#475569',
                        }}>{t.label}</span>
                      ))}
                    </div>
                    <div style={{ fontSize: '6px', color: '#475569' }}>
                      {Object.values(transitStatus).filter(Boolean).length}/4
                    </div>
                  </div>
                </div>
              )}

              {/* Deploy / Re-Plan */}
              {(lifecycle === 'idle' || lifecycle === 'approved') && (
                <button 
                  onClick={handleDeploy}
                  disabled={deploying || !aslCriticalReady}
                  title={!aslCriticalReady ? 'Complete all critical ASL checks before deploying' : ''}
                  style={{ 
                    width: '100%', padding: '13px 12px', 
                    background: deploying || !aslCriticalReady ? '#475569' : 'linear-gradient(135deg, #ea580c, #f97316, #fb923c)', 
                    color: 'white', border: 'none', borderRadius: '10px', fontWeight: 700, fontSize: '12px',
                    cursor: deploying || !aslCriticalReady ? 'not-allowed' : 'pointer', letterSpacing: '0.05em',
                    boxShadow: deploying || !aslCriticalReady ? 'none' : '0 4px 16px rgba(234, 88, 12, 0.3), 0 0 0 1px rgba(234,88,12,0.1)',
                    transition: 'all 0.3s cubic-bezier(0.4, 0, 0.2, 1)',
                    animation: deploying || !aslCriticalReady ? 'none' : 'breathe 3s ease-in-out infinite',
                  }}
                >
                  {deploying ? '⟳ Planning via LangGraph...' : !aslCriticalReady ? '🔒 ASL Checks Required' : lifecycle === 'approved' ? '↻ Re-Plan Operation' : '▶ Deploy Operation'}
                </button>
              )}

              {/* Start Escort — only when plan is approved */}
              {lifecycle === 'approved' && (
                <>
                  {/* AI Protocol Validation Bar */}
                  <div style={{ display: 'flex', gap: '4px', marginTop: '6px' }}>
                    <button
                      onClick={runProtocolAssessment}
                      disabled={assessingProtocol}
                      style={{
                        flex: 1, padding: '8px 6px', fontSize: '9px', fontWeight: 700,
                        background: assessingProtocol ? '#334155' : 'linear-gradient(135deg, #eab308, #ca8a04)',
                        color: 'white', border: 'none', borderRadius: '8px',
                        cursor: assessingProtocol ? 'wait' : 'pointer', letterSpacing: '0.04em',
                      }}
                    >
                      {assessingProtocol ? '⟳ Assessing…' : '🛡 AI Protocol Check'}
                    </button>
                    <button
                      onClick={runThreatAssessment}
                      disabled={assessingThreat}
                      style={{
                        flex: 1, padding: '8px 6px', fontSize: '9px', fontWeight: 700,
                        background: assessingThreat ? '#334155' : 'linear-gradient(135deg, #f97316, #ea580c)',
                        color: 'white', border: 'none', borderRadius: '8px',
                        cursor: assessingThreat ? 'wait' : 'pointer', letterSpacing: '0.04em',
                      }}
                    >
                      {assessingThreat ? '⟳ Scanning…' : '🎯 Threat Scan'}
                    </button>
                  </div>

                  {/* Protocol Assessment Result */}
                  {protocolAssessment && (
                    <div style={{
                      marginTop: '4px', padding: '8px 10px', borderRadius: '8px',
                      background: 'rgba(234,179,8,0.06)', border: '1px solid rgba(234,179,8,0.2)',
                    }}>
                      <div style={{ fontSize: '8px', fontWeight: 800, color: '#eab308', letterSpacing: '0.08em', marginBottom: '4px' }}>
                        QWEN PROTOCOL ASSESSMENT
                      </div>
                      <div style={{ fontSize: '9px', color: '#cbd5e1', lineHeight: 1.4, maxHeight: '80px', overflowY: 'auto' }}>
                        {typeof protocolAssessment === 'string'
                          ? protocolAssessment.slice(0, 500)
                          : protocolAssessment?.response?.slice(0, 500) || JSON.stringify(protocolAssessment).slice(0, 500)}
                      </div>
                    </div>
                  )}

                  {/* Threat Brief Result */}
                  {threatBrief && (
                    <div style={{
                      marginTop: '4px', padding: '8px 10px', borderRadius: '8px',
                      background: threatBrief.threat_level === 'critical' ? 'rgba(220,38,38,0.08)' : 'rgba(249,115,22,0.06)',
                      border: `1px solid ${threatBrief.threat_level === 'critical' ? 'rgba(220,38,38,0.3)' : 'rgba(249,115,22,0.2)'}`,
                    }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '4px' }}>
                        <span style={{ fontSize: '8px', fontWeight: 800, color: threatBrief.threat_level === 'critical' ? '#dc2626' : '#f97316', letterSpacing: '0.08em' }}>
                          THREAT: {(threatBrief.threat_level || 'NOMINAL').toUpperCase()}
                        </span>
                      </div>
                      <div style={{ fontSize: '9px', color: '#cbd5e1', lineHeight: 1.4, maxHeight: '60px', overflowY: 'auto' }}>
                        {typeof threatBrief === 'string'
                          ? threatBrief.slice(0, 400)
                          : threatBrief?.assessment?.slice(0, 400) || threatBrief?.response?.slice(0, 400) || ''}
                      </div>
                    </div>
                  )}

                  <button 
                    onClick={handleStartEscort}
                    disabled={escortLoading}
                  style={{ 
                    width: '100%', padding: '13px 12px', marginTop: '6px',
                    background: escortLoading ? '#94a3b8' : 'linear-gradient(135deg, #16a34a, #22c55e, #4ade80)', 
                    color: 'white', border: 'none', borderRadius: '10px', fontWeight: 700, fontSize: '12px',
                    cursor: escortLoading ? 'not-allowed' : 'pointer', letterSpacing: '0.05em',
                    boxShadow: escortLoading ? 'none' : '0 4px 16px rgba(22, 163, 74, 0.3), 0 0 0 1px rgba(22,163,74,0.1)',
                    transition: 'all 0.3s cubic-bezier(0.4, 0, 0.2, 1)',
                  }}
                >
                  {escortLoading ? '⟳ Escort Running…' : '▶ Start Escort'}
                </button>
                </>
              )}

              {/* Active escort indicator + Clear */}
              {lifecycle === 'active' && (
                <>
                  <div style={{ width: '100%', padding: '12px 14px', borderRadius: '10px', background: 'linear-gradient(135deg, rgba(22,163,74,0.12), rgba(22,163,74,0.06))', border: '1px solid rgba(134,239,172,0.3)', fontSize: '11px', color: '#4ade80', fontWeight: 600, display: 'flex', alignItems: 'center', gap: '8px', boxShadow: '0 2px 8px rgba(22,163,74,0.15)' }}>
                    <span style={{ display: 'inline-block', width: '10px', height: '10px', borderRadius: '50%', background: '#22c55e', animation: 'pulse 2s infinite', boxShadow: '0 0 6px rgba(34,197,94,0.5)' }} />
                    {escortResult ? 'Escort Complete' : 'Live Escort Active'}
                    {escortResult && <span style={{ fontSize: '9px', fontWeight: 400, color: '#94a3b8', marginLeft: 'auto' }}>{escortResult.total_iterations} iters</span>}
                  </div>
                  {escortResult && (
                    <button 
                      onClick={handleClearRoute}
                      disabled={clearLoading}
                      style={{ 
                        width: '100%', padding: '13px 12px', marginTop: '6px',
                        background: clearLoading ? '#94a3b8' : 'linear-gradient(135deg, #2563eb, #3b82f6, #60a5fa)', 
                        color: 'white', border: 'none', borderRadius: '10px', fontWeight: 700, fontSize: '12px',
                        cursor: clearLoading ? 'not-allowed' : 'pointer', letterSpacing: '0.05em',
                        boxShadow: clearLoading ? 'none' : '0 4px 16px rgba(37, 99, 235, 0.3), 0 0 0 1px rgba(37,99,235,0.1)',
                        transition: 'all 0.3s cubic-bezier(0.4, 0, 0.2, 1)',
                      }}
                    >
                      {clearLoading ? '⟳ Clearing Diversions…' : '↺ Clear Route & Recover'}
                    </button>
                  )}
                </>
              )}

              {/* Completed — show report + reset */}
              {lifecycle === 'completed' && (
                <>
                  {clearResult && (
                    <div style={{ padding: '10px 12px', borderRadius: '8px', background: 'linear-gradient(135deg, rgba(37,99,235,0.1), rgba(37,99,235,0.05))', border: '1px solid rgba(59,130,246,0.3)', fontSize: '10px', color: '#93c5fd' }}>
                      <div style={{ fontWeight: 700, marginBottom: '6px', fontSize: '11px', display: 'flex', alignItems: 'center', gap: '4px', color: '#e2e8f0' }}>
                        <Shield size={11} color="#3b82f6" /> Post-Clearance Report — Blue Book §7
                      </div>
                      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '4px', marginBottom: '8px' }}>
                        <div>Recovered: <strong style={{ fontFamily: 'var(--font-mono)' }}>{clearResult.segments_recovered}/{clearResult.total_affected_segments}</strong></div>
                        <div>Time: <strong style={{ fontFamily: 'var(--font-mono)' }}>{(clearResult.recovery_time_sec || 0).toFixed(1)}s</strong></div>
                        <div>Diversions off: <strong style={{ fontFamily: 'var(--font-mono)' }}>{clearResult.diversions_deactivated}</strong></div>
                        <div>Alerts: <strong style={{ fontFamily: 'var(--font-mono)', color: (clearResult.alerts_during_escort || 0) > 0 ? '#fca5a5' : '#4ade80' }}>{clearResult.alerts_during_escort || 0}</strong></div>
                      </div>
                      {/* Extended Blue Book Operational Metrics */}
                      <div style={{ borderTop: '1px solid rgba(59,130,246,0.2)', paddingTop: '6px', display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '4px' }}>
                        <div style={{ textAlign: 'center' }}>
                          <div style={{ fontSize: '7px', color: '#64748b', letterSpacing: '0.05em' }}>DECISIONS</div>
                          <div style={{ fontSize: '12px', fontWeight: 800, fontFamily: 'var(--font-mono)', color: '#e2e8f0' }}>{clearResult.total_decisions || 0}</div>
                        </div>
                        <div style={{ textAlign: 'center' }}>
                          <div style={{ fontSize: '7px', color: '#64748b', letterSpacing: '0.05em' }}>ESCORT</div>
                          <div style={{ fontSize: '12px', fontWeight: 800, fontFamily: 'var(--font-mono)', color: '#e2e8f0' }}>{clearResult.escort_duration_sec ? `${(clearResult.escort_duration_sec / 60).toFixed(1)}m` : '—'}</div>
                        </div>
                        <div style={{ textAlign: 'center' }}>
                          <div style={{ fontSize: '7px', color: '#64748b', letterSpacing: '0.05em' }}>TOTAL</div>
                          <div style={{ fontSize: '12px', fontWeight: 800, fontFamily: 'var(--font-mono)', color: '#e2e8f0' }}>{clearResult.planning_to_completion_sec ? `${(clearResult.planning_to_completion_sec / 60).toFixed(1)}m` : '—'}</div>
                        </div>
                      </div>
                      {/* Recovery Efficiency Bar */}
                      {clearResult.total_affected_segments > 0 && (
                        <div style={{ marginTop: '6px' }}>
                          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '2px' }}>
                            <span style={{ fontSize: '7px', color: '#64748b', letterSpacing: '0.05em' }}>RECOVERY RATE</span>
                            <span style={{ fontSize: '9px', fontWeight: 700, fontFamily: 'var(--font-mono)', color: '#4ade80' }}>
                              {((clearResult.segments_recovered / clearResult.total_affected_segments) * 100).toFixed(0)}%
                            </span>
                          </div>
                          <div style={{ height: '3px', backgroundColor: '#334155', borderRadius: '3px', overflow: 'hidden' }}>
                            <div style={{
                              height: '100%', borderRadius: '3px',
                              width: `${(clearResult.segments_recovered / clearResult.total_affected_segments) * 100}%`,
                              background: 'linear-gradient(90deg, #3b82f6, #60a5fa)',
                              transition: 'width 0.8s ease',
                            }} />
                          </div>
                        </div>
                      )}
                    </div>
                  )}
                  <button 
                    onClick={handleReset}
                    style={{ 
                      width: '100%', padding: '13px 12px', marginTop: '6px',
                      background: 'linear-gradient(135deg, #7c3aed, #8b5cf6, #a78bfa)', 
                      color: 'white', border: 'none', borderRadius: '10px', fontWeight: 700, fontSize: '12px',
                      cursor: 'pointer', letterSpacing: '0.05em',
                      boxShadow: '0 4px 16px rgba(124, 58, 237, 0.3), 0 0 0 1px rgba(124,58,237,0.1)',
                      transition: 'all 0.3s cubic-bezier(0.4, 0, 0.2, 1)',
                    }}
                  >
                    ⭐ New Operation
                  </button>
                </>
              )}
            </div>
          </div>
        </div>

        {/* Vehicles Section */}
        <div style={{ marginBottom: '16px' }}>
          <SHdr title="Convoy Fleet" badge={`${selectedVehicles.length}/${vehicles.length} Active`} />
          <div style={{ padding: '0 14px 14px 14px' }}>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
              {vehicles.map(v => {
                const isSelected = selectedVehicles.includes(v.id);
                const fuelColor = v.fuel > 70 ? '#16a34a' : v.fuel > 40 ? '#ea580c' : '#dc2626';
                return (
                  <div 
                    key={v.id}
                    onClick={() => {
                      setSelectedVehicles(s => s.includes(v.id) ? s.filter(x => x !== v.id) : [...s, v.id]);
                      if (planResult?.primary_route?.segment_ids?.length) {
                        flyToSegment(planResult.primary_route.segment_ids[0]);
                      }
                    }}
                    style={{
                      padding: '10px 12px', borderRadius: '10px',
                      border: `1px solid ${isSelected ? '#fb923c' : '#334155'}`,
                      backgroundColor: isSelected ? 'rgba(234,88,12,0.08)' : '#1e293b',
                      cursor: 'pointer', 
                      transition: 'all 0.25s cubic-bezier(0.4, 0, 0.2, 1)',
                      boxShadow: isSelected ? '0 2px 12px rgba(234,88,12,0.2)' : '0 1px 3px rgba(0,0,0,0.15)',
                      transform: isSelected ? 'scale(1.01)' : 'scale(1)',
                    }}
                  >
                    <div className="flex justify-between items-start">
                      <div className="flex items-center gap-2">
                        <span style={{ fontSize: '16px' }}>{v.icon}</span>
                        <div>
                          <div style={{ fontSize: '11px', fontWeight: 700, color: '#e2e8f0' }}>{v.name}</div>
                          <div style={{ fontSize: '9px', color: '#94a3b8', fontFamily: 'var(--font-mono)', letterSpacing: '0.5px' }}>{v.regId}</div>
                        </div>
                      </div>
                      <span className={`badge ${v.status === 'ready' ? 'badge-green' : v.status === 'standby' ? 'badge-orange' : 'badge-red'}`} style={{ border: 'none', minWidth: '60px', fontSize: '8px' }}>
                        {v.status}
                      </span>
                    </div>
                    {/* Fuel gauge bar */}
                    <div style={{ marginTop: '8px' }}>
                      <div className="flex justify-between items-center" style={{ marginBottom: '3px' }}>
                        <span style={{ fontSize: '9px', color: '#94a3b8', fontWeight: 600 }}>FUEL</span>
                        <span style={{ fontSize: '9px', fontWeight: 700, color: fuelColor, fontFamily: 'var(--font-mono)' }}>{v.fuel}%</span>
                      </div>
                      <div style={{ height: '4px', backgroundColor: '#334155', borderRadius: '4px', overflow: 'hidden' }}>
                        <div style={{ 
                          height: '100%', width: `${v.fuel}%`, borderRadius: '4px',
                          background: `linear-gradient(90deg, ${fuelColor}, ${fuelColor}cc)`,
                          transition: 'width 0.5s cubic-bezier(0.4, 0, 0.2, 1)',
                        }} />
                      </div>
                    </div>
                    <div className="flex items-center justify-between" style={{ marginTop: '6px' }}>
                      <div className="flex items-center gap-1">
                        <ClockIcon size={9} color="#94a3b8" />
                        <span style={{ fontSize: '9px', fontWeight: 600, color: '#94a3b8' }}>Range: {v.range}</span>
                      </div>
                      {isSelected && (
                        <CheckCircle size={12} color="#ea580c" />
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        </div>

        {/* ─── Blue Book: Situational Awareness ──────────────────── */}
        <div style={{ marginBottom: '16px' }}>
          <SHdr title="Situational Awareness" badge={`${(anomalies?.length || 0)} alerts`} />
          <div style={{ padding: '0 14px 14px 14px', display: 'flex', flexDirection: 'column', gap: '8px' }}>
            
            {/* Active Movements */}
            {activeMovements && activeMovements.length > 0 && (
              <div style={{ 
                padding: '8px 10px', borderRadius: '8px',
                backgroundColor: 'rgba(22,163,74,0.06)', border: '1px solid rgba(22,163,74,0.2)',
              }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '4px', marginBottom: '6px' }}>
                  <Eye size={9} color="#16a34a" />
                  <span style={{ fontSize: '8px', fontWeight: 700, color: '#16a34a', letterSpacing: '0.08em' }}>ACTIVE MOVEMENTS</span>
                  <span style={{ marginLeft: 'auto', fontSize: '9px', fontWeight: 700, fontFamily: 'var(--font-mono)', color: '#4ade80' }}>{activeMovements.length}</span>
                </div>
                {activeMovements.slice(0, 3).map((mov, i) => (
                  <div key={mov.movement_id || i} style={{ 
                    display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                    padding: '4px 6px', marginBottom: '3px', borderRadius: '4px',
                    backgroundColor: 'rgba(22,163,74,0.08)',
                    cursor: 'pointer', transition: 'background-color 0.15s',
                  }}
                  onClick={() => mov.segment_ids?.length && flyToSegment(mov.segment_ids[0])}
                  onMouseEnter={e => e.currentTarget.style.backgroundColor = 'rgba(22,163,74,0.18)'}
                  onMouseLeave={e => e.currentTarget.style.backgroundColor = 'rgba(22,163,74,0.08)'}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
                      <Siren size={8} color="#4ade80" />
                      <span style={{ fontSize: '8px', fontFamily: 'var(--font-mono)', color: '#e2e8f0' }}>
                        {(mov.movement_id || '').slice(0, 8)}…
                      </span>
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
                      {mov.vvip_class && (
                        <span style={{ fontSize: '7px', padding: '1px 4px', borderRadius: '3px', fontWeight: 700, backgroundColor: 'rgba(234,88,12,0.15)', color: '#fb923c' }}>{mov.vvip_class}</span>
                      )}
                      <span style={{ fontSize: '7px', padding: '1px 4px', borderRadius: '3px', fontWeight: 600, backgroundColor: 'rgba(22,163,74,0.15)', color: '#4ade80' }}>{mov.status || 'active'}</span>
                    </div>
                  </div>
                ))}
              </div>
            )}

            {/* Recent Anomalies */}
            {anomalies && anomalies.length > 0 ? (
              <div style={{ 
                padding: '8px 10px', borderRadius: '8px',
                backgroundColor: 'rgba(220,38,38,0.04)', border: '1px solid rgba(220,38,38,0.15)',
              }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '4px', marginBottom: '6px' }}>
                  <Target size={9} color="#dc2626" />
                  <span style={{ fontSize: '8px', fontWeight: 700, color: '#dc2626', letterSpacing: '0.08em' }}>ANOMALY FEED</span>
                  <span style={{ marginLeft: 'auto', fontSize: '9px', fontWeight: 700, fontFamily: 'var(--font-mono)', color: '#fca5a5' }}>{anomalies.length}</span>
                </div>
                {anomalies.slice(0, 4).map((a, i) => {
                  const sevColor = a.severity === 'high' ? '#dc2626' : a.severity === 'medium' ? '#ea580c' : '#eab308';
                  return (
                    <div key={a.anomaly_id || i} style={{ 
                      display: 'flex', alignItems: 'center', gap: '6px',
                      padding: '4px 6px', marginBottom: '3px', borderRadius: '4px',
                      backgroundColor: `${sevColor}08`, cursor: 'pointer',
                    }}
                      onClick={() => a.segment_id && flyToSegment?.(a.segment_id)}
                    >
                      <div style={{ 
                        width: '5px', height: '5px', borderRadius: '50%', 
                        backgroundColor: sevColor, boxShadow: `0 0 4px ${sevColor}60`,
                        flexShrink: 0,
                      }} />
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ fontSize: '8px', fontWeight: 600, color: '#e2e8f0', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                          {(a.anomaly_type || 'unknown').replace(/_/g, ' ')}
                        </div>
                        <div style={{ fontSize: '7px', color: '#94a3b8', fontFamily: 'var(--font-mono)' }}>
                          Seg {a.segment_id || '?'}
                        </div>
                      </div>
                      <span style={{ 
                        fontSize: '6px', padding: '1px 4px', borderRadius: '3px', fontWeight: 700,
                        backgroundColor: `${sevColor}18`, color: sevColor, flexShrink: 0,
                      }}>{a.severity?.toUpperCase()}</span>
                    </div>
                  );
                })}
                {anomalies.length > 4 && (
                  <div style={{ fontSize: '8px', color: '#94a3b8', textAlign: 'center', marginTop: '2px' }}>
                    +{anomalies.length - 4} more anomalies
                  </div>
                )}
              </div>
            ) : (
              <div style={{ 
                padding: '10px', borderRadius: '8px', textAlign: 'center',
                backgroundColor: 'rgba(22,163,74,0.04)', border: '1px solid rgba(22,163,74,0.1)',
              }}>
                <Crosshair size={14} color="#16a34a" style={{ margin: '0 auto 4px' }} />
                <div style={{ fontSize: '9px', fontWeight: 600, color: '#16a34a' }}>All Clear</div>
                <div style={{ fontSize: '8px', color: '#94a3b8' }}>No anomalies detected</div>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Toggle Button */}
      <button
        onClick={onToggle}
        style={{
          position: 'absolute', top: '50%', transform: 'translateY(-50%)',
          left: open ? `${LWIDTH}px` : '0', zIndex: 1001,
          width: '26px', height: '52px', 
          backgroundColor: 'rgba(15,23,42,0.92)', backdropFilter: 'blur(8px)',
          border: '1px solid rgba(51,65,85,0.6)', borderLeft: 'none',
          borderRadius: '0 10px 10px 0', cursor: 'pointer',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          transition: 'all 0.3s cubic-bezier(0.4, 0, 0.2, 1)',
          color: '#ea580c', boxShadow: '3px 0 12px rgba(0,0,0,0.04)',
        }}
      >
        {open ? <ChevronLeft size={16} /> : <ChevronRight size={16} />}
      </button>
    </>
  );
};

export default LeftPanel;
