use std::collections::HashMap;

use crate::anomaly::config::AnomalyConfig;
use crate::anomaly::flags::{AnomalyFlag, AnomalyResult};
use crate::anomaly::stats::SegmentStats;
use crate::{DataQuality, TrafficObservation};

/// Tier-1 inline anomaly detector.
///
/// Runs an 8-check sequence on every observation in ~500ns target.
/// Maintains per-segment rolling statistics for statistical anomaly detection.
pub struct AnomalyDetector {
    config: AnomalyConfig,
    segment_stats: HashMap<String, SegmentStats>,
}

impl AnomalyDetector {
    pub fn new(config: AnomalyConfig) -> Self {
        Self {
            config,
            segment_stats: HashMap::new(),
        }
    }

    /// Run the 8-check anomaly detection sequence on a single observation.
    /// Returns the observation (potentially re-tagged) with anomaly flags.
    pub fn check(&mut self, mut obs: TrafficObservation) -> AnomalyResult {
        let mut flags = Vec::new();
        let now_ms = chrono::Utc::now().timestamp_millis();

        let stats = self
            .segment_stats
            .entry(obs.segment_id.clone())
            .or_insert_with(|| SegmentStats::new(self.config.rolling_window_size));

        // ── Check 1: Future timestamp ───────────────────────────────
        if obs.timestamp_ms > now_ms + self.config.future_tolerance_ms {
            flags.push(AnomalyFlag::FutureTimestamp);
        }

        // ── Check 2: Stale data ─────────────────────────────────────
        if now_ms - obs.timestamp_ms > self.config.stale_threshold_ms {
            flags.push(AnomalyFlag::StaleData);
        }

        // ── Check 3: Speed jump ─────────────────────────────────────
        if stats.last_timestamp_ms > 0 {
            let delta = (obs.speed_kmh - stats.last_speed_kmh).abs();
            if delta > self.config.max_speed_jump_kmh {
                flags.push(AnomalyFlag::SpeedJump);
            }
        }

        // ── Check 4: Phantom reading (identical repeats) ────────────
        if stats.repeat_count >= self.config.phantom_repeat_threshold {
            // Stats haven't been updated yet — check if the new reading
            // would continue the streak
            let current_hash = {
                use std::collections::hash_map::DefaultHasher;
                use std::hash::{Hash, Hasher};
                let mut h = DefaultHasher::new();
                obs.speed_kmh.to_bits().hash(&mut h);
                obs.congestion_index.to_bits().hash(&mut h);
                h.finish()
            };
            // If the hash matches, this extends the phantom streak
            let _ = current_hash; // phantom is checked after push below
        }

        // ── Check 5: Z-score on speed ──────────────────────────────
        if stats.count() >= self.config.min_observations {
            let mean = stats.speed_mean();
            let std = stats.speed_std_dev();
            if std > 0.0 {
                let z = ((obs.speed_kmh as f64) - mean).abs() / std;
                if z > self.config.z_score_threshold {
                    flags.push(AnomalyFlag::SpeedZScore);
                }
            }
        }

        // ── Check 6: IQR outlier on congestion ─────────────────────
        if stats.count() >= self.config.min_observations {
            let (q1, q3) = stats.congestion_iqr();
            let iqr = q3 - q1;
            let lower = q1 - self.config.iqr_multiplier * iqr;
            let upper = q3 + self.config.iqr_multiplier * iqr;
            let c = obs.congestion_index as f64;
            if c < lower || c > upper {
                flags.push(AnomalyFlag::CongestionOutlier);
            }
        }

        // ── Check 7: Road class plausibility ────────────────────────
        // Max plausible speeds by road class (simplified — full lookup in Phase 5)
        let max_for_class = match obs.segment_id.len() {
            // Without segment metadata, use a generous upper bound
            _ => 180.0_f32,
        };
        if obs.speed_kmh > max_for_class || obs.speed_kmh < -0.1 {
            flags.push(AnomalyFlag::RoadClassMismatch);
        }

        // ── Check 8: Spatial mismatch ──────────────────────────────
        // Requires segment centroid lookup — deferred to Phase 5 when DB is available.
        // For now, flag negative coordinates or obvious impossibilities.
        if obs.lon < -180.0 || obs.lon > 180.0 || obs.lat < -90.0 || obs.lat > 90.0 {
            flags.push(AnomalyFlag::SpatialMismatch);
        }

        // Update rolling stats AFTER checks (so checks use prior state)
        stats.push(obs.speed_kmh, obs.congestion_index, obs.timestamp_ms);

        // Re-check phantom after push
        if stats.repeat_count >= self.config.phantom_repeat_threshold
            && !flags.contains(&AnomalyFlag::PhantomReading)
        {
            flags.push(AnomalyFlag::PhantomReading);
        }

        // Tag the observation
        if !flags.is_empty() {
            obs.data_quality = DataQuality::Anomalous;
        }

        AnomalyResult::compute(obs, flags)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::FeedSource;

    fn make_obs(timestamp_ms: i64, speed: f32, congestion: f32) -> TrafficObservation {
        TrafficObservation {
            timestamp_ms,
            lon: 77.2090,
            lat: 28.6139,
            segment_id: "seg_001".to_string(),
            speed_kmh: speed,
            congestion_index: congestion,
            source: FeedSource::FleetGps,
            data_quality: DataQuality::default(),
            confidence: 1.0,
        }
    }

    #[test]
    fn test_future_timestamp() {
        let mut detector = AnomalyDetector::new(AnomalyConfig::default());
        let future_ts = chrono::Utc::now().timestamp_millis() + 120_000; // 2 min in future
        let result = detector.check(make_obs(future_ts, 50.0, 0.3));
        assert!(result.flags.contains(&AnomalyFlag::FutureTimestamp));
    }

    #[test]
    fn test_stale_data() {
        let mut detector = AnomalyDetector::new(AnomalyConfig::default());
        let stale_ts = chrono::Utc::now().timestamp_millis() - 600_000; // 10 min ago
        let result = detector.check(make_obs(stale_ts, 50.0, 0.3));
        assert!(result.flags.contains(&AnomalyFlag::StaleData));
    }

    #[test]
    fn test_speed_jump() {
        let mut detector = AnomalyDetector::new(AnomalyConfig::default());
        let now = chrono::Utc::now().timestamp_millis();
        // First reading — no jump possible
        let r1 = detector.check(make_obs(now, 50.0, 0.3));
        assert!(!r1.flags.contains(&AnomalyFlag::SpeedJump));
        // Second reading — 60 km/h jump (> 40 threshold)
        let r2 = detector.check(make_obs(now + 1000, 110.0, 0.3));
        assert!(r2.flags.contains(&AnomalyFlag::SpeedJump));
    }

    #[test]
    fn test_phantom_reading() {
        let config = AnomalyConfig {
            phantom_repeat_threshold: 3,
            ..Default::default()
        };
        let mut detector = AnomalyDetector::new(config);
        let now = chrono::Utc::now().timestamp_millis();
        // Send 4 identical readings
        for i in 0..4 {
            let result = detector.check(make_obs(now + i * 1000, 50.0, 0.3));
            if i >= 2 {
                // After 3rd identical reading, phantom should trigger
                assert!(
                    result.flags.contains(&AnomalyFlag::PhantomReading),
                    "Expected phantom at iteration {i}"
                );
            }
        }
    }

    #[test]
    fn test_normal_observation_passes() {
        let mut detector = AnomalyDetector::new(AnomalyConfig::default());
        let now = chrono::Utc::now().timestamp_millis();
        let result = detector.check(make_obs(now, 50.0, 0.3));
        // Fresh, plausible observation should have no critical flags
        // (may have minor flags depending on timing)
        assert!(
            !result.flags.contains(&AnomalyFlag::FutureTimestamp)
                && !result.flags.contains(&AnomalyFlag::SpeedJump)
                && !result.flags.contains(&AnomalyFlag::SpatialMismatch)
        );
    }

    #[test]
    fn test_spatial_mismatch_invalid_coords() {
        let mut detector = AnomalyDetector::new(AnomalyConfig::default());
        let now = chrono::Utc::now().timestamp_millis();
        let mut obs = make_obs(now, 50.0, 0.3);
        obs.lon = 999.0; // impossible longitude
        let result = detector.check(obs);
        assert!(result.flags.contains(&AnomalyFlag::SpatialMismatch));
    }

    #[test]
    fn test_confidence_scoring() {
        let mut detector = AnomalyDetector::new(AnomalyConfig::default());
        let future_ts = chrono::Utc::now().timestamp_millis() + 120_000;
        let mut obs = make_obs(future_ts, 50.0, 0.3);
        obs.lon = 999.0; // spatial mismatch too
        let result = detector.check(obs);
        // Two high-severity flags: confidence = 1.0 - 2*0.50 = 0.0
        assert!(result.confidence < 0.01);
        assert!(result.is_anomalous);
    }
}
