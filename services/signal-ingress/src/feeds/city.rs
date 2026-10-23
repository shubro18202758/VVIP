//! Ahmedabad city model shared by the edge-synthetic feeds.
//!
//! With no live ITMS / fleet AVL / crowd / mapping-provider access, the four
//! collectors do not invent independent numbers: they all observe ONE
//! congestion field over the seeded Ahmedabad network
//! (`segments_ahmedabad.rs`, generated from corridor.road_segments), each
//! through its own sensor model. The feeds therefore agree with each other
//! the way real sources agree, and the per-segment rolling statistics of the
//! Tier-1 anomaly detector see one coherent signal plus sensor noise.
//!
//! The field is a pure function of (segment, instant):
//!
//!   congestion = demand(IST time of day, weekday) × sensitivity(segment)
//!                + drift(segment, t)
//!
//! * `demand` is an Ahmedabad weekday profile in IST (UTC+05:30): a late
//!   morning peak around 10:15 (offices and markets open ~10:00), a heavier
//!   evening peak around 19:00, a midday shoulder and a quiet night. Sundays
//!   lose most of the morning peak.
//! * `sensitivity` scales demand by road class (ring roads and highways have
//!   spare capacity, inner primaries choke first) and a stable per-segment
//!   factor, so the city is never one uniform colour.
//! * `drift` is smooth two-octave value noise (15 min and 3 min lattices), so
//!   each segment wanders on its own without jumps.
//!
//! Every reading derived from this field is SYNTHETIC and is published with
//! `DataQuality::Synthetic`; nothing here is presented as a measurement.

use chrono::{DateTime, Datelike, FixedOffset, Timelike, Utc, Weekday};

use super::rng::{hash_unit, splitmix64};
use super::segments_ahmedabad::{NETWORK_BBOX, NODE_COUNT, SEGMENTS};

/// Road class vocabulary of corridor.road_segments (001_spatial_init.sql CHECK).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RoadClass {
    Motorway,
    Trunk,
    Primary,
    Secondary,
    Tertiary,
    Residential,
    Service,
}

impl RoadClass {
    /// How strongly the class responds to city-wide demand.
    pub fn peak_sensitivity(self) -> f32 {
        match self {
            Self::Motorway => 0.85,
            Self::Trunk => 1.0,
            Self::Primary => 1.08,
            Self::Secondary => 0.95,
            Self::Tertiary => 0.85,
            Self::Residential => 0.7,
            Self::Service => 0.6,
        }
    }

    /// Plausible upper bound for an observed speed, as a multiple of the limit.
    pub fn plausible_speed_factor(self) -> f32 {
        match self {
            Self::Motorway | Self::Trunk => 1.5,
            _ => 1.6,
        }
    }
}

/// One seeded road segment (see `segments_ahmedabad.rs`).
#[derive(Debug)]
pub struct Segment {
    pub id: i64,
    pub name: &'static str,
    pub road_class: RoadClass,
    pub lanes: u8,
    pub speed_limit_kmh: f32,
    pub oneway: bool,
    /// Geodesic length from PostGIS (geography), metres.
    pub length_m: f64,
    /// ST_LineInterpolatePoint(geom, 0.5) as (lon, lat).
    pub mid: (f64, f64),
    /// Full LINESTRING, WGS84 (lon, lat).
    pub path: &'static [(f64, f64)],
    pub start_node: u16,
    pub end_node: u16,
}

/// Metres per degree of (longitude, latitude) at `lat` (WGS84 series).
pub fn metres_per_degree(lat: f64) -> (f64, f64) {
    let phi = lat.to_radians();
    let m_lat = 111_132.92 - 559.82 * (2.0 * phi).cos() + 1.175 * (4.0 * phi).cos();
    let m_lon = 111_412.84 * phi.cos() - 93.5 * (3.0 * phi).cos();
    (m_lon, m_lat)
}

impl Segment {
    /// Planar (local equirectangular) length of each polyline piece, metres.
    fn piece_lengths(&self) -> impl Iterator<Item = f64> + '_ {
        self.path.windows(2).map(|w| {
            let (m_lon, m_lat) = metres_per_degree((w[0].1 + w[1].1) / 2.0);
            let dx = (w[1].0 - w[0].0) * m_lon;
            let dy = (w[1].1 - w[0].1) * m_lat;
            (dx * dx + dy * dy).sqrt()
        })
    }

    /// Polyline length in metres (agrees with `length_m` to well under 1%).
    pub fn polyline_length_m(&self) -> f64 {
        self.piece_lengths().sum()
    }

    /// Point `along_m` metres from the start vertex, clamped to the polyline.
    pub fn point_at(&self, along_m: f64) -> (f64, f64) {
        let mut remaining = along_m.max(0.0);
        for (w, len) in self.path.windows(2).zip(self.piece_lengths()) {
            if remaining <= len {
                let f = if len > 0.0 { remaining / len } else { 0.0 };
                return (
                    w[0].0 + (w[1].0 - w[0].0) * f,
                    w[0].1 + (w[1].1 - w[0].1) * f,
                );
            }
            remaining -= len;
        }
        *self
            .path
            .last()
            .expect("generated paths have >= 2 vertices")
    }

    /// Shortest distance in metres from (lon, lat) to the polyline.
    pub fn distance_m(&self, lon: f64, lat: f64) -> f64 {
        let (m_lon, m_lat) = metres_per_degree(lat);
        let mut best = f64::INFINITY;
        for w in self.path.windows(2) {
            let (ax, ay) = ((w[0].0 - lon) * m_lon, (w[0].1 - lat) * m_lat);
            let (bx, by) = ((w[1].0 - lon) * m_lon, (w[1].1 - lat) * m_lat);
            let (dx, dy) = (bx - ax, by - ay);
            let len2 = dx * dx + dy * dy;
            let t = if len2 > 0.0 {
                (-(ax * dx + ay * dy) / len2).clamp(0.0, 1.0)
            } else {
                0.0
            };
            let (px, py) = (ax + t * dx, ay + t * dy);
            best = best.min((px * px + py * py).sqrt());
        }
        best
    }
}

/// The seeded Ahmedabad network, ordered by segment id.
pub fn segments() -> &'static [Segment] {
    SEGMENTS
}

pub fn segment_by_id(id: i64) -> Option<&'static Segment> {
    SEGMENTS
        .binary_search_by_key(&id, |s| s.id)
        .ok()
        .map(|i| &SEGMENTS[i])
}

/// Keep a reported coordinate inside the modelled network envelope. Sensor
/// noise is applied on top of on-road positions; at the outermost endpoints
/// (e.g. SP Ring Road West at 72.48 E) it must not leave the modelled city.
pub fn clamp_to_network(lon: f64, lat: f64) -> (f64, f64) {
    let (min_lon, min_lat, max_lon, max_lat) = NETWORK_BBOX;
    (lon.clamp(min_lon, max_lon), lat.clamp(min_lat, max_lat))
}

/// Offset (lon, lat) by (east_m, north_m) metres.
pub fn offset_m(lon: f64, lat: f64, east_m: f64, north_m: f64) -> (f64, f64) {
    let (m_lon, m_lat) = metres_per_degree(lat);
    (lon + east_m / m_lon, lat + north_m / m_lat)
}

/// Endpoint-node incidence of the network: which segments meet at each node.
pub struct Topology {
    incident: Vec<Vec<usize>>,
}

impl Topology {
    pub fn ahmedabad() -> Self {
        let mut incident = vec![Vec::new(); NODE_COUNT];
        for (i, s) in SEGMENTS.iter().enumerate() {
            incident[s.start_node as usize].push(i);
            if s.end_node != s.start_node {
                incident[s.end_node as usize].push(i);
            }
        }
        Self { incident }
    }

    /// Segment indices that can be ENTERED at `node`: a two-way segment from
    /// either end, a one-way segment only at its start node.
    pub fn entries(&self, node: u16) -> impl Iterator<Item = usize> + '_ {
        self.incident[node as usize]
            .iter()
            .copied()
            .filter(move |&i| !SEGMENTS[i].oneway || SEGMENTS[i].start_node == node)
    }

    /// Connected pieces of the network (segment indices, ascending), largest
    /// total length first. The seeded network is schematic and not fully
    /// connected, so anything that drives on it must know where it can go.
    pub fn components(&self) -> Vec<Vec<usize>> {
        let mut component = vec![usize::MAX; SEGMENTS.len()];
        let mut pieces: Vec<Vec<usize>> = Vec::new();
        for first in 0..SEGMENTS.len() {
            if component[first] != usize::MAX {
                continue;
            }
            let id = pieces.len();
            let mut stack = vec![first];
            let mut piece = Vec::new();
            component[first] = id;
            while let Some(i) = stack.pop() {
                piece.push(i);
                for node in [SEGMENTS[i].start_node, SEGMENTS[i].end_node] {
                    for &j in &self.incident[node as usize] {
                        if component[j] == usize::MAX {
                            component[j] = id;
                            stack.push(j);
                        }
                    }
                }
            }
            piece.sort_unstable();
            pieces.push(piece);
        }
        let length = |p: &Vec<usize>| p.iter().map(|&i| SEGMENTS[i].length_m).sum::<f64>();
        pieces.sort_by(|a, b| length(b).total_cmp(&length(a)));
        pieces
    }
}

// ─── IST clock and the Ahmedabad demand profile ─────────────────────────────

/// India Standard Time, UTC+05:30 (no daylight saving).
pub fn ist() -> FixedOffset {
    FixedOffset::east_opt(5 * 3600 + 30 * 60).expect("valid fixed offset")
}

/// IST hour of day as a fraction (e.g. 19.5 = 19:30 IST) and IST weekday.
pub fn ist_clock(t: DateTime<Utc>) -> (f32, Weekday) {
    let local = t.with_timezone(&ist());
    let hour = local.hour() as f32 + local.minute() as f32 / 60.0 + local.second() as f32 / 3600.0;
    (hour, local.weekday())
}

/// Gaussian bump on the 24 h circle.
fn bump(hour: f32, centre: f32, width: f32) -> f32 {
    let mut d = (hour - centre).abs();
    if d > 12.0 {
        d = 24.0 - d;
    }
    let z = d / width;
    (-0.5 * z * z).exp()
}

/// City-wide congestion demand for Ahmedabad at instant `t` (0 = empty
/// roads, 1 = gridlock), from the IST time of day and weekday.
pub fn demand(t: DateTime<Utc>) -> f32 {
    let (hour, weekday) = ist_clock(t);
    // (morning, evening, midday) peak scaling by day of week
    let (am, pm, midday) = match weekday {
        Weekday::Sun => (0.55, 0.85, 1.1),
        Weekday::Sat => (0.85, 1.05, 1.0),
        _ => (1.0, 1.0, 1.0),
    };
    let level = 0.08
        + am * 0.40 * bump(hour, 10.25, 1.05)
        + pm * 0.48 * bump(hour, 19.0, 1.4)
        + midday * 0.13 * bump(hour, 14.5, 2.4);
    level.clamp(0.0, 1.0)
}

// ─── Congestion field and the speed/congestion relation ─────────────────────

/// Fraction of the limit lost at full congestion: speed = limit × (1 − 0.7·c).
/// Every feed uses this one relation (and its inverse) so a reading's speed
/// and congestion index are always consistent with each other.
pub const GRIDLOCK_SPEED_DROP: f32 = 0.7;

pub fn speed_for(limit_kmh: f32, congestion: f32) -> f32 {
    limit_kmh * (1.0 - GRIDLOCK_SPEED_DROP * congestion.clamp(0.0, 1.0))
}

pub fn congestion_for(limit_kmh: f32, speed_kmh: f32) -> f32 {
    ((1.0 - speed_kmh / limit_kmh) / GRIDLOCK_SPEED_DROP).clamp(0.0, 1.0)
}

/// Default seed of the city field; INGRESS_CITY_SEED overrides it.
pub const DEFAULT_CITY_SEED: u64 = 0xA4D0_2026;

pub fn city_seed_from_env() -> u64 {
    std::env::var("INGRESS_CITY_SEED")
        .ok()
        .and_then(|v| v.trim().parse().ok())
        .unwrap_or(DEFAULT_CITY_SEED)
}

/// (lattice period ms, amplitude) of the drift octaves.
const DRIFT_OCTAVES: [(f64, f32); 2] = [(15.0 * 60_000.0, 0.07), (3.0 * 60_000.0, 0.03)];

#[derive(Debug, Clone, Copy)]
pub struct CongestionField {
    seed: u64,
}

impl CongestionField {
    pub fn new(seed: u64) -> Self {
        Self { seed }
    }

    pub fn from_env() -> Self {
        Self::new(city_seed_from_env())
    }

    fn key(&self, segment_id: i64, salt: u64) -> u64 {
        splitmix64(self.seed ^ splitmix64((segment_id as u64) ^ (salt << 48)))
    }

    /// Demand multiplier of one segment: class response × a stable per-segment
    /// factor in [0.85, 1.15).
    pub fn sensitivity(&self, seg: &Segment) -> f32 {
        let local = 0.85 + 0.30 * hash_unit(self.key(seg.id, 1)) as f32;
        seg.road_class.peak_sensitivity() * local
    }

    /// Smooth per-segment drift in roughly [-0.10, 0.10].
    pub fn drift(&self, segment_id: i64, t_ms: i64) -> f32 {
        let mut total = 0.0f32;
        for (octave, &(period_ms, amplitude)) in DRIFT_OCTAVES.iter().enumerate() {
            let x = t_ms as f64 / period_ms;
            let k = x.floor();
            let f = x - k;
            let base = self.key(segment_id, 2 + octave as u64);
            let a = hash_unit(base ^ splitmix64(k as i64 as u64)) * 2.0 - 1.0;
            let b = hash_unit(base ^ splitmix64((k as i64 + 1) as u64)) * 2.0 - 1.0;
            let s = f * f * (3.0 - 2.0 * f);
            total += amplitude * (a + (b - a) * s) as f32;
        }
        total
    }

    /// Congestion index of `seg` at instant `t`, in [0.02, 0.97].
    pub fn congestion(&self, seg: &Segment, t: DateTime<Utc>) -> f32 {
        (demand(t) * self.sensitivity(seg) + self.drift(seg.id, t.timestamp_millis()))
            .clamp(0.02, 0.97)
    }

    /// Prevailing speed on `seg` at instant `t`, km/h.
    pub fn speed(&self, seg: &Segment, t: DateTime<Utc>) -> f32 {
        speed_for(seg.speed_limit_kmh, self.congestion(seg, t))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    /// 2026-10-22 is a Thursday.
    fn ist_at(h: u32, m: u32) -> DateTime<Utc> {
        ist()
            .with_ymd_and_hms(2026, 10, 22, h, m, 0)
            .unwrap()
            .with_timezone(&Utc)
    }

    #[test]
    fn every_segment_is_in_ahmedabad() {
        // the 49 segments of the corridor network, every one on its real
        // road inside the OpenStreetMap extract it was traced from
        // (scripts/gen_osm_network.py: lon 72.45-72.75, lat 22.90-23.20 —
        // the SP Ring Road runs out to Bopal, Aslali, Odhav and Bhat)
        assert_eq!(SEGMENTS.len(), 49);
        for s in SEGMENTS {
            assert!((1001..=1049).contains(&s.id), "{}", s.id);
            for &(lon, lat) in s.path.iter().chain(std::iter::once(&s.mid)) {
                assert!((72.45..=72.75).contains(&lon), "{} lon {lon}", s.id);
                assert!((22.90..=23.20).contains(&lat), "{} lat {lat}", s.id);
            }
            assert!(s.speed_limit_kmh >= 30.0 && s.speed_limit_kmh <= 120.0);
        }
        assert!(
            SEGMENTS.windows(2).all(|w| w[0].id < w[1].id),
            "sorted for binary search"
        );
    }

    #[test]
    fn planar_length_matches_postgis_geodesic_length() {
        for s in SEGMENTS {
            let rel = (s.polyline_length_m() - s.length_m).abs() / s.length_m;
            assert!(
                rel < 0.01,
                "{} planar {} vs geodesic {}",
                s.id,
                s.polyline_length_m(),
                s.length_m
            );
        }
    }

    #[test]
    fn point_at_walks_the_polyline() {
        let s = segment_by_id(1002).unwrap();
        assert_eq!(s.point_at(0.0), s.path[0]);
        assert_eq!(s.point_at(1e9), *s.path.last().unwrap());
        let half = s.point_at(s.polyline_length_m() / 2.0);
        assert!(s.distance_m(half.0, half.1) < 0.5);
        // the PostGIS midpoint lies on the road
        assert!(s.distance_m(s.mid.0, s.mid.1) < 0.5);
        // ~1.1 km east of the road is far from it
        assert!(s.distance_m(s.mid.0 + 0.01, s.mid.1) > 900.0);
    }

    #[test]
    fn topology_respects_one_way_entry() {
        let topo = Topology::ahmedabad();
        let tilak = segment_by_id(1013).unwrap();
        assert!(tilak.oneway);
        let idx = SEGMENTS.iter().position(|s| s.id == 1013).unwrap();
        assert!(topo.entries(tilak.start_node).any(|i| i == idx));
        assert!(!topo.entries(tilak.end_node).any(|i| i == idx));
    }

    #[test]
    fn ist_clock_is_utc_plus_five_thirty() {
        let t = Utc.with_ymd_and_hms(2026, 10, 22, 13, 30, 0).unwrap();
        let (hour, weekday) = ist_clock(t);
        assert!((hour - 19.0).abs() < 1e-4);
        assert_eq!(weekday, Weekday::Thu);
    }

    #[test]
    fn demand_peaks_follow_ahmedabad_local_time() {
        let night = demand(ist_at(3, 0));
        let morning_peak = demand(ist_at(10, 15));
        let midday = demand(ist_at(14, 30));
        let evening_peak = demand(ist_at(19, 0));
        assert!(night < 0.12, "night {night}");
        assert!(midday < morning_peak && midday > night);
        assert!(
            evening_peak > morning_peak,
            "evening {evening_peak} morning {morning_peak}"
        );
        // 09:00 UTC is 14:30 IST: a UTC-hour profile would put the peak here
        assert!(demand(Utc.with_ymd_and_hms(2026, 10, 22, 9, 0, 0).unwrap()) < 0.3);
        // Sunday mornings are light
        let sunday = ist()
            .with_ymd_and_hms(2026, 10, 25, 10, 15, 0)
            .unwrap()
            .with_timezone(&Utc);
        assert!(demand(sunday) < morning_peak);
    }

    #[test]
    fn field_is_heterogeneous_smooth_and_bounded() {
        let field = CongestionField::new(DEFAULT_CITY_SEED);
        let t = ist_at(19, 0);
        let values: Vec<f32> = SEGMENTS.iter().map(|s| field.congestion(s, t)).collect();
        let min = values.iter().cloned().fold(1.0, f32::min);
        let max = values.iter().cloned().fold(0.0, f32::max);
        let mean = values.iter().sum::<f32>() / values.len() as f32;
        assert!(max - min > 0.2, "evening peak spread {min}..{max}");
        assert!((0.4..0.7).contains(&mean), "evening peak mean {mean}");
        for s in SEGMENTS {
            let mut prev = field.congestion(s, t);
            for step in 1..=600 {
                let c = field.congestion(s, t + chrono::Duration::seconds(step));
                assert!((0.02..=0.97).contains(&c));
                assert!(
                    (c - prev).abs() < 0.01,
                    "segment {} jumped {prev} -> {c}",
                    s.id
                );
                prev = c;
            }
        }
    }

    #[test]
    fn speed_and_congestion_are_inverse() {
        for &c in &[0.0f32, 0.25, 0.5, 0.9] {
            let v = speed_for(60.0, c);
            assert!((congestion_for(60.0, v) - c).abs() < 1e-5);
        }
        assert_eq!(congestion_for(60.0, 80.0), 0.0);
    }
}
