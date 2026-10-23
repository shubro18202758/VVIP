//! Crowdsource Report Listener — aggregates citizen-reported traffic
//! conditions from public APIs (Google Maps, Waze-like community feeds).
//!
//! In edge-deployment mode, emulates crowdsourced reports on the
//! high-traffic public roads of the seeded Ahmedabad network (trunk and
//! primary segments), with lower accuracy and higher variance than official
//! feeds, exercising the anomaly detector's noise-filtering capabilities.
//! Reports observe the shared city congestion field (city.rs). Rows are
//! labelled `DataQuality::Synthetic`.

use chrono::{DateTime, Utc};
use tokio::sync::mpsc;
use tracing::instrument;

use super::city::{
    CongestionField, RoadClass, clamp_to_network, congestion_for, offset_m, segments,
};
use super::rng::Lcg;
use crate::{DataQuality, FeedSource, IngressError, TrafficObservation};

/// Seconds between report bursts.
pub const CYCLE_SEC: u64 = 60;

/// Relative error of an honest report's speed (1 sigma).
const REPORT_SPEED_ERROR: f64 = 0.10;
/// Phone GPS position error (1 sigma per axis), metres.
const PHONE_GPS_SIGMA_M: f64 = 12.0;
/// The on-road bar: every published report lies within this many metres of
/// the real road geometry of the segment it reports on (WP-20 acceptance).
pub const ON_ROAD_BAR_M: f64 = 30.0;
/// The phone-GPS error is truncated at 2.5 sigma — the on-road bar — by
/// clamping the offset's radius (its direction is kept), less 1 cm so a
/// geodesic re-measure (PostGIS geography) of a report at the cap still
/// lands inside the bar. Below the cap the error stays the full 12 m
/// Gaussian; about 4 % of reports sit on the cap.
pub const PHONE_GPS_MAX_OFFSET_M: f64 = 2.5 * PHONE_GPS_SIGMA_M - 0.01;
/// Share of reports that grossly misstate conditions.
const OUTLIER_SHARE: f64 = 0.05;
/// Most reports per segment per burst.
const MAX_REPORTS: u32 = 4;

/// One phone-GPS position error (east, north) in metres: an isotropic
/// Gaussian of PHONE_GPS_SIGMA_M per axis whose radius is clamped at
/// PHONE_GPS_MAX_OFFSET_M, so the noise looks like a phone's but never
/// carries a report off its road. Two draws, as before the clamp, so the
/// rest of a seeded burst is unchanged.
pub fn phone_gps_error(rng: &mut Lcg) -> (f64, f64) {
    let east = PHONE_GPS_SIGMA_M * rng.gaussian();
    let north = PHONE_GPS_SIGMA_M * rng.gaussian();
    let radius = east.hypot(north);
    if radius > PHONE_GPS_MAX_OFFSET_M {
        let k = PHONE_GPS_MAX_OFFSET_M / radius;
        (east * k, north * k)
    } else {
        (east, north)
    }
}

/// Emulated community traffic reports.
pub struct CrowdFeed {
    field: CongestionField,
    rng: Lcg,
    covered: Vec<usize>,
}

impl CrowdFeed {
    pub fn new(field: CongestionField, seed: u64) -> Self {
        let covered = segments()
            .iter()
            .enumerate()
            .filter(|(_, s)| matches!(s.road_class, RoadClass::Trunk | RoadClass::Primary))
            .map(|(i, _)| i)
            .collect();
        Self {
            field,
            rng: Lcg::new(seed),
            covered,
        }
    }

    /// Segment indices the community reports on.
    pub fn covered(&self) -> &[usize] {
        &self.covered
    }

    /// Poisson draw (Knuth), capped at MAX_REPORTS.
    fn report_count(&mut self, mean: f64) -> u32 {
        let floor = (-mean).exp();
        let (mut k, mut p) = (0u32, 1.0f64);
        loop {
            p *= self.rng.next_f64();
            if p <= floor || k >= MAX_REPORTS {
                return k;
            }
            k += 1;
        }
    }

    /// One burst of reports received at instant `now`.
    pub fn burst(&mut self, now: DateTime<Utc>) -> Vec<TrafficObservation> {
        let mut reports = Vec::new();
        for idx in self.covered.clone() {
            let seg = &segments()[idx];
            // jams draw more reports than free-flowing roads
            let mean = 0.4 + 1.6 * self.field.congestion(seg, now) as f64;
            for _ in 0..self.report_count(mean) {
                // app-reported data arrives 10-60 s late
                let stale_ms = 10_000 + self.rng.below(50_000) as i64;
                let seen_at = now - chrono::Duration::milliseconds(stale_ms);
                let truth = self.field.speed(seg, seen_at);

                let along = self.rng.range_f64(0.05, 0.95) * seg.polyline_length_m();
                let (lon, lat) = seg.point_at(along);
                let (east_m, north_m) = phone_gps_error(&mut self.rng);
                let (lon, lat) = offset_m(lon, lat, east_m, north_m);
                let (lon, lat) = clamp_to_network(lon, lat);

                let (speed, confidence) = if self.rng.chance(OUTLIER_SHARE) {
                    // exaggerated report: claims free flow in a jam or a jam on a clear road
                    let factor = if self.rng.chance(0.5) { 2.5 } else { 0.3 };
                    (truth * factor, 0.3)
                } else {
                    let err = 1.0 + REPORT_SPEED_ERROR * self.rng.gaussian();
                    ((truth as f64 * err) as f32, 0.6)
                };
                let speed = speed.clamp(0.0, 2.6 * seg.speed_limit_kmh);

                reports.push(TrafficObservation {
                    timestamp_ms: seen_at.timestamp_millis(),
                    lon,
                    lat,
                    segment_id: seg.id.to_string(),
                    speed_kmh: speed,
                    congestion_index: congestion_for(seg.speed_limit_kmh, speed),
                    source: FeedSource::Crowdsource,
                    data_quality: DataQuality::Synthetic,
                    confidence,
                });
            }
        }
        reports
    }
}

/// Listens for crowdsourced traffic reports and incident alerts.
///
/// In edge mode, emulates citizen-reported traffic conditions with
/// characteristics typical of crowdsource data:
/// - ±10% speed error vs detector-grade noise for government sensors
/// - phone-GPS position error (12 m sigma, truncated at the 30 m on-road
///   bar: every report stays on the road it reports on)
/// - ~5% exaggerated reports (outliers that should trigger anomaly flags)
/// - Stale reports (10-60 second delay typical of app-reported data)
/// - Sparser coverage (trunk and primary roads only), more reports in jams
///
/// Interval: 60 seconds per burst (crowdsource data arrives in bursts).
#[instrument(name = "crowdsource_listener", skip(tx))]
pub async fn listen_crowdsource_reports(
    tx: mpsc::Sender<TrafficObservation>,
) -> Result<(), IngressError> {
    let mut feed = CrowdFeed::new(
        CongestionField::from_env(),
        Utc::now().timestamp_millis() as u64 ^ 0xCAFE_BABE,
    );
    tracing::info!(
        segments = feed.covered().len(),
        interval_sec = CYCLE_SEC,
        "crowdsource report listener initialized — Ahmedabad trunk/primary roads, edge synthetic mode"
    );

    let mut cycle: u64 = 0;
    loop {
        cycle += 1;
        for obs in feed.burst(Utc::now()) {
            if tx.send(obs).await.is_err() {
                tracing::error!("feed channel closed — shutting down crowdsource listener");
                return Err(IngressError::ChannelSend);
            }
        }

        if cycle.is_multiple_of(5) {
            tracing::debug!(cycle, "crowdsource 5-cycle summary");
        }

        tokio::time::sleep(std::time::Duration::from_secs(CYCLE_SEC)).await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::feeds::city::{DEFAULT_CITY_SEED, ist, segment_by_id};
    use chrono::TimeZone;

    #[test]
    fn reports_cover_public_arterials_on_the_road() {
        let mut feed = CrowdFeed::new(CongestionField::new(DEFAULT_CITY_SEED), 8);
        assert!(feed.covered().len() >= 10);
        // evening peak (report volume grows with congestion)
        let now = ist()
            .with_ymd_and_hms(2026, 10, 22, 19, 0, 0)
            .unwrap()
            .with_timezone(&Utc);
        let mut total = 0;
        let mut outliers = 0;
        for _ in 0..30 {
            for obs in feed.burst(now) {
                total += 1;
                let seg = segment_by_id(obs.segment_id.parse().unwrap()).unwrap();
                assert!(matches!(
                    seg.road_class,
                    RoadClass::Trunk | RoadClass::Primary
                ));
                assert!(seg.distance_m(obs.lon, obs.lat) < ON_ROAD_BAR_M);
                let lag = now.timestamp_millis() - obs.timestamp_ms;
                assert!((10_000..60_000).contains(&lag), "lag {lag}");
                assert_eq!(obs.data_quality, DataQuality::Synthetic);
                if obs.confidence < 0.5 {
                    outliers += 1;
                }
            }
        }
        let share = outliers as f64 / total as f64;
        assert!(total > 300, "only {total} reports in 30 bursts");
        assert!((0.02..0.09).contains(&share), "outlier share {share:.3}");
    }

    #[test]
    fn phone_gps_error_is_truncated_at_the_on_road_bar() {
        // A phone's error, not a squashed one: the 12 m Gaussian below the
        // cap, its radius clamped at 2.5 sigma (direction kept) above it.
        assert!(PHONE_GPS_MAX_OFFSET_M < ON_ROAD_BAR_M);
        assert!(ON_ROAD_BAR_M - PHONE_GPS_MAX_OFFSET_M < 0.05);
        let mut rng = Lcg::new(2_026_10_23);
        let n = 50_000;
        let mut radii = Vec::with_capacity(n);
        let (mut sum_sq, mut capped, mut quadrants) = (0.0f64, 0usize, [0usize; 4]);
        for _ in 0..n {
            let (east, north) = phone_gps_error(&mut rng);
            let r = east.hypot(north);
            assert!(r <= PHONE_GPS_MAX_OFFSET_M + 1e-9, "offset {r:.4} m");
            if r > PHONE_GPS_MAX_OFFSET_M - 1e-9 {
                capped += 1;
            }
            sum_sq += east * east + north * north;
            quadrants[(east >= 0.0) as usize * 2 + (north >= 0.0) as usize] += 1;
            radii.push(r);
        }
        // per-axis RMS of a 12 m Gaussian clamped at 2.5 sigma: ~11.7 m
        let rms = (sum_sq / (2.0 * n as f64)).sqrt();
        assert!((11.3..12.1).contains(&rms), "per-axis rms {rms:.2} m");
        // Rayleigh tail beyond 2.5 sigma: exp(-3.125) = 4.4 % on the cap
        let cap_share = capped as f64 / n as f64;
        assert!((0.035..0.055).contains(&cap_share), "on the cap {cap_share:.4}");
        // the median radius is the Gaussian's own (sigma * sqrt(2 ln 2) = 14.1 m)
        radii.sort_by(f64::total_cmp);
        let median = radii[n / 2];
        assert!((13.6..14.6).contains(&median), "median {median:.2} m");
        // isotropic: every direction as likely
        for q in quadrants {
            let share = q as f64 / n as f64;
            assert!((0.235..0.265).contains(&share), "quadrant share {share:.3}");
        }
    }

    #[test]
    fn every_report_stays_within_the_on_road_bar() {
        // Thousands of reports over the day's three regimes: none leaves the
        // 30 m bar of its own road (the untruncated noise reached 40.5 m),
        // and the noise still spreads out to the cap.
        let (mut total, mut worst, mut sum, mut beyond_20) = (0usize, 0.0f64, 0.0f64, 0usize);
        for (seed, hour) in [(31u64, 19u32), (32, 10), (33, 14), (34, 3)] {
            let mut feed = CrowdFeed::new(CongestionField::new(DEFAULT_CITY_SEED), seed);
            let now = ist()
                .with_ymd_and_hms(2026, 10, 22, hour, 0, 0)
                .unwrap()
                .with_timezone(&Utc);
            for burst in 0..60 {
                for obs in feed.burst(now + chrono::Duration::seconds(60 * burst)) {
                    let seg = segment_by_id(obs.segment_id.parse().unwrap()).unwrap();
                    let d = seg.distance_m(obs.lon, obs.lat);
                    assert!(d < ON_ROAD_BAR_M, "segment {}: {d:.3} m off its road", seg.id);
                    total += 1;
                    sum += d;
                    worst = worst.max(d);
                    beyond_20 += (d > 20.0) as usize;
                }
            }
        }
        let mean = sum / total as f64;
        println!("crowd positions: {total} reports, mean {mean:.2} m, worst {worst:.2} m, >20 m {beyond_20}");
        assert!(total > 2_000, "only {total} reports");
        assert!(worst > 27.0, "noise squashed: worst {worst:.2} m");
        assert!((7.0..11.0).contains(&mean), "mean offset {mean:.2} m");
        assert!(beyond_20 > 0);
    }
}
