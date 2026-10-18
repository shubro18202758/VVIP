import React, { useMemo, useState } from 'react';
import { useConvoy } from '../context/ConvoyContext';
import { SUPPORTED, t, localizeNumber } from '../i18n/advisoryStrings';

/**
 * Citizen Advisory Layer — the public-facing half of the corridor platform.
 *
 * The operator console manages the movement; this page gives the commuter and
 * the emergency driver what they have never had: advance notice. It renders
 * the SAME live corridor state the operators see (via ConvoyContext — in the
 * scripted demo, the built-in movement simulation), so the story stays one
 * story: what control decides here is what the citizen sees there.
 *
 * Deliberately light-themed and mobile-first: this is a public info page, not
 * a command console. Works with zero backend.
 */

// Indicative alternates for the demo corridor's stretches (real parallel
// arterials of east-bank Ahmedabad). Curated, not computed — labelled as
// indicative in the footer.
const ZONE_ALTERNATES = {
  'Shahibag': 'Riverfront Road (West) · Krishnanagar Road',
  'Raipur-Darwaza': 'Ring Road via Relief Road',
  'Saraspur': 'Naroda–Narol via SP Ring Road',
  'Rakhial': 'Vatva CTM – Gomtipur route',
  'Meghaninagar': '132-ft Ring Road',
  'Airport-Expressway': 'S.V. Road via Thaltej',
  'Airport-Approach': 'Chandkheda link via Indira Bridge',
};
const FALLBACK_ALTERNATE = 'Parallel arterial · follow diversion signage';

// Rolling-hold duration per stretch (minutes) — mirrors the platform's
// 60–180 s activation window ahead of the convoy.
const HOLD_MINUTES = 3;

function clockAt(secondsFromNow, lang) {
  const d = new Date(Date.now() + secondsFromNow * 1000);
  const hhmm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  return localizeNumber(hhmm, lang);
}

function CitizenAdvisory({ navigate }) {
  const { convoySimulation: sim, startDemoSimulation } = useConvoy();
  const [lang, setLang] = useState('en');
  const [phone, setPhone] = useState('');
  const [alertLang, setAlertLang] = useState('en');
  const [subscribed, setSubscribed] = useState(false);
  const [greenRequest, setGreenRequest] = useState(null);

  const simActive = sim && sim.active;
  const simDone = sim && !sim.active && sim.progress >= 1;

  /**
   * Group the per-point route metadata into ordered stretches (zones) and
   * compute, for each, when the convoy reaches it (scaled by the live ETA so
   * the demo's time multiplier cancels out) plus its pass/clear state.
   */
  const stretches = useMemo(() => {
    if (!sim || !sim.pointMeta || !sim.cumDist) return [];
    const zones = [];
    sim.pointMeta.forEach((meta, i) => {
      const last = zones[zones.length - 1];
      if (last && last.name === meta.zone) return;
      zones.push({ name: meta.zone, startDist: sim.cumDist[i], endIdx: i });
    });
    if (!zones.length) return [];

    const traveled = sim.distanceTraveledM || 0;
    const total = sim.totalDistanceM || 1;
    const eta = Math.max(0, sim.etaSeconds || 0); // seconds to destination, demo-time aware

    return zones.map((z, idx) => {
      const fracFromNow = (z.startDist - traveled) / total;
      const reachIn = Math.max(0, eta * fracFromNow);
      const state = z.name === sim.currentZone && simActive ? 'now'
        : z.startDist <= traveled ? 'passed'
        : 'upcoming';
      return {
        id: idx,
        name: z.name,
        reachIn,
        clock: clockAt(reachIn, lang),
        state,
        alternate: ZONE_ALTERNATES[z.name] || FALLBACK_ALTERNATE,
      };
    });
  }, [sim, simActive, lang]);

  const nextStretch = stretches.find((s) => s.state === 'upcoming');
  const holdStretch = nextStretch || stretches.find((s) => s.state === 'now');

  const phoneValid = /^[6-9]\d{9}$/.test(phone.trim());

  const handleSubmit = (e) => {
    e.preventDefault();
    if (phoneValid) setSubscribed(true);
  };

  const handleGreenRequest = (e) => {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    setGreenRequest({
      vehicle: form.get('vehicle') || '108-AHM-0000',
      kind: form.get('kind') || 'ambulance',
    });
  };

  // Demo SMS preview — exactly what the subscriber would receive, in script.
  const alertPreview = holdStretch ? (
    `${t(lang, 'brand').toUpperCase()}: ${holdStretch.name} — ${t(lang, 'freezeMinutes', HOLD_MINUTES)} ` +
    `@ ${holdStretch.clock}. ${t(lang, 'alternate')}: ${holdStretch.alternate}.`
  ) : null;

  const page = {
    minHeight: '100vh',
    background: 'linear-gradient(180deg,#f8fafc 0%,#eef2f7 100%)',
    fontFamily: "'Inter', system-ui, -apple-system, 'Noto Sans Devanagari', 'Noto Sans Gujarati', sans-serif",
    color: '#0f172a',
    padding: '0 16px 48px',
  };
  const shell = { maxWidth: 760, margin: '0 auto' };
  const card = {
    background: '#ffffff',
    borderRadius: 16,
    border: '1px solid #e2e8f0',
    boxShadow: '0 1px 3px rgba(15,23,42,0.06)',
    padding: '20px 22px',
  };

  return (
    <div style={page}>
      <div style={shell}>
        {/* ── Header ─────────────────────────────────────────────── */}
        <header style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '18px 0 6px' }}>
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <div style={{ width: 38, height: 38, borderRadius: 12, background: 'linear-gradient(135deg,#ea580c,#f59e0b)', display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#fff', fontWeight: 800, fontSize: 18 }}>🛣</div>
              <div>
                <div style={{ fontWeight: 800, fontSize: 17, letterSpacing: -0.2 }}>{t(lang, 'brand')}</div>
                <div style={{ fontSize: 12.5, color: '#64748b' }}>{t(lang, 'tagline')}</div>
              </div>
            </div>
          </div>
          <div style={{ display: 'flex', gap: 6 }}>
            {SUPPORTED.map((s) => (
              <button key={s.code} onClick={() => setLang(s.code)}
                style={{
                  padding: '6px 12px', borderRadius: 999, fontSize: 12.5, fontWeight: 700, cursor: 'pointer',
                  border: lang === s.code ? '1px solid #ea580c' : '1px solid #e2e8f0',
                  background: lang === s.code ? '#fff7ed' : '#fff',
                  color: lang === s.code ? '#c2410c' : '#475569',
                }}>
                {s.label}
              </button>
            ))}
          </div>
        </header>
        <div style={{ fontSize: 11.5, color: '#94a3b8', padding: '2px 0 14px', display: 'flex', justifyContent: 'space-between', flexWrap: 'wrap', gap: 8 }}>
          <span>● {t(lang, 'updated')}</span>
          <button onClick={() => navigate('/')} style={{ background: 'none', border: 'none', color: '#94a3b8', fontSize: 11.5, cursor: 'pointer', textDecoration: 'underline', padding: 0 }}>
            {t(lang, 'backToOps')} →
          </button>
        </div>

        {/* ── Live status banner ─────────────────────────────────── */}
        <section style={{ ...card, marginBottom: 14, borderTop: `4px solid ${simActive ? '#ea580c' : simDone ? '#16a34a' : '#16a34a'}` }}>
          <div style={{ display: 'flex', alignItems: 'flex-start', gap: 14, flexWrap: 'wrap' }}>
            <div style={{ flex: 1, minWidth: 240 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <span style={{
                  width: 10, height: 10, borderRadius: 999, flexShrink: 0,
                  background: simActive ? '#ea580c' : '#16a34a',
                  boxShadow: simActive ? '0 0 0 4px rgba(234,88,12,0.15)' : '0 0 0 4px rgba(22,163,74,0.15)',
                  animation: simActive ? 'pulse 1.6s infinite' : 'none',
                }} />
                <h1 style={{ margin: 0, fontSize: 19, fontWeight: 800 }}>
                  {simActive ? t(lang, 'statusActive') : simDone ? t(lang, 'statusDone') : t(lang, 'statusClear')}
                </h1>
              </div>
              <p style={{ margin: '6px 0 0', color: '#475569', fontSize: 13.5 }}>
                {simActive ? t(lang, 'statusActiveSub') : simDone ? t(lang, 'statusDoneSub') : t(lang, 'statusClearSub')}
              </p>
              {simActive && sim.progress != null && (
                <div style={{ marginTop: 12 }}>
                  <div style={{ height: 6, borderRadius: 999, background: '#e2e8f0', overflow: 'hidden' }}>
                    <div style={{ width: `${Math.round(sim.progress * 100)}%`, height: '100%', background: 'linear-gradient(90deg,#ea580c,#f59e0b)', transition: 'width 0.3s linear' }} />
                  </div>
                  <div style={{ fontSize: 11, color: '#94a3b8', marginTop: 4 }}>
                    {localizeNumber(Math.round(sim.progress * 100), lang)}% · {sim.currentZone}
                  </div>
                </div>
              )}
            </div>
            {!simActive && !simDone && (
              <button onClick={startDemoSimulation}
                style={{
                  display: 'flex', alignItems: 'center', gap: 8, padding: '10px 16px', borderRadius: 12,
                  background: 'linear-gradient(135deg,#ea580c,#f97316)', color: '#fff', border: 'none',
                  fontWeight: 800, fontSize: 13, cursor: 'pointer', boxShadow: '0 4px 14px rgba(234,88,12,0.3)',
                }}>
                ▶ {t(lang, 'startDemo')}
              </button>
            )}
          </div>
        </section>

        {/* ── Freeze windows ─────────────────────────────────────── */}
        {stretches.length > 0 && (
          <section style={{ ...card, marginBottom: 14 }}>
            <h2 style={{ margin: '0 0 12px', fontSize: 15, fontWeight: 800 }}>{t(lang, 'freezeTitle')}</h2>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              {stretches.map((s) => (
                <div key={s.id} style={{
                  display: 'grid', gridTemplateColumns: 'minmax(0,1.1fr) auto minmax(0,1.4fr)', gap: 10,
                  alignItems: 'center', padding: '10px 12px', borderRadius: 10,
                  background: s.state === 'now' ? '#fff7ed' : s.state === 'passed' ? '#f8fafc' : '#fff',
                  border: `1px solid ${s.state === 'now' ? '#fdba74' : '#eef2f7'}`,
                  opacity: s.state === 'passed' ? 0.55 : 1,
                }}>
                  <div>
                    <div style={{ fontWeight: 700, fontSize: 13.5 }}>📍 {s.name}</div>
                    {s.state === 'now' && <div style={{ fontSize: 11, color: '#c2410c', fontWeight: 800, marginTop: 2 }}>● {t(lang, 'holdNow')}</div>}
                    {s.state === 'passed' && <div style={{ fontSize: 11, color: '#16a34a', fontWeight: 700, marginTop: 2 }}>✓ {t(lang, 'passed')}</div>}
                  </div>
                  <div style={{ textAlign: 'center', whiteSpace: 'nowrap' }}>
                    <div style={{ fontSize: 15, fontWeight: 800, color: s.state === 'upcoming' ? '#ea580c' : '#334155' }}>
                      {s.state === 'passed' ? '—' : s.clock}
                    </div>
                    <div style={{ fontSize: 10.5, color: '#94a3b8' }}>
                      {s.state === 'passed' ? '' : t(lang, 'freezeWindow')}
                    </div>
                  </div>
                  <div style={{ fontSize: 12, color: '#475569' }}>
                    <div style={{ fontSize: 10, textTransform: 'uppercase', letterSpacing: 0.4, color: '#94a3b8', fontWeight: 700 }}>{t(lang, 'alternate')}</div>
                    {s.alternate}
                  </div>
                </div>
              ))}
            </div>
            <div style={{ marginTop: 12, padding: '10px 12px', background: '#f0fdf4', border: '1px solid #bbf7d0', borderRadius: 10, fontSize: 12.5, color: '#166534' }}>
              <strong>{t(lang, 'why')}</strong> — {t(lang, 'whyBody')}
            </div>
          </section>
        )}

        {/* ── Emergency green corridor ───────────────────────────── */}
        <section style={{ ...card, marginBottom: 14, background: 'linear-gradient(135deg,#ecfdf5,#f0fdfa)', borderColor: '#a7f3d0' }}>
          <h2 style={{ margin: '0 0 4px', fontSize: 15, fontWeight: 800, color: '#065f46' }}>🚑 {t(lang, 'emergencyTitle')}</h2>
          <p style={{ margin: '0 0 12px', fontSize: 13, color: '#047857' }}>{t(lang, 'emergencyBody')}</p>
          {greenRequest ? (
            <div style={{ padding: '12px 14px', background: '#fff', border: '1px solid #a7f3d0', borderRadius: 10, fontSize: 13, color: '#065f46' }}>
              ✓ <strong>{greenRequest.vehicle}</strong> ({greenRequest.kind}) → {t(lang, 'emergencyCta')} · <span style={{ fontSize: 11, color: '#94a3b8' }}>demo</span>
              <button onClick={() => navigate('green-corridor')}
                style={{ display: 'block', marginTop: 8, background: '#ecfdf5', border: '1px solid #a7f3d0', color: '#047857', borderRadius: 8, padding: '6px 10px', fontSize: 12, fontWeight: 700, cursor: 'pointer' }}>
                🚑 {t(lang, 'openDriverView')} →
              </button>
            </div>
          ) : (
            <form onSubmit={handleGreenRequest} style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
              <input name="vehicle" placeholder="108-AHM-0000" defaultValue="108-AHM-0000"
                style={{ flex: '1 1 140px', padding: '9px 12px', borderRadius: 10, border: '1px solid #a7f3d0', fontSize: 13 }} />
              <select name="kind" defaultValue="ambulance"
                style={{ padding: '9px 12px', borderRadius: 10, border: '1px solid #a7f3d0', fontSize: 13, background: '#fff' }}>
                <option value="ambulance">🚑 Ambulance</option>
                <option value="fire">🚒 Fire</option>
                <option value="police">🚓 Police</option>
              </select>
              <button type="submit"
                style={{ padding: '9px 16px', borderRadius: 10, border: 'none', background: '#059669', color: '#fff', fontWeight: 800, fontSize: 13, cursor: 'pointer' }}>
                {t(lang, 'emergencyCta')} →
              </button>
              <button type="button" onClick={() => navigate('green-corridor')}
                style={{ padding: '9px 12px', borderRadius: 10, border: '1px solid #a7f3d0', background: '#fff', color: '#047857', fontWeight: 700, fontSize: 12, cursor: 'pointer' }}>
                🚑 {t(lang, 'openDriverView')} →
              </button>
            </form>
          )}
        </section>

        {/* ── Alert subscription (demo transport) ────────────────── */}
        <section style={card}>
          <h2 style={{ margin: '0 0 4px', fontSize: 15, fontWeight: 800 }}>🔔 {t(lang, 'subscribeTitle')}</h2>
          <p style={{ margin: '0 0 12px', fontSize: 13, color: '#64748b' }}>{t(lang, 'subscribeSub')}</p>
          <form onSubmit={handleSubmit} style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'flex-end' }}>
            <label style={{ flex: '1 1 160px', fontSize: 11.5, color: '#475569', fontWeight: 700 }}>
              {t(lang, 'phoneLabel')}
              <input value={phone} onChange={(e) => { setPhone(e.target.value); setSubscribed(false); }}
                placeholder={t(lang, 'phonePlaceholder')} inputMode="numeric" maxLength={10}
                style={{ width: '100%', marginTop: 4, padding: '10px 12px', borderRadius: 10, border: '1px solid #e2e8f0', fontSize: 14 }} />
            </label>
            <label style={{ fontSize: 11.5, color: '#475569', fontWeight: 700 }}>
              {t(lang, 'languageLabel')}
              <select value={alertLang} onChange={(e) => setAlertLang(e.target.value)}
                style={{ display: 'block', marginTop: 4, padding: '10px 12px', borderRadius: 10, border: '1px solid #e2e8f0', fontSize: 14, background: '#fff' }}>
                {SUPPORTED.map((s) => <option key={s.code} value={s.code}>{s.label}</option>)}
              </select>
            </label>
            <button type="submit" disabled={!phoneValid}
              style={{
                padding: '11px 20px', borderRadius: 10, border: 'none', fontWeight: 800, fontSize: 13, cursor: phoneValid ? 'pointer' : 'not-allowed',
                background: phoneValid ? 'linear-gradient(135deg,#ea580c,#f97316)' : '#e2e8f0',
                color: phoneValid ? '#fff' : '#94a3b8',
              }}>
              {t(lang, 'subscribeBtn')}
            </button>
          </form>

          {subscribed && alertPreview && (
            <div style={{ marginTop: 14 }}>
              <div style={{ fontSize: 10.5, fontWeight: 800, color: '#c2410c', textTransform: 'uppercase', letterSpacing: 0.5 }}>
                ⚠ {t(lang, 'demoBadge')}
              </div>
              <div style={{ marginTop: 8, background: '#0f172a', color: '#e2e8f0', borderRadius: 14, padding: '12px 16px', maxWidth: 340, fontSize: 13, lineHeight: 1.5, boxShadow: '0 8px 20px rgba(15,23,42,0.25)' }}>
                <div style={{ fontSize: 10, color: '#64748b', marginBottom: 4 }}>{t(lang, 'previewTitle')}</div>
                {alertPreview}
              </div>
              <div style={{ fontSize: 12, color: '#16a34a', fontWeight: 700, marginTop: 8 }}>✓ {t(lang, 'subscribed')}</div>
            </div>
          )}
          {subscribed && !alertPreview && (
            <div style={{ marginTop: 12, fontSize: 12.5, color: '#16a34a', fontWeight: 700 }}>✓ {t(lang, 'subscribed')}</div>
          )}
        </section>

        <footer style={{ marginTop: 18, fontSize: 11, color: '#94a3b8', textAlign: 'center', lineHeight: 1.6 }}>
          {t(lang, 'footerNote')}
        </footer>
      </div>
    </div>
  );
}

export default CitizenAdvisory;
