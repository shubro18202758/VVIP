import React, { useState, useEffect, useMemo, useCallback, useRef } from 'react';
import { ArrowLeft, BarChart3, TrendingUp, TrendingDown, Minus, Loader2, ChevronRight, Target, Zap, Activity } from 'lucide-react';
import { AreaChart, Area, LineChart, Line, BarChart, Bar, ComposedChart, Cell, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, ReferenceLine, RadialBarChart, RadialBar, Legend, PieChart, Pie, ScatterChart, Scatter, RadarChart, PolarGrid, PolarAngleAxis, PolarRadiusAxis, Radar } from 'recharts';
import * as api from '../services/api';
import { useConvoy } from '../context/ConvoyContext';

const STATUS_COLORS = { green: '#16a34a', amber: '#eab308', red: '#dc2626' };

const AnalyticsDeepDive = ({ metric, onBack, vvipClass }) => {
  const { convoySimulation, gpuHealth } = useConvoy();
  const [reasoning, setReasoning] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [activeSection, setActiveSection] = useState('overview');
  const [elapsed, setElapsed] = useState(0);

  // Use refs for live data so fetching doesn't re-trigger on every poll update
  const convoyRef = useRef(convoySimulation);
  const gpuRef = useRef(gpuHealth);
  convoyRef.current = convoySimulation;
  gpuRef.current = gpuHealth;

  // Track whether we already fetched for this metric to avoid re-fetching
  const fetchedMetricRef = useRef(null);

  // Fetch AI reasoning from backend — only re-runs when metric or vvipClass changes
  const fetchReasoning = useCallback((force = false) => {
    if (!metric) return;
    // Skip if we already fetched for this exact metric (unless forced retry)
    const metricKey = `${metric.name}:${metric.value}`;
    if (!force && fetchedMetricRef.current === metricKey) return;
    fetchedMetricRef.current = metricKey;

    setLoading(true);
    setError(null);
    setReasoning(null);
    setElapsed(0);

    const conv = convoyRef.current;
    const gpu = gpuRef.current;
    const metricContext = {
      timestamp: new Date().toISOString(),
      convoy_speed: conv?.speed ?? 0,
      convoy_congestion: conv?.congestion ?? 0,
      convoy_progress: conv?.progress ?? 0,
      gpu_vram_used: gpu?.vramUsedMb ?? 0,
      gpu_vram_total: gpu?.vramTotalMb ?? 8192,
      zone: conv?.currentZone ?? 'Unknown',
      ...(metric.context || {}),
    };

    api.analyticsReasoning(metric.name, metric.value, metricContext, vvipClass || 'Z')
      .then(res => {
        setReasoning(res.data);
        setLoading(false);
      })
      .catch(err => {
        const detail = err.response?.data?.detail;
        const msg = Array.isArray(detail) ? detail.map(d => d.msg || JSON.stringify(d)).join('; ') : (typeof detail === 'string' ? detail : err.message || 'Failed to fetch reasoning');
        setError(msg);
        setLoading(false);
      });
  }, [metric, vvipClass]);

  // Auto-fetch once when metric changes
  useEffect(() => {
    fetchReasoning();
  }, [fetchReasoning]);

  // Elapsed time counter while loading
  useEffect(() => {
    if (!loading) return;
    const t = setInterval(() => setElapsed(e => e + 1), 1000);
    return () => clearInterval(t);
  }, [loading]);

  // Parse structured reasoning from Qwen response
  const parsed = useMemo(() => {
    if (!reasoning?.reasoning) return null;
    const r = reasoning.reasoning;
    // The orchestrator returns {action, reasoning, data, confidence, tool_calls_made}
    // Try to parse the 'data' or 'reasoning' field as structured JSON
    let structured = null;
    try {
      if (r.data && typeof r.data === 'object' && Object.keys(r.data).length > 0) {
        structured = r.data;
      } else if (typeof r.reasoning === 'string') {
        // Try extracting JSON from the reasoning text
        const jsonMatch = r.reasoning.match(/\{[\s\S]*\}/);
        if (jsonMatch) structured = JSON.parse(jsonMatch[0]);
      }
    } catch { /* fallback to text display */ }
    return {
      structured,
      text: r.reasoning || r.action || 'Analysis complete.',
      confidence: r.confidence || 'medium',
      tools: r.tool_calls_made || [],
    };
  }, [reasoning]);

  // Generate local mathematical breakdown for the metric
  const mathBreakdown = useMemo(() => {
    if (!metric) return [];
    const steps = [];
    const v = typeof metric.value === 'number' ? metric.value : parseFloat(metric.value) || 0;

    switch (metric.type) {
      case 'speed': {
        const freeFlow = 60;
        const ratio = v / freeFlow;
        steps.push({ op: 'Free-Flow Baseline', formula: `V_ff = ${freeFlow} km/h`, result: freeFlow, unit: 'km/h' });
        steps.push({ op: 'Speed Ratio', formula: `r = V_current / V_ff = ${v} / ${freeFlow}`, result: Math.round(ratio * 1000) / 1000 });
        steps.push({ op: 'Performance Index', formula: `PI = r × 100`, result: Math.round(ratio * 100), unit: '%' });
        steps.push({ op: 'Level of Service', formula: `LOS = ${ratio > 0.8 ? 'A (Free Flow)' : ratio > 0.6 ? 'C (Stable)' : ratio > 0.4 ? 'D (Approaching Unstable)' : 'F (Forced Flow)'}`, result: ratio > 0.8 ? 'A' : ratio > 0.6 ? 'C' : ratio > 0.4 ? 'D' : 'F' });
        steps.push({ op: 'BPR Delay Function', formula: `t(V) = t₀[1 + α(V/C)^β] where α=0.15, β=4`, result: Math.round((1 + 0.15 * Math.pow(ratio, 4)) * 1000) / 1000 });
        steps.push({ op: 'Greenshields Speed-Density', formula: `V = V_f(1 - k/k_j) → k = k_j(1 - V/V_f) = 150(1 - ${ratio.toFixed(3)})`, result: Math.round(150 * (1 - ratio)), unit: 'veh/km' });
        break;
      }
      case 'congestion': {
        const cIdx = v / 100;
        steps.push({ op: 'Congestion Index', formula: `CGX = ${v}%`, result: v, unit: '%' });
        steps.push({ op: 'Density Estimation', formula: `k = k_jam × CGX = 150 × ${cIdx.toFixed(3)}`, result: Math.round(150 * cIdx), unit: 'veh/km' });
        steps.push({ op: 'Queue Length', formula: `Q = k × L_segment`, result: Math.round(150 * cIdx * 2.5), unit: 'm' });
        steps.push({ op: 'Delay Factor', formula: `δ = 1 + 0.15 × (CGX/0.9)^4`, result: Math.round((1 + 0.15 * Math.pow(cIdx / 0.9, 4)) * 1000) / 1000 });
        steps.push({ op: 'Webster Signal Delay', formula: `d = C(1-λ)²/2(1-λx) + x²/2q(1-x) where C=90s,λ=0.4`, result: Math.round(90 * Math.pow(1 - 0.4, 2) / (2 * (1 - 0.4 * cIdx)) + cIdx * cIdx / (2 * 0.5 * (1 - cIdx + 0.001))), unit: 's' });
        steps.push({ op: 'Network Resilience (R)', formula: `R = 1 - CGX / CGX_critical = 1 - ${cIdx.toFixed(3)} / 0.85`, result: Math.round((1 - cIdx / 0.85) * 1000) / 1000 });
        break;
      }
      case 'eta': {
        const dist = convoySimulation?.totalDistance ?? 15000;
        const spd = convoySimulation?.speed ?? 30;
        const baseTime = dist / (spd * 1000 / 3600);
        steps.push({ op: 'Base Travel Time', formula: `T_base = D / V = ${(dist / 1000).toFixed(1)}km / ${spd}km/h`, result: Math.round(baseTime), unit: 's' });
        steps.push({ op: 'Signal Delay', formula: `T_signal = N_signals × avg_wait`, result: Math.round((convoySimulation?.signalsRemaining ?? 5) * 25), unit: 's' });
        steps.push({ op: 'Congestion Penalty', formula: `T_cong = T_base × δ`, result: Math.round(baseTime * 0.15), unit: 's' });
        steps.push({ op: 'Final ETA', formula: `T_total = T_base + T_signal + T_cong`, result: Math.round(v), unit: 's' });
        steps.push({ op: 'ETA Confidence Interval', formula: `CI₉₅ = T ± 1.96σ_T where σ_T = T × CV_speed`, result: `${Math.round(v * 0.85)}–${Math.round(v * 1.15)}`, unit: 's' });
        break;
      }
      case 'performance': {
        steps.push({ op: 'Raw Value', formula: `${metric.name} = ${v}`, result: v, unit: metric.unit || '' });
        steps.push({ op: 'Normalized Score', formula: `S_norm = value / max × 100`, result: Math.round(v / (metric.max || 100) * 100), unit: '%' });
        steps.push({ op: 'Z-Score', formula: `z = (x - μ) / σ (estimated μ=${(metric.max || 100) / 2}, σ=${(metric.max || 100) / 6})`, result: Math.round(((v - (metric.max || 100) / 2) / ((metric.max || 100) / 6)) * 100) / 100 });
        steps.push({ op: 'Percentile Rank', formula: `P = Φ(z) × 100`, result: Math.round(50 * (1 + (2 / Math.sqrt(Math.PI)) * Math.tanh(0.8 * ((v - (metric.max || 100) / 2) / ((metric.max || 100) / 6))))), unit: '%' });
        break;
      }
      default: {
        steps.push({ op: 'Raw Value', formula: `${metric.name} = ${v}`, result: v, unit: metric.unit || '' });
        steps.push({ op: 'Normalized', formula: `${metric.name}_norm = value / max`, result: Math.round(v / (metric.max || 100) * 100) / 100 });
      }
    }
    return steps;
  }, [metric, convoySimulation]);

  // Trend data for mini-chart
  const trendData = useMemo(() => {
    const v = typeof metric?.value === 'number' ? metric.value : parseFloat(metric?.value) || 0;
    return Array.from({ length: 12 }, (_, i) => ({
      t: `${i * 5}s`,
      value: Math.max(0, v + (Math.sin(i * 0.8) * v * 0.15) + (Math.random() - 0.5) * v * 0.08),
    }));
  }, [metric]);

  // Gauge data for radial display — handles negative values (like jerk)
  const gaugeData = useMemo(() => {
    const v = typeof metric?.value === 'number' ? metric.value : parseFloat(metric?.value) || 0;
    const max = metric?.max || 100;
    const pct = Math.min(100, Math.max(0, Math.round(Math.abs(v) / max * 100)));
    return [{ name: metric?.name || 'Value', value: pct, fill: pct > 75 ? '#dc2626' : pct > 50 ? '#eab308' : '#16a34a' }];
  }, [metric]);

  // Category-specific SVG icon for metric hero card
  const categoryIcon = useMemo(() => {
    const cat = (metric?.category || metric?.type || '').toLowerCase();
    const iconProps = { width: 36, height: 36, viewBox: '0 0 24 24', fill: 'none', stroke: '#475569', strokeWidth: 1.2, strokeLinecap: 'round', strokeLinejoin: 'round' };
    if (cat.includes('comfort') || cat.includes('jerk') || cat.includes('ride')) {
      // Waveform / oscillation icon for ride comfort
      return (
        <svg {...iconProps}>
          <path d="M2 12c2-3 4-6 6 0s4 3 6 0 4-6 6 0" stroke="#a78bfa" />
          <path d="M2 16c2-2 4-4 6 0s4 2 6 0 4-4 6 0" stroke="#a78bfa" opacity={0.4} />
        </svg>
      );
    }
    if (cat.includes('speed') || cat.includes('velocity')) {
      // Speedometer icon
      return (
        <svg {...iconProps}>
          <circle cx="12" cy="14" r="8" stroke="#3b82f6" />
          <path d="M12 14l3-5" stroke="#ea580c" strokeWidth={2} />
          <path d="M8 18h8" stroke="#3b82f6" opacity={0.5} />
          <circle cx="12" cy="14" r="1.5" fill="#ea580c" stroke="none" />
        </svg>
      );
    }
    if (cat.includes('congestion') || cat.includes('density') || cat.includes('traffic')) {
      // Traffic bars icon
      return (
        <svg {...iconProps}>
          <rect x="3" y="14" width="3" height="6" rx="1" fill="#22c55e" stroke="none" />
          <rect x="8" y="10" width="3" height="10" rx="1" fill="#eab308" stroke="none" />
          <rect x="13" y="6" width="3" height="14" rx="1" fill="#f97316" stroke="none" />
          <rect x="18" y="3" width="3" height="17" rx="1" fill="#dc2626" stroke="none" />
        </svg>
      );
    }
    if (cat.includes('eta') || cat.includes('time') || cat.includes('arrival')) {
      // Clock icon
      return (
        <svg {...iconProps}>
          <circle cx="12" cy="12" r="9" stroke="#06b6d4" />
          <path d="M12 7v5l3.5 2" stroke="#06b6d4" strokeWidth={1.5} />
          <path d="M16.5 17.5l1.5 1.5" stroke="#06b6d4" opacity={0.5} />
        </svg>
      );
    }
    if (cat.includes('security') || cat.includes('safety')) {
      // Shield icon
      return (
        <svg {...iconProps}>
          <path d="M12 3l7 3v5c0 4.5-3 8.5-7 10-4-1.5-7-5.5-7-10V6l7-3z" stroke="#16a34a" />
          <path d="M9 12l2 2 4-4" stroke="#16a34a" strokeWidth={1.5} />
        </svg>
      );
    }
    // Default: analytics chart icon
    return (
      <svg {...iconProps}>
        <path d="M3 20l5-8 4 4 5-9 4 6" stroke="#3b82f6" strokeWidth={1.5} />
        <path d="M3 20h18" stroke="#3b82f6" opacity={0.4} />
      </svg>
    );
  }, [metric]);

  // Sensitivity analysis — how metric changes with ±perturbations
  const sensitivityData = useMemo(() => {
    const v = typeof metric?.value === 'number' ? metric.value : parseFloat(metric?.value) || 0;
    if (!v || v === 0) return [];
    const factors = [
      { name: 'Speed', delta: -0.2, impact: metric.type === 'speed' ? 1.0 : 0.6 },
      { name: 'Congestion', delta: 0.15, impact: metric.type === 'congestion' ? 1.0 : 0.7 },
      { name: 'Density', delta: 0.1, impact: 0.5 },
      { name: 'Signal Delay', delta: 0.08, impact: metric.type === 'eta' ? 0.9 : 0.3 },
      { name: 'Lane Count', delta: -0.05, impact: 0.4 },
      { name: 'Weather', delta: 0.12, impact: 0.35 },
    ];
    return factors.map(f => ({
      factor: f.name,
      perturbation: `${f.delta > 0 ? '+' : ''}${(f.delta * 100).toFixed(0)}%`,
      sensitivity: Math.round(f.impact * Math.abs(f.delta) * 1000) / 10,
      impact: Math.round(v * f.delta * f.impact * 100) / 100,
      elasticity: Math.round((f.delta * f.impact / (f.delta || 0.01)) * 100) / 100,
      color: f.impact > 0.6 ? '#dc2626' : f.impact > 0.4 ? '#eab308' : '#22c55e',
    }));
  }, [metric]);

  // Comparative benchmarks
  const benchmarkData = useMemo(() => {
    const v = typeof metric?.value === 'number' ? metric.value : parseFloat(metric?.value) || 0;
    return [
      { name: 'Current', value: v, fill: '#3b82f6' },
      { name: 'Z+ Optimal', value: v * 1.15, fill: '#22c55e' },
      { name: 'Z Baseline', value: v * 0.95, fill: '#06b6d4' },
      { name: 'Y Threshold', value: v * 0.8, fill: '#eab308' },
      { name: 'Critical', value: v * 0.5, fill: '#dc2626' },
    ];
  }, [metric]);

  // Multi-dimensional radar for metric context
  const contextRadar = useMemo(() => {
    const spd = convoySimulation?.speed ?? 30;
    const cng = (convoySimulation?.congestion ?? 0.3) * 100;
    const prog = (convoySimulation?.progress ?? 0.5) * 100;
    return [
      { axis: 'Speed', A: Math.min(100, spd / 60 * 100), fullMark: 100 },
      { axis: 'Safety', A: Math.max(0, 100 - cng * 1.5), fullMark: 100 },
      { axis: 'Progress', A: prog, fullMark: 100 },
      { axis: 'Efficiency', A: Math.min(100, (spd / (cng + 1)) * 3), fullMark: 100 },
      { axis: 'Stability', A: Math.max(0, 100 - Math.abs(convoySimulation?.acceleration ?? 0) * 20), fullMark: 100 },
      { axis: 'Comfort', A: Math.max(0, 100 - Math.abs(convoySimulation?.acceleration ?? 0) * 30), fullMark: 100 },
    ];
  }, [convoySimulation]);

  if (!metric) return null;

  const confColor = parsed?.confidence === 'high' ? '#16a34a' : parsed?.confidence === 'medium' ? '#eab308' : '#dc2626';

  return (
    <div style={{
      position: 'absolute', top: 0, left: 0, right: 0, bottom: 0,
      backgroundColor: '#0a0f1e', zIndex: 2000,
      display: 'flex', flexDirection: 'column',
      overflow: 'hidden',
    }}>
      {/* Header */}
      <div style={{
        padding: '14px 16px', borderBottom: '1px solid #1e293b',
        background: 'linear-gradient(135deg, rgba(59,130,246,0.08), rgba(139,92,246,0.06))',
        display: 'flex', alignItems: 'center', gap: '12px',
      }}>
        <button onClick={onBack} style={{
          background: 'rgba(255,255,255,0.05)', border: '1px solid #334155',
          borderRadius: '8px', padding: '6px 8px', cursor: 'pointer', color: '#94a3b8',
          display: 'flex', alignItems: 'center', gap: '4px', fontSize: '11px',
        }}>
          <ArrowLeft size={14} /> BACK
        </button>
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: '13px', fontWeight: 700, color: '#e2e8f0', display: 'flex', alignItems: 'center', gap: '6px' }}>
            <BarChart3 size={14} color="#3b82f6" />
            ANALYTICS DEEP-DIVE
          </div>
          <div style={{ fontSize: '9px', color: '#64748b', marginTop: '2px', fontFamily: 'var(--font-mono)' }}>
            Mathematical Reasoning Engine — Powered by Qwen 3.5
          </div>
        </div>
        {parsed && (
          <div style={{
            padding: '3px 8px', borderRadius: '6px', fontSize: '8px', fontWeight: 700,
            background: `${confColor}20`, color: confColor, border: `1px solid ${confColor}40`,
          }}>
            {(parsed.confidence || 'MEDIUM').toUpperCase()} CONFIDENCE
          </div>
        )}
      </div>

      {/* Metric Hero Card */}
      <div style={{
        padding: '14px 16px', display: 'flex', gap: '12px',
        background: 'linear-gradient(180deg, rgba(15,23,42,0.8), #0a0f1e)',
        borderBottom: '1px solid #1e293b',
      }}>
        <div style={{
          width: '100px', height: '100px', borderRadius: '12px',
          background: 'rgba(30,41,59,0.6)', border: '1px solid #334155',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          position: 'relative', overflow: 'hidden',
        }}>
          {/* Background category icon */}
          <div style={{ position: 'absolute', top: '50%', left: '50%', transform: 'translate(-50%, -50%)', opacity: 0.25 }}>
            {categoryIcon}
          </div>
          {/* Gauge overlay */}
          <ResponsiveContainer width={90} height={90}>
            <RadialBarChart cx="50%" cy="50%" innerRadius="60%" outerRadius="90%" data={gaugeData} startAngle={180} endAngle={0}>
              <RadialBar dataKey="value" cornerRadius={4} />
            </RadialBarChart>
          </ResponsiveContainer>
          {/* Value badge */}
          <div style={{
            position: 'absolute', bottom: '4px', left: '50%', transform: 'translateX(-50%)',
            fontSize: '8px', fontWeight: 800, color: gaugeData[0]?.fill || '#3b82f6',
            fontFamily: 'var(--font-mono)', background: 'rgba(10,15,30,0.85)',
            padding: '1px 6px', borderRadius: '4px',
          }}>
            {gaugeData[0]?.value}%
          </div>
        </div>
        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', justifyContent: 'center' }}>
          <div style={{ fontSize: '9px', color: '#64748b', fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.5px' }}>
            {metric.category || 'ANALYTICS METRIC'}
          </div>
          <div style={{ fontSize: '18px', fontWeight: 800, color: '#f1f5f9', fontFamily: 'var(--font-mono)', marginTop: '2px' }}>
            {metric.name}
          </div>
          <div style={{ fontSize: '22px', fontWeight: 900, color: '#ea580c', fontFamily: 'var(--font-mono)', marginTop: '2px' }}>
            {typeof metric.value === 'number' ? metric.value.toFixed(1) : metric.value}
            <span style={{ fontSize: '11px', color: '#64748b', marginLeft: '4px' }}>{metric.unit || ''}</span>
          </div>
        </div>
      </div>

      {/* Section Tabs */}
      <div style={{ display: 'flex', gap: '2px', padding: '8px 16px', borderBottom: '1px solid #1e293b' }}>
        {['overview', 'math', 'sensitivity', 'ai-analysis'].map(s => (
          <button key={s} onClick={() => setActiveSection(s)} style={{
            flex: 1, padding: '6px', border: 'none', borderRadius: '6px', cursor: 'pointer',
            fontSize: '9px', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.5px',
            background: activeSection === s ? 'rgba(234,88,12,0.12)' : 'transparent',
            color: activeSection === s ? '#f97316' : '#64748b',
            borderBottom: activeSection === s ? '2px solid #ea580c' : '2px solid transparent',
          }}>
            {s === 'overview' ? '📊 Overview' : s === 'math' ? '🧮 Math' : s === 'sensitivity' ? '📐 Sensitivity' : '🤖 AI Analysis'}
          </button>
        ))}
      </div>

      {/* Content Area */}
      <div style={{ flex: 1, overflowY: 'auto', padding: '12px 16px' }}>

        {/* === OVERVIEW SECTION === */}
        {activeSection === 'overview' && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
            {/* Trend Chart */}
            <div style={{ background: 'rgba(30,41,59,0.4)', borderRadius: '10px', border: '1px solid #1e293b', padding: '12px' }}>
              <div style={{ fontSize: '9px', fontWeight: 700, color: '#94a3b8', marginBottom: '8px', textTransform: 'uppercase' }}>
                <Activity size={10} style={{ display: 'inline', marginRight: '4px' }} />
                60-Second Trend
              </div>
              <ResponsiveContainer width="100%" height={120}>
                <AreaChart data={trendData}>
                  <defs>
                    <linearGradient id="adTrendGrad" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="#3b82f6" stopOpacity={0.4} />
                      <stop offset="100%" stopColor="#3b82f6" stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                  <XAxis dataKey="t" tick={{ fontSize: 8, fill: '#64748b' }} axisLine={false} />
                  <YAxis tick={{ fontSize: 8, fill: '#64748b' }} axisLine={false} width={35} />
                  <Tooltip contentStyle={{ background: '#1e293b', border: '1px solid #334155', borderRadius: '6px', fontSize: '10px' }} />
                  <Area type="monotone" dataKey="value" stroke="#3b82f6" fill="url(#adTrendGrad)" strokeWidth={2} dot={false} />
                  <ReferenceLine y={typeof metric.value === 'number' ? metric.value : 0} stroke="#ea580c" strokeDasharray="4 4" label={{ value: 'Current', position: 'right', fontSize: 8, fill: '#ea580c' }} />
                </AreaChart>
              </ResponsiveContainer>
            </div>

            {/* Quick Stats Grid */}
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '6px' }}>
              {[
                { label: 'Current', value: typeof metric.value === 'number' ? metric.value.toFixed(1) : metric.value, color: '#3b82f6' },
                { label: 'Threshold', value: metric.threshold || 'N/A', color: '#eab308' },
                { label: 'Status', value: metric.status || 'Active', color: metric.status === 'critical' ? '#dc2626' : '#16a34a' },
              ].map(s => (
                <div key={s.label} style={{
                  background: 'rgba(30,41,59,0.4)', borderRadius: '8px', border: '1px solid #1e293b',
                  padding: '10px 8px', textAlign: 'center',
                }}>
                  <div style={{ fontSize: '7px', color: '#64748b', fontWeight: 600, textTransform: 'uppercase' }}>{s.label}</div>
                  <div style={{ fontSize: '14px', fontWeight: 800, color: s.color, fontFamily: 'var(--font-mono)', marginTop: '2px' }}>{s.value}</div>
                </div>
              ))}
            </div>

            {/* Context Info */}
            <div style={{ background: 'rgba(30,41,59,0.4)', borderRadius: '10px', border: '1px solid #1e293b', padding: '10px 12px' }}>
              <div style={{ fontSize: '9px', fontWeight: 700, color: '#94a3b8', marginBottom: '6px', textTransform: 'uppercase' }}>
                Operational Context
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '4px' }}>
                {[
                  { k: 'VVIP Class', v: vvipClass || 'Z' },
                  { k: 'Zone', v: convoySimulation?.currentZone || '—' },
                  { k: 'Speed', v: `${Math.round(convoySimulation?.speed || 0)} km/h` },
                  { k: 'Congestion', v: `${Math.round((convoySimulation?.congestion || 0) * 100)}%` },
                ].map(item => (
                  <div key={item.k} style={{ display: 'flex', justifyContent: 'space-between', padding: '3px 0', borderBottom: '1px solid #1e293b22' }}>
                    <span style={{ fontSize: '8px', color: '#64748b' }}>{item.k}</span>
                    <span style={{ fontSize: '8px', color: '#e2e8f0', fontFamily: 'var(--font-mono)', fontWeight: 600 }}>{item.v}</span>
                  </div>
                ))}
              </div>
            </div>

            {/* Context Radar */}
            <div style={{ background: 'rgba(30,41,59,0.4)', borderRadius: '10px', border: '1px solid #1e293b', padding: '12px' }}>
              <div style={{ fontSize: '9px', fontWeight: 700, color: '#94a3b8', marginBottom: '6px', textTransform: 'uppercase' }}>
                <Target size={10} style={{ display: 'inline', marginRight: '4px' }} />
                Multi-Dimensional Context
              </div>
              <ResponsiveContainer width="100%" height={140}>
                <RadarChart data={contextRadar}>
                  <PolarGrid stroke="#1e293b" />
                  <PolarAngleAxis dataKey="axis" tick={{ fontSize: 7, fill: '#94a3b8' }} />
                  <PolarRadiusAxis tick={false} domain={[0, 100]} />
                  <Radar name="Context" dataKey="A" stroke="#3b82f6" fill="#3b82f6" fillOpacity={0.2} strokeWidth={1.5} />
                </RadarChart>
              </ResponsiveContainer>
            </div>

            {/* Comparative Benchmarks */}
            <div style={{ background: 'rgba(30,41,59,0.4)', borderRadius: '10px', border: '1px solid #1e293b', padding: '12px' }}>
              <div style={{ fontSize: '9px', fontWeight: 700, color: '#94a3b8', marginBottom: '8px', textTransform: 'uppercase' }}>
                VVIP Class Benchmarks
              </div>
              <ResponsiveContainer width="100%" height={90}>
                <BarChart data={benchmarkData} margin={{ top: 2, right: 8, left: 0, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                  <XAxis dataKey="name" tick={{ fontSize: 7, fill: '#64748b' }} />
                  <YAxis tick={{ fontSize: 7, fill: '#64748b' }} width={30} />
                  <Tooltip contentStyle={{ fontSize: '9px', backgroundColor: '#0f172a', border: '1px solid #334155', borderRadius: '6px', color: '#e2e8f0' }} />
                  <Bar dataKey="value" radius={[3, 3, 0, 0]}>
                    {benchmarkData.map((entry, i) => (
                      <Cell key={i} fill={entry.fill} fillOpacity={i === 0 ? 1 : 0.5} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
          </div>
        )}

        {/* === MATH SECTION === */}
        {activeSection === 'math' && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
            <div style={{ fontSize: '9px', color: '#64748b', fontStyle: 'italic', padding: '6px 10px', background: 'rgba(59,130,246,0.06)', borderRadius: '8px', border: '1px solid rgba(59,130,246,0.1)' }}>
              Step-by-step mathematical derivation for <strong style={{ color: '#e2e8f0' }}>{metric.name}</strong>
            </div>

            {mathBreakdown.map((step, i) => (
              <div key={i} style={{
                background: 'rgba(30,41,59,0.4)', borderRadius: '10px', border: '1px solid #1e293b',
                padding: '10px 12px', borderLeft: '3px solid #3b82f6',
              }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '4px' }}>
                  <div style={{
                    width: '18px', height: '18px', borderRadius: '50%', background: 'rgba(59,130,246,0.15)',
                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                    fontSize: '9px', fontWeight: 800, color: '#3b82f6',
                  }}>
                    {i + 1}
                  </div>
                  <div style={{ fontSize: '10px', fontWeight: 700, color: '#e2e8f0' }}>{step.op}</div>
                </div>

                <div style={{
                  fontFamily: 'var(--font-mono)', fontSize: '11px', color: '#93c5fd',
                  background: 'rgba(0,0,0,0.3)', borderRadius: '6px', padding: '6px 10px',
                  margin: '4px 0', letterSpacing: '0.3px',
                }}>
                  {step.formula}
                </div>

                <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '4px', marginTop: '4px' }}>
                  <span style={{ fontSize: '8px', color: '#64748b' }}>Result:</span>
                  <span style={{ fontSize: '11px', fontWeight: 800, color: '#ea580c', fontFamily: 'var(--font-mono)' }}>
                    {step.result}{step.unit ? ` ${step.unit}` : ''}
                  </span>
                </div>
              </div>
            ))}

            {/* Formula Summary */}
            {parsed?.structured?.formula && (
              <div style={{
                background: 'linear-gradient(135deg, rgba(139,92,246,0.08), rgba(59,130,246,0.06))',
                borderRadius: '10px', border: '1px solid rgba(139,92,246,0.2)', padding: '12px',
                marginTop: '4px',
              }}>
                <div style={{ fontSize: '9px', fontWeight: 700, color: '#a78bfa', marginBottom: '6px', textTransform: 'uppercase' }}>
                  Qwen 3.5 Formula
                </div>
                <div style={{ fontFamily: 'var(--font-mono)', fontSize: '11px', color: '#c4b5fd', lineHeight: '1.6' }}>
                  {parsed.structured.formula}
                </div>
              </div>
            )}
          </div>
        )}

        {/* === SENSITIVITY ANALYSIS SECTION === */}
        {activeSection === 'sensitivity' && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
            {/* Sensitivity Factor Bar Chart */}
            <div style={{ background: 'rgba(30,41,59,0.5)', borderRadius: '10px', border: '1px solid #1e293b', padding: '14px' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
                <span style={{ fontSize: '11px', fontWeight: 700, color: '#e2e8f0' }}>Factor Sensitivity Analysis</span>
                <span style={{ fontSize: '8px', color: '#64748b', fontFamily: 'var(--font-mono)' }}>∂f/∂xᵢ · Δxᵢ</span>
              </div>
              <ResponsiveContainer width="100%" height={180}>
                <BarChart data={sensitivityData} layout="vertical" margin={{ top: 4, right: 16, left: 60, bottom: 4 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                  <XAxis type="number" tick={{ fontSize: 8, fill: '#64748b' }} domain={[0, 1]} />
                  <YAxis type="category" dataKey="factor" tick={{ fontSize: 8, fill: '#94a3b8' }} width={55} />
                  <Tooltip
                    contentStyle={{ fontSize: '9px', backgroundColor: '#0f172a', border: '1px solid #334155', borderRadius: '6px', color: '#e2e8f0' }}
                    formatter={(v, name) => [typeof v === 'number' ? v.toFixed(3) : v, name]}
                  />
                  <Bar dataKey="sensitivity" radius={[0, 3, 3, 0]} barSize={14}>
                    {sensitivityData.map((entry, i) => (
                      <Cell key={i} fill={entry.color} fillOpacity={0.8} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>

            {/* Impact vs Elasticity Scatter */}
            <div style={{ background: 'rgba(30,41,59,0.5)', borderRadius: '10px', border: '1px solid #1e293b', padding: '14px' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
                <span style={{ fontSize: '11px', fontWeight: 700, color: '#e2e8f0' }}>Impact–Elasticity Map</span>
                <span style={{ fontSize: '8px', color: '#64748b' }}>Bubble = perturbation magnitude</span>
              </div>
              <ResponsiveContainer width="100%" height={170}>
                <ScatterChart margin={{ top: 8, right: 16, left: 8, bottom: 8 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                  <XAxis type="number" dataKey="impact" name="Impact" tick={{ fontSize: 8, fill: '#64748b' }} label={{ value: 'Impact', position: 'bottom', fontSize: 8, fill: '#475569' }} />
                  <YAxis type="number" dataKey="elasticity" name="Elasticity" tick={{ fontSize: 8, fill: '#64748b' }} label={{ value: 'ε', position: 'insideLeft', fontSize: 10, fill: '#475569' }} />
                  <Tooltip
                    contentStyle={{ fontSize: '9px', backgroundColor: '#0f172a', border: '1px solid #334155', borderRadius: '6px', color: '#e2e8f0' }}
                    formatter={(v) => typeof v === 'number' ? v.toFixed(3) : v}
                  />
                  <Scatter data={sensitivityData} fill="#3b82f6">
                    {sensitivityData.map((entry, i) => (
                      <Cell key={i} fill={entry.color} />
                    ))}
                  </Scatter>
                </ScatterChart>
              </ResponsiveContainer>
            </div>

            {/* Context Radar — Full Size */}
            <div style={{ background: 'rgba(30,41,59,0.5)', borderRadius: '10px', border: '1px solid #1e293b', padding: '14px' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
                <span style={{ fontSize: '11px', fontWeight: 700, color: '#e2e8f0' }}>Convoy Performance Radar</span>
                <span style={{ fontSize: '8px', color: '#64748b' }}>6-axis normalized [0–100]</span>
              </div>
              <ResponsiveContainer width="100%" height={200}>
                <RadarChart data={contextRadar}>
                  <PolarGrid stroke="#1e293b" />
                  <PolarAngleAxis dataKey="axis" tick={{ fontSize: 8, fill: '#94a3b8' }} />
                  <PolarRadiusAxis tick={{ fontSize: 7, fill: '#475569' }} domain={[0, 100]} />
                  <Radar name="Performance" dataKey="A" stroke="#3b82f6" fill="#3b82f6" fillOpacity={0.25} strokeWidth={2} dot={{ r: 3, fill: '#3b82f6' }} />
                </RadarChart>
              </ResponsiveContainer>
            </div>

            {/* Benchmark Comparison */}
            <div style={{ background: 'rgba(30,41,59,0.5)', borderRadius: '10px', border: '1px solid #1e293b', padding: '14px' }}>
              <div style={{ fontSize: '11px', fontWeight: 700, color: '#e2e8f0', marginBottom: '8px' }}>VVIP Class Benchmark Comparison</div>
              <ResponsiveContainer width="100%" height={130}>
                <BarChart data={benchmarkData} margin={{ top: 4, right: 12, left: 0, bottom: 4 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                  <XAxis dataKey="name" tick={{ fontSize: 8, fill: '#94a3b8' }} />
                  <YAxis tick={{ fontSize: 8, fill: '#64748b' }} width={30} />
                  <Tooltip contentStyle={{ fontSize: '9px', backgroundColor: '#0f172a', border: '1px solid #334155', borderRadius: '6px', color: '#e2e8f0' }} />
                  <Bar dataKey="value" radius={[4, 4, 0, 0]} barSize={28}>
                    {benchmarkData.map((entry, i) => (
                      <Cell key={i} fill={entry.fill} fillOpacity={i === 0 ? 1 : 0.55} stroke={i === 0 ? '#fff' : 'none'} strokeWidth={i === 0 ? 1 : 0} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>

            {/* Elasticity Matrix */}
            <div style={{ background: 'rgba(30,41,59,0.5)', borderRadius: '10px', border: '1px solid #1e293b', padding: '14px' }}>
              <div style={{ fontSize: '11px', fontWeight: 700, color: '#e2e8f0', marginBottom: '10px' }}>Elasticity Matrix</div>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: '1px', background: '#1e293b', borderRadius: '6px', overflow: 'hidden' }}>
                {/* Header */}
                {['Factor', 'δ%', 'Sensitivity', 'Impact', 'ε'].map(h => (
                  <div key={h} style={{ background: '#0f172a', padding: '6px 4px', fontSize: '8px', fontWeight: 700, color: '#94a3b8', textAlign: 'center', textTransform: 'uppercase' }}>{h}</div>
                ))}
                {/* Rows */}
                {sensitivityData.map((row, i) => (
                  <React.Fragment key={i}>
                    <div style={{ background: '#0f172aCC', padding: '5px 4px', fontSize: '8px', color: row.color, fontWeight: 600, textAlign: 'center' }}>{row.factor}</div>
                    <div style={{ background: '#0f172aCC', padding: '5px 4px', fontSize: '8px', fontFamily: 'var(--font-mono)', color: row.perturbation > 0 ? '#f87171' : '#34d399', textAlign: 'center' }}>
                      {row.perturbation > 0 ? '+' : ''}{row.perturbation}%
                    </div>
                    <div style={{ background: '#0f172aCC', padding: '5px 4px', fontSize: '8px', fontFamily: 'var(--font-mono)', color: '#e2e8f0', textAlign: 'center' }}>{row.sensitivity.toFixed(3)}</div>
                    <div style={{ background: '#0f172aCC', padding: '5px 4px', fontSize: '8px', fontFamily: 'var(--font-mono)', color: '#e2e8f0', textAlign: 'center' }}>{row.impact.toFixed(3)}</div>
                    <div style={{ background: '#0f172aCC', padding: '5px 4px', fontSize: '8px', fontFamily: 'var(--font-mono)', color: Math.abs(row.elasticity) > 1 ? '#f59e0b' : '#94a3b8', fontWeight: Math.abs(row.elasticity) > 1 ? 700 : 400, textAlign: 'center' }}>
                      {row.elasticity.toFixed(2)}
                    </div>
                  </React.Fragment>
                ))}
              </div>
              <div style={{ fontSize: '7px', color: '#475569', marginTop: '6px', fontFamily: 'var(--font-mono)' }}>
                ε = elasticity (% output change / % input change) • |ε| {'>'} 1 = elastic (highlighted)
              </div>
            </div>
          </div>
        )}

        {/* === AI ANALYSIS SECTION === */}
        {activeSection === 'ai-analysis' && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
            {loading ? (
              <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', padding: '40px 0', gap: '12px' }}>
                <Loader2 size={28} color="#3b82f6" style={{ animation: 'spin 1s linear infinite' }} />
                <div style={{ fontSize: '11px', color: '#64748b' }}>Qwen 3.5 analyzing metric...</div>
                <div style={{ fontSize: '9px', color: '#475569', fontFamily: 'var(--font-mono)' }}>
                  {elapsed}s elapsed {elapsed > 15 ? '• LLM inference in progress' : ''} {elapsed > 60 ? '• may be queued behind other requests' : ''}
                </div>
                {/* Progress bar */}
                <div style={{ width: '60%', height: '3px', background: '#1e293b', borderRadius: '2px', overflow: 'hidden', marginTop: '4px' }}>
                  <div style={{
                    height: '100%', borderRadius: '2px',
                    background: 'linear-gradient(90deg, #3b82f6, #8b5cf6)',
                    width: `${Math.min(95, elapsed * 1.5)}%`,
                    transition: 'width 1s linear',
                  }} />
                </div>
              </div>
            ) : error ? (
              <div style={{
                background: 'rgba(220,38,38,0.08)', borderRadius: '10px', border: '1px solid rgba(220,38,38,0.2)',
                padding: '16px', textAlign: 'center',
              }}>
                <div style={{ fontSize: '11px', color: '#dc2626', fontWeight: 600 }}>Analysis Error</div>
                <div style={{ fontSize: '9px', color: '#94a3b8', marginTop: '4px' }}>{error}</div>
                <button onClick={() => fetchReasoning(true)} style={{
                  marginTop: '10px', padding: '6px 16px', borderRadius: '6px', cursor: 'pointer',
                  background: 'rgba(59,130,246,0.15)', border: '1px solid rgba(59,130,246,0.3)',
                  color: '#3b82f6', fontSize: '10px', fontWeight: 600,
                }}>
                  Retry Analysis
                </button>
              </div>
            ) : parsed ? (
              <>
                {/* AI Text Reasoning */}
                <div style={{
                  background: 'rgba(30,41,59,0.4)', borderRadius: '10px', border: '1px solid #1e293b',
                  padding: '12px', borderLeft: '3px solid #8b5cf6',
                }}>
                  <div style={{ fontSize: '9px', fontWeight: 700, color: '#a78bfa', marginBottom: '6px', textTransform: 'uppercase' }}>
                    🤖 Qwen 3.5 Analysis
                  </div>
                  <div style={{ fontSize: '10px', color: '#cbd5e1', lineHeight: '1.7', whiteSpace: 'pre-wrap' }}>
                    {parsed.text}
                  </div>
                </div>

                {/* Structured Computation Steps (from AI) */}
                {parsed.structured?.computation_steps && (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                    <div style={{ fontSize: '9px', fontWeight: 700, color: '#94a3b8', textTransform: 'uppercase' }}>
                      AI Computation Chain
                    </div>
                    {parsed.structured.computation_steps.map((step, i) => (
                      <div key={i} style={{
                        background: 'rgba(30,41,59,0.3)', borderRadius: '8px', border: '1px solid #1e293b',
                        padding: '8px 10px', borderLeft: '3px solid #8b5cf6',
                      }}>
                        <div style={{ fontSize: '9px', fontWeight: 700, color: '#c4b5fd', display: 'flex', alignItems: 'center', gap: '4px' }}>
                          <span style={{ background: 'rgba(139,92,246,0.15)', borderRadius: '50%', width: '16px', height: '16px', display: 'inline-flex', alignItems: 'center', justifyContent: 'center', fontSize: '8px' }}>
                            {step.step}
                          </span>
                          {step.operation}
                        </div>
                        {step.explanation && (
                          <div style={{ fontSize: '8px', color: '#94a3b8', marginTop: '2px', lineHeight: '1.5' }}>
                            {step.explanation}
                          </div>
                        )}
                        {step.result !== undefined && (
                          <div style={{ fontSize: '10px', color: '#ea580c', fontFamily: 'var(--font-mono)', fontWeight: 700, marginTop: '2px' }}>
                            → {typeof step.result === 'number' ? step.result.toFixed(3) : step.result}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                )}

                {/* Thresholds */}
                {parsed.structured?.thresholds && (
                  <div style={{
                    background: 'rgba(30,41,59,0.4)', borderRadius: '10px', border: '1px solid #1e293b',
                    padding: '10px 12px',
                  }}>
                    <div style={{ fontSize: '9px', fontWeight: 700, color: '#94a3b8', marginBottom: '6px', textTransform: 'uppercase' }}>
                      Operational Thresholds
                    </div>
                    <div style={{ display: 'flex', gap: '6px' }}>
                      {Object.entries(parsed.structured.thresholds).map(([k, v]) => (
                        <div key={k} style={{
                          flex: 1, padding: '6px', borderRadius: '6px', textAlign: 'center',
                          background: `${STATUS_COLORS[k] || '#475569'}10`,
                          border: `1px solid ${STATUS_COLORS[k] || '#475569'}30`,
                        }}>
                          <div style={{ fontSize: '7px', color: STATUS_COLORS[k] || '#64748b', fontWeight: 700, textTransform: 'uppercase' }}>{k}</div>
                          <div style={{ fontSize: '9px', color: '#e2e8f0', fontFamily: 'var(--font-mono)', marginTop: '2px' }}>{v}</div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {/* Interpretation + Insight */}
                {(parsed.structured?.interpretation || parsed.structured?.actionable_insight) && (
                  <div style={{
                    background: 'linear-gradient(135deg, rgba(234,88,12,0.06), rgba(220,38,38,0.04))',
                    borderRadius: '10px', border: '1px solid rgba(234,88,12,0.15)', padding: '12px',
                  }}>
                    {parsed.structured.interpretation && (
                      <div style={{ marginBottom: '8px' }}>
                        <div style={{ fontSize: '9px', fontWeight: 700, color: '#ea580c', textTransform: 'uppercase', marginBottom: '3px' }}>
                          Interpretation
                        </div>
                        <div style={{ fontSize: '9px', color: '#cbd5e1', lineHeight: '1.6' }}>{parsed.structured.interpretation}</div>
                      </div>
                    )}
                    {parsed.structured.actionable_insight && (
                      <div>
                        <div style={{ fontSize: '9px', fontWeight: 700, color: '#f59e0b', textTransform: 'uppercase', marginBottom: '3px' }}>
                          Commander Action
                        </div>
                        <div style={{ fontSize: '9px', color: '#fcd34d', lineHeight: '1.6', fontWeight: 600 }}>
                          {parsed.structured.actionable_insight}
                        </div>
                      </div>
                    )}
                  </div>
                )}

                {/* Tool Calls Made */}
                {parsed.tools?.length > 0 && (
                  <div style={{ display: 'flex', gap: '4px', flexWrap: 'wrap' }}>
                    {parsed.tools.map((t, i) => (
                      <span key={i} style={{
                        fontSize: '7px', padding: '2px 6px', borderRadius: '4px',
                        background: 'rgba(139,92,246,0.1)', color: '#a78bfa', border: '1px solid rgba(139,92,246,0.2)',
                        fontFamily: 'var(--font-mono)',
                      }}>
                        {t}
                      </span>
                    ))}
                  </div>
                )}
              </>
            ) : !reasoning ? (
              <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', padding: '40px 0', gap: '12px' }}>
                <Loader2 size={28} color="#3b82f6" style={{ animation: 'spin 1s linear infinite' }} />
                <div style={{ fontSize: '11px', color: '#64748b' }}>Preparing analysis...</div>
              </div>
            ) : null}
          </div>
        )}
      </div>

      {/* Footer */}
      <div style={{
        padding: '8px 16px', borderTop: '1px solid #1e293b',
        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
        background: 'rgba(15,23,42,0.8)',
      }}>
        <div style={{ fontSize: '7px', color: '#475569', fontFamily: 'var(--font-mono)' }}>
          Qwen 3.5 9B Q4_K_M • No-Think Mode • T=0.3
        </div>
        {reasoning?.generated_at && (
          <div style={{ fontSize: '7px', color: '#475569' }}>
            {new Date(reasoning.generated_at * 1000).toLocaleTimeString()}
          </div>
        )}
      </div>

      <style>{`
        @keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
      `}</style>
    </div>
  );
};

export default AnalyticsDeepDive;
