use std::collections::hash_map::DefaultHasher;
use std::collections::VecDeque;
use std::hash::{Hash, Hasher};

/// Per-segment rolling statistics for anomaly detection.
/// Maintains a fixed-size window of recent speed and congestion values.
pub struct SegmentStats {
    speeds: VecDeque<f32>,
    congestions: VecDeque<f32>,
    window_size: usize,
    pub last_timestamp_ms: i64,
    pub last_speed_kmh: f32,
    last_reading_hash: u64,
    pub repeat_count: u32,
}

impl SegmentStats {
    pub fn new(window_size: usize) -> Self {
        Self {
            speeds: VecDeque::with_capacity(window_size),
            congestions: VecDeque::with_capacity(window_size),
            window_size,
            last_timestamp_ms: 0,
            last_speed_kmh: 0.0,
            last_reading_hash: 0,
            repeat_count: 0,
        }
    }

    /// Push new speed and congestion values, maintaining window bounds.
    pub fn push(&mut self, speed: f32, congestion: f32, timestamp_ms: i64) {
        if self.speeds.len() >= self.window_size {
            self.speeds.pop_front();
        }
        if self.congestions.len() >= self.window_size {
            self.congestions.pop_front();
        }
        self.speeds.push_back(speed);
        self.congestions.push_back(congestion);

        // Track repeated readings (phantom detection)
        let hash = Self::hash_reading(speed, congestion);
        if hash == self.last_reading_hash {
            self.repeat_count += 1;
        } else {
            self.repeat_count = 1;
            self.last_reading_hash = hash;
        }

        self.last_speed_kmh = speed;
        self.last_timestamp_ms = timestamp_ms;
    }

    pub fn count(&self) -> usize {
        self.speeds.len()
    }

    // ── Speed statistics ────────────────────────────────────────────

    pub fn speed_mean(&self) -> f64 {
        if self.speeds.is_empty() {
            return 0.0;
        }
        let sum: f64 = self.speeds.iter().map(|&v| v as f64).sum();
        sum / self.speeds.len() as f64
    }

    pub fn speed_std_dev(&self) -> f64 {
        if self.speeds.len() < 2 {
            return 0.0;
        }
        let mean = self.speed_mean();
        let variance: f64 = self
            .speeds
            .iter()
            .map(|&v| {
                let diff = v as f64 - mean;
                diff * diff
            })
            .sum::<f64>()
            / (self.speeds.len() - 1) as f64;
        variance.sqrt()
    }

    // ── Congestion statistics ───────────────────────────────────────

    pub fn congestion_median(&self) -> f64 {
        Self::median_of(&self.congestions)
    }

    pub fn congestion_iqr(&self) -> (f64, f64) {
        Self::iqr_of(&self.congestions)
    }

    pub fn congestion_mad(&self) -> f64 {
        Self::mad_of(&self.congestions)
    }

    // ── Helpers ─────────────────────────────────────────────────────

    fn median_of(data: &VecDeque<f32>) -> f64 {
        if data.is_empty() {
            return 0.0;
        }
        let mut sorted: Vec<f32> = data.iter().copied().collect();
        sorted.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        let mid = sorted.len() / 2;
        if sorted.len() % 2 == 0 {
            (sorted[mid - 1] as f64 + sorted[mid] as f64) / 2.0
        } else {
            sorted[mid] as f64
        }
    }

    fn iqr_of(data: &VecDeque<f32>) -> (f64, f64) {
        if data.len() < 4 {
            return (0.0, 1.0);
        }
        let mut sorted: Vec<f32> = data.iter().copied().collect();
        sorted.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        let n = sorted.len();
        let q1 = sorted[n / 4] as f64;
        let q3 = sorted[3 * n / 4] as f64;
        (q1, q3)
    }

    fn mad_of(data: &VecDeque<f32>) -> f64 {
        if data.is_empty() {
            return 0.0;
        }
        let median = Self::median_of(data);
        let mut deviations: Vec<f64> = data.iter().map(|&v| (v as f64 - median).abs()).collect();
        deviations.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        let mid = deviations.len() / 2;
        if deviations.len() % 2 == 0 && deviations.len() >= 2 {
            (deviations[mid - 1] + deviations[mid]) / 2.0
        } else {
            deviations[mid]
        }
    }

    fn hash_reading(speed: f32, congestion: f32) -> u64 {
        let mut hasher = DefaultHasher::new();
        speed.to_bits().hash(&mut hasher);
        congestion.to_bits().hash(&mut hasher);
        hasher.finish()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_mean_and_std_dev() {
        let mut stats = SegmentStats::new(10);
        for &speed in &[40.0, 42.0, 38.0, 41.0, 39.0] {
            stats.push(speed, 0.3, 1000);
        }
        let mean = stats.speed_mean();
        assert!((mean - 40.0).abs() < 0.01);
        let std = stats.speed_std_dev();
        assert!(std > 1.0 && std < 2.0);
    }

    #[test]
    fn test_median() {
        let mut stats = SegmentStats::new(10);
        for &c in &[0.1, 0.5, 0.3, 0.7, 0.2] {
            stats.push(50.0, c, 1000);
        }
        let median = stats.congestion_median();
        assert!((median - 0.3).abs() < 0.001);
    }

    #[test]
    fn test_iqr() {
        let mut stats = SegmentStats::new(20);
        for i in 0..20 {
            stats.push(50.0, i as f32 / 20.0, 1000);
        }
        let (q1, q3) = stats.congestion_iqr();
        assert!(q1 < q3);
    }

    #[test]
    fn test_repeat_count() {
        let mut stats = SegmentStats::new(10);
        stats.push(50.0, 0.5, 1000);
        stats.push(50.0, 0.5, 2000);
        stats.push(50.0, 0.5, 3000);
        assert_eq!(stats.repeat_count, 3);
        stats.push(51.0, 0.5, 4000);
        assert_eq!(stats.repeat_count, 1);
    }

    #[test]
    fn test_window_eviction() {
        let mut stats = SegmentStats::new(3);
        stats.push(10.0, 0.1, 1000);
        stats.push(20.0, 0.2, 2000);
        stats.push(30.0, 0.3, 3000);
        assert_eq!(stats.count(), 3);
        stats.push(40.0, 0.4, 4000);
        assert_eq!(stats.count(), 3);
        // Oldest (10.0) should be evicted, mean should be (20+30+40)/3 = 30
        assert!((stats.speed_mean() - 30.0).abs() < 0.01);
    }
}
