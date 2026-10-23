"""The Stolen-Vehicle ANPR Sweep states its premise (C-N) — no network.

The 'watchlist_anpr_sweep' use case drives its FIR-named stolen plate
(GJ27EM9001) through the cameras. On a fresh install no earmark exists, so
its reads used to file PENAL e-challans (speeding, red light) against the
theft victim's car. The run now makes the scenario's premise explicit at
t0, through the oracle's own earmark API:

* the plate gets an ACTIVE stolen_vehicle_fir earmark naming the use case's
  FIR, earmarked_by "event-emulator (system) — scenario precondition", the
  reason labelled EMULATED — so the oracle files evidence-only
  R_STOLEN_SIGHTING for its reads and 0 penal e-challans;
* idempotent: an active stolen-FIR earmark (an operator's) is reused, never
  duplicated; a replay re-checks it;
* fail-closed: when the oracle cannot confirm or take the earmark, that
  plate's reads are SKIPPED (said so in the frame log), never filed as
  penal; every other read is delivered;
* the calls ride the real OracleSeam's guarded client (fixed internal
  endpoint) — pinned with an httpx MockTransport;
* the earmark-exercise plate (GJ27EM4321) is an OPERATOR exercise: the run
  never earmarks it, its reads hit only once a duty officer has — and the
  library text promises nothing a fresh install cannot produce.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

import httpx
from fakes import FakeNats, FakeOracle, FakeStore, FakeTwin, FakeValkey, NoSleep
from test_watchlist import LEGS, PROGRAM, SITES

from event_emulator.library import load_library
from event_emulator.profiles import profile_for
from event_emulator.runner import (
    SCENARIO_EARMARKER,
    RunnerDeps,
    ensure_stolen_earmark,
    execute_replay,
    execute_run,
    stolen_fir_plates,
)
from event_emulator.spec import EmulationSpec
from event_emulator.twin_seam import OracleSeam, SafeHttpClient

UC = "watchlist_anpr_sweep"
STOLEN = "GJ27EM9001"
EXERCISE = "GJ27EM4321"   # the operator earmark-exercise plate
BASE = "http://localhost:8121"  # the seam's internal-endpoint allowlist
PENAL = ("R_SPD_SEGMENT", "R_SIGNAL_VIOLATION")


class _VehiclesClient:
    """The oracle's earmark API over the fake's in-memory earmarks
    (SafeHttpClient's get/post signature)."""

    def __init__(self, oracle: EarmarkingOracle) -> None:
        self.oracle = oracle

    async def get(self, url, *, replay_run_id=None, timeout=None):
        self.oracle.calls.append(("GET", url, None))
        if self.oracle.vehicles_down:
            raise httpx.ConnectError("oracle down")
        vehicles = [{"plate": plate, "lawful_basis": basis, "earmark_reason": reason,
                     "earmark_state": "flagged"}
                    for plate, (basis, reason) in sorted(self.oracle.earmarks.items())]
        return httpx.Response(200, json={"vehicles": vehicles})

    async def post(self, url, *, json_body, replay_run_id=None, timeout=None):
        self.oracle.calls.append(("POST", url, dict(json_body)))
        if self.oracle.refuse_earmark:
            return httpx.Response(422, json={"detail": "earmarked_by is required"})
        plate = url.rsplit("/", 2)[-2]
        self.oracle.earmarks[plate] = (json_body["lawful_basis"], json_body["reason"])
        # the C-N seam: an active stolen-FIR earmark turns reads into hits
        self.oracle.earmarked[plate] = "stolen_vehicle"
        return httpx.Response(200, json={"earmark_id": "e-1", "plate": plate,
                                         "earmarked_by": json_body["earmarked_by"],
                                         "retention_until": "2026-10-30T00:00:00+00:00"})


@dataclass
class EarmarkingOracle(FakeOracle):
    """FakeOracle + the earmark API (GET /vehicles, POST /{plate}/earmark),
    with penal rules for every read above 60 km/h (the fleeing car's)."""

    earmarks: dict = field(default_factory=dict)  # plate -> (lawful_basis, reason)
    calls: list = field(default_factory=list)
    refuse_earmark: bool = False
    vehicles_down: bool = False

    def __post_init__(self) -> None:
        self._client = _VehiclesClient(self)
        self._base = BASE
        if self.rules is None:
            self.rules = lambda read: ["R_SPD_SEGMENT"] if (read.get("speed_kmh") or 0) > 60 else []

    def recommendations_for(self, plate: str) -> list[str]:
        return [rule for s in self.sightings.values() if s["body"]["plate"] == plate
                for rule in s["fired"]]


def _deps(oracle: EarmarkingOracle):
    store = FakeStore(cameras=[dict(s) for s in SITES], programs={3: PROGRAM}, legs=LEGS)
    oracle.store = store
    return RunnerDeps(store=store, valkey=FakeValkey(), twin=FakeTwin(), nats=FakeNats(),
                      oracle=oracle, sleep=NoSleep())


def _spec(**kw):
    params = {"use_case_id": UC, "seed": 42, "t0_anchor": None, "tick_sec": 30.0,
              "clock_scale": 1000.0, "deliver_twin": False}
    params.update(kw)
    return EmulationSpec(**params)


def _run(oracle: EarmarkingOracle):
    deps = _deps(oracle)
    return deps, asyncio.run(execute_run(deps, _spec(), library=load_library()))


class TestFreshInstall:
    def test_no_penal_echallan_for_the_fir_named_stolen_plate(self):
        oracle = EarmarkingOracle()            # a fresh store: no earmark at all
        outcome = _run(oracle)[1]
        assert outcome.status == "completed"
        fired = oracle.recommendations_for(STOLEN)
        assert fired and set(fired) == {"R_STOLEN_SIGHTING"}   # evidence only
        assert not [r for r in fired if r in PENAL]            # 0 penal e-challans
        # the premise, stated once, before the first read
        [pre] = [e for e in outcome.frame_log if e["kind"] == "scenario_precondition"]
        assert pre["result"] == "delivered" and pre["earmark"] == "created"
        assert pre["plate"] == STOLEN and pre["lawful_basis"] == "stolen_vehicle_fir"
        posts = [c for c in oracle.calls if c[0] == "POST"]
        assert [c[1] for c in posts] == [f"{BASE}/api/v1/vehicles/{STOLEN}/earmark"]
        body = posts[0][2]
        assert body["state"] == "flagged" and body["lawful_basis"] == "stolen_vehicle_fir"
        assert body["earmarked_by"] == SCENARIO_EARMARKER == (
            "event-emulator (system) — scenario precondition")
        assert body["reason"].startswith(
            "EMULATED scenario precondition — stolen-vehicle FIR 4401/2026, Kalupur PS names "
            "GJ27EM9001 (event-emulator use case 'watchlist_anpr_sweep', run ")
        assert body["retention_days"] == 7
        first_read = next(i for i, e in enumerate(outcome.frame_log) if e["kind"] == "anpr_read")
        assert outcome.frame_log.index(pre) < first_read
        # the earmark-exercise plate and background traffic are untouched
        assert set(oracle.earmarks) == {STOLEN}

    def test_the_ledger_records_the_precondition(self):
        oracle = EarmarkingOracle()
        deps, outcome = _run(oracle)
        ledger = deps.store.runs[outcome.run_id]["spec"]   # FakeStore keeps the finished doc
        [pre] = ledger["scenario_precondition"]
        assert pre["earmark"] == "created" and pre["fir_ref"] == "FIR 4401/2026, Kalupur PS"

    def test_the_running_row_carries_the_precondition_mid_run(self):
        # The deck's run list reads spec.scenario_precondition: it is merged
        # into the RUNNING row as soon as the premise is settled, before the
        # finish doc — so the panel shows it while the sweep runs.
        oracle = EarmarkingOracle()
        deps, outcome = _run(oracle)
        patches = [doc for rid, doc in deps.store.heartbeats
                   if rid == outcome.run_id and "scenario_precondition" in doc]
        assert len(patches) == 1
        [pre] = patches[0]["scenario_precondition"]
        assert pre["result"] == "delivered" and pre["earmark"] == "created"
        assert pre["plate"] == STOLEN and pre["lawful_basis"] == "stolen_vehicle_fir"
        # merged before the first progress beat that saw a read delivered
        first = next(i for i, (rid, doc) in enumerate(deps.store.heartbeats)
                     if rid == outcome.run_id and "scenario_precondition" in doc)
        assert all(doc.get("progress", {}).get("delivered", 0) == 0
                   for _rid, doc in deps.store.heartbeats[:first])


class TestIdempotent:
    def test_an_operators_active_stolen_fir_earmark_is_reused(self):
        oracle = EarmarkingOracle(
            earmarks={STOLEN: ("stolen_vehicle_fir", "STOLEN — FIR 4401/2026, Kalupur PS")},
            earmarked={STOLEN: "stolen_vehicle"})
        outcome = _run(oracle)[1]
        [pre] = [e for e in outcome.frame_log if e["kind"] == "scenario_precondition"]
        assert pre["earmark"] == "already_active" and pre["result"] == "delivered"
        assert not [c for c in oracle.calls if c[0] == "POST"]   # never duplicated
        assert set(oracle.recommendations_for(STOLEN)) == {"R_STOLEN_SIGHTING"}

    def test_a_replay_rechecks_and_reuses_it(self):
        oracle = EarmarkingOracle()
        deps, first = _run(oracle)
        second = asyncio.run(execute_replay(deps, tape=first.tape, clock_scale=1000.0,
                                            deliver_twin=False, replay_of=first.run_id,
                                            library=load_library()))
        [pre] = [e for e in second.frame_log if e["kind"] == "scenario_precondition"]
        assert pre["earmark"] == "already_active"
        assert len([c for c in oracle.calls if c[0] == "POST"]) == 1


class TestFailClosed:
    def _assert_skipped(self, outcome, oracle):
        [pre] = [e for e in outcome.frame_log if e["kind"] == "scenario_precondition"]
        assert pre["result"] == "failed"
        stolen = [e for e in outcome.frame_log if e["kind"] == "anpr_read"
                  and e["result"] == "skipped" and "scenario precondition not met" in e["reason"]]
        assert stolen                                  # said so, frame by frame
        assert not oracle.recommendations_for(STOLEN)  # nothing filed against the victim
        others = [e for e in outcome.frame_log if e["kind"] == "anpr_read"
                  and e["result"] == "delivered"]
        assert others                                  # the rest of the sweep ran

    def test_a_refused_earmark_skips_the_stolen_plates_reads(self):
        oracle = EarmarkingOracle(refuse_earmark=True)
        outcome = _run(oracle)[1]
        self._assert_skipped(outcome, oracle)

    def test_an_unreachable_oracle_skips_them_too(self):
        oracle = EarmarkingOracle(vehicles_down=True)
        outcome = _run(oracle)[1]
        self._assert_skipped(outcome, oracle)


class TestEarmarkExercise:
    """GJ27EM4321 is an OPERATOR exercise: the emulator drives it past the
    cameras but never earmarks it, so the library text may not promise its
    hit — on a fresh install its reads are ordinary sightings."""

    def test_the_library_promises_no_hit_a_fresh_install_cannot_produce(self):
        cfg = load_library()[UC].config
        assert "earmarked_plate_hit" not in cfg["expected_features"]
        assert "operator_earmark_exercise" in cfg["expected_features"]
        text = cfg["description"]
        assert "EARMARKED plate (hit: earmarked_plate)" not in text
        assert EXERCISE in text and "OPERATOR EXERCISE" in text
        assert "the emulator never earmarks it" in text
        assert "on a fresh install they are ordinary sightings" in text
        # the stolen plate's hit IS produced: its premise is the run's own,
        # labelled EMULATED precondition
        assert "stolen_plate_hit" in cfg["expected_features"]
        assert SCENARIO_EARMARKER in text and "EMULATED precondition" in text

    def test_on_a_fresh_install_its_reads_are_ordinary_sightings(self):
        oracle = EarmarkingOracle()
        _run(oracle)
        reads = [s for s in oracle.sightings.values() if s["body"]["plate"] == EXERCISE]
        assert reads and all(s["hit"] is None for s in reads)
        assert EXERCISE not in oracle.earmarks

    def test_its_reads_hit_once_a_duty_officer_has_earmarked_it(self):
        oracle = EarmarkingOracle(
            earmarks={EXERCISE: ("mva_1988_s177", "Duty Officer A — earmark exercise")},
            earmarked={EXERCISE: "watchlist"})
        _run(oracle)
        reads = [s for s in oracle.sightings.values() if s["body"]["plate"] == EXERCISE]
        assert reads and all(s["hit"] == "watchlist" for s in reads)
        # the officer's earmark, untouched: the run posts none for this plate
        assert not [c for c in oracle.calls if c[0] == "POST" and EXERCISE in c[1]]


class TestRealSeam:
    def test_the_calls_ride_the_oracle_seams_guarded_client(self):
        seen: list[tuple[str, str, dict | None]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content) if request.content else None
            seen.append((request.method, request.url.path, body))
            if request.method == "GET":
                return httpx.Response(200, json={"vehicles": [
                    {"plate": "GJ27EM4321", "lawful_basis": "mva_1988_s177"}]})
            return httpx.Response(200, json={"earmark_id": "e-9", "earmarked_by":
                                             body["earmarked_by"]})

        client = SafeHttpClient((BASE,), transport=httpx.MockTransport(handler))
        seam = OracleSeam(client, BASE, "event-emulator (system)")
        entry = asyncio.run(ensure_stolen_earmark(
            seam, STOLEN, fir_ref="FIR 4401/2026, Kalupur PS", use_case_id=UC, run_id=7))
        asyncio.run(client.close())
        assert entry["result"] == "delivered" and entry["earmark"] == "created"
        assert entry["earmark_id"] == "e-9"
        assert [(m, p) for m, p, _b in seen] == [
            ("GET", "/api/v1/vehicles"), ("POST", f"/api/v1/vehicles/{STOLEN}/earmark")]
        assert seen[1][2]["reason"].endswith(
            "(event-emulator use case 'watchlist_anpr_sweep', run 7); its reads are "
            "evidence-only sightings, never a fine")

    def test_the_sweep_tape_names_one_stolen_plate_and_its_fir(self):
        oracle = EarmarkingOracle()
        outcome = _run(oracle)[1]
        assert stolen_fir_plates(outcome.tape) == [STOLEN]
        assert profile_for(UC).anpr_stolen_fir_ref == "FIR 4401/2026, Kalupur PS"
