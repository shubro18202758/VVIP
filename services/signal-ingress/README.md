# signal-ingress — Ahmedabad corridor traffic feed (Rust → NATS)

Aggregates traffic observations from four feed types, runs the Tier-1 inline
anomaly detector, and publishes Arrow IPC batches on NATS
`corridor.traffic.live` (consumed by traffic-oracle) plus a JSON document per
flagged row on `corridor.traffic.anomaly`.

| NATS subject | Payload | Consumer |
|---|---|---|
| `corridor.traffic.live` | Arrow IPC record batches (`timestamp_ms`, `lon`, `lat`, `segment_id`, `speed_kmh`, `congestion_index`, `source`, `data_quality`, `confidence`) | traffic-oracle ingest → `traffic.observations` + Valkey `traffic:latest:{sid}` |
| `corridor.traffic.anomaly` | one JSON document per flagged row (contract C-H, below) | traffic-oracle → `traffic.anomaly_log` |

The broker is `NATS_URL` (in the argus stack the compose service DNS of the
project's own NATS, `nats://nats:4222` on its bridge network, published to
the host as 127.0.0.1:4223). The emulator's showcase plane,
`corridor.traffic.emulated`, is NOT published here.

## Edge-synthetic mode (what runs today)

No live ITMS, fleet AVL, crowd or mapping-provider connector is wired yet, so
every collector runs in **edge-synthetic mode** and every row is published
with `data_quality = synthetic` (ordinal 2; rows the detector flags become
`anomalous`, ordinal 1). Nothing here is presented as a measurement: the
oracle keeps these rows out of the REAL hourly aggregates (they roll up into
`traffic.hourly_aggregates_synthetic`, migration 019) and the deck labels
them SIMULATED FEED.

All four collectors observe one shared congestion field over the Ahmedabad
road network (`src/feeds/city.rs`), each through its own sensor model:

| Feed (`source` ordinal) | Model |
|---|---|
| government ITMS (0) | one fixed station per segment midpoint, 15 s cycle, phase-staggered (every segment reports in any 15 s window), detector noise |
| mapping provider (1) | 5-minute segment-speed snapshot for every segment, ±5% provider smoothing |
| fleet GPS (2) | 8 AVL vehicles at 1 Hz driving along the segment polylines, turning at shared junction nodes (one-way respected), mean-reverting speed around the prevailing speed |
| crowdsource (3) | 60 s bursts on trunk/primary roads, more reports in jams, 10-60 s stale, ±10% speed error, ~5% exaggerated reports; phone-GPS position error 12 m σ, truncated at the 30 m on-road bar (`ON_ROAD_BAR_M`) so a report never leaves the road it reports on |

The field is `demand(IST time of day, weekday) × sensitivity(segment) + drift`:
an Ahmedabad weekday profile in IST (UTC+05:30) with a late-morning peak
(~10:15) and a heavier evening peak (~19:00), per-class and per-segment
sensitivity, and smooth per-segment drift. `INGRESS_CITY_SEED` changes the
city's per-segment character; the default is fixed.

## The network table — the real OpenStreetMap road network

`src/feeds/segments_ahmedabad.rs` is GENERATED from `corridor.road_segments`:
the 49 segments, ids 1001–1049, with their ids, attributes (name, class,
lanes, speed limit, one-way), geodesic lengths, ST_LineInterpolatePoint
midpoints, full polylines and endpoint nodes (`NODE_COUNT` 37 distinct
endpoint nodes; a shared endpoint is one node). Since migration 020 the
network's routing geometry IS the OpenStreetMap road between real junction
nodes — drawn by `scripts/gen_osm_network.py` from the committed Overpass
extracts — so the ingress readings land on the real roadway. Road geometry:
© OpenStreetMap contributors, ODbL 1.0
(https://www.openstreetmap.org/copyright), stated in the generated header.
The ingress has no database access at runtime.

```bash
python3 scripts/gen_ingress_segments.py          # regenerate (read-only DB access)
python3 scripts/gen_ingress_segments.py --check  # exit 1 if the store has drifted
```

Regenerate and rebuild the image whenever the corridor network changes.

## Anomaly detection and the anomaly document (contract C-H)

Thresholds: `config/anomaly_detector.yaml`, compiled into the binary and
installed at `/etc/signal-ingress/anomaly_detector.yaml` in the image
(`ANOMALY_CONFIG_PATH` overrides). Each source is judged against its own
recent history on a segment (the last 30 minutes, at most 50 readings); the
z-score and congestion-IQR checks start once that history holds
`min_observations` readings, fewer for the 5-minute mapping snapshots
(`source_min_observations`). The road-class and spatial checks use the
segment's speed limit and geometry.

`corridor.traffic.anomaly` documents keep the observation fields
(`segment_id` string, `timestamp_ms`, `lon`, `lat`, `speed_kmh`,
`congestion_index`, `source`, `data_quality`, `confidence`) and add
`anomaly_flags` (values only from future_timestamp, stale_data, speed_jump,
phantom_reading, speed_zscore, congestion_outlier, road_class_mismatch,
spatial_mismatch — the traffic.anomaly_log vocabulary), `severity`
(low | medium | high) and, additively, `feed_quality`: the reading's data
quality BEFORE the detector relabelled it `Anomalous` (`"Synthetic"` on
these simulated feeds, `"Real"` on a live connector;
`src/normalize/schema_align.rs`). A flag raised on simulated data is
therefore never mistaken for a field anomaly — the oracle stores it in
`traffic.anomaly_log.details` and the city picture labels the anomaly with
it. The field is omitted when unknown; consumers tolerate its absence.

## Build and test

```bash
docker run --rm -v $PWD/services/signal-ingress:/src -w /src rust:alpine \
  sh -c 'apk add -q musl-dev pkgconfig build-base && cargo test --locked'
docker build -f infra/docker/Dockerfile.signal-ingress services/signal-ingress
```

`cargo test --locked` on this tree: **120 passed, 0 failed** — 49 library
unit tests, 7 in `tests/ahmedabad_feed_tests.rs`, 45 in
`tests/anomaly_tests.rs` and 19 in `tests/pipeline_tests.rs` (rust:alpine
on a clean target, re-run 2026-10-24 on the deployed tree). TLS is rustls
(no OpenSSL). `tests/ahmedabad_feed_tests.rs`
drives all four feeds through the detector over simulated IST hours and
checks what the live acceptance probe checks: ≥35 distinct segments per
20 s window, the Ahmedabad envelope, anomalous share, fleet speed vs limit,
the C-H flags — and every row within the 30 m on-road bar of its own
segment's real geometry.
