import React, { useMemo } from 'react';
import { 
  Shield, AlertTriangle, Users, Radio, Navigation, Clock, 
  Crosshair, MapPin, Zap, ChevronRight, X, Lock, Eye, 
  Truck, Activity, CheckCircle, XCircle, Target, Siren,
  BadgeAlert, ArrowRightLeft, HeartPulse, Wifi, Milestone
} from 'lucide-react';
import { useConvoy } from '../context/ConvoyContext';

// ── Blue Book security specs per VVIP class (mirrors LeftPanel SECURITY_SPECS) ──
const SECURITY_SPECS = {
  'Z+': { minLanes: 6, closure: 'Full closure', advance: '180s', maxQueue: '2000m', color: '#dc2626', label: 'SOVEREIGN / HEAD OF STATE' },
  'Z':  { minLanes: 4, closure: 'Partial closure', advance: '120s', maxQueue: '1000m', color: '#ea580c', label: 'CABINET / HIGH COMMAND' },
  'Y':  { minLanes: 2, closure: 'Speed restriction + signal priority', advance: '60s', maxQueue: '500m', label: 'SENIOR OFFICIALS', color: '#2563eb' },
  'X':  { minLanes: 0, closure: 'Signal priority only', advance: '0s', maxQueue: '0m', label: 'ADMINISTRATIVE', color: '#64748b' },
};

// ── Fleet template structure per VVIP class (mirrors LeftPanel FLEET_TEMPLATES) ──
const FLEET_ROLES = {
  'Z+': ['Pilot Vehicle', 'Advance Scout', 'VVIP Primary', 'Rear Guard', 'Medical Unit', 'Traffic Control'],
  'Z':  ['Pilot Vehicle', 'VVIP Primary', 'Rear Guard', 'Traffic Control'],
  'Y':  ['Lead Escort', 'VVIP Primary', 'Tail Vehicle'],
  'X':  ['Lead Escort', 'VVIP Primary'],
};

const ROLE_ICONS = {
  'Pilot Vehicle': Siren,
  'Advance Scout': Eye,
  'VVIP Primary': Shield,
  'Rear Guard': Target,
  'Medical Unit': HeartPulse,
  'Traffic Control': ArrowRightLeft,
  'Lead Escort': Siren,
  'Tail Vehicle': Truck,
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
      </div>

      {/* ── Scrollable Body ── */}
      <div className="dark-panel-scroll" style={{ flex: 1, overflowY: 'auto', padding: '14px 16px' }}>

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

        {/* ═══ SECTION 5: CONTINGENCY / ALTERNATE ROUTES ═══ */}
        <SectionBadge 
          icon={ArrowRightLeft} 
          title="Contingency Routing" 
          status={alternateRoutes.length > 0 ? `${alternateRoutes.length} ALT ROUTE${alternateRoutes.length > 1 ? 'S' : ''}` : 'NONE'}
          statusColor={alternateRoutes.length > 0 ? '#2563eb' : '#64748b'}
        />
        <div style={{ padding: '0 6px 16px 6px' }}>
          {alternateRoutes.length > 0 ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
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
            <div style={{ fontSize: '10px', color: '#64748b', fontStyle: 'italic', padding: '8px 0' }}>
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

        {/* ═══ SECTION 8: POST-CLEARANCE REPORT ═══ */}
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
            DOSSIER v1 · {new Date().toLocaleTimeString('en-IN', { hour12: false })} IST
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
