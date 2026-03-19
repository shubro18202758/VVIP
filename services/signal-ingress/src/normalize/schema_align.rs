//! Schema Alignment Pipeline — normalizes heterogeneous traffic feed data
//! into a canonical Arrow-compatible columnar format for zero-copy IPC.
//!
//! Data flow: feed_rx → anomaly detection → MicroBatcher → batch_tx
//! Flagged anomalies are also published separately to NATS `corridor.traffic.anomaly`.

use arrow::record_batch::RecordBatch;
use tokio::sync::mpsc;
use tracing::instrument;

use crate::anomaly::config::AnomalyConfig;
use crate::anomaly::detector::AnomalyDetector;
use crate::pipeline::batch::MicroBatcher;
use crate::{IngressError, TrafficObservation};

/// Flush timeout for the micro-batcher when observation flow is slow.
const BATCH_FLUSH_TIMEOUT_MS: u64 = 100;

/// Default flush count for micro-batcher.
const BATCH_FLUSH_COUNT: usize = 100;

/// Runs the continuous normalization pipeline.
///
/// Receives raw observations from all feed collectors via `feed_rx`,
/// runs each through the Tier-1 inline anomaly detector, batches into
/// Arrow RecordBatches via MicroBatcher, and forwards to `batch_tx`
/// for Arrow IPC publishing.
///
/// Anomalous observations are also serialized to JSON and published to
/// NATS `corridor.traffic.anomaly` for Tier-2 ML processing.
#[instrument(name = "normalization_pipeline", skip_all)]
pub async fn run_normalization_pipeline(
    mut feed_rx: mpsc::Receiver<TrafficObservation>,
    batch_tx: mpsc::Sender<RecordBatch>,
    nats: async_nats::Client,
) -> Result<(), IngressError> {
    let config = AnomalyConfig::from_yaml("models/configs/anomaly_detector.yaml")
        .unwrap_or_else(|e| {
            tracing::warn!(%e, "failed to load anomaly config, using defaults");
            AnomalyConfig::default()
        });

    let mut detector = AnomalyDetector::new(config);
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
                let result = detector.check(obs);

                // Publish anomalous observations to NATS for Tier-2 processing
                if result.is_anomalous {
                    let flag_names: Vec<&str> = result.flags.iter().map(|f| f.name()).collect();
                    tracing::debug!(
                        segment_id = %result.observation.segment_id,
                        flags = ?flag_names,
                        confidence = result.confidence,
                        "anomaly detected"
                    );

                    if let Ok(json) = serde_json::to_vec(&result.observation) {
                        if let Err(e) = nats.publish("corridor.traffic.anomaly", json.into()).await {
                            tracing::warn!(%e, "failed to publish anomaly to NATS");
                        }
                    }
                }

                // Update confidence on the observation and push to batcher
                let mut obs = result.observation;
                obs.confidence = result.confidence;

                if let Some(batch) = batcher.push(obs)? {
                    if let Err(e) = batch_tx.send(batch).await {
                        tracing::error!(%e, "batch channel closed — publisher down?");
                        return Err(IngressError::ChannelSend);
                    }
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
