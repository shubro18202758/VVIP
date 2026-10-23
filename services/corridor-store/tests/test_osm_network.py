"""The road network on OpenStreetMap — WP-20 (contract C-K basis 'osm').

scripts/gen_osm_network.py traces every segment along its real road from the
reviewable mapping table scripts/data/osm_network_map.json and writes the
fresh-install seed, the network step that converts an existing store
(reanchor_osm_network.sql), the superseded display file and the evidence
table scripts/data/osm_network.json.  Migration 020 records the provenance.

Static checks (always run, no database):
    * the mapping table keeps every id (segments 1001-1049, junctions 1-33),
      names every junction in English and Gujarati, says why each renamed
      segment was renamed and keeps every camera id;
    * the generator's offline --verify passes on the committed evidence:
      Hausdorff to the OSM centreline <= 15 m, ends exactly at the junction
      points, length within 2 % of the OSM path, one strongly connected
      network, the seed and the network step carrying the evidence's
      network, the ODbL attribution in every header — and it says the
      junction-node check was not run when no extract is loaded;
    * the committed extract snapshot (scripts/data/osm/*.overpass.json.gz)
      is the one the evidence was built from, and from it (or an Overpass
      cache, VEER_OSM_CACHE) two regenerations are byte-identical and equal
      the committed files;
    * the network step is one guarded transaction (every write an upsert or
      a delete with a WHERE) in the splitter-safe grammar; migration 020 is
      schema only, additive, idempotent;
    * it re-anchors ONLY emulated camera reads, BEFORE the segments move —
      and the same reads, by read id, in the runs' recorded tapes (position
      only) — and writes no decision or audit table;
    * its event-profile spines are the seeded values of 008 / 010 moving to
      the use-case library's routes, every library spine a legal path;
    * its event-profile text corrections start from the values 010 seeds
      (the watchlist sweep no longer promises an earmarked-plate hit);
    * its network-probe line states the evidence's counts and is the line
      scripts/veer_up.sh reads.

Database checks (VEER_TEST_PG_DSN names a store with 001-020 + the seed
files; every write is rolled back):
    * the store's network IS the evidence: 49 segments of geom_source 'osm'
      on their evidence lines with no display line, junctions on their OSM
      nodes, the seed-fabricated hourly aggregates gone;
    * applying the network step again changes no row;
    * 020's CHECK refuses an unknown geom_source;
    * an emulated read on a segment the step moves keeps its fraction along
      that segment (attributed by the recommendation evidence, the line it
      lies on, else its camera), a live or unattributable read stays put,
      and a second apply moves nothing;
    * the same read in a run's recorded tape follows it (payload.position
      only, every other frame byte-identical); a tape frame of a live read
      or of no stored read stays put; a second apply rewrites no tape;
    * a profile still at its seeded spine takes the library's, an
      operator's spine is kept; every stored profile spine is a legal path;
    * the sweep profile's seeded description and template are corrected,
      an operator's text is kept, a second apply changes nothing.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import secrets
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]  # services/corridor-store
REPO = ROOT.parents[1]
GENERATOR = REPO / "scripts" / "gen_osm_network.py"
VEER_UP = REPO / "scripts" / "veer_up.sh"
MAPPING = REPO / "scripts" / "data" / "osm_network_map.json"
EVIDENCE = REPO / "scripts" / "data" / "osm_network.json"
NETWORK = ROOT / "seed" / "reanchor_osm_network.sql"
SEED = ROOT / "seed" / "sample_corridor.sql"
M020 = ROOT / "migrations" / "020_road_network_osm.sql"
SNAPSHOT = REPO / "scripts" / "data" / "osm"
#: an Overpass cache when one is named, else a path that never exists (the
#: generator then reads the committed snapshot)
CACHE = Path(os.environ.get("VEER_OSM_CACHE", "/nonexistent"))

CAMERA_IDS = {
    "EM-J01", "EM-J01B", "EM-J02", "EM-J03", "EM-J03B", "EM-J04", "EM-J05", "EM-J06",
    "EM-J07", "EM-J07B", "EM-J08", "EM-J08B", "EM-J09", "EM-J10", "EM-J11", "EM-J12",
    "EM-J13", "EM-J13B", "EM-J14", "EM-J15", "EM-BR-NEHRU-W", "EM-BR-NEHRU-E",
    "EM-BR-ELLIS-W", "EM-BR-ELLIS-E", "EM-CAM-01", "EM-CAM-02", "EM-CAM-03", "EM-CAM-04",
}


def _generator():
    if "gen_osm_network" in sys.modules:
        return sys.modules["gen_osm_network"]
    spec = importlib.util.spec_from_file_location("gen_osm_network", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["gen_osm_network"] = module   # dataclasses resolve their module
    spec.loader.exec_module(module)
    return module


def _no_comments(sql_text: str) -> str:
    return "\n".join(line.split("--", 1)[0] for line in sql_text.splitlines())


def _statements(sql_text: str) -> list[str]:
    """Split like the naive consumers: strip -- comments, split on ';'."""
    return [s.strip() for s in _no_comments(sql_text).split(";") if s.strip()]


def _flat(stmt: str) -> str:
    return " ".join(stmt.split())


# ── static: the mapping table ──────────────────────────────────────────────


class TestMappingTable:
    def test_every_id_is_kept(self):
        doc = json.loads(MAPPING.read_text(encoding="utf-8"))
        assert sorted(s["segment_id"] for s in doc["segments"]) == list(range(1001, 1050))
        jids = sorted(j["junction_id"] for j in doc["junctions"])
        assert jids[:33] == list(range(1, 34)) and all(j > 33 for j in jids[33:])
        assert {c["camera_id"] for c in doc["cameras"]} == CAMERA_IDS

    def test_junctions_are_named_in_both_languages_with_evidence(self):
        doc = json.loads(MAPPING.read_text(encoding="utf-8"))
        for j in doc["junctions"]:
            assert j["name"] and j["evidence"], j["junction_id"]
            assert re.search(r"[઀-૿]", j["name_gu"]), j["junction_id"]  # Gujarati
            assert j["confidence"] in ("high", "medium"), j["junction_id"]
            assert isinstance(j["osm_node_id"], int) and j["osm_node_id"] > 0

    def test_a_renamed_segment_says_why(self):
        doc = json.loads(MAPPING.read_text(encoding="utf-8"))
        for s in doc["segments"]:
            if s["road_name"] != s["seed"]["road_name"]:
                assert s.get("rename_reason"), s["segment_id"]

    def test_only_osm_one_ways_are_one_way(self):
        """1012 Gandhi Road (eastbound) and 1013 Swami Vivekananda Road
        (westbound) are one-way in OpenStreetMap; every other segment is a
        two-way road (a dual carriageway drawn along one of its halves)."""
        doc = json.loads(MAPPING.read_text(encoding="utf-8"))
        assert {s["segment_id"] for s in doc["segments"] if s["oneway"]} == {1012, 1013}


# ── static: the evidence and the generated files ───────────────────────────


class TestEvidence:
    def test_offline_verify_passes(self, capsys):
        gen = _generator()
        assert gen.verify(None) == 0
        out = capsys.readouterr().out
        assert "OK: every segment on its OSM road" in out
        assert "FAIL" not in out
        # nothing measured the junctions against OSM nodes: it must say so
        lal_darwaja = next(line for line in out.splitlines()
                           if re.match(r"\s*3 Lal Darwaja\b", line))
        assert " n/a " in lal_darwaja and "0.00" not in lal_darwaja
        assert "junction nodes and byte-identity NOT checked" in out

    def test_the_committed_snapshot_is_the_evidence_extract(self):
        gen = _generator()
        doc = json.loads(EVIDENCE.read_text(encoding="utf-8"))
        for label, query in gen.EXTRACTS:
            response = gen.read_snapshot(label, query)
            assert response is not None, f"no committed snapshot of the {label} extract"
            assert response["osm3s"]["timestamp_osm_base"] == doc["osm_base"][label], label
            assert "openstreetmap.org" in response["osm3s"]["copyright"], label
            assert "ODbL" in response["osm3s"]["copyright"], label

    def test_every_segment_names_its_osm_ways_and_name_evidence(self):
        doc = json.loads(EVIDENCE.read_text(encoding="utf-8"))
        assert doc["attribution"].startswith("(c) OpenStreetMap contributors, ODbL 1.0")
        assert doc["network"]["strong_components"] == 1 and not doc["network"]["problems"]
        for s in doc["segments"]:
            assert s["osm_way_ids"] and s["osm_way_id"] in s["osm_way_ids"], s["segment_id"]
            assert s["name_match"] in ("exact", "partial", "connector", "osm_unnamed")
            if s["name_match"] == "osm_unnamed":
                assert s["name_note"] or s["rename_reason"], s["segment_id"]
            # drivable the way it is open (a one-way traced its legal direction)
            assert s["drive_m"] is not None, s["segment_id"]
            if not s["oneway"]:
                assert s["reverse_drive_m"] is not None, s["segment_id"]

    def test_cameras_sit_on_their_own_segment_clear_of_the_others(self):
        gen = _generator()
        doc = json.loads(EVIDENCE.read_text(encoding="utf-8"))
        lines = {s["segment_id"]: [tuple(p) for p in s["line"]] for s in doc["segments"]}
        for c in doc["cameras"]:
            p = (c["lon"], c["lat"])
            assert gen.point_line_m(p, lines[c["segment_id"]]) < 0.5, c["camera_id"]
            for sid, line in lines.items():
                if sid != c["segment_id"]:
                    assert gen.point_line_m(p, line) >= gen.CAMERA_CLEARANCE_M, (c["camera_id"], sid)
            if c["junction_id"] is not None and c["kind"] == "anpr":
                # an ANPR stop line: inside the e-challan engine's 40 m radius
                j = next(j for j in doc["junctions"] if j["junction_id"] == c["junction_id"])
                assert gen.dist_m(p, (j["lon"], j["lat"])) <= 40.0, c["camera_id"]

    def test_the_twin_parses_the_whole_seed(self):
        sys.path.insert(0, str(REPO / "services" / "traffic-oracle" / "src"))
        from traffic_oracle.simulation.network import from_seed_sql

        net = from_seed_sql(SEED)
        assert sorted(net.segments) == list(range(1001, 1050))
        doc = json.loads(EVIDENCE.read_text(encoding="utf-8"))
        for s in doc["segments"]:
            assert net.segments[s["segment_id"]].road_name == s["road_name"]


@pytest.mark.skipif(
    not (CACHE.is_dir() or any(SNAPSHOT.glob("*.overpass.json.gz"))),
    reason="no committed extract snapshot and no Overpass cache (VEER_OSM_CACHE)",
)
class TestDeterminism:
    def test_two_regenerations_are_byte_identical_and_committed(self):
        gen = _generator()
        first = gen.render_all(gen.build_network(gen.load_graph(CACHE, False), gen.load_mapping()))
        second = gen.render_all(gen.build_network(gen.load_graph(CACHE, False), gen.load_mapping()))
        assert first == second
        for path, text in first.items():
            assert path.read_text(encoding="utf-8") == text, path.name


class TestNetworkStepGrammar:
    def test_one_guarded_transaction_with_attribution(self):
        text = NETWORK.read_text(encoding="utf-8")
        assert "(c) OpenStreetMap contributors, ODbL 1.0" in "\n".join(text.splitlines()[:8])
        assert "$$" not in text and "/*" not in text
        stmts = _statements(text)
        assert stmts[0].upper() == "BEGIN" and stmts[-1].upper() == "COMMIT"
        for stmt in stmts[1:-1]:
            flat = _flat(stmt)
            if flat.startswith("INSERT INTO corridor."):
                assert "ON CONFLICT" in flat and "IS DISTINCT FROM" in flat, flat[:80]
            if flat.startswith(("DELETE", "UPDATE")):
                assert " WHERE " in flat, flat[:80]
            assert not flat.startswith(("DROP", "TRUNCATE", "ALTER")), flat[:80]

    def test_splitter_safe_literals(self):
        for literal in re.findall(r"'([^']*)'", _no_comments(NETWORK.read_text(encoding="utf-8"))):
            assert ";" not in literal and "--" not in literal, literal[:80]


#: every statement of the network step that writes a non-temporary table
_WRITE_RE = re.compile(r"^(?:INSERT INTO|UPDATE|DELETE FROM)\s+([a-z_]+\.[a-z_]+)")


class TestEmulatedHistoryStep:
    """B1: the emulated camera reads keep their place along their segment."""

    def test_reads_are_re_anchored_before_the_segments_and_cameras_move(self):
        flats = [_flat(s) for s in _statements(NETWORK.read_text(encoding="utf-8"))]
        order = [next(i for i, f in enumerate(flats) if f.startswith(head)) for head in (
            "CREATE TEMP TABLE _veer_osm_segments",
            "INSERT INTO _veer_osm_segments",
            "UPDATE corridor.vehicle_sightings",
            "UPDATE emulator.emulation_runs",      # the tapes follow the moved reads
            "INSERT INTO corridor.road_segments",
            "INSERT INTO corridor.cameras",
        )]
        assert order == sorted(order), order
        # A writes exactly the lines the reads were re-anchored onto
        a = flats[order[4]]
        assert "FROM _veer_osm_segments ON CONFLICT (segment_id) DO UPDATE" in a

    def test_a_tape_read_takes_its_emulated_read_position_and_nothing_else(self):
        """1b: the God's-eye REPLAY draws a tape's reads — the network step
        gives an anpr_read frame the position of the EMULATED read its
        read_id names (same camera), and writes nothing else of the run."""
        upd = next(_flat(s) for s in _statements(NETWORK.read_text(encoding="utf-8"))
                   if _flat(s).startswith("UPDATE emulator.emulation_runs"))
        # one SET target: the frames, rebuilt in order with only payload.position replaced
        assert upd.startswith("UPDATE emulator.emulation_runs AS r SET tape = "
                              "jsonb_set(r.tape, '{frames}', m.frames) FROM (")
        assert upd.count("jsonb_set(") == 2 and "'{payload,position}'" in upd
        assert "END ORDER BY f.ord) AS frames" in upd and "WITH ORDINALITY AS f (frame, ord)" in upd
        # matched by read id AND camera, to an EMULATED read only
        assert "s.sighting_id::TEXT = f.frame->'payload'->>'read_id'" in upd
        assert "s.camera_id = f.frame->'payload'->>'camera_id'" in upd
        assert "s.source = 'emulated_anpr'" in upd and "live_anpr" not in upd
        assert "f.frame->>'kind' = 'anpr_read'" in upd
        # guarded: a frame within 5 cm of its read is kept, a run with none moved is not written
        assert "NOT ST_DWithin(s.position::geography, tp.position::geography, 0.05)" in upd
        assert upd.endswith("WHERE r.run_id = m.run_id AND m.moved > 0")
        # no cast can fail on a malformed tape: positions are checked numbers first
        assert "jsonb_typeof(f.frame->'payload'->'position'->0) = 'number'" in upd
        assert "CASE WHEN jsonb_typeof(t.tape->'frames') = 'array'" in upd

    def test_only_emulated_reads_move_and_only_their_position(self):
        upd = next(_flat(s) for s in _statements(NETWORK.read_text(encoding="utf-8"))
                   if _flat(s).startswith("UPDATE corridor.vehicle_sightings"))
        assert "x.source = 'emulated_anpr'" in upd and "v.source = 'emulated_anpr'" in upd
        assert "live_anpr" not in upd
        # one SET target, the position, at the same fraction on the new line
        assert re.search(r"\bSET position = ST_LineInterpolatePoint\(n\.geom, "
                         r"ST_LineLocatePoint\(s\.geom, v\.position\)\) FROM\b", upd), upd[:160]
        # attribution: the recommendation evidence, the line it lies on, its camera
        assert "r.evidence->>'segment_id'" in upd and "ST_DWithin" in upd
        assert "c.segment_id) AS segment_id" in upd
        # a segment whose line is already the new one moves no read
        assert upd.endswith("AND NOT ST_OrderingEquals(s.geom, n.geom)")

    def test_no_decision_or_audit_table_is_written(self):
        written = {m.group(1) for s in _statements(NETWORK.read_text(encoding="utf-8"))
                   if (m := _WRITE_RE.match(_flat(s)))}
        assert written == {
            "corridor.vehicle_sightings", "emulator.emulation_runs", "corridor.road_segments",
            "corridor.junctions", "corridor.segment_adjacency", "corridor.signal_programs",
            "corridor.cameras", "corridor.agency_stations", "traffic.hourly_aggregates",
            "events.event_profiles",
        }, written
        # the emulator ledger: its recorded tape only — never status, spec or the frame log
        for s in _statements(NETWORK.read_text(encoding="utf-8")):
            if _flat(s).startswith("UPDATE emulator.emulation_runs"):
                assert re.match(r"UPDATE emulator\.emulation_runs AS r SET tape = ", _flat(s))
                assert not re.search(r"\b(status|spec|ended_at)\s*=", _flat(s).split(" FROM (")[0])


class TestProfileSpines:
    """B2: events.event_profiles spines on the real network."""

    def test_the_seeded_spines_are_the_migrations(self):
        seeded = _generator().seeded_profile_spines()
        assert set(seeded) == {
            "vvip_convoy_base", "rath_yatra_ahmedabad", "kumbh_scale_gathering",
            "riverfront_marathon", "national_day_parade", "motera_stadium_egress",
            "flood_evacuation_corridor", "metro_work_zone", "watchlist_anpr_sweep",
        }
        assert seeded["rath_yatra_ahmedabad"] == (1012, 1015, 1005, 1013)      # 008
        assert seeded["watchlist_anpr_sweep"] == (1012, 1015, 1005, 1013)      # 010

    def test_every_library_spine_is_a_legal_path_and_the_step_carries_them(self):
        gen = _generator()
        doc = json.loads(EVIDENCE.read_text(encoding="utf-8"))
        edges = gen.evidence_edges(doc)
        assert len(edges) == doc["network"]["road_graph_edges"]
        ids = {s["segment_id"] for s in doc["segments"]}
        spines, problems = gen.profile_spines(ids, edges)
        assert problems == []
        use_cases, _scenarios = gen.library_routes()
        seeded = gen.seeded_profile_spines()
        for pid in seeded:
            assert gen.spine_problems(use_cases[pid], ids, edges) == [], pid
        # the step rewrites exactly the profiles whose seeded spine differs
        assert gen.sql_profile_spines(NETWORK.read_text(encoding="utf-8")) == spines
        assert {p.profile_id for p in spines} == {
            pid for pid in seeded if seeded[pid] != use_cases[pid]}
        for p in spines:
            assert p.seeded == seeded[p.profile_id] and p.spine == use_cases[p.profile_id]
        # the seeded spines it replaces are no longer paths on the real network
        assert all(gen.spine_problems(p.seeded, ids, edges) for p in spines)
        # a one-way is only ever driven its own way
        assert gen.spine_problems((1013, 1049), ids, edges)    # against Swami Vivekananda Road

    def test_an_operator_spine_is_kept(self):
        upd = next(_flat(s) for s in _statements(NETWORK.read_text(encoding="utf-8"))
                   if _flat(s).startswith("UPDATE events.event_profiles AS p SET route_segment_ids"))
        assert upd.startswith("UPDATE events.event_profiles AS p SET route_segment_ids = v.spine")
        assert upd.endswith("WHERE p.profile_id = v.profile_id AND p.route_segment_ids = v.seeded "
                            "AND p.route_segment_ids IS DISTINCT FROM v.spine")


class TestProfileTexts:
    """Item 3: the watchlist sweep profile (010) no longer promises an
    earmarked-plate hit — GJ27EM4321 is an operator exercise."""

    def test_the_seeded_values_are_010s(self):
        gen = _generator()
        row = gen.seeded_profile_rows()["watchlist_anpr_sweep"]
        assert row["description"].startswith(
            "A stolen-vehicle report drives an emulated ANPR camera sweep along the old-city belt.")
        assert "include the stolen plate and an earmarked plate" in row["description"]
        assert json.loads(row["template"])["earmarked_plate_hit"] is True
        # the same parse reads every seeded spine the H parser reads
        rows = gen.seeded_profile_rows()
        for pid, spine in gen.seeded_profile_spines().items():
            ids = re.search(r"ARRAY\[([0-9,\s]+)\]", rows[pid]["route_segment_ids"]).group(1)
            assert tuple(int(x) for x in ids.split(",")) == spine, pid

    def test_the_corrected_text_is_honest_and_the_step_carries_it(self):
        gen = _generator()
        texts, problems = gen.profile_texts()
        assert problems == []
        by_col = {t.column: t for t in texts if t.profile_id == "watchlist_anpr_sweep"}
        assert set(by_col) == {"description", "template"}
        desc = by_col["description"].corrected
        assert "an earmarked plate" not in desc and "e-challan" not in desc
        for needle in ("EMULATED", "GJ27EM4321 is an OPERATOR EXERCISE",
                       "the emulator never earmarks it", "R_STOLEN_SIGHTING", "never a fine",
                       "only a named human officer"):
            assert needle in desc, needle
        template = json.loads(by_col["template"].corrected)
        assert template["earmarked_plate_hit"] is False
        assert template["operator_earmark_exercise"] is True
        assert template["stolen_plate_hit"] is True and template["seed"] == 20261105
        # the library carries the same honest feature
        lib = json.loads(gen.USE_CASE_LIBRARY.read_text(encoding="utf-8"))
        sweep = next(u for u in lib if u["id"] == "watchlist_anpr_sweep")["scenarioConfig"]
        assert "operator_earmark_exercise" in sweep["expected_features"]
        # the generated step writes exactly these, from exactly 010's values
        got = gen.sql_profile_texts(NETWORK.read_text(encoding="utf-8"))
        assert [gen._text_key(t) for t in got] == [gen._text_key(t) for t in texts]

    def test_only_a_still_seeded_value_is_corrected(self):
        stmts = [_flat(s) for s in _statements(NETWORK.read_text(encoding="utf-8"))
                 if re.match(r"UPDATE events\.event_profiles AS p SET (description|template) ",
                             _flat(s))]
        assert len(stmts) == 2
        for upd in stmts:
            column = upd.split(" SET ")[1].split()[0]
            assert upd.endswith(f"WHERE p.profile_id = v.profile_id AND p.{column} = v.seeded "
                                f"AND p.{column} IS DISTINCT FROM v.corrected"), upd[-160:]

    def test_verify_notices_a_step_out_of_step_with_010(self):
        gen = _generator()
        texts, _ = gen.profile_texts()
        text = NETWORK.read_text(encoding="utf-8")
        tampered = text.replace("include the stolen plate and an ",
                                "include the stolen plate and one ", 1)
        assert tampered != text
        assert ([gen._text_key(t) for t in gen.sql_profile_texts(tampered)]
                != [gen._text_key(t) for t in texts])


class TestNetworkProbe:
    """B3: the line scripts/veer_up.sh probes a store against."""

    def test_the_probe_line_states_the_evidence_network(self):
        doc = json.loads(EVIDENCE.read_text(encoding="utf-8"))
        m = _generator().PROBE_RE.search(NETWORK.read_text(encoding="utf-8"))
        assert m, "no network-probe line"
        assert (int(m.group(1)), int(m.group(2))) == (len(doc["segments"]), len(doc["junctions"]))

    def test_veer_up_reads_it(self):
        text = VEER_UP.read_text(encoding="utf-8")
        body = re.search(r"^network_expected\(\) \{.*?^\}", text, re.S | re.M)
        assert body, "veer_up.sh has no network_expected()"
        out = subprocess.run(
            ["bash", "-c", body.group(0) + "\nnetwork_expected"],
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "NETWORK_SQL": str(NETWORK)},
            capture_output=True, text=True, check=True).stdout.split()
        doc = json.loads(EVIDENCE.read_text(encoding="utf-8"))
        assert out == [str(len(doc["segments"])), str(len(doc["junctions"]))]


class TestMigration020Grammar:
    def test_013_header_grammar(self):
        lines = M020.read_text(encoding="utf-8").splitlines()
        assert lines[0] == f"-- {M020.name}"
        header = "\n".join(line for line in lines if line.startswith("--"))
        assert "SCHEMA ONLY, additive, idempotent" in header
        assert "Grammar constraint (007 header): consumed by naive ';'-splitters" in header
        assert "Ethics rails (C6)" in header

    def test_schema_only_idempotent_statements(self):
        stmts = _statements(M020.read_text(encoding="utf-8"))
        dropped: set[str] = set()
        for stmt in stmts:
            flat = _flat(stmt)
            assert flat.split()[0] in {"ALTER", "COMMENT"}, flat[:60]
            assert not re.search(r"\bINSERT\s+INTO\b|\bDELETE\s+FROM\b|\bUPDATE\s+\S+\s+SET\b",
                                 flat, re.I), flat[:60]
            if "ADD COLUMN" in flat:
                assert "ADD COLUMN IF NOT EXISTS" in flat, flat[:60]
            m = re.search(r"DROP CONSTRAINT IF EXISTS (\w+)", flat)
            if m:
                dropped.add(m.group(1))
            m = re.search(r"ADD CONSTRAINT (\w+)", flat)
            if m:
                assert m.group(1) in dropped, f"ADD {m.group(1)} without a guarded DROP"
        flat = _flat(_no_comments(M020.read_text(encoding="utf-8")))
        assert "ADD COLUMN IF NOT EXISTS geom_source TEXT;" in flat
        assert "geom_source IN ('schematic', 'osm')" in flat
        assert "ADD COLUMN IF NOT EXISTS geom_meta JSONB;" in flat
        assert "ALTER TABLE corridor.junctions ADD COLUMN IF NOT EXISTS osm_node_id BIGINT;" in flat


# ── database ────────────────────────────────────────────────────────────────

_DSN = os.environ.get("VEER_TEST_PG_DSN", "")
db = pytest.mark.skipif(
    not _DSN or ":56434" in _DSN,
    reason="set VEER_TEST_PG_DSN to a SANDBOX store with 001-020 + the seed files",
)


def _run(coro):
    return asyncio.run(coro)


async def _connect():
    asyncpg = pytest.importorskip("asyncpg")
    conn = await asyncpg.connect(_DSN)
    has_020 = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = "
        "'corridor' AND table_name = 'road_segments' AND column_name = 'geom_source')")
    if not has_020:
        await conn.close()
        pytest.skip("migration 020 not applied on this store")
    return conn


@db
class TestStore:
    def test_the_store_network_is_the_evidence(self):
        doc = json.loads(EVIDENCE.read_text(encoding="utf-8"))

        async def run():
            conn = await _connect()
            try:
                rows = {r["segment_id"]: r for r in await conn.fetch(
                    "SELECT segment_id, road_name, oneway, geom_source, osm_way_id, "
                    "display_geom IS NULL AS no_display, "
                    "ST_AsText(geom) AS wkt FROM corridor.road_segments")}
                for s in doc["segments"]:
                    r = rows[s["segment_id"]]
                    assert r["geom_source"] == "osm" and r["no_display"], s["segment_id"]
                    assert (r["road_name"], r["oneway"], r["osm_way_id"]) == (
                        s["road_name"], s["oneway"], s["osm_way_id"])
                    pts = [tuple(float(v) for v in p.split())
                           for p in re.search(r"\((.*)\)", r["wkt"]).group(1).split(",")]
                    assert [(round(x, 7), round(y, 7)) for x, y in pts] == [
                        tuple(p) for p in s["line"]], s["segment_id"]
                junctions = {r["junction_id"]: r for r in await conn.fetch(
                    "SELECT junction_id, osm_node_id, name, ST_X(geom) AS lon, ST_Y(geom) AS lat "
                    "FROM corridor.junctions")}
                for j in doc["junctions"]:
                    r = junctions[j["junction_id"]]
                    assert r["osm_node_id"] == j["osm_node_id"] and r["name"] == j["name"]
                    assert (round(r["lon"], 7), round(r["lat"], 7)) == (j["lon"], j["lat"])
                # C-P: nothing in the REAL-only table without a real observation
                fabricated = await conn.fetchval(
                    "SELECT count(*) FROM traffic.hourly_aggregates h WHERE NOT EXISTS ("
                    "SELECT 1 FROM traffic.observations o WHERE o.segment_id = h.segment_id "
                    "AND o.data_quality = 'real' AND o.timestamp_utc >= h.hour_utc "
                    "AND o.timestamp_utc < h.hour_utc + interval '1 hour')")
                assert fabricated == 0
            finally:
                await conn.close()

        _run(run())

    def test_applying_the_network_step_again_changes_no_row(self):
        stmts = _statements(NETWORK.read_text(encoding="utf-8"))[1:-1]

        async def run():
            conn = await _connect()
            tr = conn.transaction()
            await tr.start()
            try:
                for stmt in stmts:
                    status = await conn.execute(stmt)
                    flat = _flat(stmt)
                    if flat.startswith(("INSERT INTO corridor.", "DELETE FROM", "UPDATE ")):
                        assert status in ("INSERT 0 0", "DELETE 0", "UPDATE 0"), (status, flat[:70])
            finally:
                await tr.rollback()
                await conn.close()

        _run(run())

    def test_an_emulated_read_keeps_its_place_along_a_moved_segment(self):
        """1015 and 1013 drawn 230 m off (the 'old' network), then the step:
        each emulated read lands on its segment's real line at the fraction
        it had along the old one — attributed by the recommendation evidence
        (over its camera), the line it lies on, else its camera; a live read
        and an unattributable read stay put; a second apply moves nothing."""
        stmts = _statements(NETWORK.read_text(encoding="utf-8"))[1:-1]
        cam = "EM-CAM-02"   # registered on 1015

        async def run():
            conn = await _connect()
            tr = conn.transaction()
            await tr.start()
            try:
                assert await conn.fetchval(
                    "SELECT segment_id FROM corridor.cameras WHERE camera_id = $1", cam) == 1015
                real = {r["segment_id"]: r["wkt"] for r in await conn.fetch(
                    "SELECT segment_id, ST_AsText(geom) AS wkt FROM corridor.road_segments "
                    "WHERE segment_id IN (1013, 1015)")}
                await conn.execute(
                    "UPDATE corridor.road_segments SET geom = ST_Translate(geom, 0.002, 0.001) "
                    "WHERE segment_id IN (1013, 1015)")
                vehicle = await conn.fetchval(
                    "INSERT INTO corridor.vehicles (plate) VALUES ($1) RETURNING vehicle_id",
                    f"GJ99EM{secrets.randbelow(9000) + 1000}")
                # (label, source, camera, position SQL over the moved line g of segment $1)
                reads = {
                    "on_line": ("emulated_anpr", cam, 1015, "ST_LineInterpolatePoint(g, 0.3)"),
                    "unregistered_on_line": ("emulated_anpr", "EM-TEST-NONE", 1013,
                                             "ST_LineInterpolatePoint(g, 0.6)"),
                    "evidence": ("emulated_anpr", cam, 1013,
                                 "ST_Translate(ST_LineInterpolatePoint(g, 0.5), 0, 0.0003)"),
                    "camera": ("emulated_anpr", cam, 1015,
                               "ST_Translate(ST_LineInterpolatePoint(g, 0.5), 0, 0.00045)"),
                    "live": ("live_anpr", cam, 1015, "ST_LineInterpolatePoint(g, 0.3)"),
                    "nowhere": ("emulated_anpr", "EM-TEST-NONE", 1015,
                                "ST_SetSRID(ST_MakePoint(72.30, 23.30), 4326)"),
                }
                ids: dict[str, uuid.UUID] = {}
                for label, (source, camera, sid, pos) in reads.items():
                    ids[label] = uuid.uuid4()
                    await conn.execute(
                        "INSERT INTO corridor.vehicle_sightings (sighting_id, vehicle_id, plate, "
                        "camera_id, t, position, source) SELECT $1::uuid, $2::uuid, 'GJ99EM0000', "
                        f"$3::text, NOW(), {pos}, $4::text FROM (SELECT geom AS g FROM "
                        "corridor.road_segments WHERE segment_id = $5::bigint) x",
                        ids[label], vehicle, camera, source, sid)
                evidence = json.dumps({"segment_id": 1013, "source": "emulated_anpr"})
                await conn.execute(
                    "INSERT INTO events.enforcement_recommendations (sighting_id, vehicle_id, "
                    "rule_id, recommendation, evidence) VALUES ($1, $2, 'R_SPD_SEGMENT', "
                    "'test', $3::jsonb)", ids["evidence"], vehicle, evidence)
                rec_before = await conn.fetchval(
                    "SELECT md5(r::text) FROM events.enforcement_recommendations r "
                    "WHERE sighting_id = $1", ids["evidence"])
                # the fraction each read has along its old line, and where it was
                old = {}
                for label, sid in (("on_line", 1015), ("unregistered_on_line", 1013),
                                   ("evidence", 1013), ("camera", 1015)):
                    old[label] = (sid, await conn.fetchval(
                        "SELECT ST_LineLocatePoint(g.geom, s.position) FROM "
                        "corridor.vehicle_sightings s, corridor.road_segments g "
                        "WHERE s.sighting_id = $1 AND g.segment_id = $2", ids[label], sid))
                kept = {label: await conn.fetchval(
                    "SELECT ST_AsEWKB(position) FROM corridor.vehicle_sightings "
                    "WHERE sighting_id = $1", ids[label]) for label in ("live", "nowhere")}

                for stmt in stmts:
                    await conn.execute(stmt)

                for label, (sid, frac) in old.items():
                    row = await conn.fetchrow(
                        "SELECT ST_Distance(s.position::geography, g.geom::geography) AS off_m, "
                        "ST_LineLocatePoint(g.geom, s.position) AS frac, "
                        "ST_AsText(g.geom) = $3 AS real_line "
                        "FROM corridor.vehicle_sightings s, corridor.road_segments g "
                        "WHERE s.sighting_id = $1 AND g.segment_id = $2", ids[label], sid, real[sid])
                    assert row["real_line"], (label, "segment not back on its real line")
                    assert row["off_m"] < 0.01, (label, row["off_m"])
                    assert abs(row["frac"] - frac) < 1e-6, (label, row["frac"], frac)
                    assert frac > 0.0 and frac < 1.0, label
                for label, ewkb in kept.items():
                    assert await conn.fetchval(
                        "SELECT ST_AsEWKB(position) FROM corridor.vehicle_sightings "
                        "WHERE sighting_id = $1", ids[label]) == ewkb, label
                assert await conn.fetchval(
                    "SELECT md5(r::text) FROM events.enforcement_recommendations r "
                    "WHERE sighting_id = $1", ids["evidence"]) == rec_before

                # a second apply (its own transaction drops the step's temp tables
                # at COMMIT): nothing moves, the lines are already the real ones
                await conn.execute("DROP TABLE _veer_osm_segments, _veer_osm_moves")
                snapshot = await conn.fetchval(
                    "SELECT md5(string_agg(sighting_id::text || ST_AsText(position), ',' "
                    "ORDER BY sighting_id)) FROM corridor.vehicle_sightings")
                for stmt in stmts:
                    status = await conn.execute(stmt)
                    if _flat(stmt).startswith("UPDATE corridor.vehicle_sightings"):
                        assert status == "UPDATE 0", status
                assert await conn.fetchval(
                    "SELECT md5(string_agg(sighting_id::text || ST_AsText(position), ',' "
                    "ORDER BY sighting_id)) FROM corridor.vehicle_sightings") == snapshot
            finally:
                await tr.rollback()
                await conn.close()

        _run(run())

    def test_a_recorded_tape_read_follows_its_read(self):
        """1b: segment 1015 drawn 230 m off (the 'old' network) with three
        reads on it — emulated, live, and one no tape names — and a run whose
        tape records the emulated read, a frame naming the live read's id, a
        read no sighting holds and an agency unit frame: after the step the
        emulated read's frame sits where its read now is, every other frame
        is byte-identical, the run's other columns are untouched, and a
        second apply rewrites no tape."""
        stmts = _statements(NETWORK.read_text(encoding="utf-8"))[1:-1]
        cam = "EM-CAM-02"   # registered on 1015

        async def run():
            conn = await _connect()
            tr = conn.transaction()
            await tr.start()
            try:
                await conn.execute(
                    "UPDATE corridor.road_segments SET geom = ST_Translate(geom, 0.002, 0.001) "
                    "WHERE segment_id = 1015")
                vehicle = await conn.fetchval(
                    "INSERT INTO corridor.vehicles (plate) VALUES ($1) RETURNING vehicle_id",
                    f"GJ99EM{secrets.randbelow(9000) + 1000}")
                ids = {k: uuid.uuid4() for k in ("emulated", "live", "absent")}
                pos = {}
                for label, frac in (("emulated", 0.3), ("live", 0.6)):
                    source = "emulated_anpr" if label == "emulated" else "live_anpr"
                    await conn.execute(
                        "INSERT INTO corridor.vehicle_sightings (sighting_id, vehicle_id, plate, "
                        "camera_id, t, position, source) SELECT $1::uuid, $2::uuid, 'GJ99EM0000', "
                        "$3::text, NOW(), ST_LineInterpolatePoint(geom, $4::float8), $5::text "
                        "FROM corridor.road_segments WHERE segment_id = 1015",
                        ids[label], vehicle, cam, frac, source)
                    pos[label] = list(await conn.fetchrow(
                        "SELECT round(ST_X(position)::numeric, 6)::float8, "
                        "round(ST_Y(position)::numeric, 6)::float8 "
                        "FROM corridor.vehicle_sightings WHERE sighting_id = $1", ids[label]))

                def read(label, position):
                    return {"kind": "anpr_read", "t_sec": 60.0, "frame": 0, "payload": {
                        "camera_id": cam, "plate": "GJ99EM0000", "position": position,
                        "read_id": str(ids[label]), "role": "background", "speed_kmh": 30.0}}

                frames = [
                    {"kind": "event_row", "t_sec": 0.0, "frame": 0,
                     "payload": {"action": "create", "route_segment_ids": [1015]}},
                    read("emulated", pos["emulated"]),
                    read("live", pos["live"]),
                    read("absent", [72.5, 23.0]),
                    {"kind": "agency_unit", "t_sec": 90.0, "frame": 0,
                     "payload": {"unit_id": "JR-EM-T", "position": [72.58, 23.02]}},
                ]
                tape = {"tape_version": 2, "use_case_id": "watchlist_anpr_sweep", "seed": 1,
                        "t0_anchor": "2026-10-23T00:00:00+00:00", "clock_scale": 1.0,
                        "movement_id": str(uuid.uuid4()), "frames": frames}
                run_id = await conn.fetchval(
                    "INSERT INTO emulator.emulation_runs (spec_hash, spec, seed, t0_anchor, "
                    "clock_scale, status, tape) VALUES ('test', '{\"test\": true}'::jsonb, 1, "
                    "NOW(), 1.0, 'completed', $1::jsonb) RETURNING run_id", json.dumps(tape))
                before = await conn.fetchrow(
                    "SELECT to_jsonb(r) - 'tape' AS row, tape FROM emulator.emulation_runs r "
                    "WHERE run_id = $1", run_id)

                for stmt in stmts:
                    await conn.execute(stmt)

                after = await conn.fetchrow(
                    "SELECT to_jsonb(r) - 'tape' AS row, tape FROM emulator.emulation_runs r "
                    "WHERE run_id = $1", run_id)
                assert after["row"] == before["row"]                   # status, spec, …
                old_tape, new_tape = json.loads(before["tape"]), json.loads(after["tape"])
                assert {k: v for k, v in new_tape.items() if k != "frames"} == {
                    k: v for k, v in old_tape.items() if k != "frames"}
                old_frames, new_frames = old_tape["frames"], new_tape["frames"]
                assert len(new_frames) == len(old_frames)
                for i, (o, n) in enumerate(zip(old_frames, new_frames)):
                    if i != 1:
                        assert n == o, (i, n)                          # byte-identical
                moved = new_frames[1]
                assert {k: v for k, v in moved["payload"].items() if k != "position"} == {
                    k: v for k, v in old_frames[1]["payload"].items() if k != "position"}
                assert moved["payload"]["position"] != old_frames[1]["payload"]["position"]
                off = await conn.fetchrow(
                    "SELECT ST_Distance(s.position::geography, ST_SetSRID(ST_MakePoint($2, $3), "
                    "4326)::geography) AS to_read, ST_Distance(g.geom::geography, ST_SetSRID("
                    "ST_MakePoint($2, $3), 4326)::geography) AS to_road FROM "
                    "corridor.vehicle_sightings s, corridor.road_segments g "
                    "WHERE s.sighting_id = $1 AND g.segment_id = 1015", ids["emulated"],
                    *moved["payload"]["position"])
                assert off["to_read"] < 0.01 and off["to_road"] < 0.02, dict(off)
                # the live read itself never moved, so its frame was never touched
                assert await conn.fetchval(
                    "SELECT ST_DWithin(position::geography, ST_SetSRID(ST_MakePoint($2, $3), "
                    "4326)::geography, 0.1) FROM corridor.vehicle_sightings "
                    "WHERE sighting_id = $1", ids["live"], *pos["live"])

                # a second apply rewrites no tape
                await conn.execute("DROP TABLE _veer_osm_segments, _veer_osm_moves")
                for stmt in stmts:
                    status = await conn.execute(stmt)
                    if _flat(stmt).startswith("UPDATE emulator.emulation_runs"):
                        assert status == "UPDATE 0", status
                assert await conn.fetchval(
                    "SELECT tape FROM emulator.emulation_runs WHERE run_id = $1",
                    run_id) == after["tape"]
            finally:
                await tr.rollback()
                await conn.close()

        _run(run())

    def test_a_live_reads_tape_frame_is_never_moved(self):
        """Even a tape frame naming a LIVE read (no emulator tape does, but
        the step must not trust that) keeps its recorded position."""
        stmt = next(s for s in _statements(NETWORK.read_text(encoding="utf-8"))
                    if _flat(s).startswith("UPDATE emulator.emulation_runs"))

        async def run():
            conn = await _connect()
            tr = conn.transaction()
            await tr.start()
            try:
                vehicle = await conn.fetchval(
                    "INSERT INTO corridor.vehicles (plate) VALUES ($1) RETURNING vehicle_id",
                    f"GJ99EM{secrets.randbelow(9000) + 1000}")
                sid = uuid.uuid4()
                await conn.execute(
                    "INSERT INTO corridor.vehicle_sightings (sighting_id, vehicle_id, plate, "
                    "camera_id, t, position, source) VALUES ($1, $2, 'GJ99EM0000', 'EM-CAM-02', "
                    "NOW(), ST_SetSRID(ST_MakePoint(72.581, 23.021), 4326), 'live_anpr')",
                    sid, vehicle)
                tape = {"frames": [{"kind": "anpr_read", "t_sec": 1.0, "frame": 0, "payload": {
                    "camera_id": "EM-CAM-02", "plate": "GJ99EM0000", "position": [72.59, 23.03],
                    "read_id": str(sid)}}]}
                run_id = await conn.fetchval(
                    "INSERT INTO emulator.emulation_runs (spec_hash, spec, seed, t0_anchor, "
                    "status, tape) VALUES ('test', '{}'::jsonb, 1, NOW(), 'completed', "
                    "$1::jsonb) RETURNING run_id", json.dumps(tape))
                await conn.execute(stmt)
                assert json.loads(await conn.fetchval(
                    "SELECT tape FROM emulator.emulation_runs WHERE run_id = $1", run_id)) == tape
            finally:
                await tr.rollback()
                await conn.close()

        _run(run())

    def test_the_sweep_profile_text_is_corrected_only_from_its_seeded_value(self):
        """Item 3: description and template at 010's value take the honest
        text, each column on its own guard; an operator's text stays; a
        second apply changes nothing."""
        gen = _generator()
        row = gen.seeded_profile_rows()["watchlist_anpr_sweep"]
        texts = {t.column: t for t in gen.profile_texts()[0]
                 if t.profile_id == "watchlist_anpr_sweep"}
        stmts = {_flat(s).split(" SET ")[1].split()[0]: s
                 for s in _statements(NETWORK.read_text(encoding="utf-8"))
                 if re.match(r"UPDATE events\.event_profiles AS p SET (description|template) ",
                             _flat(s))}
        assert set(stmts) == {"description", "template"}

        async def run():
            conn = await _connect()
            tr = conn.transaction()
            await tr.start()
            try:
                set_seed = ("UPDATE events.event_profiles SET description = $1, "
                            "template = $2::jsonb WHERE profile_id = 'watchlist_anpr_sweep'")
                read = ("SELECT description, template FROM events.event_profiles "
                        "WHERE profile_id = 'watchlist_anpr_sweep'")
                others = ("SELECT md5(string_agg(p::text, ',' ORDER BY profile_id)) FROM "
                          "events.event_profiles p WHERE profile_id <> 'watchlist_anpr_sweep'")
                untouched = await conn.fetchval(others)
                await conn.execute(set_seed, row["description"], row["template"])
                assert await conn.execute(stmts["description"]) == "UPDATE 1"
                assert await conn.execute(stmts["template"]) == "UPDATE 1"
                got = await conn.fetchrow(read)
                assert got["description"] == texts["description"].corrected
                assert json.loads(got["template"]) == json.loads(texts["template"].corrected)
                assert json.loads(got["template"])["earmarked_plate_hit"] is False
                assert await conn.execute(stmts["description"]) == "UPDATE 0"
                assert await conn.execute(stmts["template"]) == "UPDATE 0"
                # an operator's description is kept; the still-seeded template is corrected
                await conn.execute(set_seed, "Operator wording", row["template"])
                assert await conn.execute(stmts["description"]) == "UPDATE 0"
                assert await conn.execute(stmts["template"]) == "UPDATE 1"
                assert (await conn.fetchrow(read))["description"] == "Operator wording"
                assert await conn.fetchval(others) == untouched
            finally:
                await tr.rollback()
                await conn.close()

        _run(run())

    def test_a_seeded_profile_spine_moves_and_an_operator_spine_stays(self):
        stmts = [s for s in _statements(NETWORK.read_text(encoding="utf-8"))
                 if _flat(s).startswith("UPDATE events.event_profiles AS p SET route_segment_ids")]
        assert len(stmts) == 1
        gen = _generator()
        seeded = gen.seeded_profile_spines()
        use_cases, _scenarios = gen.library_routes()

        async def run():
            conn = await _connect()
            tr = conn.transaction()
            await tr.start()
            try:
                set_spine = ("UPDATE events.event_profiles SET route_segment_ids = $2::bigint[] "
                             "WHERE profile_id = $1")
                await conn.execute(set_spine, "rath_yatra_ahmedabad",
                                   list(seeded["rath_yatra_ahmedabad"]))
                await conn.execute(set_spine, "kumbh_scale_gathering", [1001, 1006])
                assert await conn.execute(stmts[0]) == "UPDATE 1"
                spines = {r["profile_id"]: tuple(r["route_segment_ids"]) for r in await conn.fetch(
                    "SELECT profile_id, route_segment_ids FROM events.event_profiles")}
                assert spines["rath_yatra_ahmedabad"] == use_cases["rath_yatra_ahmedabad"]
                assert spines["kumbh_scale_gathering"] == (1001, 1006)       # the operator's
                assert await conn.execute(stmts[0]) == "UPDATE 0"
                # every stored spine but the operator's is a legal path on the store
                illegal = await conn.fetch(
                    "SELECT p.profile_id FROM events.event_profiles p WHERE EXISTS ("
                    "SELECT 1 FROM generate_subscripts(p.route_segment_ids, 1) i "
                    "WHERE i < array_length(p.route_segment_ids, 1) AND NOT EXISTS ("
                    "SELECT 1 FROM corridor.road_graph g WHERE g.source = p.route_segment_ids[i] "
                    "AND g.target = p.route_segment_ids[i + 1]))")
                assert {r["profile_id"] for r in illegal} <= {"kumbh_scale_gathering"}
            finally:
                await tr.rollback()
                await conn.close()

        _run(run())

    def test_020_check_refuses_an_unknown_source(self):
        async def run():
            asyncpg = pytest.importorskip("asyncpg")
            conn = await _connect()
            tr = conn.transaction()
            await tr.start()
            try:
                with pytest.raises(asyncpg.CheckViolationError):
                    await conn.execute(
                        "UPDATE corridor.road_segments SET geom_source = 'hand_drawn' "
                        "WHERE segment_id = 1001")
            finally:
                await tr.rollback()
                await conn.close()

        _run(run())
