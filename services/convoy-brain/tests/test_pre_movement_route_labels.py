"""Pre-movement route checkpoint: content labels make the tier-0 cache reachable.

Regression (live gate check_tier0_live.sh (a), 2026-10-24): three identical
repeat plans all reported tier0=miss_no_entry. The route checkpoint offered
the oracle's route ids as the decider's route_strategy options, and the
oracle mints them per request (``f"{source}_{uuid4().hex[:8]}"``). The
question set is part of the tier-0 key — (movement, trigger, questions
signature, best_doc_id) — so identical plans never shared a key, every plan
stored a fresh entry, and the decision-memory feeder wrote a new doc per plan.

The options are now content labels: ``route_`` + a short sha256 of the
candidate's ordered segment sequence. Pinned here:
    (1) two plans over the same candidates ask the identical question set,
        so a tier-0 entry stored by the first is found by the second
        (``_tier0_consult`` against a fake memory with the same best_doc_id);
    (2) a cached answer resolves to the same PHYSICAL route it was decided
        for (the current request's route id), and a re-ranked set misses
        instead of serving a stale "second option";
    (3) a different candidate set yields a different signature (a miss);
    (4) pre-movement behaviour is unchanged: rank order, delay_departure, the
        primary/alternate reorder, the lane-floor guard, the audit row (plus
        the label -> route_id map), one decision doc per candidate set;
    (5) the live sequence (3, 5, 3 candidates for three identical bodies,
        the best memory doc churning from a foreign doc to the movement's
        own) hits on its recurrence, whichever own doc the search names: the
        decider also files a pre_movement answer for the movement's own
        decision docs (decider.py, TIER0_OWN_DOC_TRIGGERS);
plus label determinism, duplicate-content folding and digest collisions.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from dataclasses import replace

import pytest

from convoy_brain.decider import (
    MAX_ROUTE_OPTIONS,
    CheckpointResult,
    DeciderBrain,
    _questions_signature,
)
from convoy_brain.llm.decider_client import DeciderAnswer, validate_questions
from convoy_brain.workflows import pre_movement
from convoy_brain.workflows.pre_movement import (
    DELAY_DEPARTURE,
    _apply_decider_pick,
    _route_checkpoint,
    _route_options,
    _route_questions,
    _route_state_doc,
    route_option_label,
    run_pre_movement_workflow,
)
from conftest import (
    DESTINATION,
    NETWORK,
    ORIGIN,
    FakeContextStore,
    FakeExecutor,
    bbox_handler,
    details_handler,
)
from test_agents import StubBridge
from test_novelty_latch import FakeMemoryClient, ScriptedClock, memory_ok
from test_pre_movement_fusion import RouteDecider

MOVEMENT = "2c223e77-82b4-48c6-a586-88cff15a2b20"
DEPARTURE = "2030-01-01T00:00:00Z"
CORRIDOR = {"overall_status": "free_flow", "observations": 12, "segments": 5}

PATH_A = [1001, 1002, 1004]           # the tactical primary
PATH_B = [1005, 1001, 1002, 1004]     # an alternate
PATH_C = [1005, 1001, 1002, 1003]     # a different physical route


def candidate(route_id: str, segments: list[int], *, tier: str = "compliant",
              eta: int = 700, score: float = 0.8) -> dict:
    """One shaped candidate, as the route planner hands it to the checkpoint."""
    return {
        "route_id": route_id,
        "segment_ids": list(segments),
        "route_tier": tier,
        "lane_violations": [] if tier == "compliant" else [{"segment_id": segments[-1]}],
        "distance_m": 6000.0,
        "estimated_time_sec": eta,
        "weighted_score": score,
    }


def oracle_request(n: int, *paths: list[int]) -> list[dict]:
    """The candidates of the n-th identical request: same paths, fresh route
    ids every time (the oracle mints ``{source}_{uuid}`` per request)."""
    return [
        candidate(f"{('mip', 'ksp')[min(i, 1)]}_{n:04x}{i:04x}", path)
        for i, path in enumerate(paths)
    ]


def gate_memory() -> FakeMemoryClient:
    """Memory that reports the SAME covered best_doc_id on every search."""
    return FakeMemoryClient(
        results=[memory_ok(best_score=0.88, doc_id="doc-gate", kind="decision")]
    )


def gate_brain(decider, memory=None, **kwargs) -> DeciderBrain:
    return DeciderBrain(decider, memory_client=memory or gate_memory(), min_interval_sec=0,
                        clock=ScriptedClock(1000.0), memory_record_enabled=True, **kwargs)


async def checkpoint(brain: DeciderBrain, routes: list[dict], *,
                     movement_id: str = MOVEMENT) -> CheckpointResult:
    return await _route_checkpoint(
        brain, movement_id=movement_id, vvip_class="Z", planned_departure=DEPARTURE,
        corridor=CORRIDOR, options=_route_options(routes), remaining=120.0,
    )


def signature(routes: list[dict]) -> str:
    return _questions_signature(_route_questions(_route_options(routes)))


def segments_of(options: list[tuple[str, dict]]) -> dict[str, list[int]]:
    return {label: list(route["segment_ids"]) for label, route in options}


# ─── labels ──────────────────────────────────────────────────────────────────


class TestLabels:
    def test_label_is_a_short_hash_of_the_segment_sequence(self):
        label = route_option_label(candidate("mip_1a2b3c4d", PATH_A))
        digest = hashlib.sha256(b"1001,1002,1004").hexdigest()
        assert label == "route_" + digest[: pre_movement.ROUTE_LABEL_HEX]

    def test_label_ignores_route_id_source_rank_and_per_tick_figures(self):
        a = candidate("mip_1a2b3c4d", PATH_A, eta=700, score=0.8)
        b = candidate("ksp_9f8e7d6c", PATH_A, eta=1300, score=0.1, tier="conditional")
        assert route_option_label(a) == route_option_label(b)
        assert route_option_label(dict(a, segment_ids=["1001", "1002", 1004.0])) == (
            route_option_label(a)
        )

    def test_a_non_integral_id_is_never_truncated_onto_another_segment(self):
        whole = route_option_label(candidate("x", [1001, 1002]))
        assert route_option_label(candidate("x", [1001.5, 1002])) != whole
        assert route_option_label(candidate("x", [True, 1002])) != route_option_label(
            candidate("x", [1, 1002])
        )
        assert route_option_label(candidate("x", [float("nan"), 1002])) != whole

    def test_order_matters_and_paths_differ(self):
        forward = route_option_label(candidate("x", PATH_A))
        assert forward != route_option_label(candidate("x", list(reversed(PATH_A))))
        assert forward != route_option_label(candidate("x", PATH_B))
        assert not forward.startswith(("mip_", "ksp_"))  # never an oracle id

    def test_options_are_deterministic_and_in_rank_order(self):
        routes = oracle_request(1, PATH_A, PATH_B, PATH_C)
        first, second = _route_options(routes), _route_options(routes)
        assert first == second
        assert [route["route_id"] for _, route in first] == [r["route_id"] for r in routes]
        assert [label for label, _ in first] == [route_option_label(r) for r in routes]
        assert len({label for label, _ in first}) == 3

    def test_duplicate_content_folds_into_the_higher_ranked_candidate(self):
        routes = oracle_request(1, PATH_A, PATH_B) + [candidate("ksp_dup", PATH_A, eta=900)]
        options = _route_options(routes)
        assert [route["route_id"] for _, route in options] == [r["route_id"] for r in routes[:2]]
        # Deterministic whatever the duplicate's id/rank figures, and the
        # state doc lists each physical route once.
        assert _route_options(routes) == options
        doc = _route_state_doc(vvip_class="Z", planned_departure=DEPARTURE,
                               corridor=CORRIDOR, options=options)
        assert sum(line.startswith("- route_") for line in doc.splitlines()) == 2
        assert _route_questions(options)["route_strategy"]["options"] == [
            options[0][0], options[1][0], DELAY_DEPARTURE,
        ]

    def test_a_pick_of_a_folded_label_resolves_to_the_higher_ranked_copy(self):
        routes = oracle_request(1, PATH_A, PATH_B) + [candidate("ksp_dup", PATH_B)]
        result = decided(route_option_label(candidate("any", PATH_B)))
        reordered, pick = _apply_decider_pick(result, routes, "Z")
        assert pick["applied"] is True
        assert pick["route_id"] == routes[1]["route_id"]
        assert [r["route_id"] for r in reordered] == [
            routes[1]["route_id"], routes[0]["route_id"], "ksp_dup",
        ]

    def test_short_digest_collision_lengthens_the_label(self, monkeypatch):
        monkeypatch.setattr(pre_movement, "ROUTE_LABEL_HEX", 1)
        first_char: dict[str, list[int]] = {}
        for n in range(2000, 2100):  # two different paths, same first hex digit
            path = [n, n + 1]
            char = hashlib.sha256(f"{n},{n + 1}".encode()).hexdigest()[0]
            if char in first_char:
                clash = [candidate("mip_x", first_char[char]), candidate("ksp_y", path)]
                break
            first_char[char] = path
        options = _route_options(clash)
        labels = [label for label, _ in options]
        assert len(options) == 2 and len(set(labels)) == 2
        assert len(labels[0]) == len("route_") + 1
        assert len(labels[1]) > len("route_") + 1  # widened, still content-derived
        assert segments_of(options) == {
            labels[0]: clash[0]["segment_ids"], labels[1]: clash[1]["segment_ids"],
        }

    def test_options_are_capped_for_the_readout_head(self):
        routes = [candidate(f"mip_{i}", [10_000 + i]) for i in range(MAX_ROUTE_OPTIONS + 20)]
        options = _route_options(routes)
        assert len(options) == MAX_ROUTE_OPTIONS - 1
        questions = _route_questions(options)
        assert questions["route_strategy"]["options"][-1] == DELAY_DEPARTURE
        validate_questions(questions)  # the client-side contract still holds


# ─── tier-0: the regression ──────────────────────────────────────────────────


class TestTier0RoutineCache:
    def test_identical_plans_ask_the_identical_question_set(self):
        first, second = oracle_request(1, PATH_A, PATH_B), oracle_request(2, PATH_A, PATH_B)
        assert [r["route_id"] for r in first] != [r["route_id"] for r in second]
        assert signature(first) == signature(second)
        # The root cause, for the record: route-id options never repeat.
        by_ids = [
            _questions_signature(DeciderBrain.pre_movement_questions(
                [r["route_id"] for r in routes] + [DELAY_DEPARTURE]
            ))
            for routes in (first, second)
        ]
        assert by_ids[0] != by_ids[1]
        # The state doc names the candidates by the same labels (and so is
        # identical across identical requests).
        docs = [
            _route_state_doc(vvip_class="Z", planned_departure=DEPARTURE,
                             corridor=CORRIDOR, options=_route_options(routes))
            for routes in (first, second)
        ]
        assert docs[0] == docs[1]
        for label, _ in _route_options(first):
            assert f"- {label}: " in docs[0]

    async def test_entry_stored_by_the_first_plan_is_found_by_the_second(self):
        decider = RouteDecider(first=True, confidence=0.93)
        brain = gate_brain(decider)
        first = await checkpoint(brain, oracle_request(1, PATH_A, PATH_B))
        assert first.tier == "decider"
        assert first.audit["tier0_cache"]["status"] == "miss_no_entry"
        # ONE answer, filed under the best doc and for the movement's own
        # decision docs (pre_movement only) — both under ONE question set.
        assert len(brain._tier0_cache) == 2
        assert len({entry["state_doc_sha256"] for entry in brain._tier0_cache.values()}) == 1
        assert len({key[:3] for key in brain._tier0_cache}) == 1

        second_routes = oracle_request(2, PATH_A, PATH_B)
        row, novelty, entry = await brain._tier0_consult(
            MOVEMENT, "pre_movement", decider.states[0],
            _route_questions(_route_options(second_routes)),
        )
        assert row["status"] == "hit" and entry is not None
        assert novelty["best_doc_id"] == "doc-gate"

        second = await checkpoint(brain, second_routes)
        assert second.tier == "tier0_cache_hit"
        assert second.audit["tier0_cache"]["status"] == "hit"
        assert len(decider.questions) == 1  # ZERO /decide calls for the repeat
        assert len(brain._tier0_cache) == 2  # a hit stores nothing new

    async def test_cached_answer_resolves_to_the_same_physical_route(self):
        alternate = route_option_label(candidate("any", PATH_B))
        decider = RouteDecider(alternate, confidence=0.93)
        brain = gate_brain(decider)
        first_routes = oracle_request(1, PATH_A, PATH_B)
        first = await checkpoint(brain, first_routes)
        routes1, pick1 = _apply_decider_pick(first, first_routes, "Z")
        assert pick1["applied"] is True and routes1[0]["segment_ids"] == PATH_B

        second_routes = oracle_request(2, PATH_A, PATH_B)
        second = await checkpoint(brain, second_routes)
        assert second.tier == "tier0_cache_hit"
        routes2, pick2 = _apply_decider_pick(second, second_routes, "Z")
        assert pick2["choice"] == alternate
        assert pick2["tier"] == "tier0_cache_hit"
        assert pick2["applied"] is True
        # The CURRENT request's id for the SAME path (never request 1's id).
        assert pick2["route_id"] == second_routes[1]["route_id"]
        assert routes2[0]["segment_ids"] == PATH_B
        assert [r["route_id"] for r in routes2] == [
            second_routes[1]["route_id"], second_routes[0]["route_id"],
        ]

        # The same routes RE-RANKED: a rank label would serve the cached
        # "second option" (now PATH_A). Content labels miss and re-decide.
        reranked = oracle_request(3, PATH_B, PATH_A)
        assert signature(reranked) != signature(first_routes)
        third = await checkpoint(brain, reranked)
        assert third.tier == "decider"
        assert len(decider.questions) == 2
        # label -> path never changes across requests, whatever the rank.
        assert segments_of(_route_options(reranked)) == segments_of(_route_options(first_routes))

    async def test_a_different_candidate_set_misses(self):
        decider = RouteDecider(first=True, confidence=0.93)
        brain = gate_brain(decider)
        await checkpoint(brain, oracle_request(1, PATH_A, PATH_B))
        base = signature(oracle_request(1, PATH_A, PATH_B))
        for changed in (
            oracle_request(2, PATH_A, PATH_C),           # one path differs
            oracle_request(2, PATH_A),                   # a candidate dropped
            oracle_request(2, PATH_A, PATH_B, PATH_C),   # a candidate added
        ):
            assert signature(changed) != base
            row, _, entry = await brain._tier0_consult(
                MOVEMENT, "pre_movement", decider.states[0],
                _route_questions(_route_options(changed)),
            )
            assert row["status"] == "miss_no_entry" and entry is None
            assert row["best_doc_id"] == "doc-gate"  # same memory evidence, still a miss

    async def test_feeder_keeps_one_decision_doc_per_candidate_set(self):
        # Tier-0 off: every request reaches the 27B and is recorded. The
        # ref_id carries the questions signature, so identical candidate sets
        # upsert ONE doc (stable doc_id -> stable best_doc_id) instead of
        # one per plan; a different set is its own doc.
        memory = gate_memory()
        brain = gate_brain(RouteDecider(first=True, confidence=0.93), memory,
                           tier0_enabled=False)
        for n in (1, 2, 3):
            result = await checkpoint(brain, oracle_request(n, PATH_A, PATH_B))
            assert result.tier == "decider"
        assert len(memory.recorded) == 3
        assert len({r["ref_id"] for r in memory.recorded}) == 1
        await checkpoint(brain, oracle_request(4, PATH_A, PATH_C))
        assert len({r["ref_id"] for r in memory.recorded}) == 2


# ─── pick resolution (behaviour unchanged) ───────────────────────────────────


def decided(value: str, confidence: float = 0.9, tier: str = "decider") -> CheckpointResult:
    return CheckpointResult(tier=tier, answers={"route_strategy": DeciderAnswer(
        name="route_strategy", type="choice", value=value, probability=0.9,
        confidence=confidence,
    )})


class TestPickResolution:
    def test_floor_breaking_pick_is_not_applied_while_a_compliant_one_exists(self):
        routes = [candidate("mip_by", PATH_A), candidate("ksp_nar", PATH_C, tier="conditional")]
        reordered, pick = _apply_decider_pick(decided(route_option_label(routes[1])), routes, "Z+")
        assert reordered is routes
        assert pick["applied"] is False
        assert pick["route_id"] == "ksp_nar"
        assert pick["reason"] == (
            "ksp_nar breaks the Z+ lane floor while a compliant candidate exists — "
            "protocol is authoritative, tactical ranking kept"
        )

    def test_delay_departure_holds(self):
        routes = oracle_request(1, PATH_A, PATH_B)
        reordered, pick = _apply_decider_pick(decided(DELAY_DEPARTURE), routes, "Z")
        assert reordered is routes
        assert pick == {
            "route_id": None, "choice": DELAY_DEPARTURE, "confidence": 0.9,
            "tier": "decider", "applied": True, "hold": True,
            "reason": "strategic tier chose to delay departure — plan held",
        }

    @pytest.mark.parametrize("tier", ["gemma", "rate_limited", "unavailable"])
    def test_no_strategic_answer_no_pick(self, tier):
        routes = oracle_request(1, PATH_A, PATH_B)
        assert _apply_decider_pick(decided(DELAY_DEPARTURE, tier=tier), routes, "Z") == (
            routes, None,
        )


# ─── end to end: the live gate's three identical plans ───────────────────────


def minting_executor(paths: list[list[int]], requests: itertools.count) -> FakeExecutor:
    """Oracle double minting FRESH route ids on every find_convoy_routes call
    (``requests`` is shared across plans, like the oracle's uuid source)."""

    def find(args):
        if (args["origin_segment"], args["destination_segment"]) != (1001, 1003):
            return {"routes": []}
        n = next(requests)
        return {"routes": [
            {"route_id": f"{('mip', 'ksp')[min(i, 1)]}_{n:04x}{i:04x}",
             "segment_ids": list(path), "total_distance_m": 6000.0,
             "estimated_time_sec": 700 + 200 * i, "disruption_score": 20.0,
             "security_score": 80.0, "composite_score": 10.0 + i}
            for i, path in enumerate(paths)
        ]}

    return FakeExecutor(routes={
        "query_segments_in_bbox": bbox_handler(NETWORK),
        "query_segment_details": details_handler(NETWORK),
        "find_convoy_routes": find,
        "predict_traffic_flow": {"predictions": {}},
        "get_live_traffic": {"observations": []},
        "plan_diversions": {"diversions": []},
        "evaluate_scenarios": {"results": []},
    })


class TestRepeatPlans:
    async def test_identical_repeat_plans_hit_tier0_and_keep_the_same_route(self):
        store = FakeContextStore()
        memory = gate_memory()
        decider = RouteDecider(first=True, confidence=0.93)
        brain = gate_brain(decider, memory)
        requests = itertools.count(1)
        docs = []
        for _ in range(3):
            docs.append(await run_pre_movement_workflow(
                movement_id=MOVEMENT, origin=ORIGIN, destination=DESTINATION,
                vvip_class="Z", planned_departure=DEPARTURE, bridge=StubBridge(fail=True),
                executor=minting_executor([PATH_A, PATH_B], requests), context_store=store,
                decider_brain=brain,
            ))
        statuses = [d["decider_checkpoint"]["tier0_cache"]["status"] for d in docs]
        assert statuses == ["miss_no_entry", "hit", "hit"]
        assert [d["decider_checkpoint"]["tier"] for d in docs] == [
            "decider", "tier0_cache_hit", "tier0_cache_hit",
        ]
        assert len(decider.questions) == 1  # one 27B /decide for three plans
        primary_ids = [d["primary_route"]["route_id"] for d in docs]
        assert len(set(primary_ids)) == 3  # the oracle minted fresh ids each time
        for doc in docs:
            assert doc["primary_route"]["segment_ids"] == PATH_A
            assert doc["decider_pick"]["route_id"] == doc["primary_route"]["route_id"]
            assert doc["decider_pick"]["applied"] is True
            assert doc["decider_checkpoint"]["route_labels"] == {
                route_option_label(r): r["route_id"]
                for r in [doc["primary_route"], *doc["alternate_routes"]]
            }
            json.dumps(doc["decider_checkpoint"])  # decisions_log rows stay JSON-safe
        # Only the plan that reached the 27B fed the decision memory.
        assert len(memory.recorded) == 1
        rows = [r for r in store.contexts[MOVEMENT].decisions_log
                if r.get("phase") == "decider_checkpoint"]
        assert [r["tier"] for r in rows] == ["decider", "tier0_cache_hit", "tier0_cache_hit"]

        # The cache stays movement-scoped: a fresh movement over the very
        # same candidates is a miss (the live gate's (b) guard).
        other = await run_pre_movement_workflow(
            movement_id="a-different-movement", origin=ORIGIN, destination=DESTINATION,
            vvip_class="Z", planned_departure=DEPARTURE, bridge=StubBridge(fail=True),
            executor=minting_executor([PATH_A, PATH_B], requests), context_store=store,
            decider_brain=brain,
        )
        assert other["decider_checkpoint"]["tier0_cache"]["status"] == "miss_no_entry"
        assert len(decider.questions) == 2


# ─── the live sequence: a varying candidate set, a churning best doc ─────────

PATH_D = [1001, 1002, 1003, 1004]
PATH_E = [1005, 1002, 1004]
THREE = [PATH_A, PATH_B, PATH_C]
FIVE = [PATH_A, PATH_B, PATH_C, PATH_D, PATH_E]


class LiveLikeMemory:
    """Memory as the live audit rows of movement 2c223e77 show it.

    Until the movement has a decision doc of its own, the best match is a
    FOREIGN doc (another movement's plan, maxsim 0.8172 live); afterwards it
    is one of the movement's own docs (0.8289 / 0.8315 live), the one whose
    body shares the most lines with the query. A three-candidate state doc
    is a near-subset of the five-candidate one (MaxSim scores a query the
    same against any doc holding all its tokens), so the two own docs tie
    for it: ``tie`` picks the ``"first"`` or ``"last"`` recorded. The feeder
    upserts on ref_id (a ref_id keeps its doc id); the search reports both.
    """

    FOREIGN_REF = "decision:f9d220d5-bf1b-4145-9987-a435c8b6a500:pre_movement:548407541b232701"

    def __init__(self, tie: str = "first") -> None:
        self.tie = tie
        self.docs: dict[str, tuple[str, str, str]] = {}  # ref_id -> (doc_id, body, movement)
        self.recorded: list[str] = []

    async def search(self, state_doc, *, movement_id=None, top_k=5):
        own = [(ref, doc) for ref, doc in self.docs.items() if doc[2] == movement_id]
        if not own:
            return replace(memory_ok(best_score=0.8172, doc_id="foreign-doc", kind="decision"),
                           best_ref_id=self.FOREIGN_REF)
        lines = set(state_doc.splitlines())
        overlap = [len(lines & set(doc[1].splitlines())) for _, doc in own]
        best = [pair for pair, n in zip(own, overlap) if n == max(overlap)]
        ref, doc = best[0] if self.tie == "first" else best[-1]
        return replace(memory_ok(best_score=0.83, doc_id=doc[0], kind="decision"),
                       best_ref_id=ref)

    async def record_decision(self, state_doc, *, movement_id, ref_id, title, metadata=None,
                              timeout_sec=5.0):
        doc_id = self.docs[ref_id][0] if ref_id in self.docs else f"own-{len(self.docs) + 1}"
        self.docs[ref_id] = (doc_id, state_doc, movement_id)
        self.recorded.append(ref_id)
        return True


class TestLiveCandidateVariance:
    """The live gate's three identical bodies planned 3, 5 and 3 candidates
    (the tactical narration trimmed the two tail alternates in plans 1 and 3,
    not in plan 2; the head kept its rank order), ~30 s apart (past the
    novelty memo). Content labels alone never hit on that sequence: plan 1's
    answer sat under the foreign doc, and plan 3 matched an own doc.
    """

    async def plans(self, candidate_sets, *, tie="first", **brain_kwargs):
        clock = ScriptedClock(1000.0)
        memory = LiveLikeMemory(tie)
        decider = RouteDecider(route_option_label(candidate("any", PATH_B)), confidence=0.93)
        brain = DeciderBrain(decider, memory_client=memory, min_interval_sec=0, clock=clock,
                             memory_record_enabled=True, **brain_kwargs)
        store, requests, docs = FakeContextStore(), itertools.count(1), []
        for paths in candidate_sets:
            docs.append(await run_pre_movement_workflow(
                movement_id=MOVEMENT, origin=ORIGIN, destination=DESTINATION,
                vvip_class="Z", planned_departure=DEPARTURE, bridge=StubBridge(fail=True),
                executor=minting_executor(paths, requests), context_store=store,
                decider_brain=brain,
            ))
            clock.now += 31.0
        return docs, decider, memory

    @pytest.mark.parametrize("tie", ["first", "last"])
    async def test_the_live_sequence_hits_on_its_recurrence(self, tie):
        docs, decider, memory = await self.plans([THREE, FIVE, THREE], tie=tie)
        rows = [d["decider_checkpoint"]["tier0_cache"] for d in docs]
        assert [r["status"] for r in rows] == ["miss_no_entry", "miss_no_entry", "hit"]
        assert [r["best_doc_id"] for r in rows] == [
            "foreign-doc", "own-1", "own-1" if tie == "first" else "own-2",
        ]
        assert [r["memo"] for r in rows] == [False, False, False]
        assert len(decider.questions) == 2  # plan 3 made no /decide call
        assert len(memory.recorded) == 2
        # The cached answer opens the same physical route under plan 3's own id.
        third = docs[2]
        assert third["decider_pick"]["tier"] == "tier0_cache_hit"
        assert third["decider_pick"]["applied"] is True
        assert third["primary_route"]["segment_ids"] == PATH_B
        assert third["decider_pick"]["route_id"] == third["primary_route"]["route_id"]
        assert third["primary_route"]["route_id"] not in {
            d["primary_route"]["route_id"] for d in docs[:2]
        }

    async def test_content_labels_alone_never_hit_on_it(self):
        docs, decider, _ = await self.plans(
            [THREE, FIVE, THREE], tie="last", tier0_own_doc_triggers=frozenset(),
        )
        statuses = [d["decider_checkpoint"]["tier0_cache"]["status"] for d in docs]
        assert statuses == ["miss_no_entry"] * 3
        assert len(decider.questions) == 3

    @pytest.mark.parametrize("tie", ["first", "last"])
    @pytest.mark.parametrize("sets", [
        [THREE, THREE, THREE], [FIVE, FIVE, FIVE], [FIVE, THREE, THREE],
        [THREE, THREE, FIVE], [THREE, FIVE, FIVE], [FIVE, THREE, FIVE],
    ])
    async def test_two_candidate_sets_always_give_a_hit_in_three_plans(self, sets, tie):
        docs, decider, _ = await self.plans(sets, tie=tie)
        statuses = [d["decider_checkpoint"]["tier0_cache"]["status"] for d in docs]
        assert "hit" in statuses
        assert len(decider.questions) == 3 - statuses.count("hit")
        for doc in docs:  # whatever served it, the pick is PATH_B in that plan's ids
            assert doc["primary_route"]["segment_ids"] == PATH_B
            assert doc["decider_pick"]["route_id"] == doc["primary_route"]["route_id"]
