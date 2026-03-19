//! Integration tests for the Tier-1 inline anomaly detection pipeline.
//!
//! Tests the 8-check sequence: future timestamp, stale data, speed jump,
//! phantom reading, z-score, IQR outlier, road-class plausibility, spatial mismatch.
//! Also validates `SegmentStats` rolling statistics and `AnomalyResult` confidence scoring.

use signal_ingress::anomaly::config::AnomalyConfig;
use signal_ingress::anomaly::detector::AnomalyDetector;
use signal_ingress::anomaly::flags::{AnomalyFlag, AnomalyResult, Severity};
use signal_ingress::anomaly::stats::SegmentStats;
use signal_ingress::{DataQuality, FeedSource, TrafficObservation};

// ─── Helpers ──────────────────────────────────────────────────────────────────

fn now_ms() -> i64 {
    chrono::Utc::now().timestamp_millis()
}

fn make_obs(segment_id: &str, timestamp_ms: i64, speed: f32, congestion: f32) -> TrafficObservation {
    TrafficObservation {
        timestamp_ms,
        lon: 77.2090,
        lat: 28.6139,
        segment_id: segment_id.to_string(),
        speed_kmh: speed,
        congestion_index: congestion,
        source: FeedSource::FleetGps,
        data_quality: DataQuality::Real,
        confidence: 1.0,
    }
}

fn make_obs_at_coords(lon: f64, lat: f64, speed: f32) -> TrafficObservation {
    TrafficObservation {
        timestamp_ms: now_ms(),
        lon,
        lat,
        segment_id: "seg_100".to_string(),
        speed_kmh: speed,
        congestion_index: 0.3,
        source: FeedSource::GovernmentTraffic,
        data_quality: DataQuality::Real,
        confidence: 1.0,
    }
}

// ─── SegmentStats Tests ──────────────────────────────────────────────────────

#[test]
fn stats_empty_returns_zero() {
    let stats = SegmentStats::new(50);
    assert_eq!(stats.count(), 0);
    assert!((stats.speed_mean() - 0.0).abs() < f64::EPSILON);
    assert!((stats.speed_std_dev() - 0.0).abs() < f64::EPSILON);
    assert!((stats.congestion_median() - 0.0).abs() < f64::EPSILON);
    assert!((stats.congestion_mad() - 0.0).abs() < f64::EPSILON);
}

#[test]
fn stats_single_value() {
    let mut stats = SegmentStats::new(50);
    stats.push(60.0, 0.5, 1000);
    assert_eq!(stats.count(), 1);
    assert!((stats.speed_mean() - 60.0).abs() < 0.001);
    assert!((stats.speed_std_dev() - 0.0).abs() < 0.001); // need >= 2 for std
}

#[test]
fn stats_mean_calculation() {
    let mut stats = SegmentStats::new(50);
    let speeds = [30.0, 40.0, 50.0, 60.0, 70.0];
    for &s in &speeds {
        stats.push(s, 0.3, 1000);
    }
    assert!((stats.speed_mean() - 50.0).abs() < 0.001);
}

#[test]
fn stats_std_dev_calculation() {
    let mut stats = SegmentStats::new(50);
    // Known std dev: [10, 20, 30, 40, 50] -> mean=30, sample_std~=15.811
    for &s in &[10.0, 20.0, 30.0, 40.0, 50.0] {
        stats.push(s, 0.5, 1000);
    }
    let std = stats.speed_std_dev();
    assert!((std - 15.8114).abs() < 0.01, "Expected ~15.81, got {std}");
}

#[test]
fn stats_median_odd_count() {
    let mut stats = SegmentStats::new(50);
    for &c in &[0.1, 0.9, 0.3, 0.7, 0.5] {
        stats.push(50.0, c, 1000);
    }
    let median = stats.congestion_median();
    assert!((median - 0.5).abs() < 0.001, "Expected 0.5, got {median}");
}

#[test]
fn stats_median_even_count() {
    let mut stats = SegmentStats::new(50);
    for &c in &[0.1, 0.3, 0.5, 0.7] {
        stats.push(50.0, c, 1000);
    }
    let median = stats.congestion_median();
    assert!((median - 0.4).abs() < 0.001, "Expected 0.4, got {median}");
}

#[test]
fn stats_iqr_with_enough_data() {
    let mut stats = SegmentStats::new(50);
    for i in 0..20 {
        stats.push(50.0, i as f32 * 0.05, 1000);
    }
    let (q1, q3) = stats.congestion_iqr();
    assert!(q1 < q3, "Q1={q1} should be < Q3={q3}");
    assert!(q1 >= 0.0);
    assert!(q3 <= 1.0);
}

#[test]
fn stats_iqr_too_few_returns_defaults() {
    let mut stats = SegmentStats::new(50);
    stats.push(50.0, 0.5, 1000);
    stats.push(50.0, 0.6, 1000);
    let (q1, q3) = stats.congestion_iqr();
    assert!((q1 - 0.0).abs() < 0.001);
    assert!((q3 - 1.0).abs() < 0.001);
}

#[test]
fn stats_mad_calculation() {
    let mut stats = SegmentStats::new(50);
    // Data: [1,1,2,2,4,6,9] -> median=2, deviations=[1,1,0,0,2,4,7] sorted=[0,0,1,1,2,4,7] -> MAD=1
    for &c in &[1.0, 1.0, 2.0, 2.0, 4.0, 6.0, 9.0] {
        stats.push(50.0, c, 1000);
    }
    let mad = stats.congestion_mad();
    assert!((mad - 1.0).abs() < 0.001, "Expected MAD=1.0, got {mad}");
}

#[test]
fn stats_window_evicts_oldest() {
    let mut stats = SegmentStats::new(3);
    stats.push(10.0, 0.1, 1000);
    stats.push(20.0, 0.2, 2000);
    stats.push(30.0, 0.3, 3000);
    stats.push(40.0, 0.4, 4000);
    assert_eq!(stats.count(), 3);
    // Mean should be (20+30+40)/3 = 30
    assert!((stats.speed_mean() - 30.0).abs() < 0.01);
}

#[test]
fn stats_repeat_count_increments() {
    let mut stats = SegmentStats::new(50);
    stats.push(50.0, 0.5, 1000);
    assert_eq!(stats.repeat_count, 1);
    stats.push(50.0, 0.5, 2000); // same reading
    assert_eq!(stats.repeat_count, 2);
    stats.push(50.0, 0.5, 3000); // same again
    assert_eq!(stats.repeat_count, 3);
}

#[test]
fn stats_repeat_count_resets_on_different() {
    let mut stats = SegmentStats::new(50);
    stats.push(50.0, 0.5, 1000);
    stats.push(50.0, 0.5, 2000);
    assert_eq!(stats.repeat_count, 2);
    stats.push(51.0, 0.5, 3000); // different speed
    assert_eq!(stats.repeat_count, 1);
}

// ─── AnomalyFlag Severity Tests ──────────────────────────────────────────────

#[test]
fn flag_severity_classification() {
    assert_eq!(AnomalyFlag::StaleData.severity(), Severity::Low);
    assert_eq!(AnomalyFlag::RoadClassMismatch.severity(), Severity::Low);
    assert_eq!(AnomalyFlag::SpeedZScore.severity(), Severity::Medium);
    assert_eq!(AnomalyFlag::CongestionOutlier.severity(), Severity::Medium);
    assert_eq!(AnomalyFlag::SpeedJump.severity(), Severity::Medium);
    assert_eq!(AnomalyFlag::FutureTimestamp.severity(), Severity::High);
    assert_eq!(AnomalyFlag::PhantomReading.severity(), Severity::High);
    assert_eq!(AnomalyFlag::SpatialMismatch.severity(), Severity::High);
}

#[test]
fn flag_names_are_snake_case() {
    assert_eq!(AnomalyFlag::FutureTimestamp.name(), "future_timestamp");
    assert_eq!(AnomalyFlag::StaleData.name(), "stale_data");
    assert_eq!(AnomalyFlag::SpeedJump.name(), "speed_jump");
    assert_eq!(AnomalyFlag::PhantomReading.name(), "phantom_reading");
    assert_eq!(AnomalyFlag::SpeedZScore.name(), "speed_zscore");
    assert_eq!(AnomalyFlag::CongestionOutlier.name(), "congestion_outlier");
    assert_eq!(AnomalyFlag::RoadClassMismatch.name(), "road_class_mismatch");
    assert_eq!(AnomalyFlag::SpatialMismatch.name(), "spatial_mismatch");
}

// ─── AnomalyResult Confidence Tests ──────────────────────────────────────────

#[test]
fn confidence_no_flags_is_one() {
    let obs = make_obs("seg_1", now_ms(), 50.0, 0.3);
    let result = AnomalyResult::compute(obs, vec![]);
    assert!((result.confidence - 1.0).abs() < f32::EPSILON);
    assert!(!result.is_anomalous);
}

#[test]
fn confidence_one_low_flag() {
    let obs = make_obs("seg_1", now_ms(), 50.0, 0.3);
    let result = AnomalyResult::compute(obs, vec![AnomalyFlag::StaleData]);
    // 1.0 - 0.15 = 0.85
    assert!((result.confidence - 0.85).abs() < 0.001);
    assert!(result.is_anomalous);
}

#[test]
fn confidence_one_medium_flag() {
    let obs = make_obs("seg_1", now_ms(), 50.0, 0.3);
    let result = AnomalyResult::compute(obs, vec![AnomalyFlag::SpeedJump]);
    // 1.0 - 0.30 = 0.70
    assert!((result.confidence - 0.70).abs() < 0.001);
}

#[test]
fn confidence_one_high_flag() {
    let obs = make_obs("seg_1", now_ms(), 50.0, 0.3);
    let result = AnomalyResult::compute(obs, vec![AnomalyFlag::FutureTimestamp]);
    // 1.0 - 0.50 = 0.50
    assert!((result.confidence - 0.50).abs() < 0.001);
}

#[test]
fn confidence_multiple_flags_combines() {
    let obs = make_obs("seg_1", now_ms(), 50.0, 0.3);
    // 1 high + 1 medium + 1 low = 1.0 - (0.50 + 0.30 + 0.15) = 0.05
    let flags = vec![
        AnomalyFlag::FutureTimestamp,
        AnomalyFlag::SpeedJump,
        AnomalyFlag::StaleData,
    ];
    let result = AnomalyResult::compute(obs, flags);
    assert!((result.confidence - 0.05).abs() < 0.001);
}

#[test]
fn confidence_floors_at_zero() {
    let obs = make_obs("seg_1", now_ms(), 50.0, 0.3);
    // 2 high flags: 1.0 - 2*0.50 = 0.0
    let flags = vec![AnomalyFlag::FutureTimestamp, AnomalyFlag::SpatialMismatch];
    let result = AnomalyResult::compute(obs, flags);
    assert!(result.confidence >= 0.0);
    assert!(result.confidence < 0.01);
}

// ─── AnomalyDetector: Check 1 — Future Timestamp ────────────────────────────

#[test]
fn detector_flags_future_timestamp() {
    let mut det = AnomalyDetector::new(AnomalyConfig::default());
    let future_ts = now_ms() + 120_000; // 2 min in future (> 30s tolerance)
    let result = det.check(make_obs("seg_1", future_ts, 50.0, 0.3));
    assert!(result.flags.contains(&AnomalyFlag::FutureTimestamp));
}

#[test]
fn detector_allows_slight_future_within_tolerance() {
    let mut det = AnomalyDetector::new(AnomalyConfig::default());
    let slight_future = now_ms() + 15_000; // 15s (< 30s tolerance)
    let result = det.check(make_obs("seg_1", slight_future, 50.0, 0.3));
    assert!(!result.flags.contains(&AnomalyFlag::FutureTimestamp));
}

// ─── AnomalyDetector: Check 2 — Stale Data ──────────────────────────────────

#[test]
fn detector_flags_stale_data() {
    let mut det = AnomalyDetector::new(AnomalyConfig::default());
    let stale_ts = now_ms() - 600_000; // 10 min old (> 5 min threshold)
    let result = det.check(make_obs("seg_1", stale_ts, 50.0, 0.3));
    assert!(result.flags.contains(&AnomalyFlag::StaleData));
}

#[test]
fn detector_allows_recent_data() {
    let mut det = AnomalyDetector::new(AnomalyConfig::default());
    let recent_ts = now_ms() - 60_000; // 1 min old (<5 min threshold)
    let result = det.check(make_obs("seg_1", recent_ts, 50.0, 0.3));
    assert!(!result.flags.contains(&AnomalyFlag::StaleData));
}

// ─── AnomalyDetector: Check 3 — Speed Jump ──────────────────────────────────

#[test]
fn detector_flags_speed_jump() {
    let mut det = AnomalyDetector::new(AnomalyConfig::default());
    let now = now_ms();
    det.check(make_obs("seg_1", now, 50.0, 0.3));
    let result = det.check(make_obs("seg_1", now + 1000, 110.0, 0.3));
    assert!(result.flags.contains(&AnomalyFlag::SpeedJump));
}

#[test]
fn detector_allows_gradual_speed_change() {
    let mut det = AnomalyDetector::new(AnomalyConfig::default());
    let now = now_ms();
    det.check(make_obs("seg_1", now, 50.0, 0.3));
    let result = det.check(make_obs("seg_1", now + 1000, 70.0, 0.3));
    assert!(!result.flags.contains(&AnomalyFlag::SpeedJump));
}

#[test]
fn detector_no_speed_jump_on_first_reading() {
    let mut det = AnomalyDetector::new(AnomalyConfig::default());
    let result = det.check(make_obs("seg_1", now_ms(), 120.0, 0.3));
    assert!(!result.flags.contains(&AnomalyFlag::SpeedJump));
}

// ─── AnomalyDetector: Check 4 — Phantom Reading ─────────────────────────────

#[test]
fn detector_flags_phantom_reading() {
    let config = AnomalyConfig {
        phantom_repeat_threshold: 3,
        ..Default::default()
    };
    let mut det = AnomalyDetector::new(config);
    let now = now_ms();
    // Send identical readings to trigger phantom
    for i in 0..5 {
        let result = det.check(make_obs("seg_1", now + i * 1000, 50.0, 0.5));
        if i >= 2 {
            assert!(
                result.flags.contains(&AnomalyFlag::PhantomReading),
                "Expected phantom at iteration {i}"
            );
        }
    }
}

#[test]
fn detector_no_phantom_with_varying_readings() {
    let config = AnomalyConfig {
        phantom_repeat_threshold: 5,
        ..Default::default()
    };
    let mut det = AnomalyDetector::new(config);
    let now = now_ms();
    for i in 0..5 {
        let speed = 50.0 + i as f32 * 0.1;
        let result = det.check(make_obs("seg_1", now + i * 1000, speed, 0.3));
        assert!(!result.flags.contains(&AnomalyFlag::PhantomReading));
    }
}

// ─── AnomalyDetector: Check 5 — Z-Score ─────────────────────────────────────

#[test]
fn detector_flags_zscore_outlier() {
    let config = AnomalyConfig {
        min_observations: 5,
        z_score_threshold: 2.0,
        ..Default::default()
    };
    let mut det = AnomalyDetector::new(config);
    let now = now_ms();
    // Build up normal readings
    for i in 0..10 {
        det.check(make_obs("seg_1", now + i * 1000, 50.0 + (i as f32 % 3.0), 0.3));
    }
    // Inject extreme outlier
    let result = det.check(make_obs("seg_1", now + 11_000, 150.0, 0.3));
    assert!(result.flags.contains(&AnomalyFlag::SpeedZScore));
}

#[test]
fn detector_no_zscore_before_min_observations() {
    let config = AnomalyConfig {
        min_observations: 20,
        ..Default::default()
    };
    let mut det = AnomalyDetector::new(config);
    let now = now_ms();
    for i in 0..5 {
        let result = det.check(make_obs("seg_1", now + i * 1000, 150.0, 0.3));
        assert!(!result.flags.contains(&AnomalyFlag::SpeedZScore));
    }
}

// ─── AnomalyDetector: Check 6 — IQR Congestion Outlier ──────────────────────

#[test]
fn detector_flags_congestion_iqr_outlier() {
    let config = AnomalyConfig {
        min_observations: 5,
        iqr_multiplier: 1.5,
        ..Default::default()
    };
    let mut det = AnomalyDetector::new(config);
    let now = now_ms();
    // Build up narrow congestion distribution around 0.3
    for i in 0..15 {
        det.check(make_obs("seg_1", now + i * 1000, 50.0, 0.28 + (i as f32 % 5.0) * 0.01));
    }
    // Inject extreme congestion outlier
    let result = det.check(make_obs("seg_1", now + 16_000, 50.0, 0.99));
    assert!(result.flags.contains(&AnomalyFlag::CongestionOutlier));
}

// ─── AnomalyDetector: Check 7 — Road Class Mismatch ─────────────────────────

#[test]
fn detector_flags_negative_speed() {
    let mut det = AnomalyDetector::new(AnomalyConfig::default());
    let result = det.check(make_obs("seg_1", now_ms(), -5.0, 0.3));
    assert!(result.flags.contains(&AnomalyFlag::RoadClassMismatch));
}

// ─── AnomalyDetector: Check 8 — Spatial Mismatch ────────────────────────────

#[test]
fn detector_flags_invalid_longitude() {
    let mut det = AnomalyDetector::new(AnomalyConfig::default());
    let result = det.check(make_obs_at_coords(999.0, 28.6, 50.0));
    assert!(result.flags.contains(&AnomalyFlag::SpatialMismatch));
}

#[test]
fn detector_flags_invalid_latitude() {
    let mut det = AnomalyDetector::new(AnomalyConfig::default());
    let result = det.check(make_obs_at_coords(77.0, -95.0, 50.0));
    assert!(result.flags.contains(&AnomalyFlag::SpatialMismatch));
}

#[test]
fn detector_allows_valid_coordinates() {
    let mut det = AnomalyDetector::new(AnomalyConfig::default());
    let result = det.check(make_obs_at_coords(77.2090, 28.6139, 50.0));
    assert!(!result.flags.contains(&AnomalyFlag::SpatialMismatch));
}

// ─── Cross-cutting: Data quality tagging ─────────────────────────────────────

#[test]
fn detector_tags_anomalous_quality() {
    let mut det = AnomalyDetector::new(AnomalyConfig::default());
    let future_ts = now_ms() + 120_000;
    let result = det.check(make_obs("seg_1", future_ts, 50.0, 0.3));
    assert_eq!(result.observation.data_quality, DataQuality::Anomalous);
}

#[test]
fn detector_preserves_real_quality_when_clean() {
    let mut det = AnomalyDetector::new(AnomalyConfig::default());
    let result = det.check(make_obs("seg_1", now_ms(), 50.0, 0.3));
    if result.flags.is_empty() {
        assert_eq!(result.observation.data_quality, DataQuality::Real);
    }
}

// ─── Cross-segment isolation ─────────────────────────────────────────────────

#[test]
fn detector_maintains_separate_segment_stats() {
    let mut det = AnomalyDetector::new(AnomalyConfig::default());
    let now = now_ms();
    // Segment A going fast
    det.check(make_obs("seg_A", now, 100.0, 0.2));
    // Segment B going slow
    det.check(make_obs("seg_B", now, 20.0, 0.8));
    // Another reading on seg_A — no speed jump because seg_A's last was 100
    let result = det.check(make_obs("seg_A", now + 1000, 105.0, 0.2));
    assert!(!result.flags.contains(&AnomalyFlag::SpeedJump));
}

// ─── AnomalyConfig ──────────────────────────────────────────────────────────

#[test]
fn config_default_values() {
    let config = AnomalyConfig::default();
    assert!((config.z_score_threshold - 3.0).abs() < f64::EPSILON);
    assert!((config.iqr_multiplier - 1.5).abs() < f64::EPSILON);
    assert!((config.max_speed_jump_kmh - 40.0).abs() < f32::EPSILON);
    assert_eq!(config.stale_threshold_ms, 300_000);
    assert_eq!(config.future_tolerance_ms, 30_000);
    assert!((config.spatial_tolerance_m - 50.0).abs() < f64::EPSILON);
    assert_eq!(config.min_observations, 10);
    assert_eq!(config.phantom_repeat_threshold, 5);
    assert_eq!(config.rolling_window_size, 50);
}
