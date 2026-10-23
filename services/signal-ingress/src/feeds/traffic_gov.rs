//! Government Traffic Feed Aggregator — ingests official traffic data
//! from Indian government ITS feeds (NTCIP, ITMS endpoints).
//!
//! In edge-deployment mode (no live API access), emulates the ITMS detector
//! stations of the seeded Ahmedabad network: one fixed station at the
//! midpoint of every corridor segment (segments_ahmedabad.rs), reporting
//! the shared city congestion field (city.rs: IST time-of-day demand with
//! Ahmedabad's late-morning and evening peaks) plus detector noise.
//!
//! Stations report on a 15 s cycle, phase-staggered across the cycle like
//! unsynchronised roadside units, so every segment reports at least once in
//! any 15 s window instead of all 40 arriving in one burst. Rows are
//! labelled `DataQuality::Synthetic`.

use chrono::{DateTime, Utc};
use tokio::sync::mpsc;
use tracing::instrument;

use super::city::{CongestionField, Segment, segments, speed_for};
use super::rng::Lcg;
use crate::{DataQuality, FeedSource, IngressError, TrafficObservation};

/// Reporting cycle of every detector station, seconds.
pub const CYCLE_SEC: i64 = 15;

/// Detector measurement noise on the congestion index (1 sigma).
const DETECTOR_NOISE: f64 = 0.025;

/// Emulated ITMS detector network over the Ahmedabad corridor.
pub struct GovernmentFeed {
    field: CongestionField,
    rng: Lcg,
}

impl GovernmentFeed {
    pub fn new(field: CongestionField, seed: u64) -> Self {
        Self {
            field,
            rng: Lcg::new(seed),
        }
    }

    /// Second of the 15 s cycle at which segment `index` reports.
    pub fn phase_of(index: usize) -> i64 {
        (index as i64 * CYCLE_SEC) / segments().len() as i64
    }

    /// One station reading for `seg` at instant `t`.
    pub fn observe(&mut self, seg: &Segment, t: DateTime<Utc>) -> TrafficObservation {
        let truth = self.field.congestion(seg, t) as f64;
        let congestion = (truth + DETECTOR_NOISE * self.rng.gaussian()).clamp(0.0, 1.0) as f32;
        TrafficObservation {
            timestamp_ms: t.timestamp_millis(),
            lon: seg.mid.0,
            lat: seg.mid.1,
            segment_id: seg.id.to_string(),
            speed_kmh: speed_for(seg.speed_limit_kmh, congestion).max(3.0),
            congestion_index: congestion,
            source: FeedSource::GovernmentTraffic,
            data_quality: DataQuality::Synthetic,
            confidence: 1.0,
        }
    }

    /// Readings of every station whose phase falls in the whole second
    /// `epoch_sec` (2-3 stations per second for 40 segments).
    pub fn readings_for_second(&mut self, epoch_sec: i64) -> Vec<TrafficObservation> {
        let slot = epoch_sec.rem_euclid(CYCLE_SEC);
        let t = DateTime::<Utc>::from_timestamp(epoch_sec, 0).unwrap_or_else(Utc::now);
        segments()
            .iter()
            .enumerate()
            .filter(|(i, _)| Self::phase_of(*i) == slot)
            .map(|(_, seg)| self.observe(seg, t))
            .collect()
    }
}

/// Polls government traffic management systems for real-time corridor conditions.
///
/// In edge-deployment mode, emits the staggered ITMS station readings for all
/// Ahmedabad corridor segments (each station every 15 s).
#[instrument(name = "gov_traffic_poller", skip(tx))]
pub async fn poll_government_feeds(
    tx: mpsc::Sender<TrafficObservation>,
) -> Result<(), IngressError> {
    tracing::info!(
        segment_count = segments().len(),
        cycle_sec = CYCLE_SEC,
        "government traffic feed poller initialized — Ahmedabad ITMS stations, edge synthetic mode"
    );

    let mut feed = GovernmentFeed::new(
        CongestionField::from_env(),
        Utc::now().timestamp_millis() as u64,
    );
    let mut ticker = tokio::time::interval(std::time::Duration::from_secs(1));
    ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    let mut last_sec = Utc::now().timestamp() - 1;
    let mut cycles: u64 = 0;

    loop {
        ticker.tick().await;
        let now_sec = Utc::now().timestamp();
        // Catch up on any whole second a delayed tick skipped, so no station
        // misses its slot (bounded to one cycle after a long stall).
        for sec in (last_sec + 1).max(now_sec - CYCLE_SEC + 1)..=now_sec {
            for obs in feed.readings_for_second(sec) {
                if tx.send(obs).await.is_err() {
                    tracing::error!("feed channel closed — shutting down government poller");
                    return Err(IngressError::ChannelSend);
                }
            }
            if sec.rem_euclid(CYCLE_SEC) == CYCLE_SEC - 1 {
                cycles += 1;
                if cycles.is_multiple_of(20) {
                    tracing::debug!(cycles, "government feed: 20 full station cycles complete");
                }
            }
        }
        last_sec = now_sec.max(last_sec);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::feeds::city::{DEFAULT_CITY_SEED, congestion_for};

    #[test]
    fn every_station_reports_once_per_cycle() {
        let mut feed = GovernmentFeed::new(CongestionField::new(DEFAULT_CITY_SEED), 1);
        let start = 1_792_670_400; // a whole 15 s boundary
        let mut seen = std::collections::HashMap::new();
        for sec in start..start + CYCLE_SEC {
            for obs in feed.readings_for_second(sec) {
                *seen.entry(obs.segment_id.clone()).or_insert(0) += 1;
            }
        }
        assert_eq!(seen.len(), segments().len());
        assert!(seen.values().all(|&n| n == 1));
        // stations are spread evenly over the cycle (no whole-network burst)
        let per_second = segments().len().div_ceil(CYCLE_SEC as usize);
        for sec in start..start + CYCLE_SEC {
            assert!(feed.readings_for_second(sec).len() <= per_second);
        }
    }

    #[test]
    fn readings_sit_on_the_segment_midpoint_and_are_consistent() {
        let mut feed = GovernmentFeed::new(CongestionField::new(DEFAULT_CITY_SEED), 2);
        let t = Utc::now();
        for seg in segments() {
            let obs = feed.observe(seg, t);
            assert_eq!((obs.lon, obs.lat), seg.mid);
            assert_eq!(obs.data_quality, DataQuality::Synthetic);
            let implied = congestion_for(seg.speed_limit_kmh, obs.speed_kmh);
            assert!((implied - obs.congestion_index).abs() < 0.02 || obs.speed_kmh <= 3.0);
        }
    }
}
