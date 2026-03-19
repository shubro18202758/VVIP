/// Configuration thresholds for the Tier-1 inline anomaly detector.
/// Loaded from `models/configs/anomaly_detector.yaml` at startup.
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

    /// Number of identical consecutive readings to flag as phantom
    #[serde(default = "default_phantom_repeat")]
    pub phantom_repeat_threshold: u32,

    /// Rolling window size for per-segment statistics
    #[serde(default = "default_rolling_window")]
    pub rolling_window_size: usize,
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
            phantom_repeat_threshold: default_phantom_repeat(),
            rolling_window_size: default_rolling_window(),
        }
    }
}

impl AnomalyConfig {
    /// Load from a YAML file path.
    pub fn from_yaml(path: &str) -> Result<Self, crate::IngressError> {
        let contents = std::fs::read_to_string(path).map_err(|e| {
            crate::IngressError::AnomalyConfig {
                msg: format!("failed to read config {path}: {e}"),
            }
        })?;
        serde_yaml::from_str(&contents).map_err(|e| crate::IngressError::AnomalyConfig {
            msg: format!("failed to parse config {path}: {e}"),
        })
    }
}
