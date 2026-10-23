use std::collections::HashMap;

use crate::FeedSource;

/// Configuration thresholds for the Tier-1 inline anomaly detector.
/// Loaded at startup by [`AnomalyConfig::load`] from `$ANOMALY_CONFIG_PATH`,
/// the image copy at [`IMAGE_CONFIG_PATH`], or the copy of
/// `config/anomaly_detector.yaml` embedded at build time.
#[derive(Debug, Clone, serde::Deserialize)]
pub struct AnomalyConfig {
    /// Z-score threshold for speed outlier detection
    #[serde(default = "default_z_score")]
    pub z_score_threshold: f64,

    /// IQR multiplier for box-plot outlier detection
    #[serde(default = "default_iqr")]
    pub iqr_multiplier: f64,

    /// Maximum plausible speed jump between consecutive readings (km/h)
    #[serde(default = "default_max_speed_jump")]
    pub max_speed_jump_kmh: f32,

    /// Observation older than this is considered stale (milliseconds)
    #[serde(default = "default_stale_threshold")]
    pub stale_threshold_ms: i64,

    /// Tolerance for future timestamps (clock skew allowance, milliseconds)
    #[serde(default = "default_future_tolerance")]
    pub future_tolerance_ms: i64,

    /// Spatial mismatch tolerance — max distance from segment centroid (meters)
    #[serde(default = "default_spatial_tolerance")]
    pub spatial_tolerance_m: f64,

    /// Minimum observations before statistical checks activate
    #[serde(default = "default_min_observations")]
    pub min_observations: usize,

    /// Per-source `min_observations` for sources too sparse to collect that
    /// many readings per segment within `max_window_age_ms` (the mapping
    /// provider's 5-minute snapshots)
    #[serde(default = "default_source_min_observations")]
    pub source_min_observations: HashMap<FeedSource, usize>,

    /// Number of identical consecutive readings to flag as phantom
    #[serde(default = "default_phantom_repeat")]
    pub phantom_repeat_threshold: u32,

    /// Rolling window size for per-segment statistics
    #[serde(default = "default_rolling_window")]
    pub rolling_window_size: usize,

    /// Samples older than this (relative to the newest) leave the rolling
    /// window, so readings are judged against current conditions (ms)
    #[serde(default = "default_max_window_age")]
    pub max_window_age_ms: i64,

    /// Floor on the speed standard deviation used by the z-score check (km/h)
    #[serde(default = "default_min_speed_std")]
    pub min_speed_std_kmh: f64,

    /// Floor on the congestion IQR used by the box-plot check
    #[serde(default = "default_min_congestion_iqr")]
    pub min_congestion_iqr: f64,
}

fn default_z_score() -> f64 { 3.0 }
fn default_iqr() -> f64 { 1.5 }
fn default_max_speed_jump() -> f32 { 40.0 }
fn default_stale_threshold() -> i64 { 300_000 }
fn default_future_tolerance() -> i64 { 30_000 }
fn default_spatial_tolerance() -> f64 { 50.0 }
fn default_min_observations() -> usize { 10 }
fn default_phantom_repeat() -> u32 { 5 }
fn default_rolling_window() -> usize { 50 }
fn default_max_window_age() -> i64 { 1_800_000 }
fn default_min_speed_std() -> f64 { 4.0 }
fn default_min_congestion_iqr() -> f64 { 0.12 }

fn default_source_min_observations() -> HashMap<FeedSource, usize> {
    // a 30-minute window holds at most 7 snapshots of the 5-minute mapping
    // feed (feeds::mapping_api); 6 leaves one for a late refresh
    HashMap::from([(FeedSource::MappingApi, 6)])
}

impl Default for AnomalyConfig {
    fn default() -> Self {
        Self {
            z_score_threshold: default_z_score(),
            iqr_multiplier: default_iqr(),
            max_speed_jump_kmh: default_max_speed_jump(),
            stale_threshold_ms: default_stale_threshold(),
            future_tolerance_ms: default_future_tolerance(),
            spatial_tolerance_m: default_spatial_tolerance(),
            min_observations: default_min_observations(),
            source_min_observations: default_source_min_observations(),
            phantom_repeat_threshold: default_phantom_repeat(),
            rolling_window_size: default_rolling_window(),
            max_window_age_ms: default_max_window_age(),
            min_speed_std_kmh: default_min_speed_std(),
            min_congestion_iqr: default_min_congestion_iqr(),
        }
    }
}

/// Env override for the threshold file.
pub const CONFIG_PATH_ENV: &str = "ANOMALY_CONFIG_PATH";

/// Where Dockerfile.signal-ingress installs `config/anomaly_detector.yaml`.
pub const IMAGE_CONFIG_PATH: &str = "/etc/signal-ingress/anomaly_detector.yaml";

/// `config/anomaly_detector.yaml`, compiled in so the service always has
/// its thresholds, whatever the working directory or mounts.
pub const EMBEDDED_YAML: &str = include_str!("../../config/anomaly_detector.yaml");

impl AnomalyConfig {
    /// Load from a YAML file path.
    pub fn from_yaml(path: &str) -> Result<Self, crate::IngressError> {
        let contents = std::fs::read_to_string(path).map_err(|e| {
            crate::IngressError::AnomalyConfig {
                msg: format!("failed to read config {path}: {e}"),
            }
        })?;
        Self::from_yaml_str(&contents).map_err(|e| crate::IngressError::AnomalyConfig {
            msg: format!("failed to parse config {path}: {e}"),
        })
    }

    pub fn from_yaml_str(contents: &str) -> Result<Self, serde_yaml::Error> {
        serde_yaml::from_str(contents)
    }

    /// The thresholds compiled into the binary.
    pub fn embedded() -> Self {
        Self::from_yaml_str(EMBEDDED_YAML).expect("embedded anomaly_detector.yaml is valid")
    }

    /// Resolve the thresholds: `$ANOMALY_CONFIG_PATH`, then the image copy at
    /// [`IMAGE_CONFIG_PATH`], then the embedded file. Returns the config and
    /// where it came from. Only an explicitly configured path that cannot be
    /// read is worth a warning; absent optional files fall through silently.
    pub fn load() -> (Self, String) {
        if let Ok(path) = std::env::var(CONFIG_PATH_ENV)
            && !path.trim().is_empty()
        {
            match Self::from_yaml(&path) {
                Ok(cfg) => return (cfg, path),
                Err(e) => {
                    tracing::warn!(%e, "{CONFIG_PATH_ENV} unusable — using built-in thresholds")
                }
            }
        }
        if std::path::Path::new(IMAGE_CONFIG_PATH).is_file() {
            match Self::from_yaml(IMAGE_CONFIG_PATH) {
                Ok(cfg) => return (cfg, IMAGE_CONFIG_PATH.to_string()),
                Err(e) => {
                    tracing::warn!(%e, "image anomaly config unusable — using built-in thresholds")
                }
            }
        }
        (
            Self::embedded(),
            "built-in (config/anomaly_detector.yaml)".to_string(),
        )
    }

    /// Readings a segment's window of `source` needs before the statistical
    /// checks (speed z-score, congestion IQR) judge that source's readings.
    pub fn min_observations_for(&self, source: FeedSource) -> usize {
        self.source_min_observations
            .get(&source)
            .copied()
            .unwrap_or(self.min_observations)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn embedded_thresholds_parse_and_match_the_defaults() {
        let cfg = AnomalyConfig::embedded();
        let d = AnomalyConfig::default();
        assert_eq!(cfg.z_score_threshold, d.z_score_threshold);
        assert_eq!(cfg.stale_threshold_ms, d.stale_threshold_ms);
        assert_eq!(cfg.phantom_repeat_threshold, d.phantom_repeat_threshold);
        assert_eq!(cfg.rolling_window_size, d.rolling_window_size);
        assert_eq!(cfg.min_observations, d.min_observations);
        assert_eq!(cfg.source_min_observations, d.source_min_observations);
        assert_eq!(cfg.max_window_age_ms, d.max_window_age_ms);
        assert_eq!(cfg.min_speed_std_kmh, d.min_speed_std_kmh);
        assert_eq!(cfg.min_congestion_iqr, d.min_congestion_iqr);
        assert!(EMBEDDED_YAML.contains("Ahmedabad"));
    }

    #[test]
    fn the_mapping_window_can_reach_its_min_observations() {
        // When a reading is checked, its window holds the source's earlier
        // readings within max_window_age_ms of the newest one. The sparsest
        // source is the mapping provider: one snapshot per segment per
        // refresh. Required: its min_observations, plus one for a late refresh.
        let cfg = AnomalyConfig::embedded();
        let refresh_ms = crate::feeds::mapping_api::REFRESH_SEC as i64 * 1000;
        let held = (cfg.max_window_age_ms / refresh_ms + 1) as usize;
        let needed = cfg.min_observations_for(FeedSource::MappingApi);
        assert!(
            held > needed,
            "a mapping window holds {held} snapshots, {needed} are needed"
        );
        // denser sources keep the general threshold
        assert_eq!(
            cfg.min_observations_for(FeedSource::FleetGps),
            cfg.min_observations
        );
    }

    #[test]
    fn source_min_observations_are_keyed_by_source_name() {
        let cfg =
            AnomalyConfig::from_yaml_str("source_min_observations:\n  Crowdsource: 4\n").unwrap();
        assert_eq!(cfg.min_observations_for(FeedSource::Crowdsource), 4);
        // an explicit map replaces the default one
        assert_eq!(
            cfg.min_observations_for(FeedSource::MappingApi),
            cfg.min_observations
        );
    }
}
