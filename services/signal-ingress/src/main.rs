use signal_ingress::feeds;
use signal_ingress::normalize;
use signal_ingress::pipeline::channels::PipelineChannels;
use signal_ingress::publish;
use tracing_subscriber::EnvFilter;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    // Initialize structured JSON logging with env-filter (RUST_LOG)
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::from_default_env())
        .json()
        .init();

    tracing::info!("signal-ingress starting — VVIP corridor traffic feed aggregator");

    // Initialize NATS connection for downstream IPC
    let nats_url =
        std::env::var("NATS_URL").unwrap_or_else(|_| "nats://localhost:4222".to_string());
    let nats_client = async_nats::connect(&nats_url).await?;
    tracing::info!(nats_url = %nats_url, "connected to NATS message bus");

    // Create bounded pipeline channels
    // feed_tx/rx: 10,000 raw observations buffer
    // batch_tx/rx: 100 Arrow RecordBatch buffer
    let channels = PipelineChannels::create(10_000, 100);

    // Clone feed_tx for each independent feed collector
    let gov_tx = channels.feed_tx.clone();
    let gps_tx = channels.feed_tx.clone();
    let map_tx = channels.feed_tx.clone();
    let crowd_tx = channels.feed_tx;

    // Spawn independent feed collector tasks — each runs on its own Tokio task
    // to maximize throughput without blocking other feeds
    let gov_handle = tokio::spawn(feeds::traffic_gov::poll_government_feeds(gov_tx));
    let gps_handle = tokio::spawn(feeds::fleet_gps::stream_fleet_telemetry(gps_tx));
    let map_handle = tokio::spawn(feeds::mapping_api::fetch_road_network_updates(map_tx));
    let crowd_handle = tokio::spawn(feeds::crowdsource::listen_crowdsource_reports(crowd_tx));

    // Normalization pipeline — reads raw observations, runs anomaly detection,
    // batches into Arrow RecordBatch, forwards to publisher
    let nats_for_normalizer = nats_client.clone();
    let normalize_handle = tokio::spawn(async move {
        normalize::schema_align::run_normalization_pipeline(
            channels.feed_rx,
            channels.batch_tx,
            nats_for_normalizer,
        )
        .await
    });

    // Arrow IPC publisher — serializes RecordBatches and publishes to NATS
    let publish_handle = tokio::spawn(async move {
        publish::arrow_ipc::run_publisher(nats_client, channels.batch_rx).await
    });

    // Await all tasks — if any feed crashes, log and continue others
    tokio::select! {
        res = gov_handle => tracing::warn!(?res, "government feed task exited"),
        res = gps_handle => tracing::warn!(?res, "fleet GPS task exited"),
        res = map_handle => tracing::warn!(?res, "mapping API task exited"),
        res = crowd_handle => tracing::warn!(?res, "crowdsource task exited"),
        res = normalize_handle => tracing::error!(?res, "normalization pipeline exited"),
        res = publish_handle => tracing::error!(?res, "Arrow IPC publisher exited"),
    }

    Ok(())
}
