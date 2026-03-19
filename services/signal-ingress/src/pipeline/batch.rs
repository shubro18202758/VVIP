use std::sync::Arc;

use arrow::array::{
    Float32Array, Float64Array, Int64Array, StringBuilder, UInt8Array,
};
use arrow::datatypes::{DataType, Field, Schema};
use arrow::record_batch::RecordBatch;

use crate::{IngressError, TrafficObservation};

/// Accumulates observations and flushes them as Arrow RecordBatches.
///
/// Dual-trigger flush: by count threshold OR by timeout (whichever fires first).
/// Target latency: <300μs per batch of 100 observations.
pub struct MicroBatcher {
    buffer: Vec<TrafficObservation>,
    flush_count: usize,
    schema: Arc<Schema>,
}

impl MicroBatcher {
    pub fn new(flush_count: usize) -> Self {
        let schema = Arc::new(Self::traffic_schema());
        Self {
            buffer: Vec::with_capacity(flush_count),
            flush_count,
            schema,
        }
    }

    /// Push an observation into the buffer. Returns `Some(RecordBatch)` if flush threshold is reached.
    pub fn push(&mut self, obs: TrafficObservation) -> Result<Option<RecordBatch>, IngressError> {
        self.buffer.push(obs);
        if self.buffer.len() >= self.flush_count {
            Ok(Some(self.flush()?))
        } else {
            Ok(None)
        }
    }

    /// Force-flush whatever is in the buffer (timeout path).
    pub fn flush(&mut self) -> Result<RecordBatch, IngressError> {
        let batch = self.buffer.drain(..).collect::<Vec<_>>();
        Self::to_record_batch(&self.schema, &batch)
    }

    /// Returns true if the buffer has pending observations.
    pub fn has_pending(&self) -> bool {
        !self.buffer.is_empty()
    }

    fn traffic_schema() -> Schema {
        Schema::new(vec![
            Field::new("timestamp_ms", DataType::Int64, false),
            Field::new("lon", DataType::Float64, false),
            Field::new("lat", DataType::Float64, false),
            Field::new("segment_id", DataType::Utf8, false),
            Field::new("speed_kmh", DataType::Float32, false),
            Field::new("congestion_index", DataType::Float32, false),
            Field::new("source", DataType::UInt8, false),
            Field::new("data_quality", DataType::UInt8, false),
            Field::new("confidence", DataType::Float32, false),
        ])
    }

    fn to_record_batch(
        schema: &Arc<Schema>,
        observations: &[TrafficObservation],
    ) -> Result<RecordBatch, IngressError> {
        let len = observations.len();

        let timestamps = Int64Array::from(
            observations.iter().map(|o| o.timestamp_ms).collect::<Vec<_>>(),
        );
        let lons = Float64Array::from(
            observations.iter().map(|o| o.lon).collect::<Vec<_>>(),
        );
        let lats = Float64Array::from(
            observations.iter().map(|o| o.lat).collect::<Vec<_>>(),
        );

        let mut seg_builder = StringBuilder::with_capacity(len, len * 16);
        for obs in observations {
            seg_builder.append_value(&obs.segment_id);
        }
        let segment_ids = seg_builder.finish();

        let speeds = Float32Array::from(
            observations.iter().map(|o| o.speed_kmh).collect::<Vec<_>>(),
        );
        let congestion = Float32Array::from(
            observations.iter().map(|o| o.congestion_index).collect::<Vec<_>>(),
        );
        let sources = UInt8Array::from(
            observations
                .iter()
                .map(|o| match o.source {
                    crate::FeedSource::GovernmentTraffic => 0u8,
                    crate::FeedSource::MappingApi => 1,
                    crate::FeedSource::FleetGps => 2,
                    crate::FeedSource::Crowdsource => 3,
                })
                .collect::<Vec<_>>(),
        );
        let data_qualities = UInt8Array::from(
            observations.iter().map(|o| o.data_quality.as_u8()).collect::<Vec<_>>(),
        );
        let confidences = Float32Array::from(
            observations.iter().map(|o| o.confidence).collect::<Vec<_>>(),
        );

        RecordBatch::try_new(
            schema.clone(),
            vec![
                Arc::new(timestamps),
                Arc::new(lons),
                Arc::new(lats),
                Arc::new(segment_ids),
                Arc::new(speeds),
                Arc::new(congestion),
                Arc::new(sources),
                Arc::new(data_qualities),
                Arc::new(confidences),
            ],
        )
        .map_err(|e| IngressError::ArrowSerialize {
            msg: e.to_string(),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{DataQuality, FeedSource};

    fn sample_obs(speed: f32) -> TrafficObservation {
        TrafficObservation {
            timestamp_ms: 1710000000000,
            lon: 77.2090,
            lat: 28.6139,
            segment_id: "12345".to_string(),
            speed_kmh: speed,
            congestion_index: 0.3,
            source: FeedSource::FleetGps,
            data_quality: DataQuality::Real,
            confidence: 1.0,
        }
    }

    #[test]
    fn test_flush_on_count() {
        let mut batcher = MicroBatcher::new(3);
        assert!(batcher.push(sample_obs(40.0)).unwrap().is_none());
        assert!(batcher.push(sample_obs(45.0)).unwrap().is_none());
        let batch = batcher.push(sample_obs(50.0)).unwrap();
        assert!(batch.is_some());
        let batch = batch.unwrap();
        assert_eq!(batch.num_rows(), 3);
        assert_eq!(batch.num_columns(), 9);
    }

    #[test]
    fn test_force_flush() {
        let mut batcher = MicroBatcher::new(100);
        batcher.push(sample_obs(40.0)).unwrap();
        batcher.push(sample_obs(45.0)).unwrap();
        assert!(batcher.has_pending());
        let batch = batcher.flush().unwrap();
        assert_eq!(batch.num_rows(), 2);
        assert!(!batcher.has_pending());
    }
}
