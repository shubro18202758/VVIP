"""Tests for the live data-plane service (ingest/service.py) and its parts.

Coverage:
    - IngestService payload path for BOTH traffic subjects (signal-ingress
      utf8/uint8 and emulator int64/int8 wire schemas): hot-cache keys,
      DB buffer, emulated rows forced 'synthetic', drop accounting, and
      the /health ``ingest`` block shape.
    - Units consumer (contract C-A): the emulator's exact agency_unit
      document → ``units:emulated:{unit_id}`` (EX 30), status/agency
      mapping, replay header → run_id, counted drops.
    - Anomaly consumer (contract C-H): flags → anomaly_log rows with the
      CHECK vocabulary, never 'anomalous'; one row per segment/flag/minute;
      flagless (pre-C-H) payloads counted and skipped.
    - Partition manager: pure scheduling/naming helpers, plus a DB tier
      that runs a full maintenance pass INSIDE an outer transaction that is
      always rolled back (needs VVIP_DATABASE_URL + VEER_TEST_DB_WRITES=1,
      scratch databases only).
    - End to end over a REAL NATS server when INGEST_TEST_NATS_URL is set
      (a private server — never the shared bus: the test publishes).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest

from traffic_oracle.anomaly.stream_detector import (
    ANOMALY_FLAG_VOCABULARY,
    INSERT_ANOMALIES_SQL,
    AnomalyStreamConsumer,
    normalize_flag,
)
from traffic_oracle.ingest.partitions import partition_name, seconds_until_next_run
from traffic_oracle.ingest.service import IngestService
from traffic_oracle.ingest.units_consumer import (
    UNIT_SOURCE,
    UnitDocError,
    UnitsConsumer,
    normalize_unit_doc,
)

from conftest import MockValkey
from test_ingest_pipeline import (
    PipelineValkey,
    RecordingPool,
    emulated_ipc,
    live_ipc,
    make_cache,
    now_ms,
)


def make_service(pool=None, valkey=None, units_valkey=None, **kw) -> IngestService:
    valkey = valkey or PipelineValkey()
    return IngestService(
        nats_url="nats://127.0.0.1:1",  # never connected in unit tests
        pool=pool or RecordingPool(),
        cache=make_cache(valkey),
        valkey_client=units_valkey or MockValkey(),
        **kw,
    )


# ─── Traffic payloads through the service ─────────────────────────────────────


class TestTrafficPayloads:
    async def test_live_payload_reaches_cache_and_db_buffer(self):
        valkey = PipelineValkey()
        service = make_service(valkey=valkey)
        ts = now_ms()
        accepted = await service.ingest_traffic_payload(
            "corridor.traffic.live",
            live_ipc([(ts, "1001", 41.0, 0.35, 0, 0), (ts, "1002", 12.0, 0.8, 2, 1)]),
        )
        assert accepted == 2
        assert set(valkey._store) == {"traffic:latest:1001", "traffic:latest:1002"}
        assert valkey._ttls["traffic:latest:1001"] == 300
        assert service._db_writer.pending_rows == 2

    async def test_emulated_payload_is_persisted_as_synthetic(self):
        pool = RecordingPool(known={1001, 1015})
        valkey = PipelineValkey()
        service = make_service(pool=pool, valkey=valkey)
        ts = now_ms()
        await service.ingest_traffic_payload(
            "corridor.traffic.emulated",
            emulated_ipc([(ts, 1001, 18.0, 0.9, 1), (ts, 1015, 44.0, 0.3, 0)]),
        )
        # the emulator's rows live in their OWN key space: the live plane's
        # latest row is never overwritten by showcase traffic (WP-19)
        assert set(valkey._store) == {
            "traffic:emulated:latest:1001",
            "traffic:emulated:latest:1015",
        }
        cached = json.loads(valkey._store["traffic:emulated:latest:1001"])
        assert cached["data_quality"] == "synthetic"
        assert cached["origin"] == "emulated"
        assert valkey._ttls["traffic:emulated:latest:1001"] == 300
        await service._flush_once()
        (_query, args), = pool.conn.calls
        assert args[0] == [1001, 1015]
        assert args[5] == ["synthetic", "synthetic"]  # data_quality column

    async def test_emulated_traffic_never_overwrites_the_live_plane(self):
        valkey = PipelineValkey()
        service = make_service(valkey=valkey)
        ts = now_ms()
        await service.ingest_traffic_payload(
            "corridor.traffic.live", live_ipc([(ts, "1001", 41.0, 0.35, 2, 2)])
        )
        # a LATER emulated row on the same segment (it used to win)
        await service.ingest_traffic_payload(
            "corridor.traffic.emulated", emulated_ipc([(ts + 500, 1001, 6.0, 0.95, 0)])
        )
        live = json.loads(valkey._store["traffic:latest:1001"])
        emulated = json.loads(valkey._store["traffic:emulated:latest:1001"])
        assert (live["speed_kmh"], live["data_quality"]) == (41.0, "synthetic")
        assert "origin" not in live  # the live row keeps its exact shape
        assert (emulated["speed_kmh"], emulated["origin"]) == (6.0, "emulated")
        # /health cache_keys counts the live plane only
        assert (await service.health())["cache_keys"] == 1

    async def test_the_emulated_plane_is_named_explicitly(self, monkeypatch):
        # The emulated cache plane never rests on normalize's inference
        # (forced quality => 'emulated'): the service names the origin of
        # every emulated batch itself, and leaves the live plane's alone.
        from traffic_oracle.ingest import service as service_mod

        seen: list[tuple[str | None, str | None]] = []
        real = service_mod.normalize_batch

        def spy(df, **kw):
            seen.append((kw.get("origin"), getattr(kw.get("force_quality"), "value", None)))
            return real(df, **kw)

        monkeypatch.setattr(service_mod, "normalize_batch", spy)
        valkey = PipelineValkey()
        service = make_service(valkey=valkey)
        ts = now_ms()
        await service.ingest_traffic_payload(
            "corridor.traffic.emulated", emulated_ipc([(ts, 1001, 18.0, 0.9, 1)])
        )
        await service.ingest_traffic_payload(
            "corridor.traffic.live", live_ipc([(ts, "1002", 41.0, 0.35, 0, 0)])
        )
        assert seen == [("emulated", "synthetic"), (None, None)]
        assert json.loads(valkey._store["traffic:emulated:latest:1001"])["origin"] == "emulated"
        assert "traffic:latest:1002" in valkey._store

    async def test_corrupt_and_unrepresentable_payloads_are_counted(self):
        service = make_service()
        assert await service.ingest_traffic_payload("corridor.traffic.live", b"not arrow") == 0
        ts = now_ms()
        await service.ingest_traffic_payload(
            "corridor.traffic.live",
            live_ipc([(ts, "x1", 40.0, 0.3, 0, 0), (ts + 3_600_000, "1001", 40.0, 0.3, 0, 0)]),
        )
        health = await service.health()
        assert health["rows_dropped"] == {
            "decode:invalid_arrow_ipc": 1,
            "normalize:future_timestamp": 1,
            "normalize:invalid_segment_id": 1,
        }

    async def test_unknown_segments_dropped_at_flush_are_reported(self):
        service = make_service(pool=RecordingPool(known={1001}))
        ts = now_ms()
        await service.ingest_traffic_payload(
            "corridor.traffic.live",
            live_ipc([(ts, "1001", 40.0, 0.3, 0, 0), (ts, "4242", 40.0, 0.3, 0, 0)]),
        )
        await service._flush_once()
        health = await service.health()
        assert health["rows_written_5m"] == 1
        assert health["rows_dropped"]["db:unknown_segment"] == 1


    async def test_buffer_overflow_while_db_is_down_is_reported_once(self):
        pool = RecordingPool(fail=True)
        service = make_service(pool=pool)
        service._db_writer._max_buffer_rows = 2
        ts = now_ms()
        for sid in ("1001", "1002", "1003"):
            await service.ingest_traffic_payload(
                "corridor.traffic.live", live_ipc([(ts, sid, 40.0, 0.3, 0, 0)])
            )
        with pytest.raises(ConnectionError):
            await service._flush_once()
        with pytest.raises(ConnectionError):
            await service._flush_once()  # second failure must not double count
        health = await service.health()
        assert health["rows_dropped"] == {"db:buffer_overflow": 1}
        assert health["pending_rows"] == 2
        pool.conn.fail = False
        await service._flush_once()
        assert (await service.health())["rows_written_5m"] == 2


class TestHealthBlock:
    async def test_health_carries_the_contract_keys(self):
        service = make_service()
        health = await service.health()
        for key in ("connected", "subjects", "msgs_per_min", "last_msg_age_sec",
                    "rows_written_5m", "cache_keys"):
            assert key in health
        assert health["connected"] is False
        assert set(health["subjects"]) == {
            "corridor.traffic.live",
            "corridor.traffic.emulated",
            "corridor.traffic.anomaly",
            "corridor.units.emulated",
        }
        assert health["last_msg_age_sec"] is None  # nothing received yet
        assert health["cache_keys"] == 0

    async def test_live_freshness_is_never_borrowed_from_the_emulator(self):
        clock = [1000.0]
        service = make_service(clock=lambda: clock[0])

        class Emulated:
            subject = "corridor.traffic.emulated"
            headers = None
            data = emulated_ipc([(now_ms(), 1001, 40.0, 0.3, 2, 0)])

        await service._on_traffic(Emulated())
        clock[0] += 1.0
        health = await service.health()
        assert health["last_msg_age_sec"] is None  # signal-ingress is dark
        assert health["emulated_last_msg_age_sec"] == 1.0

    async def test_subscribed_needs_a_live_connection(self):
        service = make_service()
        service._subscribed = True  # subscribed earlier, connection now gone
        health = await service.health()
        assert health["connected"] is False and health["subscribed"] is False

    async def test_handler_failures_are_counted_not_raised_into_nats(self):
        service = make_service()

        async def boom(*_a, **_k):
            raise RuntimeError("handler bug")

        service.ingest_traffic_payload = boom
        service._anomalies.handle = lambda data: (_ for _ in ()).throw(KeyError("x"))
        service._units.handle = boom

        class Msg:
            headers = None
            data = b"{}"

            def __init__(self, subject: str) -> None:
                self.subject = subject

        await service._on_traffic(Msg("corridor.traffic.live"))
        await service._on_anomaly(Msg("corridor.traffic.anomaly"))
        await service._on_unit(Msg("corridor.units.emulated"))
        await service._on_traffic(Msg("corridor.not.subscribed"))  # unknown subject
        health = await service.health()
        assert health["callback_errors"] == 4
        assert health["subjects"]["corridor.traffic.live"]["errors"] == 1
        assert health["subjects"]["corridor.traffic.anomaly"]["errors"] == 1
        assert health["subjects"]["corridor.units.emulated"]["errors"] == 1
        assert health["nats"]["connect_errors"] == 0  # never mislabelled

    async def test_error_fields_carry_class_names_only(self):
        pool = RecordingPool(fail=True)
        service = make_service(pool=pool)
        await service.ingest_traffic_payload(
            "corridor.traffic.live", live_ipc([(now_ms(), "1001", 40.0, 0.3, 0, 0)])
        )
        service._flush_now.set()
        service._stopping.clear()
        task = asyncio.create_task(service._flusher())
        for _ in range(50):
            await asyncio.sleep(0.01)
            if service._flush_errors:
                break
        service._stopping.set()
        service._flush_now.set()
        await asyncio.wait_for(task, timeout=2)
        health = await service.health()
        assert health["flush_errors"] >= 1
        assert health["last_flush_error"] == "ConnectionError"  # not "db down"

    async def test_emulated_subjects_can_be_switched_off(self):
        service = make_service(emulated=False)
        assert service.subjects == ["corridor.traffic.live", "corridor.traffic.anomaly"]

    async def test_message_rate_and_age_come_from_received_messages(self):
        clock = [1000.0]
        service = make_service(clock=lambda: clock[0])

        class Msg:
            subject = "corridor.traffic.live"
            headers = None
            data = live_ipc([(now_ms(), "1001", 40.0, 0.3, 0, 0)])

        await service._on_traffic(Msg())
        clock[0] += 3.0
        await service._on_traffic(Msg())
        clock[0] += 2.0
        health = await service.health()
        assert health["subjects"]["corridor.traffic.live"]["messages"] == 2
        assert health["msgs_per_min"] == 2
        assert health["last_msg_age_sec"] == 2.0
        clock[0] += 61.0
        assert (await service.health())["msgs_per_min"] == 0


# ─── Units (contract C-A) ─────────────────────────────────────────────────────


#: The exact document runner._deliver_agency_unit publishes today.
EMULATOR_UNIT_DOC = {
    "agency": "jan_rakshak",
    "position": [72.571234, 23.025678],
    "source": "emulated",
    "status": "patrolling",
    "unit_id": "JR-EM-01",
}


class TestUnitsConsumer:
    def test_emulator_doc_maps_onto_contract_c_a(self):
        received = datetime(2026, 10, 22, 12, 56, 14, 174000, tzinfo=timezone.utc)
        unit = normalize_unit_doc(EMULATOR_UNIT_DOC, received_at=received)
        assert unit == {
            "unit_id": "JR-EM-01",
            "agency": "jan_rakshak",
            "label": "Jan Rakshak JR-EM-01",
            "lon": 72.571234,
            "lat": 23.025678,
            "status": "patrol",
            "heading": None,
            "speed_kmh": None,
            "t": "2026-10-22T12:56:14.174Z",
            "source": UNIT_SOURCE,
            "run_id": None,
        }

    def test_optional_fields_and_aliases(self):
        unit = normalize_unit_doc(
            {
                "unit_id": "TP-07",
                "agency": "traffic_police",
                "lon": 72.6,
                "lat": 23.0,
                "status": "Dispatched",
                "heading": 370.0,
                "speed_kmh": 31.26,
                "t": "2026-10-22T13:00:00+05:30",
                "label": "Traffic Police TP-07",
                "run_id": 42,
            },
            replay_run_id="9",
        )
        assert unit["agency"] == "traffic"
        assert unit["status"] == "en_route"
        assert unit["heading"] == 10.0
        assert unit["speed_kmh"] == 31.3
        assert unit["t"] == "2026-10-22T07:30:00.000Z"
        assert unit["run_id"] == "42"  # the doc's own run id wins
        assert unit["source"] == "emulated_patrol"  # never relabelled live

    @pytest.mark.parametrize(
        ("doc", "reason"),
        [
            ({**EMULATOR_UNIT_DOC, "agency": "army"}, "unknown_agency"),
            ({**EMULATOR_UNIT_DOC, "status": "sleeping"}, "unknown_status"),
            ({**EMULATOR_UNIT_DOC, "unit_id": "bad id with spaces"}, "invalid_unit_id"),
            ({**EMULATOR_UNIT_DOC, "position": [200.0, 23.0]}, "invalid_position"),
            ({**EMULATOR_UNIT_DOC, "position": None}, "invalid_position"),
            (["not", "a", "dict"], "not_an_object"),
        ],
    )
    def test_unrepresentable_docs_are_dropped_with_a_reason(self, doc, reason):
        with pytest.raises(UnitDocError) as err:
            normalize_unit_doc(doc)
        assert err.value.reason == reason

    def test_out_of_range_numbers_degrade_to_null_not_a_crash(self):
        unit = normalize_unit_doc(
            {**EMULATOR_UNIT_DOC, "heading": 10**400, "speed_kmh": -1e400,
             "run_id": "r" * 500}
        )
        assert unit["heading"] is None and unit["speed_kmh"] is None
        assert unit["run_id"] == "r" * 64  # bounded

    async def test_handle_writes_the_valkey_key_with_ttl_30(self):
        valkey = MockValkey()
        consumer = UnitsConsumer(valkey)
        ok = await consumer.handle(
            json.dumps(EMULATOR_UNIT_DOC).encode(),
            headers={"X-Emulation-Replay": "17"},
        )
        assert ok
        stored = json.loads(valkey._store["units:emulated:JR-EM-01"])
        assert valkey._ttls["units:emulated:JR-EM-01"] == 30
        assert stored["run_id"] == "17"  # replay header → run_id
        assert stored["status"] == "patrol"
        assert await consumer.handle(b"{not json") is False
        assert await consumer.handle(json.dumps({"unit_id": "X"}).encode()) is False
        assert consumer.dropped == Counter({"invalid_json": 1, "unknown_agency": 1})
        assert consumer.written_total == 1


# ─── Anomalies (contract C-H) ─────────────────────────────────────────────────


def anomaly_payload(flags, segment_id="1001", ts=None) -> bytes:
    doc = {
        "timestamp_ms": ts or now_ms(),
        "lon": 72.58,
        "lat": 23.02,
        "segment_id": segment_id,
        "speed_kmh": 12.0,
        "congestion_index": 0.8,
        "source": "FleetGps",
        "data_quality": "Anomalous",
        "confidence": 0.5,
    }
    if flags is not None:
        doc["anomaly_flags"] = flags
    return json.dumps(doc).encode()


class TestAnomalyConsumer:
    def test_flag_spellings_normalise_onto_the_check_vocabulary(self):
        assert normalize_flag("speed_jump") == "speed_jump"
        assert normalize_flag("SpeedJump") == "speed_jump"
        assert normalize_flag("SpeedZScore") == "speed_zscore"
        assert normalize_flag("road-class-mismatch") == "road_class_mismatch"
        assert normalize_flag("anomalous") is None
        assert normalize_flag("isolation_forest") is None  # Tier-2 only
        assert normalize_flag(7) is None
        assert len(ANOMALY_FLAG_VOCABULARY) == 8

    def test_flags_become_rows_and_anomalous_never_does(self):
        consumer = AnomalyStreamConsumer(pool=None)
        queued = consumer.handle(anomaly_payload(["speed_jump", "spatial_mismatch", "bogus"]))
        assert queued == 2
        types = [r[2] for r in consumer._rows]
        severities = [r[3] for r in consumer._rows]
        assert types == ["speed_jump", "spatial_mismatch"]
        assert severities == ["medium", "high"]  # mirrors flags.rs severity()
        assert "anomalous" not in types
        assert consumer.counters["unknown_flag"] == 1
        assert json.loads(consumer._rows[0][4])["data_quality"] == "Anomalous"  # details

    def test_flagless_pre_c_h_payloads_are_counted_and_skipped(self):
        consumer = AnomalyStreamConsumer(pool=None)
        assert consumer.handle(anomaly_payload(None)) == 0
        assert consumer.handle(anomaly_payload([])) == 0
        assert consumer.handle(anomaly_payload(["anomalous"])) == 0
        assert consumer.counters["no_valid_flag"] == 3
        assert consumer.pending_rows == 0

    def test_one_row_per_segment_flag_and_minute(self):
        clock = [1_792_676_160.0]  # wall clock (s), start of a minute
        consumer = AnomalyStreamConsumer(pool=None, clock=lambda: clock[0])
        for second in range(0, 50, 5):  # ten stuck-sensor readings in one minute
            clock[0] = 1_792_676_160.0 + second
            consumer.handle(anomaly_payload(["phantom_reading"], ts=int(clock[0] * 1000)))
        consumer.handle(anomaly_payload(["phantom_reading"], segment_id="1002"))
        clock[0] += 60.0
        consumer.handle(anomaly_payload(["phantom_reading"], ts=int(clock[0] * 1000)))
        assert consumer.pending_rows == 3
        assert consumer.counters["duplicate"] == 9

    def test_a_repeated_stale_timestamp_cannot_flood_the_log(self):
        # A stuck sensor re-sending one OLD timestamp: rate-limited per
        # arrival minute, not re-logged on every message.
        clock = [1_792_676_160.0]
        consumer = AnomalyStreamConsumer(pool=None, clock=lambda: clock[0])
        stale_ts = int((clock[0] - 3 * 3600) * 1000)
        for step in range(120):  # two minutes of 1 Hz repeats
            clock[0] = 1_792_676_160.0 + step
            consumer.handle(anomaly_payload(["stale_data"], ts=stale_ts))
            consumer.handle(anomaly_payload(["speed_zscore"], segment_id="1002"))
        rows = [r for r in consumer._rows if r[2] == "stale_data"]
        assert len(rows) == 2  # one per arrival minute
        assert all(r[1] == stale_ts for r in rows)  # observation time kept

    def test_future_dated_claims_are_clamped_to_arrival(self):
        clock = [1_792_676_160.0]
        consumer = AnomalyStreamConsumer(pool=None, clock=lambda: clock[0])
        claimed = int((clock[0] + 86_400 * 365) * 1000)  # a year ahead
        consumer.handle(anomaly_payload(["future_timestamp"], ts=claimed))
        (row,) = consumer._rows
        assert row[1] == int(clock[0] * 1000)
        assert json.loads(row[4])["timestamp_ms"] == claimed  # kept in details

    @pytest.mark.parametrize(
        ("payload", "reason"),
        [
            # jsonb refuses all three, which would stall every retry of the batch
            (b'{"anomaly_flags":["speed_jump"],"segment_id":"1001",'
             b'"timestamp_ms":1792676160000,"speed_kmh":NaN}', "invalid_json"),
            (b'{"anomaly_flags":["speed_jump"],"segment_id":"1001",'
             b'"timestamp_ms":1792676160000,"speed_kmh":1e400}', "invalid_json"),
            (b'{"anomaly_flags":["speed_jump"],"segment_id":"1001",'
             b'"timestamp_ms":1792676160000,"note":"a\\u0000b"}', "invalid_json"),
            # ids/timestamps that do not fit their columns
            (anomaly_payload(["speed_jump"], segment_id="9" * 25), "invalid_segment_id"),
            (anomaly_payload(["speed_jump"], segment_id="-5"), "invalid_segment_id"),
            (b'{"anomaly_flags":["speed_jump"],"segment_id":"1001",'
             b'"timestamp_ms":1e400}', "invalid_timestamp"),
        ],
    )
    def test_rows_the_db_would_refuse_are_counted_never_queued(self, payload, reason):
        consumer = AnomalyStreamConsumer(pool=None)
        assert consumer.handle(payload) == 0
        assert consumer.counters[reason] == 1
        assert consumer.pending_rows == 0

    async def test_buffer_is_capped_while_the_db_is_down(self):
        pool = RecordingPool(known={1001, 1002, 1003, 1004, 1005}, fail=True)
        consumer = AnomalyStreamConsumer(pool=pool, max_pending_rows=3)
        for sid in ("1001", "1002", "1003", "1004"):
            consumer.handle(anomaly_payload(["stale_data"], segment_id=sid))
        assert consumer.pending_rows == 3
        assert consumer.counters["buffer_overflow"] == 1
        with pytest.raises(ConnectionError):
            await consumer.flush()
        consumer.handle(anomaly_payload(["stale_data"], segment_id="1005"))
        assert consumer.pending_rows == 3  # still capped after a failed flush
        assert consumer.counters["buffer_overflow"] == 2
        # …and surfaced as drops by the service, exactly once
        service = make_service(pool=pool)
        service._anomalies = consumer
        with pytest.raises(ConnectionError):
            await service._flush_once()
        with pytest.raises(ConnectionError):
            await service._flush_once()
        assert (await service.health())["rows_dropped"] == {"anomaly:buffer_overflow": 2}
        assert consumer.last_error == "ConnectionError"
        pool.conn.fail = False
        assert await consumer.flush() == 3
        assert pool.conn.calls[-1][1][0] == [1003, 1004, 1005]  # oldest dropped first

    async def test_flush_is_one_statement_and_failures_keep_rows(self):
        pool = RecordingPool(known={1001})
        consumer = AnomalyStreamConsumer(pool=pool)
        consumer.handle(anomaly_payload(["stale_data"]))
        consumer.handle(anomaly_payload(["stale_data"], segment_id="4242"))
        pool.conn.fail = True
        with pytest.raises(ConnectionError):
            await consumer.flush()
        assert consumer.pending_rows == 2
        pool.conn.fail = False
        assert await consumer.flush() == 1
        query, args = pool.conn.calls[-1]
        assert query == INSERT_ANOMALIES_SQL
        assert args[2] == ["stale_data", "stale_data"] and args[3] == ["low", "low"]
        assert consumer.counters["unknown_segment"] == 1
        assert consumer.pending_rows == 0


# ─── Partitions ───────────────────────────────────────────────────────────────


class TestPartitionHelpers:
    def test_partition_name_is_daily_utc(self):
        assert partition_name(datetime(2026, 10, 22).date()) == "observations_2026_10_22"

    def test_rollup_runs_shortly_after_each_utc_hour(self):
        base = datetime(2026, 10, 22, 13, 0, 30, tzinfo=timezone.utc)
        assert seconds_until_next_run(base, offset_sec=90) == 60.0
        assert seconds_until_next_run(base + timedelta(minutes=5), offset_sec=90) == 3360.0
        ist = timezone(timedelta(hours=5, minutes=30))
        assert seconds_until_next_run(base.astimezone(ist), offset_sec=90) == 60.0


_DSN = os.environ.get("VVIP_DATABASE_URL")
_WRITES = os.environ.get("VEER_TEST_DB_WRITES") == "1"


class _TxPool:
    """Single connection inside an OUTER transaction: every PartitionManager
    transaction becomes a savepoint, and the outer one is rolled back."""

    def __init__(self, conn) -> None:
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


@pytest.mark.skipif(
    not (_DSN and _WRITES),
    reason="partition DDL tier needs VVIP_DATABASE_URL + VEER_TEST_DB_WRITES=1 (scratch DB)",
)
class TestPartitionManagerOnDb:
    async def test_full_pass_creates_moves_rolls_up_and_retains(self):
        asyncpg = pytest.importorskip("asyncpg")
        from traffic_oracle.ingest.partitions import PartitionManager

        conn = await asyncpg.connect(_DSN, timeout=5)
        tx = conn.transaction()
        await tx.start()
        try:
            day = datetime(2031, 3, 4, tzinfo=timezone.utc)  # no partition exists
            sid = await conn.fetchval("SELECT min(segment_id) FROM corridor.road_segments")
            # Two real rows parked in DEFAULT for that day (+ one anomalous).
            for minute, quality in ((5, "real"), (10, "real"), (15, "anomalous")):
                await conn.execute(
                    "INSERT INTO traffic.observations (segment_id, timestamp_utc, speed_kmh, "
                    "congestion_idx, source, data_quality, confidence) "
                    "VALUES ($1, $2, 30, 0.4, 'government', $3, 1.0)",
                    sid, day + timedelta(hours=11, minutes=minute), quality,
                )
            manager = PartitionManager(_TxPool(conn), retention_days=14)
            report = await manager.run(now=day + timedelta(hours=12, minutes=1))
            assert report.errors == []
            assert report.created == [
                "traffic.observations_2031_03_04",
                "traffic.observations_2031_03_05",
                "traffic.observations_2031_03_06",
            ]
            assert report.rows_moved_from_default == 3
            where = await conn.fetch(
                "SELECT tableoid::regclass::text AS t FROM traffic.observations "
                "WHERE timestamp_utc >= $1 AND timestamp_utc < $1 + interval '1 day'",
                day,
            )
            assert {r["t"] for r in where} == {"traffic.observations_2031_03_04"}
            # Rollup of 11:00-12:00 aggregates the two REAL rows only.
            assert report.rollup_rows == 1
            agg = await conn.fetchrow(
                "SELECT observation_cnt FROM traffic.hourly_aggregates "
                "WHERE segment_id = $1 AND hour_utc = $2",
                sid, day + timedelta(hours=11),
            )
            assert agg["observation_cnt"] == 2
            # Idempotent: a second pass creates nothing.
            again = await manager.run(now=day + timedelta(hours=12, minutes=2))
            assert again.created == [] and again.errors == []
            # Retention: 20 days later the 2031-03-04..06 partitions go.
            later = await manager.run(now=day + timedelta(days=20))
            assert {
                "traffic.observations_2031_03_04",
                "traffic.observations_2031_03_05",
                "traffic.observations_2031_03_06",
            } <= set(later.dropped)
        finally:
            await tx.rollback()  # ALWAYS: nothing above persists
            await conn.close()


# ─── End to end over a real (private) NATS server ─────────────────────────────


_NATS = os.environ.get("INGEST_TEST_NATS_URL")


@pytest.mark.skipif(
    not _NATS, reason="INGEST_TEST_NATS_URL not set (private NATS server — this test publishes)"
)
class TestEndToEndOverNats:
    async def test_all_four_subjects_flow_through_one_connection(self):
        import nats

        valkey = PipelineValkey()
        units = MockValkey()
        pool = RecordingPool(known={1001, 1002, 1015})
        service = IngestService(
            nats_url=_NATS, pool=pool, cache=make_cache(valkey), valkey_client=units,
            flush_interval_sec=0.5,
        )
        service._partitions.run = _noop_partitions  # no DB in this tier
        service.start()
        try:
            for _ in range(50):
                if (await service.health())["subscribed"]:
                    break
                await asyncio.sleep(0.1)
            nc = await nats.connect(_NATS)
            ts = now_ms()
            await nc.publish("corridor.traffic.live", live_ipc([(ts, "1001", 33.0, 0.4, 0, 0)]))
            await nc.publish(
                "corridor.traffic.emulated", emulated_ipc([(ts, 1015, 21.0, 0.7, 0)])
            )
            await nc.publish("corridor.traffic.anomaly", anomaly_payload(["speed_jump"]))
            await nc.publish(
                "corridor.units.emulated", json.dumps(EMULATOR_UNIT_DOC).encode()
            )
            await nc.flush()
            await nc.close()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not (
                "units:emulated:JR-EM-01" in units._store
                and service._db_writer.rows_written_total >= 2
                and service._anomalies.rows_written_total >= 1
            ):
                await asyncio.sleep(0.1)
            health = await service.health()
            assert health["connected"] is True
            assert {"traffic:latest:1001", "traffic:latest:1015"} <= set(valkey._store)
            assert "units:emulated:JR-EM-01" in units._store
            assert health["last_msg_age_sec"] is not None and health["last_msg_age_sec"] < 10
            assert health["rows_written_5m"] == 2
            queries = [q for q, _ in pool.conn.calls]
            assert INSERT_ANOMALIES_SQL in queries
        finally:
            await service.stop()
        assert (await service.health())["connected"] is False


async def _noop_partitions(now=None):
    return None
