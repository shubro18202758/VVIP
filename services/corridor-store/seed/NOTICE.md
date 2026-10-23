# Third-party notices — corridor-store seed data

## OpenStreetMap (ODbL 1.0)

Veer's Ahmedabad road network is traced on OpenStreetMap:

> Road data © OpenStreetMap contributors, available under the
> Open Database License 1.0 (ODbL) —
> https://www.openstreetmap.org/copyright ·
> https://opendatacommons.org/licenses/odbl/1-0/

What is derived from it: every segment's routing geometry
(`corridor.road_segments.geom`, `geom_source = 'osm'`, the OSM way ids and the
extract's `osm_base` in `geom_meta`), the junction positions
(`corridor.junctions.osm_node_id`), the junction movements and road graph
built on them, the emulated camera sites placed on the real approaches, and
the agency station positions — and, in a store the network step converts,
the positions of the EMULATED camera reads it re-anchors onto those lines
(`corridor.vehicle_sightings.position`, source `emulated_anpr`, and the
same reads inside the runs' recorded tapes, `emulator.emulation_runs.tape`:
each keeps its fraction along its segment, now interpolated on
OSM-derived geometry; these live only in the store, never in a repo file,
and a `live_anpr` read is never moved). Segment names, one-way flags and road classes
follow the OSM ways; lanes and speed limits fall back to the seed values where
OSM carries none (`scripts/data/osm_network.json` records which, per segment).
Junction type and signal control are NOT taken from OSM: each junction keeps
its seed `junction_type` / `signal_control`, for continuity with the signal
programs and the history recorded against them. The evidence records the OSM
side beside them (`osm_traffic_signals_m`, the nearest `traffic_signals` node):
J4 Paldi stays a roundabout with an OSM signal 10.4 m away, J18 Shivranjani and
J22 CG Road - Commerce College Road stay unsignalised with one 9.8 m and 0 m
away, and ten signalised junctions have no OSM signal node within 150 m.

Source: two bulk Overpass API extracts (`overpass-api.de`, mirror
`overpass.kumi.systems`), `osm_base` city 2026-10-23T08:08:01Z and walled city
2026-10-23T08:34:36Z. `scripts/gen_osm_network.py` traces the network from the
reviewable mapping table `scripts/data/osm_network_map.json` and writes every
file below. The two extracts themselves are committed beside the mapping
table (`scripts/data/osm/*.overpass.json.gz`), so a clone regenerates the
same files offline and `--verify` checks them against a regeneration; re-run
the generator with `--fetch --cache DIR` (an empty DIR) for a newer extract,
which rewrites the files and the snapshot together.

| File | What it carries | Where it is credited |
| --- | --- | --- |
| `seed/sample_corridor.sql` | the network rows of a fresh install | file header |
| `seed/reanchor_osm_network.sql` | the idempotent network step that converts an existing store (and re-anchors its EMULATED reads onto the new lines) | file header |
| `seed/display_geometry_osm.sql` | superseded display lines (now only clears one a store still carries) | file header |
| `scripts/data/osm/city.overpass.json.gz`, `scripts/data/osm/walled_city.overpass.json.gz` | the two Overpass extracts the network is built from, unmodified (gzip of the request and the response) | the response's own `osm3s.copyright` (openstreetmap.org, ODbL) and this notice |
| `scripts/data/osm_network_map.json` | the mapping table: OSM node per junction, road per segment, evidence | `about` |
| `scripts/data/osm_network.json` | the evidence: traced paths, OSM way and node ids, validation metrics | `attribution` |
| `services/signal-ingress/src/feeds/segments_ahmedabad.rs` | the ingress feeds' polylines (`scripts/gen_ingress_segments.py`, read from the store) | module header |
| `scripts/data/route_acceptance_pairs.json` | routes over the network (`scripts/poi_route_matrix.py`) | `attribution` |
| `scripts/scenario_library.json` | the twin's network facts (`python -m traffic_oracle.simulation export`) | `attribution` |

Share-alike: the OSM-derived data in these files remains under ODbL 1.0
(section 4.4), whatever licence covers the code around it (the repository's
MIT `LICENSE`). A map drawn from it (the God's-eye canvas) is a Produced Work
and credits OpenStreetMap on screen; the deck's own notice is
`services/command-deck/src/lib/gods-eye/NOTICE.md`.
