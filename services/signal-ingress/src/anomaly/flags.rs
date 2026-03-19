use crate::TrafficObservation;

/// Individual anomaly flag types detected by the Tier-1 inline detector.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AnomalyFlag {
    /// Observation timestamp is in the future beyond clock-skew tolerance
    FutureTimestamp,
    /// Observation is older than the stale threshold
    StaleData,
    /// Speed changed too abruptly between consecutive readings
    SpeedJump,
    /// Identical reading repeated suspiciously many times (sensor stuck)
    PhantomReading,
    /// Speed deviates beyond z-score threshold from segment mean
    SpeedZScore,
    /// Congestion index is an IQR outlier
    CongestionOutlier,
    /// Speed is implausible for the road class (e.g. 120 km/h on residential)
    RoadClassMismatch,
    /// Observation coordinates too far from the claimed segment
    SpatialMismatch,
}

/// Severity classification for anomaly flags.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Severity {
    Low,
    Medium,
    High,
}

impl AnomalyFlag {
    pub fn severity(self) -> Severity {
        match self {
            Self::StaleData | Self::RoadClassMismatch => Severity::Low,
            Self::SpeedZScore | Self::CongestionOutlier | Self::SpeedJump => Severity::Medium,
            Self::FutureTimestamp | Self::PhantomReading | Self::SpatialMismatch => Severity::High,
        }
    }

    pub fn name(self) -> &'static str {
        match self {
            Self::FutureTimestamp => "future_timestamp",
            Self::StaleData => "stale_data",
            Self::SpeedJump => "speed_jump",
            Self::PhantomReading => "phantom_reading",
            Self::SpeedZScore => "speed_zscore",
            Self::CongestionOutlier => "congestion_outlier",
            Self::RoadClassMismatch => "road_class_mismatch",
            Self::SpatialMismatch => "spatial_mismatch",
        }
    }
}

/// Result of running an observation through the 8-check anomaly detector.
pub struct AnomalyResult {
    pub observation: TrafficObservation,
    pub flags: Vec<AnomalyFlag>,
    pub is_anomalous: bool,
    pub confidence: f32,
}

impl AnomalyResult {
    /// Compute confidence based on accumulated flag severities.
    /// Confidence = 1.0 - (0.15 * low + 0.30 * medium + 0.50 * high)
    pub fn compute(observation: TrafficObservation, flags: Vec<AnomalyFlag>) -> Self {
        let (low, med, high) = flags.iter().fold((0u32, 0u32, 0u32), |(l, m, h), f| {
            match f.severity() {
                Severity::Low => (l + 1, m, h),
                Severity::Medium => (l, m + 1, h),
                Severity::High => (l, m, h + 1),
            }
        });

        let penalty = 0.15 * low as f32 + 0.30 * med as f32 + 0.50 * high as f32;
        let confidence = (1.0 - penalty).max(0.0);
        let is_anomalous = !flags.is_empty();

        Self {
            observation,
            flags,
            is_anomalous,
            confidence,
        }
    }
}
