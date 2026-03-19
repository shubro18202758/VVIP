use crate::TrafficObservation;
use arrow::record_batch::RecordBatch;
use tokio::sync::mpsc;

/// Channel handles for the ingestion pipeline.
///
/// Data flow: feeds → feed_tx/rx → normalizer → batch_tx/rx → publisher
pub struct PipelineChannels {
    pub feed_tx: mpsc::Sender<TrafficObservation>,
    pub feed_rx: mpsc::Receiver<TrafficObservation>,
    pub batch_tx: mpsc::Sender<RecordBatch>,
    pub batch_rx: mpsc::Receiver<RecordBatch>,
}

impl PipelineChannels {
    /// Create bounded pipeline channels.
    ///
    /// `obs_buffer` — capacity for raw observations (default 10,000).
    /// `batch_buffer` — capacity for Arrow RecordBatches (default 100).
    pub fn create(obs_buffer: usize, batch_buffer: usize) -> Self {
        let (feed_tx, feed_rx) = mpsc::channel(obs_buffer);
        let (batch_tx, batch_rx) = mpsc::channel(batch_buffer);
        Self {
            feed_tx,
            feed_rx,
            batch_tx,
            batch_rx,
        }
    }
}
