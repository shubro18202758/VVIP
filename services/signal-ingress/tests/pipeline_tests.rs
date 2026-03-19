//! Integration tests for the ingestion pipeline infrastructure.
//!
//! Tests PipelineChannels creation, MicroBatcher flush semantics,
//! Arrow RecordBatch schema correctness, and end-to-end channel flow.

use std::sync::Arc;

use arrow::array::{AsArray, Float32Array, Float64Array, Int64Array, UInt8Array};
use arrow::datatypes::DataType;
use signal_ingress::pipeline::batch::MicroBatcher;
use signal_ingress::pipeline::channels::PipelineChannels;
use signal_ingress::{DataQuality, FeedSource, TrafficObservation};

// ─── Helpers ──────────────────────────────────────────────────────────────────

fn make_obs(segment_id: &str, speed: f32, quality: DataQuality) -> TrafficObservation {
    TrafficObservation {
        timestamp_ms: 1710000000000,
        lon: 77.2090,
        lat: 28.6139,
        segment_id: segment_id.to_string(),
        speed_kmh: speed,
        congestion_index: 0.3,
        source: FeedSource::FleetGps,
        data_quality: quality,
        confidence: 0.85,
    }
}

fn make_default_obs(speed: f32) -> TrafficObservation {
    make_obs("seg_100", speed, DataQuality::Real)
}

// ─── PipelineChannels Tests ──────────────────────────────────────────────────

#[tokio::test]
async fn channels_create_and_send_observation() {
    let channels = PipelineChannels::create(100, 10);
    let tx = channels.feed_tx;
    let mut rx = channels.feed_rx;

    let obs = make_default_obs(60.0);
    tx.send(obs.clone()).await.unwrap();
    let received = rx.recv().await.unwrap();
    assert_eq!(received.speed_kmh, 60.0);
    assert_eq!(received.segment_id, "seg_100");
}

#[tokio::test]
async fn channels_bounded_capacity() {
    let channels = PipelineChannels::create(2, 1);
    let tx = channels.feed_tx;

    // Fill the channel to capacity
    tx.send(make_default_obs(40.0)).await.unwrap();
    tx.send(make_default_obs(50.0)).await.unwrap();

    // Third send should not complete immediately (channel full)
    let result = tx.try_send(make_default_obs(60.0));
    assert!(result.is_err());
}

#[tokio::test]
async fn channels_batch_side_sends_record_batch() {
    let channels = PipelineChannels::create(100, 10);
    let batch_tx = channels.batch_tx;
    let mut batch_rx = channels.batch_rx;

    // Build a RecordBatch via MicroBatcher and send it through the batch channel
    let mut batcher = MicroBatcher::new(2);
    batcher.push(make_default_obs(40.0)).unwrap();
    let batch = batcher.flush().unwrap();
    let num_rows = batch.num_rows();

    batch_tx.send(batch).await.unwrap();
    let received = batch_rx.recv().await.unwrap();
    assert_eq!(received.num_rows(), num_rows);
}

#[tokio::test]
async fn channels_rx_returns_none_when_tx_dropped() {
    let channels = PipelineChannels::create(10, 10);
    let tx = channels.feed_tx;
    let mut rx = channels.feed_rx;

    drop(tx);
    let result = rx.recv().await;
    assert!(result.is_none());
}

// ─── MicroBatcher Integration Tests ──────────────────────────────────────────

#[test]
fn batcher_schema_has_nine_columns() {
    let mut batcher = MicroBatcher::new(10);
    batcher.push(make_default_obs(50.0)).unwrap();
    let batch = batcher.flush().unwrap();

    assert_eq!(batch.num_columns(), 9);
    let schema = batch.schema();
    assert_eq!(schema.field(0).name(), "timestamp_ms");
    assert_eq!(schema.field(0).data_type(), &DataType::Int64);
    assert_eq!(schema.field(1).name(), "lon");
    assert_eq!(schema.field(1).data_type(), &DataType::Float64);
    assert_eq!(schema.field(2).name(), "lat");
    assert_eq!(schema.field(2).data_type(), &DataType::Float64);
    assert_eq!(schema.field(3).name(), "segment_id");
    assert_eq!(schema.field(3).data_type(), &DataType::Utf8);
    assert_eq!(schema.field(4).name(), "speed_kmh");
    assert_eq!(schema.field(4).data_type(), &DataType::Float32);
    assert_eq!(schema.field(5).name(), "congestion_index");
    assert_eq!(schema.field(5).data_type(), &DataType::Float32);
    assert_eq!(schema.field(6).name(), "source");
    assert_eq!(schema.field(6).data_type(), &DataType::UInt8);
    assert_eq!(schema.field(7).name(), "data_quality");
    assert_eq!(schema.field(7).data_type(), &DataType::UInt8);
    assert_eq!(schema.field(8).name(), "confidence");
    assert_eq!(schema.field(8).data_type(), &DataType::Float32);
}

#[test]
fn batcher_preserves_observation_values() {
    let mut batcher = MicroBatcher::new(10);
    let obs = TrafficObservation {
        timestamp_ms: 1710099999000,
        lon: 77.5,
        lat: 28.1,
        segment_id: "seg_42".to_string(),
        speed_kmh: 88.5,
        congestion_index: 0.72,
        source: FeedSource::GovernmentTraffic,
        data_quality: DataQuality::Anomalous,
        confidence: 0.65,
    };
    batcher.push(obs).unwrap();
    let batch = batcher.flush().unwrap();

    // Verify each column preserves the value
    let ts = batch.column(0).as_any().downcast_ref::<Int64Array>().unwrap();
    assert_eq!(ts.value(0), 1710099999000);

    let lon = batch.column(1).as_any().downcast_ref::<Float64Array>().unwrap();
    assert!((lon.value(0) - 77.5).abs() < 0.001);

    let lat = batch.column(2).as_any().downcast_ref::<Float64Array>().unwrap();
    assert!((lat.value(0) - 28.1).abs() < 0.001);

    let speed = batch.column(4).as_any().downcast_ref::<Float32Array>().unwrap();
    assert!((speed.value(0) - 88.5).abs() < 0.01);

    let congestion = batch.column(5).as_any().downcast_ref::<Float32Array>().unwrap();
    assert!((congestion.value(0) - 0.72).abs() < 0.01);

    // Source: GovernmentTraffic = 0
    let source = batch.column(6).as_any().downcast_ref::<UInt8Array>().unwrap();
    assert_eq!(source.value(0), 0);

    // DataQuality: Anomalous = 1
    let quality = batch.column(7).as_any().downcast_ref::<UInt8Array>().unwrap();
    assert_eq!(quality.value(0), 1);

    let confidence = batch.column(8).as_any().downcast_ref::<Float32Array>().unwrap();
    assert!((confidence.value(0) - 0.65).abs() < 0.01);
}

#[test]
fn batcher_source_enum_mapping_all_variants() {
    let mut batcher = MicroBatcher::new(10);

    let sources = [
        (FeedSource::GovernmentTraffic, 0u8),
        (FeedSource::MappingApi, 1u8),
        (FeedSource::FleetGps, 2u8),
        (FeedSource::Crowdsource, 3u8),
    ];

    for (src, _) in &sources {
        batcher
            .push(TrafficObservation {
                timestamp_ms: 1710000000000,
                lon: 77.0,
                lat: 28.0,
                segment_id: "seg_1".to_string(),
                speed_kmh: 50.0,
                congestion_index: 0.3,
                source: src.clone(),
                data_quality: DataQuality::Real,
                confidence: 1.0,
            })
            .unwrap();
    }

    let batch = batcher.flush().unwrap();
    let source_col = batch.column(6).as_any().downcast_ref::<UInt8Array>().unwrap();

    for (i, (_, expected)) in sources.iter().enumerate() {
        assert_eq!(source_col.value(i), *expected, "Source mismatch at index {i}");
    }
}

#[test]
fn batcher_data_quality_enum_mapping() {
    let mut batcher = MicroBatcher::new(10);

    let qualities = [
        (DataQuality::Real, 0u8),
        (DataQuality::Anomalous, 1u8),
        (DataQuality::Synthetic, 2u8),
    ];

    for (q, _) in &qualities {
        batcher.push(make_obs("seg_1", 50.0, *q)).unwrap();
    }

    let batch = batcher.flush().unwrap();
    let quality_col = batch.column(7).as_any().downcast_ref::<UInt8Array>().unwrap();

    for (i, (_, expected)) in qualities.iter().enumerate() {
        assert_eq!(quality_col.value(i), *expected, "Quality mismatch at index {i}");
    }
}

#[test]
fn batcher_flush_count_boundary() {
    let mut batcher = MicroBatcher::new(5);

    // Push exactly 4 — should not trigger flush
    for i in 0..4 {
        let result = batcher.push(make_default_obs(40.0 + i as f32)).unwrap();
        assert!(result.is_none(), "Should not flush at count {}", i + 1);
    }
    assert!(batcher.has_pending());

    // 5th push should trigger flush
    let result = batcher.push(make_default_obs(44.0)).unwrap();
    assert!(result.is_some());
    let batch = result.unwrap();
    assert_eq!(batch.num_rows(), 5);
    assert!(!batcher.has_pending());
}

#[test]
fn batcher_consecutive_flushes() {
    let mut batcher = MicroBatcher::new(2);

    // First batch
    batcher.push(make_default_obs(40.0)).unwrap();
    let result = batcher.push(make_default_obs(50.0)).unwrap();
    assert!(result.is_some());
    assert_eq!(result.unwrap().num_rows(), 2);

    // Second batch — batcher should be reusable
    batcher.push(make_default_obs(60.0)).unwrap();
    let result = batcher.push(make_default_obs(70.0)).unwrap();
    assert!(result.is_some());
    assert_eq!(result.unwrap().num_rows(), 2);
}

#[test]
fn batcher_empty_flush_returns_zero_rows() {
    let mut batcher = MicroBatcher::new(10);
    let batch = batcher.flush().unwrap();
    assert_eq!(batch.num_rows(), 0);
}

#[test]
fn batcher_large_batch() {
    let mut batcher = MicroBatcher::new(500);
    for i in 0..499 {
        let result = batcher.push(make_default_obs(30.0 + (i % 50) as f32)).unwrap();
        assert!(result.is_none());
    }
    // 500th push triggers flush
    let result = batcher.push(make_default_obs(80.0)).unwrap();
    assert!(result.is_some());
    assert_eq!(result.unwrap().num_rows(), 500);
}

// ─── Arrow IPC Round-trip Tests ──────────────────────────────────────────────

#[test]
fn arrow_ipc_roundtrip() {
    use arrow::ipc::reader::StreamReader;
    use arrow::ipc::writer::StreamWriter;
    use std::io::Cursor;

    // Build a batch
    let mut batcher = MicroBatcher::new(10);
    for i in 0..5 {
        batcher.push(make_default_obs(40.0 + i as f32 * 10.0)).unwrap();
    }
    let batch = batcher.flush().unwrap();

    // Serialize to IPC
    let mut buf = Cursor::new(Vec::new());
    {
        let mut writer = StreamWriter::try_new(&mut buf, &batch.schema()).unwrap();
        writer.write(&batch).unwrap();
        writer.finish().unwrap();
    }

    // Deserialize from IPC
    let ipc_bytes = buf.into_inner();
    let reader = StreamReader::try_new(Cursor::new(ipc_bytes), None).unwrap();
    let batches: Vec<_> = reader.into_iter().filter_map(|b| b.ok()).collect();

    assert_eq!(batches.len(), 1);
    assert_eq!(batches[0].num_rows(), 5);
    assert_eq!(batches[0].num_columns(), 9);

    // Verify values survived round-trip
    let speed = batches[0].column(4).as_any().downcast_ref::<Float32Array>().unwrap();
    assert!((speed.value(0) - 40.0).abs() < 0.01);
    assert!((speed.value(4) - 80.0).abs() < 0.01);
}

// ─── End-to-end channel flow ─────────────────────────────────────────────────

#[tokio::test]
async fn end_to_end_feed_to_batch_channel() {
    let channels = PipelineChannels::create(1000, 100);
    let feed_tx = channels.feed_tx;
    let mut feed_rx = channels.feed_rx;
    let batch_tx = channels.batch_tx;
    let mut batch_rx = channels.batch_rx;

    // Simulate: feed → batcher → batch channel (no anomaly detection for simplicity)
    let producer = tokio::spawn(async move {
        for i in 0..10 {
            feed_tx
                .send(make_default_obs(40.0 + i as f32))
                .await
                .unwrap();
        }
        drop(feed_tx);
    });

    let normalizer = tokio::spawn(async move {
        let mut batcher = MicroBatcher::new(5);
        while let Some(obs) = feed_rx.recv().await {
            if let Some(batch) = batcher.push(obs).unwrap() {
                batch_tx.send(batch).await.unwrap();
            }
        }
        // Flush remaining
        if batcher.has_pending() {
            let batch = batcher.flush().unwrap();
            batch_tx.send(batch).await.unwrap();
        }
        drop(batch_tx);
    });

    // Consume batches
    let mut total_rows = 0;
    let mut batch_count = 0;
    while let Some(batch) = batch_rx.recv().await {
        total_rows += batch.num_rows();
        batch_count += 1;
    }

    producer.await.unwrap();
    normalizer.await.unwrap();

    assert_eq!(total_rows, 10);
    assert_eq!(batch_count, 2); // 10 observations / 5 = 2 batches
}

#[tokio::test]
async fn end_to_end_with_multiple_segments() {
    let channels = PipelineChannels::create(1000, 100);
    let feed_tx = channels.feed_tx;
    let mut feed_rx = channels.feed_rx;
    let batch_tx = channels.batch_tx;
    let mut batch_rx = channels.batch_rx;

    let producer = tokio::spawn(async move {
        for i in 0..6 {
            let seg = format!("seg_{}", i % 3);
            feed_tx
                .send(make_obs(&seg, 50.0 + i as f32 * 5.0, DataQuality::Real))
                .await
                .unwrap();
        }
        drop(feed_tx);
    });

    let normalizer = tokio::spawn(async move {
        let mut batcher = MicroBatcher::new(3);
        while let Some(obs) = feed_rx.recv().await {
            if let Some(batch) = batcher.push(obs).unwrap() {
                batch_tx.send(batch).await.unwrap();
            }
        }
        if batcher.has_pending() {
            let batch = batcher.flush().unwrap();
            batch_tx.send(batch).await.unwrap();
        }
        drop(batch_tx);
    });

    let mut total_rows = 0;
    while let Some(batch) = batch_rx.recv().await {
        total_rows += batch.num_rows();
        // Verify segment_id column exists and is Utf8
        assert_eq!(batch.schema().field(3).data_type(), &DataType::Utf8);
    }

    producer.await.unwrap();
    normalizer.await.unwrap();

    assert_eq!(total_rows, 6);
}

// ─── DataQuality unit tests ─────────────────────────────────────────────────

#[test]
fn data_quality_as_u8() {
    assert_eq!(DataQuality::Real.as_u8(), 0);
    assert_eq!(DataQuality::Anomalous.as_u8(), 1);
    assert_eq!(DataQuality::Synthetic.as_u8(), 2);
}

#[test]
fn data_quality_default_is_real() {
    assert_eq!(DataQuality::default(), DataQuality::Real);
}

// ─── TrafficObservation serialization ────────────────────────────────────────

#[test]
fn observation_json_roundtrip() {
    let obs = make_default_obs(55.0);
    let json = serde_json::to_string(&obs).unwrap();
    let deserialized: TrafficObservation = serde_json::from_str(&json).unwrap();
    assert_eq!(deserialized.speed_kmh, 55.0);
    assert_eq!(deserialized.segment_id, "seg_100");
    assert!((deserialized.confidence - 0.85).abs() < 0.01);
}

#[test]
fn observation_default_data_quality_in_json() {
    // Deserialize JSON without data_quality field — should default to Real
    let json = r#"{
        "timestamp_ms": 1710000000000,
        "lon": 77.0,
        "lat": 28.0,
        "segment_id": "seg_1",
        "speed_kmh": 50.0,
        "congestion_index": 0.3,
        "source": "FleetGps"
    }"#;
    let obs: TrafficObservation = serde_json::from_str(json).unwrap();
    assert_eq!(obs.data_quality, DataQuality::Real);
    assert!((obs.confidence - 1.0).abs() < f32::EPSILON);
}
