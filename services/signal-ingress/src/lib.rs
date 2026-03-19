pub mod anomaly;
pub mod feeds;
pub mod normalize;
pub mod pipeline;
pub mod publish;

use thiserror::Error;

#[derive(Error, Debug)]
pub enum IngressError {
    #[error("Feed connection failed: {source}")]
    FeedConnection { source: reqwest::Error },

    #[error("GeoJSON parse error: {msg}")]
    GeoParse { msg: String },

    #[error("Arrow IPC serialization failed: {msg}")]
    ArrowSerialize { msg: String },

    #[error("NATS publish failed: {source}")]
    NatsPublish { source: async_nats::PublishError },

    #[error("Channel send failed")]
    ChannelSend,

    #[error("Anomaly config error: {msg}")]
    AnomalyConfig { msg: String },
}

/// Data quality classification for traffic observations.
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub enum DataQuality {
    Real,
    Anomalous,
    Synthetic,
}

impl Default for DataQuality {
    fn default() -> Self {
        Self::Real
    }
}

impl DataQuality {
    pub fn as_u8(self) -> u8 {
        match self {
            Self::Real => 0,
            Self::Anomalous => 1,
            Self::Synthetic => 2,
        }
    }
}

/// Canonical traffic observation normalized from any feed source.
/// Designed for zero-copy Arrow columnar encoding.
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct TrafficObservation {
    /// Unix epoch milliseconds of the observation
    pub timestamp_ms: i64,
    /// WGS84 longitude
    pub lon: f64,
    /// WGS84 latitude
    pub lat: f64,
    /// Road segment identifier (OSM way ID or government road code)
    pub segment_id: String,
    /// Average speed in km/h on this segment at observation time
    pub speed_kmh: f32,
    /// Congestion level [0.0 = free flow, 1.0 = gridlock]
    pub congestion_index: f32,
    /// Source feed identifier
    pub source: FeedSource,
    /// Data quality classification (real, anomalous, or synthetic)
    #[serde(default)]
    pub data_quality: DataQuality,
    /// Confidence score [0.0 = no confidence, 1.0 = fully trusted]
    #[serde(default = "default_confidence")]
    pub confidence: f32,
}

fn default_confidence() -> f32 {
    1.0
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub enum FeedSource {
    GovernmentTraffic,
    MappingApi,
    FleetGps,
    Crowdsource,
}
