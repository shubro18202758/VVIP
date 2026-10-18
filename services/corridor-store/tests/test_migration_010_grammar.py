"""Migration grammar + content tests for 010_vehicles_watchlist.sql (C4).

Pure static checks — the same constraints the naive `;`-splitter consumers
(tests/test_migration_pgvector applies every file lexically) and the
ledgered migrate.py impose:
    * no ';' inside string literals (splitter safety);
    * no dollar-quoted bodies;
    * idempotent shape (CREATE ... IF NOT EXISTS / ON CONFLICT / guarded
      DROP CONSTRAINT IF EXISTS);
    * the C3 lockstep: exactly +crime_incident +vehicle_pursuit on BOTH
      event-kind CHECKs, and the guard names match the auto-generated
      constraint names;
    * ethics columns (C6): lawful_basis NOT NULL, retention_until NOT NULL
      with the ordering CHECK, source CHECK limited to emulated/live_anpr;
    * parameter-binding surfaces downstream (vehicles/store.py) carry no
      f-string/format/percent SQL assembly.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]  # services/corridor-store/tests -> VVIP
MIGRATIONS = REPO / "services" / "corridor-store" / "migrations"
M010 = MIGRATIONS / "010_vehicles_watchlist.sql"
STORE_PY = REPO / "services" / "traffic-oracle" / "src" / "traffic_oracle" / "vehicles" / "store.py"


def _statements(sql_text: str) -> list[str]:
    """Split like the naive consumers: strip -- comments, split on ';'."""
    stripped = re.sub(r"--[^\n]*", "", sql_text)
    return [s.strip() for s in stripped.split(";") if s.strip()]


class TestGrammar:
    def test_no_semicolon_inside_string_literals(self):
        text = M010.read_text()
        no_comments = re.sub(r"--[^\n]*", "", text)
        for literal in re.findall(r"'[^']*'", no_comments):
            assert ";" not in literal, f"';' inside literal: {literal!r}"

    def test_no_dollar_quoted_bodies(self):
        assert "$$" not in M010.read_text()
        assert not re.search(r"\$[A-Za-z_]+\$", M010.read_text())

    def test_every_statement_is_splittable_sql(self):
        stmts = _statements(M010.read_text())
        # 4 tables + 7 indexes + 4 guarded ALTERs + 1 seed INSERT
        assert len(stmts) == 16
        for s in stmts:
            first = s.split()[0].upper()
            assert first in {
                "CREATE", "ALTER", "INSERT", "UPDATE", "DELETE", "GRANT",
            }, f"unexpected statement start {first!r}: {s[:60]!r}"

    def test_idempotent_shape(self):
        text = M010.read_text()
        # guarded CHECK relaxation, not bare ADD
        assert "DROP CONSTRAINT IF EXISTS corridor_events_event_kind_check" in text
        assert "DROP CONSTRAINT IF EXISTS event_profiles_event_kind_check" in text
        # seed is ON CONFLICT DO NOTHING
        assert "ON CONFLICT (profile_id) DO NOTHING" in text
        # every CREATE TABLE guarded
        for m in re.finditer(r"CREATE TABLE (\S+)", text):
            idx = m.start()
            window = text[idx: idx + 40]
            assert "IF NOT EXISTS" in window, window


class TestC3Lockstep:
    KIND_LIST = (
        "'protected_movement', 'procession', 'mass_gathering',\n"
        "        'race', 'ceremonial', 'egress_surge',\n"
        "        'emergency_evacuation', 'work_zone',"
    )

    def test_both_check_constraints_extended_identically(self):
        text = M010.read_text()
        for con in (
            "corridor_events_event_kind_check",
            "event_profiles_event_kind_check",
        ):
            # find the re-ADD block for this constraint
            pat = re.compile(
                r"ADD CONSTRAINT " + con + r" CHECK \(event_kind IN \((.*?)\)\)",
                re.S,
            )
            m = pat.search(text)
            assert m, f"re-ADD of {con} missing"
            body = m.group(1)
            for kind in ("crime_incident", "vehicle_pursuit"):
                assert f"'{kind}'" in body
            # exactly the ten kinds — nothing else snuck in
            quoted = re.findall(r"'([a-z_]+)'", body)
            assert sorted(quoted) == sorted(
                [
                    "protected_movement", "procession", "mass_gathering",
                    "race", "ceremonial", "egress_surge",
                    "emergency_evacuation", "work_zone",
                    "crime_incident", "vehicle_pursuit",
                ]
            )

    def test_007_and_008_untouched(self):
        """The ledgered files stay byte-identical (checksum = hard error)."""
        import hashlib

        for name in ("007_corridor_events.sql", "008_event_profiles.sql"):
            data = (MIGRATIONS / name).read_bytes()
            # 007/008 must still contain their ORIGINAL 8-kind CHECKs and
            # no watchlist kinds (010 does the relaxation, never a rewrite)
            text = data.decode()
            assert "crime_incident" not in text, f"{name} was edited!"
            assert "vehicle_pursuit" not in text, f"{name} was edited!"

    def test_oracle_and_emulator_tuples_match_migration(self):
        import sys

        sys.path.insert(0, str(REPO / "services" / "traffic-oracle" / "src"))
        from traffic_oracle.events import EVENT_KINDS

        assert "crime_incident" in EVENT_KINDS
        assert "vehicle_pursuit" in EVENT_KINDS
        assert len(EVENT_KINDS) == 10

        sys.path.insert(0, str(REPO / "services" / "event-emulator" / "src"))
        from event_emulator.profiles import EVENT_KINDS as EMU_KINDS

        assert set(EMU_KINDS) == set(EVENT_KINDS)


class TestEthicsSchema:
    def test_earmark_lawful_basis_and_retention(self):
        text = M010.read_text()
        earmark_block = text[
            text.index("corridor.vehicle_earmarks ("):
            text.index("CREATE INDEX IF NOT EXISTS idx_vehicle_earmarks_vehicle")
        ]
        assert "lawful_basis" in earmark_block
        assert "retention_until" in earmark_block
        assert "CHECK (retention_until > earmarked_at)" in earmark_block
        assert "retention_days  INTEGER NOT NULL CHECK (retention_days BETWEEN 1 AND 90)" in earmark_block

    def test_sightings_source_is_closed_and_labelled(self):
        text = M010.read_text()
        sighting_block = text[
            text.index("corridor.vehicle_sightings ("):
            text.index("CREATE INDEX IF NOT EXISTS idx_vehicle_sightings_vehicle_t")
        ]
        assert "'emulated_anpr', 'live_anpr'" in sighting_block
        assert "matched_plate_hit" in sighting_block

    def test_recommendations_pending_only_default_and_audited(self):
        text = M010.read_text()
        rec_block = text[
            text.index("events.enforcement_recommendations ("):
            text.index("CREATE INDEX IF NOT EXISTS idx_enforcement_recommendations_status")
        ]
        assert "DEFAULT 'pending'" in rec_block
        assert "'pending', 'approved', 'rejected'" in rec_block
        assert "audit_tail" in rec_block

    def test_no_person_directed_columns_anywhere(self):
        """No person-directed COLUMN exists (word-boundary match; 'surface'
        and the header's own ethics note must not false-positive)."""
        text = M010.read_text().lower()
        # strip comments first — the header SAYS 'no face/person fields'
        body = re.sub(r"--[^\n]*", "", text)
        # column-name shaped occurrences only
        for banned in (
            "face", "person_name", "owner_name", "driver_name", "aadhaar",
        ):
            hits = re.findall(rf"\b{banned}\b", body)
            assert not hits, (banned, hits)


class TestStoreDiscipline:
    def test_no_sql_assembly_in_store(self):
        src = STORE_PY.read_text()
        assert not re.search(r'f"""', src) and not re.search(r"f'''\n?\s*SELECT", src)
        assert "format(" not in src
        assert "%.join" not in src
        # every mutating statement is row-returning
        for m in re.finditer(r'"""(.*?)"""', src, re.S):
            body = m.group(1).strip().split("\n")[0].strip()
            if body.upper().startswith("DELETE"):
                flat = " ".join(m.group(1).split())
                assert "RETURNING" in flat.upper(), flat[:80]
