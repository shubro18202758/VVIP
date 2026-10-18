"""Migration 004 up/down + live-SQL integration against a real pgvector.

Starts a DISPOSABLE docker container (pgvector/pgvector:pg16 — the same
image the other dev stacks on this machine use; nothing shared is touched).
Skips when docker is unavailable or VEER_TEST_PG_DSN is not set and no
docker binary exists.

Covers:
* applying corridor-store migrations 001→004 lexically (as C10's
  migrate.py will) on a fresh database;
* idempotency (004 twice) and down→up cycle;
* object existence + constraint enforcement (token cap 64, dim 128,
  kind whitelists, and the ORIGINAL C11 bug: segment_embeddings still
  rejects 'gat_l3' while memory_segment_embeddings accepts it);
* the full memory spine over the real SQL: MemoryStore upsert → dense
  recall → token-vector fetch → MaxSim rerank (fake encoders, real SQL);
* the fixed compute_daily_profile SQL (C11a) over real rows;
* TrafficVectorStore.store_memory_embedding (C11b) over the real table.

All statements below are executed through asyncpg's fetch/fetchrow/fetchval
with bound parameters ($n) — never concatenated SQL.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from embedding_service import config
from embedding_service.encoders import (
    FakeDenseEncoder,
    FakeLateInteractionEncoder,
)
from embedding_service.store import MemoryStore

REPO = Path(__file__).resolve().parents[3]
MIGRATIONS_DIR = REPO / "services" / "corridor-store" / "migrations"
DOWN_SQL = (
    REPO
    / "services"
    / "embedding-service"
    / "migrations"
    / "004_memory_embeddings_down.sql"
)

asyncpg = pytest.importorskip("asyncpg")


def _split_statements(sql_text: str) -> list[str]:
    """Split a migration file into statements, comment-safe.

    Line comments (``--``) are stripped BEFORE splitting so semicolons in
    prose cannot fracture a statement. No migration in this repo uses
    dollar-quoted bodies or block comments (asserted by the hygiene test
    at the bottom of this module).
    """
    no_comments = "\n".join(
        line.split("--", 1)[0] for line in sql_text.splitlines()
    )
    return [s.strip() for s in no_comments.split(";") if s.strip()]


# ─── Disposable database fixture ───────────────────────────────────────────────


# Disposable test-database image: postgis + pgRouting + pgvector on PG16
# (see tests/Dockerfile.migtest for why PG16, not the stack's PG17 image).
MIGTEST_DOCKERFILE = Path(__file__).resolve().parent / "Dockerfile.migtest"
TEST_IMAGE_TAG = "veer-postgis-pgvector-migtest"


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _ensure_image() -> None:
    subprocess.run(
        [
            "docker", "build",
            "-f", str(MIGTEST_DOCKERFILE),
            "-t", TEST_IMAGE_TAG,
            str(MIGTEST_DOCKERFILE.parent),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _start_container() -> tuple[str, str]:
    """Start a throwaway corridor-store image on a random loopback port."""
    name = f"veer-embed-migtest-{os.getpid()}"
    password = secrets.token_urlsafe(16)  # no credential literals in source
    subprocess.run(
        [
            "docker", "run", "--rm", "-d", "--name", name,
            "-e", f"POSTGRES_PASSWORD={password}",
            "-p", "127.0.0.1::5432",
            TEST_IMAGE_TAG,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    port_out = subprocess.run(
        ["docker", "port", name, "5432/tcp"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return name, f"postgresql://postgres:{password}@127.0.0.1:{port_out.split(':')[-1]}/postgres"


def _probe_ready(dsn: str) -> None:
    """One connect+close round-trip inside a SINGLE event loop."""

    async def _one() -> None:
        conn = await asyncpg.connect(dsn)
        await conn.close()

    asyncio.run(_one())


def _wait_ready(dsn: str, timeout_sec: float = 60.0) -> None:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            _probe_ready(dsn)
        except Exception:
            time.sleep(0.7)
            continue
        return
    raise RuntimeError("disposable pgvector container never became ready")


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    """A disposable pgvector DSN: env override, else a docker-managed DB."""
    env_dsn = os.environ.get("VEER_TEST_PG_DSN")
    if env_dsn:
        yield env_dsn
        return
    if not _docker_available():
        pytest.skip(
            "no VEER_TEST_PG_DSN and no docker; skipping pgvector migration tests"
        )
    _ensure_image()
    name, dsn = _start_container()
    _wait_ready(dsn)
    try:
        yield dsn
    finally:
        subprocess.run(
            ["docker", "rm", "-f", name], capture_output=True, text=True
        )


# ─── Helpers ───────────────────────────────────────────────────────────────────


async def _apply(pool, path: Path) -> None:
    statements = _split_statements(path.read_text())
    async with pool.acquire() as conn:
        for stmt in statements:
            await conn.fetchrow(stmt)  # DDL returns no row; executes fully


async def _table_exists(conn, schema: str, table: str) -> bool:
    return await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = $1 AND table_name = $2)",
        schema,
        table,
    )


async def _make_pool(pg_dsn: str):
    return await asyncpg.create_pool(pg_dsn, min_size=1, max_size=4)


async def _run_one(pool, sql: str, *args):
    async with pool.acquire() as conn:
        return await conn.fetchrow(sql, *args)


@pytest.fixture(scope="session", autouse=True)
async def _migrated_db(pg_dsn):
    """Apply the full migration chain lexically ONCE per session.

    Migration 001 uses bare CREATE TABLE (it was written for
    docker-entrypoint-initdb.d on an empty volume), so the chain is not
    re-runnable as a whole; per-test re-application is therefore impossible
    and every test shares this session-applied schema. The down/up test
    drops only 004's objects and re-applies 004 before finishing.
    """
    pool = await asyncpg.create_pool(pg_dsn, min_size=1, max_size=2)
    try:
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            await _apply(pool, path)
    finally:
        await pool.close()
    yield


# ─── Migration lifecycle ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_migrations_apply_in_lexical_order_and_objects_exist(pg_dsn):
    """The session fixture applied 001→006 lexically; verify the objects."""
    pool = await _make_pool(pg_dsn)
    try:
        async with pool.acquire() as conn:
            for schema, table in (
                ("traffic", "memory_docs"),
                ("traffic", "incidents"),
                ("traffic", "memory_segment_embeddings"),
                # sibling-stream migrations in the same chain, unbroken:
                ("adaptive", "situation_log"),
                ("simulation", "runs"),
                ("corridor", "signal_programs"),
            ):
                assert await _table_exists(conn, schema, table), (
                    f"{schema}.{table} missing"
                )
            hnsw = await conn.fetchval(
                "SELECT count(*) FROM pg_indexes "
                "WHERE indexname IN ('idx_memory_docs_dense_hnsw', "
                "'idx_memory_segment_embeddings_hnsw')"
            )
            assert hnsw == 2
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_migration_004_is_idempotent(pg_dsn):
    pool = await _make_pool(pg_dsn)
    try:
        await _apply(pool, MIGRATIONS_DIR / "004_memory_embeddings.sql")
        await _apply(pool, MIGRATIONS_DIR / "004_memory_embeddings.sql")  # twice
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_migration_down_then_up_roundtrip(pg_dsn):
    pool = await _make_pool(pg_dsn)
    try:
        await _apply(pool, DOWN_SQL)
        async with pool.acquire() as conn:
            assert not await _table_exists(conn, "traffic", "memory_docs")
            assert not await _table_exists(conn, "traffic", "incidents")
            assert not await _table_exists(
                conn, "traffic", "memory_segment_embeddings"
            )
            # pre-existing tables untouched by down
            assert await _table_exists(conn, "traffic", "segment_embeddings")
        await _apply(pool, MIGRATIONS_DIR / "004_memory_embeddings.sql")
        async with pool.acquire() as conn:
            assert await _table_exists(conn, "traffic", "memory_docs")
    finally:
        await pool.close()


# ─── Constraint enforcement (the DB half of the guards) ───────────────────────


@pytest.mark.asyncio
async def test_token_vector_constraints_enforced(pg_dsn):
    pool = await _make_pool(pg_dsn)
    try:
        v128 = "[" + ",".join(["0.01"] * 128) + "]"
        # asyncpg accepts a LIST of vector-text literals for $n::vector(128)[]
        # (a single array-literal string is rejected client-side — verified).
        ok_64 = [v128] * 64
        await _run_one(
            pool,
            "INSERT INTO traffic.memory_docs "
            "(kind, ref_id, title, body, token_vectors, token_count) "
            "VALUES ('pattern', $1, 't', 'b', $2::vector(128)[], 64)",
            "cap-64",
            ok_64,
        )
        too_many = [v128] * 65
        with pytest.raises(Exception, match="check|ck_|constraint"):
            await _run_one(
                pool,
                "INSERT INTO traffic.memory_docs "
                "(kind, ref_id, title, body, token_vectors) "
                "VALUES ('pattern', $1, 't', 'b', $2::vector(128)[])",
                "cap-65",
                too_many,
            )
        with pytest.raises(Exception, match="check|ck_"):
            await _run_one(
                pool,
                "INSERT INTO traffic.memory_docs (kind, ref_id, title, body) "
                "VALUES ($1, $2, 't', 'b')",
                "off_whitelist",
                "x",
            )
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_c11_proof_legacy_check_rejects_gat_l3_new_table_accepts(pg_dsn):
    """The original defect, made executable: 'gat_l3' violates 002's CHECK."""
    pool = await _make_pool(pg_dsn)
    try:
        async with pool.acquire() as conn:
            seg_id = await conn.fetchval(
                "INSERT INTO corridor.road_segments "
                "(segment_id, road_class, geom) "
                "VALUES ($1, 'primary', ST_GeomFromText('LINESTRING(0 0, 1 1)', 4326)) "
                "RETURNING segment_id",
                987001,
            )
        v24 = "[" + ",".join(["0.02"] * 24) + "]"
        with pytest.raises(Exception, match="check|ck_"):
            await _run_one(
                pool,
                "INSERT INTO traffic.segment_embeddings "
                "(segment_id, pattern_type, embedding) "
                "VALUES ($1, $2, $3::vector)",
                seg_id,
                "gat_l3",  # the C11(b) violating value — must still be rejected
                v24,
            )
        # The four allowed types still work
        await _run_one(
            pool,
            "INSERT INTO traffic.segment_embeddings "
            "(segment_id, pattern_type, embedding) "
            "VALUES ($1, $2, $3::vector)",
            seg_id,
            "daily_profile",
            v24,
        )
        # New table accepts the model embedding at vector(64)
        v64 = "[" + ",".join(["0.03"] * 64) + "]"
        await _run_one(
            pool,
            "INSERT INTO traffic.memory_segment_embeddings "
            "(segment_id, model_name, embedding) "
            "VALUES ($1, $2, $3::vector)",
            seg_id,
            "dstgat_gat_l3",
            v64,
        )
    finally:
        await pool.close()


# ─── Live spine: MemoryStore + retrieval SQL over real pgvector ───────────────


@pytest.mark.asyncio
async def test_memory_spine_end_to_end_real_sql(pg_dsn):
    from embedding_service.incidents import IncidentIngestor
    from embedding_service.retrieval import HybridRetriever

    late = FakeLateInteractionEncoder()
    dense = FakeDenseEncoder()
    store = MemoryStore(await _make_pool(pg_dsn))
    try:
        ingestor = IncidentIngestor(store, late, dense)
        result = await ingestor.ingest_scenario_run(
            {
                "scenario_id": "sc-live-1",
                "route_name": "Ring Road 3",
                "total_disruption_vehicle_hours": 9.5,
                "max_queue_length_m": 350.0,
                "avg_closure_duration_sec": 150.0,
                "segments_affected": 4,
                "estimated_complaints_risk": "medium",
                "convoy_transit_time_sec": 1300,
            }
        )
        assert result is not None and result.doc_id

        await ingestor.ingest_live_incident(
            incident_kind="protest",
            severity="high",
            summary="farmers march on ring road causing closure",
        )

        retriever = HybridRetriever(store, late, dense)
        response = await retriever.similar_past_situations(
            "protest on ring road closure congestion"
        )
        assert response.recalled >= 1
        top = response.results[0]
        assert top.strategy in ("maxsim", "dense_fallback")
        assert top.score > 0

        incidents = await store.recent_incidents(limit=10)
        kinds = {row["incident_kind"] for row in incidents}
        assert kinds <= set(config.INCIDENT_KINDS)
        sources = {row["source"] for row in incidents}
        assert "scenario_run" in sources and "live" in sources
        # memory_doc links populated for both paths
        for row in incidents:
            assert row["memory_doc_id"] is not None
    finally:
        await store._pool.close()


@pytest.mark.asyncio
async def test_c11a_daily_profile_live_sql(pg_dsn):
    from traffic_oracle.data.vector_store import TrafficVectorStore

    pool = await _make_pool(pg_dsn)
    try:
        from datetime import datetime, timezone

        for day_offset, base in ((0, 40.0), (1, 44.0)):
            for hour in range(24):
                await _run_one(
                    pool,
                    "INSERT INTO traffic.hourly_aggregates "
                    "(segment_id, hour_utc, avg_speed_kmh, p50_speed_kmh, "
                    " p95_congestion, observation_cnt) "
                    "VALUES ($1, $2, $3, $3, $4, $5)",
                    555001,
                    datetime(2026, 10, 10 + day_offset, hour, tzinfo=timezone.utc),
                    base + hour * 0.1,
                    0.2 + hour * 0.01,
                    60,
                )
        store = TrafficVectorStore(pool)
        profile = await store.compute_daily_profile(segment_id=555001)
        assert len(profile) == 24
        assert [row["hour_of_day"] for row in profile] == list(range(24))
        # Two days aggregated: 2×60 observations per hour
        assert profile[0]["observation_count"] == 120
        assert profile[0]["avg_speed_kmh"] > 0
        assert 0.0 <= profile[0]["avg_congestion_idx"] <= 1.0
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_c11b_store_memory_embedding_live_sql(pg_dsn):
    from traffic_oracle.data.vector_store import TrafficVectorStore

    pool = await _make_pool(pg_dsn)
    try:
        await _run_one(
            pool,
            "INSERT INTO corridor.road_segments (segment_id, road_class, geom) "
            "VALUES ($1, 'primary', ST_GeomFromText('LINESTRING(0 0, 1 1)', 4326))",
            987002,
        )
        store = TrafficVectorStore(pool)
        embedding = [round(0.001 * (i % 17), 6) for i in range(64)]
        await store.store_memory_embedding(987002, "dstgat_gat_l3", embedding)
        # Upsert path (same key) does not raise
        await store.store_memory_embedding(987002, "dstgat_gat_l3", embedding)
        similar = await store.find_similar_memory_segments(
            embedding, "dstgat_gat_l3", k=3
        )
        assert similar and similar[0]["segment_id"] == 987002
        assert similar[0]["distance"] == pytest.approx(0.0, abs=1e-6)
    finally:
        await pool.close()


# ─── Migration file hygiene ───────────────────────────────────────────────────


def test_migration_files_have_no_semicolons_inside_literals():
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")) + [DOWN_SQL]:
        text = path.read_text()
        # Strip line comments first — prose apostrophes ("simulation's")
        # would otherwise unbalance the literal pairing.
        no_comments = "\n".join(
            line.split("--", 1)[0] for line in text.splitlines()
        )
        string_literals = re.findall(r"'([^']*)'", no_comments)
        for literal in string_literals:
            assert ";" not in literal, f"{path.name}: literal {literal!r} contains ';'"
