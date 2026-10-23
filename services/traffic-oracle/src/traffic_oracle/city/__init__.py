"""City fusion spine — one server-side picture of the city (WP-02, contract C-C).

The God's-eye canvas, the ops wall and the brain all read the SAME fused
model, built here from tables and keys that already exist — the client
never simulates anything:

    cameras.py   GET /api/v1/cameras — the ANPR/CCTV layer: locations and
                 15-minute read counters only (C6), registry or derived.
    entities.py  GET /api/v1/city/entities — every geolocated thing as a
                 CityEntity {urn, kind, label, lon, lat, state, band,
                 source, t, age_sec, links, props} with per-kind feed
                 freshness, batched on one connection.
    dossier.py   GET /api/v1/city/entity/{kind}/{id} — one entity, its
                 1-hop related entities through existing keys, and its
                 merged audit trail.
    feed.py      GET /api/v1/city/feed and /api/v1/events/{id}/audit — the
                 time-ordered narrative unioned from every audit source
                 (enforcement, ANPR hits, dispatch, signals, AMA, event
                 lifecycle, convoy-brain decisions).
    router.py    the HTTP surface (mounted by coordination.py).
    common.py    URNs, id validation, record constructors, plate masking.

This package file stays import-light on purpose: other packages (events,
units, adaptive surfacing) import ``city.common`` without pulling the
router graph in.
"""
