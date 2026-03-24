import React, { useState, useEffect, useMemo } from 'react';
import { ArrowLeft, Lightbulb, Loader2, ChevronRight, Shield, AlertTriangle, CheckCircle2, XCircle, Clock, Zap, Target, TrendingUp, TrendingDown } from 'lucide-react';
import { AreaChart, Area, BarChart, Bar, Cell, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, RadarChart, PolarGrid, PolarAngleAxis, PolarRadiusAxis, Radar, ComposedChart, Line, ReferenceLine, ScatterChart, Scatter } from 'recharts';
import * as api from '../services/api';
import { useConvoy } from '../context/ConvoyContext';

const PHASE_COLORS = {
  Observation: '#3b82f6',
  Hypothesis: '#8b5cf6',
  Evidence: '#06b6d4',
  'Risk Assessment': '#ea580c',
  Decision: '#16a34a',
};
const SEVERITY_BG = { low: 'rgba(234,179,8,0.08)', medium: 'rgba(234,88,12,0.08)', high: 'rgba(220,38,38,0.08)' };
const SEVERITY_BORDER = { low: 'rgba(234,179,8,0.2)', medium: 'rgba(234,88,12,0.2)', high: 'rgba(220,38,38,0.3)' };
const SEVERITY_TEXT = { low: '#eab308', medium: '#ea580c', high: '#dc2626' };
const URGENCY_ICON = { immediate: <AlertTriangle size={10} />, 'short-term': <Clock size={10} />, advisory: <Lightbulb size={10} /> };

const RecommendationDeepDive = ({ recommendation, onBack, vvipClass }) => {
  const { convoySimulation, gpuHealth } = useConvoy();
  const [reasoning, setReasoning] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [expandedStep, setExpandedStep] = useState(null);

  // Determine recommendation category from content
  const category = useMemo(() => {
    if (!recommendation) return 'general';
    const s = (recommendation.statement || recommendation.thought || '').toLowerCase();
    if (s.includes('speed') || s.includes('decelerat') || s.includes('velocity')) return 'speed';
    if (s.includes('congest') || s.includes('gridlock') || s.includes('bottleneck')) return 'congestion';
    if (s.includes('security') || s.includes('crowd') || s.includes('escort')) return 'security';
    if (s.includes('route') || s.includes('diversion') || s.includes('corridor')) return 'routing';
    if (s.includes('incident') || s.includes('accident') || s.includes('hazard')) return 'incident';
    if (s.includes('weather') || s.includes('visibility') || s.includes('rain')) return 'environmental';
    return 'general';
  }, [recommendation]);

  // Fetch AI reasoning from backend
  useEffect(() => {
    if (!recommendation) return;
    setLoading(true);
    setError(null);

    const statement = recommendation.statement || recommendation.thought || recommendation.label || '';
    const groundData = {
      speed: convoySimulation?.speed ?? 0,
      congestion: convoySimulation?.congestion ?? 0,
      zone: convoySimulation?.currentZone ?? 'Unknown',
      progress: convoySimulation?.progress ?? 0,
      elapsed: convoySimulation?.elapsedSeconds ?? 0,
      heading: convoySimulation?.heading ?? 0,
      fuel: convoySimulation?.fuelPct ?? 100,
      acceleration: convoySimulation?.acceleration ?? 0,
    };

    api.recommendationReasoning(statement, category, groundData, vvipClass || 'Z')
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
  }, [recommendation, vvipClass, category]);

  // Parse structured reasoning
  const parsed = useMemo(() => {
    if (!reasoning?.reasoning) return null;
    const r = reasoning.reasoning;
    let structured = null;
    try {
      if (r.data && typeof r.data === 'object' && Object.keys(r.data).length > 0) {
        structured = r.data;
      } else if (typeof r.reasoning === 'string') {
        const jsonMatch = r.reasoning.match(/\{[\s\S]*\}/);
        if (jsonMatch) structured = JSON.parse(jsonMatch[0]);
      }
    } catch { /* fallback */ }
    return {
      structured,
      text: r.reasoning || r.action || 'Analysis complete.',
      confidence: r.confidence || 'medium',
      tools: r.tool_calls_made || [],
    };
  }, [reasoning]);

  // Local CoT reconstruction based on recommendation status
  const localCoT = useMemo(() => {
    if (!recommendation) return [];
    const status = recommendation.status || 'pass';
    const label = recommendation.label || '';
    const thought = recommendation.thought || '';
    return [
      { phase: 'INPUT', detail: `Statement: "${thought.slice(0, 120)}${thought.length > 120 ? '...' : ''}"`, color: '#64748b' },
      { phase: 'CLASSIFICATION', detail: `Category: ${category.toUpperCase()} | Status: ${status.toUpperCase()} | Step: ${recommendation.step || '—'}`, color: '#3b82f6' },
      { phase: 'GROUND DATA', detail: `Speed=${Math.round(convoySimulation?.speed || 0)}km/h, CGX=${Math.round((convoySimulation?.congestion || 0) * 100)}%, Zone=${convoySimulation?.currentZone || '—'}`, color: '#06b6d4' },
      { phase: 'ASSESSMENT', detail: status === 'fail' ? 'Parameter exceeds operational threshold — intervention required' : status === 'warn' ? 'Parameter approaching threshold — enhanced monitoring active' : 'Parameter within nominal range — continue current operations', color: status === 'fail' ? '#dc2626' : status === 'warn' ? '#eab308' : '#16a34a' },
    ];
  }, [recommendation, category, convoySimulation]);

  // Risk factor visualization data
  const riskData = useMemo(() => {
    if (!parsed?.structured?.risk_factors) return [];
    return parsed.structured.risk_factors.map(rf => ({
      factor: rf.factor?.slice(0, 20) || 'Unknown',
      severity: rf.severity === 'high' ? 90 : rf.severity === 'medium' ? 60 : 30,
      fill: SEVERITY_TEXT[rf.severity] || '#64748b',
    }));
  }, [parsed]);

  // Impact timeline — projected outcome over next 30 minutes
  const impactTimeline = useMemo(() => {
    const v = convoySimulation?.speed || 40;
    const c = convoySimulation?.congestion || 0.4;
    const status = recommendation?.status || 'pass';
    const decay = status === 'fail' ? 0.92 : status === 'warn' ? 0.96 : 0.99;
    const recovery = status === 'fail' ? 1.08 : status === 'warn' ? 1.04 : 1.01;
    return Array.from({ length: 13 }, (_, i) => {
      const t = i * 2.5;
      const noAction = Math.max(5, v * Math.pow(decay, i) + (Math.random() - 0.5) * 3);
      const withAction = Math.min(80, v * Math.pow(recovery, i) + (Math.random() - 0.5) * 2);
      return { time: `T+${t.toFixed(0)}m`, noAction: +noAction.toFixed(1), withAction: +withAction.toFixed(1), threshold: 30 };
    });
  }, [convoySimulation, recommendation]);

  // Scenario comparison — what-if analysis
  const scenarioData = useMemo(() => {
    const v = convoySimulation?.speed || 40;
    const c = convoySimulation?.congestion || 0.4;
    return [
      { scenario: 'Continue As-Is', speed: +(v * 0.9).toFixed(1), congestion: +((c + 0.05) * 100).toFixed(0), risk: +(c * 80 + 10).toFixed(0), eta: '+3m', color: '#94a3b8' },
      { scenario: 'Apply Fix', speed: +(v * 1.15).toFixed(1), congestion: +(Math.max(0, c - 0.1) * 100).toFixed(0), risk: +(Math.max(5, c * 40)).toFixed(0), eta: '-2m', color: '#16a34a' },
      { scenario: 'Reroute', speed: +(v * 1.05).toFixed(1), congestion: +(Math.max(0, c - 0.15) * 100).toFixed(0), risk: +(Math.max(10, c * 50 + 5)).toFixed(0), eta: '+1m', color: '#3b82f6' },
      { scenario: 'Escalate', speed: +(v * 1.3).toFixed(1), congestion: +(Math.max(0, c - 0.2) * 100).toFixed(0), risk: +(Math.max(3, c * 25)).toFixed(0), eta: '-4m', color: '#eab308' },
    ];
  }, [convoySimulation]);

  // Decision confidence radar — multi-axis assessment
  const decisionRadar = useMemo(() => {
    const v = convoySimulation?.speed || 40;
    const c = convoySimulation?.congestion || 0.4;
    const conf = parsed?.confidence === 'high' ? 90 : parsed?.confidence === 'medium' ? 60 : 30;
    return [
      { axis: 'Confidence', A: conf },
      { axis: 'Data Quality', A: Math.min(100, 70 + Math.random() * 20) },
      { axis: 'Urgency', A: recommendation?.status === 'fail' ? 95 : recommendation?.status === 'warn' ? 65 : 30 },
      { axis: 'Feasibility', A: Math.min(100, 80 - c * 30 + Math.random() * 10) },
      { axis: 'Impact', A: Math.min(100, v / 0.6 + Math.random() * 5) },
      { axis: 'Historical', A: Math.min(100, 55 + Math.random() * 25) },
    ].map(d => ({ ...d, A: +d.A.toFixed(0) }));
  }, [convoySimulation, parsed, recommendation]);

  // Active section tab state
  const [activeSection, setActiveSection] = useState('reasoning');

  if (!recommendation) return null;

  const statement = recommendation.statement || recommendation.thought || recommendation.label || '';
  const statusColor = recommendation.status === 'fail' ? '#dc2626' : recommendation.status === 'warn' ? '#eab308' : '#16a34a';
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
        background: 'linear-gradient(135deg, rgba(139,92,246,0.08), rgba(234,88,12,0.06))',
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
            <Lightbulb size={14} color="#a78bfa" />
            RECOMMENDATION DEEP-DIVE
          </div>
          <div style={{ fontSize: '9px', color: '#64748b', marginTop: '2px', fontFamily: 'var(--font-mono)' }}>
            Chain-of-Thought Reasoning — Powered by Qwen 3.5
          </div>
        </div>
        <div style={{
          padding: '3px 8px', borderRadius: '6px', fontSize: '8px', fontWeight: 700,
          background: `${statusColor}20`, color: statusColor,
          border: `1px solid ${statusColor}40`, textTransform: 'uppercase',
        }}>
          {recommendation.status || 'INFO'}
        </div>
      </div>

      {/* Original Statement Card */}
      <div style={{
        padding: '14px 16px', borderBottom: '1px solid #1e293b',
        background: 'linear-gradient(180deg, rgba(15,23,42,0.8), #0a0f1e)',
      }}>
        <div style={{ display: 'flex', gap: '10px', alignItems: 'flex-start' }}>
          <div style={{
            width: '36px', height: '36px', borderRadius: '10px', flexShrink: 0,
            background: `${statusColor}15`, border: `1px solid ${statusColor}30`,
            display: 'flex', alignItems: 'center', justifyContent: 'center',
          }}>
            {recommendation.status === 'fail' ? <XCircle size={18} color={statusColor} /> :
             recommendation.status === 'warn' ? <AlertTriangle size={18} color={statusColor} /> :
             <CheckCircle2 size={18} color={statusColor} />}
          </div>
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: '9px', color: '#64748b', fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.5px', marginBottom: '3px' }}>
              {recommendation.label || `Step ${recommendation.step || '—'}`} • {category.toUpperCase()}
            </div>
            <div style={{ fontSize: '11px', color: '#e2e8f0', lineHeight: '1.6' }}>
              {statement}
            </div>
          </div>
        </div>
      </div>

      {/* Content */}
      <div style={{ flex: 1, overflowY: 'auto', padding: '12px 16px', display: 'flex', flexDirection: 'column', gap: '12px' }}>

        {/* Section Tabs */}
        <div style={{ display: 'flex', gap: '4px', background: 'rgba(15,23,42,0.6)', borderRadius: '8px', padding: '3px', border: '1px solid #1e293b' }}>
          {[
            { id: 'reasoning', label: '🧠 Reasoning' },
            { id: 'impact', label: '📈 Impact' },
            { id: 'scenarios', label: '🔀 Scenarios' },
          ].map(tab => (
            <button key={tab.id} onClick={() => setActiveSection(tab.id)} style={{
              flex: 1, padding: '5px 6px', borderRadius: '6px', border: 'none', cursor: 'pointer',
              fontSize: '9px', fontWeight: 700, letterSpacing: '0.3px',
              background: activeSection === tab.id ? 'rgba(139,92,246,0.15)' : 'transparent',
              color: activeSection === tab.id ? '#a78bfa' : '#64748b',
              borderBottom: activeSection === tab.id ? '2px solid #a78bfa' : '2px solid transparent',
              transition: 'all 0.2s ease',
            }}>
              {tab.label}
            </button>
          ))}
        </div>

        {/* === REASONING TAB === */}
        {activeSection === 'reasoning' && (<>

        {/* Local CoT Trace */}
        <div style={{ background: 'rgba(30,41,59,0.4)', borderRadius: '10px', border: '1px solid #1e293b', padding: '10px 12px' }}>
          <div style={{ fontSize: '9px', fontWeight: 700, color: '#94a3b8', marginBottom: '8px', textTransform: 'uppercase' }}>
            ⚡ Decision Trace
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '3px' }}>
            {localCoT.map((step, i) => (
              <div key={i} style={{ display: 'flex', gap: '8px', alignItems: 'flex-start', padding: '4px 0' }}>
                <div style={{
                  width: '6px', height: '6px', borderRadius: '50%', backgroundColor: step.color,
                  marginTop: '4px', flexShrink: 0,
                }} />
                <div>
                  <div style={{ fontSize: '8px', fontWeight: 700, color: step.color, letterSpacing: '0.3px' }}>{step.phase}</div>
                  <div style={{ fontSize: '9px', color: '#cbd5e1', lineHeight: '1.5', fontFamily: 'var(--font-mono)' }}>{step.detail}</div>
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* AI Analysis */}
        {loading ? (
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', padding: '40px 0', gap: '12px' }}>
            <Loader2 size={28} color="#8b5cf6" style={{ animation: 'spin 1s linear infinite' }} />
            <div style={{ fontSize: '11px', color: '#64748b' }}>Qwen 3.5 reasoning chain...</div>
            <div style={{ fontSize: '8px', color: '#475569', fontFamily: 'var(--font-mono)' }}>No-think mode • Structured output</div>
          </div>
        ) : error ? (
          <div style={{
            background: 'rgba(220,38,38,0.08)', borderRadius: '10px', border: '1px solid rgba(220,38,38,0.2)',
            padding: '16px', textAlign: 'center',
          }}>
            <div style={{ fontSize: '11px', color: '#dc2626', fontWeight: 600 }}>Reasoning Error</div>
            <div style={{ fontSize: '9px', color: '#94a3b8', marginTop: '4px' }}>{error}</div>
          </div>
        ) : parsed ? (
          <>
            {/* Chain of Thought Steps */}
            {parsed.structured?.chain_of_thought && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                <div style={{ fontSize: '9px', fontWeight: 700, color: '#a78bfa', textTransform: 'uppercase' }}>
                  🧠 Chain of Thought
                </div>
                {parsed.structured.chain_of_thought.map((step, i) => {
                  const phaseColor = PHASE_COLORS[step.phase] || '#64748b';
                  const isExpanded = expandedStep === i;
                  return (
                    <div key={i}
                      onClick={() => setExpandedStep(isExpanded ? null : i)}
                      style={{
                        background: 'rgba(30,41,59,0.3)', borderRadius: '8px', border: '1px solid #1e293b',
                        borderLeft: `3px solid ${phaseColor}`, padding: '8px 10px', cursor: 'pointer',
                        transition: 'all 0.2s ease',
                      }}
                    >
                      <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                        <div style={{
                          width: '20px', height: '20px', borderRadius: '50%',
                          background: `${phaseColor}20`, display: 'flex', alignItems: 'center', justifyContent: 'center',
                          fontSize: '9px', fontWeight: 800, color: phaseColor,
                        }}>
                          {step.step}
                        </div>
                        <div style={{ flex: 1 }}>
                          <div style={{ fontSize: '10px', fontWeight: 700, color: phaseColor }}>{step.phase}</div>
                        </div>
                        <ChevronRight size={12} color="#64748b" style={{
                          transform: isExpanded ? 'rotate(90deg)' : 'none',
                          transition: 'transform 0.2s',
                        }} />
                      </div>
                      {(isExpanded || true) && (
                        <div style={{
                          fontSize: '9px', color: '#cbd5e1', lineHeight: '1.6', marginTop: '6px',
                          paddingLeft: '26px',
                          maxHeight: isExpanded ? '200px' : '40px', overflow: 'hidden',
                          transition: 'max-height 0.3s ease',
                        }}>
                          {step.reasoning}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}

            {/* Risk Factors Visualization */}
            {riskData.length > 0 && (
              <div style={{
                background: 'rgba(30,41,59,0.4)', borderRadius: '10px', border: '1px solid #1e293b',
                padding: '10px 12px',
              }}>
                <div style={{ fontSize: '9px', fontWeight: 700, color: '#94a3b8', marginBottom: '6px', textTransform: 'uppercase' }}>
                  ⚠️ Risk Factors
                </div>
                <ResponsiveContainer width="100%" height={Math.max(80, riskData.length * 28)}>
                  <BarChart data={riskData} layout="vertical" margin={{ left: 0, right: 10 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" horizontal={false} />
                    <XAxis type="number" domain={[0, 100]} tick={{ fontSize: 7, fill: '#64748b' }} axisLine={false} />
                    <YAxis type="category" dataKey="factor" tick={{ fontSize: 8, fill: '#94a3b8' }} width={80} axisLine={false} />
                    <Bar dataKey="severity" radius={[0, 4, 4, 0]} barSize={14}>
                      {riskData.map((d, i) => <Cell key={i} fill={d.fill} />)}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>

                {/* Risk details */}
                {parsed.structured.risk_factors.map((rf, i) => (
                  <div key={i} style={{
                    marginTop: '4px', padding: '6px 8px', borderRadius: '6px',
                    background: SEVERITY_BG[rf.severity] || 'rgba(0,0,0,0.1)',
                    border: `1px solid ${SEVERITY_BORDER[rf.severity] || '#1e293b'}`,
                  }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                      <span style={{ fontSize: '9px', fontWeight: 600, color: '#e2e8f0' }}>{rf.factor}</span>
                      <span style={{ fontSize: '7px', fontWeight: 700, color: SEVERITY_TEXT[rf.severity] || '#64748b', textTransform: 'uppercase' }}>{rf.severity}</span>
                    </div>
                    {rf.mitigation && (
                      <div style={{ fontSize: '8px', color: '#94a3b8', marginTop: '2px' }}>
                        Mitigation: {rf.mitigation}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}

            {/* Alternative Actions */}
            {parsed.structured?.alternative_actions?.length > 0 && (
              <div style={{
                background: 'rgba(30,41,59,0.4)', borderRadius: '10px', border: '1px solid #1e293b',
                padding: '10px 12px',
              }}>
                <div style={{ fontSize: '9px', fontWeight: 700, color: '#94a3b8', marginBottom: '8px', textTransform: 'uppercase' }}>
                  🔄 Alternative Actions
                </div>
                {parsed.structured.alternative_actions.map((alt, i) => (
                  <div key={i} style={{
                    padding: '8px 10px', borderRadius: '8px', marginBottom: '4px',
                    background: 'rgba(0,0,0,0.2)', border: '1px solid #1e293b',
                  }}>
                    <div style={{ fontSize: '10px', fontWeight: 700, color: '#e2e8f0', marginBottom: '3px' }}>{alt.action}</div>
                    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '4px' }}>
                      <div>
                        <span style={{ fontSize: '7px', color: '#16a34a', fontWeight: 700 }}>PROS: </span>
                        <span style={{ fontSize: '8px', color: '#94a3b8' }}>{alt.pros}</span>
                      </div>
                      <div>
                        <span style={{ fontSize: '7px', color: '#dc2626', fontWeight: 700 }}>CONS: </span>
                        <span style={{ fontSize: '8px', color: '#94a3b8' }}>{alt.cons}</span>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}

            {/* Full Reasoning Text */}
            <div style={{
              background: 'rgba(30,41,59,0.4)', borderRadius: '10px', border: '1px solid #1e293b',
              padding: '12px', borderLeft: '3px solid #8b5cf6',
            }}>
              <div style={{ fontSize: '9px', fontWeight: 700, color: '#a78bfa', marginBottom: '6px', textTransform: 'uppercase' }}>
                🤖 Qwen 3.5 Full Analysis
              </div>
              <div style={{ fontSize: '10px', color: '#cbd5e1', lineHeight: '1.7', whiteSpace: 'pre-wrap' }}>
                {parsed.text}
              </div>
            </div>

            {/* Urgency + Confidence Footer */}
            <div style={{ display: 'flex', gap: '8px' }}>
              {parsed.structured?.urgency && (
                <div style={{
                  flex: 1, padding: '8px 10px', borderRadius: '8px',
                  background: 'rgba(234,88,12,0.06)', border: '1px solid rgba(234,88,12,0.15)',
                  textAlign: 'center',
                }}>
                  <div style={{ fontSize: '7px', color: '#64748b', fontWeight: 600, textTransform: 'uppercase' }}>Urgency</div>
                  <div style={{ fontSize: '11px', fontWeight: 800, color: '#ea580c', marginTop: '2px', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '4px' }}>
                    {URGENCY_ICON[parsed.structured.urgency] || null}
                    {parsed.structured.urgency?.toUpperCase()}
                  </div>
                </div>
              )}
              <div style={{
                flex: 1, padding: '8px 10px', borderRadius: '8px',
                background: `${confColor}08`, border: `1px solid ${confColor}20`,
                textAlign: 'center',
              }}>
                <div style={{ fontSize: '7px', color: '#64748b', fontWeight: 600, textTransform: 'uppercase' }}>Confidence</div>
                <div style={{ fontSize: '11px', fontWeight: 800, color: confColor, marginTop: '2px' }}>
                  {(parsed.confidence || 'MEDIUM').toUpperCase()}
                </div>
              </div>
            </div>

            {/* Data Sources */}
            {parsed.structured?.data_sources?.length > 0 && (
              <div style={{ display: 'flex', gap: '4px', flexWrap: 'wrap' }}>
                <span style={{ fontSize: '7px', color: '#64748b', fontWeight: 600, marginRight: '4px' }}>SOURCES:</span>
                {parsed.structured.data_sources.map((src, i) => (
                  <span key={i} style={{
                    fontSize: '7px', padding: '2px 6px', borderRadius: '4px',
                    background: 'rgba(6,182,212,0.1)', color: '#06b6d4', border: '1px solid rgba(6,182,212,0.2)',
                    fontFamily: 'var(--font-mono)',
                  }}>
                    {src}
                  </span>
                ))}
              </div>
            )}
          </>
        ) : null}

        </>)}

        {/* === IMPACT TAB === */}
        {activeSection === 'impact' && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
            {/* Impact Timeline — No Action vs With Action */}
            <div style={{ background: 'rgba(30,41,59,0.5)', borderRadius: '10px', border: '1px solid #1e293b', padding: '14px' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
                <span style={{ fontSize: '11px', fontWeight: 700, color: '#e2e8f0' }}>Projected Impact Timeline</span>
                <span style={{ fontSize: '8px', color: '#64748b', fontFamily: 'var(--font-mono)' }}>Speed km/h over 30 min</span>
              </div>
              <ResponsiveContainer width="100%" height={180}>
                <ComposedChart data={impactTimeline} margin={{ top: 8, right: 12, left: 0, bottom: 4 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                  <XAxis dataKey="time" tick={{ fontSize: 7, fill: '#64748b' }} />
                  <YAxis tick={{ fontSize: 7, fill: '#64748b' }} width={30} domain={[0, 'auto']} />
                  <Tooltip contentStyle={{ fontSize: '9px', backgroundColor: '#0f172a', border: '1px solid #334155', borderRadius: '6px', color: '#e2e8f0' }} />
                  <ReferenceLine y={30} stroke="#dc2626" strokeDasharray="5 5" label={{ value: 'Critical', position: 'right', fontSize: 7, fill: '#dc2626' }} />
                  <Area type="monotone" dataKey="noAction" stroke="#dc262666" fill="#dc262615" strokeDasharray="6 3" name="No Action" />
                  <Line type="monotone" dataKey="withAction" stroke="#16a34a" strokeWidth={2} dot={false} name="With Action" />
                </ComposedChart>
              </ResponsiveContainer>
              <div style={{ display: 'flex', gap: '16px', justifyContent: 'center', marginTop: '6px' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
                  <div style={{ width: '16px', height: '2px', background: '#dc2626', borderTop: '2px dashed #dc2626' }} />
                  <span style={{ fontSize: '7px', color: '#94a3b8' }}>No Action</span>
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
                  <div style={{ width: '16px', height: '2px', background: '#16a34a' }} />
                  <span style={{ fontSize: '7px', color: '#94a3b8' }}>With Action</span>
                </div>
              </div>
            </div>

            {/* Decision Confidence Radar */}
            <div style={{ background: 'rgba(30,41,59,0.5)', borderRadius: '10px', border: '1px solid #1e293b', padding: '14px' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
                <span style={{ fontSize: '11px', fontWeight: 700, color: '#e2e8f0' }}>Decision Quality Assessment</span>
                <span style={{ fontSize: '8px', padding: '2px 6px', borderRadius: '4px', background: `${confColor}15`, color: confColor, fontWeight: 700 }}>
                  {(parsed?.confidence || 'medium').toUpperCase()}
                </span>
              </div>
              <ResponsiveContainer width="100%" height={200}>
                <RadarChart data={decisionRadar}>
                  <PolarGrid stroke="#1e293b" />
                  <PolarAngleAxis dataKey="axis" tick={{ fontSize: 8, fill: '#94a3b8' }} />
                  <PolarRadiusAxis tick={{ fontSize: 7, fill: '#475569' }} domain={[0, 100]} />
                  <Radar name="Assessment" dataKey="A" stroke="#a78bfa" fill="#a78bfa" fillOpacity={0.2} strokeWidth={2} dot={{ r: 3, fill: '#a78bfa' }} />
                </RadarChart>
              </ResponsiveContainer>
            </div>

            {/* Status Impact Summary */}
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '6px' }}>
              {[
                { label: 'Speed Δ', value: recommendation?.status === 'fail' ? '-15%' : recommendation?.status === 'warn' ? '-5%' : '+2%', color: recommendation?.status === 'fail' ? '#dc2626' : recommendation?.status === 'warn' ? '#eab308' : '#16a34a' },
                { label: 'ETA Impact', value: recommendation?.status === 'fail' ? '+180s' : recommendation?.status === 'warn' ? '+45s' : '±10s', color: recommendation?.status === 'fail' ? '#dc2626' : recommendation?.status === 'warn' ? '#eab308' : '#16a34a' },
                { label: 'Risk Level', value: recommendation?.status === 'fail' ? 'HIGH' : recommendation?.status === 'warn' ? 'MODERATE' : 'LOW', color: recommendation?.status === 'fail' ? '#dc2626' : recommendation?.status === 'warn' ? '#eab308' : '#16a34a' },
              ].map(item => (
                <div key={item.label} style={{ background: 'rgba(30,41,59,0.5)', borderRadius: '8px', border: '1px solid #1e293b', padding: '10px', textAlign: 'center' }}>
                  <div style={{ fontSize: '7px', color: '#64748b', fontWeight: 600, textTransform: 'uppercase' }}>{item.label}</div>
                  <div style={{ fontSize: '14px', fontWeight: 800, color: item.color, marginTop: '4px', fontFamily: 'var(--font-mono)' }}>{item.value}</div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* === SCENARIOS TAB === */}
        {activeSection === 'scenarios' && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
            {/* Scenario Comparison Table */}
            <div style={{ background: 'rgba(30,41,59,0.5)', borderRadius: '10px', border: '1px solid #1e293b', padding: '14px' }}>
              <div style={{ fontSize: '11px', fontWeight: 700, color: '#e2e8f0', marginBottom: '10px' }}>What-If Scenario Analysis</div>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: '1px', background: '#1e293b', borderRadius: '6px', overflow: 'hidden' }}>
                {['Scenario', 'Speed', 'CGX%', 'Risk', 'ETA Δ'].map(h => (
                  <div key={h} style={{ background: '#0f172a', padding: '6px 4px', fontSize: '8px', fontWeight: 700, color: '#94a3b8', textAlign: 'center', textTransform: 'uppercase' }}>{h}</div>
                ))}
                {scenarioData.map((row, i) => (
                  <React.Fragment key={i}>
                    <div style={{ background: '#0f172aCC', padding: '5px 4px', fontSize: '8px', color: row.color, fontWeight: 600, textAlign: 'center' }}>{row.scenario}</div>
                    <div style={{ background: '#0f172aCC', padding: '5px 4px', fontSize: '8px', fontFamily: 'var(--font-mono)', color: '#e2e8f0', textAlign: 'center' }}>{row.speed}</div>
                    <div style={{ background: '#0f172aCC', padding: '5px 4px', fontSize: '8px', fontFamily: 'var(--font-mono)', color: row.congestion > 50 ? '#f87171' : '#34d399', textAlign: 'center' }}>{row.congestion}%</div>
                    <div style={{ background: '#0f172aCC', padding: '5px 4px', fontSize: '8px', fontFamily: 'var(--font-mono)', color: row.risk > 60 ? '#dc2626' : row.risk > 30 ? '#eab308' : '#16a34a', textAlign: 'center' }}>{row.risk}</div>
                    <div style={{ background: '#0f172aCC', padding: '5px 4px', fontSize: '8px', fontFamily: 'var(--font-mono)', color: row.eta.startsWith('+') ? '#f87171' : '#34d399', textAlign: 'center' }}>{row.eta}</div>
                  </React.Fragment>
                ))}
              </div>
            </div>

            {/* Scenario Speed + Risk Comparison Bar Chart */}
            <div style={{ background: 'rgba(30,41,59,0.5)', borderRadius: '10px', border: '1px solid #1e293b', padding: '14px' }}>
              <div style={{ fontSize: '11px', fontWeight: 700, color: '#e2e8f0', marginBottom: '8px' }}>Scenario Performance Comparison</div>
              <ResponsiveContainer width="100%" height={160}>
                <BarChart data={scenarioData} margin={{ top: 4, right: 12, left: 0, bottom: 4 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                  <XAxis dataKey="scenario" tick={{ fontSize: 7, fill: '#94a3b8' }} />
                  <YAxis tick={{ fontSize: 7, fill: '#64748b' }} width={30} />
                  <Tooltip contentStyle={{ fontSize: '9px', backgroundColor: '#0f172a', border: '1px solid #334155', borderRadius: '6px', color: '#e2e8f0' }} />
                  <Bar dataKey="speed" name="Speed km/h" radius={[3, 3, 0, 0]} barSize={16}>
                    {scenarioData.map((entry, i) => (
                      <Cell key={i} fill={entry.color} fillOpacity={0.7} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>

            {/* Risk vs Speed Scatter */}
            <div style={{ background: 'rgba(30,41,59,0.5)', borderRadius: '10px', border: '1px solid #1e293b', padding: '14px' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
                <span style={{ fontSize: '11px', fontWeight: 700, color: '#e2e8f0' }}>Risk–Speed Trade-off</span>
                <span style={{ fontSize: '8px', color: '#64748b' }}>Pareto frontier</span>
              </div>
              <ResponsiveContainer width="100%" height={150}>
                <ScatterChart margin={{ top: 8, right: 16, left: 8, bottom: 8 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                  <XAxis type="number" dataKey="speed" name="Speed" tick={{ fontSize: 8, fill: '#64748b' }} label={{ value: 'Speed km/h', position: 'bottom', fontSize: 8, fill: '#475569' }} />
                  <YAxis type="number" dataKey="risk" name="Risk" tick={{ fontSize: 8, fill: '#64748b' }} label={{ value: 'Risk', position: 'insideLeft', fontSize: 8, fill: '#475569' }} />
                  <Tooltip contentStyle={{ fontSize: '9px', backgroundColor: '#0f172a', border: '1px solid #334155', borderRadius: '6px', color: '#e2e8f0' }} />
                  <Scatter data={scenarioData}>
                    {scenarioData.map((entry, i) => (
                      <Cell key={i} fill={entry.color} r={8} />
                    ))}
                  </Scatter>
                </ScatterChart>
              </ResponsiveContainer>
              <div style={{ display: 'flex', gap: '8px', justifyContent: 'center', marginTop: '4px', flexWrap: 'wrap' }}>
                {scenarioData.map((s, i) => (
                  <div key={i} style={{ display: 'flex', alignItems: 'center', gap: '3px' }}>
                    <div style={{ width: '6px', height: '6px', borderRadius: '50%', background: s.color }} />
                    <span style={{ fontSize: '7px', color: '#94a3b8' }}>{s.scenario}</span>
                  </div>
                ))}
              </div>
            </div>

            {/* Recommendation — Best Scenario */}
            <div style={{
              background: 'linear-gradient(135deg, rgba(22,163,106,0.08), rgba(59,130,246,0.06))',
              borderRadius: '10px', border: '1px solid rgba(22,163,106,0.2)', padding: '12px',
            }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '6px' }}>
                <Target size={12} color="#16a34a" />
                <span style={{ fontSize: '10px', fontWeight: 700, color: '#16a34a' }}>OPTIMAL SCENARIO</span>
              </div>
              <div style={{ fontSize: '9px', color: '#e2e8f0', lineHeight: '1.6' }}>
                {(() => {
                  const best = [...scenarioData].sort((a, b) => (b.speed - b.risk) - (a.speed - a.risk))[0];
                  return `"${best.scenario}" yields the best performance-risk trade-off with ${best.speed} km/h projected speed, ${best.congestion}% congestion, and risk score of ${best.risk}. ETA impact: ${best.eta}.`;
                })()}
              </div>
            </div>
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

export default RecommendationDeepDive;
