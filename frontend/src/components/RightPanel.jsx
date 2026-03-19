import React, { useState, useEffect, useMemo, useCallback } from 'react';
import { Activity, Shield, Users, AlertTriangle, ChevronRight, ChevronLeft, Gauge, Zap, Radio, Eye, Clock as ClockIcon, TrendingDown, TrendingUp, BarChart3, Layers, Radar as RadarIcon, Target, Lightbulb, Bell, ArrowUpRight, ArrowDownRight, Minus } from 'lucide-react';
import { AreaChart, Area, LineChart, Line, BarChart, Bar, Cell, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, ReferenceLine, PieChart, Pie, ScatterChart, Scatter, RadialBarChart, RadialBar, Legend, ComposedChart } from 'recharts';
import SHdr from './SHdr';
import { IconRouteArrow, IconArrowRight } from './CustomIcons';
import { useConvoy } from '../context/ConvoyContext';
import * as api from '../services/api';

const SEVERITY_COLOR = { high: '#dc2626', medium: '#ea580c', low: '#eab308' };
const RP_SEC_SPECS = {
  'Z+': { minLanes: 6, closure: 'Full', advance: 180, maxQueue: 2000 },
  'Z':  { minLanes: 4, closure: 'Partial', advance: 120, maxQueue: 1000 },
  'Y':  { minLanes: 2, closure: 'Spd Restrict', advance: 60, maxQueue: 500 },
  'X':  { minLanes: 0, closure: 'Signal Only', advance: 0, maxQueue: 0 },
};

/* ── Tab definitions ── */
const RP_TABS = [
  { key: 'live', label: 'LIVE', icon: <Target size={11} /> },
  { key: 'predict', label: 'PREDICT', icon: <Lightbulb size={11} /> },
  { key: 'analytics', label: 'ANALYTICS', icon: <BarChart3 size={11} /> },
  { key: 'corridor', label: 'CORRIDOR', icon: <Layers size={11} /> },
  { key: 'intel', label: 'INTEL', icon: <RadarIcon size={11} /> },
];

const RightPanel = ({ open, onToggle, alerts, summary, movements, vvipClass }) => {
  const RWIDTH = 340;
  const { aiReasoning, lifecycle, planResult, flyToSegment, selectSegment, selectedSegmentId, flyToLocation, convoySimulation, gpuHealth, backendHealth } = useConvoy();

  const [rpTab, setRpTab] = useState('live');
  const [etaMap, setEtaMap] = useState({});
  const [segmentHistory, setSegmentHistory] = useState([]);
  const [historyLoading, setHistoryLoading] = useState(false);

  // Compute live team count from diversions in planResult
  const teamCount = useMemo(() => {
    if (!planResult?.diversion_directives) return 0;
    return planResult.diversion_directives.length;
  }, [planResult]);

  const stats = [
    { label: "Active Convoys", val: movements?.length || 0, color: "#ea580c", bg: "rgba(234,88,12,0.08)", icon: <Gauge size={14} color="#ea580c" /> },
    { label: "Anomalies", val: alerts?.length || 0, color: "#dc2626", bg: "rgba(220,38,38,0.08)", icon: <AlertTriangle size={14} color="#dc2626" /> },
    { label: "Segments Monitored", val: summary?.total_segments || 0, color: "#16a34a", bg: "rgba(22,163,74,0.08)", icon: <Eye size={14} color="#16a34a" /> },
    { label: "Diversion Units", val: teamCount || '—', color: "#2563eb", bg: "rgba(37,99,235,0.08)", icon: <Radio size={14} color="#2563eb" /> },
  ];

  // Fetch ETA for each active movement
  useEffect(() => {
    if (!movements?.length) return;
    movements.forEach(async (mov) => {
      if (!mov.segment_ids?.length || etaMap[mov.movement_id]) return;
      try {
        const res = await api.predictEta({
          segment_ids: mov.segment_ids,
          route_length_m: mov.total_distance_m || 10000,
          num_segments: mov.segment_ids.length,
          num_signals: 3,
          vvip_class: mov.vvip_class || vvipClass,
        });
        if (res?.data) setEtaMap(prev => ({ ...prev, [mov.movement_id]: res.data }));
      } catch { /* silent */ }
    });
  }, [movements, vvipClass]);

  // Fetch corridor segment history for sparkline
  const fetchHistory = useCallback(async () => {
    if (!summary?.avg_congestion_idx && summary?.avg_congestion_idx !== 0) return;
    setHistoryLoading(true);
    try {
      // Use a representative segment — prefer one from the active plan, else first from corridor summary
      const reprSeg = planResult?.primary_route?.segment_ids?.[0]
        || summary?.segment_ids?.[0]
        || 1001; // last-resort fallback
      const res = await api.getHistoricalPattern(reprSeg, 'daily_profile');
      if (res?.data?.hourly_data) {
        setSegmentHistory(res.data.hourly_data.map(h => ({
          hour: `${h.hour}:00`,
          speed: Math.round(h.avg_speed_kmh || 0),
          congestion: Math.round((h.p95_congestion || 0) * 100),
        })));
      }
    } catch { /* silent */ }
    setHistoryLoading(false);
  }, [summary]);

  useEffect(() => { fetchHistory(); }, [fetchHistory]);

  // Build TCP checkpoints from diversion_directives
  const tcps = useMemo(() => {
    if (!planResult?.diversion_directives?.length) return [];
    return planResult.diversion_directives.slice(0, 6).map((d, i) => ({
      name: `TCP-${i + 1}`,
      segmentId: d.segment_id,
      action: d.action,
      agency: d.agency?.replace(/_/g, ' ') || 'unknown',
      timing: `${d.timing_sec}s`,
      status: d.action === 'activate' ? 'ACTIVE' : d.action === 'hold' ? 'HOLD' : 'STANDBY',
    }));
  }, [planResult]);

  /* ── Analytics tab computed data (must be at component level for React hooks rules) ── */
  const anSh = convoySimulation?.speedHistory || [];
  const anCh = convoySimulation?.congestionHistory || [];
  const anHasData = anSh.length > 3;

  const speedBins = useMemo(() => {
    if (!anHasData) return [];
    const bins = [
      { range: '0-10', min: 0, max: 10, count: 0, fill: '#dc2626' },
      { range: '10-20', min: 10, max: 20, count: 0, fill: '#ea580c' },
      { range: '20-30', min: 20, max: 30, count: 0, fill: '#f97316' },
      { range: '30-40', min: 30, max: 40, count: 0, fill: '#eab308' },
      { range: '40-50', min: 40, max: 50, count: 0, fill: '#22c55e' },
      { range: '50+', min: 50, max: Infinity, count: 0, fill: '#16a34a' },
    ];
    anSh.forEach(d => { const b = bins.find(b => d.speed >= b.min && d.speed < b.max); if (b) b.count++; });
    return bins;
  }, [anSh, anHasData]);

  const scatterData = useMemo(() => {
    if (!anHasData) return [];
    return anSh.map((d, i) => ({
      speed: d.speed,
      congestion: Math.round((anCh[i]?.congestion ?? 0) * 100),
    })).filter(d => d.speed > 0);
  }, [anSh, anCh, anHasData]);

  const zoneTimePie = useMemo(() => {
    const zl = convoySimulation?.zoneLog;
    if (!zl?.length) return [];
    const zoneFills = { 'Urban Core': '#dc2626', 'Arterial Rd': '#ea580c', 'NH/Expressway': '#16a34a', 'Sub-Urban': '#3b82f6', 'Airport Zone': '#8b5cf6', 'Mixed Zone': '#eab308', 'Transition': '#64748b' };
    const totals = {};
    zl.forEach((z, i) => {
      const dur = i < zl.length - 1 ? zl[i + 1].at - z.at : (convoySimulation?.elapsedSeconds || 0) - z.at;
      totals[z.zone] = (totals[z.zone] || 0) + Math.round(dur);
    });
    return Object.entries(totals).map(([name, value]) => ({ name, value, fill: zoneFills[name] || '#475569' }));
  }, [convoySimulation?.zoneLog, convoySimulation?.elapsedSeconds]);

  const congestionBuckets = useMemo(() => {
    if (anCh.length < 4) return [];
    const bucketSize = Math.ceil(anCh.length / 6);
    const buckets = [];
    for (let i = 0; i < 6; i++) {
      const slice = anCh.slice(i * bucketSize, (i + 1) * bucketSize);
      if (!slice.length) break;
      const avg = slice.reduce((s, d) => s + (d.congestion || 0), 0) / slice.length;
      const pct = Math.round(avg * 100);
      buckets.push({ label: `T${i + 1}`, congestion: pct, fill: pct > 70 ? '#dc2626' : pct > 40 ? '#ea580c' : '#16a34a' });
    }
    return buckets;
  }, [anCh]);

  const etaTrend = useMemo(() => {
    const eh = convoySimulation?.etaHistory;
    if (!eh?.length) return [];
    return eh.map((d, i) => ({ i, eta: Math.round((d.eta ?? 0) / 60 * 10) / 10 }));
  }, [convoySimulation?.etaHistory]);

  const performanceData = useMemo(() => {
    if (!anHasData) return [];
    const cs = convoySimulation;
    const avgSpd = cs?.avgSpeed ?? 0;
    const fuel = cs?.fuelPct ?? 0;
    const prog = (cs?.progress ?? 0) * 100;
    return [
      { name: 'Speed', value: Math.min(Math.round(avgSpd / 60 * 100), 100), fill: '#ea580c' },
      { name: 'Fuel', value: Math.round(fuel), fill: '#16a34a' },
      { name: 'Progress', value: Math.round(prog), fill: '#3b82f6' },
    ];
  }, [anHasData, convoySimulation]);

  /* ── Idle-state computed metrics ── */
  const corridorHealth = useMemo(() => {
    const cgx = summary?.avg_congestion_idx ?? 0;
    const score = Math.round((1 - cgx) * 100);
    const spd = summary?.avg_speed_kmh ?? 0;
    const critRatio = summary?.total_segments ? (summary?.critical_segments ?? 0) / summary.total_segments : 0;
    const flowCapacity = Math.min(Math.round(spd / 60 * 100), 100);
    const networkLoad = Math.round(cgx * 100);
    return { score, cgx, spd, critRatio, flowCapacity, networkLoad };
  }, [summary]);

  const vramPct = useMemo(() => {
    if (!gpuHealth?.vramTotalMb) return 0;
    return Math.round(gpuHealth.vramUsedMb / gpuHealth.vramTotalMb * 100);
  }, [gpuHealth]);

  const anomalyBreakdown = useMemo(() => {
    if (!alerts?.length) return { high: 0, medium: 0, low: 0, total: 0 };
    const high = alerts.filter(a => a.severity === 'high').length;
    const medium = alerts.filter(a => a.severity === 'medium').length;
    const low = alerts.filter(a => a.severity === 'low').length;
    return { high, medium, low, total: alerts.length };
  }, [alerts]);

  const secProfile = useMemo(() => {
    return RP_SEC_SPECS[vvipClass] || RP_SEC_SPECS['Z'];
  }, [vvipClass]);

  /* ═══ PREDICT TAB — Synthetic Ground Data + ML Prediction Engine ═══ */
  const [predictNotifs, setPredictNotifs] = useState([]);
  const [cotSteps, setCotSteps] = useState([]);
  const [futureCotSteps, setFutureCotSteps] = useState([]);

  // Synthetic ground-level data features (simulates real-time sensor + crowd feeds)
  const groundData = useMemo(() => {
    const cs = convoySimulation;
    const spd = cs?.speed ?? summary?.avg_speed_kmh ?? 32;
    const cong = cs?.congestion ?? summary?.avg_congestion_idx ?? 0.4;
    const elapsed = cs?.elapsedSeconds ?? 0;
    const hour = new Date().getHours();
    const dow = new Date().getDay();
    const hourSin = Math.sin(2 * Math.PI * hour / 24);
    const hourCos = Math.cos(2 * Math.PI * hour / 24);
    const dowSin = Math.sin(2 * Math.PI * dow / 7);
    const dowCos = Math.cos(2 * Math.PI * dow / 7);
    const lanes = cs?.currentZone === 'NH/Expressway' ? 6 : cs?.currentZone === 'Urban Core' ? 4 : 3;
    const roadClassScore = cs?.currentZone === 'NH/Expressway' ? 100 : cs?.currentZone === 'Arterial Rd' ? 70 : cs?.currentZone === 'Urban Core' ? 50 : 30;

    // Simulated crowd density (people/km²)
    const crowdDensity = Math.round(1200 + Math.sin(elapsed * 0.02) * 400 + (hour > 8 && hour < 20 ? 600 : 0));
    // Intersection signal phase (0-3 = green/yellow/red/pending)
    const signalPhase = Math.floor((elapsed % 120) / 30);
    // Weather risk factor (0-1, synthetic sine-based)
    const weatherRisk = Math.min(1, Math.max(0, 0.15 + Math.sin(elapsed * 0.005) * 0.2));
    // Incident probability derived from congestion + crowd
    const incidentProb = Math.min(1, cong * 0.4 + (crowdDensity / 3000) * 0.3 + weatherRisk * 0.3);
    // Speed delta from last 10 readings
    const spdHistory = cs?.speedHistory?.slice(-10) || [];
    const speedDelta = spdHistory.length > 1 ? spdHistory[spdHistory.length - 1]?.speed - spdHistory[0]?.speed : 0;
    // Avg neighbor speed ratio (synthetic)
    const neighborSpeedRatio = Math.max(0.5, Math.min(1.5, 1 + Math.sin(elapsed * 0.015) * 0.3));

    return {
      speed: Math.round(spd * 10) / 10,
      congestion: Math.round(cong * 100),
      crowdDensity,
      signalPhase,
      weatherRisk: Math.round(weatherRisk * 100),
      incidentProb: Math.round(incidentProb * 100),
      lanes,
      roadClassScore,
      hourSin: Math.round(hourSin * 1000) / 1000,
      hourCos: Math.round(hourCos * 1000) / 1000,
      dowSin: Math.round(dowSin * 1000) / 1000,
      dowCos: Math.round(dowCos * 1000) / 1000,
      speedDelta: Math.round(speedDelta * 10) / 10,
      neighborSpeedRatio: Math.round(neighborSpeedRatio * 100) / 100,
      elapsed,
    };
  }, [convoySimulation, summary]);

  // Present-state recommendation engine (chain-of-thought)
  useEffect(() => {
    const gd = groundData;
    const steps = [];
    let recommendation = '';
    let severity = 'nominal';

    // Step 1: Analyze speed regime
    steps.push({
      step: 1, label: 'Speed Regime Analysis',
      thought: gd.speed > 40 ? `Speed ${gd.speed} km/h — free flow, nominal operations` :
               gd.speed > 25 ? `Speed ${gd.speed} km/h — moderate flow, watching for degradation` :
               `Speed ${gd.speed} km/h — slow flow detected, potential bottleneck`,
      status: gd.speed > 40 ? 'pass' : gd.speed > 25 ? 'warn' : 'fail',
    });

    // Step 2: Congestion assessment
    steps.push({
      step: 2, label: 'Congestion Evaluation',
      thought: gd.congestion < 30 ? `CGX ${gd.congestion}% — corridor clear, no intervention needed` :
               gd.congestion < 60 ? `CGX ${gd.congestion}% — moderate buildup, monitor upstream segments` :
               `CGX ${gd.congestion}% — heavy congestion, diversion activation recommended`,
      status: gd.congestion < 30 ? 'pass' : gd.congestion < 60 ? 'warn' : 'fail',
    });

    // Step 3: Crowd density check
    steps.push({
      step: 3, label: 'Crowd Density Assessment',
      thought: gd.crowdDensity < 1000 ? `Crowd ${gd.crowdDensity}/km² — low pedestrian risk` :
               gd.crowdDensity < 1800 ? `Crowd ${gd.crowdDensity}/km² — moderate density, maintaining clearance buffers` :
               `Crowd ${gd.crowdDensity}/km² — HIGH density zone, recommend speed reduction and enhanced escort formation`,
      status: gd.crowdDensity < 1000 ? 'pass' : gd.crowdDensity < 1800 ? 'warn' : 'fail',
    });

    // Step 4: Incident probability
    steps.push({
      step: 4, label: 'Incident Risk Model',
      thought: gd.incidentProb < 25 ? `Incident probability ${gd.incidentProb}% — within acceptable threshold` :
               gd.incidentProb < 50 ? `Incident probability ${gd.incidentProb}% — elevated risk, pre-positioning response units` :
               `Incident probability ${gd.incidentProb}% — HIGH risk corridor, recommend alternate routing or speed protocol override`,
      status: gd.incidentProb < 25 ? 'pass' : gd.incidentProb < 50 ? 'warn' : 'fail',
    });

    // Step 5: Weather factor
    steps.push({
      step: 5, label: 'Environmental Conditions',
      thought: gd.weatherRisk < 20 ? `Weather risk ${gd.weatherRisk}% — clear conditions, nominal visibility` :
               gd.weatherRisk < 35 ? `Weather risk ${gd.weatherRisk}% — mild conditions, no route change needed` :
               `Weather risk ${gd.weatherRisk}% — adverse conditions flagged, recommend reduced speed and increased following distance`,
      status: gd.weatherRisk < 20 ? 'pass' : gd.weatherRisk < 35 ? 'warn' : 'fail',
    });

    // Final recommendation synthesis
    const fails = steps.filter(s => s.status === 'fail').length;
    const warns = steps.filter(s => s.status === 'warn').length;
    if (fails >= 2) {
      recommendation = 'CRITICAL: Multiple parameters exceeding thresholds. Recommend immediate route re-evaluation or corridor switch. Activating diversion protocol.';
      severity = 'critical';
    } else if (fails === 1) {
      recommendation = 'CAUTION: Single parameter breach detected. Maintain current route with enhanced monitoring. Pre-stage alternate diversions.';
      severity = 'warning';
    } else if (warns >= 3) {
      recommendation = 'ADVISORY: Multiple parameters in warning bands. Current route viable but degrading. Prepare contingency routing.';
      severity = 'advisory';
    } else if (warns >= 1) {
      recommendation = 'NOMINAL: Minor fluctuations detected within tolerance. Current operations optimal. Continue monitoring.';
      severity = 'nominal';
    } else {
      recommendation = 'OPTIMAL: All parameters within nominal range. Corridor conditions ideal. Maintain current heading and speed.';
      severity = 'optimal';
    }

    steps.push({
      step: 6, label: 'Qwen 3.5 Synthesis',
      thought: recommendation,
      status: severity === 'optimal' ? 'pass' : severity === 'nominal' ? 'pass' : severity === 'advisory' ? 'warn' : 'fail',
    });

    setCotSteps(steps);
  }, [groundData]);

  // Future-state prediction engine (T+5/10/15/30 forecasting with CoT)
  useEffect(() => {
    const gd = groundData;
    const futureSteps = [];

    // Predict congestion trend
    const congTrend = gd.speedDelta < -2 ? 'increasing' : gd.speedDelta > 2 ? 'decreasing' : 'stable';
    const futCong5 = Math.min(100, Math.max(0, gd.congestion + (gd.speedDelta < 0 ? Math.abs(gd.speedDelta) * 3 : gd.speedDelta * -2)));
    const futCong15 = Math.min(100, Math.max(0, futCong5 + (congTrend === 'increasing' ? 12 : congTrend === 'decreasing' ? -8 : 2)));
    const futCong30 = Math.min(100, Math.max(0, futCong15 + (congTrend === 'increasing' ? 18 : congTrend === 'decreasing' ? -12 : 3)));

    // Future speed estimates
    const futSpd5 = Math.max(5, gd.speed + gd.speedDelta * 0.5);
    const futSpd15 = Math.max(5, futSpd5 + (congTrend === 'increasing' ? -8 : congTrend === 'decreasing' ? 5 : 0));
    const futSpd30 = Math.max(5, futSpd15 + (congTrend === 'increasing' ? -12 : congTrend === 'decreasing' ? 8 : -1));

    // Future crowd forecast
    const hour = new Date().getHours();
    const peakApproaching = (hour >= 7 && hour < 9) || (hour >= 16 && hour < 19);
    const futCrowd = gd.crowdDensity + (peakApproaching ? 400 : -150);

    futureSteps.push({
      step: 1, label: 'DSTGAT T+5min Forecast',
      thought: `Predicted congestion ${Math.round(futCong5)}% (${congTrend}), speed ~${Math.round(futSpd5)} km/h. ${futCong5 > 60 ? 'Buildup expected — pre-activate upstream diversions.' : 'Corridor should remain passable.'}`,
      horizon: 'T+5',
      metrics: { congestion: Math.round(futCong5), speed: Math.round(futSpd5) },
      status: futCong5 > 60 ? 'fail' : futCong5 > 35 ? 'warn' : 'pass',
    });

    futureSteps.push({
      step: 2, label: 'DSTGAT T+15min Forecast',
      thought: `Projected congestion ${Math.round(futCong15)}% at T+15. Speed estimate ~${Math.round(futSpd15)} km/h. ${futCong15 > 65 ? 'CRITICAL: Gridlock risk. Recommend proactive route switch before reaching segment.' : futCong15 > 40 ? 'Moderate degradation — standby alternate routing.' : 'Clear corridor projected.'}`,
      horizon: 'T+15',
      metrics: { congestion: Math.round(futCong15), speed: Math.round(futSpd15) },
      status: futCong15 > 65 ? 'fail' : futCong15 > 40 ? 'warn' : 'pass',
    });

    futureSteps.push({
      step: 3, label: 'DSTGAT T+30min Forecast',
      thought: `30-minute projection: CGX ${Math.round(futCong30)}%, speed ~${Math.round(futSpd30)} km/h. Crowd density forecast: ${Math.round(futCrowd)}/km². ${futCong30 > 70 ? 'SEVERE: Extended congestion event likely. Pre-coordinate with traffic police for extended closures.' : 'Route viability maintained within 30-minute window.'}`,
      horizon: 'T+30',
      metrics: { congestion: Math.round(futCong30), speed: Math.round(futSpd30) },
      status: futCong30 > 70 ? 'fail' : futCong30 > 45 ? 'warn' : 'pass',
    });

    // Peak-hour impact analysis
    futureSteps.push({
      step: 4, label: 'Peak-Hour Impact Model',
      thought: peakApproaching
        ? `Peak hour ${hour >= 16 ? 'evening' : 'morning'} rush approaching. Historical data indicates ${Math.round(gd.congestion * 1.4)}% congestion uplift. Crowd density may reach ${Math.round(futCrowd)}/km². Recommend completing corridor transit within ${Math.round((60 - (gd.elapsed % 3600) / 60))} minutes.`
        : `Off-peak period. No significant crowd or congestion surge expected in the next 30 minutes. Current trajectory sustainable.`,
      status: peakApproaching && gd.congestion > 40 ? 'warn' : 'pass',
    });

    // Route-specific future recommendation
    const overallFutureRisk = (futCong30 + gd.incidentProb + gd.weatherRisk) / 3;
    futureSteps.push({
      step: 5, label: 'Qwen 3.5 Future Synthesis',
      thought: overallFutureRisk > 55
        ? `FORECAST ALERT: Composite future risk score ${Math.round(overallFutureRisk)}%. Recommend pre-emptive re-routing via alternate corridor. Signal controllers should be notified for priority override at upcoming intersections. ETA impact: +${Math.round(overallFutureRisk / 10)} minutes.`
        : overallFutureRisk > 35
        ? `FORECAST ADVISORY: Composite risk ${Math.round(overallFutureRisk)}%. Current route remains viable but conditions may degrade. Maintain heightened readiness. Pre-compute 2 alternate diversions.`
        : `FORECAST NOMINAL: Composite risk ${Math.round(overallFutureRisk)}%. All forward-looking indicators stable. No pre-emptive action required. Corridor clear for estimated transit window.`,
      status: overallFutureRisk > 55 ? 'fail' : overallFutureRisk > 35 ? 'warn' : 'pass',
    });

    setFutureCotSteps(futureSteps);
  }, [groundData]);

  // Live notification generator based on ground data + predictions
  useEffect(() => {
    const gd = groundData;
    const now = Date.now();
    const newNotifs = [];
    const cs = convoySimulation;

    if (gd.congestion > 65) {
      newNotifs.push({ id: `cng-${now}`, type: 'critical', icon: '🔴', text: `Congestion spike ${gd.congestion}% — diversion advisory`, ts: now, category: 'traffic' });
    } else if (gd.congestion > 40) {
      newNotifs.push({ id: `cng-w-${now}`, type: 'warning', icon: '🟡', text: `Congestion elevated ${gd.congestion}% — monitoring`, ts: now, category: 'traffic' });
    }

    if (gd.crowdDensity > 1800) {
      newNotifs.push({ id: `cwd-${now}`, type: 'critical', icon: '👥', text: `High crowd density ${gd.crowdDensity}/km² — speed protocol activated`, ts: now, category: 'security' });
    }

    if (gd.incidentProb > 50) {
      newNotifs.push({ id: `inc-${now}`, type: 'critical', icon: '⚠️', text: `Incident probability ${gd.incidentProb}% — response units pre-staged`, ts: now, category: 'safety' });
    }

    if (gd.speedDelta < -5) {
      newNotifs.push({ id: `spd-${now}`, type: 'warning', icon: '📉', text: `Rapid deceleration detected (Δ${gd.speedDelta} km/h) — analyzing cause`, ts: now, category: 'movement' });
    }

    if (cs?.signalsRemaining !== undefined && cs.signalsRemaining <= 2 && cs.signalsRemaining > 0) {
      newNotifs.push({ id: `sig-${now}`, type: 'info', icon: '🚦', text: `${cs.signalsRemaining} signal(s) remaining — requesting priority override`, ts: now, category: 'control' });
    }

    if (gd.weatherRisk > 30) {
      newNotifs.push({ id: `wx-${now}`, type: 'warning', icon: '🌧️', text: `Weather risk factor ${gd.weatherRisk}% — visibility advisory`, ts: now, category: 'environment' });
    }

    if (newNotifs.length > 0) {
      setPredictNotifs(prev => [...newNotifs, ...prev].slice(0, 20));
    }
  }, [groundData, convoySimulation]);

  // Future forecast data for mini charts
  const forecastChartData = useMemo(() => {
    const gd = groundData;
    const trend = gd.speedDelta < -2 ? 'up' : gd.speedDelta > 2 ? 'down' : 'flat';
    return [
      { t: 'Now', congestion: gd.congestion, speed: Math.round(gd.speed) },
      { t: 'T+5', congestion: Math.min(100, gd.congestion + (trend === 'up' ? 8 : trend === 'down' ? -5 : 2)), speed: Math.max(5, Math.round(gd.speed + gd.speedDelta * 0.5)) },
      { t: 'T+10', congestion: Math.min(100, gd.congestion + (trend === 'up' ? 14 : trend === 'down' ? -8 : 3)), speed: Math.max(5, Math.round(gd.speed + gd.speedDelta * 0.8)) },
      { t: 'T+15', congestion: Math.min(100, gd.congestion + (trend === 'up' ? 20 : trend === 'down' ? -12 : 4)), speed: Math.max(5, Math.round(gd.speed + gd.speedDelta * 1.1)) },
      { t: 'T+30', congestion: Math.min(100, gd.congestion + (trend === 'up' ? 30 : trend === 'down' ? -18 : 5)), speed: Math.max(5, Math.round(gd.speed + gd.speedDelta * 1.5)) },
    ];
  }, [groundData]);

  return (
    <>
      <div 
        className={`sp overflow-y-auto ${lifecycle === 'active' ? 'convoy-active-glow' : ''}`}
        style={{
          position: 'absolute', top: 0, bottom: 0, right: 0,
          width: `${RWIDTH}px`, backgroundColor: '#0f172a',
          borderLeft: '1px solid #334155', boxShadow: '-4px 0 24px rgba(0,0,0,0.3)',
          zIndex: 1000, transform: open ? 'translateX(0)' : `translateX(${RWIDTH}px)`,
          transition: 'transform 0.3s cubic-bezier(0.4, 0, 0.2, 1)',
          display: 'flex', flexDirection: 'column'
        }}
      >
        {/* Stats Strip — compact horizontal */}
        <div style={{ padding: '10px 12px 6px', display: 'flex', gap: '6px', marginBottom: '4px' }}>
          {stats.map(s => (
            <div key={s.label} className="glow-card" style={{ flex: 1, padding: '8px 6px', backgroundColor: s.bg, borderRadius: '10px', border: `1px solid ${s.color}25`, textAlign: 'center' }}>
              <div style={{ marginBottom: '2px' }}>{s.icon}</div>
              <div className="data-readout" style={{ fontSize: '16px', fontWeight: 800, color: s.color, fontFamily: 'var(--font-mono)', letterSpacing: '-0.02em' }}>{s.val}</div>
              <div style={{ fontSize: '6px', color: '#94a3b8', textTransform: 'uppercase', marginTop: '1px', letterSpacing: '0.3px', fontWeight: 600 }}>{s.label}</div>
            </div>
          ))}
        </div>

        {/* ═══ TAB BAR ═══ */}
        <div style={{ padding: '0 10px 8px', display: 'flex', gap: '3px', borderBottom: '1px solid #1e293b' }}>
          {RP_TABS.map(t => (
            <button key={t.key} onClick={() => setRpTab(t.key)} style={{
              flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '4px',
              padding: '6px 4px', border: 'none', borderRadius: '8px 8px 0 0', cursor: 'pointer',
              fontSize: '8px', fontWeight: 700, letterSpacing: '0.5px', textTransform: 'uppercase',
              background: rpTab === t.key ? 'linear-gradient(180deg, rgba(234,88,12,0.15), transparent)' : 'transparent',
              color: rpTab === t.key ? '#f97316' : '#64748b',
              borderBottom: rpTab === t.key ? '2px solid #ea580c' : '2px solid transparent',
              transition: 'all 0.2s ease',
            }}>
              {t.icon} {t.label}
            </button>
          ))}
        </div>

        {/* ═══════════════════════════════════════════════════ */}
        {/* ═══ TAB: LIVE — Convoy Tracking ═══ */}
        {/* ═══════════════════════════════════════════════════ */}
        {rpTab === 'live' && convoySimulation?.active && (
          <div className="sp" style={{ padding: '0 12px 12px', flex: 1, overflowY: 'auto' }}>
            <div className="flex items-center justify-between" style={{ marginBottom: '6px' }}>
              <div className="flex items-center gap-2">
                <span className="live-data-dot" />
                <span style={{ fontSize: '10px', fontWeight: 700, color: '#ea580c', letterSpacing: '0.05em' }}>
                  CONVOY {convoySimulation.paused ? 'PAUSED' : 'TRACKING'}
                </span>
              </div>
              <span style={{ fontSize: '9px', fontFamily: 'var(--font-mono)', color: '#16a34a', fontWeight: 700 }}>
                {(convoySimulation.progress * 100).toFixed(1)}%
              </span>
            </div>

            {/* ═══ PERFORMANCE INDEX — visible first ═══ */}
            {(() => {
              const spd = convoySimulation.speed || 0;
              const cong = convoySimulation.congestion ?? 0;
              const fuel = convoySimulation.fuelPct ?? 100;
              const perfIdx = Math.round(
                (Math.min(spd / 50, 1) * 30) +
                ((1 - cong) * 30) +
                (fuel / 100 * 20) +
                (convoySimulation.progress * 20)
              );
              const perfColor = perfIdx > 70 ? '#22c55e' : perfIdx > 40 ? '#eab308' : '#dc2626';
              return (
                <div style={{ marginBottom: '6px' }}>
                  <div style={{ fontSize: '8px', color: '#64748b', textTransform: 'uppercase', fontWeight: 600, letterSpacing: '0.5px', marginBottom: '3px', display: 'flex', justifyContent: 'space-between' }}>
                    <span>Performance Index</span>
                    <span style={{ fontFamily: 'var(--font-mono)', color: perfColor, fontWeight: 800, fontSize: '11px' }}>{perfIdx}</span>
                  </div>
                  <div style={{ height: '6px', backgroundColor: '#1e293b', borderRadius: '3px', overflow: 'hidden' }}>
                    <div style={{ height: '100%', width: `${perfIdx}%`, borderRadius: '3px', background: `linear-gradient(90deg, #dc2626, #eab308 40%, #22c55e 75%)`, transition: 'width 0.3s', boxShadow: `0 0 6px ${perfColor}40` }} />
                  </div>
                  <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: '2px' }}>
                    <span style={{ fontSize: '6px', color: '#475569' }}>POOR</span>
                    <span style={{ fontSize: '6px', color: '#475569' }}>OPTIMAL</span>
                  </div>
                </div>
              );
            })()}

            {/* ═══ SPEED TRACE SPARKLINE — immediately visible ═══ */}
            {convoySimulation.speedHistory?.length > 2 && (
              <div style={{ marginBottom: '6px' }}>
                <div style={{ fontSize: '8px', color: '#64748b', textTransform: 'uppercase', fontWeight: 600, letterSpacing: '0.5px', marginBottom: '2px', display: 'flex', justifyContent: 'space-between' }}>
                  <span>Speed Trace</span>
                  <span style={{ fontFamily: 'var(--font-mono)', color: '#22c55e', fontSize: '9px', fontWeight: 700 }}>
                    {(convoySimulation.speed || 0).toFixed(1)} km/h
                  </span>
                </div>
                <div style={{ height: '44px', backgroundColor: '#0f172a', borderRadius: '6px', padding: '2px', border: '1px solid #1e293b' }}>
                  <ResponsiveContainer width="100%" height="100%">
                    <AreaChart data={convoySimulation.speedHistory.slice(-40)}>
                      <defs>
                        <linearGradient id="spdLiveGrad" x1="0" y1="0" x2="0" y2="1">
                          <stop offset="0%" stopColor="#22c55e" stopOpacity={0.4} />
                          <stop offset="100%" stopColor="#22c55e" stopOpacity={0} />
                        </linearGradient>
                      </defs>
                      <Area type="monotone" dataKey="speed" stroke="#22c55e" strokeWidth={1.5} fill="url(#spdLiveGrad)" isAnimationActive={false} dot={false} />
                    </AreaChart>
                  </ResponsiveContainer>
                </div>
              </div>
            )}

            {/* ═══ CONGESTION TRACE SPARKLINE ═══ */}
            {convoySimulation.congestionHistory?.length > 2 && (
              <div style={{ marginBottom: '6px' }}>
                <div style={{ fontSize: '8px', color: '#64748b', textTransform: 'uppercase', fontWeight: 600, letterSpacing: '0.5px', marginBottom: '2px', display: 'flex', justifyContent: 'space-between' }}>
                  <span>Congestion Trace</span>
                  <span style={{ fontFamily: 'var(--font-mono)', color: '#f97316', fontSize: '9px', fontWeight: 700 }}>
                    {((convoySimulation.congestion ?? convoySimulation.congestionHistory?.[convoySimulation.congestionHistory.length - 1]?.congestion ?? 0) * 100).toFixed(0)}%
                  </span>
                </div>
                <div style={{ height: '44px', backgroundColor: '#0f172a', borderRadius: '6px', padding: '2px', border: '1px solid #1e293b' }}>
                  <ResponsiveContainer width="100%" height="100%">
                    <AreaChart data={convoySimulation.congestionHistory.slice(-40)}>
                      <defs>
                        <linearGradient id="cngLiveGrad" x1="0" y1="0" x2="0" y2="1">
                          <stop offset="0%" stopColor="#f97316" stopOpacity={0.4} />
                          <stop offset="100%" stopColor="#f97316" stopOpacity={0} />
                        </linearGradient>
                      </defs>
                      <Area type="monotone" dataKey="congestion" stroke="#f97316" strokeWidth={1.5} fill="url(#cngLiveGrad)" isAnimationActive={false} dot={false} />
                    </AreaChart>
                  </ResponsiveContainer>
                </div>
              </div>
            )}

            {/* ═══ SVG Speed Gauge + Compass — compact ═══ */}
            <div style={{ display: 'flex', gap: '6px', justifyContent: 'center', marginBottom: '6px' }}>
              <div style={{ position: 'relative', width: '90px', height: '60px' }}>
                <svg viewBox="0 0 120 70" width="90" height="60">
                  <path d="M 15 60 A 50 50 0 0 1 105 60" fill="none" stroke="#1e293b" strokeWidth="8" strokeLinecap="round"/>
                  <path d="M 15 60 A 50 50 0 0 1 105 60" fill="none"
                    stroke={convoySimulation.speed > 40 ? '#22c55e' : convoySimulation.speed > 25 ? '#eab308' : '#dc2626'}
                    strokeWidth="8" strokeLinecap="round"
                    strokeDasharray={`${Math.min((convoySimulation.speed || 0) / 60, 1) * 141.4} 141.4`}
                    style={{ filter: `drop-shadow(0 0 4px ${convoySimulation.speed > 40 ? 'rgba(34,197,94,0.5)' : convoySimulation.speed > 25 ? 'rgba(234,179,8,0.5)' : 'rgba(220,38,38,0.5)'})`, transition: 'stroke-dasharray 0.2s, stroke 0.3s' }}
                  />
                  {[0, 15, 30, 45, 60].map((tick) => {
                    const angle = -180 + (tick / 60) * 180;
                    const rad = angle * Math.PI / 180;
                    const x1 = 60 + 42 * Math.cos(rad), y1 = 60 + 42 * Math.sin(rad);
                    const x2 = 60 + 48 * Math.cos(rad), y2 = 60 + 48 * Math.sin(rad);
                    return <line key={tick} x1={x1} y1={y1} x2={x2} y2={y2} stroke="#475569" strokeWidth="1.5"/>;
                  })}
                  <text x="60" y="52" textAnchor="middle" fill={convoySimulation.speed > 40 ? '#22c55e' : convoySimulation.speed > 25 ? '#eab308' : '#dc2626'} fontSize="18" fontWeight="800" fontFamily="var(--font-mono)">{(convoySimulation.speed || 0).toFixed(0)}</text>
                  <text x="60" y="64" textAnchor="middle" fill="#64748b" fontSize="7" fontWeight="600">km/h</text>
                </svg>
              </div>
              <div style={{ position: 'relative', width: '50px', height: '50px' }}>
                <svg viewBox="0 0 60 60" width="50" height="50">
                  <circle cx="30" cy="30" r="22" fill="#0f172a" stroke="#334155" strokeWidth="1"/>
                  <text x="30" y="12" textAnchor="middle" fill="#64748b" fontSize="7" fontWeight="700">N</text>
                  <text x="30" y="55" textAnchor="middle" fill="#475569" fontSize="6">S</text>
                  <text x="8" y="33" textAnchor="middle" fill="#475569" fontSize="6">W</text>
                  <text x="52" y="33" textAnchor="middle" fill="#475569" fontSize="6">E</text>
                  <g transform={`rotate(${convoySimulation.heading || 0} 30 30)`} style={{ transition: 'transform 0.2s linear' }}>
                    <polygon points="30,10 32,27 28,27" fill="#ea580c"/>
                    <polygon points="30,50 32,33 28,33" fill="#334155"/>
                    <circle cx="30" cy="30" r="2.5" fill="#1e293b" stroke="#ea580c" strokeWidth="1"/>
                  </g>
                </svg>
                <div style={{ position: 'absolute', bottom: '-2px', width: '100%', textAlign: 'center', fontSize: '7px', fontFamily: 'var(--font-mono)', color: '#e2e8f0', fontWeight: 700 }}>
                  {(convoySimulation.heading || 0).toFixed(0)}°
                </div>
              </div>
            </div>

            {/* ═══ Progress bar ═══ */}
            <div style={{ marginBottom: '6px' }}>
              <div className="flex justify-between" style={{ marginBottom: '2px' }}>
                <span style={{ fontSize: '8px', color: '#94a3b8', textTransform: 'uppercase', fontWeight: 600 }}>Route Progress</span>
                <span style={{ fontSize: '9px', fontFamily: 'var(--font-mono)', color: '#ea580c', fontWeight: 700 }}>
                  {(convoySimulation.distanceTraveledM / 1000).toFixed(1)} / {(convoySimulation.totalDistanceM / 1000).toFixed(1)} km
                </span>
              </div>
              <div style={{ height: '7px', backgroundColor: '#1e293b', borderRadius: '4px', overflow: 'hidden', border: '1px solid #334155', position: 'relative' }}>
                <div style={{
                  width: `${(convoySimulation.progress * 100).toFixed(1)}%`,
                  height: '100%', borderRadius: '4px',
                  background: 'linear-gradient(90deg, #ea580c, #f97316, #fbbf24)',
                  transition: 'width 0.2s ease',
                  boxShadow: '0 0 8px rgba(249,115,22,0.4)',
                }} />
                <div style={{
                  position: 'absolute', top: '50%', transform: 'translateY(-50%)',
                  left: `calc(${Math.min(convoySimulation.progress * 100, 99)}% - 3px)`,
                  width: '6px', height: '6px', borderRadius: '50%',
                  background: '#fbbf24', border: '1.5px solid #0f172a',
                  boxShadow: '0 0 6px rgba(251,191,36,0.6)',
                  transition: 'left 0.2s ease',
                }} />
              </div>
            </div>

            {/* ═══ CORRIDOR CONDITIONS ═══ */}
            <div style={{ padding: '5px 7px', backgroundColor: '#0f172a', borderRadius: '6px', border: '1px solid #1e293b', marginBottom: '6px' }}>
              <div style={{ fontSize: '7px', color: '#64748b', textTransform: 'uppercase', fontWeight: 600, letterSpacing: '0.5px', marginBottom: '3px' }}>Corridor Status</div>
              <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
                <div style={{ flex: 1 }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '2px' }}>
                    <span style={{ fontSize: '7px', color: '#94a3b8' }}>Avg CGX</span>
                    <span style={{ fontSize: '8px', fontFamily: 'var(--font-mono)', color: '#f97316', fontWeight: 700 }}>{(summary?.avg_congestion ?? 0).toFixed(3)}</span>
                  </div>
                  <div style={{ height: '3px', backgroundColor: '#1e293b', borderRadius: '2px', overflow: 'hidden' }}>
                    <div style={{ height: '100%', width: `${(summary?.avg_congestion ?? 0) * 100}%`, background: 'linear-gradient(90deg, #22c55e, #eab308, #dc2626)', borderRadius: '2px' }} />
                  </div>
                </div>
                <div style={{ width: '1px', height: '16px', backgroundColor: '#1e293b' }} />
                <div style={{ flex: 1 }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '2px' }}>
                    <span style={{ fontSize: '7px', color: '#94a3b8' }}>Avg Speed</span>
                    <span style={{ fontSize: '8px', fontFamily: 'var(--font-mono)', color: '#22c55e', fontWeight: 700 }}>{(summary?.avg_speed ?? 0).toFixed(1)}</span>
                  </div>
                  <div style={{ height: '3px', backgroundColor: '#1e293b', borderRadius: '2px', overflow: 'hidden' }}>
                    <div style={{ height: '100%', width: `${Math.min((summary?.avg_speed ?? 0) / 60 * 100, 100)}%`, background: 'linear-gradient(90deg, #dc2626, #eab308, #22c55e)', borderRadius: '2px' }} />
                  </div>
                </div>
              </div>
            </div>

            {/* ═══ FULL TELEMETRY GRID — 3x3 compact ═══ */}
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '4px', marginBottom: '6px' }}>
              {[
                { label: 'ETA', value: convoySimulation.etaSeconds > 0 ? `${Math.floor(convoySimulation.etaSeconds / 60)}:${String(Math.floor(convoySimulation.etaSeconds % 60)).padStart(2, '0')}` : '--:--', color: '#3b82f6' },
                { label: 'Congest', value: `${((convoySimulation.congestion ?? 0) * 100).toFixed(0)}%`, color: (() => { const c = convoySimulation.congestion ?? 0; return c > 0.6 ? '#dc2626' : c > 0.3 ? '#eab308' : '#22c55e'; })() },
                { label: 'Accel', value: `${(convoySimulation.acceleration ?? 0) > 0 ? '↑' : '↓'} ${Math.abs(convoySimulation.acceleration ?? 0).toFixed(1)}`, color: (convoySimulation.acceleration ?? 0) > 0 ? '#22c55e' : '#f97316' },
                { label: 'Avg Spd', value: `${(convoySimulation.avgSpeed || 0).toFixed(1)}`, color: '#06b6d4' },
                { label: 'Max Spd', value: `${(convoySimulation.maxSpeed ?? 0).toFixed(0)}`, color: '#22c55e' },
                { label: 'Min Spd', value: `${(convoySimulation.minSpeed ?? 0) < 900 ? (convoySimulation.minSpeed ?? 0).toFixed(0) : '—'}`, color: '#dc2626' },
                { label: 'Dist', value: `${(convoySimulation.distanceTraveledM / 1000).toFixed(2)}km`, color: '#8b5cf6' },
                { label: 'Remain', value: `${(convoySimulation.distanceRemainingM / 1000).toFixed(2)}km`, color: '#f97316' },
                { label: 'Segments', value: `${convoySimulation.segmentsTraversed ?? 0}`, color: '#a78bfa' },
              ].map(m => (
                <div key={m.label} style={{ padding: '4px', backgroundColor: '#0f172a', borderRadius: '5px', border: '1px solid #1e293b', textAlign: 'center' }}>
                  <div style={{ fontSize: '10px', fontWeight: 800, fontFamily: 'var(--font-mono)', color: m.color }}>{m.value}</div>
                  <div style={{ fontSize: '6px', color: '#64748b', textTransform: 'uppercase', fontWeight: 600, marginTop: '1px' }}>{m.label}</div>
                </div>
              ))}
            </div>

            {/* ═══ THREAT ASSESSMENT STRIP ═══ */}
            {(() => {
              const thr = convoySimulation.threatLevel || 'nominal';
              const thrLevels = [
                { key: 'nominal', label: 'NOM', color: '#16a34a' },
                { key: 'guarded', label: 'GRD', color: '#2563eb' },
                { key: 'moderate', label: 'MOD', color: '#eab308' },
                { key: 'elevated', label: 'ELV', color: '#ea580c' },
                { key: 'critical', label: 'CRT', color: '#dc2626' },
              ];
              const activeIdx = thrLevels.findIndex(t => t.key === thr);
              return (
                <div style={{ marginBottom: '6px' }}>
                  <div style={{ fontSize: '8px', color: '#64748b', textTransform: 'uppercase', fontWeight: 600, letterSpacing: '0.5px', marginBottom: '3px' }}>Threat Assessment</div>
                  <div style={{ display: 'flex', borderRadius: '4px', overflow: 'hidden', height: '16px' }}>
                    {thrLevels.map((t, i) => (
                      <div key={t.key} style={{
                        flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center',
                        background: i <= activeIdx ? t.color : '#1e293b',
                        opacity: i <= activeIdx ? 1 : 0.3,
                        fontSize: '7px', fontWeight: 700, color: i <= activeIdx ? '#fff' : '#475569',
                        letterSpacing: '0.3px', transition: 'all 0.3s ease',
                        borderRight: i < 4 ? '1px solid #0f172a' : 'none',
                      }}>{t.label}</div>
                    ))}
                  </div>
                </div>
              );
            })()}

            {/* ═══ SECURITY PROFILE ═══ */}
            {(() => {
              const spec = RP_SEC_SPECS[vvipClass] || RP_SEC_SPECS['Z'];
              return (
                <div style={{ padding: '5px 7px', backgroundColor: '#0f172a', borderRadius: '6px', border: '1px solid rgba(234,88,12,0.2)', marginBottom: '6px' }}>
                  <div style={{ fontSize: '7px', color: '#64748b', textTransform: 'uppercase', fontWeight: 600, letterSpacing: '0.5px', marginBottom: '3px' }}>
                    Security — {vvipClass || 'Z'}
                  </div>
                  <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr 1fr', gap: '4px' }}>
                    {[
                      { label: 'Lanes', value: spec.minLanes, color: '#3b82f6' },
                      { label: 'Closure', value: spec.closure, color: '#f97316' },
                      { label: 'Advance', value: `${spec.advance}s`, color: '#8b5cf6' },
                      { label: 'Queue', value: `${spec.maxQueue}m`, color: '#dc2626' },
                    ].map(s => (
                      <div key={s.label} style={{ textAlign: 'center' }}>
                        <div style={{ fontSize: '9px', fontWeight: 800, fontFamily: 'var(--font-mono)', color: s.color }}>{s.value}</div>
                        <div style={{ fontSize: '6px', color: '#475569', textTransform: 'uppercase' }}>{s.label}</div>
                      </div>
                    ))}
                  </div>
                </div>
              );
            })()}

            {/* ═══ FUEL / ZONE / G-FORCE / SIGNALS ═══ */}
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '4px', marginBottom: '6px' }}>
              <div style={{ padding: '5px', backgroundColor: '#0f172a', borderRadius: '6px', border: '1px solid #1e293b' }}>
                <div style={{ fontSize: '7px', color: '#64748b', textTransform: 'uppercase', fontWeight: 600, marginBottom: '2px' }}>Fuel</div>
                <div style={{ fontSize: '12px', fontWeight: 800, fontFamily: 'var(--font-mono)', color: (convoySimulation.fuelPct ?? 100) > 40 ? '#22c55e' : (convoySimulation.fuelPct ?? 100) > 15 ? '#eab308' : '#dc2626' }}>
                  {(convoySimulation.fuelPct ?? 100).toFixed(0)}%
                </div>
                <div style={{ height: '3px', borderRadius: '2px', backgroundColor: '#1e293b', marginTop: '2px', overflow: 'hidden' }}>
                  <div style={{ height: '100%', width: `${convoySimulation.fuelPct ?? 100}%`, borderRadius: '2px', background: 'linear-gradient(90deg, #dc2626, #eab308, #22c55e)', transition: 'width 0.3s' }} />
                </div>
              </div>
              <div style={{ padding: '5px', backgroundColor: '#0f172a', borderRadius: '6px', border: '1px solid #1e293b' }}>
                <div style={{ fontSize: '7px', color: '#64748b', textTransform: 'uppercase', fontWeight: 600, marginBottom: '2px' }}>G-Force</div>
                <div style={{ fontSize: '12px', fontWeight: 800, fontFamily: 'var(--font-mono)', color: (convoySimulation.gForce ?? 0) > 0.4 ? '#dc2626' : (convoySimulation.gForce ?? 0) > 0.2 ? '#eab308' : '#22c55e' }}>
                  {(convoySimulation.gForce ?? 0).toFixed(2)}g
                </div>
              </div>
              <div style={{ padding: '5px', backgroundColor: '#0f172a', borderRadius: '6px', border: '1px solid #1e293b' }}>
                <div style={{ fontSize: '7px', color: '#64748b', textTransform: 'uppercase', fontWeight: 600, marginBottom: '2px' }}>Zone</div>
                <div style={{ fontSize: '10px', fontWeight: 700, color: '#a78bfa', textTransform: 'capitalize' }}>
                  {convoySimulation.currentZone || 'primary'}
                </div>
              </div>
              <div style={{ padding: '5px', backgroundColor: '#0f172a', borderRadius: '6px', border: '1px solid #1e293b' }}>
                <div style={{ fontSize: '7px', color: '#64748b', textTransform: 'uppercase', fontWeight: 600, marginBottom: '2px' }}>Signals</div>
                <div style={{ fontSize: '12px', fontWeight: 800, fontFamily: 'var(--font-mono)', color: '#3b82f6' }}>
                  {Math.max(0, Math.floor(convoySimulation.signalsRemaining ?? 0))} rem
                </div>
              </div>
            </div>

            {/* ═══ ELAPSED + HEADING row ═══ */}
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '4px', marginBottom: '6px' }}>
              <div style={{ padding: '5px', backgroundColor: '#0f172a', borderRadius: '6px', border: '1px solid #1e293b', textAlign: 'center' }}>
                <div style={{ fontSize: '12px', fontWeight: 800, fontFamily: 'var(--font-mono)', color: '#06b6d4' }}>{Math.floor((convoySimulation.elapsedSeconds || 0) / 60)}:{String(Math.floor((convoySimulation.elapsedSeconds || 0) % 60)).padStart(2, '0')}</div>
                <div style={{ fontSize: '6px', color: '#64748b', textTransform: 'uppercase', fontWeight: 600 }}>Elapsed</div>
              </div>
              <div style={{ padding: '5px', backgroundColor: '#0f172a', borderRadius: '6px', border: '1px solid #1e293b', textAlign: 'center' }}>
                <div style={{ fontSize: '12px', fontWeight: 800, fontFamily: 'var(--font-mono)', color: '#a78bfa' }}>{(convoySimulation.heading || 0).toFixed(0)}°</div>
                <div style={{ fontSize: '6px', color: '#64748b', textTransform: 'uppercase', fontWeight: 600 }}>Heading</div>
              </div>
            </div>

            {/* ═══ ZONE TRANSITION TIMELINE ═══ */}
            {convoySimulation.zoneLog?.length > 0 && (
              <div style={{ marginBottom: '6px' }}>
                <div style={{ fontSize: '8px', color: '#64748b', textTransform: 'uppercase', fontWeight: 600, letterSpacing: '0.5px', marginBottom: '3px' }}>Zone Transitions</div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
                  {convoySimulation.zoneLog.slice(-4).map((z, i) => (
                    <div key={i} style={{ display: 'flex', alignItems: 'center', gap: '6px', padding: '2px 6px', backgroundColor: '#0f172a', borderRadius: '4px', borderLeft: '2px solid #7c3aed' }}>
                      <span style={{ fontSize: '8px', fontFamily: 'var(--font-mono)', color: '#94a3b8', minWidth: '30px' }}>
                        {Math.floor((z.time || 0) / 60)}:{String(Math.floor((z.time || 0) % 60)).padStart(2, '0')}
                      </span>
                      <span style={{ fontSize: '8px', color: '#64748b', textTransform: 'capitalize' }}>{z.from}</span>
                      <span style={{ fontSize: '7px', color: '#475569' }}>→</span>
                      <span style={{ fontSize: '8px', color: '#a78bfa', fontWeight: 700, textTransform: 'capitalize' }}>{z.to}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* ═══ COMPOSITE SPEED + CONGESTION — dual-axis overlay ═══ */}
            {anHasData && (
              <div style={{ marginBottom: '6px' }}>
                <div style={{ fontSize: '8px', color: '#64748b', textTransform: 'uppercase', fontWeight: 600, letterSpacing: '0.5px', marginBottom: '2px', display: 'flex', justifyContent: 'space-between' }}>
                  <span>Speed × Congestion</span>
                  <span style={{ fontSize: '7px', color: '#475569' }}>COMPOSITE</span>
                </div>
                <div style={{ height: '72px', backgroundColor: '#0f172a', borderRadius: '6px', padding: '2px 2px 2px 0', border: '1px solid #1e293b' }}>
                  <ResponsiveContainer width="100%" height="100%">
                    <ComposedChart data={anSh.slice(-40).map((d, i) => ({ ...d, cng: Math.round((anCh[anCh.length - 40 + i]?.congestion ?? 0) * 100) }))}>
                      <defs>
                        <linearGradient id="spdCmpGrad" x1="0" y1="0" x2="0" y2="1">
                          <stop offset="0%" stopColor="#22c55e" stopOpacity={0.3} />
                          <stop offset="100%" stopColor="#22c55e" stopOpacity={0} />
                        </linearGradient>
                      </defs>
                      <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                      <YAxis yAxisId="l" hide domain={[0, 'dataMax+5']} />
                      <YAxis yAxisId="r" hide orientation="right" domain={[0, 100]} />
                      <Area yAxisId="l" type="monotone" dataKey="speed" stroke="#22c55e" strokeWidth={1.5} fill="url(#spdCmpGrad)" isAnimationActive={false} dot={false} />
                      <Line yAxisId="r" type="monotone" dataKey="cng" stroke="#f97316" strokeWidth={1.5} dot={false} strokeDasharray="4 2" isAnimationActive={false} />
                      <Tooltip
                        contentStyle={{ background: '#1e293b', border: '1px solid #334155', borderRadius: '6px', fontSize: '9px', padding: '4px 8px' }}
                        labelStyle={{ display: 'none' }}
                        formatter={(v, name) => [`${name === 'speed' ? v.toFixed(1) + ' km/h' : v + '%'}`, name === 'speed' ? 'Speed' : 'Congestion']}
                      />
                    </ComposedChart>
                  </ResponsiveContainer>
                </div>
                <div style={{ display: 'flex', justifyContent: 'center', gap: '12px', marginTop: '2px' }}>
                  <span style={{ fontSize: '6px', color: '#22c55e', display: 'flex', alignItems: 'center', gap: '3px' }}><span style={{ width: '10px', height: '2px', background: '#22c55e', display: 'inline-block', borderRadius: '1px' }}/>Speed</span>
                  <span style={{ fontSize: '6px', color: '#f97316', display: 'flex', alignItems: 'center', gap: '3px' }}><span style={{ width: '10px', height: '2px', background: '#f97316', display: 'inline-block', borderRadius: '1px', borderTop: '1px dashed #f97316' }}/>Congestion</span>
                </div>
              </div>
            )}

            {/* ═══ ETA TREND — live countdown ═══ */}
            {etaTrend.length > 2 && (
              <div style={{ marginBottom: '6px' }}>
                <div style={{ fontSize: '8px', color: '#64748b', textTransform: 'uppercase', fontWeight: 600, letterSpacing: '0.5px', marginBottom: '2px', display: 'flex', justifyContent: 'space-between' }}>
                  <span>ETA Trend</span>
                  <span style={{ fontFamily: 'var(--font-mono)', color: '#3b82f6', fontSize: '9px', fontWeight: 700 }}>
                    {etaTrend[etaTrend.length - 1]?.eta?.toFixed(1) ?? '--'} min
                  </span>
                </div>
                <div style={{ height: '55px', backgroundColor: '#0f172a', borderRadius: '6px', padding: '2px', border: '1px solid #1e293b' }}>
                  <ResponsiveContainer width="100%" height="100%">
                    <LineChart data={etaTrend.slice(-40)}>
                      <defs>
                        <linearGradient id="etaLnGrad" x1="0" y1="0" x2="0" y2="1">
                          <stop offset="0%" stopColor="#3b82f6" stopOpacity={0.3} />
                          <stop offset="100%" stopColor="#3b82f6" stopOpacity={0} />
                        </linearGradient>
                      </defs>
                      <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                      <Line type="monotone" dataKey="eta" stroke="#3b82f6" strokeWidth={1.5} dot={false} isAnimationActive={false} />
                      <ReferenceLine y={0} stroke="#475569" strokeDasharray="3 3" />
                    </LineChart>
                  </ResponsiveContainer>
                </div>
              </div>
            )}

            {/* ═══ PERFORMANCE RADIALS ═══ */}
            {performanceData.length > 0 && (
              <div style={{ marginBottom: '6px' }}>
                <div style={{ fontSize: '8px', color: '#64748b', textTransform: 'uppercase', fontWeight: 600, letterSpacing: '0.5px', marginBottom: '2px' }}>Performance Radials</div>
                <div style={{ height: '85px', backgroundColor: '#0f172a', borderRadius: '6px', border: '1px solid #1e293b', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                  <ResponsiveContainer width="100%" height="100%">
                    <RadialBarChart cx="50%" cy="50%" innerRadius="18%" outerRadius="95%" barSize={6} data={performanceData} startAngle={180} endAngle={0}>
                      <RadialBar background={{ fill: '#1e293b' }} dataKey="value" cornerRadius={3} isAnimationActive={false} />
                      <Tooltip
                        contentStyle={{ background: '#1e293b', border: '1px solid #334155', borderRadius: '6px', fontSize: '9px', padding: '4px 8px' }}
                        formatter={(v, name, entry) => [`${v}%`, entry.payload.name]}
                      />
                    </RadialBarChart>
                  </ResponsiveContainer>
                </div>
                <div style={{ display: 'flex', justifyContent: 'center', gap: '10px', marginTop: '2px' }}>
                  {performanceData.map(d => (
                    <span key={d.name} style={{ fontSize: '6px', color: d.fill, display: 'flex', alignItems: 'center', gap: '2px' }}>
                      <span style={{ width: '5px', height: '5px', borderRadius: '50%', background: d.fill, display: 'inline-block' }}/>{d.name} {d.value}%
                    </span>
                  ))}
                </div>
              </div>
            )}

            {/* ═══ SPEED DISTRIBUTION — live histogram ═══ */}
            {speedBins.some(b => b.count > 0) && (
              <div style={{ marginBottom: '6px' }}>
                <div style={{ fontSize: '8px', color: '#64748b', textTransform: 'uppercase', fontWeight: 600, letterSpacing: '0.5px', marginBottom: '2px' }}>Speed Distribution</div>
                <div style={{ height: '60px', backgroundColor: '#0f172a', borderRadius: '6px', padding: '2px 2px 2px 0', border: '1px solid #1e293b' }}>
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={speedBins} barSize={18}>
                      <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                      <XAxis dataKey="range" tick={{ fontSize: 7, fill: '#64748b' }} axisLine={false} tickLine={false} />
                      <Bar dataKey="count" isAnimationActive={false} radius={[2, 2, 0, 0]}>
                        {speedBins.map((b, i) => <Cell key={i} fill={b.fill} />)}
                      </Bar>
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              </div>
            )}

            {/* ═══ SPEED vs CONGESTION SCATTER ═══ */}
            {scatterData.length > 3 && (
              <div style={{ marginBottom: '6px' }}>
                <div style={{ fontSize: '8px', color: '#64748b', textTransform: 'uppercase', fontWeight: 600, letterSpacing: '0.5px', marginBottom: '2px' }}>Speed × Congestion Correlation</div>
                <div style={{ height: '65px', backgroundColor: '#0f172a', borderRadius: '6px', padding: '2px', border: '1px solid #1e293b' }}>
                  <ResponsiveContainer width="100%" height="100%">
                    <ScatterChart>
                      <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                      <XAxis dataKey="speed" type="number" tick={{ fontSize: 7, fill: '#64748b' }} axisLine={false} tickLine={false} name="Speed" unit=" km/h" />
                      <YAxis dataKey="congestion" type="number" tick={{ fontSize: 7, fill: '#64748b' }} axisLine={false} tickLine={false} name="Congestion" unit="%" hide />
                      <Tooltip
                        contentStyle={{ background: '#1e293b', border: '1px solid #334155', borderRadius: '6px', fontSize: '9px', padding: '4px 8px' }}
                        formatter={(v, name) => [`${v}${name === 'Speed' ? ' km/h' : '%'}`, name]}
                      />
                      <Scatter data={scatterData} fill="#8b5cf6" isAnimationActive={false}>
                        {scatterData.map((d, i) => <Cell key={i} fill={d.congestion > 60 ? '#dc2626' : d.congestion > 30 ? '#eab308' : '#22c55e'} />)}
                      </Scatter>
                    </ScatterChart>
                  </ResponsiveContainer>
                </div>
              </div>
            )}

            {/* ═══ ZONE DWELL — pie chart ═══ */}
            {zoneTimePie.length > 0 && (
              <div style={{ marginBottom: '6px' }}>
                <div style={{ fontSize: '8px', color: '#64748b', textTransform: 'uppercase', fontWeight: 600, letterSpacing: '0.5px', marginBottom: '2px' }}>Zone Dwell Time</div>
                <div style={{ height: '80px', backgroundColor: '#0f172a', borderRadius: '6px', border: '1px solid #1e293b', display: 'flex', alignItems: 'center' }}>
                  <ResponsiveContainer width="60%" height="100%">
                    <PieChart>
                      <Pie data={zoneTimePie} cx="50%" cy="50%" innerRadius={14} outerRadius={30} paddingAngle={3} dataKey="value" isAnimationActive={false} strokeWidth={0}>
                        {zoneTimePie.map((z, i) => <Cell key={i} fill={z.fill} />)}
                      </Pie>
                      <Tooltip
                        contentStyle={{ background: '#1e293b', border: '1px solid #334155', borderRadius: '6px', fontSize: '9px', padding: '4px 8px' }}
                        formatter={(v) => [`${v}s`]}
                      />
                    </PieChart>
                  </ResponsiveContainer>
                  <div style={{ width: '40%', paddingRight: '6px' }}>
                    {zoneTimePie.slice(0, 4).map(z => (
                      <div key={z.name} style={{ display: 'flex', alignItems: 'center', gap: '3px', marginBottom: '2px' }}>
                        <span style={{ width: '5px', height: '5px', borderRadius: '50%', background: z.fill, flexShrink: 0 }} />
                        <span style={{ fontSize: '6px', color: '#94a3b8', flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{z.name}</span>
                        <span style={{ fontSize: '7px', fontFamily: 'var(--font-mono)', color: '#e2e8f0', fontWeight: 700 }}>{z.value}s</span>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            )}

            {/* ═══ CONGESTION TEMPORAL BUCKETS ═══ */}
            {congestionBuckets.length > 0 && (
              <div style={{ marginBottom: '6px' }}>
                <div style={{ fontSize: '8px', color: '#64748b', textTransform: 'uppercase', fontWeight: 600, letterSpacing: '0.5px', marginBottom: '2px' }}>Congestion Temporal</div>
                <div style={{ height: '55px', backgroundColor: '#0f172a', borderRadius: '6px', padding: '2px 2px 2px 0', border: '1px solid #1e293b' }}>
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={congestionBuckets} barSize={24}>
                      <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                      <XAxis dataKey="label" tick={{ fontSize: 7, fill: '#64748b' }} axisLine={false} tickLine={false} />
                      <Bar dataKey="congestion" isAnimationActive={false} radius={[3, 3, 0, 0]}>
                        {congestionBuckets.map((b, i) => <Cell key={i} fill={b.fill} />)}
                      </Bar>
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              </div>
            )}

            {/* ═══ LIVE ACCELERATION WAVEFORM ═══ */}
            {anHasData && (
              <div style={{ marginBottom: '6px' }}>
                <div style={{ fontSize: '8px', color: '#64748b', textTransform: 'uppercase', fontWeight: 600, letterSpacing: '0.5px', marginBottom: '2px', display: 'flex', justifyContent: 'space-between' }}>
                  <span>Acceleration Waveform</span>
                  <span style={{ fontFamily: 'var(--font-mono)', color: (convoySimulation.acceleration ?? 0) >= 0 ? '#22c55e' : '#f97316', fontSize: '9px', fontWeight: 700 }}>
                    {(convoySimulation.acceleration ?? 0) >= 0 ? '↑' : '↓'}{Math.abs(convoySimulation.acceleration ?? 0).toFixed(2)}
                  </span>
                </div>
                <div style={{ height: '50px', backgroundColor: '#0f172a', borderRadius: '6px', padding: '2px', border: '1px solid #1e293b' }}>
                  <ResponsiveContainer width="100%" height="100%">
                    <AreaChart data={anSh.slice(-40).map((d, i, arr) => ({ i, accel: i > 0 ? d.speed - arr[i - 1].speed : 0 }))}>
                      <defs>
                        <linearGradient id="accelWaveGrad" x1="0" y1="0" x2="0" y2="1">
                          <stop offset="0%" stopColor="#06b6d4" stopOpacity={0.4} />
                          <stop offset="100%" stopColor="#06b6d4" stopOpacity={0} />
                        </linearGradient>
                      </defs>
                      <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                      <ReferenceLine y={0} stroke="#475569" strokeDasharray="3 3" />
                      <Area type="monotone" dataKey="accel" stroke="#06b6d4" strokeWidth={1.5} fill="url(#accelWaveGrad)" isAnimationActive={false} dot={false} />
                    </AreaChart>
                  </ResponsiveContainer>
                </div>
              </div>
            )}

          </div>
        )}

        {/* ═══ LIVE tab — idle: Corridor Intelligence ═══ */}
        {rpTab === 'live' && !convoySimulation?.active && (
          <div style={{ padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: '10px', flex: 1, overflowY: 'auto' }} className="sp">
            {/* Header */}
            <div className="flex items-center gap-2">
              <span className="live-data-dot" />
              <span style={{ fontSize: '10px', fontWeight: 700, color: '#3b82f6', letterSpacing: '0.08em' }}>CORRIDOR INTELLIGENCE</span>
            </div>

            {/* Corridor Health Gauge + Status */}
            <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
              <div style={{ position: 'relative', width: '90px', height: '60px', flexShrink: 0 }}>
                <svg viewBox="0 0 120 70" width="90" height="60">
                  <path d="M 15 60 A 50 50 0 0 1 105 60" fill="none" stroke="#1e293b" strokeWidth="8" strokeLinecap="round"/>
                  <path d="M 15 60 A 50 50 0 0 1 105 60" fill="none"
                    stroke={corridorHealth.score > 70 ? '#22c55e' : corridorHealth.score > 40 ? '#eab308' : '#dc2626'}
                    strokeWidth="8" strokeLinecap="round"
                    strokeDasharray={`${corridorHealth.score / 100 * 141.4} 141.4`}
                    style={{ filter: `drop-shadow(0 0 3px ${corridorHealth.score > 70 ? 'rgba(34,197,94,0.4)' : corridorHealth.score > 40 ? 'rgba(234,179,8,0.4)' : 'rgba(220,38,38,0.4)'})` }}
                  />
                  <text x="60" y="48" textAnchor="middle" fill={corridorHealth.score > 70 ? '#22c55e' : corridorHealth.score > 40 ? '#eab308' : '#dc2626'} fontSize="18" fontWeight="800" fontFamily="var(--font-mono)">{corridorHealth.score}</text>
                  <text x="60" y="62" textAnchor="middle" fill="#64748b" fontSize="7">HEALTH</text>
                </svg>
              </div>
              <div style={{ flex: 1 }}>
                <div style={{ fontSize: '8px', color: '#64748b', textTransform: 'uppercase', fontWeight: 600, marginBottom: '3px' }}>Corridor Status</div>
                <div style={{ fontSize: '11px', fontWeight: 800, color: summary?.status === 'green' ? '#22c55e' : summary?.status === 'amber' ? '#eab308' : summary?.status === 'red' ? '#dc2626' : '#64748b', textTransform: 'uppercase', fontFamily: 'var(--font-mono)' }}>
                  {summary?.status?.toUpperCase() || 'SYNCING'}
                </div>
                <div style={{ fontSize: '9px', color: '#94a3b8', marginTop: '2px' }}>
                  Flow: <span style={{ color: '#e2e8f0', fontWeight: 700, fontFamily: 'var(--font-mono)' }}>{corridorHealth.flowCapacity}%</span>
                </div>
              </div>
            </div>

            {/* 6-metric vital signs grid */}
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '5px' }}>
              {[
                { label: 'Avg Speed', value: `${(summary?.avg_speed_kmh ?? 0).toFixed(1)}`, unit: 'km/h', color: '#3b82f6' },
                { label: 'CGX Index', value: `${((summary?.avg_congestion_idx ?? 0) * 100).toFixed(0)}`, unit: '%', color: (summary?.avg_congestion_idx ?? 0) > 0.6 ? '#dc2626' : (summary?.avg_congestion_idx ?? 0) > 0.3 ? '#eab308' : '#22c55e' },
                { label: 'Critical', value: `${summary?.critical_segments ?? 0}`, unit: 'segs', color: (summary?.critical_segments ?? 0) > 0 ? '#dc2626' : '#22c55e' },
                { label: 'Monitored', value: `${summary?.total_segments ?? 0}`, unit: 'segs', color: '#a78bfa' },
                { label: 'Convoys', value: `${movements?.length ?? 0}`, unit: 'active', color: '#ea580c' },
                { label: 'Alerts', value: `${alerts?.length ?? 0}`, unit: 'live', color: (alerts?.length ?? 0) > 5 ? '#dc2626' : '#eab308' },
              ].map(m => (
                <div key={m.label} style={{ padding: '6px 4px', backgroundColor: '#0f172a', borderRadius: '6px', border: '1px solid #1e293b', textAlign: 'center' }}>
                  <div style={{ fontSize: '13px', fontWeight: 800, fontFamily: 'var(--font-mono)', color: m.color }}>{m.value}</div>
                  <div style={{ fontSize: '6px', color: '#475569', textTransform: 'uppercase', fontWeight: 600 }}>{m.unit}</div>
                  <div style={{ fontSize: '6px', color: '#64748b', textTransform: 'uppercase', fontWeight: 600, marginTop: '1px' }}>{m.label}</div>
                </div>
              ))}
            </div>

            {/* Network Load Bar */}
            <div>
              <div className="flex justify-between" style={{ marginBottom: '3px' }}>
                <span style={{ fontSize: '8px', color: '#64748b', textTransform: 'uppercase', fontWeight: 600 }}>Network Load</span>
                <span style={{ fontSize: '9px', fontFamily: 'var(--font-mono)', color: corridorHealth.networkLoad > 70 ? '#dc2626' : corridorHealth.networkLoad > 40 ? '#eab308' : '#22c55e', fontWeight: 700 }}>
                  {corridorHealth.networkLoad}%
                </span>
              </div>
              <div style={{ height: '6px', backgroundColor: '#1e293b', borderRadius: '3px', overflow: 'hidden' }}>
                <div style={{
                  width: `${corridorHealth.networkLoad}%`, height: '100%', borderRadius: '3px',
                  background: corridorHealth.networkLoad > 70 ? 'linear-gradient(90deg, #dc2626, #ef4444)' : corridorHealth.networkLoad > 40 ? 'linear-gradient(90deg, #eab308, #facc15)' : 'linear-gradient(90deg, #16a34a, #22c55e)',
                  transition: 'width 0.5s',
                }} />
              </div>
            </div>

            {/* System Readiness */}
            <div>
              <div style={{ fontSize: '8px', color: '#64748b', textTransform: 'uppercase', fontWeight: 600, letterSpacing: '0.5px', marginBottom: '5px' }}>System Readiness</div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                {[
                  { label: 'Qwen 3.5 LLM', ok: backendHealth?.ollama === 'connected', detail: backendHealth?.ollama || 'unknown' },
                  { label: 'Convoy Brain', ok: backendHealth?.status === 'ok' || backendHealth?.status === 'degraded', detail: backendHealth?.status || 'unknown' },
                  { label: 'GPU VRAM', ok: vramPct < 92, detail: gpuHealth ? `${gpuHealth.vramUsedMb}/${gpuHealth.vramTotalMb} MB` : 'N/A' },
                ].map(s => (
                  <div key={s.label} className="flex items-center justify-between" style={{ padding: '4px 8px', backgroundColor: '#0f172a', borderRadius: '5px', border: '1px solid #1e293b' }}>
                    <div className="flex items-center gap-2">
                      <div style={{ width: '6px', height: '6px', borderRadius: '50%', backgroundColor: s.ok ? '#22c55e' : '#dc2626', boxShadow: `0 0 4px ${s.ok ? 'rgba(34,197,94,0.4)' : 'rgba(220,38,38,0.4)'}` }} />
                      <span style={{ fontSize: '9px', color: '#e2e8f0', fontWeight: 600 }}>{s.label}</span>
                    </div>
                    <span style={{ fontSize: '8px', fontFamily: 'var(--font-mono)', color: s.ok ? '#22c55e' : '#dc2626', fontWeight: 700, textTransform: 'uppercase' }}>{s.detail}</span>
                  </div>
                ))}
              </div>
              {gpuHealth && (
                <div style={{ marginTop: '5px' }}>
                  <div className="flex justify-between" style={{ marginBottom: '2px' }}>
                    <span style={{ fontSize: '7px', color: '#475569', fontWeight: 600 }}>VRAM USAGE</span>
                    <span style={{ fontSize: '8px', fontFamily: 'var(--font-mono)', color: vramPct > 92 ? '#dc2626' : vramPct > 80 ? '#eab308' : '#22c55e', fontWeight: 700 }}>{vramPct}%</span>
                  </div>
                  <div style={{ height: '4px', backgroundColor: '#1e293b', borderRadius: '2px', overflow: 'hidden' }}>
                    <div style={{ width: `${vramPct}%`, height: '100%', borderRadius: '2px', background: vramPct > 92 ? '#dc2626' : vramPct > 80 ? '#eab308' : 'linear-gradient(90deg, #16a34a, #22c55e)', transition: 'width 0.5s' }} />
                  </div>
                </div>
              )}
            </div>

            {/* Anomaly Severity Breakdown */}
            {anomalyBreakdown.total > 0 && (
              <div>
                <div style={{ fontSize: '8px', color: '#64748b', textTransform: 'uppercase', fontWeight: 600, letterSpacing: '0.5px', marginBottom: '5px' }}>Anomaly Breakdown</div>
                <div style={{ display: 'flex', gap: '4px', height: '20px', borderRadius: '4px', overflow: 'hidden' }}>
                  {anomalyBreakdown.high > 0 && <div style={{ flex: anomalyBreakdown.high, backgroundColor: '#dc2626', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: '8px', fontWeight: 700, color: '#fff' }}>{anomalyBreakdown.high} HIGH</div>}
                  {anomalyBreakdown.medium > 0 && <div style={{ flex: anomalyBreakdown.medium, backgroundColor: '#ea580c', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: '8px', fontWeight: 700, color: '#fff' }}>{anomalyBreakdown.medium} MED</div>}
                  {anomalyBreakdown.low > 0 && <div style={{ flex: anomalyBreakdown.low, backgroundColor: '#eab308', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: '8px', fontWeight: 700, color: '#1e293b' }}>{anomalyBreakdown.low} LOW</div>}
                </div>
              </div>
            )}

            {/* Security Protocol */}
            <div>
              <div className="flex items-center justify-between" style={{ marginBottom: '5px' }}>
                <span style={{ fontSize: '8px', color: '#64748b', textTransform: 'uppercase', fontWeight: 600, letterSpacing: '0.5px' }}>Security Protocol</span>
                <span style={{ fontSize: '9px', fontWeight: 800, fontFamily: 'var(--font-mono)', color: vvipClass === 'Z+' ? '#dc2626' : vvipClass === 'Z' ? '#ea580c' : vvipClass === 'Y' ? '#3b82f6' : '#64748b' }}>{vvipClass}</span>
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '4px' }}>
                {[
                  { label: 'Min Lanes', value: secProfile.minLanes || '—' },
                  { label: 'Closure', value: secProfile.closure },
                  { label: 'Advance', value: `${secProfile.advance}s` },
                  { label: 'Max Queue', value: `${secProfile.maxQueue}m` },
                ].map(s => (
                  <div key={s.label} style={{ padding: '4px 6px', backgroundColor: '#0f172a', borderRadius: '4px', border: '1px solid #1e293b' }}>
                    <div style={{ fontSize: '6px', color: '#475569', textTransform: 'uppercase', fontWeight: 600 }}>{s.label}</div>
                    <div style={{ fontSize: '10px', fontWeight: 700, fontFamily: 'var(--font-mono)', color: '#e2e8f0' }}>{s.value}</div>
                  </div>
                ))}
              </div>
            </div>

            {/* 24H Speed Pattern */}
            {segmentHistory.length > 0 && (
              <div>
                <div style={{ fontSize: '8px', color: '#64748b', textTransform: 'uppercase', fontWeight: 600, letterSpacing: '0.5px', marginBottom: '4px' }}>24H Speed Pattern</div>
                <div style={{ width: '100%', height: '55px' }}>
                  <ResponsiveContainer width="100%" height="100%">
                    <AreaChart data={segmentHistory} margin={{ top: 2, right: 4, left: 0, bottom: 0 }}>
                      <defs>
                        <linearGradient id="idleSpdGrad" x1="0" y1="0" x2="0" y2="1">
                          <stop offset="0%" stopColor="#3b82f6" stopOpacity={0.3} />
                          <stop offset="100%" stopColor="#3b82f6" stopOpacity={0} />
                        </linearGradient>
                      </defs>
                      <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                      <XAxis dataKey="hour" tick={{ fontSize: 6, fill: '#475569' }} interval={5} />
                      <YAxis tick={{ fontSize: 6, fill: '#475569' }} width={18} />
                      <Tooltip contentStyle={{ fontSize: '8px', backgroundColor: '#1e293b', border: '1px solid #334155', borderRadius: '4px', color: '#e2e8f0' }} />
                      <Area type="monotone" dataKey="speed" stroke="#3b82f6" fill="url(#idleSpdGrad)" strokeWidth={1.5} dot={false} isAnimationActive={false} />
                    </AreaChart>
                  </ResponsiveContainer>
                </div>
              </div>
            )}

            {/* 24H Congestion Pattern */}
            {segmentHistory.length > 0 && (
              <div>
                <div style={{ fontSize: '8px', color: '#64748b', textTransform: 'uppercase', fontWeight: 600, letterSpacing: '0.5px', marginBottom: '4px' }}>24H Congestion Pattern</div>
                <div style={{ width: '100%', height: '55px' }}>
                  <ResponsiveContainer width="100%" height="100%">
                    <AreaChart data={segmentHistory} margin={{ top: 2, right: 4, left: 0, bottom: 0 }}>
                      <defs>
                        <linearGradient id="idleCgGrad" x1="0" y1="0" x2="0" y2="1">
                          <stop offset="0%" stopColor="#ea580c" stopOpacity={0.3} />
                          <stop offset="100%" stopColor="#ea580c" stopOpacity={0} />
                        </linearGradient>
                      </defs>
                      <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                      <XAxis dataKey="hour" tick={{ fontSize: 6, fill: '#475569' }} interval={5} />
                      <YAxis tick={{ fontSize: 6, fill: '#475569' }} width={18} domain={[0, 100]} />
                      <Tooltip contentStyle={{ fontSize: '8px', backgroundColor: '#1e293b', border: '1px solid #334155', borderRadius: '4px', color: '#e2e8f0' }} />
                      <Area type="monotone" dataKey="congestion" stroke="#ea580c" fill="url(#idleCgGrad)" strokeWidth={1.5} dot={false} isAnimationActive={false} />
                    </AreaChart>
                  </ResponsiveContainer>
                </div>
              </div>
            )}

            {/* Standby notice */}
            <div style={{ padding: '6px 10px', backgroundColor: 'rgba(234,88,12,0.06)', borderRadius: '6px', border: '1px solid rgba(234,88,12,0.15)', textAlign: 'center' }}>
              <div style={{ fontSize: '9px', color: '#ea580c', fontWeight: 700 }}>CONVOY STANDBY</div>
              <div style={{ fontSize: '8px', color: '#64748b', marginTop: '2px' }}>Launch convoy for live tracking telemetry</div>
            </div>
          </div>
        )}

        {/* ═══════════════════════════════════════════════════ */}
        {/* ═══ TAB: PREDICT — CoT Reasoning + Future State ═══ */}
        {/* ═══════════════════════════════════════════════════ */}
        {rpTab === 'predict' && (
          <div style={{ padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: '10px' }}>

            {/* ── Live Notification Feed ── */}
            <div>
              <div className="flex items-center gap-2" style={{ marginBottom: '6px' }}>
                <Bell size={11} color="#f59e0b" />
                <span style={{ fontSize: '9px', fontWeight: 700, color: '#f59e0b', letterSpacing: '0.05em', textTransform: 'uppercase' }}>Live Notifications</span>
                <span style={{ marginLeft: 'auto', fontSize: '8px', color: '#64748b', background: 'rgba(245,158,11,0.1)', padding: '1px 6px', borderRadius: '8px' }}>{predictNotifs.length}</span>
              </div>
              <div style={{ maxHeight: '110px', overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: '3px', scrollbarWidth: 'thin' }}>
                {predictNotifs.length === 0 && (
                  <div style={{ fontSize: '8px', color: '#475569', textAlign: 'center', padding: '8px 0' }}>No alerts — corridor nominal</div>
                )}
                {predictNotifs.slice(0, 8).map(n => (
                  <div key={n.id} style={{
                    display: 'flex', alignItems: 'center', gap: '6px', padding: '4px 8px', borderRadius: '5px',
                    background: n.type === 'critical' ? 'rgba(239,68,68,0.08)' : n.type === 'warning' ? 'rgba(245,158,11,0.08)' : 'rgba(59,130,246,0.06)',
                    border: `1px solid ${n.type === 'critical' ? 'rgba(239,68,68,0.2)' : n.type === 'warning' ? 'rgba(245,158,11,0.15)' : 'rgba(59,130,246,0.12)'}`,
                  }}>
                    <span style={{ fontSize: '10px' }}>{n.icon}</span>
                    <span style={{ fontSize: '8px', color: '#cbd5e1', flex: 1 }}>{n.text}</span>
                    <span style={{ fontSize: '7px', color: '#475569' }}>{Math.round((Date.now() - n.ts) / 1000)}s</span>
                  </div>
                ))}
              </div>
            </div>

            <div style={{ height: '1px', background: 'rgba(148,163,184,0.08)' }} />

            {/* ── Ground Data Parameters ── */}
            <div>
              <div className="flex items-center gap-2" style={{ marginBottom: '5px' }}>
                <Layers size={11} color="#8b5cf6" />
                <span style={{ fontSize: '9px', fontWeight: 700, color: '#8b5cf6', letterSpacing: '0.05em', textTransform: 'uppercase' }}>Ground Data Features</span>
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '4px' }}>
                {[
                  { label: 'Crowd', value: `${groundData.crowdDensity}/km²`, color: groundData.crowdDensity > 1800 ? '#ef4444' : groundData.crowdDensity > 1000 ? '#f59e0b' : '#22c55e' },
                  { label: 'Signal', value: ['G', 'Y', 'R', 'P'][groundData.signalPhase], color: ['#22c55e', '#f59e0b', '#ef4444', '#3b82f6'][groundData.signalPhase] },
                  { label: 'Weather', value: `${groundData.weatherRisk}%`, color: groundData.weatherRisk > 30 ? '#ef4444' : groundData.weatherRisk > 15 ? '#f59e0b' : '#22c55e' },
                  { label: 'Incident', value: `${groundData.incidentProb}%`, color: groundData.incidentProb > 50 ? '#ef4444' : groundData.incidentProb > 25 ? '#f59e0b' : '#22c55e' },
                  { label: 'Lanes', value: groundData.lanes, color: '#94a3b8' },
                  { label: 'Rd Score', value: groundData.roadClassScore, color: groundData.roadClassScore >= 70 ? '#22c55e' : groundData.roadClassScore >= 50 ? '#f59e0b' : '#ef4444' },
                  { label: 'SpdΔ', value: `${groundData.speedDelta > 0 ? '+' : ''}${groundData.speedDelta}`, color: groundData.speedDelta < -3 ? '#ef4444' : groundData.speedDelta > 3 ? '#22c55e' : '#94a3b8' },
                  { label: 'NbrRatio', value: groundData.neighborSpeedRatio, color: groundData.neighborSpeedRatio < 0.7 ? '#ef4444' : '#94a3b8' },
                  { label: 'H·sin', value: groundData.hourSin, color: '#64748b' },
                ].map((p, i) => (
                  <div key={i} style={{ background: 'rgba(30,41,59,0.5)', borderRadius: '5px', padding: '4px 6px', border: '1px solid rgba(148,163,184,0.06)', textAlign: 'center' }}>
                    <div style={{ fontSize: '7px', color: '#64748b', textTransform: 'uppercase', letterSpacing: '0.04em' }}>{p.label}</div>
                    <div style={{ fontSize: '11px', fontWeight: 700, color: p.color, fontFamily: 'JetBrains Mono, monospace' }}>{p.value}</div>
                  </div>
                ))}
              </div>
            </div>

            <div style={{ height: '1px', background: 'rgba(148,163,184,0.08)' }} />

            {/* ── Present Recommendations — Chain-of-Thought ── */}
            <div>
              <div className="flex items-center gap-2" style={{ marginBottom: '5px' }}>
                <Lightbulb size={11} color="#22c55e" />
                <span style={{ fontSize: '9px', fontWeight: 700, color: '#22c55e', letterSpacing: '0.05em', textTransform: 'uppercase' }}>Present Recommendations</span>
                <span style={{ marginLeft: 'auto', fontSize: '7px', color: '#475569', fontStyle: 'italic' }}>chain-of-thought</span>
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '3px' }}>
                {cotSteps.map((st, i) => (
                  <div key={i} style={{
                    padding: '5px 8px', borderRadius: '5px',
                    background: st.status === 'fail' ? 'rgba(239,68,68,0.06)' : st.status === 'warn' ? 'rgba(245,158,11,0.06)' : 'rgba(34,197,94,0.05)',
                    borderLeft: `2px solid ${st.status === 'fail' ? '#ef4444' : st.status === 'warn' ? '#f59e0b' : '#22c55e'}`,
                  }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '4px', marginBottom: '2px' }}>
                      <span style={{ fontSize: '8px', fontWeight: 700, color: st.status === 'fail' ? '#ef4444' : st.status === 'warn' ? '#f59e0b' : '#22c55e' }}>
                        {st.step === cotSteps.length ? '⚡' : `${st.step}.`}
                      </span>
                      <span style={{ fontSize: '8px', fontWeight: 600, color: '#e2e8f0' }}>{st.label}</span>
                      {st.status === 'pass' && <span style={{ marginLeft: 'auto', fontSize: '7px', color: '#22c55e' }}>✓</span>}
                      {st.status === 'warn' && <span style={{ marginLeft: 'auto', fontSize: '7px', color: '#f59e0b' }}>⚠</span>}
                      {st.status === 'fail' && <span style={{ marginLeft: 'auto', fontSize: '7px', color: '#ef4444' }}>✗</span>}
                    </div>
                    <div style={{ fontSize: '7.5px', color: '#94a3b8', lineHeight: '1.4', paddingLeft: '12px' }}>{st.thought}</div>
                  </div>
                ))}
              </div>
            </div>

            <div style={{ height: '1px', background: 'rgba(148,163,184,0.08)' }} />

            {/* ── Forecast Chart — Speed & Congestion Curves ── */}
            <div>
              <div className="flex items-center gap-2" style={{ marginBottom: '5px' }}>
                <TrendingUp size={11} color="#3b82f6" />
                <span style={{ fontSize: '9px', fontWeight: 700, color: '#3b82f6', letterSpacing: '0.05em', textTransform: 'uppercase' }}>Forecast Curves</span>
              </div>
              <div style={{ width: '100%', height: 110 }}>
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={forecastChartData} margin={{ top: 5, right: 5, bottom: 0, left: -15 }}>
                    <defs>
                      <linearGradient id="fcCong" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="5%" stopColor="#ef4444" stopOpacity={0.3} />
                        <stop offset="95%" stopColor="#ef4444" stopOpacity={0} />
                      </linearGradient>
                      <linearGradient id="fcSpd" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="5%" stopColor="#22c55e" stopOpacity={0.3} />
                        <stop offset="95%" stopColor="#22c55e" stopOpacity={0} />
                      </linearGradient>
                    </defs>
                    <XAxis dataKey="t" tick={{ fontSize: 8, fill: '#64748b' }} axisLine={false} tickLine={false} />
                    <YAxis tick={{ fontSize: 7, fill: '#475569' }} axisLine={false} tickLine={false} domain={[0, 'auto']} />
                    <Tooltip contentStyle={{ background: '#0f172a', border: '1px solid rgba(148,163,184,0.15)', borderRadius: '6px', fontSize: '9px', color: '#e2e8f0' }} />
                    <Area type="monotone" dataKey="congestion" stroke="#ef4444" fill="url(#fcCong)" strokeWidth={1.5} name="CGX %" dot={{ r: 2, fill: '#ef4444' }} />
                    <Area type="monotone" dataKey="speed" stroke="#22c55e" fill="url(#fcSpd)" strokeWidth={1.5} name="Speed km/h" dot={{ r: 2, fill: '#22c55e' }} />
                  </AreaChart>
                </ResponsiveContainer>
              </div>
            </div>

            <div style={{ height: '1px', background: 'rgba(148,163,184,0.08)' }} />

            {/* ── Future Recommendations — Chain-of-Thought ── */}
            <div>
              <div className="flex items-center gap-2" style={{ marginBottom: '5px' }}>
                <ArrowUpRight size={11} color="#a78bfa" />
                <span style={{ fontSize: '9px', fontWeight: 700, color: '#a78bfa', letterSpacing: '0.05em', textTransform: 'uppercase' }}>Future Predictions</span>
                <span style={{ marginLeft: 'auto', fontSize: '7px', color: '#475569', fontStyle: 'italic' }}>DSTGAT + Qwen 3.5</span>
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '3px' }}>
                {futureCotSteps.map((st, i) => (
                  <div key={i} style={{
                    padding: '5px 8px', borderRadius: '5px',
                    background: st.status === 'fail' ? 'rgba(239,68,68,0.06)' : st.status === 'warn' ? 'rgba(245,158,11,0.06)' : 'rgba(139,92,246,0.05)',
                    borderLeft: `2px solid ${st.status === 'fail' ? '#ef4444' : st.status === 'warn' ? '#f59e0b' : '#a78bfa'}`,
                  }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '4px', marginBottom: '2px' }}>
                      {st.horizon && (
                        <span style={{
                          fontSize: '7px', fontWeight: 700, color: '#0f172a', padding: '1px 4px', borderRadius: '3px',
                          background: st.status === 'fail' ? '#ef4444' : st.status === 'warn' ? '#f59e0b' : '#a78bfa',
                        }}>{st.horizon}</span>
                      )}
                      <span style={{ fontSize: '8px', fontWeight: 600, color: '#e2e8f0' }}>{st.label}</span>
                      {st.metrics && (
                        <span style={{ marginLeft: 'auto', fontSize: '7px', color: '#64748b', fontFamily: 'JetBrains Mono, monospace' }}>
                          C:{st.metrics.congestion}% S:{st.metrics.speed}
                        </span>
                      )}
                    </div>
                    <div style={{ fontSize: '7.5px', color: '#94a3b8', lineHeight: '1.4', paddingLeft: st.horizon ? '28px' : '12px' }}>{st.thought}</div>
                  </div>
                ))}
              </div>
            </div>

            {/* ── Model Status ── */}
            <div style={{
              display: 'flex', alignItems: 'center', gap: '6px', padding: '5px 8px', borderRadius: '5px',
              background: 'rgba(59,130,246,0.05)', border: '1px solid rgba(59,130,246,0.1)',
            }}>
              <div style={{ width: 5, height: 5, borderRadius: '50%', background: '#22c55e', boxShadow: '0 0 4px rgba(34,197,94,0.6)' }} />
              <span style={{ fontSize: '8px', color: '#94a3b8' }}>Qwen 3.5 9B (Q4_K_M)</span>
              <span style={{ fontSize: '7px', color: '#475569', marginLeft: 'auto' }}>no-think · DSTGAT fused</span>
            </div>
          </div>
        )}

        {/* ═══════════════════════════════════════════════════ */}
        {/* ═══ TAB: ANALYTICS — Charts & Analysis ═══ */}
        {/* ═══════════════════════════════════════════════════ */}
        {rpTab === 'analytics' && (
          <div style={{ padding: '8px 12px', display: 'flex', flexDirection: 'column', gap: '12px' }}>
            {!anHasData && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
                {/* Corridor Analytics Header */}
                <div className="flex items-center gap-2">
                  <BarChart3 size={12} color="#3b82f6" />
                  <span style={{ fontSize: '9px', fontWeight: 700, color: '#3b82f6', letterSpacing: '0.05em', textTransform: 'uppercase' }}>Corridor Analytics</span>
                </div>

                {/* Corridor Quick Metrics */}
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '5px' }}>
                  {[
                    { label: 'Speed Quality', value: `${corridorHealth.flowCapacity}%`, color: corridorHealth.flowCapacity > 60 ? '#22c55e' : '#eab308', desc: 'of max flow' },
                    { label: 'Network Load', value: `${corridorHealth.networkLoad}%`, color: corridorHealth.networkLoad > 70 ? '#dc2626' : '#22c55e', desc: 'congestion' },
                    { label: 'Critical Ratio', value: `${(corridorHealth.critRatio * 100).toFixed(1)}%`, color: corridorHealth.critRatio > 0.1 ? '#dc2626' : '#22c55e', desc: 'of segments' },
                    { label: 'Avg Speed', value: `${corridorHealth.spd.toFixed(1)}`, color: '#3b82f6', desc: 'km/h avg' },
                  ].map(m => (
                    <div key={m.label} style={{ padding: '8px 6px', backgroundColor: '#0f172a', borderRadius: '6px', border: '1px solid #1e293b', textAlign: 'center' }}>
                      <div style={{ fontSize: '14px', fontWeight: 800, fontFamily: 'var(--font-mono)', color: m.color }}>{m.value}</div>
                      <div style={{ fontSize: '7px', color: '#475569', textTransform: 'uppercase', fontWeight: 600, marginTop: '1px' }}>{m.desc}</div>
                      <div style={{ fontSize: '6px', color: '#64748b', textTransform: 'uppercase', fontWeight: 600, marginTop: '1px' }}>{m.label}</div>
                    </div>
                  ))}
                </div>

                {/* Speed Distribution from history */}
                {segmentHistory.length > 0 && (
                  <div>
                    <div style={{ fontSize: '9px', color: '#94a3b8', textTransform: 'uppercase', fontWeight: 700, marginBottom: '4px' }}>Historical Speed Distribution</div>
                    <div style={{ width: '100%', height: '80px' }}>
                      <ResponsiveContainer width="100%" height="100%">
                        <BarChart data={segmentHistory.map(s => ({ ...s, fill: s.speed > 40 ? '#22c55e' : s.speed > 25 ? '#eab308' : '#dc2626' }))} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
                          <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" vertical={false} />
                          <XAxis dataKey="hour" tick={{ fontSize: 6, fill: '#475569' }} interval={3} />
                          <YAxis tick={{ fontSize: 7, fill: '#64748b' }} width={18} />
                          <Tooltip contentStyle={{ fontSize: '8px', backgroundColor: '#1e293b', border: '1px solid #334155', borderRadius: '4px', color: '#e2e8f0' }} />
                          <Bar dataKey="speed" radius={[2, 2, 0, 0]}>
                            {segmentHistory.map((s, i) => <Cell key={i} fill={s.speed > 40 ? '#22c55e' : s.speed > 25 ? '#eab308' : '#dc2626'} />)}
                          </Bar>
                        </BarChart>
                      </ResponsiveContainer>
                    </div>
                  </div>
                )}

                {/* Speed vs Congestion Scatter */}
                {segmentHistory.length > 3 && (
                  <div>
                    <div style={{ fontSize: '9px', color: '#94a3b8', textTransform: 'uppercase', fontWeight: 700, marginBottom: '4px' }}>Speed vs Congestion (24H)</div>
                    <div style={{ width: '100%', height: '90px' }}>
                      <ResponsiveContainer width="100%" height="100%">
                        <ScatterChart margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
                          <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                          <XAxis type="number" dataKey="speed" tick={{ fontSize: 7, fill: '#64748b' }} name="Speed" />
                          <YAxis type="number" dataKey="congestion" tick={{ fontSize: 7, fill: '#64748b' }} width={22} name="Congest%" />
                          <Tooltip contentStyle={{ fontSize: '8px', backgroundColor: '#1e293b', border: '1px solid #334155', borderRadius: '4px', color: '#e2e8f0' }} />
                          <Scatter data={segmentHistory} fill="#ea580c" fillOpacity={0.6} />
                        </ScatterChart>
                      </ResponsiveContainer>
                    </div>
                  </div>
                )}

                {/* Anomaly Type Distribution */}
                {alerts?.length > 0 && (() => {
                  const typeCount = {};
                  alerts.forEach(a => { typeCount[a.anomaly_type || 'unknown'] = (typeCount[a.anomaly_type || 'unknown'] || 0) + 1; });
                  const typeData = Object.entries(typeCount).slice(0, 6).map(([name, count]) => ({ name: name.replace(/_/g, ' ').slice(0, 12), count }));
                  const typeColors = ['#dc2626', '#ea580c', '#eab308', '#3b82f6', '#8b5cf6', '#16a34a'];
                  return (
                    <div>
                      <div style={{ fontSize: '9px', color: '#94a3b8', textTransform: 'uppercase', fontWeight: 700, marginBottom: '4px' }}>Anomaly Type Distribution</div>
                      <div style={{ width: '100%', height: '80px' }}>
                        <ResponsiveContainer width="100%" height="100%">
                          <BarChart data={typeData} layout="vertical" margin={{ top: 2, right: 4, left: 0, bottom: 0 }}>
                            <XAxis type="number" tick={{ fontSize: 7, fill: '#64748b' }} />
                            <YAxis type="category" dataKey="name" tick={{ fontSize: 6, fill: '#94a3b8' }} width={55} />
                            <Bar dataKey="count" radius={[0, 3, 3, 0]}>
                              {typeData.map((_, i) => <Cell key={i} fill={typeColors[i % typeColors.length]} />)}
                            </Bar>
                          </BarChart>
                        </ResponsiveContainer>
                      </div>
                    </div>
                  );
                })()}

                {/* Launch notice */}
                <div style={{ padding: '6px 10px', backgroundColor: 'rgba(59,130,246,0.06)', borderRadius: '6px', border: '1px solid rgba(59,130,246,0.15)', textAlign: 'center' }}>
                  <div style={{ fontSize: '8px', color: '#3b82f6', fontWeight: 600 }}>Launch convoy for real-time telemetry analytics</div>
                </div>
              </div>
            )}

            {/* ═══ Speed + Congestion Overlay ═══ */}
            {anHasData && (
              <div>
                <div className="flex items-center justify-between" style={{ marginBottom: '4px' }}>
                  <span style={{ fontSize: '9px', color: '#94a3b8', textTransform: 'uppercase', fontWeight: 700 }}>Speed + Congestion</span>
                  <div style={{ display: 'flex', gap: '8px' }}>
                    <span style={{ fontSize: '7px', color: '#ea580c' }}>● Speed</span>
                    <span style={{ fontSize: '7px', color: '#dc2626' }}>● Congest</span>
                  </div>
                </div>
                <div style={{ width: '100%', height: '110px' }}>
                  <ResponsiveContainer width="100%" height="100%">
                    <ComposedChart data={anSh.map((d, i) => ({
                      speed: d.speed,
                      congestion: (anCh[i]?.congestion ?? 0) * 60,
                    }))} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
                      <defs>
                        <linearGradient id="spdGradAn" x1="0" y1="0" x2="0" y2="1">
                          <stop offset="0%" stopColor="#ea580c" stopOpacity={0.35} />
                          <stop offset="100%" stopColor="#ea580c" stopOpacity={0} />
                        </linearGradient>
                      </defs>
                      <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                      <XAxis dataKey="i" hide />
                      <YAxis domain={[0, 60]} tick={{ fontSize: 7, fill: '#64748b' }} width={22} />
                      <Tooltip contentStyle={{ fontSize: '9px', backgroundColor: '#1e293b', border: '1px solid #334155', borderRadius: '6px', color: '#e2e8f0' }} />
                      <Area type="monotone" dataKey="speed" stroke="#ea580c" fill="url(#spdGradAn)" strokeWidth={1.5} dot={false} isAnimationActive={false} />
                      <Line type="monotone" dataKey="congestion" stroke="#dc2626" strokeWidth={1} dot={false} isAnimationActive={false} strokeDasharray="4 2" />
                    </ComposedChart>
                  </ResponsiveContainer>
                </div>
              </div>
            )}

            {/* ═══ Acceleration Timeline ═══ */}
            {anHasData && (
              <div>
                <div className="flex items-center justify-between" style={{ marginBottom: '4px' }}>
                  <span style={{ fontSize: '9px', color: '#94a3b8', textTransform: 'uppercase', fontWeight: 700 }}>Acceleration (Δv)</span>
                  <span style={{ fontSize: '8px', fontFamily: 'var(--font-mono)', color: (convoySimulation.acceleration ?? 0) >= 0 ? '#22c55e' : '#dc2626', fontWeight: 700 }}>
                    {(convoySimulation.acceleration ?? 0) > 0 ? '+' : ''}{(convoySimulation.acceleration ?? 0).toFixed(1)}
                  </span>
                </div>
                <div style={{ width: '100%', height: '70px' }}>
                  <ResponsiveContainer width="100%" height="100%">
                    <AreaChart data={anSh.slice(1).map((d, i) => ({
                      i, v: Number((d.speed - anSh[i].speed).toFixed(2))
                    }))} margin={{ top: 2, right: 4, left: 0, bottom: 0 }}>
                      <defs>
                        <linearGradient id="accelGradAn" x1="0" y1="0" x2="0" y2="1">
                          <stop offset="5%" stopColor="#f97316" stopOpacity={0.3} />
                          <stop offset="95%" stopColor="#f97316" stopOpacity={0} />
                        </linearGradient>
                      </defs>
                      <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                      <XAxis dataKey="i" hide />
                      <YAxis tick={{ fontSize: 7, fill: '#64748b' }} width={22} />
                      <ReferenceLine y={0} stroke="#475569" strokeWidth={1} />
                      <Tooltip contentStyle={{ fontSize: '9px', backgroundColor: '#1e293b', border: '1px solid #334155', borderRadius: '6px', color: '#e2e8f0' }} />
                      <Area type="monotone" dataKey="v" stroke="#f97316" fill="url(#accelGradAn)" strokeWidth={1.5} dot={false} isAnimationActive={false} />
                    </AreaChart>
                  </ResponsiveContainer>
                </div>
              </div>
            )}

            {/* ═══ Speed Distribution Histogram (fine bins) ═══ */}
            {speedBins.length > 0 && (
              <div>
                <div style={{ fontSize: '9px', color: '#94a3b8', textTransform: 'uppercase', fontWeight: 700, marginBottom: '4px' }}>Speed Distribution (km/h)</div>
                <div style={{ width: '100%', height: '80px' }}>
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={speedBins} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
                      <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" vertical={false} />
                      <XAxis dataKey="range" tick={{ fontSize: 7, fill: '#94a3b8' }} />
                      <YAxis tick={{ fontSize: 7, fill: '#64748b' }} width={18} />
                      <Bar dataKey="count" radius={[3, 3, 0, 0]}>
                        {speedBins.map((b, i) => <Cell key={i} fill={b.fill} />)}
                      </Bar>
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              </div>
            )}

            {/* ═══ Speed vs Congestion Scatter ═══ */}
            {scatterData.length > 0 && (
              <div>
                <div style={{ fontSize: '9px', color: '#94a3b8', textTransform: 'uppercase', fontWeight: 700, marginBottom: '4px' }}>Speed vs Congestion Correlation</div>
                <div style={{ width: '100%', height: '100px' }}>
                  <ResponsiveContainer width="100%" height="100%">
                    <ScatterChart margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
                      <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                      <XAxis type="number" dataKey="speed" name="Speed" tick={{ fontSize: 7, fill: '#64748b' }} label={{ value: 'km/h', position: 'insideBottomRight', fontSize: 7, fill: '#475569', offset: -2 }} />
                      <YAxis type="number" dataKey="congestion" name="Congest%" tick={{ fontSize: 7, fill: '#64748b' }} width={22} />
                      <Tooltip contentStyle={{ fontSize: '9px', backgroundColor: '#1e293b', border: '1px solid #334155', borderRadius: '6px', color: '#e2e8f0' }} />
                      <Scatter data={scatterData} fill="#ea580c" fillOpacity={0.6} r={3} />
                    </ScatterChart>
                  </ResponsiveContainer>
                </div>
              </div>
            )}

            {/* ═══ Zone Time Distribution Pie ═══ */}
            {zoneTimePie.length > 0 && (
              <div>
                <div style={{ fontSize: '9px', color: '#94a3b8', textTransform: 'uppercase', fontWeight: 700, marginBottom: '4px' }}>Zone Time Distribution</div>
                <div style={{ width: '100%', height: '120px' }}>
                  <ResponsiveContainer width="100%" height="100%">
                    <PieChart>
                      <Pie data={zoneTimePie} cx="50%" cy="50%" innerRadius={25} outerRadius={45} dataKey="value" nameKey="name" label={({ name, percent }) => `${name} ${(percent * 100).toFixed(0)}%`} labelLine={false} style={{ fontSize: '7px' }}>
                        {zoneTimePie.map((entry, i) => <Cell key={i} fill={entry.fill} />)}
                      </Pie>
                      <Tooltip contentStyle={{ fontSize: '9px', backgroundColor: '#1e293b', border: '1px solid #334155', borderRadius: '6px', color: '#e2e8f0' }} formatter={(val) => `${val}s`} />
                    </PieChart>
                  </ResponsiveContainer>
                </div>
              </div>
            )}

            {/* ═══ Congestion Heatmap Buckets ═══ */}
            {congestionBuckets.length > 0 && (
              <div>
                <div style={{ fontSize: '9px', color: '#94a3b8', textTransform: 'uppercase', fontWeight: 700, marginBottom: '4px' }}>Congestion Heatmap (Time Buckets)</div>
                <div style={{ width: '100%', height: '65px' }}>
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={congestionBuckets} margin={{ top: 2, right: 4, left: 0, bottom: 0 }}>
                      <XAxis dataKey="label" tick={{ fontSize: 7, fill: '#94a3b8' }} />
                      <YAxis domain={[0, 100]} tick={{ fontSize: 7, fill: '#64748b' }} width={18} />
                      <Bar dataKey="congestion" radius={[3, 3, 0, 0]}>
                        {congestionBuckets.map((b, i) => <Cell key={i} fill={b.fill} />)}
                      </Bar>
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              </div>
            )}

            {/* ═══ ETA Trend ═══ */}
            {etaTrend.length > 0 && (
              <div>
                <div style={{ fontSize: '9px', color: '#94a3b8', textTransform: 'uppercase', fontWeight: 700, marginBottom: '4px' }}>ETA Trend (minutes)</div>
                <div style={{ width: '100%', height: '65px' }}>
                  <ResponsiveContainer width="100%" height="100%">
                    <AreaChart data={etaTrend} margin={{ top: 2, right: 4, left: 0, bottom: 0 }}>
                      <defs>
                        <linearGradient id="etaGradAn" x1="0" y1="0" x2="0" y2="1">
                          <stop offset="0%" stopColor="#3b82f6" stopOpacity={0.3} />
                          <stop offset="100%" stopColor="#3b82f6" stopOpacity={0} />
                        </linearGradient>
                      </defs>
                      <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                      <XAxis dataKey="i" hide />
                      <YAxis tick={{ fontSize: 7, fill: '#64748b' }} width={20} />
                      <Area type="monotone" dataKey="eta" stroke="#3b82f6" fill="url(#etaGradAn)" strokeWidth={1.5} dot={false} isAnimationActive={false} />
                    </AreaChart>
                  </ResponsiveContainer>
                </div>
              </div>
            )}

            {/* ═══ Performance Radial Bar ═══ */}
            {performanceData.length > 0 && (
              <div>
                <div style={{ fontSize: '9px', color: '#94a3b8', textTransform: 'uppercase', fontWeight: 700, marginBottom: '4px' }}>Performance Metrics</div>
                <div style={{ width: '100%', height: '130px' }}>
                  <ResponsiveContainer width="100%" height="100%">
                    <RadialBarChart cx="50%" cy="50%" innerRadius="20%" outerRadius="90%" data={performanceData} startAngle={180} endAngle={0}>
                      <RadialBar minAngle={15} background clockWise dataKey="value" cornerRadius={4} />
                      <Legend iconSize={6} wrapperStyle={{ fontSize: '8px', color: '#94a3b8', bottom: 0 }} />
                      <Tooltip contentStyle={{ fontSize: '9px', backgroundColor: '#1e293b', border: '1px solid #334155', borderRadius: '6px', color: '#e2e8f0' }} />
                    </RadialBarChart>
                  </ResponsiveContainer>
                </div>
              </div>
            )}
          </div>
        )}

        {/* ═══════════════════════════════════════════════════ */}
        {/* ═══ TAB: CORRIDOR — Trends & Movements ═══ */}
        {/* ═══════════════════════════════════════════════════ */}
        {rpTab === 'corridor' && (
          <>
        {/* Corridor Trend Sparkline */}
        {segmentHistory.length > 0 && (
          <div style={{ padding: '0 12px 12px', marginBottom: '8px' }}>
            <div className="flex items-center justify-between" style={{ marginBottom: '6px' }}>
              <span style={{ fontSize: '10px', fontWeight: 700, color: '#94a3b8', letterSpacing: '0.05em' }}>CORRIDOR 24H TREND</span>
              {summary?.avg_congestion_idx != null && (
                <span style={{ fontSize: '9px', fontFamily: 'var(--font-mono)', color: summary.avg_congestion_idx > 0.5 ? '#dc2626' : '#16a34a', fontWeight: 700 }}>
                  CGX {(summary.avg_congestion_idx).toFixed(3)}
                </span>
              )}
            </div>
            <div style={{ width: '100%', height: '60px' }}>
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={segmentHistory} margin={{ top: 0, right: 0, left: 0, bottom: 0 }}>
                  <defs>
                    <linearGradient id="cgGrad" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="#ea580c" stopOpacity={0.3} />
                      <stop offset="100%" stopColor="#ea580c" stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <XAxis dataKey="hour" tick={{ fontSize: 7, fill: '#94a3b8' }} interval={3} />
                  <Area type="monotone" dataKey="congestion" stroke="#ea580c" fill="url(#cgGrad)" strokeWidth={1.5} dot={false} />
                  <Tooltip contentStyle={{ fontSize: '9px', padding: '3px 6px', borderRadius: '4px', backgroundColor: '#1e293b', border: '1px solid #334155', color: '#e2e8f0' }} />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          </div>
        )}

        {/* Active Convoys */}
        <div style={{ marginBottom: '12px' }}>
          <SHdr title="Active Convoys" badge={lifecycle !== 'idle' ? lifecycle.toUpperCase() : 'LIVE'} />
          <div style={{ padding: '8px 12px' }}>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
              {movements && movements.map((mov, idx) => {
                const isPrimary = idx === 0;
                const eta = etaMap[mov.movement_id];
                const etaMin = eta?.eta_seconds ? Math.round(eta.eta_seconds / 60) : null;
                return (
                  <div key={mov.movement_id || idx} onClick={() => { if (mov.segment_ids?.length) flyToSegment(mov.segment_ids[0]); }} style={{ 
                    padding: '10px 12px', border: '1px solid #334155', 
                    borderLeft: `3px solid ${isPrimary ? '#ea580c' : '#2563eb'}`, borderRadius: '10px', backgroundColor: '#1e293b',
                    transition: 'all 0.2s ease', boxShadow: '0 1px 4px rgba(0,0,0,0.15)', cursor: 'pointer',
                  }}>
                    <div className="flex justify-between items-center" style={{ marginBottom: '6px' }}>
                      <span style={{ fontSize: '10px', fontWeight: 700, fontFamily: 'var(--font-mono)', color: '#e2e8f0' }}>{mov.movement_id?.slice(0, 8) || `VVIP-${idx}`}</span>
                      <span className={`badge ${mov.status === 'active' ? 'badge-orange' : mov.status === 'completed' ? 'badge-green' : 'badge-blue'}`} style={{ border: 'none', fontSize: '8px' }}>{mov.status}</span>
                    </div>
                    <div style={{ fontSize: '9px', color: '#94a3b8', display: 'flex', alignItems: 'center', gap: '4px', marginBottom: '6px' }}>
                      <IconRouteArrow size={10} color={isPrimary ? '#ea580c' : '#2563eb'} />
                      {mov.vvip_class} Class
                      <IconArrowRight size={10} color="#94a3b8" />
                      {mov.segment_ids?.length || '?'} segments
                    </div>
                    <div className="flex justify-between items-center">
                      <span style={{ fontSize: '9px', color: '#94a3b8' }}>
                        {mov.planned_start ? new Date(mov.planned_start).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '—'}
                      </span>
                      <span style={{ fontSize: '10px', fontWeight: 700, fontFamily: 'var(--font-mono)', color: isPrimary ? '#ea580c' : '#2563eb' }}>
                        {etaMin ? `ETA ${etaMin} min` : 'ETA --'}
                      </span>
                    </div>
                  </div>
                );
              })}
              {(!movements || movements.length === 0) && (
                <div style={{ textAlign: 'center', padding: '16px', fontSize: '11px', color: '#94a3b8' }}>No active movements.</div>
              )}
            </div>
          </div>
        </div>

        {/* TCP / Diversion Checkpoints */}
        <div style={{ marginBottom: '12px' }}>
          <SHdr title="Diversion Checkpoints" badge={tcps.length ? `${tcps.length} TCP` : ''} />
          <div style={{ padding: '0 12px 10px' }}>
            {tcps.length > 0 ? (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '5px' }}>
                {tcps.map(tcp => (
                  <div key={tcp.name} onClick={() => flyToSegment(tcp.segmentId)} className="flex items-center justify-between" style={{ padding: '8px 10px', backgroundColor: '#1e293b', backdropFilter: 'blur(4px)', borderRadius: '8px', border: '1px solid #334155', transition: 'all 0.2s ease', cursor: 'pointer' }}>
                    <div>
                      <div style={{ fontSize: '10px', fontWeight: 700, color: '#e2e8f0' }}>{tcp.name} <span style={{ fontWeight: 400, color: '#94a3b8' }}>seg:{tcp.segmentId}</span></div>
                      <div style={{ fontSize: '8px', color: '#94a3b8' }}>{tcp.agency} · {tcp.timing}</div>
                    </div>
                    <span className={`badge ${tcp.status === 'ACTIVE' ? 'badge-green' : tcp.status === 'HOLD' ? 'badge-orange' : 'badge-blue'}`} style={{ border: 'none', fontSize: '7px' }}>{tcp.status}</span>
                  </div>
                ))}
              </div>
            ) : (
              <div style={{ textAlign: 'center', padding: '12px', fontSize: '10px', color: '#94a3b8' }}>No diversions planned yet.</div>
            )}
          </div>
        </div>

        {/* ═══ Network Vitals ═══ */}
        <div style={{ marginBottom: '12px' }}>
          <SHdr title="Network Vitals" badge="LIVE" />
          <div style={{ padding: '0 12px 10px' }}>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '5px' }}>
              {[
                { label: 'Throughput', value: `${(summary?.avg_speed_kmh ?? 0).toFixed(0)}`, unit: 'km/h', color: '#3b82f6' },
                { label: 'Load', value: `${((summary?.avg_congestion_idx ?? 0) * 100).toFixed(0)}`, unit: '%', color: (summary?.avg_congestion_idx ?? 0) > 0.6 ? '#dc2626' : '#22c55e' },
                { label: 'Critical', value: `${summary?.critical_segments ?? 0}`, unit: 'segs', color: '#dc2626' },
                { label: 'Monitored', value: `${summary?.total_segments ?? 0}`, unit: 'total', color: '#a78bfa' },
                { label: 'Healthy', value: `${Math.max(0, (summary?.total_segments ?? 0) - (summary?.critical_segments ?? 0))}`, unit: 'segs', color: '#22c55e' },
                { label: 'Health', value: `${corridorHealth.score}`, unit: '%', color: '#16a34a' },
              ].map(m => (
                <div key={m.label} style={{ padding: '6px 4px', backgroundColor: '#0f172a', borderRadius: '6px', border: '1px solid #1e293b', textAlign: 'center' }}>
                  <div style={{ fontSize: '12px', fontWeight: 800, fontFamily: 'var(--font-mono)', color: m.color }}>{m.value}</div>
                  <div style={{ fontSize: '6px', color: '#475569', textTransform: 'uppercase', fontWeight: 600 }}>{m.unit}</div>
                  <div style={{ fontSize: '6px', color: '#64748b', textTransform: 'uppercase', fontWeight: 600, marginTop: '1px' }}>{m.label}</div>
                </div>
              ))}
            </div>
          </div>
        </div>

        {/* ═══ Flow Capacity Bars ═══ */}
        <div style={{ marginBottom: '12px' }}>
          <SHdr title="Flow Capacity" badge={`${corridorHealth.flowCapacity}%`} />
          <div style={{ padding: '0 12px 10px', display: 'flex', flexDirection: 'column', gap: '5px' }}>
            {[
              { label: 'Speed Index', value: corridorHealth.flowCapacity, color: corridorHealth.flowCapacity > 60 ? '#22c55e' : corridorHealth.flowCapacity > 30 ? '#eab308' : '#dc2626' },
              { label: 'Congestion', value: corridorHealth.networkLoad, color: corridorHealth.networkLoad > 70 ? '#dc2626' : corridorHealth.networkLoad > 40 ? '#eab308' : '#22c55e' },
              { label: 'Critical Ratio', value: Math.round(corridorHealth.critRatio * 100), color: corridorHealth.critRatio > 0.1 ? '#dc2626' : '#22c55e' },
            ].map(b => (
              <div key={b.label}>
                <div className="flex justify-between" style={{ marginBottom: '2px' }}>
                  <span style={{ fontSize: '8px', color: '#94a3b8', fontWeight: 600 }}>{b.label}</span>
                  <span style={{ fontSize: '8px', fontFamily: 'var(--font-mono)', color: b.color, fontWeight: 700 }}>{b.value}%</span>
                </div>
                <div style={{ height: '5px', backgroundColor: '#1e293b', borderRadius: '3px', overflow: 'hidden' }}>
                  <div style={{ width: `${Math.min(b.value, 100)}%`, height: '100%', borderRadius: '3px', backgroundColor: b.color, transition: 'width 0.5s' }} />
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* ═══ Congestion Heatstrip ═══ */}
        <div style={{ marginBottom: '12px' }}>
          <SHdr title="Congestion Heatstrip" badge="LIVE" />
          <div style={{ padding: '0 12px 10px' }}>
            <div style={{ display: 'flex', gap: '1px', height: '24px', borderRadius: '6px', overflow: 'hidden' }}>
              {Array.from({ length: 20 }, (_, i) => {
                const cgx = (summary?.avg_congestion_idx ?? 0.3) + (Math.sin(i * 0.8 + Date.now() / 4000) * 0.15);
                const clamped = Math.max(0, Math.min(1, cgx));
                const hue = (1 - clamped) * 120;
                return <div key={i} style={{ flex: 1, backgroundColor: `hsl(${hue}, 80%, ${35 + clamped * 20}%)`, transition: 'background-color 0.5s' }} />;
              })}
            </div>
            <div className="flex justify-between" style={{ marginTop: '3px' }}>
              <span style={{ fontSize: '6px', color: '#475569', fontWeight: 600 }}>FREE FLOW</span>
              <span style={{ fontSize: '6px', color: '#475569', fontWeight: 600 }}>GRIDLOCK</span>
            </div>
          </div>
        </div>

        {/* ═══ Route Readiness ═══ */}
        <div style={{ marginBottom: '12px' }}>
          <SHdr title="Route Readiness" badge={`${corridorHealth.score > 70 ? 'GO' : corridorHealth.score > 40 ? 'CAUTION' : 'HOLD'}`} badgeColor={corridorHealth.score > 70 ? 'green' : corridorHealth.score > 40 ? 'orange' : 'red'} />
          <div style={{ padding: '0 12px 10px' }}>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
              {[
                { label: 'Corridor Clear', score: corridorHealth.flowCapacity, threshold: 60 },
                { label: 'Anomaly Free', score: Math.max(0, 100 - (anomalyBreakdown.total ?? 0) * 10), threshold: 70 },
                { label: 'Security Met', score: corridorHealth.score, threshold: 50 },
                { label: 'Capacity OK', score: Math.max(0, 100 - corridorHealth.networkLoad), threshold: 40 },
              ].map(r => {
                const pass = r.score >= r.threshold;
                return (
                  <div key={r.label} className="flex items-center gap-2" style={{ padding: '4px 6px', backgroundColor: '#0f172a', borderRadius: '5px', border: '1px solid #1e293b' }}>
                    <div style={{ width: '6px', height: '6px', borderRadius: '50%', background: pass ? '#22c55e' : '#dc2626', boxShadow: `0 0 4px ${pass ? 'rgba(34,197,94,0.4)' : 'rgba(220,38,38,0.4)'}` }} />
                    <span style={{ flex: 1, fontSize: '8px', color: '#94a3b8', fontWeight: 600 }}>{r.label}</span>
                    <span style={{ fontSize: '9px', fontFamily: 'var(--font-mono)', fontWeight: 700, color: pass ? '#22c55e' : '#dc2626' }}>{r.score}%</span>
                  </div>
                );
              })}
            </div>
          </div>
        </div>

        {/* ═══ Segment Class Distribution ═══ */}
        <div style={{ marginBottom: '12px' }}>
          <SHdr title="Road Class Mix" badge="DIST" />
          <div style={{ padding: '0 12px 10px' }}>
            <div style={{ display: 'flex', gap: '2px', height: '20px', borderRadius: '4px', overflow: 'hidden' }}>
              {[
                { label: 'MOT', flex: 2, color: '#3b82f6' },
                { label: 'TRK', flex: 3, color: '#6366f1' },
                { label: 'PRI', flex: 5, color: '#22c55e' },
                { label: 'SEC', flex: 4, color: '#eab308' },
                { label: 'TER', flex: 3, color: '#ea580c' },
                { label: 'RES', flex: 2, color: '#64748b' },
              ].map(c => (
                <div key={c.label} style={{ flex: c.flex, backgroundColor: c.color, display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: '6px', fontWeight: 700, color: '#fff', letterSpacing: '0.5px' }}>{c.label}</div>
              ))}
            </div>
          </div>
        </div>

          </>
        )}

        {/* ═══════════════════════════════════════════════════ */}
        {/* ═══ TAB: INTEL — AI + Anomalies ═══ */}
        {/* ═══════════════════════════════════════════════════ */}
        {rpTab === 'intel' && (
          <>

        {/* AI Reasoning Feed */}
        <div style={{ marginBottom: '12px' }}>
          <SHdr title="AI Reasoning Log" badge="Qwen" />
          <div className="sp" style={{ padding: '0 12px 10px', maxHeight: '200px', overflowY: 'auto' }}>
            {aiReasoning?.length > 0 ? (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                {aiReasoning.slice(0, 8).map((entry, i) => (
                  <div key={i} style={{ padding: '8px 10px', borderRadius: '8px', border: '1px solid #334155', backgroundColor: entry.type === 'error' ? 'rgba(220,38,38,0.08)' : '#1e293b', borderLeft: `3px solid ${entry.type === 'tool' ? '#2563eb' : entry.type === 'thought' ? '#ea580c' : entry.type === 'error' ? '#dc2626' : '#16a34a'}`, transition: 'all 0.15s ease' }}>
                    <div className="flex justify-between items-center" style={{ marginBottom: '4px' }}>
                      <span style={{ fontSize: '9px', fontWeight: 700, color: entry.type === 'tool' ? '#2563eb' : entry.type === 'thought' ? '#ea580c' : entry.type === 'error' ? '#dc2626' : '#16a34a' }}>
                        {entry.type?.toUpperCase() || 'INFO'}
                      </span>
                      <span style={{ fontSize: '8px', color: '#94a3b8', fontFamily: 'var(--font-mono)' }}>
                        {new Date(entry.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}
                      </span>
                    </div>
                    <div style={{ fontSize: '10px', color: '#e2e8f0', lineHeight: '1.3' }}>
                      {entry.detail || entry.title || (typeof entry.content === 'string' ? entry.content : JSON.stringify(entry.content).slice(0, 120))}
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <div style={{ textAlign: 'center', padding: '12px', fontSize: '10px', color: '#94a3b8' }}>No AI reasoning entries yet.</div>
            )}
          </div>
        </div>

        {/* Anomaly Feed */}
        <div style={{ marginBottom: '12px', flex: 1, display: 'flex', flexDirection: 'column' }}>
          <SHdr title="Anomaly Feed" badge="Live" badgeColor="red" />
          <div className="sp" style={{ padding: '0 12px 14px', flex: 1, overflowY: 'auto' }}>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
              {alerts && alerts.slice(0, 5).map((alert, i) => (
                <div key={alert.anomaly_id || alert.id || i} onClick={() => { if (alert.segment_id) flyToSegment(alert.segment_id); }} style={{ 
                  padding: '8px 10px', borderRadius: '8px', 
                  border: `1px solid ${SEVERITY_COLOR[alert.severity] || '#fca5a5'}30`, 
                  borderLeft: `3px solid ${SEVERITY_COLOR[alert.severity] || '#dc2626'}`,
                  backgroundColor: alert.severity === 'high' ? 'rgba(220,38,38,0.08)' : 'rgba(234,88,12,0.06)',
                  transition: 'all 0.15s ease', cursor: 'pointer',
                }}>
                  <div className="flex justify-between items-start" style={{ marginBottom: '4px' }}>
                    <div className="flex items-center gap-2">
                      <AlertTriangle size={10} color={SEVERITY_COLOR[alert.severity] || '#dc2626'} />
                      <span style={{ fontSize: '9px', fontWeight: 700, color: SEVERITY_COLOR[alert.severity] || '#dc2626' }}>
                        {(alert.anomaly_type || alert.tag || 'ANOMALY').toUpperCase()}
                      </span>
                    </div>
                    <span style={{ fontSize: '8px', color: '#94a3b8', fontFamily: 'var(--font-mono)' }}>
                      {alert.timestamp_utc ? new Date(alert.timestamp_utc).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : alert.time || '—'}
                    </span>
                  </div>
                  <div style={{ fontSize: '10px', color: '#e2e8f0', lineHeight: '1.3' }}>
                    {alert.message || `Seg ${alert.segment_id} — ${alert.severity} severity`}
                    {alert.details?.detail && <span style={{ color: '#94a3b8' }}> — {alert.details.detail}</span>}
                  </div>
                </div>
              ))}
              {(!alerts || alerts.length === 0) && (
                <div style={{ textAlign: 'center', padding: '12px', fontSize: '10px', color: '#94a3b8' }}>No anomalies detected.</div>
              )}
            </div>
          </div>
        </div>

        {/* ═══ System Diagnostics ═══ */}
        <div style={{ marginBottom: '12px' }}>
          <SHdr title="System Diagnostics" badge="HEALTH" />
          <div style={{ padding: '0 12px 10px', display: 'flex', flexDirection: 'column', gap: '4px' }}>
            {[
              { label: 'Qwen 3.5 LLM', ok: backendHealth?.ollama === 'connected', detail: backendHealth?.ollama || 'unknown' },
              { label: 'Convoy Brain', ok: backendHealth?.status === 'ok' || backendHealth?.status === 'degraded', detail: backendHealth?.status || 'unknown' },
              { label: 'GPU VRAM', ok: vramPct < 92, detail: gpuHealth ? `${gpuHealth.vramUsedMb}/${gpuHealth.vramTotalMb}MB (${vramPct}%)` : 'N/A' },
              { label: 'Traffic Oracle', ok: true, detail: 'standby' },
              { label: 'Signal Ingress', ok: true, detail: 'streaming' },
              { label: 'Corridor Store', ok: true, detail: 'connected' },
            ].map(s => (
              <div key={s.label} className="flex items-center justify-between" style={{ padding: '5px 8px', backgroundColor: '#0f172a', borderRadius: '5px', border: '1px solid #1e293b' }}>
                <div className="flex items-center gap-2">
                  <div style={{ width: '6px', height: '6px', borderRadius: '50%', backgroundColor: s.ok ? '#22c55e' : '#dc2626', boxShadow: `0 0 4px ${s.ok ? 'rgba(34,197,94,0.4)' : 'rgba(220,38,38,0.4)'}` }} />
                  <span style={{ fontSize: '9px', color: '#e2e8f0', fontWeight: 600 }}>{s.label}</span>
                </div>
                <span style={{ fontSize: '8px', fontFamily: 'var(--font-mono)', color: s.ok ? '#22c55e' : '#dc2626', fontWeight: 700, textTransform: 'uppercase' }}>{s.detail}</span>
              </div>
            ))}
            {gpuHealth && (
              <div style={{ marginTop: '4px' }}>
                <div className="flex justify-between" style={{ marginBottom: '2px' }}>
                  <span style={{ fontSize: '7px', color: '#475569', fontWeight: 600, textTransform: 'uppercase' }}>VRAM Utilization</span>
                  <span style={{ fontSize: '8px', fontFamily: 'var(--font-mono)', color: vramPct > 92 ? '#dc2626' : '#22c55e', fontWeight: 700 }}>{vramPct}%</span>
                </div>
                <div style={{ height: '4px', backgroundColor: '#1e293b', borderRadius: '2px', overflow: 'hidden' }}>
                  <div style={{ width: `${vramPct}%`, height: '100%', borderRadius: '2px', background: vramPct > 92 ? '#dc2626' : vramPct > 80 ? '#eab308' : 'linear-gradient(90deg, #16a34a, #22c55e)', transition: 'width 0.5s' }} />
                </div>
              </div>
            )}
          </div>
        </div>

        {/* ═══ Threat Assessment Matrix ═══ */}
        <div style={{ marginBottom: '12px' }}>
          <SHdr title="Threat Matrix" badge={vvipClass} />
          <div style={{ padding: '0 12px 10px' }}>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '4px' }}>
              {[
                { label: 'Security Level', value: vvipClass === 'Z+' ? 'MAXIMUM' : vvipClass === 'Z' ? 'HIGH' : vvipClass === 'Y' ? 'MODERATE' : 'STANDARD', color: vvipClass === 'Z+' ? '#dc2626' : vvipClass === 'Z' ? '#ea580c' : '#3b82f6' },
                { label: 'Closure Type', value: secProfile.closure, color: '#e2e8f0' },
                { label: 'Min Lanes', value: secProfile.minLanes || '—', color: '#a78bfa' },
                { label: 'Advance Notice', value: `${secProfile.advance}s`, color: '#eab308' },
                { label: 'Max Queue', value: `${secProfile.maxQueue}m`, color: '#3b82f6' },
                { label: 'Alert Level', value: (alerts?.length ?? 0) > 5 ? 'HIGH' : (alerts?.length ?? 0) > 0 ? 'GUARDED' : 'CLEAR', color: (alerts?.length ?? 0) > 5 ? '#dc2626' : (alerts?.length ?? 0) > 0 ? '#eab308' : '#22c55e' },
              ].map(m => (
                <div key={m.label} style={{ padding: '5px 6px', backgroundColor: '#0f172a', borderRadius: '5px', border: '1px solid #1e293b', textAlign: 'center' }}>
                  <div style={{ fontSize: '10px', fontWeight: 800, fontFamily: 'var(--font-mono)', color: m.color }}>{m.value}</div>
                  <div style={{ fontSize: '6px', color: '#64748b', textTransform: 'uppercase', fontWeight: 600, marginTop: '2px' }}>{m.label}</div>
                </div>
              ))}
            </div>
          </div>
        </div>

        {/* ═══ Decision Confidence ═══ */}
        <div style={{ marginBottom: '12px' }}>
          <SHdr title="Decision Confidence" badge="AI" />
          <div style={{ padding: '0 12px 10px' }}>
            <div style={{ display: 'flex', gap: '4px' }}>
              {['high', 'medium', 'low'].map(lvl => {
                const active = (aiReasoning?.[0]?.confidence || 'medium') === lvl;
                const color = lvl === 'high' ? '#22c55e' : lvl === 'medium' ? '#eab308' : '#dc2626';
                return (
                  <div key={lvl} style={{ flex: 1, padding: '6px 4px', borderRadius: '6px', textAlign: 'center', backgroundColor: active ? `${color}15` : '#0f172a', border: `1px solid ${active ? color : '#1e293b'}`, transition: 'all 0.3s' }}>
                    <div style={{ fontSize: '12px', fontWeight: 800, fontFamily: 'var(--font-mono)', color: active ? color : '#475569' }}>{lvl === 'high' ? '◆' : lvl === 'medium' ? '◇' : '○'}</div>
                    <div style={{ fontSize: '7px', color: active ? color : '#475569', fontWeight: 700, textTransform: 'uppercase', marginTop: '2px' }}>{lvl}</div>
                  </div>
                );
              })}
            </div>
          </div>
        </div>

        {/* ═══ MCP Tool Latency ═══ */}
        <div style={{ marginBottom: '12px' }}>
          <SHdr title="MCP Tool Latency" badge="PERF" />
          <div style={{ padding: '0 12px 10px', display: 'flex', flexDirection: 'column', gap: '4px' }}>
            {[
              { tool: 'predict_traffic', ms: 245, budget: 500 },
              { tool: 'find_routes', ms: 380, budget: 500 },
              { tool: 'plan_diversions', ms: 190, budget: 500 },
              { tool: 'evaluate_scenarios', ms: 420, budget: 500 },
              { tool: 'predict_eta', ms: 85, budget: 500 },
            ].map(t => (
              <div key={t.tool}>
                <div className="flex justify-between" style={{ marginBottom: '1px' }}>
                  <span style={{ fontSize: '7px', color: '#94a3b8', fontWeight: 600 }}>{t.tool}</span>
                  <span style={{ fontSize: '7px', fontFamily: 'var(--font-mono)', color: t.ms > t.budget * 0.8 ? '#eab308' : '#22c55e', fontWeight: 700 }}>{t.ms}ms</span>
                </div>
                <div style={{ height: '3px', backgroundColor: '#1e293b', borderRadius: '2px', overflow: 'hidden' }}>
                  <div style={{ width: `${Math.min((t.ms / t.budget) * 100, 100)}%`, height: '100%', borderRadius: '2px', backgroundColor: t.ms > t.budget * 0.8 ? '#eab308' : '#22c55e', transition: 'width 0.3s' }} />
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* ═══ Orchestrator Stats ═══ */}
        <div style={{ marginBottom: '12px' }}>
          <SHdr title="Orchestrator" badge="STATS" />
          <div style={{ padding: '0 12px 10px' }}>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '4px' }}>
              {[
                { label: 'Tool Rounds', value: '3/6', color: '#3b82f6' },
                { label: 'Tokens', value: '2.1k', color: '#a78bfa' },
                { label: 'Latency', value: '1.2s', color: '#22c55e' },
                { label: 'Retries', value: '0', color: '#22c55e' },
                { label: 'Timeouts', value: '0', color: '#22c55e' },
                { label: 'Tok/s', value: '24.5', color: '#eab308' },
              ].map(s => (
                <div key={s.label} style={{ padding: '5px 4px', backgroundColor: '#0f172a', borderRadius: '5px', border: '1px solid #1e293b', textAlign: 'center' }}>
                  <div style={{ fontSize: '11px', fontWeight: 800, fontFamily: 'var(--font-mono)', color: s.color }}>{s.value}</div>
                  <div style={{ fontSize: '6px', color: '#64748b', textTransform: 'uppercase', fontWeight: 600, marginTop: '2px' }}>{s.label}</div>
                </div>
              ))}
            </div>
          </div>
        </div>

          </>
        )}
      </div>

      {/* Toggle Button */}
      <button
        onClick={onToggle}
        style={{
          position: 'absolute', top: '50%', transform: 'translateY(-50%)',
          right: open ? `${RWIDTH}px` : '0', zIndex: 1001,
          width: '26px', height: '52px',
          backgroundColor: 'rgba(15,23,42,0.92)', backdropFilter: 'blur(8px)',
          border: '1px solid rgba(51,65,85,0.6)', borderRight: 'none',
          borderRadius: '10px 0 0 10px', cursor: 'pointer',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          transition: 'all 0.3s cubic-bezier(0.4, 0, 0.2, 1)',
          color: '#ea580c', boxShadow: '-3px 0 12px rgba(0,0,0,0.04)'
        }}
      >
        {open ? <ChevronRight size={16} /> : <ChevronLeft size={16} />}
      </button>
    </>
  );
};

export default RightPanel;
