//! End-to-end checks of the edge-synthetic Ahmedabad feed: all four
//! collectors driven over simulated IST time through the same normalisation
//! step the service runs (`assess_at` + `AnomalyEvent`), measured the way
//! the WP-08 acceptance probe measures the live NATS plane:
//! distinct segments per 20 s window, the Ahmedabad envelope, the anomalous
//! share, the fleet's mean speed against the limit, and the C-H flags — and
//! every row within the 30 m on-road bar of its own segment's real geometry
//! (WP-20's acceptance bar, all four feeds).

use std::collections::{HashMap, HashSet};

use chrono::{DateTime, TimeZone, Utc};
use signal_ingress::anomaly::config::AnomalyConfig;
use signal_ingress::anomaly::detector::AnomalyDetector;
use signal_ingress::anomaly::flags::AnomalyFlag;
use signal_ingress::feeds::city::{
    CongestionField, DEFAULT_CITY_SEED, congestion_for, ist, segment_by_id,
};
use signal_ingress::feeds::crowdsource::{CrowdFeed, ON_ROAD_BAR_M};
use signal_ingress::feeds::fleet_gps::{FLEET_SIZE, FleetSim};
use signal_ingress::feeds::mapping_api::{MappingFeed, REFRESH_SEC};
use signal_ingress::feeds::traffic_gov::GovernmentFeed;
use signal_ingress::normalize::schema_align::{AnomalyEvent, assess_at};
use signal_ingress::{DataQuality, FeedSource, TrafficObservation};

const ANOMALY_LOG_VOCABULARY: [&str; 8] = [
    "future_timestamp",
    "stale_data",
    "speed_jump",
    "phantom_reading",
    "speed_zscore",
    "congestion_outlier",
    "road_class_mismatch",
    "spatial_mismatch",
];

struct Published {
    second: i64,
    row: TrafficObservation,
    event: Option<serde_json::Value>,
}

fn ist_at(h: u32, m: u32) -> DateTime<Utc> {
    ist()
        .with_ymd_and_hms(2026, 10, 22, h, m, 0)
        .unwrap()
        .with_timezone(&Utc)
}

/// Runs every feed for `secs` seconds from `start`, exactly as the service
/// interleaves them, and returns what would be published.
fn simulate(start: DateTime<Utc>, secs: i64, seed: u64) -> Vec<Published> {
    let field = CongestionField::new(DEFAULT_CITY_SEED);
    let mut gov = GovernmentFeed::new(field, seed);
    let mut fleet = FleetSim::new(field, seed + 1, FLEET_SIZE, start);
    let mut crowd = CrowdFeed::new(field, seed + 2);
    let mut mapping = MappingFeed::new(field, seed + 3);
    let mut detector = AnomalyDetector::with_network(AnomalyConfig::embedded());

    let mut out = Vec::new();
    for s in 0..secs {
        let t = start + chrono::Duration::seconds(s);
        let mut arrivals = Vec::new();
        if s % 300 == 0 {
            arrivals.extend(mapping.snapshot(t));
        }
        if s % 60 == 0 {
            arrivals.extend(crowd.burst(t));
        }
        arrivals.extend(gov.readings_for_second(t.timestamp()));
        arrivals.extend(fleet.tick(t, 1.0));
        for obs in arrivals {
            let result = assess_at(&mut detector, obs, t.timestamp_millis());
            let event =
                AnomalyEvent::from_result(&result).map(|e| serde_json::to_value(&e).unwrap());
            out.push(Published {
                second: s,
                row: result.observation,
                event,
            });
        }
    }
    out
}

fn anomalous_share(rows: &[&Published]) -> f64 {
    rows.iter()
        .filter(|p| p.row.data_quality == DataQuality::Anomalous)
        .count() as f64
        / rows.len() as f64
}

fn fleet_speed_ratio(rows: &[&Published]) -> f64 {
    let fleet: Vec<f64> = rows
        .iter()
        .filter(|p| matches!(p.row.source, FeedSource::FleetGps))
        .map(|p| {
            let limit = segment_by_id(p.row.segment_id.parse().unwrap())
                .unwrap()
                .speed_limit_kmh;
            (p.row.speed_kmh / limit) as f64
        })
        .collect();
    fleet.iter().sum::<f64>() / fleet.len() as f64
}

fn check_period(label: &str, start: DateTime<Utc>, seed: u64, min_fleet_ratio: f64) {
    let published = simulate(start, 1800, seed);
    let all: Vec<&Published> = published.iter().collect();
    assert!(!all.is_empty());

    for p in &all {
        let id: i64 = p.row.segment_id.parse().expect("numeric segment id");
        assert!((1001..=1049).contains(&id), "{label}: segment {id}");
        // on its own road: within the 30 m bar of the segment's real
        // (OpenStreetMap) geometry, whatever the feed's sensor noise
        let off = segment_by_id(id).unwrap().distance_m(p.row.lon, p.row.lat);
        assert!(
            off < ON_ROAD_BAR_M,
            "{label}: {:?} row {off:.2} m off segment {id}",
            p.row.source
        );
        // inside the OpenStreetMap extract the network is traced from
        assert!(
            (72.45..=72.75).contains(&p.row.lon),
            "{label}: lon {}",
            p.row.lon
        );
        assert!(
            (22.90..=23.20).contains(&p.row.lat),
            "{label}: lat {}",
            p.row.lat
        );
        assert!(matches!(
            p.row.data_quality,
            DataQuality::Synthetic | DataQuality::Anomalous
        ));
        assert!((0.0..=1.0).contains(&p.row.congestion_index));
        assert!((0.0..=1.0).contains(&p.row.confidence));
        match &p.event {
            Some(doc) => {
                assert_eq!(p.row.data_quality, DataQuality::Anomalous);
                let flags = doc["anomaly_flags"]
                    .as_array()
                    .expect("anomaly_flags array");
                assert!(!flags.is_empty());
                for f in flags {
                    assert!(
                        ANOMALY_LOG_VOCABULARY.contains(&f.as_str().unwrap()),
                        "{label}: flag {f}"
                    );
                }
                assert!(["low", "medium", "high"].contains(&doc["severity"].as_str().unwrap()));
            }
            None => assert_eq!(p.row.data_quality, DataQuality::Synthetic),
        }
    }

    // every 20 s window (the acceptance probe's duration) after warm-up
    let mut by_second: HashMap<i64, Vec<&Published>> = HashMap::new();
    for p in &all {
        by_second.entry(p.second).or_default().push(p);
    }
    let mut worst_share: f64 = 0.0;
    let mut worst_fleet: f64 = f64::MAX;
    for w in (120..1780).step_by(20) {
        let window: Vec<&Published> = (w..w + 20)
            .flat_map(|s| by_second.get(&s).cloned().unwrap_or_default())
            .collect();
        let distinct: HashSet<&str> = window.iter().map(|p| p.row.segment_id.as_str()).collect();
        assert!(
            distinct.len() >= 35,
            "{label}: window at {w}s saw {} segments",
            distinct.len()
        );
        worst_share = worst_share.max(anomalous_share(&window));
        worst_fleet = worst_fleet.min(fleet_speed_ratio(&window));
    }

    let share = anomalous_share(&all);
    let fleet = fleet_speed_ratio(&all);
    let mut by_source: HashMap<String, (usize, usize)> = HashMap::new();
    let mut by_flag: HashMap<String, usize> = HashMap::new();
    for p in &all {
        let e = by_source.entry(format!("{:?}", p.row.source)).or_default();
        e.0 += 1;
        if let Some(doc) = &p.event {
            e.1 += 1;
            for f in doc["anomaly_flags"].as_array().unwrap() {
                *by_flag
                    .entry(format!("{:?}/{}", p.row.source, f.as_str().unwrap()))
                    .or_default() += 1;
            }
        }
    }
    let mut flags: Vec<_> = by_flag.into_iter().collect();
    flags.sort_by_key(|f| std::cmp::Reverse(f.1));
    println!("{label}: rows/flagged by source {by_source:?}; flags {flags:?}");
    println!(
        "{label}: rows {} anomalous {:.2}% (worst 20 s window {:.2}%), fleet {:.3}×limit (worst window {:.3})",
        all.len(),
        share * 100.0,
        worst_share * 100.0,
        fleet,
        worst_fleet
    );
    assert!(share < 0.10, "{label}: anomalous share {share:.3}");
    assert!(
        worst_share < 0.15,
        "{label}: worst 20 s window anomalous share {worst_share:.3}"
    );
    assert!(
        fleet >= min_fleet_ratio,
        "{label}: fleet mean {fleet:.3}×limit"
    );
    assert!(
        worst_fleet >= 0.5,
        "{label}: worst 20 s window fleet mean {worst_fleet:.3}×limit"
    );
}

#[test]
fn off_peak_feed_meets_the_live_acceptance_bar() {
    check_period("14:30 IST", ist_at(14, 30), 1, 0.6);
}

#[test]
fn evening_peak_feed_meets_the_live_acceptance_bar() {
    check_period("19:00 IST", ist_at(19, 0), 2, 0.5);
}

#[test]
fn morning_peak_feed_meets_the_live_acceptance_bar() {
    check_period("10:15 IST", ist_at(10, 15), 3, 0.5);
}

#[test]
fn night_feed_meets_the_live_acceptance_bar() {
    check_period("03:00 IST", ist_at(3, 0), 4, 0.6);
}

#[test]
fn exaggerated_crowd_reports_are_caught_after_warm_up() {
    // Both kinds of exaggeration (free flow claimed in a jam: ×2.5; a jam
    // claimed on a clear road: ×0.3) must reach the anomaly channel once the
    // per-segment crowd history exists, while honest reports mostly pass.
    let start = ist_at(17, 30);
    let field = CongestionField::new(DEFAULT_CITY_SEED);
    let published = simulate(start, 90 * 60, 13);
    let (mut exaggerated, mut caught, mut honest, mut honest_flagged) = (0, 0, 0, 0);
    for p in published.iter().filter(|p| p.second >= 30 * 60) {
        if !matches!(p.row.source, FeedSource::Crowdsource) {
            continue;
        }
        let seg = segment_by_id(p.row.segment_id.parse().unwrap()).unwrap();
        let seen_at = DateTime::<Utc>::from_timestamp_millis(p.row.timestamp_ms).unwrap();
        let ratio = p.row.speed_kmh / field.speed(seg, seen_at);
        let flagged = p.row.data_quality == DataQuality::Anomalous;
        if (ratio - 1.0).abs() > 0.6 {
            exaggerated += 1;
            caught += flagged as usize;
        } else {
            honest += 1;
            honest_flagged += flagged as usize;
        }
    }
    let recall = caught as f64 / exaggerated as f64;
    let false_alarms = honest_flagged as f64 / honest as f64;
    println!(
        "crowd: exaggerated {caught}/{exaggerated} caught, honest {honest_flagged}/{honest} flagged"
    );
    assert!(exaggerated >= 20, "only {exaggerated} exaggerated reports");
    assert!(recall >= 0.9, "recall {recall:.2}");
    assert!(
        false_alarms < 0.1,
        "honest reports flagged {false_alarms:.2}"
    );
}

#[test]
fn crowd_outliers_reach_the_anomaly_channel() {
    // Outliers are a ~5% random draw and only the free-flow claims in a jam
    // exceed 1.6x the limit, so pool a few seeds: the check must never be
    // vacuous, whatever the network size does to one seed's draw order.
    let published: Vec<Published> = (9..15)
        .flat_map(|seed| simulate(ist_at(18, 30), 1800, seed))
        .collect();
    let crowd: Vec<&Published> = published
        .iter()
        .filter(|p| matches!(p.row.source, FeedSource::Crowdsource))
        .collect();
    let exaggerated: Vec<&&Published> = crowd
        .iter()
        .filter(|p| {
            let seg = segment_by_id(p.row.segment_id.parse().unwrap()).unwrap();
            p.row.speed_kmh > 1.6 * seg.speed_limit_kmh
        })
        .collect();
    assert!(
        !exaggerated.is_empty(),
        "no exaggerated crowd reports in 6 x 30 min"
    );
    for p in exaggerated {
        let doc = p
            .event
            .as_ref()
            .expect("an exaggerated report must be flagged");
        let flags: Vec<&str> = doc["anomaly_flags"]
            .as_array()
            .unwrap()
            .iter()
            .map(|f| f.as_str().unwrap())
            .collect();
        assert!(flags.contains(&"road_class_mismatch"), "flags {flags:?}");
    }
}

#[test]
fn mapping_snapshots_reach_the_statistical_checks() {
    // The provider refreshes every 5 minutes, yet within the 30-minute
    // window its snapshots must reach the statistical checks: a provider
    // glitch that is neither a 40 km/h jump nor implausible for the road (a
    // phantom jam on a clear arterial) is caught only by the z-score / IQR
    // checks.
    let start = ist_at(14, 0);
    let field = CongestionField::new(DEFAULT_CITY_SEED);
    let mut mapping = MappingFeed::new(field, 21);
    let mut detector = AnomalyDetector::with_network(AnomalyConfig::embedded());
    let at = |k: i64| start + chrono::Duration::seconds(k * REFRESH_SEC as i64);

    // 35 minutes of honest snapshots
    let (mut rows, mut flagged) = (0, 0);
    let mut last: HashMap<String, f32> = HashMap::new();
    for k in 0..8 {
        for obs in mapping.snapshot(at(k)) {
            last.insert(obs.segment_id.clone(), obs.speed_kmh);
            let r = assess_at(&mut detector, obs, at(k).timestamp_millis());
            rows += 1;
            flagged += r.is_anomalous as usize;
        }
    }
    assert!(
        flagged * 50 < rows,
        "{flagged}/{rows} honest mapping rows flagged"
    );

    // the next snapshot reports the fastest segment 30 km/h below its last
    // reading
    let t = at(8);
    let mut glitch = mapping
        .snapshot(t)
        .into_iter()
        .max_by(|a, b| a.speed_kmh.total_cmp(&b.speed_kmh))
        .unwrap();
    let seg = segment_by_id(glitch.segment_id.parse().unwrap()).unwrap();
    glitch.speed_kmh = last[&glitch.segment_id] - 30.0;
    glitch.congestion_index = congestion_for(seg.speed_limit_kmh, glitch.speed_kmh);
    let r = assess_at(&mut detector, glitch, t.timestamp_millis());
    assert!(!r.flags.contains(&AnomalyFlag::SpeedJump), "{:?}", r.flags);
    assert!(
        !r.flags.contains(&AnomalyFlag::RoadClassMismatch),
        "{:?}",
        r.flags
    );
    assert!(r.flags.contains(&AnomalyFlag::SpeedZScore), "{:?}", r.flags);
}
