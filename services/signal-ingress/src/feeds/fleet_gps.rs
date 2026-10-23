//! Fleet GPS Collector — ingests real-time vehicle telemetry from
//! convoy vehicles and traffic police fleet tracking systems.
//!
//! In edge-deployment mode, emulates a small AVL fleet patrolling the seeded
//! Ahmedabad network at 1 Hz. Vehicles drive ALONG the segment polylines
//! (segments_ahmedabad.rs) and turn onto another segment at shared junction
//! nodes (one-way segments are only entered at their start; dead ends turn
//! around), so every fix lies on the road it is attributed to. Speed follows
//! the shared city congestion field (city.rs) with a mean-reverting
//! per-vehicle deviation, bounded acceleration and braking ahead of a slower
//! road. Rows are labelled `DataQuality::Synthetic`.

use chrono::{DateTime, Utc};
use tokio::sync::mpsc;
use tracing::instrument;

use super::city::{
    CongestionField, Topology, clamp_to_network, congestion_for, offset_m, segments,
};
use super::rng::Lcg;
use crate::{DataQuality, FeedSource, IngressError, TrafficObservation};

/// Vehicles in the emulated fleet.
pub const FLEET_SIZE: usize = 8;

/// Memory of the per-vehicle speed deviation (Ornstein–Uhlenbeck), seconds.
const DEVIATION_TAU_SEC: f64 = 30.0;
/// Stationary standard deviation of the relative speed deviation.
const DEVIATION_SIGMA: f64 = 0.07;
/// Speed change bounds, km/h per second (+1.5 m/s², -2.5 m/s²).
const ACCEL_KMH_PER_SEC: f32 = 5.4;
const DECEL_KMH_PER_SEC: f32 = 9.0;
/// Comfortable braking used to plan the slow-down before a slower road, m/s².
const BRAKE_MPS2: f64 = 2.5;
/// Minimum look-ahead to the next junction, metres.
const APPROACH_M: f64 = 40.0;
/// GPS position error (1 sigma per axis), metres.
const GPS_SIGMA_M: f64 = 2.5;

/// Smoothly bounds a speed/limit ratio into (0.15, 1.05). Strictly
/// increasing, so a vehicle pressing against a bound still produces distinct
/// readings (a hard clamp would repeat the same value and trip the
/// phantom-reading check).
fn soft_ratio(x: f32) -> f32 {
    if x > 0.95 {
        0.95 + 0.10 * ((x - 0.95) / 0.10).tanh()
    } else if x < 0.25 {
        0.25 - 0.10 * ((0.25 - x) / 0.10).tanh()
    } else {
        x
    }
}

#[derive(Debug, Clone)]
struct Vehicle {
    seg: usize,
    /// Travelling from the polyline's first vertex towards its last.
    forward: bool,
    /// Distance travelled from the entry end of the current segment, metres.
    along_m: f64,
    len_m: f64,
    next: (usize, bool),
    speed_kmh: f32,
    deviation: f64,
}

/// Emulated AVL fleet over the Ahmedabad network.
pub struct FleetSim {
    field: CongestionField,
    rng: Lcg,
    topo: Topology,
    vehicles: Vec<Vehicle>,
}

impl FleetSim {
    /// Places `size` vehicles at instant `t`, shared out over the connected
    /// pieces of the network in proportion to their road length (largest
    /// remainder) and spread across each piece's segments.
    pub fn new(field: CongestionField, seed: u64, size: usize, t: DateTime<Utc>) -> Self {
        let mut sim = Self {
            field,
            rng: Lcg::new(seed),
            topo: Topology::ahmedabad(),
            vehicles: Vec::new(),
        };
        let pieces = sim.topo.components();
        let lengths: Vec<f64> = pieces
            .iter()
            .map(|p| p.iter().map(|&i| segments()[i].length_m).sum())
            .collect();
        let total: f64 = lengths.iter().sum();
        let quotas: Vec<f64> = lengths.iter().map(|l| size as f64 * l / total).collect();
        let mut counts: Vec<usize> = quotas.iter().map(|q| q.floor() as usize).collect();
        let mut by_remainder: Vec<usize> = (0..pieces.len()).collect();
        by_remainder.sort_by(|&a, &b| {
            (quotas[b] - quotas[b].floor()).total_cmp(&(quotas[a] - quotas[a].floor()))
        });
        let assigned: usize = counts.iter().sum();
        for &p in by_remainder.iter().take(size - assigned) {
            counts[p] += 1;
        }

        for (piece, &count) in pieces.iter().zip(&counts) {
            for j in 0..count {
                let seg = piece[j * piece.len() / count];
                let s = &segments()[seg];
                let forward = s.oneway || sim.rng.chance(0.5);
                let len_m = s.polyline_length_m();
                let along_m = sim.rng.range_f64(0.1, 0.9) * len_m;
                let next = sim.plan_next(seg, forward);
                let speed_kmh = sim.field.speed(s, t);
                sim.vehicles.push(Vehicle {
                    seg,
                    forward,
                    along_m,
                    len_m,
                    next,
                    speed_kmh,
                    deviation: 0.0,
                });
            }
        }
        sim
    }

    pub fn len(&self) -> usize {
        self.vehicles.len()
    }

    pub fn is_empty(&self) -> bool {
        self.vehicles.is_empty()
    }

    /// Picks the segment (and direction) a vehicle takes when it leaves `seg`.
    fn plan_next(&mut self, seg: usize, forward: bool) -> (usize, bool) {
        let s = &segments()[seg];
        let exit = if forward { s.end_node } else { s.start_node };
        let options: Vec<usize> = self.topo.entries(exit).filter(|&i| i != seg).collect();
        if options.is_empty() {
            // dead end: turn around on the same road
            return (seg, !forward);
        }
        let pick = options[self.rng.below(options.len() as u64) as usize];
        (pick, segments()[pick].start_node == exit)
    }

    /// Advances every vehicle by `dt_s` seconds to instant `t` and returns
    /// one fix per vehicle, in fleet order.
    pub fn tick(&mut self, t: DateTime<Utc>, dt_s: f64) -> Vec<TrafficObservation> {
        let mut fixes = Vec::with_capacity(self.vehicles.len());
        for i in 0..self.vehicles.len() {
            let mut v = self.vehicles[i].clone();
            let seg = &segments()[v.seg];
            let limit = seg.speed_limit_kmh;

            // mean-reverting deviation from the prevailing speed
            let decay = dt_s / DEVIATION_TAU_SEC;
            v.deviation +=
                -v.deviation * decay + DEVIATION_SIGMA * (2.0 * decay).sqrt() * self.rng.gaussian();
            let factor = (1.0 + v.deviation) as f32;
            let mut target = soft_ratio(self.field.speed(seg, t) * factor / limit) * limit;

            // brake ahead of a slower next road so the vehicle arrives at its pace
            let next_seg = &segments()[v.next.0];
            let next_target =
                soft_ratio(self.field.speed(next_seg, t) * factor / next_seg.speed_limit_kmh)
                    * next_seg.speed_limit_kmh;
            let (now_ms, next_ms) = (v.speed_kmh as f64 / 3.6, next_target as f64 / 3.6);
            let braking_m = ((now_ms * now_ms - next_ms * next_ms) / (2.0 * BRAKE_MPS2)).max(0.0);
            if v.len_m - v.along_m < APPROACH_M + braking_m {
                target = target.min(next_target);
            }

            let dt = dt_s as f32;
            v.speed_kmh = (v.speed_kmh
                + (target - v.speed_kmh).clamp(-DECEL_KMH_PER_SEC * dt, ACCEL_KMH_PER_SEC * dt))
            .max(3.0);

            // move along the road network
            v.along_m += v.speed_kmh as f64 / 3.6 * dt_s;
            while v.along_m >= v.len_m {
                v.along_m -= v.len_m;
                let (next_seg, next_forward) = v.next;
                v.seg = next_seg;
                v.forward = next_forward;
                v.len_m = segments()[next_seg].polyline_length_m();
                v.next = self.plan_next(next_seg, next_forward);
            }

            let seg = &segments()[v.seg];
            let along_path = if v.forward {
                v.along_m
            } else {
                v.len_m - v.along_m
            };
            let (lon, lat) = seg.point_at(along_path);
            let (lon, lat) = offset_m(
                lon,
                lat,
                GPS_SIGMA_M * self.rng.gaussian(),
                GPS_SIGMA_M * self.rng.gaussian(),
            );
            let (lon, lat) = clamp_to_network(lon, lat);
            fixes.push(TrafficObservation {
                timestamp_ms: t.timestamp_millis(),
                lon,
                lat,
                segment_id: seg.id.to_string(),
                speed_kmh: v.speed_kmh,
                congestion_index: congestion_for(seg.speed_limit_kmh, v.speed_kmh),
                source: FeedSource::FleetGps,
                data_quality: DataQuality::Synthetic,
                confidence: 0.95,
            });
            self.vehicles[i] = v;
        }
        fixes
    }
}

/// Streams GPS telemetry from convoy vehicles and allied fleet units.
///
/// In edge-deployment mode, emulates FLEET_SIZE vehicles producing 1 Hz
/// fixes as they drive the Ahmedabad corridor network.
#[instrument(name = "fleet_gps_stream", skip(tx))]
pub async fn stream_fleet_telemetry(
    tx: mpsc::Sender<TrafficObservation>,
) -> Result<(), IngressError> {
    tracing::info!(
        vehicles = FLEET_SIZE,
        interval_ms = 1000,
        "fleet GPS telemetry stream initialized — Ahmedabad network, edge synthetic mode"
    );

    let mut fleet = FleetSim::new(
        CongestionField::from_env(),
        Utc::now().timestamp_millis() as u64 ^ 0xDEAD_BEEF,
        FLEET_SIZE,
        Utc::now(),
    );
    let mut ticker = tokio::time::interval(std::time::Duration::from_secs(1));
    ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    let mut last = Utc::now();
    let mut tick: u64 = 0;

    loop {
        ticker.tick().await;
        let now = Utc::now();
        // real elapsed time drives the motion (bounded after a stall)
        let dt_s = ((now - last).num_milliseconds() as f64 / 1000.0).clamp(0.0, 5.0);
        last = now;
        tick += 1;

        for obs in fleet.tick(now, dt_s) {
            if tx.send(obs).await.is_err() {
                tracing::error!("feed channel closed — shutting down GPS stream");
                return Err(IngressError::ChannelSend);
            }
        }

        if tick.is_multiple_of(60) {
            tracing::debug!(tick, vehicles = fleet.len(), "fleet GPS 60s summary");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::feeds::city::{DEFAULT_CITY_SEED, ist, metres_per_degree, segment_by_id};
    use chrono::TimeZone;

    fn ist_at(h: u32, m: u32) -> DateTime<Utc> {
        ist()
            .with_ymd_and_hms(2026, 10, 22, h, m, 0)
            .unwrap()
            .with_timezone(&Utc)
    }

    /// Mean of speed/limit over `minutes` of 1 Hz fixes starting at `t0`.
    fn fleet_speed_ratio(t0: DateTime<Utc>, minutes: i64, seed: u64) -> f64 {
        let mut fleet = FleetSim::new(
            CongestionField::new(DEFAULT_CITY_SEED),
            seed,
            FLEET_SIZE,
            t0,
        );
        let (mut sum, mut n) = (0.0, 0usize);
        for s in 1..=minutes * 60 {
            for obs in fleet.tick(t0 + chrono::Duration::seconds(s), 1.0) {
                let limit = segment_by_id(obs.segment_id.parse().unwrap())
                    .unwrap()
                    .speed_limit_kmh;
                sum += (obs.speed_kmh / limit) as f64;
                n += 1;
            }
        }
        sum / n as f64
    }

    #[test]
    fn fleet_mean_speed_stays_near_prevailing_not_the_floor() {
        // the biased LCG collapsed fleet speeds onto the 0.2×limit clamp; off
        // peak the fleet must now run at a free-flow share of the limit
        let off_peak = fleet_speed_ratio(ist_at(14, 30), 30, 11);
        assert!(off_peak >= 0.6, "off-peak fleet mean {off_peak:.3}×limit");
        // and still at least half the limit through the evening peak
        let peak = fleet_speed_ratio(ist_at(19, 0), 30, 12);
        assert!(peak >= 0.5, "evening-peak fleet mean {peak:.3}×limit");
        assert!(
            peak < off_peak,
            "peak {peak:.3} should be slower than off-peak {off_peak:.3}"
        );
    }

    #[test]
    fn fleet_mean_speed_over_a_whole_day() {
        let mut sum = 0.0;
        for h in 0..24 {
            sum += fleet_speed_ratio(ist_at(h, 0), 5, 100 + h as u64);
        }
        let day = sum / 24.0;
        assert!(day >= 0.6, "24 h fleet mean {day:.3}×limit");
    }

    #[test]
    fn fleet_is_shared_out_over_the_network() {
        let fleet = FleetSim::new(
            CongestionField::new(DEFAULT_CITY_SEED),
            3,
            FLEET_SIZE,
            ist_at(9, 0),
        );
        assert_eq!(fleet.len(), FLEET_SIZE);
        let distinct: std::collections::HashSet<usize> =
            fleet.vehicles.iter().map(|v| v.seg).collect();
        assert_eq!(
            distinct.len(),
            FLEET_SIZE,
            "vehicles start on distinct segments"
        );
        // the largest connected piece gets most of the fleet
        let largest = &Topology::ahmedabad().components()[0];
        let on_largest = fleet
            .vehicles
            .iter()
            .filter(|v| largest.contains(&v.seg))
            .count();
        assert!(on_largest >= FLEET_SIZE / 2);
    }

    #[test]
    fn vehicles_stay_on_their_roads_without_teleporting() {
        let t0 = ist_at(10, 0);
        let mut fleet = FleetSim::new(CongestionField::new(DEFAULT_CITY_SEED), 21, FLEET_SIZE, t0);
        let mut prev: Vec<Option<(f64, f64)>> = vec![None; fleet.len()];
        let mut visited = std::collections::HashSet::new();
        for s in 1..=1800 {
            let fixes = fleet.tick(t0 + chrono::Duration::seconds(s), 1.0);
            assert_eq!(fixes.len(), FLEET_SIZE);
            for (i, obs) in fixes.iter().enumerate() {
                let seg = segment_by_id(obs.segment_id.parse().unwrap()).unwrap();
                visited.insert(seg.id);
                let off = seg.distance_m(obs.lon, obs.lat);
                assert!(off < 15.0, "fix {off:.1} m off segment {}", seg.id);
                assert!((72.45..=72.75).contains(&obs.lon) && (22.90..=23.20).contains(&obs.lat));
                // a vehicle just off a faster road may briefly exceed this one's limit
                assert!(obs.speed_kmh > 0.0 && obs.speed_kmh <= 1.2 * seg.speed_limit_kmh);
                assert_eq!(obs.data_quality, DataQuality::Synthetic);
                if let Some((plon, plat)) = prev[i] {
                    let (m_lon, m_lat) = metres_per_degree(obs.lat);
                    let step = (((obs.lon - plon) * m_lon).powi(2)
                        + ((obs.lat - plat) * m_lat).powi(2))
                    .sqrt();
                    // ≤ 1.05×100 km/h for 1 s plus GPS noise on both fixes
                    assert!(
                        step < 29.2 + 6.0 * GPS_SIGMA_M,
                        "vehicle {i} jumped {step:.1} m"
                    );
                }
                prev[i] = Some((obs.lon, obs.lat));
            }
        }
        assert!(
            visited.len() >= 12,
            "fleet covered only {} segments in 30 min",
            visited.len()
        );
    }

    #[test]
    fn consecutive_fixes_are_never_identical() {
        // night (speeds press against the upper bound) and evening peak
        for (h, seed) in [(3, 5), (19, 6)] {
            let t0 = ist_at(h, 0);
            let mut fleet = FleetSim::new(
                CongestionField::new(DEFAULT_CITY_SEED),
                seed,
                FLEET_SIZE,
                t0,
            );
            let mut last: Vec<Option<(f32, f32)>> = vec![None; fleet.len()];
            for s in 1..=900 {
                for (i, obs) in fleet
                    .tick(t0 + chrono::Duration::seconds(s), 1.0)
                    .iter()
                    .enumerate()
                {
                    let reading = (obs.speed_kmh, obs.congestion_index);
                    assert_ne!(
                        last[i],
                        Some(reading),
                        "vehicle {i} repeated {reading:?} at {h}:00"
                    );
                    last[i] = Some(reading);
                }
            }
        }
    }
}
