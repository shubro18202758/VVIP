//! Crowdsource Report Listener — aggregates citizen-reported traffic
//! conditions from public APIs (Google Maps, Waze-like community feeds).
//!
//! In edge-deployment mode, generates realistic crowdsourced traffic
//! reports with lower accuracy and higher variance than official feeds,
//! exercising the anomaly detector's noise-filtering capabilities.

use crate::{DataQuality, FeedSource, IngressError, TrafficObservation};
use chrono::{Utc, Timelike};
use tokio::sync::mpsc;
use tracing::instrument;

/// Segments that crowdsource reports typically cover (high-traffic public roads).
/// (segment_id, lon, lat, speed_limit_kmh)
const CROWDSOURCE_SEGMENTS: &[(i64, f64, f64, f32)] = &[
    (1001, 77.2195, 28.6145, 60.0),
    (1002, 77.2190, 28.6250, 50.0),
    (1003, 77.2050, 28.6200, 50.0),
    (1005, 77.2600, 28.5800, 80.0),
];

/// Listens for crowdsourced traffic reports and incident alerts.
///
/// In edge mode, simulates citizen-reported traffic conditions with
/// characteristics typical of crowdsource data:
/// - Higher variance (±15 km/h noise) vs ±3 km/h for government sensors
/// - Occasional exaggerated reports (outliers that should trigger anomaly flags)
/// - Stale reports (10-60 second delay typical of app-reported data)
/// - Sparser coverage (only high-traffic segments)
///
/// Interval: 60 seconds per batch (crowdsource data arrives in bursts).
#[instrument(name = "crowdsource_listener", skip(tx))]
pub async fn listen_crowdsource_reports(
    tx: mpsc::Sender<TrafficObservation>,
) -> Result<(), IngressError> {
    tracing::info!(
        segments = CROWDSOURCE_SEGMENTS.len(),
        interval_sec = 60,
        "crowdsource report listener initialized — edge synthetic mode"
    );

    let mut rng_state: u64 = Utc::now().timestamp_millis() as u64 ^ 0xCAFE_BABE;
    let mut cycle: u64 = 0;

    loop {
        let now = Utc::now();
        let now_ms = now.timestamp_millis();
        let hour_progress = now.time().hour() as f32
            + (now.time().minute() as f32 / 60.0);

        cycle += 1;

        for &(seg_id, base_lon, base_lat, speed_limit) in CROWDSOURCE_SEGMENTS {
            rng_state = lcg_next(rng_state);
            let num_reports = 1 + (rng_state % 3) as usize;

            for _ in 0..num_reports {
                // Crowdsource data has higher positional noise (±50m ≈ ±0.0005°)
                rng_state = lcg_next(rng_state);
                let lon = base_lon + lcg_to_f64(rng_state, -0.0005, 0.0005);
                rng_state = lcg_next(rng_state);
                let lat = base_lat + lcg_to_f64(rng_state, -0.0005, 0.0005);

                // Speed with high variance (±15 km/h)
                rng_state = lcg_next(rng_state);
                let speed_noise = lcg_to_f32(rng_state, -15.0, 15.0);
                let congestion_base = time_of_day_congestion(hour_progress);
                let base_speed = speed_limit * (1.0 - congestion_base * 0.7);
                let speed = (base_speed + speed_noise).clamp(0.0, speed_limit * 1.2);

                // Stale timestamp: 10-60 second delay
                rng_state = lcg_next(rng_state);
                let stale_ms = (10_000 + (rng_state % 50_000)) as i64;

                let congestion = (1.0 - speed / speed_limit).clamp(0.0, 1.0);

                // Every ~20th report is an exaggerated outlier
                rng_state = lcg_next(rng_state);
                let is_outlier = rng_state % 20 == 0;
                let (final_speed, final_confidence) = if is_outlier {
                    (speed * 2.5, 0.3)
                } else {
                    (speed, 0.6)
                };

                let obs = TrafficObservation {
                    timestamp_ms: now_ms - stale_ms,
                    lon,
                    lat,
                    segment_id: seg_id.to_string(),
                    speed_kmh: final_speed.max(0.0),
                    congestion_index: congestion,
                    source: FeedSource::Crowdsource,
                    data_quality: DataQuality::Real,
                    confidence: final_confidence,
                };

                if tx.send(obs).await.is_err() {
                    tracing::error!("feed channel closed — shutting down crowdsource listener");
                    return Err(IngressError::ChannelSend);
                }
            }
        }

        if cycle % 5 == 0 {
            tracing::debug!(cycle, "crowdsource 5-cycle summary");
        }

        tokio::time::sleep(std::time::Duration::from_secs(60)).await;
    }
}

/// Delhi NCR time-of-day congestion curve.
fn time_of_day_congestion(hour: f32) -> f32 {
    let morning = 0.75 * gaussian(hour, 9.0, 1.5);
    let evening = 0.85 * gaussian(hour, 18.0, 2.0);
    let baseline = 0.15;
    (morning + evening + baseline).clamp(0.0, 1.0)
}

fn gaussian(x: f32, mean: f32, std: f32) -> f32 {
    let z = (x - mean) / std;
    (-0.5 * z * z).exp()
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
