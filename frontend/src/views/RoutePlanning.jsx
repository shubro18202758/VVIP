import React, { useState, useEffect, useCallback } from 'react';
import { ArrowLeft, Navigation, AlertTriangle, CheckCircle, Clock, Zap, Shield, Brain, TrendingUp, Activity } from 'lucide-react';
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer, LineChart, Line, RadarChart, PolarGrid, PolarAngleAxis, Radar } from 'recharts';
import { useConvoy } from '../context/ConvoyContext';
import * as api from '../services/api';

const RoutePlanning = ({ navigate }) => {
  const { lifecycle, planResult, startEscort, movementId, addReasoning, corridorSummary } = useConvoy();

  // VVIP class security specs for dynamic diversion params
  const VVIP_DIVERSION_PARAMS = {
    'Z+': { convoy_speed_kmh: 40.0, advance_closure_sec: 180 },
    'Z':  { convoy_speed_kmh: 50.0, advance_closure_sec: 120 },
    'Y':  { convoy_speed_kmh: 60.0, advance_closure_sec: 60 },
    'X':  { convoy_speed_kmh: 60.0, advance_closure_sec: 0 },
  };
  const vvipClass = planResult?.vvip_class || 'Z';
  const divParams = VVIP_DIVERSION_PARAMS[vvipClass] || VVIP_DIVERSION_PARAMS['Z'];

  const [routes, setRoutes] = useState([]);
  const [loading, setLoading] = useState(true);
  const [diversions, setDiversions] = useState([]);
  const [loadingDiversions, setLoadingDiversions] = useState(false);
  const [scenarioResult, setScenarioResult] = useState(null);
  const [flowForecasts, setFlowForecasts] = useState({});
  const [etaPredictions, setEtaPredictions] = useState({});
  const [selectedRoute, setSelectedRoute] = useState(null);
  const [escortLoading, setEscortLoading] = useState(false);
  const [escortError, setEscortError] = useState(null);

  const fetchRoutes = useCallback(async () => {
    setLoading(true);
    try {
      // Use planResult segment IDs if available, otherwise derive from corridor summary
      const originSeg = planResult?.primary_route?.segment_ids?.[0] || corridorSummary?.segment_ids?.[0] || null;
      const destSeg = planResult?.primary_route?.segment_ids?.slice(-1)?.[0] || corridorSummary?.segment_ids?.slice(-1)?.[0] || null;
      if (!originSeg || !destSeg) {
        addReasoning({ type: 'warning', content: 'No segment IDs available — deploy a movement from the planner first.' });
        setLoading(false);
        return;
      }
      const res = await api.findRoutes({
        origin_segment: originSeg,
        destination_segment: destSeg,
        max_candidates: 4,
      });

      if (res?.data?.routes) {
        const mapped = res.data.routes.map((r, idx) => ({
          id: r.route_id || `route-${idx + 1}`,
          name: r.route_name || `Route ${idx + 1}`,
          distance: r.total_distance_m != null ? `${(r.total_distance_m / 1000).toFixed(1)} km` : 'N/A',
          distanceM: r.total_distance_m || 0,
          time: r.estimated_time_sec != null ? `${Math.round(r.estimated_time_sec / 60)} min` : 'N/A',
          timeSec: r.estimated_time_sec || 0,
          securityScore: r.security_score != null ? Math.round(r.security_score * 100) : 0,
          disruptionScore: r.disruption_score != null ? Math.round(r.disruption_score * 100) : 0,
          compositeScore: r.composite_score != null ? r.composite_score : 0,
          segment_ids: r.segment_ids || [],
          reason: r.reason || '',
          risk: r.security_score > 0.8 ? 'Low' : r.security_score > 0.5 ? 'Medium' : 'High',
        }));
        const sorted = mapped.sort((a, b) => b.compositeScore - a.compositeScore);
        setRoutes(sorted);
        setSelectedRoute(sorted[0]?.id || null);

        if (sorted[0]?.segment_ids.length) {
          fetchDiversions(sorted[0].segment_ids);
          fetchFlowForRoute(sorted[0]);
        }
        // Fetch ETA for each route
        sorted.forEach(r => fetchEta(r));
        // Fetch scenario evaluation
        fetchScenarios(sorted);
      }
    } catch (err) {
      addReasoning({ type: 'error', content: `Route fetch failed: ${err.message}` });
    } finally {
      setLoading(false);
    }
  }, [addReasoning]);

  useEffect(() => { fetchRoutes(); }, [fetchRoutes]);

  const fetchDiversions = async (segmentIds) => {
    setLoadingDiversions(true);
    try {
      const res = await api.planDiversions({
        route_segment_ids: segmentIds.slice(0, 8),
        convoy_speed_kmh: divParams.convoy_speed_kmh,
        advance_closure_sec: divParams.advance_closure_sec,
        departure_time: new Date().toISOString(),
      });
      if (res?.data?.diversions) setDiversions(res.data.diversions);
    } catch { /* silent */ }
    setLoadingDiversions(false);
  };

  const fetchEta = async (route) => {
    if (!route.segment_ids.length) return;
    try {
      const res = await api.predictEta({
        segment_ids: route.segment_ids,
        route_length_m: route.distanceM,
        num_segments: route.segment_ids.length,
        num_signals: Math.ceil(route.segment_ids.length / 3),
        vvip_class: planResult?.vvip_class || 'Z',
      });
      if (res?.data) {
        setEtaPredictions(prev => ({ ...prev, [route.id]: res.data }));
      }
    } catch { /* silent */ }
  };

  const fetchFlowForRoute = async (route) => {
    if (!route.segment_ids.length) return;
    try {
      const res = await api.predictFlow({
        segment_ids: route.segment_ids.slice(0, 10),
        horizons: [5, 10, 15, 30],
      });
      if (res?.data?.predictions) {
        setFlowForecasts(prev => ({ ...prev, [route.id]: res.data.predictions }));
      }
    } catch { /* silent */ }
  };

  const fetchScenarios = async (routeList) => {
    if (routeList.length < 2) return;
    try {
      const scenarios = routeList.slice(0, 3).map(r => ({
        route_id: r.id,
        segment_ids: r.segment_ids,
      }));
      const res = await api.evaluateScenarios({ scenarios, num_simulations: 100 });
      if (res?.data) setScenarioResult(res.data);
    } catch { /* silent */ }
  };

  const handleDeploy = async (routeId) => {
    if (!movementId) return;
    setEscortLoading(true);
    setEscortError(null);
    try {
      await startEscort();
      addReasoning({ type: 'info', content: `Escort started for route ${routeId}` });
    } catch (err) {
      setEscortError(err.message);
      addReasoning({ type: 'error', content: `Escort start failed: ${err.message}` });
    }
    setEscortLoading(false);
  };

  const handleRouteSelect = (route) => {
    setSelectedRoute(route.id);
    fetchDiversions(route.segment_ids);
    fetchFlowForRoute(route);
  };

  // Build radar data for selected route
  const selectedRouteObj = routes.find(r => r.id === selectedRoute);
  const radarData = selectedRouteObj ? [
    { metric: 'Security', value: selectedRouteObj.securityScore },
    { metric: 'Speed', value: Math.min(100, Math.round((60 / Math.max(1, selectedRouteObj.timeSec / 60)) * 100)) },
    { metric: 'Low Disruption', value: Math.max(0, 100 - selectedRouteObj.disruptionScore) },
    { metric: 'Segments', value: Math.min(100, selectedRouteObj.segment_ids.length * 5) },
    { metric: 'Composite', value: Math.round(selectedRouteObj.compositeScore * 100) },
  ] : [];

  // Build flow forecast chart data from selected route
  const flowData = (() => {
    const preds = flowForecasts[selectedRoute];
    if (!preds?.length) return [];
    const horizons = [5, 10, 15, 30];
    return horizons.map((h, i) => {
      const pred = preds.find(p => p.horizon === h) || preds[i];
      return {
        horizon: `T+${h}m`,
        speed: pred?.avg_speed_kmh?.toFixed(0) || 0,
        congestion: pred?.avg_congestion_idx ? (pred.avg_congestion_idx * 100).toFixed(0) : 0,
      };
    });
  })();

  return (
    <div style={{ height: '100vh', width: '100vw', backgroundColor: '#0f172a', display: 'flex', flexDirection: 'column', padding: '20px', overflow: 'hidden' }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '20px', flexShrink: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '16px' }}>
          <button
            onClick={() => navigate('/')}
            style={{ width: '36px', height: '36px', borderRadius: '10px', backgroundColor: '#1e293b', border: '1px solid #334155', display: 'flex', alignItems: 'center', justifyContent: 'center', cursor: 'pointer', color: '#94a3b8' }}
          >
            <ArrowLeft size={18} />
          </button>
          <div>
            <h1 style={{ fontSize: '20px', fontWeight: 800, color: '#e2e8f0', margin: 0 }}>Route Optimization Engine</h1>
            <p style={{ fontSize: '12px', color: '#94a3b8', margin: 0 }}>
              OR-Tools CP-SAT · DSTGAT Flow · HistGBT ETA · Monte Carlo Scenarios
            </p>
          </div>
        </div>
        <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
          <span className="badge badge-orange" style={{ padding: '4px 12px', fontSize: '10px' }}>
            {lifecycle !== 'idle' ? lifecycle.toUpperCase() : 'TACTICAL'}
          </span>
          {corridorSummary?.avg_congestion_idx != null && (
            <span className={`badge ${corridorSummary.avg_congestion_idx > 0.5 ? 'badge-red' : 'badge-green'}`} style={{ padding: '4px 12px', fontSize: '10px' }}>
              CGX {(corridorSummary.avg_congestion_idx).toFixed(3)}
            </span>
          )}
          {planResult?.confidence && (
            <span className="badge badge-blue" style={{ padding: '4px 12px', fontSize: '10px' }}>Confidence: {planResult.confidence}</span>
          )}
        </div>
      </div>

      {/* Main Content */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 380px', gap: '20px', flex: 1, overflow: 'hidden' }}>

        {/* Left Column: Route List */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: '14px', overflowY: 'auto', paddingRight: '8px' }}>
          {loading ? (
            <div style={{ padding: '40px', textAlign: 'center', color: '#94a3b8' }}>
              <div className="animate-spin" style={{ margin: '0 auto 12px', width: '28px', height: '28px', border: '3px solid #ea580c', borderTopColor: 'transparent', borderRadius: '50%' }} />
              Running OR-Tools CP-SAT Heuristics...
            </div>
          ) : routes.map((route, idx) => {
            const eta = etaPredictions[route.id];
            const isSelected = selectedRoute === route.id;
            return (
              <div
                key={route.id}
                onClick={() => handleRouteSelect(route)}
                style={{
                  backgroundColor: '#1e293b',
                  borderRadius: '12px',
                  border: isSelected ? '2px solid #ea580c' : '1px solid #334155',
                  padding: '16px',
                  cursor: 'pointer',
                  boxShadow: isSelected ? '0 4px 12px rgba(234,88,12,0.1)' : '0 2px 4px rgba(0,0,0,0.03)',
                  position: 'relative',
                  transition: 'border-color 0.2s, box-shadow 0.2s',
                }}
              >
                {idx === 0 && (
                  <div style={{ position: 'absolute', top: 0, right: 0, backgroundColor: '#ea580c', color: '#fff', fontSize: '8px', padding: '3px 10px', fontWeight: 800, borderBottomLeftRadius: '8px', letterSpacing: '0.05em' }}>RECOMMENDED</div>
                )}

                <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '12px' }}>
                  <div style={{ width: '28px', height: '28px', borderRadius: '6px', backgroundColor: idx === 0 ? 'rgba(234,88,12,0.15)' : '#334155', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                    <Navigation size={14} color={idx === 0 ? '#ea580c' : '#64748b'} />
                  </div>
                  <h3 style={{ fontSize: '14px', fontWeight: 700, margin: 0, color: '#e2e8f0' }}>{route.name}</h3>
                </div>

                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: '8px', marginBottom: '10px' }}>
                  <div>
                    <div style={{ fontSize: '9px', color: '#94a3b8', textTransform: 'uppercase' }}>Distance</div>
                    <div style={{ fontSize: '13px', fontWeight: 600, color: '#e2e8f0' }}>{route.distance}</div>
                  </div>
                  <div>
                    <div style={{ fontSize: '9px', color: '#94a3b8', textTransform: 'uppercase' }}>Time</div>
                    <div style={{ fontSize: '13px', fontWeight: 600, color: '#16a34a' }}>{route.time}</div>
                  </div>
                  <div>
                    <div style={{ fontSize: '9px', color: '#94a3b8', textTransform: 'uppercase' }}>Security</div>
                    <div style={{ fontSize: '13px', fontWeight: 600, color: '#e2e8f0' }}>{route.securityScore}%</div>
                  </div>
                  <div>
                    <div style={{ fontSize: '9px', color: '#ea580c', textTransform: 'uppercase', fontWeight: 700 }}>Disruption</div>
                    <div style={{ fontSize: '13px', fontWeight: 600, color: '#e2e8f0' }}>{route.disruptionScore}%</div>
                  </div>
                  <div>
                    <div style={{ fontSize: '9px', color: '#2563eb', textTransform: 'uppercase', fontWeight: 700 }}>ETA (AI)</div>
                    <div style={{ fontSize: '13px', fontWeight: 700, fontFamily: 'var(--font-mono)', color: '#2563eb' }}>
                      {eta?.predicted_travel_time_sec ? `${Math.round(eta.predicted_travel_time_sec / 60)}m` : '—'}
                    </div>
                  </div>
                </div>

                {route.reason && (
                  <div style={{ fontSize: '10px', color: '#94a3b8', backgroundColor: '#0f172a', padding: '6px 8px', borderRadius: '6px', marginBottom: '10px', borderLeft: '2px solid #ea580c' }}>
                    <Brain size={10} style={{ display: 'inline', verticalAlign: 'middle', marginRight: '4px' }} />
                    {route.reason}
                  </div>
                )}

                <div className="flex justify-between items-center">
                  <div className="flex gap-2 items-center">
                    <span style={{ fontSize: '10px', color: '#94a3b8', fontFamily: 'var(--font-mono)' }}>
                      Composite: {route.compositeScore.toFixed(3)}
                    </span>
                    <span className={`badge ${route.risk === 'Low' ? 'badge-green' : route.risk === 'Medium' ? 'badge-orange' : 'badge-red'}`} style={{ border: 'none', fontSize: '8px', padding: '2px 6px' }}>
                      {route.risk} Risk
                    </span>
                  </div>
                  <button
                    onClick={(e) => { e.stopPropagation(); handleDeploy(route.id); }}
                    disabled={escortLoading || lifecycle === 'active'}
                    style={{
                      padding: '6px 14px',
                      backgroundColor: idx === 0 ? '#ea580c' : '#334155',
                      color: idx === 0 ? '#fff' : '#94a3b8',
                      border: idx === 0 ? 'none' : '1px solid #475569',
                      borderRadius: '6px', fontWeight: 700, cursor: lifecycle === 'active' ? 'not-allowed' : 'pointer', fontSize: '11px',
                      opacity: escortLoading ? 0.6 : 1,
                    }}
                  >
                    {escortLoading ? 'Starting...' : lifecycle === 'active' ? 'Escort Active' : 'Deploy →'}
                  </button>
                </div>
                {escortError && isSelected && <div style={{ fontSize: '9px', color: '#dc2626', marginTop: '6px' }}>{escortError}</div>}
              </div>
            );
          })}
        </div>

        {/* Right Column: Analytics & Diversion Matrix */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: '16px', overflowY: 'auto' }}>

          {/* Heuristic Comparison BarChart */}
          <div style={{ backgroundColor: '#1e293b', borderRadius: '12px', border: '1px solid #334155', padding: '14px' }}>
            <h4 style={{ fontSize: '12px', fontWeight: 700, marginBottom: '10px', color: '#e2e8f0' }}>Heuristic Comparison</h4>
            <div style={{ width: '100%', height: '150px' }}>
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={routes.map(r => ({ name: r.name.split(' ')[0], Security: r.securityScore, Disruption: r.disruptionScore, Composite: Math.round(r.compositeScore * 100) }))} margin={{ top: 5, right: 5, left: -25, bottom: 5 }}>
                  <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#334155" />
                  <XAxis dataKey="name" axisLine={false} tickLine={false} tick={{ fontSize: 9, fill: '#94a3b8' }} />
                  <YAxis axisLine={false} tickLine={false} tick={{ fontSize: 9, fill: '#94a3b8' }} />
                  <Tooltip cursor={{ fill: '#0f172a' }} contentStyle={{ borderRadius: '6px', border: '1px solid #334155', fontSize: '10px', backgroundColor: '#1e293b', color: '#e2e8f0' }} />
                  <Legend iconType="circle" wrapperStyle={{ fontSize: '10px' }} />
                  <Bar dataKey="Security" fill="#16a34a" radius={[3, 3, 0, 0]} />
                  <Bar dataKey="Disruption" fill="#ea580c" radius={[3, 3, 0, 0]} />
                  <Bar dataKey="Composite" fill="#2563eb" radius={[3, 3, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </div>

          {/* Radar Chart for Selected Route */}
          {radarData.length > 0 && (
            <div style={{ backgroundColor: '#1e293b', borderRadius: '12px', border: '1px solid #334155', padding: '14px' }}>
              <h4 style={{ fontSize: '12px', fontWeight: 700, marginBottom: '8px', color: '#e2e8f0', display: 'flex', alignItems: 'center', gap: '6px' }}>
                <Shield size={14} color="#ea580c" /> Route Profile
              </h4>
              <div style={{ width: '100%', height: '160px' }}>
                <ResponsiveContainer width="100%" height="100%">
                  <RadarChart data={radarData} cx="50%" cy="50%" outerRadius="75%">
                    <PolarGrid stroke="#334155" />
                    <PolarAngleAxis dataKey="metric" tick={{ fontSize: 9, fill: '#94a3b8' }} />
                    <Radar name="Score" dataKey="value" stroke="#ea580c" fill="#ea580c" fillOpacity={0.2} />
                  </RadarChart>
                </ResponsiveContainer>
              </div>
            </div>
          )}

          {/* DSTGAT Flow Forecast */}
          {flowData.length > 0 && (
            <div style={{ backgroundColor: '#1e293b', borderRadius: '12px', border: '1px solid #334155', padding: '14px' }}>
              <h4 style={{ fontSize: '12px', fontWeight: 700, marginBottom: '8px', color: '#e2e8f0', display: 'flex', alignItems: 'center', gap: '6px' }}>
                <TrendingUp size={14} color="#2563eb" /> DSTGAT Flow Forecast
              </h4>
              <div style={{ width: '100%', height: '130px' }}>
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={flowData} margin={{ top: 5, right: 5, left: -25, bottom: 5 }}>
                    <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#334155" />
                    <XAxis dataKey="horizon" tick={{ fontSize: 9, fill: '#94a3b8' }} />
                    <YAxis tick={{ fontSize: 9, fill: '#94a3b8' }} />
                    <Tooltip contentStyle={{ borderRadius: '6px', fontSize: '10px', backgroundColor: '#1e293b', border: '1px solid #334155', color: '#e2e8f0' }} />
                    <Line type="monotone" dataKey="speed" stroke="#16a34a" strokeWidth={2} dot={{ r: 3 }} name="Speed km/h" />
                    <Line type="monotone" dataKey="congestion" stroke="#dc2626" strokeWidth={2} dot={{ r: 3 }} name="Congestion %" />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            </div>
          )}

          {/* Monte Carlo Scenario Evaluation */}
          {scenarioResult && (
            <div style={{ backgroundColor: '#1e293b', borderRadius: '12px', border: '1px solid #334155', padding: '14px' }}>
              <h4 style={{ fontSize: '12px', fontWeight: 700, marginBottom: '8px', color: '#e2e8f0', display: 'flex', alignItems: 'center', gap: '6px' }}>
                <Activity size={14} color="#7c3aed" /> Scenario Evaluation
              </h4>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                {(scenarioResult.rankings || scenarioResult.scenarios || []).slice(0, 3).map((sc, i) => (
                  <div key={i} style={{ padding: '8px', backgroundColor: '#0f172a', borderRadius: '6px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <div>
                      <div style={{ fontSize: '10px', fontWeight: 700, color: '#e2e8f0' }}>{sc.route_id?.slice(0, 8) || `Scenario ${i + 1}`}</div>
                      <div style={{ fontSize: '9px', color: '#94a3b8' }}>
                        {sc.avg_travel_time_sec ? `Avg ${Math.round(sc.avg_travel_time_sec / 60)}m` : ''} 
                        {sc.p95_travel_time_sec ? ` · P95 ${Math.round(sc.p95_travel_time_sec / 60)}m` : ''}
                      </div>
                    </div>
                    <span style={{ fontSize: '11px', fontWeight: 700, fontFamily: 'var(--font-mono)', color: '#7c3aed' }}>
                      {sc.score?.toFixed(2) || sc.rank || i + 1}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Tactical Diversion Matrix */}
          <div style={{ backgroundColor: '#1e293b', borderRadius: '12px', padding: '14px', color: 'white' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '10px' }}>
              <AlertTriangle size={14} color="#facc15" />
              <span style={{ fontSize: '12px', fontWeight: 700 }}>Diversion Matrix</span>
              {diversions.length > 0 && <span style={{ fontSize: '9px', color: '#94a3b8', fontFamily: 'var(--font-mono)' }}>{diversions.length} plans</span>}
            </div>

            {loadingDiversions ? (
              <div style={{ padding: '8px', textAlign: 'center', color: '#94a3b8', fontSize: '10px' }}>
                <div className="animate-spin" style={{ margin: '0 auto 6px', width: '16px', height: '16px', border: '2px solid #ea580c', borderTopColor: 'transparent', borderRadius: '50%' }} />
                Simulating closures...
              </div>
            ) : diversions.length > 0 ? (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                {diversions.map((div, i) => (
                  <div key={i} style={{ backgroundColor: '#0f172a', padding: '8px 10px', borderRadius: '6px', borderLeft: '3px solid #ea580c' }}>
                    <div className="flex justify-between items-center" style={{ marginBottom: '2px' }}>
                      <span style={{ fontSize: '10px', fontWeight: 700, color: '#f8fafc' }}>
                        {div.diversion_type?.toUpperCase()} · SEG {div.segment_id}
                      </span>
                      <span style={{ fontSize: '9px', color: '#94a3b8' }}>
                        Queue: {Math.round(div.estimated_queue_m || 0)}m
                      </span>
                    </div>
                    <div className="flex justify-between items-center">
                      <span style={{ fontSize: '9px', color: '#94a3b8', fontFamily: 'var(--font-mono)' }}>
                        Dissipation: {Math.round((div.dissipation_time_sec || 0) / 60)}min
                      </span>
                      <span style={{ fontSize: '9px', color: '#3b82f6', fontWeight: 600 }}>
                        Alt paths: {div.alt_segment_ids?.length || 0}
                      </span>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <p style={{ fontSize: '10px', color: '#94a3b8', margin: 0 }}>No diversions needed — corridor sterile.</p>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};

export default RoutePlanning;
