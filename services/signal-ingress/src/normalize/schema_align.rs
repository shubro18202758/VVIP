//! Schema Alignment Pipeline — normalizes heterogeneous traffic feed data
//! into a canonical Arrow-compatible columnar format for zero-copy IPC.
//!
//! Data flow: feed_rx → anomaly detection → MicroBatcher → batch_tx
//! Flagged anomalies are also published separately to NATS `corridor.traffic.anomaly`
//! as an [`AnomalyEvent`] (contract C-H).

use arrow::record_batch::RecordBatch;
use tokio::sync::mpsc;
use tracing::instrument;

use crate::anomaly::config::AnomalyConfig;
use crate::anomaly::detector::AnomalyDetector;
use crate::anomaly::flags::AnomalyResult;
use crate::pipeline::batch::MicroBatcher;
use crate::{DataQuality, IngressError, TrafficObservation};

/// NATS subject for Tier-1 anomaly events.
pub const SUBJECT_ANOMALY: &str = "corridor.traffic.anomaly";

/// Flush timeout for the micro-batcher when observation flow is slow.
const BATCH_FLUSH_TIMEOUT_MS: u64 = 100;

/// Default flush count for micro-batcher.
const BATCH_FLUSH_COUNT: usize = 100;

/// JSON document published on `corridor.traffic.anomaly` (contract C-H).
///
/// The observation's own fields stay at the top level exactly as before
/// (`segment_id` string, `timestamp_ms`, `lon`, `lat`, `speed_kmh`,
/// `congestion_index`, `source`, `data_quality`, `confidence`), plus:
/// * `anomaly_flags` — names from the traffic.anomaly_log anomaly_type
///   vocabulary {future_timestamp, stale_data, speed_jump, phantom_reading,
///   speed_zscore, congestion_outlier, road_class_mismatch, spatial_mismatch};
/// * `severity` — the highest flag severity, "low" | "medium" | "high"
///   (the anomaly_log severity vocabulary);
/// * `feed_quality` (additive) — the reading's data quality BEFORE the
///   detector relabelled it `Anomalous` ("Synthetic" on the simulated
///   feeds, "Real" on a live connector), so a flag raised on simulated data
///   is never mistaken for a field anomaly.  Omitted when unknown; consumers
///   must tolerate its absence.
#[derive(Debug, serde::Serialize)]
pub struct AnomalyEvent<'a> {
    #[serde(flatten)]
    pub observation: &'a TrafficObservation,
    pub anomaly_flags: Vec<&'static str>,
    pub severity: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub feed_quality: Option<DataQuality>,
}

impl<'a> AnomalyEvent<'a> {
    /// Event for a flagged result; None when the observation is clean.
    pub fn from_result(result: &'a AnomalyResult) -> Option<Self> {
        let severity = result.severity()?;
        Some(Self {
            observation: &result.observation,
            anomaly_flags: result.flag_names(),
            severity: severity.as_str(),
            feed_quality: result.feed_quality,
        })
    }
}

/// Runs the detector on one observation and folds its verdict into the
/// observation: the published confidence is the feed's own confidence
/// scaled by the detector's flag-based confidence, so a clean crowd report
/// stays at crowd-grade confidence instead of being promoted to 1.0.
pub fn assess(detector: &mut AnomalyDetector, obs: TrafficObservation) -> AnomalyResult {
    assess_at(detector, obs, chrono::Utc::now().timestamp_millis())
}

/// [`assess`] against an explicit reference clock (epoch ms).
pub fn assess_at(
    detector: &mut AnomalyDetector,
    obs: TrafficObservation,
    now_ms: i64,
) -> AnomalyResult {
    let mut result = detector.check_at(obs, now_ms);
    result.observation.confidence =
        (result.observation.confidence * result.confidence).clamp(0.0, 1.0);
    result
}

/// Runs the continuous normalization pipeline.
///
/// Receives raw observations from all feed collectors via `feed_rx`,
/// runs each through the Tier-1 inline anomaly detector (with the corridor
/// network attached), batches into Arrow RecordBatches via MicroBatcher,
/// and forwards to `batch_tx` for Arrow IPC publishing.
///
/// Anomalous observations are also serialized to JSON ([`AnomalyEvent`])
/// and published to NATS `corridor.traffic.anomaly` for Tier-2 processing.
#[instrument(name = "normalization_pipeline", skip_all)]
pub async fn run_normalization_pipeline(
    mut feed_rx: mpsc::Receiver<TrafficObservation>,
    batch_tx: mpsc::Sender<RecordBatch>,
    nats: async_nats::Client,
) -> Result<(), IngressError> {
    let (config, config_source) = AnomalyConfig::load();
    tracing::info!(source = %config_source, "anomaly thresholds loaded");

    let mut detector = AnomalyDetector::with_network(config);
    let mut batcher = MicroBatcher::new(BATCH_FLUSH_COUNT);
    let flush_duration = std::time::Duration::from_millis(BATCH_FLUSH_TIMEOUT_MS);

    tracing::info!(
        flush_count = BATCH_FLUSH_COUNT,
        flush_timeout_ms = BATCH_FLUSH_TIMEOUT_MS,
        "schema normalization pipeline running"
    );

    loop {
        tokio::select! {
            Some(obs) = feed_rx.recv() => {
                // Run Tier-1 anomaly detection
                let result = assess(&mut detector, obs);

                // Publish anomalous observations to NATS for Tier-2 processing
                if let Some(event) = AnomalyEvent::from_result(&result) {
                    tracing::debug!(
                        segment_id = %result.observation.segment_id,
                        flags = ?event.anomaly_flags,
                        confidence = result.confidence,
                        "anomaly detected"
                    );

                    if let Ok(json) = serde_json::to_vec(&event)
                        && let Err(e) = nats.publish(SUBJECT_ANOMALY, json.into()).await
                    {
                        tracing::warn!(%e, "failed to publish anomaly to NATS");
                    }
                }

                if let Some(batch) = batcher.push(result.observation)?
                    && let Err(e) = batch_tx.send(batch).await
                {
                    tracing::error!(%e, "batch channel closed — publisher down?");
                    return Err(IngressError::ChannelSend);
                }
            }
            // Timeout path: flush partial batch if observations are arriving slowly
            _ = tokio::time::sleep(flush_duration) => {
                if batcher.has_pending() {
                    let batch = batcher.flush()?;
                    if let Err(e) = batch_tx.send(batch).await {
                        tracing::error!(%e, "batch channel closed — publisher down?");
                        return Err(IngressError::ChannelSend);
                    }
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{DataQuality, FeedSource};

    const VOCABULARY: [&str; 8] = [
        "future_timestamp",
        "stale_data",
        "speed_jump",
        "phantom_reading",
        "speed_zscore",
        "congestion_outlier",
        "road_class_mismatch",
        "spatial_mismatch",
    ];

    fn obs(segment_id: &str, lon: f64, lat: f64, speed: f32) -> TrafficObservation {
        TrafficObservation {
            timestamp_ms: chrono::Utc::now().timestamp_millis(),
            lon,
            lat,
            segment_id: segment_id.to_string(),
            speed_kmh: speed,
            congestion_index: 0.3,
            source: FeedSource::Crowdsource,
            data_quality: DataQuality::Synthetic,
            confidence: 0.6,
        }
    }

    #[test]
    fn anomaly_event_carries_flags_in_the_anomaly_log_vocabulary() {
        let mut detector = AnomalyDetector::with_network(AnomalyConfig::default());
        // 150 km/h, ~1 km off Relief Road (40 km/h): road class + spatial
        let result = assess(&mut detector, obs("1005", 72.5943, 23.0207, 150.0));
        let event = AnomalyEvent::from_result(&result).expect("flagged");
        let doc: serde_json::Value = serde_json::to_value(&event).unwrap();

        let flags: Vec<&str> = doc["anomaly_flags"]
            .as_array()
            .unwrap()
            .iter()
            .map(|f| f.as_str().unwrap())
            .collect();
        assert!(flags.contains(&"road_class_mismatch"));
        assert!(flags.contains(&"spatial_mismatch"));
        assert!(flags.iter().all(|f| VOCABULARY.contains(f)));
        assert_eq!(doc["severity"], "high");
        // pre-existing keys unchanged for current consumers
        assert_eq!(doc["segment_id"], "1005");
        assert!(doc["timestamp_ms"].as_i64().is_some());
        assert_eq!(doc["data_quality"], "Anomalous");
        assert_eq!(doc["source"], "Crowdsource");
        // the published confidence is feed confidence × detector confidence
        let c = doc["confidence"].as_f64().unwrap();
        assert!(c < 0.6 * 0.5 + 1e-6, "confidence {c}");
    }

    #[test]
    fn clean_reading_keeps_its_feed_confidence_and_publishes_no_event() {
        let mut detector = AnomalyDetector::with_network(AnomalyConfig::default());
        let seg = crate::feeds::city::segment_by_id(1005).unwrap();
        let result = assess(&mut detector, obs("1005", seg.mid.0, seg.mid.1, 30.0));
        assert!(AnomalyEvent::from_result(&result).is_none());
        assert!((result.observation.confidence - 0.6).abs() < 1e-6);
        assert_eq!(result.observation.data_quality, DataQuality::Synthetic);
    }
}
