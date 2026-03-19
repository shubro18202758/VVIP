import React, { useEffect, useState, useCallback, useMemo, useRef } from 'react';
import { MapContainer, TileLayer, Marker, Popup, Polyline, CircleMarker, useMap, GeoJSON } from 'react-leaflet';
import L from 'leaflet';
import { useConvoy } from '../context/ConvoyContext';
import * as api from '../services/api';

// Fix for default marker icons in Leaflet + React
delete L.Icon.Default.prototype._getIconUrl;
L.Icon.Default.mergeOptions({
  iconRetinaUrl: 'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.7.1/images/marker-icon-2x.png',
  iconUrl: 'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.7.1/images/marker-icon.png',
  shadowUrl: 'https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.7.1/images/marker-shadow.png',
});

const CONGESTION_COLORS = [
  { max: 0.2, color: '#16a34a', label: 'Free Flow' },
  { max: 0.4, color: '#84cc16', label: 'Light' },
  { max: 0.6, color: '#eab308', label: 'Moderate' },
  { max: 0.8, color: '#ea580c', label: 'Heavy' },
  { max: 1.0, color: '#dc2626', label: 'Severe' },
];

const getCongestionColor = (idx) => {
  for (const band of CONGESTION_COLORS) {
    if (idx <= band.max) return band.color;
  }
  return '#dc2626';
};

// Helper to update map view
const MapAutoCenter = ({ center }) => {
  const map = useMap();
  useEffect(() => {
    if (center) map.setView(center, map.getZoom());
  }, [center, map]);
  return null;
};

// Fit map bounds to route polylines
const FitBounds = ({ bounds }) => {
  const map = useMap();
  useEffect(() => {
    if (bounds && bounds.length >= 2) {
      map.fitBounds(bounds, { padding: [40, 40], maxZoom: 15 });
    }
  }, [bounds, map]);
  return null;
};

// Fly-to component: watches mapFlyTarget and animates map
const FlyToTarget = ({ target }) => {
  const map = useMap();
  useEffect(() => {
    if (target?.lat && target?.lng) {
      map.flyTo([target.lat, target.lng], target.zoom || 15, { duration: 1.2 });
    }
  }, [target?._ts]);
  return null;
};

// Follow convoy: gently re-center map when convoy leaves viewport
const ConvoyFollower = ({ simulation }) => {
  const map = useMap();
  const simRef = useRef(simulation);
  simRef.current = simulation;
  useEffect(() => {
    if (!simulation?.active) return;
    const iv = setInterval(() => {
      const sim = simRef.current;
      if (sim?.active && sim.position && !sim.paused) {
        const pos = [sim.position.lat, sim.position.lng];
        if (!map.getBounds().contains(pos)) {
          map.panTo(pos, { animate: true, duration: 0.8 });
        }
      }
    }, 3000);
    return () => clearInterval(iv);
  }, [simulation?.active, map]);
  return null;
};

const ConvoyMap = ({ overlays, routeData, mapStyle, movements }) => {
  const [mapCenter] = useState([23.0225, 72.5714]);
  const { planResult, anomalies, corridorSummary, selectedSegmentId, mapFlyTarget, setMapSegments, selectSegment, flyToSegment, tempOriginCoords, tempDestCoords, highlightedSegments, convoySimulation, flyToLocation } = useConvoy();

  const [liveTraffic, setLiveTraffic] = useState({});
  const [segments, setSegments] = useState([]);
  const [segmentsLoading, setSegmentsLoading] = useState(false);

  // Custom Marker Icons
  const createDivIcon = (color, size = 12, pulse = false) => L.divIcon({
    className: 'custom-div-icon',
    html: `<div style="position:relative;">
      ${pulse ? `<div style="position:absolute;inset:-6px;border-radius:50%;border:2px solid ${color};opacity:0.4;animation:pulse 2s infinite;"></div>` : ''}
      <div style="background:${color};width:${size}px;height:${size}px;border-radius:50%;border:2.5px solid white;box-shadow:0 0 8px ${color}44,0 2px 6px rgba(0,0,0,0.3);"></div>
    </div>`,
    iconSize: [size, size],
    iconAnchor: [size / 2, size / 2]
  });

  const tcpIcon = L.divIcon({
    className: 'custom-tcp-icon',
    html: `<div style="position:relative;">
      <div style="position:absolute;inset:-4px;border:1.5px solid #ea580c;transform:rotate(45deg);opacity:0.3;animation:pulse 2s infinite;"></div>
      <div style="background:linear-gradient(135deg,#ea580c,#c2410c);width:14px;height:14px;transform:rotate(45deg);border:1.5px solid #fed7aa;box-shadow:0 0 8px rgba(234,88,12,0.4);"></div>
    </div>`,
    iconSize: [14, 14],
    iconAnchor: [7, 7]
  });

  const anomalyIcon = L.divIcon({
    className: 'custom-anomaly-icon',
    html: `<div style="position:relative;">
      <div style="position:absolute;inset:-5px;border-radius:3px;border:1.5px solid #dc2626;opacity:0;animation:ripple 1.5s infinite;"></div>
      <div style="background:linear-gradient(135deg,#dc2626,#991b1b);width:10px;height:10px;border-radius:3px;border:1.5px solid #fca5a5;box-shadow:0 0 10px rgba(220,38,38,0.6);"></div>
      <div style="position:absolute;top:-2px;right:-2px;width:5px;height:5px;background:#fbbf24;border-radius:50%;border:1px solid white;"></div>
    </div>`,
    iconSize: [10, 10],
    iconAnchor: [5, 5]
  });

  const vvipIcon = createDivIcon('#ea580c', 16, true);
  const trafficIcon = createDivIcon('#2563eb', 12, false);

  // Tiles
  const lightTiles = "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png";
  const darkTiles = "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png";
  const liveTiles = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png";

  // Helpers to extract coords from segment geometry [lon,lat] arrays
  const segStart = (seg) => {
    const c = seg?.geometry?.coordinates;
    return c?.length ? [c[0][1], c[0][0]] : null;
  };
  const segEnd = (seg) => {
    const c = seg?.geometry?.coordinates;
    return c?.length ? [c[c.length - 1][1], c[c.length - 1][0]] : null;
  };
  const segMid = (seg) => {
    const c = seg?.geometry?.coordinates;
    if (!c?.length) return null;
    const m = Math.floor(c.length / 2);
    return [c[m][1], c[m][0]];
  };

  // Fetch segments in the viewport bbox for traffic overlay
  const fetchSegments = useCallback(async () => {
    setSegmentsLoading(true);
    try {
      // Use Ahmedabad corridor bounding box
      const res = await api.getSegmentsInBbox(72.48, 22.95, 72.68, 23.12);
      if (res?.data?.segments) {
        setSegments(res.data.segments);
        setMapSegments(res.data.segments);
      }
    } catch { /* silent */ }
    setSegmentsLoading(false);
  }, []);

  // Fetch live traffic for known segment IDs
  const fetchLiveTraffic = useCallback(async () => {
    const segIds = segments.map(s => s.segment_id).filter(Boolean).slice(0, 100);
    if (!segIds.length) return;
    try {
      const res = await api.getLiveTraffic(segIds);
      if (res?.data) {
        const tm = {};
        (Array.isArray(res.data) ? res.data : res.data.observations || []).forEach(obs => {
          tm[obs.segment_id] = obs;
        });
        setLiveTraffic(tm);
      }
    } catch { /* silent */ }
  }, [segments]);

  useEffect(() => { fetchSegments(); }, [fetchSegments]);

  useEffect(() => {
    if (!segments.length) return;
    fetchLiveTraffic();
    const iv = setInterval(fetchLiveTraffic, 30000);
    return () => clearInterval(iv);
  }, [segments, fetchLiveTraffic]);

  // Diversion TCPs from planResult
  const diversionTcps = useMemo(() => {
    if (!planResult?.diversion_directives) return [];
    return planResult.diversion_directives
      .filter(d => d.segment_id)
      .map((d, i) => {
        const seg = segments.find(s => s.segment_id === d.segment_id);
        const mid = segMid(seg);
        return {
          name: `TCP-${i + 1}`,
          segmentId: d.segment_id,
          action: d.action,
          agency: d.agency,
          lat: mid?.[0] || 23.02 + (i * 0.008),
          lng: mid?.[1] || 72.57 + (i * 0.01),
        };
      });
  }, [planResult, segments]);

  // Extract origin/destination from planResult or first movement
  const originCoords = useMemo(() => {
    if (planResult?.primary_route?.segment_ids?.length) {
      const seg = segments.find(s => s.segment_id === planResult.primary_route.segment_ids[0]);
      const start = segStart(seg);
      if (start) return start;
    }
    if (movements?.[0]?.origin_geom) {
      const g = movements[0].origin_geom;
      return g.coordinates ? [g.coordinates[1], g.coordinates[0]] : null;
    }
    return null;
  }, [planResult, movements, segments]);

  const destCoords = useMemo(() => {
    if (planResult?.primary_route?.segment_ids?.length) {
      const ids = planResult.primary_route.segment_ids;
      const seg = segments.find(s => s.segment_id === ids[ids.length - 1]);
      const end = segEnd(seg);
      if (end) return end;
    }
    if (movements?.[0]?.destination_geom) {
      const g = movements[0].destination_geom;
      return g.coordinates ? [g.coordinates[1], g.coordinates[0]] : null;
    }
    return null;
  }, [planResult, movements, segments]);

  // Compute bounds for auto-fit
  const fitBounds = useMemo(() => {
    const pts = [];
    if (originCoords) pts.push(originCoords);
    if (destCoords) pts.push(destCoords);
    movements?.forEach(mov => {
      if (mov.route_geometry?.coordinates) {
        mov.route_geometry.coordinates.forEach(c => pts.push([c[1], c[0]]));
      }
    });
    return pts.length >= 2 ? pts : null;
  }, [originCoords, destCoords, movements]);

  return (
    <div style={{ width: '100%', height: '100%', position: 'absolute', inset: 0 }}>
      <MapContainer
        center={mapCenter}
        zoom={13}
        zoomControl={false}
        maxBounds={[[22.90, 72.45], [23.15, 72.75]]}
        maxBoundsViscosity={1.0}
        style={{ width: '100%', height: '100%' }}
      >
        <FlyToTarget target={mapFlyTarget} />
        <ConvoyFollower simulation={convoySimulation} />
        <TileLayer
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
          url={mapStyle === 'live' ? liveTiles : mapStyle === 'light' ? lightTiles : darkTiles}
        />

        <MapAutoCenter center={mapCenter} />
        {fitBounds && <FitBounds bounds={fitBounds} />}

        {/* Segment-level traffic heatmap overlay */}
        {overlays.traffic && segments.map(seg => {
          if (!seg.geometry?.coordinates) return null;
          const traffic = liveTraffic[seg.segment_id];
          const congestion = traffic?.congestion_idx ?? 0.3;
          const coords = seg.geometry.coordinates.map(c => [c[1], c[0]]);
          if (coords.length < 2) return null;
          return (
            <React.Fragment key={`seg-${seg.segment_id}`}>
            <Polyline
              positions={coords}
              pathOptions={{
                color: getCongestionColor(congestion),
                weight: Math.max(3, (seg.lanes || 2) + 1),
                opacity: 0.65,
                lineCap: 'round',
                lineJoin: 'round',
              }}
              eventHandlers={{ click: () => selectSegment(seg.segment_id) }}
            >
              <Popup>
                <div style={{ fontSize: '10px', lineHeight: '1.6', minWidth: '140px' }}>
                  <div style={{ fontWeight: 700, color: '#0f172a', fontSize: '11px', marginBottom: '3px', borderBottom: '1px solid #e2e8f0', paddingBottom: '3px' }}>{seg.road_name || `Segment ${seg.segment_id}`}</div>
                  <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                    <span style={{ color: '#64748b' }}>Class</span>
                    <strong style={{ textTransform: 'capitalize' }}>{seg.road_class}</strong>
                  </div>
                  <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                    <span style={{ color: '#64748b' }}>Lanes</span>
                    <strong>{seg.lanes}</strong>
                  </div>
                  {traffic && (
                    <>
                      <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: '2px' }}>
                        <span style={{ color: '#64748b' }}>Speed</span>
                        <strong>{traffic.speed_kmh?.toFixed(0)} km/h</strong>
                      </div>
                      <div style={{ marginTop: '3px' }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '2px' }}>
                          <span style={{ color: '#64748b' }}>Congestion</span>
                          <span style={{ color: getCongestionColor(congestion), fontWeight: 700 }}>{(congestion * 100).toFixed(0)}%</span>
                        </div>
                        <div style={{ height: '3px', borderRadius: '2px', background: '#e2e8f0' }}>
                          <div style={{ height: '100%', borderRadius: '2px', width: `${congestion * 100}%`, background: getCongestionColor(congestion), transition: 'width 0.5s' }} />
                        </div>
                      </div>
                    </>
                  )}
                </div>
              </Popup>
            </Polyline>
            {/* Selected segment highlight glow */}
            {selectedSegmentId === seg.segment_id && (
              <Polyline
                positions={coords}
                pathOptions={{
                  color: '#f97316',
                  weight: Math.max(3, (seg.lanes || 2) + 1) + 6,
                  opacity: 0.45,
                  lineCap: 'round',
                  lineJoin: 'round',
                }}
                interactive={false}
              />
            )}
          </React.Fragment>
          );
        })}

        {/* Dynamic Route Polylines from active movements */}
        {movements && movements.map((mov, idx) => {
          const isPrimary = idx === 0;
          let coords = [];
          if (mov.route_geometry?.coordinates) {
            coords = mov.route_geometry.coordinates.map(c => [c[1], c[0]]);
          } else if (mov.origin_geom?.coordinates && mov.destination_geom?.coordinates) {
            const o = mov.origin_geom.coordinates;
            const d = mov.destination_geom.coordinates;
            coords = [[o[1], o[0]], [d[1], d[0]]];
          }
          if (!coords.length) return null;

          return (
            <React.Fragment key={`route-${mov.movement_id || idx}`}>
              {/* Route glow effect */}
              <Polyline
                positions={coords}
                pathOptions={{
                  color: isPrimary ? '#ea580c' : '#2563eb',
                  weight: isPrimary ? 8 : 5,
                  opacity: 0.2,
                  lineJoin: 'round',
                }}
              />
              {/* Main route line */}
              <Polyline
                positions={coords}
                pathOptions={{
                  color: isPrimary ? '#ea580c' : '#2563eb',
                  weight: isPrimary ? 4 : 3,
                  opacity: 0.9,
                  dashArray: isPrimary ? null : '8, 6',
                  lineJoin: 'round',
                }}
              />
              {mov.current_position && (
                <Marker
                  position={[mov.current_position.lat, mov.current_position.lng]}
                  icon={isPrimary ? vvipIcon : trafficIcon}
                  zIndexOffset={100}
                >
                  <Popup>
                    <div style={{ padding: '4px', fontSize: '11px', minWidth: '150px' }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '6px', borderBottom: '1px solid #e2e8f0', paddingBottom: '4px', marginBottom: '4px' }}>
                        <div style={{ width: '8px', height: '8px', borderRadius: '50%', background: isPrimary ? '#ea580c' : '#2563eb', boxShadow: `0 0 6px ${isPrimary ? '#ea580c' : '#2563eb'}66` }} />
                        <span style={{ fontWeight: 700, color: '#0f172a' }}>Convoy {mov.movement_id?.slice(0, 8)}</span>
                      </div>
                      <div style={{ display: 'inline-block', background: `${isPrimary ? '#ea580c' : '#2563eb'}18`, color: isPrimary ? '#ea580c' : '#2563eb', fontSize: '9px', fontWeight: 700, padding: '1px 6px', borderRadius: '4px', marginBottom: '3px' }}>
                        LIVE TRACKING
                      </div>
                      <div style={{ fontFamily: 'var(--font-mono)', fontSize: '10px', color: '#64748b' }}>
                        {mov.vvip_class} Class · <span style={{ color: mov.status === 'active' ? '#16a34a' : '#94a3b8' }}>{mov.status}</span>
                      </div>
                    </div>
                  </Popup>
                </Marker>
              )}
            </React.Fragment>
          );
        })}

        {/* Plan result PRIMARY route — highlighted orange */}
        {planResult?.primary_route?.segment_ids && (() => {
          const routeSegs = (planResult.primary_route.segment_ids || [])
            .map(sid => segments.find(s => s.segment_id === sid))
            .filter(s => s?.geometry?.coordinates);
          return routeSegs.map((seg, si) => (
            <React.Fragment key={`pri-${si}`}>
              <Polyline
                positions={seg.geometry.coordinates.map(c => [c[1], c[0]])}
                pathOptions={{ color: '#ea580c', weight: 8, opacity: 0.15, lineJoin: 'round' }}
              />
              <Polyline
                positions={seg.geometry.coordinates.map(c => [c[1], c[0]])}
                pathOptions={{ color: '#ea580c', weight: 4, opacity: 0.9, lineJoin: 'round' }}
              >
                <Popup>
                  <div style={{ fontSize: '10px' }}>
                    <div style={{ fontWeight: 700, color: '#ea580c' }}>Primary Route</div>
                    <div>{seg.road_name || `Seg ${seg.segment_id}`}</div>
                    <div>Score: {planResult.primary_route.score?.toFixed(2)}</div>
                    <div style={{ color: '#64748b' }}>{planResult.primary_route.reason}</div>
                  </div>
                </Popup>
              </Polyline>
            </React.Fragment>
          ));
        })()}

        {/* Plan result alternate routes */}
        {planResult?.alternate_routes?.map((route, idx) => {
          const routeSegs = (route.segment_ids || [])
            .map(sid => segments.find(s => s.segment_id === sid))
            .filter(s => s?.geometry?.coordinates);
          return routeSegs.map((seg, si) => (
            <Polyline
              key={`alt-${idx}-${si}`}
              positions={seg.geometry.coordinates.map(c => [c[1], c[0]])}
              pathOptions={{ color: '#94a3b8', weight: 2, opacity: 0.5, dashArray: '4, 4' }}
            >
              <Popup>
                <div style={{ fontSize: '10px' }}>
                  <div style={{ fontWeight: 700 }}>Alt Route {idx + 1}</div>
                  <div>Score: {route.score?.toFixed(2)}</div>
                  <div style={{ color: '#64748b' }}>{route.reason}</div>
                </div>
              </Popup>
            </Polyline>
          ));
        })}

        {/* Diversion TCP Checkpoints from planResult */}
        {diversionTcps.map((tcp, idx) => (
          <Marker
            key={`tcp-${idx}`}
            position={[tcp.lat, tcp.lng]}
            icon={tcpIcon}
            eventHandlers={{ click: () => { selectSegment(tcp.segmentId); } }}
          >
            <Popup>
              <div style={{ fontSize: '10px', lineHeight: '1.5' }}>
                <div style={{ color: '#ea580c', fontWeight: 700 }}>TCP CHECKPOINT</div>
                <div style={{ fontWeight: 600 }}>{tcp.name} · Seg {tcp.segmentId}</div>
                <div style={{ color: '#64748b' }}>Agency: {tcp.agency?.replace(/_/g, ' ')}</div>
                <div style={{ color: tcp.action === 'activate' ? '#16a34a' : '#eab308', fontWeight: 700 }}>{tcp.action?.toUpperCase()}</div>
              </div>
            </Popup>
          </Marker>
        ))}

        {/* Anomaly markers from live anomaly feed */}
        {overlays.police && anomalies?.map((anom, idx) => {
          const seg = segments.find(s => s.segment_id === anom.segment_id);
          const mid = segMid(seg);
          if (!mid) return null;
          return (
            <Marker
              key={`anom-${anom.anomaly_id || idx}`}
              position={mid}
              icon={anomalyIcon}
              zIndexOffset={50}
              eventHandlers={{ click: () => { flyToSegment(anom.segment_id); } }}
            >
              <Popup>
                <div style={{ fontSize: '10px', lineHeight: '1.5' }}>
                  <div style={{ color: '#dc2626', fontWeight: 700 }}>{(anom.anomaly_type || 'ANOMALY').toUpperCase()}</div>
                  <div>Severity: <strong style={{ color: anom.severity === 'high' ? '#dc2626' : '#ea580c' }}>{anom.severity}</strong></div>
                  <div style={{ color: '#64748b' }}>Seg {anom.segment_id}</div>
                  {anom.details?.detail && <div style={{ color: '#94a3b8', fontSize: '9px' }}>{anom.details.detail}</div>}
                </div>
              </Popup>
            </Marker>
          );
        })}

        {/* Highlighted segments (pulsing orange glow from sidebar clicks) */}
        {highlightedSegments?.length > 0 && segments.filter(s => highlightedSegments.includes(s.segment_id)).map(seg => {
          if (!seg.geometry?.coordinates) return null;
          const coords = seg.geometry.coordinates.map(c => [c[1], c[0]]);
          if (coords.length < 2) return null;
          return (
            <React.Fragment key={`hl-${seg.segment_id}`}>
              <Polyline positions={coords} pathOptions={{ color: '#f59e0b', weight: 10, opacity: 0.5, lineCap: 'round', className: 'segment-highlight-pulse' }} />
              <Polyline positions={coords} pathOptions={{ color: '#fbbf24', weight: 4, opacity: 1, lineCap: 'round' }} />
            </React.Fragment>
          );
        })}

        {/* ════════ LIVE CONVOY SIMULATION ════════ */}
        {/* Route ahead (ant-march animation) */}
        {convoySimulation?.active && convoySimulation.routeCoords?.length > 1 && (() => {
          // Only show route segments AHEAD of convoy
          const aheadIdx = Math.max(0, convoySimulation.currentIndex || 0);
          const ahead = convoySimulation.routeCoords.slice(aheadIdx);
          if (ahead.length < 2) return null;

          // R2: Distance checkpoint markers every ~2km along remaining route
          const checkpoints = [];
          let cumDistCheck = 0;
          let nextCp = 2000; // first checkpoint at 2km
          for (let i = 1; i < ahead.length; i++) {
            const segLen = Math.sqrt(Math.pow(ahead[i][0] - ahead[i-1][0], 2) + Math.pow(ahead[i][1] - ahead[i-1][1], 2)) * 111320;
            cumDistCheck += segLen;
            if (cumDistCheck >= nextCp) {
              checkpoints.push({ pos: ahead[i], km: (nextCp / 1000).toFixed(0) });
              nextCp += 2000;
            }
          }

          return (
            <>
              {/* Outer halo glow */}
              <Polyline
                positions={ahead}
                pathOptions={{
                  color: '#f97316', weight: 14, opacity: 0.04,
                  lineCap: 'round', lineJoin: 'round',
                }}
              />
              {/* Faint glow under route */}
              <Polyline
                positions={ahead}
                pathOptions={{
                  color: '#ea580c', weight: 8, opacity: 0.1,
                  lineCap: 'round', lineJoin: 'round',
                }}
              />
              {/* Animated ant-march dashes */}
              <Polyline
                positions={ahead}
                pathOptions={{
                  color: '#f97316', weight: 3.5, opacity: 0.55,
                  dashArray: '12 16', lineCap: 'round', lineJoin: 'round',
                  className: 'convoy-route-ahead'
                }}
              />
              {/* R2: Distance checkpoint markers */}
              {checkpoints.map((cp, i) => (
                <CircleMarker
                  key={`cp-${i}`}
                  center={cp.pos}
                  radius={5}
                  pathOptions={{ color: '#f59e0b', fillColor: '#fbbf24', fillOpacity: 0.8, weight: 1.5 }}
                >
                  <Popup><div style={{ fontSize: '10px', fontWeight: 700, color: '#f59e0b' }}>{cp.km} km ahead</div></Popup>
                </CircleMarker>
              ))}
            </>
          );
        })()}

        {/* Speed-gradient trail behind convoy */}
        {convoySimulation?.active && convoySimulation.trailCoords?.length > 1 && (() => {
          const trail = convoySimulation.trailCoords;
          const polySegments = [];
          for (let i = 0; i < trail.length - 1; i++) {
            const spd = trail[i].speed ?? 40;
            const trailColor = spd > 45 ? '#22c55e' : spd > 35 ? '#4ade80' : spd > 25 ? '#eab308' : spd > 15 ? '#f97316' : '#dc2626';
            const opacity = 0.3 + 0.6 * (i / trail.length);
            polySegments.push(
              <Polyline
                key={`trail-${i}`}
                positions={[[trail[i].lat, trail[i].lng], [trail[i+1].lat, trail[i+1].lng]]}
                pathOptions={{
                  color: trailColor,
                  weight: 4 + 4 * (i / trail.length),
                  opacity,
                  lineCap: 'round', lineJoin: 'round',
                }}
              />
            );
          }
          return polySegments;
        })()}

        {convoySimulation?.active && convoySimulation.position && (
          <>
            {/* R2: Shockwave ripple rings emanating from convoy */}
            {[0, 1, 2].map(ring => (
              <CircleMarker
                key={`shock-${ring}`}
                center={[convoySimulation.position.lat, convoySimulation.position.lng]}
                radius={12 + ring * 10}
                pathOptions={{
                  color: '#f97316', fillColor: 'transparent', fillOpacity: 0,
                  weight: 1, opacity: 0,
                  className: `convoy-shockwave convoy-shockwave-${ring}`
                }}
              />
            ))}
            {/* Outermost security zone (detection ring) */}
            <CircleMarker
              center={[convoySimulation.position.lat, convoySimulation.position.lng]}
              radius={38}
              pathOptions={{
                color: '#f59e0b', fillColor: 'transparent', fillOpacity: 0,
                weight: 1, opacity: 0.2, dashArray: '6 6',
                className: 'convoy-perimeter-outer'
              }}
            />
            {/* Security perimeter ring */}
            <CircleMarker
              center={[convoySimulation.position.lat, convoySimulation.position.lng]}
              radius={28}
              pathOptions={{
                color: '#f97316', fillColor: '#f97316', fillOpacity: 0.04,
                weight: 1.5, opacity: 0.4, dashArray: '4 4',
                className: 'convoy-perimeter-pulse'
              }}
            />
            {/* Inner perimeter (escort zone) */}
            <CircleMarker
              center={[convoySimulation.position.lat, convoySimulation.position.lng]}
              radius={18}
              pathOptions={{
                color: '#ea580c', fillColor: '#ea580c', fillOpacity: 0.08,
                weight: 2, opacity: 0.6,
                className: 'convoy-perimeter-inner'
              }}
            />
            {/* Convoy vehicle marker — enhanced with chevron design */}
            <Marker
              position={[convoySimulation.position.lat, convoySimulation.position.lng]}
              icon={L.divIcon({
                className: 'convoy-vehicle-icon',
                html: `<div style="transform:rotate(${convoySimulation.heading || 0}deg);transition:transform 0.18s linear;">
                  <svg width="44" height="44" viewBox="0 0 44 44" fill="none" xmlns="http://www.w3.org/2000/svg">
                    <defs>
                      <filter id="convGlow2"><feGaussianBlur stdDeviation="3" result="g"/><feMerge><feMergeNode in="g"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
                      <radialGradient id="coreGrad" cx="50%" cy="50%" r="50%">
                        <stop offset="0%" stop-color="#fbbf24"/>
                        <stop offset="60%" stop-color="#f97316"/>
                        <stop offset="100%" stop-color="#ea580c"/>
                      </radialGradient>
                      <linearGradient id="arrowGrad" x1="22" y1="4" x2="22" y2="34">
                        <stop offset="0%" stop-color="#fde68a"/>
                        <stop offset="100%" stop-color="#ea580c"/>
                      </linearGradient>
                    </defs>
                    <circle cx="22" cy="22" r="20" fill="#0f172a" stroke="#ea580c" stroke-width="2" filter="url(#convGlow2)" opacity="0.9"/>
                    <circle cx="22" cy="22" r="16" fill="none" stroke="#f97316" stroke-width="0.5" opacity="0.4" stroke-dasharray="3 3"/>
                    <polygon points="22,4 32,30 22,24 12,30" fill="url(#arrowGrad)" stroke="#fed7aa" stroke-width="0.6"/>
                    <circle cx="22" cy="22" r="4" fill="url(#coreGrad)"/>
                    <circle cx="22" cy="22" r="2" fill="#fef3c7" class="convoy-core-pulse"/>
                  </svg>
                </div>`,
                iconSize: [44, 44],
                iconAnchor: [22, 22]
              })}
              zIndexOffset={600}
            >
              <Popup>
                <div style={{ minWidth: '240px', fontFamily: 'var(--font-mono, monospace)', background: '#0f172a', color: '#e2e8f0', padding: '12px', borderRadius: '10px', border: '1px solid #334155' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '10px', borderBottom: '1px solid #1e293b', paddingBottom: '8px' }}>
                    <div style={{ width: '8px', height: '8px', borderRadius: '50%', background: '#22c55e', animation: 'pulse 1.5s infinite' }} />
                    <span style={{ fontWeight: 800, color: '#f97316', fontSize: '12px', letterSpacing: '1.5px' }}>CONVOY LIVE</span>
                    {/* R2: Threat badge */}
                    <span style={{
                      marginLeft: 'auto', fontSize: '8px', fontWeight: 700, letterSpacing: '0.5px',
                      padding: '1px 6px', borderRadius: '4px',
                      background: convoySimulation.threatLevel === 'critical' ? 'rgba(220,38,38,0.2)' : convoySimulation.threatLevel === 'elevated' ? 'rgba(234,88,12,0.2)' : convoySimulation.threatLevel === 'moderate' ? 'rgba(234,179,8,0.2)' : 'rgba(22,163,74,0.2)',
                      color: convoySimulation.threatLevel === 'critical' ? '#ef4444' : convoySimulation.threatLevel === 'elevated' ? '#f97316' : convoySimulation.threatLevel === 'moderate' ? '#eab308' : '#22c55e',
                      border: `1px solid ${convoySimulation.threatLevel === 'critical' ? 'rgba(220,38,38,0.4)' : convoySimulation.threatLevel === 'elevated' ? 'rgba(234,88,12,0.4)' : 'rgba(22,163,74,0.3)'}`,
                    }}>{(convoySimulation.threatLevel || 'NOMINAL').toUpperCase()}</span>
                  </div>
                  <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '5px 14px', fontSize: '10px' }}>
                    <div style={{ color: '#94a3b8' }}>Speed</div>
                    <div style={{ fontWeight: 700, color: convoySimulation.speed > 40 ? '#22c55e' : convoySimulation.speed > 20 ? '#eab308' : '#dc2626' }}>{convoySimulation.speed?.toFixed(1)} km/h</div>
                    <div style={{ color: '#94a3b8' }}>Max / Min</div>
                    <div style={{ fontWeight: 600, fontSize: '9px' }}>{convoySimulation.maxSpeed?.toFixed(0) ?? '—'} / {(convoySimulation.minSpeed < 900 ? convoySimulation.minSpeed?.toFixed(0) : '—')} km/h</div>
                    <div style={{ color: '#94a3b8' }}>Progress</div>
                    <div style={{ fontWeight: 600 }}>{(convoySimulation.progress * 100).toFixed(1)}%</div>
                    <div style={{ color: '#94a3b8' }}>ETA</div>
                    <div style={{ fontWeight: 600 }}>{convoySimulation.etaSeconds > 0 ? `${Math.floor(convoySimulation.etaSeconds / 60)}m ${Math.floor(convoySimulation.etaSeconds % 60)}s` : '—'}</div>
                    <div style={{ color: '#94a3b8' }}>Remaining</div>
                    <div style={{ fontWeight: 600, color: '#38bdf8' }}>{((convoySimulation.distanceRemainingM || 0) / 1000).toFixed(2)} km</div>
                    <div style={{ color: '#94a3b8' }}>Heading</div>
                    <div style={{ fontWeight: 600 }}>{convoySimulation.heading?.toFixed(0)}°</div>
                    <div style={{ color: '#94a3b8' }}>Segments</div>
                    <div style={{ fontWeight: 600 }}>{convoySimulation.segmentsTraversed ?? 0} / {convoySimulation.segmentIds?.length ?? 0}</div>
                    <div style={{ color: '#94a3b8' }}>Zone</div>
                    <div style={{ fontWeight: 600, color: '#a78bfa', textTransform: 'capitalize' }}>{convoySimulation.currentZone || '—'}</div>
                    <div style={{ color: '#94a3b8' }}>Fuel</div>
                    <div style={{ fontWeight: 600, color: (convoySimulation.fuelPct || 0) > 50 ? '#22c55e' : (convoySimulation.fuelPct || 0) > 20 ? '#eab308' : '#ef4444' }}>{(convoySimulation.fuelPct || 0).toFixed(0)}%</div>
                    <div style={{ color: '#94a3b8' }}>G-Force</div>
                    <div style={{ fontWeight: 600, color: (convoySimulation.gForce || 0) > 0.5 ? '#f97316' : '#64748b' }}>{(convoySimulation.gForce || 0).toFixed(2)}G</div>
                  </div>
                  {/* R2: Fuel gauge bar */}
                  <div style={{ marginTop: '8px' }}>
                    <div style={{ fontSize: '7px', color: '#475569', marginBottom: '2px', display: 'flex', justifyContent: 'space-between' }}>
                      <span>FUEL</span><span style={{ color: '#e2e8f0' }}>{(convoySimulation.fuelPct || 0).toFixed(0)}%</span>
                    </div>
                    <div style={{ height: '4px', background: '#1e293b', borderRadius: '2px', overflow: 'hidden' }}>
                      <div style={{ height: '100%', width: `${convoySimulation.fuelPct || 0}%`, background: 'linear-gradient(90deg, #ef4444, #f59e0b, #22c55e)', borderRadius: '2px', transition: 'width 0.3s' }} />
                    </div>
                  </div>
                  <div style={{ marginTop: '6px', height: '5px', background: '#1e293b', borderRadius: '3px', overflow: 'hidden' }}>
                    <div style={{ height: '100%', width: `${convoySimulation.progress * 100}%`, background: 'linear-gradient(90deg, #ea580c, #f97316, #fbbf24)', borderRadius: '3px', transition: 'width 0.2s' }} />
                  </div>
                </div>
              </Popup>
            </Marker>

            {/* Origin waypoint (green flag) during demo simulation */}
            {convoySimulation.originCoord && (
              <Marker
                position={[convoySimulation.originCoord.lat, convoySimulation.originCoord.lng]}
                icon={L.divIcon({
                  className: 'convoy-waypoint-icon',
                  html: `<div style="position:relative;display:flex;align-items:center;justify-content:center;">
                    <div style="width:18px;height:18px;border-radius:50%;background:linear-gradient(135deg,#16a34a,#22c55e);border:2px solid #bbf7d0;box-shadow:0 0 12px rgba(22,163,74,0.6),0 2px 6px rgba(0,0,0,0.3);display:flex;align-items:center;justify-content:center;">
                      <span style="color:#fff;font-size:9px;font-weight:900;">S</span>
                    </div>
                    <div style="position:absolute;inset:-4px;border-radius:50%;border:2px solid #22c55e;opacity:0.3;animation:pulse 2s infinite;"></div>
                  </div>`,
                  iconSize: [18, 18],
                  iconAnchor: [9, 9]
                })}
                zIndexOffset={500}
              >
                <Popup><div style={{ fontSize: '10px', fontWeight: 700, color: '#16a34a' }}>ORIGIN — Start Point</div></Popup>
              </Marker>
            )}

            {/* Destination waypoint (red flag) during demo simulation */}
            {convoySimulation.destCoord && (
              <Marker
                position={[convoySimulation.destCoord.lat, convoySimulation.destCoord.lng]}
                icon={L.divIcon({
                  className: 'convoy-waypoint-icon',
                  html: `<div style="position:relative;display:flex;align-items:center;justify-content:center;">
                    <div style="width:18px;height:18px;border-radius:50%;background:linear-gradient(135deg,#dc2626,#ef4444);border:2px solid #fca5a5;box-shadow:0 0 12px rgba(220,38,38,0.6),0 2px 6px rgba(0,0,0,0.3);display:flex;align-items:center;justify-content:center;">
                      <span style="color:#fff;font-size:9px;font-weight:900;">D</span>
                    </div>
                    <div style="position:absolute;inset:-4px;border-radius:50%;border:2px solid #ef4444;opacity:0.3;animation:pulse 2s infinite;"></div>
                  </div>`,
                  iconSize: [18, 18],
                  iconAnchor: [9, 9]
                })}
                zIndexOffset={500}
              >
                <Popup><div style={{ fontSize: '10px', fontWeight: 700, color: '#dc2626' }}>DESTINATION — End Point</div></Popup>
              </Marker>
            )}
          </>
        )}

        {/* Temp Origin Marker — shown when user selects location before plan */}
        {tempOriginCoords && !originCoords && (
          <Marker
            position={[tempOriginCoords[1], tempOriginCoords[0]]}
            icon={createDivIcon('#16a34a', 18, true)}
            zIndexOffset={200}
          >
            <Popup>
              <div style={{ minWidth: '120px' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '5px', fontWeight: 700, color: '#16a34a' }}>
                  <div style={{ width: '6px', height: '6px', borderRadius: '50%', background: '#16a34a' }} />
                  ORIGIN (Pending)
                </div>
                <div style={{ fontSize: '10px', color: '#64748b', fontFamily: 'monospace', marginTop: '2px' }}>
                  {tempOriginCoords[1].toFixed(4)}°N, {tempOriginCoords[0].toFixed(4)}°E
                </div>
              </div>
            </Popup>
          </Marker>
        )}

        {/* Temp Destination Marker — shown when user selects location before plan */}
        {tempDestCoords && !destCoords && (
          <Marker
            position={[tempDestCoords[1], tempDestCoords[0]]}
            icon={createDivIcon('#dc2626', 18, true)}
            zIndexOffset={200}
          >
            <Popup>
              <div style={{ minWidth: '120px' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '5px', fontWeight: 700, color: '#dc2626' }}>
                  <div style={{ width: '6px', height: '6px', borderRadius: '50%', background: '#dc2626' }} />
                  DESTINATION (Pending)
                </div>
                <div style={{ fontSize: '10px', color: '#64748b', fontFamily: 'monospace', marginTop: '2px' }}>
                  {tempDestCoords[1].toFixed(4)}°N, {tempDestCoords[0].toFixed(4)}°E
                </div>
              </div>
            </Popup>
          </Marker>
        )}

        {/* Dynamic Origin Marker */}
        {originCoords && (
          <CircleMarker
            center={originCoords}
            pathOptions={{ color: '#16a34a', fillColor: '#16a34a', fillOpacity: 1, weight: 3 }}
            radius={10}
          >
            <Popup>
              <div style={{ minWidth: '120px' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '5px', fontWeight: 700, color: '#16a34a' }}>
                  <div style={{ width: '6px', height: '6px', borderRadius: '50%', background: '#16a34a' }} />
                  ORIGIN
                </div>
                <div style={{ fontSize: '10px', color: '#64748b', fontFamily: 'var(--font-mono)', marginTop: '2px' }}>
                  {originCoords[0].toFixed(4)}°N, {originCoords[1].toFixed(4)}°E
                </div>
              </div>
            </Popup>
          </CircleMarker>
        )}

        {/* Dynamic Destination Marker */}
        {destCoords && (
          <CircleMarker
            center={destCoords}
            pathOptions={{ color: '#dc2626', fillColor: '#dc2626', fillOpacity: 1, weight: 3 }}
            radius={10}
          >
            <Popup>
              <div style={{ minWidth: '120px' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '5px', fontWeight: 700, color: '#dc2626' }}>
                  <div style={{ width: '6px', height: '6px', borderRadius: '50%', background: '#dc2626' }} />
                  DESTINATION
                </div>
                <div style={{ fontSize: '10px', color: '#64748b', fontFamily: 'var(--font-mono)', marginTop: '2px' }}>
                  {destCoords[0].toFixed(4)}°N, {destCoords[1].toFixed(4)}°E
                </div>
              </div>
            </Popup>
          </CircleMarker>
        )}

      </MapContainer>

      {/* Congestion Legend */}
      {overlays.traffic && (
        <div style={{
          position: 'absolute', bottom: '16px', left: '50%', transform: 'translateX(-50%)',
          zIndex: 1000, background: 'rgba(15,23,42,0.92)', backdropFilter: 'blur(12px)',
          WebkitBackdropFilter: 'blur(12px)', borderRadius: '10px',
          padding: '6px 14px', boxShadow: '0 4px 16px rgba(0,0,0,0.3), 0 1px 3px rgba(0,0,0,0.15)',
          border: '1px solid #334155',
          display: 'flex', alignItems: 'center', gap: '10px', fontSize: '9px',
          animation: 'fadeIn 0.3s ease-out',
        }}>
          <span style={{ fontWeight: 700, color: '#94a3b8', letterSpacing: '0.5px' }}>CONGESTION</span>
          <div style={{ width: '1px', height: '12px', background: '#475569' }} />
          {CONGESTION_COLORS.map(b => (
            <div key={b.label} className="flex items-center gap-1">
              <div style={{ width: '16px', height: '5px', backgroundColor: b.color, borderRadius: '3px', boxShadow: `0 0 4px ${b.color}44` }} />
              <span style={{ color: '#94a3b8', fontWeight: 500 }}>{b.label}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
};

export default ConvoyMap;
