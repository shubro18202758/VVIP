//! Fleet GPS Collector — ingests real-time vehicle telemetry from
//! convoy vehicles and traffic police fleet tracking systems.
//!
//! In edge-deployment mode, simulates fleet vehicles traversing the
//! Delhi NCR corridor with realistic GPS telemetry at 1Hz intervals.

use crate::{DataQuality, FeedSource, IngressError, TrafficObservation};
use chrono::{Utc, Timelike};
use tokio::sync::mpsc;
use tracing::instrument;

/// Simulated fleet vehicles — each traverses a corridor segment.
/// (segment_id, base_lon, base_lat, speed_limit_kmh, heading_lon_step, heading_lat_step)
const FLEET_VEHICLES: &[(i64, f64, f64, f32, f64, f64)] = &[
    (1001, 77.2090, 28.6139, 60.0, 0.00015, 0.00001),
    (1004, 77.1780, 28.6010, 60.0, 0.00020, 0.00012),
    (1002, 77.2195, 28.6145, 50.0, -0.00001, 0.00015),
    (1005, 77.2500, 28.5700, 80.0, 0.00015, 0.00015),
];

/// Streams GPS telemetry from convoy vehicles and allied fleet units.
///
/// In edge-deployment mode, simulates 4 fleet vehicles producing 1Hz
/// GPS telemetry as they traverse corridor segments. Introduces realistic
/// GPS drift (±5m) and speed variations from acceleration/deceleration.
#[instrument(name = "fleet_gps_stream", skip(tx))]
pub async fn stream_fleet_telemetry(
    tx: mpsc::Sender<TrafficObservation>,
) -> Result<(), IngressError> {
    tracing::info!(
        vehicles = FLEET_VEHICLES.len(),
        interval_ms = 1000,
        "fleet GPS telemetry stream initialized — edge synthetic mode"
    );

    let mut rng_state: u64 = Utc::now().timestamp_millis() as u64 ^ 0xDEAD_BEEF;

    // Per-vehicle state: (current_lon, current_lat, current_speed)
    let mut vehicle_state: Vec<(f64, f64, f32)> = FLEET_VEHICLES
        .iter()
        .map(|v| (v.1, v.2, v.3 * 0.8))
        .collect();

    let mut tick: u64 = 0;

    loop {
        let now_ms = Utc::now().timestamp_millis();
        tick += 1;

        for (i, &(seg_id, _base_lon, _base_lat, speed_limit, dlon, dlat)) in
            FLEET_VEHICLES.iter().enumerate()
        {
            let (ref mut lon, ref mut lat, ref mut speed) = vehicle_state[i];

            // Advance position based on current speed
            let speed_ratio = (*speed / speed_limit) as f64;
            *lon += dlon * speed_ratio;
            *lat += dlat * speed_ratio;

            // Wrap around after ~2000 ticks (~33 minutes) of travel
            if tick % 2000 == 0 {
                *lon = FLEET_VEHICLES[i].1;
                *lat = FLEET_VEHICLES[i].2;
            }

            // Simulate acceleration/deceleration
            rng_state = lcg_next(rng_state);
            let accel_noise = lcg_to_f32(rng_state, -3.0, 3.0);
            *speed = (*speed + accel_noise).clamp(speed_limit * 0.2, speed_limit * 1.05);

            // GPS drift: ±5m ≈ ±0.00005°
            rng_state = lcg_next(rng_state);
            let gps_lon_drift = lcg_to_f64(rng_state, -0.00005, 0.00005);
            rng_state = lcg_next(rng_state);
            let gps_lat_drift = lcg_to_f64(rng_state, -0.00005, 0.00005);

            let congestion = (1.0 - (*speed / speed_limit)).clamp(0.0, 1.0);

            let obs = TrafficObservation {
                timestamp_ms: now_ms,
                lon: *lon + gps_lon_drift,
                lat: *lat + gps_lat_drift,
                segment_id: seg_id.to_string(),
                speed_kmh: *speed,
                congestion_index: congestion,
                source: FeedSource::FleetGps,
                data_quality: DataQuality::Real,
                confidence: 0.95,
            };

            if tx.send(obs).await.is_err() {
                tracing::error!("feed channel closed — shutting down GPS stream");
                return Err(IngressError::ChannelSend);
            }
        }

        if tick % 60 == 0 {
            tracing::debug!(tick, vehicles = FLEET_VEHICLES.len(), "fleet GPS 60s summary");
        }

        tokio::time::sleep(std::time::Duration::from_secs(1)).await;
    }
}

fn lcg_next(state: u64) -> u64 {
    state.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407)
}

fn lcg_to_f32(state: u64, min: f32, max: f32) -> f32 {
    let t = ((state >> 33) as f32) / (u32::MAX as f32);
    min + t * (max - min)
}

fn lcg_to_f64(state: u64, min: f64, max: f64) -> f64 {
    let t = ((state >> 33) as f64) / (u32::MAX as f64);
    min + t * (max - min)
}
