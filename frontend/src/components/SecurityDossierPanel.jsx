import React, { useMemo } from 'react';
import { 
  Shield, AlertTriangle, Users, Radio, Navigation, Clock, 
  Crosshair, MapPin, Zap, ChevronRight, X, Lock, Eye, 
  Truck, Activity, CheckCircle, XCircle, Target, Siren,
  BadgeAlert, ArrowRightLeft, HeartPulse, Wifi, Milestone,
  CheckSquare, Square, Building2, Home, Plane, AlertOctagon
} from 'lucide-react';
import { useConvoy } from '../context/ConvoyContext';

// ── Blue Book security specs per VVIP class (mirrors LeftPanel SECURITY_SPECS) ──
const SECURITY_SPECS = {
  'SPG': { minLanes: 8, closure: 'Full corridor lockdown', advance: '300s', maxQueue: '3000m', color: '#7f1d1d', label: 'PRIME MINISTER — SPG EXCLUSIVE', personnel: '40-60+', agency: 'SPG (Cabinet Sec.)' },
  'Z+': { minLanes: 6, closure: 'Full closure', advance: '180s', maxQueue: '2000m', color: '#dc2626', label: 'HIGH-RISK PROTECTEE — NSG+POLICE', personnel: '55', agency: 'NSG + Police' },
  'Z':  { minLanes: 4, closure: 'Partial closure', advance: '120s', maxQueue: '1000m', color: '#ea580c', label: 'CABINET / HIGH COMMAND', personnel: '22', agency: 'NSG/ITBP/CRPF' },
  'Y+': { minLanes: 3, closure: 'Partial + speed restriction', advance: '90s', maxQueue: '750m', color: '#9333ea', label: 'MODERATE-RISK — COMMANDO ESCORT', personnel: '11', agency: 'Commandos + Police' },
  'Y':  { minLanes: 2, closure: 'Speed restriction + signal priority', advance: '60s', maxQueue: '500m', label: 'LOW-MODERATE — SENIOR OFFICIALS', color: '#2563eb', personnel: '8', agency: 'Commandos + Police' },
  'X':  { minLanes: 0, closure: 'Signal priority only', advance: '0s', maxQueue: '0m', label: 'MINIMAL THREAT — ARMED POLICE', color: '#64748b', personnel: '2', agency: 'Armed Police' },
};

// ── Blue Book §3.1 convoy security box formation per VVIP class ──
const FLEET_ROLES = {
  'SPG': ['Pilot Warning Car', 'Advance Recon Unit', 'ECM / Technical Car', 'VVIP Primary (VR10)', 'Escort Car I', 'Escort Car II', 'Spare / Decoy Car', 'Medical Ambulance', 'SSP / DM Authority', 'IB Liaison Vehicle'],
  'Z+': ['Pilot Warning Car', 'ECM / Technical Car', 'VVIP Primary (VR10)', 'Escort Car I', 'Escort Car II', 'Spare / Decoy Car', 'Medical Ambulance', 'SSP / DM / IB Trail'],
  'Z':  ['Pilot Warning Car', 'VVIP Primary Car', 'Escort Car', 'Medical Support', 'Authority Trail'],
  'Y+': ['Pilot Car', 'VVIP Primary Car', 'Escort Vehicle', 'Traffic Coordination'],
  'Y':  ['Lead Escort', 'VVIP Primary Car', 'Traffic Support'],
  'X':  ['VVIP Vehicle', 'Traffic Escort'],
};

const ROLE_ICONS = {
  'Pilot Warning Car': Siren,
  'Advance Recon Unit': Eye,
  'ECM / Technical Car': Wifi,
  'VVIP Primary (VR10)': Shield,
  'VVIP Primary Car': Shield,
  'VVIP Vehicle': Shield,
  'Escort Car I': Target,
  'Escort Car II': Target,
  'Escort Car': Target,
  'Escort Vehicle': Target,
  'Spare / Decoy Car': Shield,
  'Medical Ambulance': HeartPulse,
  'Medical Support': HeartPulse,
  'SSP / DM Authority': Users,
  'SSP / DM / IB Trail': Users,
  'Authority Trail': Users,
  'IB Liaison Vehicle': Radio,
  'Lead Escort': Siren,
  'Pilot Car': Siren,
  'Traffic Support': ArrowRightLeft,
  'Traffic Escort': ArrowRightLeft,
  'Traffic Coordination': ArrowRightLeft,
};

// ── Agency abbreviation mapping ──
const AGENCY_MAP = {
  'traffic_police':  { label: 'Traffic Police', short: 'TP', color: '#2563eb' },
  'transport':       { label: 'Transport Dept', short: 'TD', color: '#16a34a' },
  'security':        { label: 'Security Forces', short: 'SF', color: '#dc2626' },
  'police':          { label: 'State Police', short: 'SP', color: '#ea580c' },
  'spg':             { label: 'SPG',           short: 'SPG', color: '#dc2626' },
  'ib':              { label: 'Intelligence Bureau', short: 'IB', color: '#7c3aed' },
};

const SecurityDossierPanel = ({ visible, onClose, vvipClass = 'Z+' }) => {
  const {
    planResult,
    escortResult,
    clearResult,
    lifecycle,
    corridorSummary,
    anomalies,
    activeMovements,
    movementId,
    // Blue Book Protocol
    aslChecklist,
    aslReadiness,
    protocolCompliance,
    protocolScore,
    antiSabotage,
    transitStatus,
    planB,
    activatePlanB,
    deactivatePlanB,
    simulatePlanBReadiness,
    // AI Dossier & Threat
    securityDossier,
    generatingDossier,
    runDossierGeneration,
    threatBrief,
    assessingThreat,
    runThreatAssessment,
    protocolAssessment,
    assessingProtocol,
    runProtocolAssessment,
  } = useConvoy();

  const spec = SECURITY_SPECS[vvipClass] || SECURITY_SPECS['Z+'];
  const fleetRoles = FLEET_ROLES[vvipClass] || FLEET_ROLES['Z+'];

  // ── Derive deployed agencies from live diversion directives ──
  const deployedAgencies = useMemo(() => {
    const directives = planResult?.diversion_directives || [];
    const agencySet = new Map();
    directives.forEach(d => {
      const key = d.agency || 'security';
      if (!agencySet.has(key)) {
        const info = AGENCY_MAP[key] || { label: key, short: key.slice(0, 3).toUpperCase(), color: '#94a3b8' };
        agencySet.set(key, { ...info, count: 1, segments: [d.segment_id] });
      } else {
        const existing = agencySet.get(key);
        existing.count += 1;
        existing.segments.push(d.segment_id);
      }
    });
    return Array.from(agencySet.values());
  }, [planResult]);

  // ── Security violations & warnings from plan ──
  const violations = planResult?.security_violations || [];
  const warnings = planResult?.security_warnings || [];
  const securityScore = planResult?.security_score;
  const securityCompliant = planResult?.security_compliant;

  // ── Route intel ──
  const primaryRoute = planResult?.primary_route;
  const alternateRoutes = planResult?.alternate_routes || [];

  // ── Diversion directives (contingency) ──
  const diversions = planResult?.diversion_directives || [];

  // ── Anomaly threat assessment from live feed ──
  const threatAssessment = useMemo(() => {
    const high = anomalies.filter(a => a.severity === 'high').length;
    const medium = anomalies.filter(a => a.severity === 'medium').length;
    const low = anomalies.filter(a => a.severity === 'low').length;
    const total = anomalies.length;
    const level = high > 2 ? 'CRITICAL' : high > 0 ? 'ELEVATED' : medium > 3 ? 'MODERATE' : total > 0 ? 'GUARDED' : 'NOMINAL';
    const color = high > 2 ? '#dc2626' : high > 0 ? '#ea580c' : medium > 3 ? '#d97706' : total > 0 ? '#2563eb' : '#16a34a';
    return { high, medium, low, total, level, color };
  }, [anomalies]);

  // ── Lifecycle phase mapping to Blue Book stages ──
  const phaseMap = {
    idle:      { label: 'PRE-PLANNING', stage: 0, color: '#64748b' },
    planning:  { label: 'ASL / ROUTE MAPPING', stage: 1, color: '#2563eb' },
    approved:  { label: 'PLAN APPROVED — AWAITING DEPLOYMENT', stage: 2, color: '#d97706' },
    active:    { label: 'LIVE ESCORT — EXECUTION PHASE', stage: 3, color: '#16a34a' },
    completed: { label: 'POST-CLEARANCE RECOVERY', stage: 4, color: '#7c3aed' },
  };
  const currentPhase = phaseMap[lifecycle] || phaseMap.idle;

  // ── Section badge component ──
  const SectionBadge = ({ icon: Icon, title, status, statusColor }) => (
    <div style={{
      display: 'flex', alignItems: 'center', justifyContent: 'space-between',
      padding: '10px 14px', marginBottom: '10px',
      background: 'linear-gradient(90deg, rgba(234,88,12,0.08) 0%, transparent 100%)',
      borderLeft: '3px solid #ea580c', borderRadius: '0 8px 8px 0',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
        <Icon size={14} color="#ea580c" />
        <span style={{ fontSize: '11px', fontWeight: 800, color: '#e2e8f0', letterSpacing: '0.1em', textTransform: 'uppercase' }}>{title}</span>
      </div>
      {status && (
        <span style={{
          fontSize: '8px', fontWeight: 800, padding: '2px 8px', borderRadius: '10px',
          backgroundColor: `${statusColor || '#334155'}22`,
          color: statusColor || '#94a3b8',
          border: `1px solid ${statusColor || '#334155'}44`,
          letterSpacing: '0.05em',
        }}>{status}</span>
      )}
    </div>
  );

  if (!visible) return null;

  return (
    <div style={{
      position: 'fixed', top: 0, right: 0, bottom: 0,
      width: '420px', zIndex: 2000,
      background: 'linear-gradient(180deg, #0a0f1c 0%, #0f172a 20%, #1a2332 100%)',
      borderLeft: '1px solid #334155',
      boxShadow: '-8px 0 40px rgba(0,0,0,0.5)',
      display: 'flex', flexDirection: 'column',
      animation: 'dossierSlideIn 0.35s cubic-bezier(0.16, 1, 0.3, 1)',
      overflow: 'hidden',
    }}>
      {/* ── Header ── */}
      <div style={{
        padding: '16px 18px', flexShrink: 0,
        background: 'linear-gradient(135deg, rgba(220,38,38,0.1) 0%, rgba(234,88,12,0.06) 100%)',
        borderBottom: '1px solid #334155',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
            <div style={{
              width: '36px', height: '36px', borderRadius: '10px',
              background: `linear-gradient(135deg, ${spec.color}, ${spec.color}88)`,
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              boxShadow: `0 0 20px ${spec.color}40`,
              animation: 'securityPulse 3s ease-in-out infinite',
            }}>
              <Shield size={18} color="#fff" />
            </div>
            <div>
              <div style={{ fontSize: '13px', fontWeight: 800, color: '#e2e8f0', letterSpacing: '0.05em' }}>
                SECURITY DOSSIER
              </div>
              <div style={{ fontSize: '9px', color: '#94a3b8', letterSpacing: '0.03em' }}>
                Blue Book Protocol · Classification {vvipClass}
              </div>
            </div>
          </div>
          <button onClick={onClose} style={{
            width: '30px', height: '30px', borderRadius: '8px',
            backgroundColor: 'rgba(51,65,85,0.4)', border: '1px solid #475569',
            color: '#94a3b8', cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center',
          }}>
            <X size={14} />
          </button>
        </div>

        {/* Classification strip */}
        <div style={{
          marginTop: '12px', display: 'flex', alignItems: 'center', gap: '10px',
          padding: '8px 12px', borderRadius: '8px',
          background: `linear-gradient(90deg, ${spec.color}18, transparent)`,
          border: `1px solid ${spec.color}33`,
        }}>
          <Lock size={12} color={spec.color} />
          <span style={{ fontSize: '10px', fontWeight: 800, color: spec.color, letterSpacing: '0.08em' }}>
            CLASSIFICATION {vvipClass} — {spec.label}
          </span>
          <div style={{ marginLeft: 'auto', display: 'flex', gap: '6px' }}>
            <span style={{ fontSize: '9px', color: '#94a3b8', fontFamily: 'var(--font-mono)' }}>
              {movementId ? `MID:${movementId.slice(0, 8)}` : 'NO ACTIVE MOVEMENT'}
            </span>
          </div>
        </div>

        {/* Phase timeline bar */}
        <div style={{ marginTop: '10px', display: 'flex', gap: '3px' }}>
          {Object.values(phaseMap).map((phase, i) => (
            <div key={i} style={{
              flex: 1, height: '4px', borderRadius: '2px',
              backgroundColor: i <= currentPhase.stage ? currentPhase.color : '#334155',
              transition: 'background-color 0.5s ease',
              boxShadow: i === currentPhase.stage ? `0 0 8px ${currentPhase.color}60` : 'none',
            }} />
          ))}
        </div>
        <div style={{ marginTop: '4px', fontSize: '9px', fontWeight: 700, color: currentPhase.color, letterSpacing: '0.06em' }}>
          {currentPhase.label}
        </div>

        {/* AI Dossier Action Buttons */}
        {movementId && (
          <div style={{ marginTop: '10px', display: 'flex', gap: '4px' }}>
            <button
              onClick={() => runDossierGeneration({ vvip_class: vvipClass })}
              disabled={generatingDossier}
              style={{
                flex: 1, padding: '7px 8px', fontSize: '9px', fontWeight: 700,
                background: generatingDossier ? '#334155' : 'linear-gradient(135deg, #06b6d4, #0891b2)',
                color: 'white', border: 'none', borderRadius: '6px',
                cursor: generatingDossier ? 'wait' : 'pointer', letterSpacing: '0.04em',
              }}
            >
              {generatingDossier ? '⟳ Generating…' : '🧠 Generate AI Dossier'}
            </button>
            <button
              onClick={runProtocolAssessment}
              disabled={assessingProtocol}
              style={{
                flex: 1, padding: '7px 8px', fontSize: '9px', fontWeight: 700,
                background: assessingProtocol ? '#334155' : 'linear-gradient(135deg, #eab308, #ca8a04)',
                color: 'white', border: 'none', borderRadius: '6px',
                cursor: assessingProtocol ? 'wait' : 'pointer', letterSpacing: '0.04em',
              }}
            >
              {assessingProtocol ? '⟳ Assessing…' : '🛡 Protocol Check'}
            </button>
            <button
              onClick={runThreatAssessment}
              disabled={assessingThreat}
              style={{
                flex: 0.8, padding: '7px 8px', fontSize: '9px', fontWeight: 700,
                background: assessingThreat ? '#334155' : 'linear-gradient(135deg, #f97316, #ea580c)',
                color: 'white', border: 'none', borderRadius: '6px',
                cursor: assessingThreat ? 'wait' : 'pointer', letterSpacing: '0.04em',
              }}
            >
              {assessingThreat ? '⟳…' : '🎯 Threat'}
            </button>
          </div>
        )}
      </div>

      {/* ── Scrollable Body ── */}
      <div className="dark-panel-scroll" style={{ flex: 1, overflowY: 'auto', padding: '14px 16px' }}>

        {/* ═══ AI-GENERATED DOSSIER (Qwen 3.5) ═══ */}
        {securityDossier && (
          <>
            <SectionBadge icon={Shield} title="AI Security Dossier" status="QWEN 3.5" statusColor="#06b6d4" />
            <div style={{
              padding: '10px 12px', marginBottom: '16px', borderRadius: '8px',
              background: 'rgba(6,182,212,0.04)', border: '1px solid rgba(6,182,212,0.15)',
            }}>
              <div style={{
                fontSize: '10px', color: '#cbd5e1', lineHeight: 1.6, whiteSpace: 'pre-wrap',
                fontFamily: 'var(--font-mono)', maxHeight: '300px', overflowY: 'auto',
              }}>
                {typeof securityDossier === 'string'
                  ? securityDossier
                  : securityDossier?.dossier || securityDossier?.response || JSON.stringify(securityDossier, null, 2)}
              </div>
              <div style={{ marginTop: '6px', fontSize: '8px', color: '#475569', fontStyle: 'italic' }}>
                Generated by Qwen 3.5 9B · Movement {movementId?.slice(0, 8) || '—'}
              </div>
            </div>
          </>
        )}

        {/* ═══ AI THREAT BRIEF (Qwen 3.5) ═══ */}
        {threatBrief && (
          <>
            <SectionBadge icon={Crosshair} title="AI Threat Brief" status={(threatBrief.threat_level || 'NOMINAL').toUpperCase()} statusColor={threatBrief.threat_level === 'critical' ? '#dc2626' : threatBrief.threat_level === 'high' ? '#f97316' : '#eab308'} />
            <div style={{
              padding: '10px 12px', marginBottom: '16px', borderRadius: '8px',
              background: threatBrief.threat_level === 'critical' ? 'rgba(220,38,38,0.06)' : 'rgba(249,115,22,0.04)',
              border: `1px solid ${threatBrief.threat_level === 'critical' ? 'rgba(220,38,38,0.2)' : 'rgba(249,115,22,0.15)'}`,
            }}>
              <div style={{ fontSize: '10px', color: '#cbd5e1', lineHeight: 1.6, whiteSpace: 'pre-wrap', fontFamily: 'var(--font-mono)', maxHeight: '200px', overflowY: 'auto' }}>
                {typeof threatBrief === 'string'
                  ? threatBrief
                  : threatBrief?.assessment || threatBrief?.response || JSON.stringify(threatBrief, null, 2)}
              </div>
            </div>
          </>
        )}

        {/* ═══ AI PROTOCOL ASSESSMENT ═══ */}
        {protocolAssessment && (
          <>
            <SectionBadge icon={CheckCircle} title="Protocol Compliance" status="AI VALIDATED" statusColor="#eab308" />
            <div style={{
              padding: '10px 12px', marginBottom: '16px', borderRadius: '8px',
              background: 'rgba(234,179,8,0.04)', border: '1px solid rgba(234,179,8,0.15)',
            }}>
              <div style={{ fontSize: '10px', color: '#cbd5e1', lineHeight: 1.6, whiteSpace: 'pre-wrap', fontFamily: 'var(--font-mono)', maxHeight: '200px', overflowY: 'auto' }}>
                {typeof protocolAssessment === 'string'
                  ? protocolAssessment
                  : protocolAssessment?.response || JSON.stringify(protocolAssessment, null, 2)}
              </div>
            </div>
          </>
        )}

        {/* ═══ SECTION 1: THREAT ASSESSMENT ═══ */}
        <SectionBadge icon={Crosshair} title="Threat Assessment" status={threatAssessment.level} statusColor={threatAssessment.color} />
        <div style={{ padding: '0 6px 16px 6px' }}>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr 1fr', gap: '6px', marginBottom: '10px' }}>
            {[
              { label: 'HIGH', value: threatAssessment.high, color: '#dc2626' },
              { label: 'MED', value: threatAssessment.medium, color: '#ea580c' },
              { label: 'LOW', value: threatAssessment.low, color: '#d97706' },
              { label: 'TOTAL', value: threatAssessment.total, color: '#e2e8f0' },
            ].map(({ label, value, color }) => (
              <div key={label} style={{
                padding: '8px 6px', borderRadius: '8px', textAlign: 'center',
                background: `linear-gradient(135deg, ${color}10, ${color}05)`,
                border: `1px solid ${color}25`,
              }}>
                <div style={{ fontSize: '16px', fontWeight: 800, color, fontFamily: 'var(--font-mono)' }}>{value}</div>
                <div style={{ fontSize: '8px', fontWeight: 700, color: '#94a3b8', letterSpacing: '0.08em' }}>{label}</div>
              </div>
            ))}
          </div>
          {/* Live anomaly types breakdown */}
          {anomalies.length > 0 ? (
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px' }}>
              {[...new Set(anomalies.map(a => a.anomaly_type))].map(type => {
                const count = anomalies.filter(a => a.anomaly_type === type).length;
                return (
                  <span key={type} style={{
                    fontSize: '8px', fontWeight: 600, padding: '2px 6px', borderRadius: '4px',
                    backgroundColor: '#1e293b', border: '1px solid #334155', color: '#94a3b8',
                    fontFamily: 'var(--font-mono)',
                  }}>
                    {type.replace(/_/g, ' ')} ×{count}
                  </span>
                );
              })}
            </div>
          ) : (
            <div style={{ fontSize: '10px', color: '#64748b', fontStyle: 'italic' }}>No anomalies detected — corridor nominal</div>
          )}
        </div>

        {/* ═══ SECTION 2: AGENCIES DEPLOYED ═══ */}
        <SectionBadge icon={Users} title="Agencies Deployed" status={deployedAgencies.length > 0 ? `${deployedAgencies.length} AGENCIES` : 'STANDBY'} statusColor={deployedAgencies.length > 0 ? '#16a34a' : '#64748b'} />
        <div style={{ padding: '0 6px 16px 6px' }}>
          {deployedAgencies.length > 0 ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
              {deployedAgencies.map((agency, i) => (
                <div key={i} style={{
                  display: 'flex', alignItems: 'center', gap: '10px',
                  padding: '8px 12px', borderRadius: '8px',
                  backgroundColor: '#1e293b', border: '1px solid #334155',
                }}>
                  <div style={{
                    width: '28px', height: '28px', borderRadius: '6px',
                    background: `linear-gradient(135deg, ${agency.color}30, ${agency.color}10)`,
                    border: `1px solid ${agency.color}40`,
                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                    fontSize: '9px', fontWeight: 800, color: agency.color,
                  }}>{agency.short}</div>
                  <div style={{ flex: 1 }}>
                    <div style={{ fontSize: '11px', fontWeight: 700, color: '#e2e8f0' }}>{agency.label}</div>
                    <div style={{ fontSize: '9px', color: '#64748b', fontFamily: 'var(--font-mono)' }}>
                      {agency.count} directive{agency.count !== 1 ? 's' : ''} · Seg {agency.segments.slice(0, 3).join(', ')}{agency.segments.length > 3 ? '…' : ''}
                    </div>
                  </div>
                  <div style={{
                    width: '8px', height: '8px', borderRadius: '50%',
                    backgroundColor: '#16a34a', boxShadow: '0 0 6px rgba(22,163,74,0.5)',
                    animation: 'pulse 2s ease-in-out infinite',
                  }} />
                </div>
              ))}
            </div>
          ) : (
            <div style={{
              padding: '14px', borderRadius: '8px', textAlign: 'center',
              backgroundColor: '#1e293b', border: '1px dashed #334155',
            }}>
              <Users size={20} color="#475569" style={{ margin: '0 auto 6px' }} />
              <div style={{ fontSize: '10px', color: '#64748b' }}>Deploy a movement to activate agency coordination</div>
            </div>
          )}
        </div>

        {/* ═══ SECTION 3: ASL / PLANNING PHASE ═══ */}
        <SectionBadge 
          icon={MapPin} 
          title="ASL / Route Mapping" 
          status={securityCompliant != null ? (securityCompliant ? 'COMPLIANT' : 'NON-COMPLIANT') : 'PENDING'} 
          statusColor={securityCompliant === true ? '#16a34a' : securityCompliant === false ? '#dc2626' : '#64748b'} 
        />
        <div style={{ padding: '0 6px 16px 6px' }}>
          {/* Security score gauge */}
          {securityScore != null && (
            <div style={{
              display: 'flex', alignItems: 'center', gap: '12px',
              padding: '10px 14px', borderRadius: '8px', marginBottom: '10px',
              background: 'linear-gradient(90deg, rgba(22,163,74,0.06), transparent)',
              border: '1px solid #334155',
            }}>
              <div style={{ position: 'relative', width: '44px', height: '44px' }}>
                <svg viewBox="0 0 36 36" style={{ width: '44px', height: '44px', transform: 'rotate(-90deg)' }}>
                  <circle cx="18" cy="18" r="15.9155" fill="none" stroke="#334155" strokeWidth="3" />
                  <circle cx="18" cy="18" r="15.9155" fill="none"
                    stroke={securityScore >= 0.8 ? '#16a34a' : securityScore >= 0.5 ? '#ea580c' : '#dc2626'}
                    strokeWidth="3" strokeLinecap="round"
                    strokeDasharray={`${securityScore * 100}, 100`}
                  />
                </svg>
                <div style={{
                  position: 'absolute', top: '50%', left: '50%', transform: 'translate(-50%, -50%)',
                  fontSize: '11px', fontWeight: 800, color: '#e2e8f0', fontFamily: 'var(--font-mono)',
                }}>{Math.round(securityScore * 100)}</div>
              </div>
              <div>
                <div style={{ fontSize: '10px', fontWeight: 700, color: '#e2e8f0' }}>Security Score</div>
                <div style={{ fontSize: '9px', color: '#94a3b8' }}>
                  Min lanes: {spec.minLanes} · Closure: {spec.closure}
                </div>
              </div>
            </div>
          )}

          {/* Route mapping data */}
          {primaryRoute ? (
            <div style={{
              padding: '10px 12px', borderRadius: '8px', marginBottom: '8px',
              backgroundColor: '#1e293b', border: '1px solid #334155',
            }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '6px' }}>
                <Navigation size={12} color="#16a34a" />
                <span style={{ fontSize: '10px', fontWeight: 700, color: '#16a34a' }}>PRIMARY ROUTE</span>
                <span style={{ fontSize: '9px', color: '#94a3b8', marginLeft: 'auto', fontFamily: 'var(--font-mono)' }}>
                  Score: {primaryRoute.score?.toFixed(3)}
                </span>
              </div>
              <div style={{ fontSize: '10px', color: '#e2e8f0', marginBottom: '4px' }}>{primaryRoute.reason}</div>
              <div style={{ fontSize: '9px', color: '#64748b', fontFamily: 'var(--font-mono)' }}>
                {primaryRoute.segment_ids?.length || 0} segments · ID: {primaryRoute.route_id?.slice(0, 12)}
              </div>
            </div>
          ) : (
            <div style={{ fontSize: '10px', color: '#64748b', fontStyle: 'italic', padding: '8px 0' }}>
              Route mapping awaiting movement deployment...
            </div>
          )}

          {/* Violations */}
          {violations.length > 0 && (
            <div style={{
              padding: '8px 10px', borderRadius: '6px', marginBottom: '8px',
              background: 'rgba(220,38,38,0.08)', border: '1px solid rgba(220,38,38,0.25)',
            }}>
              <div style={{ fontSize: '9px', fontWeight: 800, color: '#fca5a5', marginBottom: '6px', display: 'flex', alignItems: 'center', gap: '4px' }}>
                <XCircle size={10} /> SECURITY VIOLATIONS ({violations.length})
              </div>
              {violations.map((v, i) => (
                <div key={i} style={{ fontSize: '9px', color: '#fca5a5', padding: '3px 0', borderTop: i > 0 ? '1px solid rgba(220,38,38,0.15)' : 'none' }}>
                  <span style={{ fontFamily: 'var(--font-mono)', color: '#f87171' }}>SEG {v.segment_id}</span> — {v.rule}: {v.detail}
                  {v.severity && <span style={{ marginLeft: '6px', fontSize: '8px', fontWeight: 700, color: '#dc2626', textTransform: 'uppercase' }}>({v.severity})</span>}
                </div>
              ))}
            </div>
          )}

          {/* Warnings */}
          {warnings.length > 0 && (
            <div style={{
              padding: '8px 10px', borderRadius: '6px',
              background: 'rgba(234,88,12,0.06)', border: '1px solid rgba(234,88,12,0.2)',
            }}>
              <div style={{ fontSize: '9px', fontWeight: 800, color: '#fdba74', marginBottom: '6px', display: 'flex', alignItems: 'center', gap: '4px' }}>
                <AlertTriangle size={10} /> SECURITY WARNINGS ({warnings.length})
              </div>
              {warnings.map((w, i) => (
                <div key={i} style={{ fontSize: '9px', color: '#fdba74', padding: '2px 0' }}>
                  {w.segment_id && <span style={{ fontFamily: 'var(--font-mono)', color: '#fb923c' }}>SEG {w.segment_id} · </span>}
                  {w.concern}
                </div>
              ))}
            </div>
          )}
        </div>

        {/* ═══ SECTION 4: EXECUTION PHASE ═══ */}
        <SectionBadge 
          icon={Siren} 
          title="Execution Phase" 
          status={lifecycle === 'active' ? 'LIVE' : lifecycle === 'completed' ? 'COMPLETE' : 'STANDBY'}
          statusColor={lifecycle === 'active' ? '#16a34a' : lifecycle === 'completed' ? '#7c3aed' : '#64748b'}
        />
        <div style={{ padding: '0 6px 16px 6px' }}>
          {/* Convoy formation grid */}
          <div style={{ marginBottom: '10px' }}>
            <div style={{ fontSize: '9px', fontWeight: 700, color: '#94a3b8', marginBottom: '6px', letterSpacing: '0.08em' }}>
              CONVOY FORMATION — {vvipClass} PROTOCOL
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '4px' }}>
              {fleetRoles.map((role, i) => {
                const IconComp = ROLE_ICONS[role] || Shield;
                return (
                  <div key={i} style={{
                    display: 'flex', alignItems: 'center', gap: '8px',
                    padding: '7px 10px', borderRadius: '6px',
                    backgroundColor: '#1e293b', border: '1px solid #334155',
                  }}>
                    <IconComp size={12} color={i === 0 ? '#ea580c' : i === 2 || (FLEET_ROLES[vvipClass]?.length === 2 && i === 1) ? '#dc2626' : '#64748b'} />
                    <span style={{ fontSize: '9px', fontWeight: 600, color: '#e2e8f0' }}>{role}</span>
                  </div>
                );
              })}
            </div>
          </div>

          {/* SOPs derived from VVIP class */}
          <div style={{
            padding: '10px 12px', borderRadius: '8px',
            background: 'linear-gradient(135deg, rgba(234,88,12,0.05), transparent)',
            border: '1px solid #334155', marginBottom: '10px',
          }}>
            <div style={{ fontSize: '9px', fontWeight: 800, color: '#ea580c', marginBottom: '8px', letterSpacing: '0.08em' }}>
              STANDING ORDERS (CLASS {vvipClass})
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '6px' }}>
              {[
                { label: 'Min Lanes', value: spec.minLanes || 'None', icon: Milestone },
                { label: 'Closure Type', value: spec.closure, icon: Lock },
                { label: 'Advance Time', value: spec.advance, icon: Clock },
                { label: 'Max Queue', value: spec.maxQueue, icon: ArrowRightLeft },
              ].map(({ label, value, icon: I }) => (
                <div key={label} style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                  <I size={10} color="#64748b" />
                  <div>
                    <div style={{ fontSize: '8px', color: '#64748b', fontWeight: 600 }}>{label}</div>
                    <div style={{ fontSize: '10px', color: '#e2e8f0', fontWeight: 600 }}>{value}</div>
                  </div>
                </div>
              ))}
            </div>
          </div>

          {/* SOP protocols */}
          <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
            {[
              { label: 'No-Stop Rule', desc: vvipClass === 'Z+' || vvipClass === 'Z' ? 'Enforced — zero stops permitted' : 'Signal priority — tactical stops permitted', active: vvipClass === 'Z+' || vvipClass === 'Z' },
              { label: 'Emergency Extraction', desc: alternateRoutes.length > 0 ? `${alternateRoutes.length} alternate route(s) pre-computed` : 'Awaiting route planning', active: alternateRoutes.length > 0 },
              { label: 'Comms Redundancy', desc: lifecycle === 'active' ? 'Active — multi-channel operational' : 'Standby', active: lifecycle === 'active' },
            ].map(({ label, desc, active }) => (
              <div key={label} style={{
                display: 'flex', alignItems: 'center', gap: '8px',
                padding: '6px 10px', borderRadius: '6px',
                backgroundColor: active ? 'rgba(22,163,74,0.05)' : '#1e293b',
                border: `1px solid ${active ? 'rgba(22,163,74,0.2)' : '#334155'}`,
              }}>
                {active ? <CheckCircle size={11} color="#16a34a" /> : <XCircle size={11} color="#475569" />}
                <div>
                  <div style={{ fontSize: '9px', fontWeight: 700, color: '#e2e8f0' }}>{label}</div>
                  <div style={{ fontSize: '8px', color: '#94a3b8' }}>{desc}</div>
                </div>
              </div>
            ))}
          </div>

          {/* Escort result data */}
          {escortResult && (
            <div style={{
              marginTop: '10px', padding: '10px 12px', borderRadius: '8px',
              background: 'linear-gradient(135deg, rgba(22,163,74,0.08), transparent)',
              border: '1px solid rgba(22,163,74,0.25)',
            }}>
              <div style={{ fontSize: '9px', fontWeight: 800, color: '#4ade80', marginBottom: '6px' }}>ESCORT TELEMETRY</div>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '6px', fontSize: '10px' }}>
                <div>
                  <span style={{ color: '#94a3b8', fontSize: '9px' }}>Iterations: </span>
                  <span style={{ fontWeight: 700, color: '#e2e8f0', fontFamily: 'var(--font-mono)' }}>{escortResult.total_iterations}</span>
                </div>
                <div>
                  <span style={{ color: '#94a3b8', fontSize: '9px' }}>Status: </span>
                  <span style={{ fontWeight: 700, color: '#4ade80', fontFamily: 'var(--font-mono)' }}>{escortResult.final_status || 'COMPLETE'}</span>
                </div>
              </div>
            </div>
          )}
        </div>

        {/* ═══ SECTION 5: CONTINGENCY / PLAN B (Blue Book §3.5) ═══ */}
        <SectionBadge 
          icon={ArrowRightLeft} 
          title="Contingency — Plan B" 
          status={planB.active ? '⚡ ACTIVATED' : alternateRoutes.length > 0 ? `${alternateRoutes.length} ALT` : 'STANDBY'}
          statusColor={planB.active ? '#dc2626' : alternateRoutes.length > 0 ? '#2563eb' : '#64748b'}
        />
        <div style={{ padding: '0 6px 16px 6px' }}>
          {/* Plan B readiness checks */}
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '4px', marginBottom: '8px' }}>
            {[
              { key: 'altRouteSanitised', label: 'Alt Route Sanitised', icon: '🛣️' },
              { key: 'altRouteRehearsed', label: 'Trial Run Done', icon: '🏃' },
              { key: 'contingencyMotorcadeReady', label: 'Motorcade Staged', icon: '🚓' },
              { key: 'transportFallback', label: 'Road Fallback Ready', icon: '🚗' },
            ].map(check => (
              <div key={check.key} style={{
                display: 'flex', alignItems: 'center', gap: '6px',
                padding: '5px 8px', borderRadius: '6px',
                backgroundColor: planB[check.key] ? 'rgba(22,163,98,0.08)' : '#1e293b',
                border: `1px solid ${planB[check.key] ? 'rgba(22,163,98,0.3)' : '#334155'}`,
              }}>
                <span style={{ fontSize: '12px' }}>{check.icon}</span>
                <div style={{ flex: 1 }}>
                  <div style={{ fontSize: '8px', fontWeight: 600, color: planB[check.key] ? '#4ade80' : '#94a3b8' }}>{check.label}</div>
                  <div style={{ fontSize: '7px', color: planB[check.key] ? '#16a34a' : '#475569', fontWeight: 700 }}>
                    {planB[check.key] ? 'READY' : 'PENDING'}
                  </div>
                </div>
              </div>
            ))}
          </div>

          {/* Simulate readiness + Activate buttons */}
          <div style={{ display: 'flex', gap: '4px', marginBottom: '8px' }}>
            {!planB.altRouteSanitised && (
              <button
                onClick={simulatePlanBReadiness}
                style={{
                  flex: 1, padding: '5px 8px', borderRadius: '6px', border: '1px solid #334155',
                  background: 'rgba(37,99,235,0.1)', color: '#60a5fa', cursor: 'pointer',
                  fontSize: '8px', fontWeight: 700, letterSpacing: '0.3px',
                }}
              >
                ✓ SIMULATE READINESS
              </button>
            )}
            <button
              onClick={() => planB.active ? deactivatePlanB() : activatePlanB('Manual contingency activation')}
              style={{
                flex: 1, padding: '5px 8px', borderRadius: '6px', border: 'none',
                background: planB.active ? 'rgba(220,38,38,0.15)' : 'rgba(234,88,12,0.12)',
                color: planB.active ? '#dc2626' : '#ea580c', cursor: 'pointer',
                fontSize: '8px', fontWeight: 700, letterSpacing: '0.3px',
              }}
            >
              {planB.active ? '✕ DEACTIVATE PLAN B' : '⚡ ACTIVATE PLAN B'}
            </button>
          </div>

          {/* Plan B activation banner */}
          {planB.active && (
            <div style={{
              padding: '8px 10px', borderRadius: '8px', marginBottom: '8px',
              background: 'linear-gradient(135deg, rgba(220,38,38,0.12), rgba(234,88,12,0.08))',
              border: '1px solid rgba(220,38,38,0.4)',
            }}>
              <div style={{ fontSize: '9px', fontWeight: 800, color: '#dc2626', marginBottom: '2px', display: 'flex', alignItems: 'center', gap: '6px' }}>
                <AlertOctagon size={12} /> PLAN B ACTIVE
              </div>
              <div style={{ fontSize: '8px', color: '#fca5a5' }}>{planB.reason}</div>
              {planB.activatedAt && (
                <div style={{ fontSize: '7px', color: '#64748b', fontFamily: 'var(--font-mono)', marginTop: '2px' }}>
                  Activated: {new Date(planB.activatedAt).toLocaleTimeString()}
                </div>
              )}
            </div>
          )}

          {/* Emergency Facilities — Hospitals, Safe Houses, Helipads */}
          {planB.emergencyFacilities.length > 0 && (
            <div style={{ marginBottom: '8px' }}>
              <div style={{ fontSize: '8px', fontWeight: 700, color: '#64748b', textTransform: 'uppercase', letterSpacing: '0.5px', marginBottom: '4px' }}>
                Emergency Facilities Along Route
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '3px' }}>
                {planB.emergencyFacilities.map((f, i) => {
                  const typeConfig = {
                    hospital: { icon: <HeartPulse size={10} />, color: '#dc2626', bg: 'rgba(220,38,38,0.08)' },
                    safe_house: { icon: <Home size={10} />, color: '#2563eb', bg: 'rgba(37,99,235,0.08)' },
                    helipad: { icon: <Plane size={10} />, color: '#16a34a', bg: 'rgba(22,163,98,0.08)' },
                  }[f.type] || { icon: <Building2 size={10} />, color: '#64748b', bg: '#1e293b' };
                  return (
                    <div key={i} style={{
                      display: 'flex', alignItems: 'center', gap: '8px',
                      padding: '5px 8px', borderRadius: '6px',
                      backgroundColor: typeConfig.bg, border: `1px solid ${typeConfig.color}22`,
                    }}>
                      <div style={{ color: typeConfig.color }}>{typeConfig.icon}</div>
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ fontSize: '8px', fontWeight: 700, color: '#e2e8f0', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{f.name}</div>
                        <div style={{ fontSize: '7px', color: '#64748b', textTransform: 'capitalize' }}>{f.type.replace('_', ' ')}</div>
                      </div>
                      <div style={{ textAlign: 'right' }}>
                        <div style={{ fontSize: '9px', fontWeight: 800, fontFamily: 'var(--font-mono)', color: typeConfig.color }}>{f.distance_km} km</div>
                        <div style={{ fontSize: '7px', color: '#475569' }}>ETA {f.eta_min}m</div>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {/* Alternate Routes */}
          {alternateRoutes.length > 0 ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
              <div style={{ fontSize: '8px', fontWeight: 700, color: '#64748b', textTransform: 'uppercase', letterSpacing: '0.5px', marginBottom: '2px' }}>
                Pre-Computed Alternate Routes
              </div>
              {alternateRoutes.map((route, i) => (
                <div key={i} style={{
                  padding: '8px 12px', borderRadius: '8px',
                  backgroundColor: '#1e293b', border: '1px solid #334155',
                  borderLeft: `3px solid ${i === 0 ? '#2563eb' : i === 1 ? '#d97706' : '#7c3aed'}`,
                }}>
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '4px' }}>
                    <span style={{ fontSize: '10px', fontWeight: 700, color: '#e2e8f0' }}>ALT-{String.fromCharCode(65 + i)}</span>
                    <span style={{ fontSize: '9px', fontFamily: 'var(--font-mono)', color: '#94a3b8' }}>
                      Score: {route.score?.toFixed(3)}
                    </span>
                  </div>
                  <div style={{ fontSize: '9px', color: '#94a3b8' }}>{route.reason}</div>
                  <div style={{ fontSize: '8px', color: '#64748b', fontFamily: 'var(--font-mono)', marginTop: '2px' }}>
                    {route.segment_ids?.length || 0} segments · {route.route_id?.slice(0, 12)}
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div style={{ fontSize: '10px', color: '#64748b', fontStyle: 'italic', padding: '4px 0' }}>
              {planResult ? 'No alternate routes computed for this corridor' : 'Deploy a movement to generate contingency routes'}
            </div>
          )}
        </div>

        {/* ═══ SECTION 6: DIVERSION COORDINATION ═══ */}
        <SectionBadge 
          icon={BadgeAlert}
          title="Diversion Coordination" 
          status={diversions.length > 0 ? `${diversions.length} ACTIVE` : 'NONE'}
          statusColor={diversions.length > 0 ? '#ea580c' : '#64748b'}
        />
        <div style={{ padding: '0 6px 16px 6px' }}>
          {diversions.length > 0 ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
              {diversions.slice(0, 8).map((d, i) => {
                const agencyInfo = AGENCY_MAP[d.agency] || { label: d.agency || 'Unknown', short: '??', color: '#94a3b8' };
                const actionColor = d.action === 'activate' ? '#16a34a' : d.action === 'deactivate' ? '#dc2626' : '#d97706';
                return (
                  <div key={i} style={{
                    display: 'flex', alignItems: 'center', gap: '8px',
                    padding: '7px 10px', borderRadius: '6px',
                    backgroundColor: '#1e293b', border: '1px solid #334155',
                  }}>
                    <div style={{
                      width: '6px', height: '6px', borderRadius: '50%',
                      backgroundColor: actionColor, boxShadow: `0 0 6px ${actionColor}60`,
                    }} />
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ fontSize: '9px', fontWeight: 700, color: '#e2e8f0', display: 'flex', alignItems: 'center', gap: '6px' }}>
                        <span style={{ fontFamily: 'var(--font-mono)' }}>SEG {d.segment_id}</span>
                        <span style={{ color: actionColor, fontSize: '8px', textTransform: 'uppercase' }}>{d.action}</span>
                      </div>
                      <div style={{ fontSize: '8px', color: '#64748b' }}>
                        {agencyInfo.label} · {d.timing_sec}s advance · {d.detail?.slice(0, 50) || ''}
                      </div>
                    </div>
                  </div>
                );
              })}
              {diversions.length > 8 && (
                <div style={{ fontSize: '9px', color: '#64748b', textAlign: 'center', padding: '4px' }}>
                  +{diversions.length - 8} more directive(s)
                </div>
              )}
            </div>
          ) : (
            <div style={{ fontSize: '10px', color: '#64748b', fontStyle: 'italic', padding: '8px 0' }}>
              {planResult ? 'No diversions required — corridor sterile' : 'Diversions generated upon movement deployment'}
            </div>
          )}
        </div>

        {/* ═══ SECTION 7: CORRIDOR INTELLIGENCE ═══ */}
        <SectionBadge icon={Activity} title="Corridor Intelligence" status={corridorSummary ? corridorSummary.status?.toUpperCase() : 'SYNCING'} statusColor={corridorSummary?.status === 'green' ? '#16a34a' : corridorSummary?.status === 'amber' ? '#ea580c' : corridorSummary?.status === 'red' ? '#dc2626' : '#64748b'} />
        <div style={{ padding: '0 6px 16px 6px' }}>
          {corridorSummary ? (
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '6px' }}>
              {[
                { label: 'AVG SPEED', value: `${corridorSummary.avg_speed_kmh?.toFixed(1) || '--'} km/h`, color: '#2563eb' },
                { label: 'CONGESTION', value: corridorSummary.avg_congestion_idx?.toFixed(3) || '0.000', color: (corridorSummary.avg_congestion_idx || 0) > 0.6 ? '#dc2626' : '#ea580c' },
                { label: 'CRITICAL', value: corridorSummary.critical_segments || 0, color: (corridorSummary.critical_segments || 0) > 0 ? '#dc2626' : '#16a34a' },
                { label: 'SEGMENTS', value: corridorSummary.total_segments || '--', color: '#94a3b8' },
                { label: 'MOVEMENTS', value: corridorSummary.active_movements || activeMovements.length, color: '#16a34a' },
                { label: 'ANOMALIES', value: corridorSummary.anomaly_count || anomalies.length, color: anomalies.length > 5 ? '#dc2626' : '#ea580c' },
              ].map(({ label, value, color }) => (
                <div key={label} style={{
                  padding: '8px', borderRadius: '6px', textAlign: 'center',
                  backgroundColor: '#1e293b', border: '1px solid #334155',
                }}>
                  <div style={{ fontSize: '14px', fontWeight: 800, color, fontFamily: 'var(--font-mono)' }}>{value}</div>
                  <div style={{ fontSize: '7px', fontWeight: 700, color: '#64748b', letterSpacing: '0.1em' }}>{label}</div>
                </div>
              ))}
            </div>
          ) : (
            <div style={{ fontSize: '10px', color: '#64748b', fontStyle: 'italic' }}>Awaiting corridor telemetry sync...</div>
          )}
        </div>

        {/* ═══ SECTION 8: PROTOCOL COMPLIANCE (Blue Book §7 — 10 Key Rules) ═══ */}
        <SectionBadge 
          icon={Lock} 
          title="Protocol Compliance" 
          status={`${protocolScore.pct}%`}
          statusColor={protocolScore.pct >= 80 ? '#16a34a' : protocolScore.pct >= 50 ? '#ea580c' : '#dc2626'}
        />
        <div style={{ padding: '0 6px 16px 6px' }}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '3px' }}>
            {[
              { key: 'r1_state_responsibility', rule: 'Rule 1', label: 'State Govt bears primary responsibility for VVIP security' },
              { key: 'r2_police_arrangements', rule: 'Rule 2', label: 'Local police arrangements cleared by SSP/DM' },
              { key: 'r3_no_stop_rule', rule: 'Rule 3', label: 'No unscheduled stops — zero deviation from route' },
              { key: 'r4_dgp_chief_sec', rule: 'Rule 4', label: 'DGP + Chief Secretary personally supervise' },
              { key: 'r5_contingency_rehearsed', rule: 'Rule 5', label: 'Contingency Plan B rehearsed & drill-tested' },
              { key: 'r6_same_make_vehicles', rule: 'Rule 6', label: 'All convoy vehicles same make/colour to prevent identification' },
              { key: 'r7_spg_director_clearance', rule: 'Rule 7', label: 'SPG Director grants final clearance before transit' },
              { key: 'r8_realtime_updates', rule: 'Rule 8', label: 'Real-time threat intel updates to convoy lead' },
              { key: 'r9_security_faces_crowd', rule: 'Rule 9', label: 'Security personnel face crowd, not protectee' },
              { key: 'r10_incidents_logged', rule: 'Rule 10', label: 'All incidents logged — post-clearance report filed' },
            ].map(item => (
              <div key={item.key} style={{
                display: 'flex', alignItems: 'flex-start', gap: '8px',
                padding: '6px 10px', borderRadius: '6px',
                backgroundColor: protocolCompliance[item.key] ? 'rgba(22,163,74,0.05)' : '#1e293b',
                border: `1px solid ${protocolCompliance[item.key] ? 'rgba(22,163,74,0.2)' : '#334155'}`,
              }}>
                {protocolCompliance[item.key] 
                  ? <CheckSquare size={11} color="#16a34a" style={{ flexShrink: 0, marginTop: '1px' }} />
                  : <Square size={11} color="#475569" style={{ flexShrink: 0, marginTop: '1px' }} />
                }
                <div>
                  <span style={{ fontSize: '8px', fontWeight: 800, color: '#ea580c', fontFamily: 'var(--font-mono)', marginRight: '4px' }}>{item.rule}</span>
                  <span style={{ fontSize: '9px', fontWeight: 600, color: protocolCompliance[item.key] ? '#86efac' : '#cbd5e1' }}>{item.label}</span>
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* ═══ SECTION 9: ANTI-SABOTAGE & TRANSIT (Blue Book §6 & §3.3) ═══ */}
        <SectionBadge 
          icon={Crosshair} 
          title="Anti-Sabotage & Transit" 
          status={
            Object.values(antiSabotage).every(Boolean) && Object.values(transitStatus).every(Boolean) 
              ? 'ALL CLEAR' 
              : 'PENDING'
          }
          statusColor={
            Object.values(antiSabotage).every(Boolean) && Object.values(transitStatus).every(Boolean) 
              ? '#16a34a' 
              : '#ea580c'
          }
        />
        <div style={{ padding: '0 6px 16px 6px' }}>
          {/* Anti-Sabotage 3-Method Framework */}
          <div style={{ fontSize: '9px', fontWeight: 700, color: '#94a3b8', marginBottom: '6px', letterSpacing: '0.08em' }}>
            ANTI-SABOTAGE — 3 METHOD FRAMEWORK (§6)
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '6px', marginBottom: '12px' }}>
            {[
              { key: 'physical_search', label: 'Physical Search', desc: 'Manual sweep of route, camp, vehicles, venues', icon: Eye },
              { key: 'technical_gadgets', label: 'Technical Gadgets', desc: 'DFMD, HHMD, mine sweepers, bomb detectors', icon: Wifi },
              { key: 'sniffer_dogs', label: 'Sniffer Dog Squad', desc: 'K9 explosive detection teams', icon: Target },
            ].map(m => {
              const I = m.icon;
              return (
                <div key={m.key} style={{
                  padding: '10px 8px', borderRadius: '8px', textAlign: 'center',
                  backgroundColor: antiSabotage[m.key] ? 'rgba(22,163,74,0.06)' : '#1e293b',
                  border: `1px solid ${antiSabotage[m.key] ? 'rgba(22,163,74,0.25)' : '#334155'}`,
                }}>
                  <I size={16} color={antiSabotage[m.key] ? '#22c55e' : '#475569'} style={{ margin: '0 auto 6px' }} />
                  <div style={{ fontSize: '9px', fontWeight: 700, color: antiSabotage[m.key] ? '#4ade80' : '#e2e8f0' }}>{m.label}</div>
                  <div style={{ fontSize: '7px', color: '#64748b', marginTop: '2px' }}>{m.desc}</div>
                  <div style={{
                    marginTop: '6px', fontSize: '7px', fontWeight: 800, letterSpacing: '0.06em',
                    color: antiSabotage[m.key] ? '#22c55e' : '#dc2626',
                  }}>
                    {antiSabotage[m.key] ? '✓ CLEARED' : '✗ PENDING'}
                  </div>
                </div>
              );
            })}
          </div>

          {/* Transit Status */}
          <div style={{ fontSize: '9px', fontWeight: 700, color: '#94a3b8', marginBottom: '6px', letterSpacing: '0.08em' }}>
            TRANSIT READINESS (§3.3)
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '4px' }}>
            {[
              { key: 'ecm_active', label: 'ECM Jamming Active', desc: 'IED signal blocking', color: '#818cf8' },
              { key: 'spg_clearance', label: 'SPG Director Clearance', desc: 'Final go-signal received', color: '#f472b6' },
              { key: 'route_sanitised', label: 'Route Sanitised', desc: 'Full corridor sweep complete', color: '#34d399' },
              { key: 'formation_intact', label: 'Formation Integrity', desc: 'Convoy box holding position', color: '#fbbf24' },
            ].map(t => (
              <div key={t.key} style={{
                display: 'flex', alignItems: 'center', gap: '8px',
                padding: '7px 10px', borderRadius: '6px',
                backgroundColor: transitStatus[t.key] ? `${t.color}08` : '#1e293b',
                border: `1px solid ${transitStatus[t.key] ? `${t.color}30` : '#334155'}`,
              }}>
                {transitStatus[t.key] 
                  ? <CheckCircle size={12} color={t.color} />
                  : <XCircle size={12} color="#475569" />
                }
                <div>
                  <div style={{ fontSize: '9px', fontWeight: 700, color: transitStatus[t.key] ? t.color : '#e2e8f0' }}>{t.label}</div>
                  <div style={{ fontSize: '7px', color: '#64748b' }}>{t.desc}</div>
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* ═══ SECTION 10: POST-CLEARANCE REPORT ═══ */}
        {clearResult && (
          <>
            <SectionBadge icon={CheckCircle} title="Post-Clearance Report" status="RECOVERED" statusColor="#7c3aed" />
            <div style={{ padding: '0 6px 16px 6px' }}>
              <div style={{
                padding: '12px', borderRadius: '8px',
                background: 'linear-gradient(135deg, rgba(124,58,237,0.08), transparent)',
                border: '1px solid rgba(124,58,237,0.25)',
              }}>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px', fontSize: '10px' }}>
                  {[
                    { label: 'Segments Recovered', value: `${clearResult.segments_recovered}/${clearResult.total_affected_segments}` },
                    { label: 'Recovery Time', value: `${(clearResult.recovery_time_sec || 0).toFixed(1)}s` },
                    { label: 'Diversions Off', value: clearResult.diversions_deactivated },
                    { label: 'Alerts During Escort', value: clearResult.alerts_during_escort },
                    { label: 'Total Decisions', value: clearResult.total_decisions },
                    { label: 'Escort Duration', value: `${Math.round((clearResult.escort_duration_sec || 0) / 60)}min` },
                  ].map(({ label, value }) => (
                    <div key={label}>
                      <span style={{ color: '#94a3b8', fontSize: '9px' }}>{label}: </span>
                      <span style={{ fontWeight: 700, color: '#e2e8f0', fontFamily: 'var(--font-mono)' }}>{value}</span>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          </>
        )}

        {/* ═══ SECTION 11: MULTI-AGENCY COMMAND HIERARCHY (Blue Book §5) ═══ */}
        <SectionBadge 
          icon={Radio} 
          title="Command Hierarchy" 
          status={lifecycle === 'active' ? 'LINKED' : 'STANDBY'}
          statusColor={lifecycle === 'active' ? '#16a34a' : '#64748b'}
        />
        <div style={{ padding: '0 6px 16px 6px' }}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '3px' }}>
            {[
              { rank: 1, agency: 'SPG', role: 'Proximate Security — Final Authority', color: '#dc2626', duty: 'Leads ASL, 40-60 officers, convoy clearance' },
              { rank: 2, agency: 'IB', role: 'Threat Intelligence & Alert Feed', color: '#ea580c', duty: 'Actionable intel, state-level coordination' },
              { rank: 3, agency: 'State Police', role: 'Area Security & Route Clearance', color: '#2563eb', duty: 'DGP present, sector deployment, sniper positions' },
              { rank: 4, agency: 'DM', role: 'Civilian & Admin Coordination', color: '#7c3aed', duty: 'Traffic diversions, public notification, ASL member' },
              { rank: 5, agency: 'NSG/ITBP', role: 'Counter-Assault & Perimeter', color: '#16a34a', duty: 'Armed response, venue perimeter, access control' },
            ].map((cmd, i) => (
              <div key={cmd.agency} style={{
                display: 'flex', alignItems: 'center', gap: '8px',
                padding: '6px 8px', borderRadius: '6px',
                backgroundColor: '#0f172a', 
                border: `1px solid ${cmd.color}20`,
                borderLeft: `3px solid ${cmd.color}`,
              }}>
                <div style={{
                  width: '18px', height: '18px', borderRadius: '50%',
                  background: `${cmd.color}18`, border: `1px solid ${cmd.color}40`,
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  fontSize: '8px', fontWeight: 800, color: cmd.color,
                }}>{cmd.rank}</div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                    <span style={{ fontSize: '9px', fontWeight: 800, color: cmd.color }}>{cmd.agency}</span>
                    <span style={{ fontSize: '8px', color: '#94a3b8' }}>· {cmd.role}</span>
                  </div>
                  <div style={{ fontSize: '7px', color: '#475569', marginTop: '1px' }}>{cmd.duty}</div>
                </div>
                <div style={{
                  width: '6px', height: '6px', borderRadius: '50%',
                  backgroundColor: lifecycle === 'active' ? cmd.color : '#334155',
                  boxShadow: lifecycle === 'active' ? `0 0 4px ${cmd.color}60` : 'none',
                }} />
              </div>
            ))}
          </div>
          <div style={{ 
            marginTop: '6px', padding: '5px 8px', borderRadius: '6px',
            backgroundColor: '#1e293b', border: '1px solid #334155',
            fontSize: '7px', color: '#64748b', fontStyle: 'italic',
          }}>
            Blue Book §5.2: All agencies operate on secure, dedicated comm channel. Incidents logged for institutional memory.
          </div>
        </div>

        {/* ═══ SECTION 12: OPERATIONAL TIMELINE (Blue Book §9) ═══ */}
        <SectionBadge 
          icon={Clock} 
          title="Ops Timeline" 
          status={lifecycle === 'complete' ? 'COMPLETE' : lifecycle === 'active' ? 'TRANSIT' : lifecycle === 'planned' ? 'PLANNED' : 'PRE-VISIT'}
          statusColor={lifecycle === 'complete' ? '#7c3aed' : lifecycle === 'active' ? '#16a34a' : lifecycle === 'planned' ? '#2563eb' : '#64748b'}
        />
        <div style={{ padding: '0 6px 16px 6px' }}>
          {(() => {
            const phaseStep = lifecycle === 'complete' ? 5 : lifecycle === 'active' ? 3 : lifecycle === 'planned' ? 2 : 0;
            const phases = [
              { label: 'ASL Meeting', detail: 'Route survey, threat assessment, agency coordination', agency: 'SPG + IB + DGP', step: 0 },
              { label: 'Contingency Rehearsal', detail: 'Route sanitisation, Plan B trial run, VP mapping', agency: 'State Police + SPG', step: 1 },
              { label: 'Route Clearance', detail: 'Sniper deploy, traffic diversion, anti-sab sweep', agency: 'State Police (SP)', step: 2 },
              { label: 'Transit', detail: 'Proximate security, ECM active, IB threat feed live', agency: 'SPG + State Police', step: 3 },
              { label: 'Arrival / Venue', detail: 'Venue anti-sab, crowd mgmt, plainclothes deployed', agency: 'State Police + SPG', step: 4 },
              { label: 'Debrief', detail: 'Incident log, institutional memory, lessons learned', agency: 'SPG Nodal Officer', step: 5 },
            ];
            return (
              <div style={{ position: 'relative' }}>
                {/* Vertical line */}
                <div style={{
                  position: 'absolute', left: '9px', top: '8px', bottom: '8px', width: '2px',
                  background: 'linear-gradient(to bottom, #334155, #1e293b)',
                }} />
                <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                  {phases.map((ph) => {
                    const isActive = ph.step === phaseStep;
                    const isDone = ph.step < phaseStep;
                    const color = isActive ? '#ea580c' : isDone ? '#16a34a' : '#334155';
                    return (
                      <div key={ph.label} style={{
                        display: 'flex', alignItems: 'flex-start', gap: '10px',
                        padding: '5px 8px 5px 0', 
                      }}>
                        <div style={{
                          width: '20px', height: '20px', borderRadius: '50%', flexShrink: 0,
                          background: isActive ? `${color}25` : isDone ? `${color}15` : '#0f172a',
                          border: `2px solid ${color}`,
                          display: 'flex', alignItems: 'center', justifyContent: 'center',
                          zIndex: 1, position: 'relative',
                        }}>
                          {isDone ? (
                            <CheckCircle size={10} color={color} />
                          ) : isActive ? (
                            <div style={{ width: '6px', height: '6px', borderRadius: '50%', backgroundColor: color, animation: 'pulse 2s infinite' }} />
                          ) : (
                            <div style={{ width: '4px', height: '4px', borderRadius: '50%', backgroundColor: '#475569' }} />
                          )}
                        </div>
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <div style={{ fontSize: '9px', fontWeight: isActive ? 800 : 600, color: isActive ? '#f1f5f9' : isDone ? '#94a3b8' : '#475569' }}>
                            {ph.label}
                          </div>
                          <div style={{ fontSize: '7px', color: isActive ? '#94a3b8' : '#475569', marginTop: '1px' }}>{ph.detail}</div>
                          <div style={{ fontSize: '7px', color: color, fontWeight: 600, marginTop: '1px' }}>{ph.agency}</div>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            );
          })()}
        </div>

      </div>

      {/* ── Footer status ── */}
      <div style={{
        padding: '10px 18px', flexShrink: 0,
        borderTop: '1px solid #334155',
        background: 'linear-gradient(135deg, #0a0f1c,#0f172a)',
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
          <div style={{
            width: '6px', height: '6px', borderRadius: '50%',
            backgroundColor: lifecycle === 'active' ? '#16a34a' : '#64748b',
            boxShadow: lifecycle === 'active' ? '0 0 8px rgba(22,163,74,0.6)' : 'none',
            animation: lifecycle === 'active' ? 'pulse 2s ease-in-out infinite' : 'none',
          }} />
          <span style={{ fontSize: '9px', fontWeight: 600, color: '#94a3b8', fontFamily: 'var(--font-mono)' }}>
            DOSSIER v2 · {new Date().toLocaleTimeString('en-IN', { hour12: false })} IST
          </span>
        </div>
        <span style={{ fontSize: '8px', color: '#475569', fontStyle: 'italic' }}>
          Classification: {vvipClass} — {spec.label}
        </span>
      </div>
    </div>
  );
};

export default SecurityDossierPanel;
