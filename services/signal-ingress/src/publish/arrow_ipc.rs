//! Arrow IPC Publisher — serializes normalized TrafficObservation batches
//! into Arrow IPC format and publishes over NATS for zero-copy downstream consumption.

use std::io::Cursor;

use arrow::ipc::writer::StreamWriter;
use arrow::record_batch::RecordBatch;
use bytes::Bytes;
use tokio::sync::mpsc;
use tracing::instrument;

use crate::IngressError;

/// NATS subject for live traffic snapshot stream.
const SUBJECT_LIVE: &str = "corridor.traffic.live";

/// Runs the Arrow IPC publisher loop.
///
/// Receives Arrow RecordBatches from the normalization pipeline via `batch_rx`,
/// serializes each to Arrow IPC streaming format, and publishes to NATS
/// `corridor.traffic.live` for consumption by corridor-store and traffic-oracle.
#[instrument(name = "arrow_ipc_publisher", skip_all)]
pub async fn run_publisher(
    nats: async_nats::Client,
    mut batch_rx: mpsc::Receiver<RecordBatch>,
) -> Result<(), IngressError> {
    tracing::info!(subject = SUBJECT_LIVE, "Arrow IPC publisher running on NATS");

    let mut batches_published: u64 = 0;
    let mut rows_published: u64 = 0;

    while let Some(batch) = batch_rx.recv().await {
        let num_rows = batch.num_rows();

        // Serialize RecordBatch to Arrow IPC streaming format
        let ipc_bytes = serialize_batch(&batch)?;

        // Publish to NATS
        if let Err(e) = nats.publish(SUBJECT_LIVE, ipc_bytes).await {
            tracing::error!(%e, "NATS publish failed for corridor.traffic.live");
            return Err(IngressError::NatsPublish { source: e });
        }

        batches_published += 1;
        rows_published += num_rows as u64;

        if batches_published % 100 == 0 {
            tracing::info!(
                batches = batches_published,
                rows = rows_published,
                "Arrow IPC publish progress"
            );
        }
    }

    tracing::warn!(
        batches = batches_published,
        rows = rows_published,
        "batch channel closed — normalization pipeline stopped"
    );
    Ok(())
}

/// Serialize an Arrow RecordBatch to IPC streaming format bytes.
fn serialize_batch(batch: &RecordBatch) -> Result<Bytes, IngressError> {
    let mut buf = Cursor::new(Vec::with_capacity(batch.num_rows() * 128));

    {
        let mut writer = StreamWriter::try_new(&mut buf, &batch.schema())
            .map_err(|e| IngressError::ArrowSerialize { msg: e.to_string() })?;
        writer
            .write(batch)
            .map_err(|e| IngressError::ArrowSerialize { msg: e.to_string() })?;
        writer
            .finish()
            .map_err(|e| IngressError::ArrowSerialize { msg: e.to_string() })?;
    }

    Ok(Bytes::from(buf.into_inner()))
}
