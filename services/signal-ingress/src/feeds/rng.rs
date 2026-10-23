//! Deterministic pseudo-random source shared by the edge-synthetic feeds.
//!
//! A 64-bit LCG (Knuth MMIX constants) whose floats come from the TOP 53
//! bits: `t = (state >> 11) / 2^53`, uniform in [0, 1). The earlier per-feed
//! helpers used `(state >> 33) / u32::MAX`, i.e. a 31-bit value over a 32-bit
//! denominator, which only spans [0, 0.5): every "symmetric" noise term was
//! one-sided, fleet speeds random-walked down to the 0.2×limit clamp and the
//! phantom-reading check flagged ~84% of the live rows. Integer draws also
//! use the high bits — an LCG's low bits have short periods (bit 0 simply
//! alternates), so `state % n` is not a fair die.

/// 2^53 — the number of distinct f64 values in [0, 1) at full mantissa precision.
const TWO_POW_53: f64 = (1u64 << 53) as f64;

#[derive(Debug, Clone)]
pub struct Lcg {
    state: u64,
}

impl Lcg {
    /// Seeds through SplitMix64 so nearby seeds (e.g. consecutive
    /// millisecond timestamps) start from unrelated states.
    pub fn new(seed: u64) -> Self {
        Self {
            state: splitmix64(seed),
        }
    }

    pub fn next_u64(&mut self) -> u64 {
        self.state = self
            .state
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        self.state
    }

    /// Uniform in [0, 1) from the top 53 bits of the state.
    pub fn next_f64(&mut self) -> f64 {
        (self.next_u64() >> 11) as f64 / TWO_POW_53
    }

    /// Uniform in [min, max).
    pub fn range_f64(&mut self, min: f64, max: f64) -> f64 {
        min + self.next_f64() * (max - min)
    }

    /// Uniform in [min, max).
    pub fn range_f32(&mut self, min: f32, max: f32) -> f32 {
        self.range_f64(min as f64, max as f64) as f32
    }

    /// Uniform integer in [0, n) from the high 32 bits (n > 0).
    pub fn below(&mut self, n: u64) -> u64 {
        ((self.next_u64() >> 32) * n) >> 32
    }

    /// True with probability `p`.
    pub fn chance(&mut self, p: f64) -> bool {
        self.next_f64() < p
    }

    /// Standard normal draw (Box–Muller).
    pub fn gaussian(&mut self) -> f64 {
        // 1 - u keeps the log argument in (0, 1].
        let u1 = 1.0 - self.next_f64();
        let u2 = self.next_f64();
        (-2.0 * u1.ln()).sqrt() * (std::f64::consts::TAU * u2).cos()
    }
}

/// SplitMix64 finaliser — also used as a stateless hash for value noise.
pub fn splitmix64(x: u64) -> u64 {
    let mut z = x.wrapping_add(0x9E37_79B9_7F4A_7C15);
    z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
    z ^ (z >> 31)
}

/// Stateless hash → uniform in [0, 1).
pub fn hash_unit(x: u64) -> f64 {
    (splitmix64(x) >> 11) as f64 / TWO_POW_53
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn floats_are_uniform_over_the_whole_unit_interval() {
        let mut rng = Lcg::new(0xDEAD_BEEF);
        let n = 100_000;
        let (mut sum, mut min, mut max) = (0.0, 1.0f64, 0.0f64);
        for _ in 0..n {
            let t = rng.next_f64();
            assert!((0.0..1.0).contains(&t));
            sum += t;
            min = min.min(t);
            max = max.max(t);
        }
        let mean = sum / n as f64;
        assert!((mean - 0.5).abs() < 0.01, "mean {mean}");
        assert!(max > 0.99, "max {max}");
        assert!(min < 0.01, "min {min}");
    }

    #[test]
    fn symmetric_ranges_are_symmetric() {
        let mut rng = Lcg::new(42);
        let n = 50_000;
        let draws: Vec<f32> = (0..n).map(|_| rng.range_f32(-3.0, 3.0)).collect();
        let mean = draws.iter().map(|&v| v as f64).sum::<f64>() / n as f64;
        let positive = draws.iter().filter(|&&v| v > 0.0).count() as f64 / n as f64;
        assert!(mean.abs() < 0.05, "mean {mean}");
        assert!((positive - 0.5).abs() < 0.02, "positive share {positive}");
    }

    #[test]
    fn integer_draws_cover_every_bucket_evenly() {
        let mut rng = Lcg::new(7);
        let mut buckets = [0u32; 20];
        for _ in 0..40_000 {
            buckets[rng.below(20) as usize] += 1;
        }
        for (i, &c) in buckets.iter().enumerate() {
            assert!((1_700..=2_300).contains(&c), "bucket {i} has {c}");
        }
    }

    #[test]
    fn gaussian_has_unit_moments() {
        let mut rng = Lcg::new(99);
        let n = 50_000;
        let draws: Vec<f64> = (0..n).map(|_| rng.gaussian()).collect();
        let mean = draws.iter().sum::<f64>() / n as f64;
        let var = draws.iter().map(|v| (v - mean) * (v - mean)).sum::<f64>() / n as f64;
        assert!(mean.abs() < 0.02, "mean {mean}");
        assert!((var - 1.0).abs() < 0.03, "var {var}");
    }

    #[test]
    fn nearby_seeds_diverge() {
        let a = Lcg::new(1_792_000_000_000).next_f64();
        let b = Lcg::new(1_792_000_000_001).next_f64();
        assert!((a - b).abs() > 1e-3);
    }
}
