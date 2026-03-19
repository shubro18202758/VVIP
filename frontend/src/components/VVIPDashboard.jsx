import React, { useState, useEffect, useMemo } from 'react';
import ConvoyMap from './ConvoyMap';
import SearchBar from './SearchBar';
import NotificationBell from './NotificationBell';
import LeftPanel from './LeftPanel';
import RightPanel from './RightPanel';
import AIReasoningPanel from './AIReasoningPanel';
import SecurityDossierPanel from './SecurityDossierPanel';
import { useConvoy } from '../context/ConvoyContext';
import { Navigation, Shield, Users, Radio, Activity, AlertTriangle, Cpu, Brain, TrendingUp, Gauge, FileText, Play, Pause, Square, MapPin } from 'lucide-react';

const VVIP_CLASSES = ['Z+', 'Z', 'Y', 'X'];

const VVIPDashboard = ({ navigate }) => {
  const {
    corridorSummary,
    activeMovements,
    anomalies,
    gpuHealth,
    backendHealth,
    lifecycle,
    planResult,
    movementId,
    mapSegments,
    highlightSegments,
    convoySimulation,
    startConvoySimulation,
    startDemoSimulation,
    stopConvoySimulation,
    pauseConvoySimulation,
  } = useConvoy();

  // --- UI States ---
  const [leftOpen, setLeftOpen] = useState(true);
  const [rightOpen, setRightOpen] = useState(true);
  const [clock, setClock] = useState(new Date());
  const [vvipClass, setVvipClass] = useState('Z+');
  const [overlays, setOverlays] = useState({ police: true, personnel: true, traffic: true, anomalies: true });
  const [mapStyle, setMapStyle] = useState('light');
  // Fleet size per VVIP class — must match FLEET_TEMPLATES in LeftPanel
  const FLEET_SIZE = { 'Z+': 6, 'Z': 4, 'Y': 3, 'X': 2 };
  const [selectedVehicles, setSelectedVehicles] = useState(() =>
    Array.from({ length: FLEET_SIZE['Z+'] }, (_, i) => i + 1)
  );
  const [origin, setOrigin] = useState("Raj Bhavan, Ahmedabad");
  const [destination, setDestination] = useState("SVPI Airport, Ahmedabad");
  const [notifOpen, setNotifOpen] = useState(false);
  const [reasoningOpen, setReasoningOpen] = useState(false);
  const [dossierOpen, setDossierOpen] = useState(false);

  // Reset selected vehicles when VVIP class changes
  useEffect(() => {
    const count = FLEET_SIZE[vvipClass] || 4;
    setSelectedVehicles(Array.from({ length: count }, (_, i) => i + 1));
  }, [vvipClass]);

  useEffect(() => {
    const clockTimer = setInterval(() => setClock(new Date()), 1000);
    return () => clearInterval(clockTimer);
  }, []);

  // Derived metrics
  const systemStatus = useMemo(() => {
    const ollamaUp = backendHealth?.ollama === 'connected';
    const brainUp = backendHealth?.status === 'ok' || backendHealth?.status === 'degraded';
    const gpuOk = gpuHealth && gpuHealth.vramUsedMb < gpuHealth.vramTotalMb * 0.92;
    return {
      ollamaUp,
      brainUp,
      gpuOk,
      overall: ollamaUp && brainUp ? 'operational' : brainUp ? 'degraded' : 'offline',
    };
  }, [backendHealth, gpuHealth]);

  const corridorTrend = useMemo(() => {
    if (!corridorSummary) return null;
    const cgx = corridorSummary.avg_congestion_idx || 0;
    if (cgx < 0.3) return { label: 'CLEAR', color: '#16a34a' };
    if (cgx < 0.6) return { label: 'MODERATE', color: '#ea580c' };
    if (cgx < 0.8) return { label: 'HEAVY', color: '#dc2626' };
    return { label: 'GRIDLOCK', color: '#991b1b' };
  }, [corridorSummary]);

  return (
    <div className="h-full w-full relative flex flex-column overflow-hidden" style={{ display: 'flex', flexDirection: 'column', height: '100vh', width: '100vw' }}>
      
      {/* Saffron accent line */}
      <div className="saffron-sweep" style={{ flexShrink: 0 }} />

      {/* Top Bar */}
      <div className="z-30" style={{ 
        display: 'grid',
        gridTemplateColumns: '1fr auto 1fr',
        alignItems: 'center',
        height: '58px', 
        background: 'linear-gradient(135deg, #0f172a 0%, #1e293b 50%, #0f172a 100%)',
        backdropFilter: 'blur(16px)',
        WebkitBackdropFilter: 'blur(16px)',
        borderBottom: '1px solid #334155',
        padding: '0 20px',
        flexShrink: 0,
        boxShadow: '0 4px 20px rgba(0,0,0,0.3)',
      }}>
        {/* Branding (Left) */}
        <div className="flex items-center" style={{ gap: '16px' }}>
          <div style={{ 
            width: '42px', 
            height: '42px', 
            borderRadius: '12px',
            background: 'linear-gradient(135deg, #fff7ed 0%, #ffedd5 100%)',
            border: '1px solid #fed7aa',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            boxShadow: '0 2px 8px rgba(234, 88, 12, 0.12)',
            transition: 'transform 0.2s',
          }}>
            <svg viewBox="0 0 100 100" style={{ width: '28px', height: '28px' }}>
              <path 
                d="M 62.0 0.0 L 66.6 1.4 L 66.8 2.6 L 72.1 4.2 L 77.0 3.9 L 78.9 5.4 L 78.4 8.6 L 82.4 10.4 L 83.1 13.5 L 85.4 16.3 L 87.4 20.2 L 90.9 21.1 L 93.9 22.6 L 96.8 24.6 L 100.0 29.1 L 97.8 33.4 L 94.0 36.0 L 95.5 36.8 L 97.5 37.7 L 94.5 38.4 L 95.4 41.9 L 95.8 44.1 L 90.6 47.6 L 90.4 50.7 L 92.9 51.6 L 95.5 51.2 L 98.7 51.3 L 94.6 52.5 L 91.1 55.7 L 87.4 57.8 L 91.1 60.2 L 92.1 64.4 L 89.5 66.8 L 85.2 65.7 L 84.1 66.6 L 83.8 70.6 L 83.1 73.0 L 80.3 73.1 L 78.8 72.5 L 77.9 71.0 L 76.1 72.8 L 72.9 74.1 L 75.6 69.5 L 74.7 64.3 L 71.6 56.4 L 71.7 51.3 L 71.4 49.1 L 71.6 44.5 L 74.9 40.3 L 69.3 38.7 L 64.9 43.6 L 65.5 48.9 L 63.1 54.6 L 61.2 57.4 L 59.8 58.2 L 58.2 58.5 L 57.2 59.2 L 55.7 60.1 L 52.0 62.1 L 47.4 63.6 L 42.7 64.8 L 39.3 63.6 L 37.5 63.1 L 32.5 60.2 L 17.2 45.6 L 11.8 40.0 L 13.2 36.6 L 13.8 37.5 L 15.2 37.3 L 15.2 37.9 L 14.8 40.1 L 17.5 39.2 L 21.2 37.8 L 23.4 37.5 L 24.6 36.8 L 25.4 36.4 L 27.2 36.4 L 28.0 35.8 L 27.9 35.5 L 28.4 35.4 L 31.1 34.5 L 32.1 32.9 L 35.2 29.2 L 35.3 29.8 L 35.1 30.7 L 36.1 30.1 L 36.4 29.3 L 36.4 28.7 L 39.2 24.1 L 34.9 24.2 L 32.3 25.2 L 31.0 27.1 L 29.4 28.5 L 29.9 28.9 L 27.2 29.6 L 23.9 30.8 L 21.6 30.7 L 19.9 30.9 L 18.9 30.8 L 15.5 30.1 L 12.6 28.5 L 6.4 23.6 L 6.2 22.5 L 2.7 20.6 L 0.0 18.1 L 8.7 8.2 L 30.9 6.6 L 42.2 7.8 L 44.9 1.2 L 55.1 0.9 L 60.6 0.9 L 76.6 71.4 Z" 
                fill="#ea580c" 
                stroke="#c2410c" 
                strokeWidth="2.5"
                strokeLinejoin="round"
              />
            </svg>
          </div>
          <div>
            <h1 style={{ fontSize: '14px', fontWeight: 700, color: '#f1f5f9', margin: 0, letterSpacing: '0.5px' }}>VVIP Convoy Command</h1>
            <p style={{ fontSize: '10px', color: '#64748b', margin: 0 }}>Gujarat Police · Mobility Intelligence Platform</p>
          </div>
          
          <div style={{ marginLeft: '12px', borderLeft: '1px solid #334155', paddingLeft: '24px', display: 'flex', alignItems: 'center', gap: '8px' }}>
            <div style={{ 
              display: 'flex', 
              backgroundColor: 'rgba(15, 23, 42, 0.6)', 
              borderRadius: '8px', 
              padding: '2px',
              border: '1px solid #334155'
            }}>
              {['light', 'live'].map((style) => (
                <button
                  key={style}
                  onClick={() => setMapStyle(style)}
                  style={{
                    padding: '4px 12px',
                    borderRadius: '6px',
                    fontSize: '10px',
                    fontWeight: 700,
                    textTransform: 'uppercase',
                    cursor: 'pointer',
                    border: 'none',
                    transition: 'all 0.2s cubic-bezier(0.4, 0, 0.2, 1)',
                    backgroundColor: mapStyle === style ? '#ea580c' : 'transparent',
                    color: mapStyle === style ? '#ffffff' : '#94a3b8',
                    boxShadow: mapStyle === style ? '0 4px 12px rgba(234, 88, 12, 0.2)' : 'none',
                  }}
                >
                  {style}
                </button>
              ))}
            </div>
            <span style={{ fontSize: '9px', color: '#64748b', fontWeight: 600 }}>MAP VIEW</span>
          </div>
        </div>

        {/* Search Bar (Center) */}
        <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', gap: '12px' }}>
          <SearchBar onSelect={(item) => console.log("Selected:", item)} />
          <button 
            onClick={() => navigate('comms')}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: '8px',
              padding: '8px 18px',
              background: 'linear-gradient(135deg, #1e293b 0%, #0f172a 100%)',
              color: 'white',
              border: 'none',
              borderRadius: '10px',
              cursor: 'pointer',
              fontWeight: 700,
              fontSize: '12px',
              transition: 'all 0.2s',
              boxShadow: '0 2px 8px rgba(0,0,0,0.15)',
              letterSpacing: '0.5px',
            }}
          >
            <Radio size={14} /> COMMS
          </button>
        </div>

        {/* Status Strip (Right) */}
        <div className="flex items-center gap-4" style={{ justifySelf: 'end' }}>
          <div className="flex items-center" style={{ gap: '20px' }}>
            {/* System status indicators */}
            <div className="flex items-center" style={{ gap: '6px', cursor: 'help' }} title={`Qwen 3.5: ${systemStatus.ollamaUp ? 'Connected' : 'Unreachable'}`}>
              <div style={{ position: 'relative', width: '10px', height: '10px' }}>
                <div style={{
                  width: '8px', height: '8px', borderRadius: '50%',
                  backgroundColor: systemStatus.ollamaUp ? '#16a34a' : '#dc2626',
                  position: 'absolute', top: '1px', left: '1px', zIndex: 2,
                }} />
                {systemStatus.ollamaUp && <div style={{
                  position: 'absolute', top: '-1px', left: '-1px', width: '12px', height: '12px', borderRadius: '50%',
                  border: '2px solid rgba(22,163,74,0.4)', animation: 'pulse 2s ease-in-out infinite',
                }} />}
              </div>
              <span style={{ fontSize: '9px', color: systemStatus.ollamaUp ? '#16a34a' : '#dc2626', fontWeight: 700, letterSpacing: '0.5px' }}>LLM</span>
            </div>
            <div className="flex items-center" style={{ gap: '6px', cursor: 'help' }} title={`GPU VRAM: ${gpuHealth ? `${gpuHealth.vramUsedMb}/${gpuHealth.vramTotalMb}MB` : 'N/A'}`}>
              <div style={{ position: 'relative', width: '10px', height: '10px' }}>
                <div style={{
                  width: '8px', height: '8px', borderRadius: '50%',
                  backgroundColor: systemStatus.gpuOk ? '#16a34a' : '#ea580c',
                  position: 'absolute', top: '1px', left: '1px', zIndex: 2,
                }} />
                {systemStatus.gpuOk && <div style={{
                  position: 'absolute', top: '-1px', left: '-1px', width: '12px', height: '12px', borderRadius: '50%',
                  border: '2px solid rgba(22,163,74,0.4)', animation: 'pulse 2s ease-in-out infinite 0.5s',
                }} />}
              </div>
              <span style={{ fontSize: '9px', color: systemStatus.gpuOk ? '#16a34a' : '#ea580c', fontWeight: 700, letterSpacing: '0.5px' }}>GPU</span>
            </div>
            <div style={{ width: '1px', height: '18px', backgroundColor: '#334155' }} />
            <div className="flex items-center" style={{ gap: '5px' }}>
              <span style={{ fontSize: '14px', fontWeight: 800, color: '#16a34a', fontFamily: 'var(--font-mono)', textShadow: '0 0 8px rgba(22,163,74,0.4)' }}>{activeMovements.length}</span>
              <span style={{ fontSize: '9px', color: '#94a3b8', fontWeight: 600, letterSpacing: '0.5px' }}>ACTIVE</span>
            </div>
            <div className="flex items-center" style={{ gap: '5px' }}>
              <AlertTriangle size={13} color={anomalies.length > 5 ? '#dc2626' : '#ea580c'} strokeWidth={2.5} />
              <span style={{ fontSize: '14px', fontWeight: 800, color: anomalies.length > 5 ? '#dc2626' : '#ea580c', fontFamily: 'var(--font-mono)', textShadow: `0 0 8px ${anomalies.length > 5 ? 'rgba(220,38,38,0.4)' : 'rgba(234,88,12,0.4)'}` }}>{anomalies.length}</span>
              <span style={{ fontSize: '9px', color: '#94a3b8', fontWeight: 600, letterSpacing: '0.5px' }}>ALERTS</span>
            </div>
          </div>

          {/* VVIP Class selector */}
          <div className="flex items-center p-1" style={{ backgroundColor: 'rgba(15, 23, 42, 0.6)', borderRadius: '20px', boxShadow: 'inset 0 1px 3px rgba(0,0,0,0.3)', border: '1px solid #334155' }}>
            {VVIP_CLASSES.map(cls => {
              const isActive = vvipClass === cls;
              const clsColor = cls === 'Z+' ? '#dc2626' : cls === 'Z' ? '#ea580c' : cls === 'Y' ? '#2563eb' : '#64748b';
              return (
              <button
                key={cls}
                onClick={() => setVvipClass(cls)}
                style={{
                  padding: '4px 12px',
                  fontSize: '9px',
                  fontWeight: 800,
                  borderRadius: '16px',
                  border: 'none',
                  cursor: 'pointer',
                  backgroundColor: isActive ? clsColor : 'transparent',
                    color: isActive ? 'white' : '#94a3b8',
                  transition: 'all 0.25s cubic-bezier(0.4, 0, 0.2, 1)',
                  boxShadow: isActive ? `0 2px 8px ${clsColor}40` : 'none',
                  transform: isActive ? 'scale(1.05)' : 'scale(1)',
                  letterSpacing: '0.5px',
                }}
              >
                {cls}
              </button>
              );
            })}
          </div>

          <NotificationBell 
            alerts={anomalies} 
            open={notifOpen} 
            onToggle={() => setNotifOpen(!notifOpen)} 
          />

          <div className="flex items-center gap-3" style={{ borderLeft: '1px solid #334155', paddingLeft: '16px' }}>
            <div style={{ textAlign: 'right' }}>
              <div style={{ fontSize: '14px', fontWeight: 700, fontFamily: 'var(--font-mono)', color: '#f1f5f9', letterSpacing: '1px' }}>
                {clock.toLocaleTimeString('en-IN', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' })}
              </div>
              <div style={{ fontSize: '9px', color: '#64748b', fontWeight: 600, letterSpacing: '0.3px' }}>
                {clock.toLocaleDateString('en-IN', { weekday: 'short', day: '2-digit', month: 'short' })} · IST
              </div>
            </div>
            <div style={{
              position: 'relative', width: '10px', height: '10px',
            }}>
              <div style={{ width: '8px', height: '8px', borderRadius: '50%', backgroundColor: '#16a34a', position: 'absolute', top: '1px', left: '1px' }} />
              <div style={{
                position: 'absolute', top: '-1px', left: '-1px', width: '12px', height: '12px', borderRadius: '50%',
                border: '2px solid rgba(22,163,74,0.3)', animation: 'pulse 2s ease-in-out infinite 1s',
              }} />
            </div>
          </div>
        </div>
      </div>

      {/* Main Container */}
      <div className="relative" style={{ flex: 1 }}>
        <ConvoyMap 
          overlays={overlays}
          routeData={{ origin, destination }}
          mapStyle={mapStyle}
          movements={activeMovements}
        />

        {/* Live Corridor Stats Bar */}
        <div className="animate-fadeIn depth-card" style={{
          position: 'absolute',
          top: '12px',
          left: '50%',
          transform: 'translateX(-50%)',
          backgroundColor: 'rgba(15, 23, 42, 0.92)',
          backdropFilter: 'blur(16px)',
          WebkitBackdropFilter: 'blur(16px)',
          padding: '10px 24px',
          borderRadius: '24px',
          border: '1px solid rgba(51, 65, 85, 0.6)',
          boxShadow: '0 4px 20px rgba(0,0,0,0.3), 0 0 0 1px rgba(234,88,12,0.08)',
          zIndex: 1100,
          display: 'flex',
          alignItems: 'center',
          gap: '16px',
          fontSize: '11px',
          fontWeight: 600,
          color: '#e2e8f0',
          overflow: 'hidden',
        }}>
          {/* scanline effect */}
          <div style={{
            position: 'absolute', inset: 0, pointerEvents: 'none', overflow: 'hidden', borderRadius: '24px',
          }}>
            <div style={{
              position: 'absolute', top: 0, left: 0, right: 0, height: '200%',
              background: 'linear-gradient(180deg, transparent 0%, rgba(234,88,12,0.03) 50%, transparent 100%)',
              animation: 'scanline 4s linear infinite',
            }} />
          </div>
          {corridorSummary ? (
            <>
              <div className="flex items-center gap-1" title="Average Corridor Speed">
                <Navigation size={14} color="#2563eb" /> 
                {corridorSummary.avg_speed_kmh?.toFixed(1) || '--'} km/h
              </div>
              <div style={{ width: '1px', height: '14px', backgroundColor: '#334155' }} />
              <div className="flex items-center gap-1" title="Congestion Index (0-1)">
                <Activity size={14} color={corridorSummary.avg_congestion_idx > 0.7 ? '#ea580c' : '#16a34a'} />
                CGX: {corridorSummary.avg_congestion_idx?.toFixed(3) || '0.000'}
              </div>
              <div style={{ width: '1px', height: '14px', backgroundColor: '#334155' }} />
              <div className="flex items-center gap-1" title="Critical Segments — click to highlight on map" 
                style={{ cursor: corridorSummary.critical_segments > 0 ? 'pointer' : 'default' }}
                onClick={() => {
                  if (corridorSummary.critical_segments > 0 && mapSegments?.length) {
                    const criticalIds = mapSegments
                      .filter(s => (s.congestion_idx ?? s.congestion) > 0.8)
                      .map(s => s.segment_id);
                    if (criticalIds.length) highlightSegments(criticalIds);
                  }
                }}
              >
                <AlertTriangle size={14} color={corridorSummary.critical_segments > 0 ? '#dc2626' : '#94a3b8'} />
                {corridorSummary.critical_segments || 0} Critical
              </div>
              <div style={{ width: '1px', height: '14px', backgroundColor: '#334155' }} />
              <div className="flex items-center gap-1" title="Total Segments Monitored">
                <Gauge size={14} color="#64748b" />
                {corridorSummary.total_segments || '--'} Segs
              </div>
              <div style={{ width: '1px', height: '14px', backgroundColor: '#334155' }} />
              {corridorTrend && (
                <div className="flex items-center gap-1" title="Corridor Flow Status">
                  <TrendingUp size={14} color={corridorTrend.color} />
                  <span style={{ color: corridorTrend.color, fontWeight: 800, textTransform: 'uppercase', letterSpacing: '0.5px' }}>
                    {corridorTrend.label}
                  </span>
                </div>
              )}
              <div style={{ width: '1px', height: '14px', backgroundColor: '#e2e8f0' }} />
              <div style={{ 
                color: corridorSummary.status === 'green' ? '#16a34a' : corridorSummary.status === 'amber' ? '#ea580c' : '#dc2626', 
                fontWeight: 800,
                textTransform: 'uppercase',
                fontSize: '10px',
              }}>
                {corridorSummary.status?.toUpperCase() || 'SYNCING'}
              </div>
              {/* Lifecycle badge */}
              {lifecycle !== 'idle' && (
                <>
                  <div style={{ width: '1px', height: '14px', backgroundColor: '#334155' }} />
                  <div style={{
                    padding: '2px 8px',
                    borderRadius: '10px',
                    fontSize: '9px',
                    fontWeight: 800,
                    textTransform: 'uppercase',
                    backgroundColor: lifecycle === 'active' ? 'rgba(22,163,74,0.2)' : lifecycle === 'approved' ? 'rgba(234,179,8,0.2)' : 'rgba(100,116,139,0.2)',
                    color: lifecycle === 'active' ? '#4ade80' : lifecycle === 'approved' ? '#facc15' : '#94a3b8',
                    border: lifecycle === 'active' ? '1px solid rgba(22,163,74,0.4)' : lifecycle === 'approved' ? '1px solid rgba(234,179,8,0.4)' : '1px solid #334155',
                    animation: lifecycle === 'active' ? 'securityPulse 2s ease-in-out infinite' : 'none',
                    boxShadow: lifecycle === 'active' ? '0 0 12px rgba(22,163,74,0.3)' : 'none',
                  }}>
                    {lifecycle === 'active' ? 'ESCORT LIVE' : lifecycle === 'approved' ? 'PLAN APPROVED' : lifecycle.toUpperCase()}
                  </div>
                </>
              )}
            </>
          ) : (
            <div className="flex items-center gap-2">
              <div className="animate-spin" style={{ width: '12px', height: '12px', border: '2px solid #2563eb', borderTopColor: 'transparent', borderRadius: '50%' }} />
              <span>SYNCING CORRIDOR TELEMETRY...</span>
            </div>
          )}
        </div>

        <div className="animate-fadeIn" style={{
          position: 'absolute',
          bottom: '12px',
          left: leftOpen ? '332px' : '12px',
          right: rightOpen ? '352px' : '12px',
          zIndex: 1100,
          display: 'flex',
          flexWrap: 'wrap',
          alignItems: 'center',
          justifyContent: 'center',
          gap: '4px',
          padding: '6px 10px',
          background: 'rgba(15,23,42,0.88)',
          borderRadius: '16px',
          border: '1px solid rgba(255,255,255,0.08)',
          transition: 'left 0.3s cubic-bezier(0.4, 0, 0.2, 1), right 0.3s cubic-bezier(0.4, 0, 0.2, 1)',
        }}>
          <button
            onClick={() => setReasoningOpen(v => !v)}
            style={{
              padding: '8px 16px', borderRadius: '20px',
              background: reasoningOpen ? 'linear-gradient(135deg, #7c3aed, #6d28d9)' : 'rgba(15,23,42,0.85)',
              backdropFilter: 'blur(8px)',
              border: `1px solid ${reasoningOpen ? '#7c3aed' : '#334155'}`,
              color: reasoningOpen ? '#ffffff' : '#94a3b8',
              fontSize: '11px', fontWeight: 700, cursor: 'pointer',
              display: 'flex', alignItems: 'center', gap: '6px',
              boxShadow: reasoningOpen ? '0 4px 14px rgba(124,58,237,0.35)' : '0 4px 12px rgba(0,0,0,0.08)',
              transition: 'all 0.25s cubic-bezier(0.4, 0, 0.2, 1)',
            }}
          >
            <Brain size={14} /> AI Trace
          </button>
          <button
            onClick={() => setDossierOpen(v => !v)}
            style={{
              padding: '8px 16px', borderRadius: '20px',
              background: dossierOpen ? 'linear-gradient(135deg, #dc2626, #b91c1c)' : 'rgba(15,23,42,0.85)',
              backdropFilter: 'blur(8px)',
              border: `1px solid ${dossierOpen ? '#dc2626' : '#334155'}`,
              color: dossierOpen ? '#ffffff' : '#94a3b8',
              fontSize: '11px', fontWeight: 700, cursor: 'pointer',
              display: 'flex', alignItems: 'center', gap: '6px',
              boxShadow: dossierOpen ? '0 4px 14px rgba(220,38,38,0.35)' : '0 4px 12px rgba(0,0,0,0.08)',
              transition: 'all 0.25s cubic-bezier(0.4, 0, 0.2, 1)',
            }}
          >
            <FileText size={14} /> Dossier
          </button>
          {[
            { key: 'police', icon: <Shield size={14} />, label: 'Police Stn.', activeColor: '#2563eb' },
            { key: 'personnel', icon: <Users size={14} />, label: 'Personnel', activeColor: '#16a34a' },
            { key: 'traffic', icon: <Activity size={14} />, label: 'Traffic Flow', activeColor: '#ea580c' },
            { key: 'anomalies', icon: <AlertTriangle size={14} />, label: 'Anomalies', activeColor: '#dc2626' },
          ].map(({ key, icon, label, activeColor }) => (
            <button
              key={key}
              onClick={() => setOverlays(o => ({ ...o, [key]: !o[key] }))}
              style={{
                padding: '8px 16px',
                borderRadius: '20px',
                background: overlays[key] ? `linear-gradient(135deg, ${activeColor}, ${activeColor}dd)` : 'rgba(15,23,42,0.85)',
                backdropFilter: 'blur(8px)',
                border: `1px solid ${overlays[key] ? activeColor : '#334155'}`,
                color: overlays[key] ? '#ffffff' : '#94a3b8',
                fontSize: '11px',
                fontWeight: 700,
                cursor: 'pointer',
                display: 'flex',
                alignItems: 'center',
                gap: '6px',
                boxShadow: overlays[key] ? `0 4px 14px ${activeColor}40` : '0 4px 12px rgba(0,0,0,0.08)',
                transition: 'all 0.25s cubic-bezier(0.4, 0, 0.2, 1)',
              }}
            >
              {icon} {label}
            </button>
          ))}

          {/* ═══ CONVOY SIMULATION CONTROLS ═══ */}
          <div style={{ width: '1px', height: '24px', background: '#475569', margin: '0 4px' }} />
          {!convoySimulation?.active && planResult?.primary_route?.segment_ids?.length > 0 && (
            <button
              className="convoy-ctrl-btn"
              onClick={() => startConvoySimulation(planResult.primary_route.segment_ids)}
              style={{
                background: 'linear-gradient(135deg, #ea580c, #c2410c)',
                border: '1px solid #f97316',
                color: '#fff',
                borderRadius: '20px',
                boxShadow: '0 4px 14px rgba(234,88,12,0.4)',
              }}
            >
              <Play size={14} /> Launch Convoy
            </button>
          )}
          {!convoySimulation?.active && !planResult?.primary_route?.segment_ids?.length && mapSegments.length > 0 && (
            <button
              className="convoy-ctrl-btn"
              onClick={startDemoSimulation}
              style={{
                background: 'linear-gradient(135deg, #ea580c, #c2410c)',
                border: '1px solid #f97316',
                color: '#fff',
                borderRadius: '20px',
                boxShadow: '0 4px 14px rgba(234,88,12,0.4)',
              }}
            >
              <Play size={14} /> Demo Convoy
            </button>
          )}
          {convoySimulation?.active && (
            <>
              <button
                className={`convoy-ctrl-btn ${convoySimulation.paused ? 'active' : ''}`}
                onClick={pauseConvoySimulation}
                style={{ borderRadius: '20px' }}
              >
                {convoySimulation.paused ? <Play size={14} /> : <Pause size={14} />}
                {convoySimulation.paused ? 'Resume' : 'Pause'}
              </button>
              <button
                className="convoy-ctrl-btn stop"
                onClick={stopConvoySimulation}
                style={{ borderRadius: '20px' }}
              >
                <Square size={14} /> End
              </button>
            </>
          )}

          {/* ═══ Coordinates Pill — inside toolbar, forced last row ═══ */}
          <div style={{ width: '100%', display: 'flex', justifyContent: 'center' }}>
            <div style={{
              display: 'flex', alignItems: 'center', gap: '6px',
              background: 'linear-gradient(135deg, rgba(15,23,42,0.92), rgba(30,41,59,0.92))',
              padding: '4px 12px', borderRadius: '20px',
              fontSize: '10px', fontFamily: 'var(--font-mono)', color: '#e2e8f0',
              border: '1px solid rgba(255,255,255,0.08)',
            }}>
              <Navigation size={10} color="#fb923c" />
              <span style={{ letterSpacing: '0.5px' }}>23.0225°N · 72.5714°E</span>
              <span style={{ color: '#fb923c', fontWeight: 700, letterSpacing: '1px' }}>AMD</span>
              <span style={{ width: '1px', height: '12px', backgroundColor: '#334155' }} />
              <span style={{ color: '#64748b', fontSize: '8px' }}>ALT 53m</span>
            </div>
          </div>
        </div>

        {/* ═══ CONVOY LIVE HUD — Enhanced Floating Panel ═══ */}
        {convoySimulation?.active && (() => {
          const spd = convoySimulation.speed || 0;
          const hdg = convoySimulation.heading || 0;
          const prog = convoySimulation.progress || 0;
          const cong = convoySimulation.congestionHistory?.[convoySimulation.congestionHistory.length - 1]?.congestion ?? 0;
          const elapsed = convoySimulation.elapsedSeconds || 0;
          const accel = convoySimulation.acceleration || 0;
          const maxSpd = convoySimulation.maxSpeed || 0;
          const segTrav = convoySimulation.segmentsTraversed || 0;
          const totalSegs = convoySimulation.routeSegments?.length || 0;
          const spdPct = Math.min(spd / 60, 1);
          const spdColor = spd > 40 ? '#22c55e' : spd > 25 ? '#eab308' : '#ef4444';
          const elMin = Math.floor(elapsed / 60);
          const elSec = Math.floor(elapsed % 60);
          return (
          <div className="convoy-hud-panel glass-panel" style={{
            position: 'absolute',
            top: '80px',
            right: rightOpen ? '352px' : '16px',
            width: '260px',
            zIndex: 1200,
            borderRadius: '14px',
            padding: '14px',
            transition: 'right 0.3s cubic-bezier(0.4, 0, 0.2, 1)',
            border: '1px solid rgba(234,88,12,0.35)',
            boxShadow: '0 8px 32px rgba(0,0,0,0.5), 0 0 24px rgba(234,88,12,0.12)',
            background: 'linear-gradient(160deg, rgba(15,23,42,0.92), rgba(30,41,59,0.92))',
          }}>
            {/* Header with elapsed timer */}
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '10px' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                <div className="live-data-dot" />
                <span style={{ color: '#f97316', fontWeight: 800, fontSize: '11px', letterSpacing: '1px' }}>CONVOY LIVE</span>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                <span style={{ color: '#94a3b8', fontSize: '9px', fontFamily: 'var(--font-mono)', background: 'rgba(30,41,59,0.8)', padding: '2px 6px', borderRadius: '4px', border: '1px solid #1e293b' }}>
                  ⏱ {String(elMin).padStart(2,'0')}:{String(elSec).padStart(2,'0')}
                </span>
                <span style={{ color: convoySimulation.paused ? '#eab308' : '#22c55e', fontSize: '8px', fontWeight: 700, letterSpacing: '0.5px' }}>
                  {convoySimulation.paused ? '❚❚ PAUSED' : '● TRACKING'}
                </span>
              </div>
            </div>

            {/* Speed Gauge + Compass Row */}
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '10px' }}>
              {/* SVG Arc Gauge — R4 Fixed */}
              <div style={{ flex: '1', display: 'flex', flexDirection: 'column', alignItems: 'center' }}>
                <svg viewBox="0 0 120 72" width="115" height="70">
                  <defs>
                    <filter id="hglow"><feGaussianBlur stdDeviation="2.5" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
                  </defs>
                  {/* Background track */}
                  <path d="M 15 60 A 50 50 0 0 1 105 60" fill="none" stroke="#1e293b" strokeWidth="8" strokeLinecap="round"/>
                  {/* Speed zone bands: Red 0-25, Yellow 25-40, Green 40-60 */}
                  <path d="M 15 60 A 50 50 0 0 1 105 60" fill="none" stroke="rgba(239,68,68,0.15)" strokeWidth="8" strokeDasharray="59 83" strokeLinecap="butt"/>
                  <path d="M 15 60 A 50 50 0 0 1 105 60" fill="none" stroke="rgba(234,179,8,0.12)" strokeWidth="8" strokeDasharray="35 107" strokeDashoffset="-59" strokeLinecap="butt"/>
                  <path d="M 15 60 A 50 50 0 0 1 105 60" fill="none" stroke="rgba(34,197,94,0.10)" strokeWidth="8" strokeDasharray="48 94" strokeDashoffset="-94" strokeLinecap="butt"/>
                  {/* Active speed fill */}
                  <path d="M 15 60 A 50 50 0 0 1 105 60" fill="none"
                    strokeWidth="8" strokeLinecap="round"
                    filter="url(#hglow)"
                    style={{ stroke: spdColor, strokeDasharray: `${spdPct * 141.4} 141.4`, transition: 'stroke-dasharray 0.2s ease, stroke 0.3s ease' }}
                  />
                  {/* Major tick marks + labels */}
                  {[0, 15, 30, 45, 60].map(tick => {
                    const ang = (-180 + (tick / 60) * 180) * Math.PI / 180;
                    const x1 = 60 + 43 * Math.cos(ang), y1 = 60 + 43 * Math.sin(ang);
                    const x2 = 60 + 48 * Math.cos(ang), y2 = 60 + 48 * Math.sin(ang);
                    const lx = 60 + 36 * Math.cos(ang), ly = 60 + 36 * Math.sin(ang);
                    return <g key={tick}>
                      <line x1={x1} y1={y1} x2={x2} y2={y2} stroke="#475569" strokeWidth="1.5"/>
                      <text x={lx} y={ly + 1} textAnchor="middle" dominantBaseline="middle" fill="#64748b" fontSize="6" fontFamily="var(--font-mono)">{tick}</text>
                    </g>;
                  })}
                  {/* Minor tick marks */}
                  {[5, 10, 20, 25, 35, 40, 50, 55].map(tick => {
                    const ang = (-180 + (tick / 60) * 180) * Math.PI / 180;
                    return <line key={`m${tick}`} x1={60 + 45 * Math.cos(ang)} y1={60 + 45 * Math.sin(ang)} x2={60 + 48 * Math.cos(ang)} y2={60 + 48 * Math.sin(ang)} stroke="#334155" strokeWidth="0.7"/>;
                  })}
                  {/* Needle */}
                  <g style={{ transformOrigin: '60px 60px', transform: `rotate(${-180 + spdPct * 180}deg)`, transition: 'transform 0.2s ease' }}>
                    <line x1="60" y1="60" x2="98" y2="60" stroke={spdColor} strokeWidth="2" strokeLinecap="round" filter="url(#hglow)"/>
                    <circle cx="60" cy="60" r="3" fill="#0f172a" stroke={spdColor} strokeWidth="1.5"/>
                  </g>
                  {/* Digital speed readout */}
                  <text x="60" y="48" textAnchor="middle" fill={spdColor} fontSize="18" fontWeight="800" fontFamily="var(--font-mono)">{spd.toFixed(0)}</text>
                  <text x="60" y="57" textAnchor="middle" fill="#64748b" fontSize="7" fontFamily="var(--font-mono)">km/h</text>
                </svg>
              </div>

              {/* Mini Compass */}
              <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '2px' }}>
                <svg width="48" height="48" viewBox="0 0 48 48">
                  <circle cx="24" cy="24" r="21" fill="none" stroke="#1e293b" strokeWidth="1.5" />
                  <circle cx="24" cy="24" r="18" fill="rgba(15,23,42,0.6)" stroke="#334155" strokeWidth="0.5" />
                  {[['N',24,7],['S',24,42],['E',41,25],['W',7,25]].map(([l,x,y]) => <text key={l} x={x} y={y} textAnchor="middle" dominantBaseline="middle" fill={l === 'N' ? '#ef4444' : '#475569'} fontSize="6" fontWeight={l === 'N' ? 700 : 400}>{l}</text>)}
                  <g transform={`rotate(${hdg}, 24, 24)`}>
                    <polygon points="24,8 22,20 26,20" fill="#f97316" opacity="0.9" />
                    <polygon points="24,40 22,28 26,28" fill="#475569" opacity="0.5" />
                  </g>
                  <circle cx="24" cy="24" r="2" fill="#f97316" />
                </svg>
                <span style={{ fontSize: '8px', color: '#94a3b8', fontFamily: 'var(--font-mono)' }}>{hdg.toFixed(0)}°</span>
              </div>
            </div>

            {/* Progress Bar with marker */}
            <div style={{ marginBottom: '10px' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '8px', color: '#64748b', marginBottom: '4px' }}>
                <span>PROGRESS</span>
                <span style={{ color: '#e2e8f0', fontWeight: 700, fontFamily: 'var(--font-mono)' }}>{(prog * 100).toFixed(1)}%</span>
              </div>
              <div style={{ position: 'relative', height: '7px', background: '#0f172a', borderRadius: '4px', overflow: 'visible', border: '1px solid #1e293b' }}>
                <div className="convoy-gauge-fill" style={{
                  height: '100%',
                  width: `${prog * 100}%`,
                  background: `linear-gradient(90deg, #ea580c, #f97316, #fbbf24)`,
                  borderRadius: '4px',
                  boxShadow: '0 0 8px rgba(249,115,22,0.4)',
                }} />
                <div style={{
                  position: 'absolute',
                  top: '-3px',
                  left: `calc(${prog * 100}% - 6px)`,
                  width: '12px', height: '12px',
                  borderRadius: '50%',
                  background: '#fbbf24',
                  border: '2px solid #0f172a',
                  boxShadow: '0 0 6px rgba(251,191,36,0.6)',
                  transition: 'left 0.2s',
                }} />
              </div>
            </div>

            {/* 3x2 Metrics Grid */}
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '5px', fontSize: '8px' }}>
              {[
                { label: 'ETA', value: convoySimulation.etaSeconds > 0 ? `${Math.floor(convoySimulation.etaSeconds / 60)}:${String(Math.floor(convoySimulation.etaSeconds % 60)).padStart(2, '0')}` : '--:--', color: '#38bdf8' },
                { label: 'DISTANCE', value: `${(convoySimulation.distanceTraveledM / 1000).toFixed(1)}km`, color: '#e2e8f0' },
                { label: 'CONGEST', value: `${(cong * 100).toFixed(0)}%`, color: cong > 0.7 ? '#ef4444' : cong > 0.4 ? '#eab308' : '#22c55e' },
                { label: 'ACCEL', value: `${accel > 0 ? '↑' : '↓'}${Math.abs(accel).toFixed(1)}`, color: accel >= 0 ? '#22c55e' : '#f97316' },
                { label: 'MAX SPD', value: `${maxSpd.toFixed(0)}`, color: '#4ade80' },
                { label: 'SEGMENTS', value: `${segTrav}/${totalSegs}`, color: '#a78bfa' },
              ].map((m, i) => (
                <div key={i} style={{ background: 'rgba(15,23,42,0.7)', padding: '5px 6px', borderRadius: '6px', border: '1px solid rgba(51,65,85,0.5)', textAlign: 'center' }}>
                  <div style={{ color: '#475569', fontSize: '7px', letterSpacing: '0.5px', marginBottom: '2px' }}>{m.label}</div>
                  <div style={{ color: m.color, fontWeight: 700, fontFamily: 'var(--font-mono)', fontSize: '11px' }}>{m.value}</div>
                </div>
              ))}
            </div>

            {/* ═══ R2 — HUD FUEL GAUGE ═══ */}
            <div style={{ margin: '6px 0', padding: '0 2px' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '2px' }}>
                <span style={{ fontSize: '7px', color: '#64748b', textTransform: 'uppercase', letterSpacing: '0.5px', fontWeight: 600 }}>Fuel</span>
                <span style={{ fontSize: '9px', fontFamily: 'var(--font-mono)', fontWeight: 700, color: (convoySimulation.fuelPct ?? 100) > 40 ? '#22c55e' : (convoySimulation.fuelPct ?? 100) > 15 ? '#eab308' : '#dc2626' }}>
                  {(convoySimulation.fuelPct ?? 100).toFixed(0)}%
                </span>
              </div>
              <div style={{ height: '4px', borderRadius: '2px', backgroundColor: 'rgba(30,41,59,0.8)', overflow: 'hidden' }}>
                <div style={{ height: '100%', width: `${convoySimulation.fuelPct ?? 100}%`, borderRadius: '2px', background: 'linear-gradient(90deg, #dc2626 0%, #eab308 40%, #22c55e 80%)', transition: 'width 0.3s', boxShadow: '0 0 6px rgba(34,197,94,0.3)' }} />
              </div>
            </div>

            {/* ═══ R2 — THREAT + ZONE + G-FORCE ROW ═══ */}
            <div style={{ display: 'flex', gap: '4px', margin: '4px 0' }}>
              {(() => {
                const thr = convoySimulation.threatLevel || 'nominal';
                const thrColors = { nominal: '#16a34a', guarded: '#2563eb', moderate: '#eab308', elevated: '#ea580c', critical: '#dc2626' };
                return (
                  <div style={{ flex: 1, padding: '4px 6px', borderRadius: '5px', background: 'rgba(15,23,42,0.7)', border: `1px solid ${thrColors[thr] || '#334155'}`, textAlign: 'center' }}>
                    <div style={{ fontSize: '6px', color: '#475569', letterSpacing: '0.4px', marginBottom: '1px' }}>THREAT</div>
                    <div style={{ fontSize: '9px', fontWeight: 800, color: thrColors[thr] || '#94a3b8', textTransform: 'uppercase', fontFamily: 'var(--font-mono)' }}>{thr}</div>
                  </div>
                );
              })()}
              <div style={{ flex: 1, padding: '4px 6px', borderRadius: '5px', background: 'rgba(15,23,42,0.7)', border: '1px solid rgba(124,58,237,0.4)', textAlign: 'center' }}>
                <div style={{ fontSize: '6px', color: '#475569', letterSpacing: '0.4px', marginBottom: '1px' }}>ZONE</div>
                <div style={{ fontSize: '9px', fontWeight: 700, color: '#a78bfa', textTransform: 'capitalize', fontFamily: 'var(--font-mono)' }}>{convoySimulation.currentZone || 'primary'}</div>
              </div>
              <div style={{ flex: 1, padding: '4px 6px', borderRadius: '5px', background: 'rgba(15,23,42,0.7)', border: '1px solid rgba(51,65,85,0.5)', textAlign: 'center' }}>
                <div style={{ fontSize: '6px', color: '#475569', letterSpacing: '0.4px', marginBottom: '1px' }}>G-FORCE</div>
                <div style={{ fontSize: '9px', fontWeight: 800, fontFamily: 'var(--font-mono)', color: (convoySimulation.gForce ?? 0) > 0.4 ? '#dc2626' : (convoySimulation.gForce ?? 0) > 0.2 ? '#eab308' : '#22c55e' }}>{(convoySimulation.gForce ?? 0).toFixed(2)}g</div>
              </div>
            </div>
          </div>
          );
        })()}



        {/* Floating Edge Compute Status */}
        <div style={{
          position: 'absolute',
          bottom: '70px',
          left: leftOpen ? '332px' : '12px',
          background: 'linear-gradient(160deg, rgba(15,23,42,0.92), rgba(30,41,59,0.90))',
          backdropFilter: 'blur(12px)',
          WebkitBackdropFilter: 'blur(12px)',
          padding: '8px 12px',
          borderRadius: '10px',
          border: '1px solid rgba(51,65,85,0.5)',
          boxShadow: '0 4px 16px rgba(0,0,0,0.3)',
          zIndex: 1100,
          transition: 'left 0.3s cubic-bezier(0.4, 0, 0.2, 1)',
          minWidth: '160px',
        }}>
          <div style={{ fontSize: '7px', color: '#64748b', textTransform: 'uppercase', fontWeight: 700, letterSpacing: '1px', marginBottom: '5px' }}>Edge Compute</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '3px' }}>
            {[
              { label: 'LLM', ok: backendHealth?.ollama === 'connected', val: 'Qwen 3.5' },
              { label: 'Brain', ok: backendHealth?.status === 'ok' || backendHealth?.status === 'degraded', val: backendHealth?.status || '—' },
              { label: 'GPU', ok: !gpuHealth || (gpuHealth.vramUsedMb / gpuHealth.vramTotalMb < 0.92), val: gpuHealth ? `${Math.round(gpuHealth.vramUsedMb / gpuHealth.vramTotalMb * 100)}%` : '—' },
            ].map(s => (
              <div key={s.label} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '6px' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
                  <div style={{ width: '5px', height: '5px', borderRadius: '50%', backgroundColor: s.ok ? '#22c55e' : '#dc2626', boxShadow: `0 0 3px ${s.ok ? 'rgba(34,197,94,0.4)' : 'rgba(220,38,38,0.4)'}` }} />
                  <span style={{ fontSize: '8px', color: '#94a3b8', fontWeight: 600 }}>{s.label}</span>
                </div>
                <span style={{ fontSize: '8px', fontFamily: 'var(--font-mono)', color: s.ok ? '#e2e8f0' : '#dc2626', fontWeight: 700 }}>{s.val}</span>
              </div>
            ))}
          </div>
          {gpuHealth && (
            <div style={{ marginTop: '4px' }}>
              <div style={{ height: '3px', backgroundColor: '#1e293b', borderRadius: '2px', overflow: 'hidden' }}>
                <div style={{
                  width: `${Math.round(gpuHealth.vramUsedMb / gpuHealth.vramTotalMb * 100)}%`,
                  height: '100%', borderRadius: '2px',
                  background: (gpuHealth.vramUsedMb / gpuHealth.vramTotalMb) > 0.92 ? '#dc2626' : (gpuHealth.vramUsedMb / gpuHealth.vramTotalMb) > 0.8 ? '#eab308' : 'linear-gradient(90deg, #16a34a, #22c55e)',
                  transition: 'width 0.5s',
                }} />
              </div>
              <div style={{ fontSize: '6px', color: '#475569', marginTop: '2px', textAlign: 'right' }}>{gpuHealth.vramUsedMb}/{gpuHealth.vramTotalMb} MB VRAM</div>
            </div>
          )}
        </div>

        {/* Panels */}
        <LeftPanel 
          open={leftOpen} 
          onToggle={() => setLeftOpen(!leftOpen)}
          origin={origin}
          setOrigin={setOrigin}
          destination={destination}
          setDestination={setDestination}
          selectedVehicles={selectedVehicles}
          setSelectedVehicles={setSelectedVehicles}
          navigate={navigate}
          vvipClass={vvipClass}
          setVvipClass={setVvipClass}
        />

        <RightPanel 
          open={rightOpen} 
          onToggle={() => setRightOpen(!rightOpen)}
          alerts={anomalies}
          summary={corridorSummary}
          movements={activeMovements}
          vvipClass={vvipClass}
        />

        <AIReasoningPanel visible={reasoningOpen} onClose={() => setReasoningOpen(false)} />
        <SecurityDossierPanel visible={dossierOpen} onClose={() => setDossierOpen(false)} vvipClass={vvipClass} />
      </div>
    </div>
  );
};

export default VVIPDashboard;
