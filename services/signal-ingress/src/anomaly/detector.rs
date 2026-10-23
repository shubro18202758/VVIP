use std::collections::HashMap;

use crate::anomaly::config::AnomalyConfig;
use crate::anomaly::flags::{AnomalyFlag, AnomalyResult};
use crate::anomaly::stats::SegmentStats;
use crate::feeds::city::{self, Segment};
use crate::{DataQuality, FeedSource, TrafficObservation};

/// Absolute speed bound for segments the detector has no metadata for (km/h).
const UNKNOWN_SEGMENT_MAX_KMH: f32 = 180.0;

/// Tier-1 inline anomaly detector.
///
/// Runs an 8-check sequence on every observation in ~500ns target.
/// Maintains rolling statistics per (segment, feed source): each source is
/// judged against its OWN recent history on the segment — a crowd report is
/// not an outlier merely for disagreeing with a loop detector, and a sensor
/// fault shows up against the same sensor's readings. Windows are bounded by
/// count and age; the statistical checks need a per-source number of
/// readings (fewer for the 5-minute mapping snapshots) and use scale floors,
/// so ordinary traffic variation is never flagged however steady the window
/// was.
/// With the corridor network attached (`with_network`), the road-class and
/// spatial checks use the claimed segment's speed limit and geometry.
pub struct AnomalyDetector {
    config: AnomalyConfig,
    segment_stats: HashMap<(String, FeedSource), SegmentStats>,
    network: HashMap<String, &'static Segment>,
}

impl AnomalyDetector {
    pub fn new(config: AnomalyConfig) -> Self {
        Self {
            config,
            segment_stats: HashMap::new(),
            network: HashMap::new(),
        }
    }

    /// Detector that knows the seeded Ahmedabad corridor segments.
    pub fn with_network(config: AnomalyConfig) -> Self {
        let mut detector = Self::new(config);
        detector.network = city::segments()
            .iter()
            .map(|s| (s.id.to_string(), s))
            .collect();
        detector
    }

    /// Run the 8-check anomaly detection sequence on a single observation.
    /// Returns the observation (potentially re-tagged) with anomaly flags.
    pub fn check(&mut self, obs: TrafficObservation) -> AnomalyResult {
        self.check_at(obs, chrono::Utc::now().timestamp_millis())
    }

    /// `check` against an explicit reference clock (`now_ms`, epoch ms) for
    /// the future/stale checks — deterministic replays and tests.
    pub fn check_at(&mut self, mut obs: TrafficObservation, now_ms: i64) -> AnomalyResult {
        let mut flags = Vec::new();
        let segment = self.network.get(&obs.segment_id).copied();

        let (window, max_age) = (
            self.config.rolling_window_size,
            self.config.max_window_age_ms,
        );
        let min_observations = self.config.min_observations_for(obs.source);
        let stats = self
            .segment_stats
            .entry((obs.segment_id.clone(), obs.source))
            .or_insert_with(|| SegmentStats::with_max_age(window, max_age));

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
        // Evaluated after the rolling stats absorb this reading (below), so
        // the streak includes it.

        // ── Check 5: Z-score on speed ──────────────────────────────
        if stats.count() >= min_observations {
            let mean = stats.speed_mean();
            let std = stats.speed_std_dev().max(self.config.min_speed_std_kmh);
            if std > 0.0 {
                let z = ((obs.speed_kmh as f64) - mean).abs() / std;
                if z > self.config.z_score_threshold {
                    flags.push(AnomalyFlag::SpeedZScore);
                }
            }
        }

        // ── Check 6: IQR outlier on congestion ─────────────────────
        if stats.count() >= min_observations {
            let (q1, q3) = stats.congestion_iqr();
            let iqr = (q3 - q1).max(self.config.min_congestion_iqr);
            let lower = q1 - self.config.iqr_multiplier * iqr;
            let upper = q3 + self.config.iqr_multiplier * iqr;
            let c = obs.congestion_index as f64;
            if c < lower || c > upper {
                flags.push(AnomalyFlag::CongestionOutlier);
            }
        }

        // ── Check 7: Road class plausibility ────────────────────────
        // Known segment: its speed limit × a class factor; otherwise a
        // generous absolute bound.
        let max_for_class = segment
            .map(|seg| seg.speed_limit_kmh * seg.road_class.plausible_speed_factor())
            .unwrap_or(UNKNOWN_SEGMENT_MAX_KMH);
        if obs.speed_kmh > max_for_class || obs.speed_kmh < -0.1 {
            flags.push(AnomalyFlag::RoadClassMismatch);
        }

        // ── Check 8: Spatial mismatch ──────────────────────────────
        // Impossible coordinates, or (known segment) a position farther than
        // spatial_tolerance_m from the claimed segment's polyline.
        let impossible = obs.lon < -180.0 || obs.lon > 180.0 || obs.lat < -90.0 || obs.lat > 90.0;
        let off_segment = !impossible
            && segment.is_some_and(|seg| {
                seg.distance_m(obs.lon, obs.lat) > self.config.spatial_tolerance_m
            });
        if impossible || off_segment {
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

        // Tag the observation — keeping the quality it arrived with: a
        // flagged Synthetic reading is still a SIMULATED-feed reading, and
        // the anomaly event says so (AnomalyEvent.feed_quality).
        if !flags.is_empty() {
            let feed_quality = obs.data_quality;
            obs.data_quality = DataQuality::Anomalous;
            return AnomalyResult::compute(obs, flags).with_feed_quality(feed_quality);
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
            lon: 72.5747,
            lat: 23.02875,
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

    fn on_segment(segment_id: &str, lon: f64, lat: f64, speed: f32) -> TrafficObservation {
        let mut obs = make_obs(chrono::Utc::now().timestamp_millis(), speed, 0.3);
        obs.segment_id = segment_id.to_string();
        obs.lon = lon;
        obs.lat = lat;
        obs
    }

    #[test]
    fn test_network_spatial_mismatch_for_off_road_position() {
        let mut detector = AnomalyDetector::with_network(AnomalyConfig::default());
        let seg = city::segment_by_id(1001).unwrap();
        // on the road: clean
        let r = detector.check(on_segment("1001", seg.mid.0, seg.mid.1, 45.0));
        assert!(!r.flags.contains(&AnomalyFlag::SpatialMismatch));
        // ~1 km east of Ashram Road (South): flagged
        let r = detector.check(on_segment("1001", seg.mid.0 + 0.01, seg.mid.1, 45.0));
        assert!(r.flags.contains(&AnomalyFlag::SpatialMismatch));
        // a reading ~900 km away on an Ahmedabad segment id is flagged
        let r = detector.check(on_segment("1001", 77.2195, 28.6145, 45.0));
        assert!(r.flags.contains(&AnomalyFlag::SpatialMismatch));
    }

    #[test]
    fn test_network_road_class_bound_uses_speed_limit() {
        let mut detector = AnomalyDetector::with_network(AnomalyConfig::default());
        let seg = city::segment_by_id(1005).unwrap(); // Relief Road, 40 km/h
        let r = detector.check(on_segment("1005", seg.mid.0, seg.mid.1, 100.0));
        assert!(r.flags.contains(&AnomalyFlag::RoadClassMismatch));
        let r = detector.check(on_segment("1005", seg.mid.0, seg.mid.1, 38.0));
        assert!(!r.flags.contains(&AnomalyFlag::RoadClassMismatch));
    }

    #[test]
    fn test_sources_are_judged_against_their_own_history() {
        let mut detector = AnomalyDetector::new(AnomalyConfig::default());
        let now = chrono::Utc::now().timestamp_millis();
        // a steady fleet vehicle on the segment...
        for i in 0..20 {
            detector.check(make_obs(now + i * 1000, 50.0 + (i % 2) as f32 * 0.2, 0.30));
        }
        // ...does not make a 60 km/h crowd report on it a statistical outlier
        let mut crowd = make_obs(now + 21_000, 60.0, 0.15);
        crowd.source = FeedSource::Crowdsource;
        let r = detector.check(crowd);
        assert!(!r.flags.contains(&AnomalyFlag::SpeedZScore));
        assert!(!r.flags.contains(&AnomalyFlag::CongestionOutlier));
        assert!(!r.flags.contains(&AnomalyFlag::SpeedJump));
        // while the same jump from the fleet vehicle itself is still caught
        let r = detector.check(make_obs(now + 22_000, 95.0, 0.0));
        assert!(r.flags.contains(&AnomalyFlag::SpeedZScore));
        assert!(r.flags.contains(&AnomalyFlag::SpeedJump));
    }

    #[test]
    fn test_scale_floor_ignores_ordinary_variation() {
        let mut detector = AnomalyDetector::new(AnomalyConfig::default());
        let now = chrono::Utc::now().timestamp_millis();
        // an unusually steady window (std ≈ 0.1 km/h)...
        for i in 0..20 {
            detector.check(make_obs(now + i * 1000, 50.0 + (i % 2) as f32 * 0.2, 0.30));
        }
        // ...a 3 km/h / 0.04 change is traffic, not an anomaly
        let r = detector.check(make_obs(now + 21_000, 53.0, 0.34));
        assert!(!r.flags.contains(&AnomalyFlag::SpeedZScore));
        assert!(!r.flags.contains(&AnomalyFlag::CongestionOutlier));
    }

    #[test]
    fn test_check_at_uses_the_reference_clock() {
        let mut detector = AnomalyDetector::new(AnomalyConfig::default());
        let reference = 1_792_670_400_000;
        let r = detector.check_at(make_obs(reference - 1_000, 50.0, 0.3), reference);
        assert!(!r.flags.contains(&AnomalyFlag::StaleData));
        assert!(!r.flags.contains(&AnomalyFlag::FutureTimestamp));
    }
}
