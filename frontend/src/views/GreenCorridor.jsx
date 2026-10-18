import React, { useEffect, useMemo, useState } from 'react';
import { useConvoy } from '../context/ConvoyContext';
import { SUPPORTED, t, localizeNumber } from '../i18n/advisoryStrings';

/**
 * Ambulance Green-Corridor mode — the 108 driver's half of the platform.
 *
 * Promotes the emergency bell from an operator notification to a first-class
 * driver flow: one tap requests a passage window; the platform evaluates the
 * live convoy gap (position + ETA from the same ConvoyContext the operators
 * watch); the duty officer approves or defers; the driver gets a countdown
 * window and an honest minutes-returned-to-golden-hour counter.
 *
 * Everything renders from the shared scripted simulation, so the demo stays
 * one story across console, citizen page and driver page.
 */

// Documented ambulance holds behind convoy corridors: 5–20 minutes
// (Mumbai 2022, Kanchipuram 2018, Agartala 2026 — see PROBLEM_STATEMENT).
const BASELINE_HOLD_MIN = 12; // midpoint used for the demo counter
// The window closes ~30 s before the convoy reaches the stretch — Blue Book
// prudence encoded in the demo.
const SAFETY_BUFFER_S = 30;

function mmss(totalSec, lang) {
  const s = Math.max(0, Math.round(totalSec));
  return localizeNumber(`${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`, lang);
}

function clockIn(secFromNow, lang) {
  const d = new Date(Date.now() + secFromNow * 1000);
  const hhmm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  return localizeNumber(hhmm, lang);
}

const URGENCIES = [
  ['cardiac', 'urgCardiac', '❤️'],
  ['trauma', 'urgTrauma', '🩹'],
  ['stroke', 'urgStroke', '🧠'],
  ['other', 'urgOther', '🚨'],
];

function GreenCorridor({ navigate }) {
  const { convoySimulation: sim } = useConvoy();
  const [lang, setLang] = useState('en');
  const [vehicle, setVehicle] = useState('108-AHM-0000');
  const [urgency, setUrgency] = useState('cardiac');
  const [stretchIdx, setStretchIdx] = useState(null);
  const [stage, setStage] = useState('idle'); // idle | evaluating | proposed | open | deferred
  const [approvedAt, setApprovedAt] = useState(null);
  const [windowSec, setWindowSec] = useState(0);
  const [remaining, setRemaining] = useState(0);

  const simActive = sim && sim.active;

  // Same stretch grouping the citizen page uses (zones from point metadata).
  const stretches = useMemo(() => {
    if (!sim || !sim.pointMeta || !sim.cumDist) return [];
    const zones = [];
    sim.pointMeta.forEach((meta, i) => {
      const last = zones[zones.length - 1];
      if (last && last.name === meta.zone) return;
      zones.push({ name: meta.zone, startDist: sim.cumDist[i], endIdx: i });
    });
    const traveled = sim.distanceTraveledM || 0;
    const total = sim.totalDistanceM || 1;
    const eta = Math.max(0, sim.etaSeconds || 0);
    return zones.map((z, idx) => {
      const fracFromNow = (z.startDist - traveled) / total;
      const state = z.name === sim.currentZone && simActive ? 'now'
        : z.startDist <= traveled ? 'passed' : 'upcoming';
      return { id: idx, name: z.name, reachIn: Math.max(0, eta * fracFromNow), state };
    });
  }, [sim, simActive]);

  const choosable = stretches.filter((s) => s.state !== 'passed');
  const chosen = choosable.find((s) => s.id === stretchIdx) || choosable[0] || null;

  // 1-second heartbeat while a window is live; the countdown lives in state
  // so render stays pure (no Date.now() during render).
  useEffect(() => {
    if (stage !== 'open' || !approvedAt) return undefined;
    const iv = setInterval(() => {
      setRemaining(Math.max(0, windowSec - (Date.now() - approvedAt) / 1000));
    }, 1000);
    return () => clearInterval(iv);
  }, [stage, approvedAt, windowSec]);

  const requestWindow = () => {
    if (!chosen) return;
    setStage('evaluating');
    // The real service evaluates the convoy gap via traffic-oracle; the demo
    // shows the same think-then-answer shape on the shared simulation.
    setTimeout(() => setStage('proposed'), 900);
  };

  const approve = () => {
    if (!chosen) return;
    const sec = Math.max(0, chosen.reachIn - SAFETY_BUFFER_S);
    setWindowSec(sec);
    setApprovedAt(Date.now());
    setRemaining(sec);
    setStage('open');
  };

  const defer = () => setStage('deferred');
  const minutesReturned = Math.max(0, BASELINE_HOLD_MIN - (stage === 'open' ? 1 : 0));

  const page = {
    minHeight: '100vh',
    background: 'linear-gradient(180deg,#f0fdf4 0%,#ecfeff 100%)',
    fontFamily: "'Inter', system-ui, -apple-system, 'Noto Sans Devanagari', 'Noto Sans Gujarati', sans-serif",
    color: '#052e16',
    padding: '0 16px 48px',
  };
  const shell = { maxWidth: 560, margin: '0 auto' }; // narrow: one-hand phone use
  const card = {
    background: '#ffffff',
    borderRadius: 16,
    border: '1px solid #d1fae5',
    boxShadow: '0 1px 3px rgba(5,46,22,0.06)',
    padding: '20px 22px',
    marginBottom: 14,
  };

  return (
    <div style={page}>
      <div style={shell}>
        {/* ── Header ─────────────────────────────────────────────── */}
        <header style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '18px 0 6px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <div style={{ width: 40, height: 40, borderRadius: 12, background: 'linear-gradient(135deg,#059669,#10b981)', display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#fff', fontWeight: 800, fontSize: 19 }}>🚑</div>
            <div>
              <div style={{ fontWeight: 800, fontSize: 16.5 }}>{t(lang, 'driverTitle')}</div>
              <div style={{ fontSize: 12, color: '#047857' }}>{t(lang, 'driverSub')}</div>
            </div>
          </div>
          <div style={{ display: 'flex', gap: 6 }}>
            {SUPPORTED.map((s) => (
              <button key={s.code} onClick={() => setLang(s.code)}
                style={{
                  padding: '5px 10px', borderRadius: 999, fontSize: 11.5, fontWeight: 700, cursor: 'pointer',
                  border: lang === s.code ? '1px solid #059669' : '1px solid #d1fae5',
                  background: lang === s.code ? '#ecfdf5' : '#fff',
                  color: lang === s.code ? '#047857' : '#475569',
                }}>
                {s.label}
              </button>
            ))}
          </div>
        </header>
        <div style={{ fontSize: 11.5, color: '#64748b', padding: '2px 0 14px' }}>
          <button onClick={() => navigate('citizen-advisory')} style={{ background: 'none', border: 'none', color: '#64748b', fontSize: 11.5, cursor: 'pointer', textDecoration: 'underline', padding: 0 }}>
            ← {t(lang, 'driverBack')}
          </button>
        </div>

        {/* ── Corridor state ─────────────────────────────────────── */}
        <section style={{ ...card, borderTop: `4px solid ${simActive ? '#059669' : '#94a3b8'}` }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span style={{ width: 10, height: 10, borderRadius: 999, background: simActive ? '#059669' : '#94a3b8', boxShadow: simActive ? '0 0 0 4px rgba(5,150,105,0.15)' : 'none' }} />
            <strong style={{ fontSize: 15 }}>
              {simActive ? t(lang, 'statusActive') : t(lang, 'noMovement')}
            </strong>
          </div>
          {simActive && sim.progress != null && (
            <div style={{ marginTop: 10 }}>
              <div style={{ height: 6, borderRadius: 999, background: '#e2e8f0', overflow: 'hidden' }}>
                <div style={{ width: `${Math.round(sim.progress * 100)}%`, height: '100%', background: 'linear-gradient(90deg,#059669,#34d399)', transition: 'width 0.3s linear' }} />
              </div>
              <div style={{ fontSize: 11, color: '#94a3b8', marginTop: 4 }}>
                {localizeNumber(Math.round(sim.progress * 100), lang)}% · {sim.currentZone} · ETA {mmss(sim.etaSeconds || 0, lang)}
              </div>
            </div>
          )}
          {!simActive && (
            <p style={{ margin: '8px 0 0', fontSize: 13, color: '#475569' }}>{t(lang, 'noMovementSub')}</p>
          )}
        </section>

        {/* ── Request form ───────────────────────────────────────── */}
        {simActive && stage === 'idle' && choosable.length > 0 && (
          <section style={card}>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              <label style={{ fontSize: 11.5, fontWeight: 700, color: '#334155' }}>
                {t(lang, 'atStretch')}
                <select value={chosen ? chosen.id : ''} onChange={(e) => setStretchIdx(Number(e.target.value))}
                  style={{ display: 'block', marginTop: 4, padding: '10px 12px', borderRadius: 10, border: '1px solid #d1fae5', fontSize: 14, background: '#fff' }}>
                  {choosable.map((s) => (
                    <option key={s.id} value={s.id}>{s.name}{s.state === 'now' ? ' ●' : ''}</option>
                  ))}
                </select>
              </label>
              <label style={{ fontSize: 11.5, fontWeight: 700, color: '#334155' }}>
                {t(lang, 'urgency')}
                <div style={{ display: 'flex', gap: 6, marginTop: 4, flexWrap: 'wrap' }}>
                  {URGENCIES.map(([code, key, icon]) => (
                    <button key={code} type="button" onClick={() => setUrgency(code)}
                      style={{
                        padding: '8px 12px', borderRadius: 10, fontSize: 12.5, fontWeight: 700, cursor: 'pointer',
                        border: urgency === code ? '1.5px solid #059669' : '1px solid #e2e8f0',
                        background: urgency === code ? '#ecfdf5' : '#fff',
                        color: urgency === code ? '#047857' : '#475569',
                      }}>
                      {icon} {t(lang, key)}
                    </button>
                  ))}
                </div>
              </label>
              <label style={{ fontSize: 11.5, fontWeight: 700, color: '#334155' }}>
                Vehicle
                <input value={vehicle} onChange={(e) => setVehicle(e.target.value)}
                  style={{ display: 'block', marginTop: 4, width: '100%', padding: '10px 12px', borderRadius: 10, border: '1px solid #d1fae5', fontSize: 14, boxSizing: 'border-box' }} />
              </label>
              <button type="button" onClick={requestWindow}
                style={{
                  padding: '14px 20px', borderRadius: 12, border: 'none', cursor: 'pointer',
                  background: 'linear-gradient(135deg,#059669,#10b981)', color: '#fff',
                  fontWeight: 800, fontSize: 15, boxShadow: '0 4px 14px rgba(5,150,105,0.3)',
                }}>
                {t(lang, 'requestBtn')} →
              </button>
            </div>
          </section>
        )}

        {/* ── Evaluating ─────────────────────────────────────────── */}
        {stage === 'evaluating' && (
          <section style={card}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, fontSize: 14, color: '#047857' }}>
              <span style={{ width: 14, height: 14, borderRadius: 999, border: '3px solid #a7f3d0', borderTopColor: '#059669', animation: 'spin 0.9s linear infinite' }} />
              {t(lang, 'evaluating')}
            </div>
          </section>
        )}

        {/* ── Proposed window + duty officer ──────────────────────── */}
        {stage === 'proposed' && chosen && (
          <section style={{ ...card, borderColor: '#6ee7b7' }}>
            <div style={{ fontSize: 10.5, fontWeight: 800, letterSpacing: 0.5, textTransform: 'uppercase', color: '#047857' }}>
              {t(lang, 'proposalTitle')}
            </div>
            <div style={{ margin: '10px 0', fontSize: 14, lineHeight: 1.6, color: '#064e3b' }}>
              <div>🚑 <strong>{vehicle}</strong> · {t(lang, `urg${urgency.charAt(0).toUpperCase()}${urgency.slice(1)}`)}</div>
              <div style={{ marginTop: 6 }}>{t(lang, 'convoyReachesIn')} <strong>{mmss(chosen.reachIn, lang)}</strong></div>
              <div>{t(lang, 'crossBefore')} <strong>{clockIn(Math.max(0, chosen.reachIn - SAFETY_BUFFER_S), lang)}</strong> ({chosen.name})</div>
            </div>
            <div style={{ borderTop: '1px dashed #d1fae5', margin: '12px 0', paddingTop: 12 }}>
              <div style={{ fontSize: 10.5, color: '#94a3b8', fontWeight: 700, marginBottom: 8 }}>{t(lang, 'officerLabel')}</div>
              <div style={{ display: 'flex', gap: 8 }}>
                <button type="button" onClick={approve}
                  style={{ flex: 1, padding: '12px 16px', borderRadius: 12, border: 'none', background: '#059669', color: '#fff', fontWeight: 800, fontSize: 13.5, cursor: 'pointer' }}>
                  ✓ {t(lang, 'approveBtn')}
                </button>
                <button type="button" onClick={defer}
                  style={{ flex: 1, padding: '12px 16px', borderRadius: 12, border: '1px solid #e2e8f0', background: '#fff', color: '#475569', fontWeight: 800, fontSize: 13.5, cursor: 'pointer' }}>
                  ⏸ {t(lang, 'deferBtn')}
                </button>
              </div>
            </div>
          </section>
        )}

        {/* ── Window open ────────────────────────────────────────── */}
        {stage === 'open' && (
          <section style={{ ...card, background: 'linear-gradient(135deg,#059669,#10b981)', borderColor: '#34d399', color: '#fff', textAlign: 'center' }}>
            <div style={{ fontSize: 20, fontWeight: 900, letterSpacing: 0.3 }}>{t(lang, 'windowOpen')}</div>
            <div style={{ fontSize: 40, fontWeight: 900, margin: '10px 0 2px', fontVariantNumeric: 'tabular-nums' }}>
              {mmss(remaining, lang)}
            </div>
            <div style={{ fontSize: 12, opacity: 0.9 }}>{t(lang, 'windowClosesIn')}</div>
            <div style={{ marginTop: 14, padding: '10px 12px', background: 'rgba(255,255,255,0.15)', borderRadius: 10, fontSize: 13 }}>
              ⏱ <strong style={{ fontSize: 16 }}>{localizeNumber(minutesReturned, lang)}</strong> {t(lang, 'minutesReturned')}
            </div>
            <div style={{ marginTop: 10, fontSize: 11, opacity: 0.85 }}>{t(lang, 'baselineNote')}</div>
          </section>
        )}

        {/* ── Deferred ───────────────────────────────────────────── */}
        {stage === 'deferred' && chosen && (
          <section style={{ ...card, borderColor: '#fde68a', background: '#fffbeb' }}>
            <div style={{ fontSize: 15, fontWeight: 800, color: '#92400e' }}>
              ⏸ {t(lang, 'holdPosition')} {clockIn(chosen.reachIn + 180, lang)}
            </div>
            <p style={{ margin: '8px 0 0', fontSize: 12.5, color: '#a16207' }}>{t(lang, 'demoNote')}</p>
          </section>
        )}

        <footer style={{ marginTop: 8, fontSize: 11, color: '#94a3b8', textAlign: 'center', lineHeight: 1.6 }}>
          {t(lang, 'demoNote')}
        </footer>
      </div>
    </div>
  );
}

export default GreenCorridor;
