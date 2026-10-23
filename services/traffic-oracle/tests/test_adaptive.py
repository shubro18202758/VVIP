"""Exhaustive deterministic unit tests for the adaptive (T2) layer.

Covers: enums, threshold mapping, the 1024-combination classifier
product space, hysteresis anti-oscillation, protocol floors and the C4
fail-closed rule, shadow-evaluation gate, escalation rules, degradation
ladder, the AdaptiveService pipeline with fake ports, the HTTP router,
gpu_arbiter v2 budget resolution, the RL environment import graph and
TwinEnv wrapper, RL protocol bounds, the __main__ wiring, and migration
005 idempotency guards.

No external services are required: every I/O surface is faked.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from traffic_oracle.adaptive import (
    BASELINE_ADVANCE_CLOSURE_SEC,
    BASELINE_CONGESTION_SPIKE_THRESHOLD,
    BASELINE_GPS_POLL_INTERVAL_SEC,
    BASELINE_SCORING_WEIGHTS,
    AdaptiveService,
    CrowdDensity,
    DegradationManager,
    DegradationTier,
    EscalationEngine,
    EscalationInputs,
    EscalationLevel,
    HysteresisConfig,
    HysteresisController,
    ModelHealthSnapshot,
    ParamSource,
    ParameterAdapter,
    SecurityTier,
    ShadowEvalStatus,
    ShadowEvaluator,
    SituationClass,
    SituationEngine,
    SituationInputs,
    TrafficRegime,
    TwinRun,
    WeatherCondition,
    all_input_combinations,
    clamp_advance_closure,
)
from traffic_oracle.adaptive.enums import _RankedEnum  # noqa: F401 - rank contract
from traffic_oracle.adaptive.hysteresis import HysteresisResult
from traffic_oracle.adaptive.inputs import (
    crowd_from_score,
    traffic_regime_from_congestion,
    weather_from_severity,
)
from traffic_oracle.adaptive.policy import (
    ADVANCE_CLOSURE_FLOOR_SEC,
    AdaptiveParams,
    GPS_POLL_MIN_SEC,
)
from traffic_oracle.adaptive.situation import (
    MAX_SEVERITY_SCORE,
    band_from_score,
)
from traffic_oracle.adaptive.twin import (
    ClosurePolicy,
    ShadowContext,
    UnavailableTwinPort,
)

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
MIGRATIONS = Path(__file__).resolve().parents[1].parent / "corridor-store" / "migrations"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class FakeSink:
    params: list[tuple[str, str]] = field(default_factory=list)
    changes: list[dict] = field(default_factory=list)

    async def publish_params(self, movement_id: str, payload_json: str) -> None:
        self.params.append((movement_id, payload_json))

    async def publish_situation_change(
        self, movement_id: str, from_code: str, to_code: str, payload_json: str
    ) -> None:
        self.changes.append(
            {
                "movement_id": movement_id,
                "from": from_code,
                "to": to_code,
                "payload": json.loads(payload_json),
            }
        )


@dataclass
class FakePersister:
    situations: list[dict] = field(default_factory=list)
    param_rows: list[dict] = field(default_factory=list)

    async def log_situation(self, record: dict) -> None:
        self.situations.append(record)

    async def log_params(self, record: dict) -> None:
        self.param_rows.append(record)


@dataclass
class FakeTwinPort:
    """Configurable twin port returning canned run pairs."""

    baseline_run: TwinRun | None = None
    candidate_run: TwinRun | None = None
    error: str | None = None
    calls: int = 0

    def run_pair(
        self, baseline: ClosurePolicy, candidate: ClosurePolicy, ctx: ShadowContext
    ):
        from traffic_oracle.adaptive.twin import TwinPair

        self.calls += 1
        return TwinPair(self.baseline_run, self.candidate_run, self.error)


def passing_twin(baseline_vh: float = 100.0, candidate_vh: float = 95.0) -> FakeTwinPort:
    return FakeTwinPort(
        baseline_run=TwinRun(
            total_disruption_vehicle_hours=baseline_vh, calibrated=True, is_stub=False
        ),
        candidate_run=TwinRun(
            total_disruption_vehicle_hours=candidate_vh, calibrated=True, is_stub=False
        ),
    )


def make_service(
    clock: FakeClock | None = None,
    sink: FakeSink | None = None,
    persister: FakePersister | None = None,
    twin: object | None = None,
) -> tuple[AdaptiveService, FakeClock, FakeSink, FakePersister]:
    clock = clock or FakeClock()
    sink = sink or FakeSink()
    persister = persister or FakePersister()
    service = AdaptiveService(
        clock=clock,
        sink=sink,
        persister=persister,
        twin_port=twin if twin is not None else UnavailableTwinPort(),
    )
    return service, clock, sink, persister


def ctx(route: tuple[int, ...] = (1, 2, 3)) -> ShadowContext:
    return ShadowContext(
        route_segment_ids=route, departure_time="2026-10-12T10:00:00"
    )


# ===========================================================================
# 1. Enums
# ===========================================================================


class TestEnums:
    def test_rank_orderings(self):
        assert [t.code for t in SecurityTier] == ["none", "X", "Y", "Z", "Z+"]
        assert (
            SecurityTier.Z_PLUS.rank
            > SecurityTier.Z.rank
            > SecurityTier.Y.rank
            > SecurityTier.X.rank
            > SecurityTier.NONE.rank
        )
        # The public-event class ranks below X and is the event-agnostic alias
        # target (ProtectionClass is SecurityTier).
        from traffic_oracle.adaptive.enums import ProtectionClass

        assert ProtectionClass is SecurityTier
        assert SecurityTier.NONE.code == "none"
        assert SecurityTier.from_code("none") is SecurityTier.NONE
        assert (
            TrafficRegime.GRIDLOCK.rank
            > TrafficRegime.CONGESTED.rank
            > TrafficRegime.HEAVY.rank
            > TrafficRegime.FREE_FLOW.rank
        )
        assert (
            WeatherCondition.STORM.rank
            > WeatherCondition.FOG.rank
            > WeatherCondition.RAIN.rank
            > WeatherCondition.CLEAR.rank
        )
        assert (
            CrowdDensity.EXTREME.rank
            > CrowdDensity.HIGH.rank
            > CrowdDensity.MEDIUM.rank
            > CrowdDensity.LOW.rank
        )
        assert (
            SituationClass.BLACK.rank
            > SituationClass.RED.rank
            > SituationClass.AMBER.rank
            > SituationClass.GREEN.rank
        )

    @pytest.mark.parametrize("alias", ["Z+", "ZPLUS", "Z_PLUS", "ZP", "z+"])
    def test_security_tier_aliases(self, alias):
        assert SecurityTier.from_code(alias) is SecurityTier.Z_PLUS

    @pytest.mark.parametrize("bad", ["Q", "", "ZZ", "z++", None, "1"])
    def test_security_tier_invalid(self, bad):
        with pytest.raises(ValueError):
            SecurityTier.from_code(bad)

    def test_published_shadow_status_is_contract_schema(self):
        assert ShadowEvalStatus.PASSED.published == "passed"
        assert ShadowEvalStatus.UNAVAILABLE.published == "unavailable"
        assert ShadowEvalStatus.FAILED.published == "unavailable"

    def test_param_source_codes(self):
        assert {s.code for s in ParamSource} == {"baseline", "retrieved", "learned"}


# ===========================================================================
# 2. Threshold mapping (boundary-exhaustive)
# ===========================================================================


class TestThresholdMapping:
    def test_congestion_boundaries(self):
        # exact boundaries fall into the bucket starting at the bound
        assert traffic_regime_from_congestion(0.0) is TrafficRegime.FREE_FLOW
        assert traffic_regime_from_congestion(0.3499) is TrafficRegime.FREE_FLOW
        assert traffic_regime_from_congestion(0.35) is TrafficRegime.HEAVY
        assert traffic_regime_from_congestion(0.5999) is TrafficRegime.HEAVY
        assert traffic_regime_from_congestion(0.60) is TrafficRegime.CONGESTED
        assert traffic_regime_from_congestion(0.8499) is TrafficRegime.CONGESTED
        assert traffic_regime_from_congestion(0.85) is TrafficRegime.GRIDLOCK
        assert traffic_regime_from_congestion(1.0) is TrafficRegime.GRIDLOCK

    @pytest.mark.parametrize("idx", [-1.0, -0.5, 5.0, 100.0])
    def test_congestion_clamped(self, idx):
        regime = traffic_regime_from_congestion(idx)
        assert regime in set(TrafficRegime)

    def test_anomaly_floors_regime_at_congested(self):
        assert (
            traffic_regime_from_congestion(0.1, anomaly_active=True)
            is TrafficRegime.CONGESTED
        )
        assert (
            traffic_regime_from_congestion(0.5, anomaly_active=True)
            is TrafficRegime.CONGESTED
        )
        # never lowers an already-worse regime
        assert (
            traffic_regime_from_congestion(0.95, anomaly_active=True)
            is TrafficRegime.GRIDLOCK
        )
        # flag off leaves forecast untouched
        assert (
            traffic_regime_from_congestion(0.1, anomaly_active=False)
            is TrafficRegime.FREE_FLOW
        )

    def test_weather_boundaries(self):
        assert weather_from_severity(0.0) is WeatherCondition.CLEAR
        assert weather_from_severity(0.2499) is WeatherCondition.CLEAR
        assert weather_from_severity(0.25) is WeatherCondition.RAIN
        assert weather_from_severity(0.4999) is WeatherCondition.RAIN
        assert weather_from_severity(0.50) is WeatherCondition.FOG
        assert weather_from_severity(0.7499) is WeatherCondition.FOG
        assert weather_from_severity(0.75) is WeatherCondition.STORM
        assert weather_from_severity(1.0) is WeatherCondition.STORM

    def test_crowd_boundaries(self):
        assert crowd_from_score(0.0) is CrowdDensity.LOW
        assert crowd_from_score(0.2499) is CrowdDensity.LOW
        assert crowd_from_score(0.25) is CrowdDensity.MEDIUM
        assert crowd_from_score(0.4999) is CrowdDensity.MEDIUM
        assert crowd_from_score(0.50) is CrowdDensity.HIGH
        assert crowd_from_score(0.7499) is CrowdDensity.HIGH
        assert crowd_from_score(0.75) is CrowdDensity.EXTREME
        assert crowd_from_score(1.0) is CrowdDensity.EXTREME

    def test_situation_inputs_round_trip(self):
        for combo in all_input_combinations():
            assert SituationInputs.from_dict(combo.to_dict()) == combo

    def test_from_scores_with_explicit_weather_code(self):
        inputs = SituationInputs.from_scores(
            vvip_class="Z", predicted_congestion_idx=0.9, weather="STORM"
        )
        assert inputs.weather is WeatherCondition.STORM

    def test_from_scores_invalid_tier_raises(self):
        with pytest.raises(ValueError):
            SituationInputs.from_scores(vvip_class="Q", predicted_congestion_idx=0.1)


# ===========================================================================
# 3. Classifier — exhaustive over the full 1280-combination product space
# ===========================================================================


class TestClassifierExhaustive:
    combos = all_input_combinations()
    engine = SituationEngine()

    def test_product_space_size(self):
        assert len(self.combos) == 5 * 4 * 4 * 4 * 2 * 2 == 1280
        assert len(set(self.combos)) == 1280  # frozen dataclass, fully hashable

    def test_determinism(self):
        for combo in self.combos:
            a = self.engine.classify(combo)
            b = self.engine.classify(combo)
            assert a == b
            assert a.to_dict() == b.to_dict()

    def test_code_format(self):
        for combo in self.combos:
            code = self.engine.classify(combo).code
            parts = code.split(":")
            assert len(parts) == 4
            assert parts[0] == combo.vvip_class.code
            assert parts[1] == combo.traffic_regime.code
            assert parts[2] == combo.weather.code
            assert parts[3] == combo.crowd.code

    def test_score_formula(self):
        from traffic_oracle.adaptive.situation import (
            ANOMALY_POINTS,
            CROWD_POINTS,
            EVENT_POINTS,
            SECURITY_POINTS,
            TRAFFIC_POINTS,
            WEATHER_POINTS,
        )

        for combo in self.combos:
            sit = self.engine.classify(combo)
            expected = (
                SECURITY_POINTS[combo.vvip_class]
                + TRAFFIC_POINTS[combo.traffic_regime]
                + WEATHER_POINTS[combo.weather]
                + CROWD_POINTS[combo.crowd]
                + (ANOMALY_POINTS if combo.anomaly_active else 0)
                + (EVENT_POINTS if combo.event_active else 0)
            )
            assert sit.severity_score == expected
            assert 0 <= expected <= MAX_SEVERITY_SCORE

    def test_band_step_function(self):
        for score, band in [
            (0, SituationClass.GREEN), (4, SituationClass.GREEN),
            (5, SituationClass.AMBER), (9, SituationClass.AMBER),
            (10, SituationClass.RED), (14, SituationClass.RED),
            (15, SituationClass.BLACK), (MAX_SEVERITY_SCORE, SituationClass.BLACK),
        ]:
            assert band_from_score(score) is band

    def test_band_monotone_in_every_dimension(self):
        """Worsening any single input can never soften the band."""
        for combo in self.combos:
            base = self.engine.classify(combo).situation_class.rank
            # explicit successor checks
            successors = {
                SecurityTier.NONE: SecurityTier.X,
                SecurityTier.X: SecurityTier.Y,
                SecurityTier.Y: SecurityTier.Z,
                SecurityTier.Z: SecurityTier.Z_PLUS,
            }
            succ = successors.get(combo.vvip_class)
            if succ is not None:
                worse_combo = SituationInputs(
                    vvip_class=succ,
                    traffic_regime=combo.traffic_regime,
                    weather=combo.weather,
                    crowd=combo.crowd,
                    anomaly_active=combo.anomaly_active,
                    event_active=combo.event_active,
                )
                assert (
                    self.engine.classify(worse_combo).situation_class.rank >= base
                ), f"security worsened but band fell: {combo} -> {worse_combo}"
            traffic_succ = {
                TrafficRegime.FREE_FLOW: TrafficRegime.HEAVY,
                TrafficRegime.HEAVY: TrafficRegime.CONGESTED,
                TrafficRegime.CONGESTED: TrafficRegime.GRIDLOCK,
            }.get(combo.traffic_regime)
            if traffic_succ is not None:
                worse_combo = SituationInputs(
                    vvip_class=combo.vvip_class,
                    traffic_regime=traffic_succ,
                    weather=combo.weather,
                    crowd=combo.crowd,
                    anomaly_active=combo.anomaly_active,
                    event_active=combo.event_active,
                )
                assert self.engine.classify(worse_combo).situation_class.rank >= base
            weather_succ = {
                WeatherCondition.CLEAR: WeatherCondition.RAIN,
                WeatherCondition.RAIN: WeatherCondition.FOG,
                WeatherCondition.FOG: WeatherCondition.STORM,
            }.get(combo.weather)
            if weather_succ is not None:
                worse_combo = SituationInputs(
                    vvip_class=combo.vvip_class,
                    traffic_regime=combo.traffic_regime,
                    weather=weather_succ,
                    crowd=combo.crowd,
                    anomaly_active=combo.anomaly_active,
                    event_active=combo.event_active,
                )
                assert self.engine.classify(worse_combo).situation_class.rank >= base
            crowd_succ = {
                CrowdDensity.LOW: CrowdDensity.MEDIUM,
                CrowdDensity.MEDIUM: CrowdDensity.HIGH,
                CrowdDensity.HIGH: CrowdDensity.EXTREME,
            }.get(combo.crowd)
            if crowd_succ is not None:
                worse_combo = SituationInputs(
                    vvip_class=combo.vvip_class,
                    traffic_regime=combo.traffic_regime,
                    weather=combo.weather,
                    crowd=crowd_succ,
                    anomaly_active=combo.anomaly_active,
                    event_active=combo.event_active,
                )
                assert self.engine.classify(worse_combo).situation_class.rank >= base
            for flag in ("anomaly_active", "event_active"):
                if not getattr(combo, flag):
                    worse_combo = SituationInputs(
                        vvip_class=combo.vvip_class,
                        traffic_regime=combo.traffic_regime,
                        weather=combo.weather,
                        crowd=combo.crowd,
                        anomaly_active=combo.anomaly_active or flag == "anomaly_active",
                        event_active=combo.event_active or flag == "event_active",
                    )
                    assert self.engine.classify(worse_combo).situation_class.rank >= base

    def test_z_plus_never_green(self):
        for combo in self.combos:
            if combo.vvip_class is SecurityTier.Z_PLUS:
                assert self.engine.classify(combo).situation_class is not (
                    SituationClass.GREEN
                )

    def test_all_bands_reachable(self):
        seen = {
            self.engine.classify(c).situation_class for c in self.combos
        }
        assert seen == {
            SituationClass.GREEN,
            SituationClass.AMBER,
            SituationClass.RED,
            SituationClass.BLACK,
        }

    def test_extreme_case_is_black(self):
        sit = self.engine.classify(
            SituationInputs(
                vvip_class=SecurityTier.Z_PLUS,
                traffic_regime=TrafficRegime.GRIDLOCK,
                weather=WeatherCondition.STORM,
                crowd=CrowdDensity.EXTREME,
                anomaly_active=True,
                event_active=True,
            )
        )
        assert sit.situation_class is SituationClass.BLACK
        assert sit.severity_score == MAX_SEVERITY_SCORE
        assert sit.code == "Z+:GRIDLOCK:STORM:EXTREME"

    def test_mildest_case_is_green(self):
        sit = self.engine.classify(
            SituationInputs(
                vvip_class=SecurityTier.X,
                traffic_regime=TrafficRegime.FREE_FLOW,
                weather=WeatherCondition.CLEAR,
                crowd=CrowdDensity.LOW,
            )
        )
        assert sit.situation_class is SituationClass.GREEN
        assert sit.severity_score == 0


# ===========================================================================
# 4. Hysteresis
# ===========================================================================


def feed(controller, observations, t0=0.0, dt=1.0):
    out: list[HysteresisResult] = []
    t = t0
    for obs in observations:
        out.append(controller.evaluate(obs, t))
        t += dt
    return out


class TestHysteresis:
    def test_first_observation_initializes(self):
        c = HysteresisController()
        r = c.evaluate(SituationClass.AMBER, 0.0)
        assert r.effective is SituationClass.AMBER
        assert not r.transitioned
        assert r.pending is None

    def test_up_requires_two_consecutive(self):
        c = HysteresisController()
        feed(c, [SituationClass.GREEN])  # init
        r1 = c.evaluate(SituationClass.RED, 1.0)
        assert r1.effective is SituationClass.GREEN and not r1.transitioned
        assert r1.pending is SituationClass.RED and r1.pending_count == 1
        r2 = c.evaluate(SituationClass.RED, 2.0)
        assert r2.effective is SituationClass.RED and r2.transitioned

    def test_down_requires_four_consecutive_and_dwell(self):
        c = HysteresisController()
        feed(c, [SituationClass.RED])  # init at RED
        # 4 consecutive GREEN but dwell not elapsed (dt=1 < 120s)
        rs = feed(c, [SituationClass.GREEN] * 4, t0=10.0)
        assert all(r.effective is SituationClass.RED for r in rs)
        assert not rs[-1].transitioned
        # dwell now elapsed: the streak counter is already at 4, so the
        # next GREEN observation transitions immediately
        rs = feed(c, [SituationClass.GREEN] * 4, t0=200.0, dt=30.0)
        assert any(r.transitioned for r in rs)
        assert rs[-1].effective is SituationClass.GREEN
        assert rs[-1].pending is None  # re-armed after the transition

    def test_black_is_immediate(self):
        c = HysteresisController()
        feed(c, [SituationClass.GREEN])
        r = c.evaluate(SituationClass.BLACK, 1.0)
        assert r.effective is SituationClass.BLACK and r.transitioned

    def test_oscillation_immune(self):
        """Alternating observations must never flip the effective class."""
        c = HysteresisController()
        feed(c, [SituationClass.GREEN])
        for i in range(50):
            obs = SituationClass.AMBER if i % 2 == 0 else SituationClass.GREEN
            r = c.evaluate(obs, float(i))
            assert r.effective is SituationClass.GREEN
            assert not r.transitioned

    def test_slow_oscillation_immune(self):
        """Even slow alternation below confirm thresholds holds steady."""
        c = HysteresisController()
        feed(c, [SituationClass.AMBER])
        t = 100.0
        for i in range(20):
            obs = (
                SituationClass.RED
                if (i // 1) % 2 == 0
                else SituationClass.AMBER
            )
            r = c.evaluate(obs, t)
            t += 60.0
            assert r.effective is SituationClass.AMBER

    def test_candidate_streak_resets_on_disagreement(self):
        c = HysteresisController()
        feed(c, [SituationClass.GREEN])
        assert c.evaluate(SituationClass.RED, 1.0).pending_count == 1
        assert c.evaluate(SituationClass.RED, 2.0).transitioned  # escalates
        # now a GREEN blip resets de-escalation progress
        assert c.evaluate(SituationClass.GREEN, 3.0).pending_count == 1
        assert c.evaluate(SituationClass.RED, 4.0).effective is SituationClass.RED

    def test_config_validation(self):
        with pytest.raises(ValueError):
            HysteresisConfig(up_confirm=0)
        with pytest.raises(ValueError):
            HysteresisConfig(down_confirm=0)
        with pytest.raises(ValueError):
            HysteresisConfig(min_dwell_sec=-1)

    def test_snapshot_is_copy(self):
        c = HysteresisController()
        c.evaluate(SituationClass.GREEN, 0.0)
        snap = c.snapshot()
        snap.current = SituationClass.BLACK
        assert c.state.current is SituationClass.GREEN


# ===========================================================================
# 5. Policy engine — protocol floors + fail-closed
# ===========================================================================


class TestPolicy:
    adapter = ParameterAdapter()

    def test_baseline_constants_are_todays_values(self):
        b = AdaptiveParams.baseline()
        assert b.advance_closure_sec == 120 == BASELINE_ADVANCE_CLOSURE_SEC
        assert (
            b.congestion_spike_threshold == 0.8 == BASELINE_CONGESTION_SPIKE_THRESHOLD
        )
        assert b.gps_poll_interval_sec == 1.0 == BASELINE_GPS_POLL_INTERVAL_SEC
        assert b.scoring_weights == BASELINE_SCORING_WEIGHTS == {
            "time": 0.3, "disruption": 0.4, "security": 0.2, "complexity": 0.1,
        }

    @pytest.mark.parametrize(
        "tier,expected_floor",
        [
            (SecurityTier.Z_PLUS, 180),
            (SecurityTier.Z, 120),
            (SecurityTier.Y, 60),
            (SecurityTier.X, None),
            (SecurityTier.NONE, None),
        ],
    )
    def test_protocol_floors(self, tier, expected_floor):
        assert ADVANCE_CLOSURE_FLOOR_SEC[tier] == expected_floor
        assert clamp_advance_closure(10, tier) == (expected_floor or 10)
        assert clamp_advance_closure(1000, tier) == 1000

    @pytest.mark.parametrize("tier", list(SecurityTier))
    def test_baseline_clamped_to_floor(self, tier):
        params, floor_applied = self.adapter.baseline_params(tier)
        floor = ADVANCE_CLOSURE_FLOOR_SEC[tier]
        if floor is not None and floor > BASELINE_ADVANCE_CLOSURE_SEC:
            assert params.advance_closure_sec == floor
            assert floor_applied is True
        else:
            assert params.advance_closure_sec == BASELINE_ADVANCE_CLOSURE_SEC
            assert floor_applied is False
        # non-advance params stay at today's constants even when clamped
        assert params.congestion_spike_threshold == BASELINE_CONGESTION_SPIKE_THRESHOLD
        assert params.gps_poll_interval_sec == BASELINE_GPS_POLL_INTERVAL_SEC
        assert params.scoring_weights == BASELINE_SCORING_WEIGHTS

    @pytest.mark.parametrize("band", list(SituationClass))
    @pytest.mark.parametrize("tier", list(SecurityTier))
    def test_candidate_invariants(self, band, tier):
        c = self.adapter.candidate_params(band, tier)
        floor = ADVANCE_CLOSURE_FLOOR_SEC[tier]
        assert c.advance_closure_sec >= max(BASELINE_ADVANCE_CLOSURE_SEC, floor or 0)
        assert c.congestion_spike_threshold <= BASELINE_CONGESTION_SPIKE_THRESHOLD
        assert GPS_POLL_MIN_SEC <= c.gps_poll_interval_sec <= BASELINE_GPS_POLL_INTERVAL_SEC
        assert abs(sum(c.scoring_weights.values()) - 1.0) < 1e-9
        assert set(c.scoring_weights) == {"time", "disruption", "security", "complexity"}
        assert all(0.0 < v <= 1.0 for v in c.scoring_weights.values())

    def test_candidate_monotone_in_severity(self):
        for tier in SecurityTier:
            bands = list(SituationClass)  # GREEN..BLACK
            advances, spikes, polls = [], [], []
            for band in bands:
                c = self.adapter.candidate_params(band, tier)
                advances.append(c.advance_closure_sec)
                spikes.append(c.congestion_spike_threshold)
                polls.append(c.gps_poll_interval_sec)
            assert advances == sorted(advances), (tier, advances)
            assert spikes == sorted(spikes, reverse=True), (tier, spikes)
            assert polls == sorted(polls, reverse=True), (tier, polls)

    @pytest.mark.parametrize("tier", list(SecurityTier))
    def test_green_candidate_is_baseline_no_shadow_needed(self, tier):
        band = SituationClass.GREEN
        c = self.adapter.candidate_params(band, tier)
        assert self.adapter.candidate_is_baseline_shaped(c, band, tier)
        decision = self.adapter.decide(band, tier)  # shadow defaults unavailable
        assert decision.source is ParamSource.BASELINE
        assert decision.fail_closed_reason is None
        assert decision.params == self.adapter.baseline_params(tier)[0]

    @pytest.mark.parametrize("band", [SituationClass.AMBER, SituationClass.RED, SituationClass.BLACK])
    @pytest.mark.parametrize("tier", list(SecurityTier))
    def test_fail_closed_without_shadow_pass(self, band, tier):
        for status in (ShadowEvalStatus.UNAVAILABLE, ShadowEvalStatus.FAILED):
            decision = self.adapter.decide(
                band, tier, shadow_status=status, shadow_detail="test"
            )
            baseline, _ = self.adapter.baseline_params(tier)
            assert decision.params == baseline
            assert decision.source is ParamSource.BASELINE
            assert decision.fail_closed_reason == (
                "shadow_eval_failed_deviation_rejected"
                if status is ShadowEvalStatus.FAILED
                else "shadow_eval_unavailable_fail_closed"
            )
            response = decision.to_response()
            assert response["shadow_eval"] == "unavailable"

    @pytest.mark.parametrize("band", [SituationClass.AMBER, SituationClass.RED, SituationClass.BLACK])
    @pytest.mark.parametrize("tier", list(SecurityTier))
    def test_deviation_published_only_with_pass(self, band, tier):
        decision = self.adapter.decide(
            band,
            tier,
            shadow_status=ShadowEvalStatus.PASSED,
            shadow_detail="no_regression",
            requested_source=ParamSource.LEARNED,
        )
        assert decision.source is ParamSource.LEARNED
        assert decision.params == self.adapter.candidate_params(band, tier)
        response = decision.to_response()
        assert response["shadow_eval"] == "passed"
        assert response["source"] == "learned"

    def test_force_baseline_overrides_even_passed_shadow(self):
        decision = self.adapter.decide(
            SituationClass.RED,
            SecurityTier.Z,
            shadow_status=ShadowEvalStatus.PASSED,
            force_baseline=True,
        )
        assert decision.source is ParamSource.BASELINE
        assert decision.fail_closed_reason == "forced_baseline_degradation"
        baseline, _ = self.adapter.baseline_params(SecurityTier.Z)
        assert decision.params == baseline

    def test_response_shape_is_c4(self):
        decision = self.adapter.decide(SituationClass.GREEN, SecurityTier.Z)
        response = decision.to_response({"movement_id": "m1"})
        assert set(response) == {
            "situation_class", "params", "source", "shadow_eval", "evidence",
        }
        assert set(response["params"]) == {
            "advance_closure_sec", "congestion_spike_threshold",
            "gps_poll_interval_sec", "scoring_weights",
        }
        assert response["evidence"]["movement_id"] == "m1"
        assert response["evidence"]["baseline_constants"]["advance_closure_sec"] == 120


# ===========================================================================
# 6. Shadow evaluator
# ===========================================================================


def run(baseline_vh, candidate_vh, calibrated=True, stub=False, ambulance_b=0.0, ambulance_c=0.0):
    baseline = TwinRun(
        total_disruption_vehicle_hours=baseline_vh,
        calibrated=calibrated,
        is_stub=stub,
        ambulance_delay_sec=ambulance_b,
    )
    candidate = TwinRun(
        total_disruption_vehicle_hours=candidate_vh,
        calibrated=calibrated,
        is_stub=stub,
        ambulance_delay_sec=ambulance_c,
    )
    return baseline, candidate


class TestShadowEvaluator:
    ev = ShadowEvaluator()

    def test_pass_when_not_worse(self):
        b, c = run(100.0, 95.0)
        outcome = self.ev.evaluate(AdaptiveParams.baseline(), b, c)
        assert outcome.passed

    def test_pass_within_relative_tolerance(self):
        b, c = run(100.0, 104.9)  # +4.9% <= 5%
        assert self.ev.evaluate(AdaptiveParams.baseline(), b, c).passed

    def test_fail_beyond_tolerance(self):
        b, c = run(100.0, 106.1)  # +6.1% > 5%
        outcome = self.ev.evaluate(AdaptiveParams.baseline(), b, c)
        assert outcome.status is ShadowEvalStatus.FAILED
        assert "disruption_regression" in outcome.detail

    def test_abs_floor_tolerance_for_tiny_runs(self):
        # baseline 1 vh: 5% = 0.05, but abs floor 1.0 vh applies
        b, c = run(1.0, 1.9)
        assert self.ev.evaluate(AdaptiveParams.baseline(), b, c).passed
        b, c = run(1.0, 3.1)
        assert self.ev.evaluate(AdaptiveParams.baseline(), b, c).status is (
            ShadowEvalStatus.FAILED
        )

    def test_ambulance_tolerance(self):
        b, c = run(100.0, 100.0, ambulance_b=10.0, ambulance_c=25.0)
        outcome = self.ev.evaluate(AdaptiveParams.baseline(), b, c)
        assert outcome.status is ShadowEvalStatus.FAILED
        assert "ambulance_regression" in outcome.detail

    def test_stub_twin_unavailable(self):
        b, c = run(100.0, 90.0, stub=True)
        assert self.ev.evaluate(AdaptiveParams.baseline(), b, c).status is (
            ShadowEvalStatus.UNAVAILABLE
        )

    def test_uncalibrated_twin_unavailable(self):
        b, c = run(100.0, 90.0, calibrated=False)
        outcome = self.ev.evaluate(AdaptiveParams.baseline(), b, c)
        assert outcome.status is ShadowEvalStatus.UNAVAILABLE
        assert outcome.detail == "twin_not_calibrated"

    def test_missing_runs_unavailable(self):
        assert (
            self.ev.evaluate(AdaptiveParams.baseline(), None, None).status
            is ShadowEvalStatus.UNAVAILABLE
        )

    def test_twin_error_unavailable(self):
        outcome = self.ev.evaluate(
            AdaptiveParams.baseline(), None, None, twin_error="RuntimeError"
        )
        assert outcome.status is ShadowEvalStatus.UNAVAILABLE
        assert "twin_error" in outcome.detail

    def test_payload_parsing_runs_list(self):
        payload = {
            "runs": [
                {"total_disruption_vehicle_hours": 10.0, "ambulance_delay_sec": 2.0},
                {"total_disruption_vehicle_hours": 20.0, "ambulance_delay_sec": 4.0},
            ],
            "calibrated": True,
        }
        twin_run = TwinRun.from_payload(payload)
        assert twin_run.total_disruption_vehicle_hours == pytest.approx(15.0)
        assert twin_run.ambulance_delay_sec == pytest.approx(3.0)
        assert twin_run.calibrated and not twin_run.is_stub

    def test_payload_parsing_single_run(self):
        twin_run = TwinRun.from_payload(
            {"total_disruption_vehicle_hours": 7.0, "calibrated": False}
        )
        assert twin_run.total_disruption_vehicle_hours == 7.0
        assert not twin_run.calibrated

    def test_empty_runs_raises(self):
        with pytest.raises(ValueError):
            TwinRun.from_payload({"runs": [], "calibrated": True})

    def test_negative_tolerance_rejected(self):
        with pytest.raises(ValueError):
            ShadowEvaluator(disruption_tolerance=-0.1)


# ===========================================================================
# 7. Escalation
# ===========================================================================


class TestEscalation:
    eng = EscalationEngine()

    def test_no_escalation_on_calm(self):
        d = self.eng.evaluate(
            SituationClass.GREEN, SecurityTier.X, DegradationTier.FULL
        )
        assert not d.escalate and d.level is EscalationLevel.NONE and d.handoff == "none"

    def test_black_takeover(self):
        for tier in SecurityTier:
            d = self.eng.evaluate(
                SituationClass.BLACK, tier, DegradationTier.FULL
            )
            assert d.escalate and d.level is EscalationLevel.TAKEOVER
            assert "SITUATION_BLACK" in d.reasons
            assert d.handoff == "operator"

    @pytest.mark.parametrize("tier", [SecurityTier.Z, SecurityTier.Z_PLUS])
    def test_red_sustained_high_tier_takeover(self, tier):
        d = self.eng.evaluate(
            SituationClass.RED,
            tier,
            DegradationTier.FULL,
            EscalationInputs(red_sustained_sec=300.0),
        )
        assert d.level is EscalationLevel.TAKEOVER
        assert "RED_SUSTAINED_HIGH_TIER" in d.reasons

    def test_red_sustained_low_tier_no_takeover(self):
        d = self.eng.evaluate(
            SituationClass.RED,
            SecurityTier.Y,
            DegradationTier.FULL,
            EscalationInputs(red_sustained_sec=3600.0),
        )
        assert d.level is EscalationLevel.NONE

    def test_red_sustained_below_window_no_takeover(self):
        d = self.eng.evaluate(
            SituationClass.RED,
            SecurityTier.Z,
            DegradationTier.FULL,
            EscalationInputs(red_sustained_sec=299.9),
        )
        assert d.level is EscalationLevel.NONE

    def test_rules_only_in_red_takeover(self):
        d = self.eng.evaluate(
            SituationClass.RED, SecurityTier.X, DegradationTier.RULES_ONLY
        )
        assert d.level is EscalationLevel.TAKEOVER
        assert "MODEL_OUT_IN_RED" in d.reasons

    def test_tactical_fallback_in_red_notify(self):
        d = self.eng.evaluate(
            SituationClass.RED, SecurityTier.X, DegradationTier.TACTICAL_FALLBACK
        )
        assert d.level is EscalationLevel.NOTIFY
        assert "DECIDER_DEGRADED_IN_RED" in d.reasons

    def test_tactical_fallback_in_green_quiet(self):
        d = self.eng.evaluate(
            SituationClass.GREEN, SecurityTier.Z, DegradationTier.TACTICAL_FALLBACK
        )
        assert d.level is EscalationLevel.NONE

    def test_shadow_failed_notify(self):
        d = self.eng.evaluate(
            SituationClass.GREEN,
            SecurityTier.X,
            DegradationTier.FULL,
            EscalationInputs(shadow_eval_failed=True),
        )
        assert d.level is EscalationLevel.NOTIFY
        assert "SHADOW_EVAL_FAILED" in d.reasons

    def test_gps_stale_takeover_in_amber_plus(self):
        for band in (SituationClass.AMBER, SituationClass.RED, SituationClass.BLACK):
            d = self.eng.evaluate(
                band, SecurityTier.X, DegradationTier.FULL,
                EscalationInputs(last_gps_age_sec=30.1),
            )
            assert d.level is EscalationLevel.TAKEOVER
            assert "GPS_STALE" in d.reasons
        d = self.eng.evaluate(
            SituationClass.GREEN, SecurityTier.X, DegradationTier.FULL,
            EscalationInputs(last_gps_age_sec=999.0),
        )
        assert d.level is EscalationLevel.NONE  # GREEN exempt

    def test_ambulance_delay_windows(self):
        d = self.eng.evaluate(
            SituationClass.GREEN, SecurityTier.X, DegradationTier.FULL,
            EscalationInputs(ambulance_delay_projection_sec=60.0),
        )
        assert d.level is EscalationLevel.NOTIFY
        assert "AMBULANCE_DELAY_ELEVATED" in d.reasons
        d = self.eng.evaluate(
            SituationClass.GREEN, SecurityTier.X, DegradationTier.FULL,
            EscalationInputs(ambulance_delay_projection_sec=120.0),
        )
        assert d.level is EscalationLevel.TAKEOVER
        assert "AMBULANCE_DELAY_SEVERE" in d.reasons

    def test_crowd_extreme_zplus_advisory(self):
        d = self.eng.evaluate(
            SituationClass.AMBER,
            SecurityTier.Z_PLUS,
            DegradationTier.FULL,
            EscalationInputs(crowd_extreme=True),
        )
        assert "CROWD_EXTREME_HIGH_TIER" in d.reasons
        assert d.level is EscalationLevel.NOTIFY
        # same flag on Z stays quiet
        d = self.eng.evaluate(
            SituationClass.AMBER,
            SecurityTier.Z,
            DegradationTier.FULL,
            EscalationInputs(crowd_extreme=True),
        )
        assert d.level is EscalationLevel.NONE

    def test_level_precedence_and_reason_accumulation(self):
        d = self.eng.evaluate(
            SituationClass.RED,
            SecurityTier.Z_PLUS,
            DegradationTier.RULES_ONLY,
            EscalationInputs(
                red_sustained_sec=400.0,
                shadow_eval_failed=True,
                last_gps_age_sec=45.0,
                ambulance_delay_projection_sec=130.0,
            ),
        )
        assert d.level is EscalationLevel.TAKEOVER
        assert list(d.reasons) == [
            "RED_SUSTAINED_HIGH_TIER",
            "MODEL_OUT_IN_RED",
            "SHADOW_EVAL_FAILED",
            "GPS_STALE",
            "AMBULANCE_DELAY_SEVERE",
        ]
        assert d.to_dict()["handoff"] == "operator"


# ===========================================================================
# 8. Degradation
# ===========================================================================


def snap(role, ok=True, latency=100.0):
    return ModelHealthSnapshot(role=role, ok=ok, latency_ms=latency)


class TestDegradation:
    def test_decider_slow_twice_degrades_to_tactical_fallback(self):
        clock = FakeClock(100.0)
        mgr = DegradationManager(clock=clock)
        assert mgr.tier is DegradationTier.FULL
        mgr.observe_health(snap("decider", latency=3500.0))
        assert mgr.tier is DegradationTier.FULL  # 1 strike: hold
        mgr.observe_health(snap("decider", latency=3500.0))
        assert mgr.tier is DegradationTier.TACTICAL_FALLBACK
        assert mgr.audit_trail[-1].to_tier if hasattr(mgr.audit_trail[-1], "to_tier") else True
        assert mgr.audit_trail[-1].to_tier == "TACTICAL_FALLBACK"

    def test_single_slow_snapshot_does_not_degrade(self):
        mgr = DegradationManager(clock=FakeClock())
        mgr.observe_health(snap("decider", latency=10_000.0))
        assert mgr.tier is DegradationTier.FULL

    def test_decider_down_twice_degrades(self):
        mgr = DegradationManager(clock=FakeClock())
        mgr.observe_health(snap("decider", ok=False))
        assert mgr.tier is DegradationTier.FULL
        mgr.observe_health(snap("decider", ok=False))
        assert mgr.tier is DegradationTier.TACTICAL_FALLBACK

    def test_both_out_rules_only(self):
        clock = FakeClock(0.0)
        mgr = DegradationManager(clock=clock)
        for role in ("decider", "gemma"):
            mgr.observe_health(snap(role, ok=False))
            mgr.observe_health(snap(role, ok=False))
        assert mgr.tier is DegradationTier.RULES_ONLY
        assert mgr.forces_baseline() is True

    def test_gemma_alone_out_keeps_full(self):
        mgr = DegradationManager(clock=FakeClock())
        mgr.observe_health(snap("gemma", ok=False))
        mgr.observe_health(snap("gemma", ok=False))
        assert mgr.tier is DegradationTier.FULL  # decision stack intact

    def test_between_thresholds_is_neutral_evidence(self):
        clock = FakeClock(0.0)
        mgr = DegradationManager(clock=clock)
        mgr.observe_health(snap("decider", latency=3500.0))
        mgr.observe_health(snap("decider", latency=3500.0))
        assert mgr.tier is DegradationTier.TACTICAL_FALLBACK
        # between recover (2000) and slow (3000): neither direction advances
        mgr.observe_health(snap("decider", latency=2500.0))
        mgr.observe_health(snap("decider", latency=2500.0))
        mgr.observe_health(snap("decider", latency=2500.0))
        mgr.observe_health(snap("decider", latency=2500.0))
        mgr.observe_health(snap("decider", latency=2500.0))
        clock.now = 10_000.0
        mgr.observe_health(snap("decider", latency=2500.0))
        assert mgr.tier is DegradationTier.TACTICAL_FALLBACK  # still degraded

    def test_latency_flap_immune(self):
        clock = FakeClock(0.0)
        mgr = DegradationManager(clock=clock)
        for i in range(30):
            latency = 3500.0 if i % 2 == 0 else 100.0
            mgr.observe_health(snap("decider", latency=latency))
            clock.now += 10.0
        assert mgr.tier is DegradationTier.FULL

    def test_recovery_needs_consecutive_healthy_and_dwell(self):
        clock = FakeClock(0.0)
        mgr = DegradationManager(clock=clock)
        mgr.observe_health(snap("decider", latency=3500.0))
        mgr.observe_health(snap("decider", latency=3500.0))
        assert mgr.tier is DegradationTier.TACTICAL_FALLBACK
        # 5 healthy but dwell not elapsed
        clock.now = 10.0
        for _ in range(5):
            mgr.observe_health(snap("decider", latency=100.0))
        assert mgr.tier is DegradationTier.TACTICAL_FALLBACK
        # dwell elapsed + continued health
        clock.now = 100.0
        mgr.observe_health(snap("decider", latency=100.0))
        assert mgr.tier is DegradationTier.FULL
        assert mgr.audit_trail[-1].to_tier == "FULL"

    def test_recovery_reset_by_one_bad_snapshot(self):
        clock = FakeClock(0.0)
        mgr = DegradationManager(clock=clock)
        mgr.observe_health(snap("decider", latency=4000.0))
        mgr.observe_health(snap("decider", latency=4000.0))
        clock.now = 100.0
        for _ in range(4):
            mgr.observe_health(snap("decider", latency=100.0))
        mgr.observe_health(snap("decider", latency=3600.0))  # bad blip
        mgr.observe_health(snap("decider", latency=100.0))
        assert mgr.tier is DegradationTier.TACTICAL_FALLBACK

    def test_status_surfaces_everything(self):
        clock = FakeClock(0.0)
        mgr = DegradationManager(clock=clock)
        mgr.observe_health(snap("decider", latency=3100.0))
        mgr.observe_health(snap("decider", latency=3100.0))
        status = mgr.status()
        assert status["degradation_tier"] == "TACTICAL_FALLBACK"
        assert status["decider_checkpoints_paused"] is True
        assert status["adaptive_params_forced_baseline"] is False
        assert status["roles"]["decider"]["outage"] is True
        assert status["roles"]["decider"]["last_latency_ms"] == 3100.0
        assert len(status["audit_trail"]) == 1
        assert status["audit_trail"][0]["from"] == "FULL"
        assert status["thresholds"]["decider_slow_ms"] == 3000.0


# ===========================================================================
# 9. AdaptiveService pipeline (fake ports)
# ===========================================================================


def inputs_for(
    vvip="Z",
    congestion=0.1,
    weather=0.0,
    crowd=0.0,
    anomaly=False,
    event=False,
) -> SituationInputs:
    return SituationInputs.from_scores(
        vvip_class=vvip,
        predicted_congestion_idx=congestion,
        weather_severity=weather,
        crowd_score=crowd,
        anomaly_active=anomaly,
        event_active=event,
    )


class TestAdaptiveService:
    async def test_first_observe_establishes_band_without_change_event(self):
        service, clock, sink, pers = make_service()
        result = await service.observe("m1", inputs_for(vvip="Z"))
        assert result.effective_class is SituationClass.GREEN
        assert not result.transitioned
        assert sink.changes == []
        assert len(pers.situations) == 1

    async def test_situation_change_published_only_on_transition(self):
        service, clock, sink, pers = make_service()
        # first observation initializes the band AT the observed class
        # (Z + rain + event = 4+1+1 = 6 → AMBER); no change notification
        await service.observe("m1", inputs_for(vvip="Z", weather=0.3, event=True))
        assert sink.changes == []  # initialization is not a transition
        # escalate: gridlock (6) + rain (1) + event (1) + Z (4) = 12 → RED
        clock.advance(10)
        r = await service.observe(
            "m1", inputs_for(vvip="Z", congestion=0.9, weather=0.3, event=True)
        )
        assert r.situation.situation_class is SituationClass.RED
        assert not r.transitioned  # 1st RED observation: awaiting confirm
        r2 = await service.observe(  # second consecutive RED confirms
            "m1", inputs_for(vvip="Z", congestion=0.9, weather=0.3, event=True)
        )
        assert r2.transitioned and r2.effective_class is SituationClass.RED
        assert len(sink.changes) == 1
        assert sink.changes[0]["from"] == "AMBER" and sink.changes[0]["to"] == "RED"

    async def test_params_fail_closed_without_twin(self):
        service, *_ = make_service()
        await service.observe("m1", inputs_for(vvip="Z", weather=0.3, event=True))
        response = await service.params_for("m1", SecurityTier.Z)
        assert response["source"] == "baseline"
        assert response["shadow_eval"] == "unavailable"
        assert response["evidence"]["shadow_eval_required"] is True
        assert (
            response["evidence"]["fail_closed_reason"]
            == "shadow_eval_unavailable_fail_closed"
        )
        assert response["params"] == AdaptiveParams.baseline().to_dict()

    async def test_params_published_with_passing_twin(self):
        twin = passing_twin()
        service, clock, sink, _ = make_service(twin=twin)
        await service.observe("m1", inputs_for(vvip="Z", weather=0.3, event=True))
        response = await service.params_for(
            "m1", SecurityTier.Z, shadow_context=ctx()
        )
        assert twin.calls == 1
        assert response["source"] == "retrieved"
        assert response["shadow_eval"] == "passed"
        assert response["params"]["congestion_spike_threshold"] == 0.75  # AMBER

    async def test_params_fail_closed_when_twin_rejects(self):
        twin = FakeTwinPort(
            baseline_run=TwinRun(100.0, True, False),
            candidate_run=TwinRun(140.0, True, False),
        )
        service, *_ = make_service(twin=twin)
        await service.observe("m1", inputs_for(vvip="Z", weather=0.3, event=True))
        response = await service.params_for("m1", SecurityTier.Z, shadow_context=ctx())
        assert response["source"] == "baseline"
        assert response["shadow_eval"] == "unavailable"
        assert (
            response["evidence"]["fail_closed_reason"]
            == "shadow_eval_failed_deviation_rejected"
        )
        assert "disruption_regression" in response["evidence"]["shadow_eval_detail"]
        # escalation R5 fired and surfaced
        assert response["evidence"]["escalation"]["reasons"] == ["SHADOW_EVAL_FAILED"]

    async def test_unknown_movement_returns_baseline_green(self):
        service, *_ = make_service()
        response = await service.params_for("never-seen", SecurityTier.Y)
        assert response["situation_class"] == "GREEN"
        assert response["source"] == "baseline"
        assert response["evidence"]["observation_count"] == 0

    async def test_rules_only_degradation_forces_baseline(self):
        twin = passing_twin()
        service, clock, *_ = make_service(twin=twin)
        await service.observe("m1", inputs_for(vvip="Z", weather=0.3, event=True))
        # degrade both roles
        service.feed_health(snap("decider", ok=False))
        service.feed_health(snap("gemma", ok=False))
        service.feed_health(snap("decider", ok=False))
        service.feed_health(snap("gemma", ok=False))
        response = await service.params_for(
            "m1", SecurityTier.Z, shadow_context=ctx()
        )
        assert response["source"] == "baseline"
        assert response["evidence"]["degradation_tier"] == "RULES_ONLY"
        assert (
            response["evidence"]["fail_closed_reason"]
            == "forced_baseline_degradation"
        )

    async def test_params_persisted_only_on_change_mirror_kept_alive(self):
        """One audit row per decision change; the TTL mirror is refreshed
        (same payload) at most every PARAMS_MIRROR_REFRESH_SEC in between."""
        from traffic_oracle.adaptive.service import PARAMS_MIRROR_REFRESH_SEC

        service, clock, sink, pers = make_service()
        await service.observe("m1", inputs_for(vvip="Z"))
        await service.params_for("m1", SecurityTier.Z)
        await service.params_for("m1", SecurityTier.Z)  # unchanged, too soon
        assert len(pers.param_rows) == 1
        assert pers.param_rows[0]["evidence"]["publish_reason"] == "first_publish"
        assert len(sink.params) == 1
        movement_id, payload = sink.params[0]
        assert movement_id == "m1"
        assert "advance_closure_sec" in json.loads(payload)

        clock.advance(PARAMS_MIRROR_REFRESH_SEC)
        await service.params_for("m1", SecurityTier.Z)  # keepalive only
        assert len(pers.param_rows) == 1
        assert len(sink.params) == 2 and sink.params[1] == sink.params[0]

        # A class change (GREEN -> AMBER) is a new decision: a new row.
        for _ in range(2):
            await service.observe("m1", inputs_for(vvip="Z", weather=0.3, event=True))
        assert service.movement_state("m1").effective_class is SituationClass.AMBER
        await service.params_for("m1", SecurityTier.Z)
        assert len(pers.param_rows) == 2
        assert pers.param_rows[1]["situation_class"] == "AMBER"
        assert pers.param_rows[1]["evidence"]["publish_reason"] == "class_change"

    async def test_untracked_subject_is_never_persisted_or_created(self):
        service, _, sink, pers = make_service()
        response = await service.params_for("ghost", SecurityTier.Z)
        assert response["source"] == "baseline"
        assert response["evidence"]["subject_tracked"] is False
        assert service.movement_state("ghost") is None
        assert pers.param_rows == [] and sink.params == []

    async def test_no_param_oscillation_under_noisy_band(self):
        """Band flapping at the input must not flap published params."""
        service, clock, sink, _ = make_service()
        # seed GREEN
        await service.observe("m1", inputs_for(vvip="Z"))
        params_seen: list[int] = []
        # alternate AMBER (would-be deviation) and GREEN observations
        for i in range(20):
            obs = (
                inputs_for(vvip="Z", weather=0.3, event=True)
                if i % 2 == 0
                else inputs_for(vvip="Z")
            )
            clock.advance(30)
            await service.observe("m1", obs)
            response = await service.params_for("m1", SecurityTier.Z)
            params_seen.append(response["params"]["congestion_spike_threshold"])
        # never left baseline sensitivity: band never settled on AMBER
        assert set(params_seen) == {0.8}

    async def test_movement_isolation(self):
        service, *_ = make_service()
        await service.observe("m1", inputs_for(vvip="Z"))
        await service.observe(
            "m2", inputs_for(vvip="Z+", congestion=0.95, weather=0.9, crowd=0.9)
        )
        status = service.status()
        assert status["movements"]["m1"]["effective_class"] == "GREEN"
        assert status["movements"]["m2"]["vvip_class"] == "Z+"

    async def test_red_sustained_escalation_flow(self):
        service, clock, *_ = make_service()
        # Z + gridlock + storm + extreme crowd = 4+6+3+3 = 16 → BLACK... use RED: Z + gridlock + rain + high = 4+6+1+2=13
        red_inputs = inputs_for(vvip="Z", congestion=0.9, weather=0.3, crowd=0.6)
        await service.observe("m1", red_inputs)
        await service.observe("m1", red_inputs)
        clock.advance(400)
        response = await service.params_for("m1", SecurityTier.Z)
        assert response["situation_class"] == "RED"
        assert (
            "RED_SUSTAINED_HIGH_TIER"
            in response["evidence"]["escalation"]["reasons"]
        )
        assert response["evidence"]["escalation"]["level"] == "TAKEOVER"

    async def test_tier_update_from_latest_observation(self):
        service, *_ = make_service()
        await service.observe("m1", inputs_for(vvip="Y"))
        await service.observe("m1", inputs_for(vvip="Z+"))
        assert service.movement_state("m1").vvip_class is SecurityTier.Z_PLUS


# ===========================================================================
# 10. Router (bare app + full app fallback)
# ===========================================================================


class TestRouter:
    @pytest.fixture()
    def bare_app(self):
        from fastapi import FastAPI

        from traffic_oracle.adaptive.router import router as adaptive_router

        app = FastAPI()
        app.include_router(adaptive_router, prefix="/api/v1")
        return app

    def test_params_fallback_without_service(self, bare_app):
        from fastapi.testclient import TestClient

        with TestClient(bare_app) as client:
            r = client.get("/api/v1/adaptive/params?movement_id=m1&vvip_class=Y")
            assert r.status_code == 200
            body = r.json()
            assert body["source"] == "baseline"
            assert body["params"]["advance_closure_sec"] == 120
            assert body["evidence"]["fallback"] is True

    def test_params_invalid_tier_400(self, bare_app):
        from fastapi.testclient import TestClient

        with TestClient(bare_app) as client:
            r = client.get("/api/v1/adaptive/params?vvip_class=Q")
            assert r.status_code == 400

    def test_status_not_configured(self, bare_app):
        from fastapi.testclient import TestClient

        with TestClient(bare_app) as client:
            r = client.get("/api/v1/adaptive/status")
            assert r.status_code == 200
            assert r.json() == {"service": "not_configured"}

    def test_observe_503_without_service(self, bare_app):
        from fastapi.testclient import TestClient

        with TestClient(bare_app) as client:
            r = client.post(
                "/api/v1/adaptive/observe",
                json={"movement_id": "m1", "vvip_class": "Z"},
            )
            assert r.status_code == 503

    def test_observe_drives_service(self, bare_app):
        from fastapi.testclient import TestClient

        with TestClient(bare_app) as app_client:
            app = bare_app
            service, *_ = make_service()
            app.state.adaptive = service
            payload = {
                "movement_id": "m1",
                "vvip_class": "Z",
                "predicted_congestion_idx": 0.2,
                "weather_severity": 0.1,
                "crowd_score": 0.1,
            }
            r = app_client.post("/api/v1/adaptive/observe", json=payload)
            assert r.status_code == 200
            body = r.json()
            assert body["effective_class"] == "GREEN"
            assert body["params_response"]["source"] == "baseline"
            # bad weather code → 400
            r2 = app_client.post(
                "/api/v1/adaptive/observe",
                json={"movement_id": "m1", "vvip_class": "Z", "weather": "HAMMER"},
            )
            assert r2.status_code == 400

    def test_health_endpoint_feeds_degradation(self, bare_app):
        from fastapi.testclient import TestClient

        with TestClient(bare_app) as app_client:
            service, *_ = make_service()
            bare_app.state.adaptive = service
            for _ in range(2):
                r = app_client.post(
                    "/api/v1/adaptive/health",
                    json={"role": "decider", "ok": True, "latency_ms": 3400.0},
                )
                assert r.status_code == 200
            assert r.json()["degradation_tier"] == "TACTICAL_FALLBACK"


# ===========================================================================
# 10b. End-to-end: real app + real in-process twin (C4 fail-closed pre-warmup)
# ===========================================================================


class TestRealAppIntegration:
    def _app(self):
        import traffic_oracle.__main__ as main

        return main.create_app()

    def test_twin_mounted_and_uncalibrated_fail_closed(self):
        """Against the REAL simulation router: uncalibrated twin → params
        fail closed to baseline even when a shadow context is provided."""
        from fastapi.testclient import TestClient

        app = self._app()
        client = TestClient(app)
        r = client.post(
            "/api/v1/simulate/twin",
            json={
                "route_segment_ids": [1, 2, 3],
                "departure_time": "2026-10-12T10:00:00",
                "closure_policy": {
                    "advance_closure_sec": 120,
                    "closure_type": "full_closure",
                },
                "horizon_min": 30,
                "n_replications": 2,
                "seed": 7,
            },
        )
        assert r.status_code == 200
        assert r.json()["calibrated"] is False  # C5 warmup: baseline-only

        from traffic_oracle.adaptive.twin import AsgiTwinPort

        port = AsgiTwinPort(app)
        pair = asyncio.run(
            port.run_pair_async(
                ClosurePolicy(advance_closure_sec=120),
                ClosurePolicy(advance_closure_sec=180),
                ctx(),
            )
        )
        assert pair.baseline is not None and pair.candidate is not None
        service, *_ = make_service(twin=port)
        result = asyncio.run(
            service.observe("m1", inputs_for(vvip="Z", weather=0.3, event=True))
        )
        assert result.effective_class is SituationClass.AMBER
        response = asyncio.run(
            service.params_for("m1", SecurityTier.Z, shadow_context=ctx())
        )
        assert response["source"] == "baseline"
        assert response["shadow_eval"] == "unavailable"
        # The refusal names why the twin is uncalibrated (C5 calibration
        # reason, e.g. "no field observations — simulated feed only").
        assert response["evidence"]["shadow_eval_detail"].startswith(
            "twin_not_calibrated"
        )

    def test_asgi_twin_port_passes_with_calibrated_stub_app(self):
        from fastapi import FastAPI

        canned = FastAPI()

        @canned.post("/api/v1/simulate/twin")
        async def twin(payload: dict) -> dict:
            advance = payload["closure_policy"]["advance_closure_sec"]
            # candidate (180) slightly better than baseline (120)
            vh = 100.0 if advance == 120 else 96.0
            return {
                "runs": [
                    {
                        "replication": 1,
                        "total_disruption_vehicle_hours": vh,
                        "max_queue_length_m": 10,
                        "avg_closure_duration_sec": advance,
                        "convoy_transit_time_sec": 500,
                        "recovery_sec": 60,
                        "ambulance_delay_sec": 5,
                    }
                ],
                "calibrated": True,
            }

        from traffic_oracle.adaptive.twin import AsgiTwinPort

        port = AsgiTwinPort(canned)
        service, *_ = make_service(twin=port)
        asyncio.run(
            service.observe("m1", inputs_for(vvip="Z", weather=0.3, event=True))
        )
        response = asyncio.run(
            service.params_for("m1", SecurityTier.Z, shadow_context=ctx())
        )
        assert response["shadow_eval"] == "passed"
        assert response["source"] == "retrieved"
        assert response["params"]["advance_closure_sec"] == 120  # AMBER: baseline advance
        assert response["params"]["congestion_spike_threshold"] == 0.75  # deviation applied

    def test_sync_run_pair_forbidden_inside_running_loop(self):
        import traffic_oracle.__main__ as main
        from traffic_oracle.adaptive.twin import AsgiTwinPort

        app = main.create_app()
        port = AsgiTwinPort(app)

        async def attempt():
            port.run_pair(
                ClosurePolicy(120), ClosurePolicy(180), ctx()
            )

        with pytest.raises(RuntimeError):
            asyncio.run(attempt())

    def test_unavailable_port(self):
        port = UnavailableTwinPort()
        pair = port.run_pair(
            ClosurePolicy(120), ClosurePolicy(180), ctx()
        )
        assert pair.error == "twin_port_not_configured"
        assert pair.baseline is None and pair.candidate is None

    def test_shadow_context_validation(self):
        with pytest.raises(ValueError):
            ShadowContext(route_segment_ids=(), departure_time="x")
        with pytest.raises(ValueError):
            ShadowContext(route_segment_ids=(1,), departure_time="x", horizon_min=61)
        with pytest.raises(ValueError):
            ShadowContext(
                route_segment_ids=(1,), departure_time="x", n_replications=21
            )
        good = ShadowContext(route_segment_ids=(1, 2), departure_time="x")
        req = good.to_request(ClosurePolicy(120))
        assert req["closure_policy"] == {
            "advance_closure_sec": 120,
            "closure_type": "full_closure",
        }
        assert req["n_replications"] == 5 and req["horizon_min"] == 30


# ===========================================================================
# 11. gpu_arbiter v2
# ===========================================================================


def fake_meminfo(total_kb=127_533_976, available_kb=100_000_000):
    def reader(path="/proc/meminfo"):
        return {"MemTotal": total_kb, "MemAvailable": available_kb}

    return reader


class TestGpuArbiter:
    def test_explicit_budget_wins(self):
        from traffic_oracle.runtime.gpu_arbiter import GpuArbiter

        arb = GpuArbiter(
            budget_mb=9999,
            env={},
            meminfo_reader=fake_meminfo(),
        )
        assert arb.unified_budget_mb == 9999
        assert arb.budget_source == "explicit"

    def test_env_budget(self, tmp_path):
        from traffic_oracle.runtime.gpu_arbiter import GpuArbiter

        arb = GpuArbiter(
            env={"VEER_UNIFIED_BUDGET_MB": "8888"},
            yaml_path=tmp_path / "nonexistent.yaml",
            meminfo_reader=fake_meminfo(),
        )
        assert arb.unified_budget_mb == 8888
        assert arb.budget_source == "env"

    def test_yaml_budget_mb(self, tmp_path):
        pytest.importorskip("yaml")
        from traffic_oracle.runtime.gpu_arbiter import GpuArbiter

        cfg = tmp_path / "veer_models.yaml"
        cfg.write_text("memory:\n  unified_budget_mb: 7777\n")
        arb = GpuArbiter(env={}, yaml_path=cfg, meminfo_reader=fake_meminfo())
        assert arb.unified_budget_mb == 7777
        assert arb.budget_source.startswith("yaml:")

    def test_yaml_budget_gib(self, tmp_path):
        pytest.importorskip("yaml")
        from traffic_oracle.runtime.gpu_arbiter import GpuArbiter

        cfg = tmp_path / "veer_models.yaml"
        cfg.write_text("memory:\n  unified_budget_gib: 121.44\n")
        arb = GpuArbiter(env={}, yaml_path=cfg, meminfo_reader=fake_meminfo())
        assert arb.unified_budget_mb == 124_354  # int(121.44 * 1024)

    def test_meminfo_fallback(self, tmp_path):
        from traffic_oracle.runtime.gpu_arbiter import GpuArbiter

        arb = GpuArbiter(
            env={},
            yaml_path=tmp_path / "nonexistent.yaml",
            meminfo_reader=fake_meminfo(total_kb=127_533_976),
        )
        assert arb.unified_budget_mb == 124_544
        assert arb.budget_source == "proc_meminfo"

    def test_default_when_nothing_available(self, tmp_path):
        from traffic_oracle.runtime.gpu_arbiter import GpuArbiter

        arb = GpuArbiter(
            env={},
            yaml_path=tmp_path / "nonexistent.yaml",
            meminfo_reader=lambda path="/proc/meminfo": {},
        )
        from traffic_oracle.runtime.gpu_arbiter import DEFAULT_UNIFIED_BUDGET_MB

        assert arb.unified_budget_mb == DEFAULT_UNIFIED_BUDGET_MB
        assert arb.budget_source == "default"

    def test_cuda_provider_when_memory_plenty(self):
        from traffic_oracle.runtime.gpu_arbiter import GpuArbiter

        arb = GpuArbiter(
            budget_mb=121_440,
            env={},
            meminfo_reader=fake_meminfo(available_kb=100_000_000),
        )
        assert arb.get_free_vram_mb() == 97_656
        assert arb.can_allocate_onnx() is True
        assert arb.can_train() is True
        assert arb.get_onnx_providers() == ["CUDAExecutionProvider", "CPUExecutionProvider"]

    def test_cpu_fallback_when_memory_tight(self):
        from traffic_oracle.runtime.gpu_arbiter import GpuArbiter

        arb = GpuArbiter(
            budget_mb=8192,
            env={},
            meminfo_reader=fake_meminfo(available_kb=1_000_000),  # ~977 MiB
        )
        assert arb.can_allocate_onnx() is False
        assert arb.get_onnx_providers() == ["CPUExecutionProvider"]

    def test_train_needs_4gb(self):
        from traffic_oracle.runtime.gpu_arbiter import GpuArbiter

        arb = GpuArbiter(
            budget_mb=121_440, env={}, meminfo_reader=fake_meminfo(available_kb=3_000_000)
        )
        assert arb.can_train() is False  # 2929 MiB < 4096

    def test_real_meminfo_read_on_this_machine(self):
        """Hardware-fact source: /proc/meminfo parses on this host."""
        from traffic_oracle.runtime.gpu_arbiter import (
            _read_meminfo,
            meminfo_memavailable_mb,
            meminfo_memtotal_mb,
        )

        info = _read_meminfo()
        assert info.get("MemTotal", 0) > 0
        assert meminfo_memtotal_mb(info) == info["MemTotal"] // 1024
        assert meminfo_memavailable_mb(info) is not None

    def test_api_surface_preserved(self):
        from traffic_oracle.runtime.gpu_arbiter import GpuArbiter

        arb = GpuArbiter(budget_mb=1000, env={}, meminfo_reader=fake_meminfo())
        for method in (
            "can_allocate_onnx", "can_train", "get_onnx_providers",
            "get_free_vram_mb", "acquire_exclusive",
        ):
            assert callable(getattr(arb, method))

    async def test_exclusive_context(self):
        from traffic_oracle.runtime.gpu_arbiter import GpuArbiter

        arb = GpuArbiter(budget_mb=1000, env={}, meminfo_reader=fake_meminfo())
        async with arb.acquire_exclusive():
            pass  # lock acquired and released


# ===========================================================================
# 12. RL environment + bounds
# ===========================================================================


class TestRLEnvironment:
    def test_module_imports_without_gymnasium(self, monkeypatch):
        """F3 fix: the import graph must load even without gymnasium."""
        pytest.importorskip("gymnasium")
        import traffic_oracle.optimize.rl.environment as env_mod

        monkeypatch.setitem(sys.modules, "gymnasium", None)
        reloaded = importlib.reload(env_mod)
        assert reloaded.GYMNASIUM_AVAILABLE is False
        with pytest.raises(RuntimeError, match="gymnasium is required"):
            reloaded.SignalCorridorEnv()
        with pytest.raises(RuntimeError, match="gymnasium is required"):
            reloaded.TwinCorridorEnv()
        # restore real module state for other tests
        monkeypatch.undo()
        importlib.reload(env_mod)
        assert env_mod.GYMNASIUM_AVAILABLE is True

    def test_signal_corridor_env_still_functions(self):
        pytest.importorskip("gymnasium")
        import numpy as np

        from traffic_oracle.optimize.rl.environment import SignalCorridorEnv

        env = SignalCorridorEnv(n_signals=4, seed=42)
        obs, info = env.reset(seed=42)
        assert obs.shape == (4, 6)
        assert obs.dtype == np.float32
        total_reward = 0.0
        for _ in range(10):
            action = np.zeros(4, dtype=np.int64)
            obs, reward, terminated, truncated, info = env.step(action)
            total_reward += reward
            assert obs.shape == (4, 6)
            assert isinstance(terminated, bool) and isinstance(truncated, bool)
        assert isinstance(total_reward, float)

    def test_signal_env_deterministic_with_seed(self):
        pytest.importorskip("gymnasium")
        import numpy as np

        from traffic_oracle.optimize.rl.environment import SignalCorridorEnv

        env_a = SignalCorridorEnv(n_signals=4, seed=7)
        env_b = SignalCorridorEnv(n_signals=4, seed=7)
        obs_a, _ = env_a.reset(seed=7)
        obs_b, _ = env_b.reset(seed=7)
        assert np.array_equal(obs_a, obs_b)
        act = np.ones(4, dtype=np.int64)
        out_a = env_a.step(act)
        out_b = env_b.step(act)
        assert np.array_equal(out_a[0], out_b[0])
        assert out_a[1] == out_b[1]

    def test_twin_adapter_five_tuple(self):
        pytest.importorskip("gymnasium")
        from traffic_oracle.optimize.rl.environment import TwinCorridorEnv

        class FakeTwin:
            is_stub = False

            def __init__(self):
                self.steps = 0

            def reset(self, seed=None):
                self.steps = 0
                return [0.1, 0.2, 0.3]

            def step(self, action):
                self.steps += 1
                return (
                    [0.2, 0.3, 0.4],
                    1.5,
                    self.steps >= 3,
                    {"k": self.steps},
                )

        env = TwinCorridorEnv(twin=FakeTwin(), max_steps=100)
        obs, info = env.reset(seed=5)
        assert obs.shape == (3,)
        obs, reward, terminated, truncated, info = env.step(0)
        assert reward == 1.5 and not terminated and not truncated and info == {"k": 1}
        env.step(0)
        obs, reward, terminated, truncated, info = env.step(0)
        assert terminated is True and truncated is False

    def test_twin_adapter_truncation(self):
        pytest.importorskip("gymnasium")
        from traffic_oracle.optimize.rl.environment import TwinCorridorEnv

        class EndlessTwin:
            is_stub = False

            def reset(self, seed=None):
                return [0.0]

            def step(self, action):
                return [0.0], 0.0, False, {}

        env = TwinCorridorEnv(twin=EndlessTwin(), max_steps=3)
        env.reset()
        _, _, terminated, truncated, _ = env.step(0)
        assert not truncated
        env.step(0)
        _, _, terminated, truncated, _ = env.step(0)
        assert truncated is True and terminated is False

    def test_twin_adapter_rejects_stub(self):
        pytest.importorskip("gymnasium")
        from traffic_oracle.optimize.rl.environment import TwinCorridorEnv

        class StubTwin:
            is_stub = True

            def reset(self, seed=None):
                return []

            def step(self, action):
                return [], 0.0, True, {}

        with pytest.raises(RuntimeError, match="stub"):
            TwinCorridorEnv(twin=StubTwin())

    def test_twin_adapter_seed_passthrough(self):
        pytest.importorskip("gymnasium")
        from traffic_oracle.optimize.rl.environment import TwinCorridorEnv

        seeds = []

        class SeedTwin:
            is_stub = False

            def reset(self, seed=None):
                seeds.append(seed)
                return [1.0]

            def step(self, action):
                return [1.0], 0.0, False, {}

        env = TwinCorridorEnv(twin=SeedTwin(), seed=99)
        env.reset()
        env.reset(seed=None)  # falls back to constructor seed
        env.reset(seed=5)
        assert seeds == [99, 99, 5]

    def test_rl_bounds(self):
        from traffic_oracle.optimize.rl.bounds import (
            baseline_snapshot,
            bound_advance_closure_sec,
            bound_congestion_spike_threshold,
            bound_gps_poll_interval_sec,
        )

        # protocol floors respected through the RL path
        assert bound_advance_closure_sec(10, "Z+") == 180
        assert bound_advance_closure_sec(10, "Z") == 120
        assert bound_advance_closure_sec(10, "Y") == 60
        assert bound_advance_closure_sec(10, "X") == 30  # search-space floor
        assert bound_advance_closure_sec(10_000, "X") == 600  # search-space cap
        assert bound_advance_closure_sec(-5, "Y") == 60
        assert bound_congestion_spike_threshold(0.0) == 0.1
        assert bound_congestion_spike_threshold(0.95) == 0.8
        # GPS poll floor is the consumer-side protocol floor (1.0 s), so the
        # RL path can never propose a value the brain would silently clamp.
        assert bound_gps_poll_interval_sec(0.0) == 1.0
        assert bound_gps_poll_interval_sec(99.0) == 5.0
        snap_zp = baseline_snapshot("Z+")
        assert snap_zp["advance_closure_sec"] == 180 and snap_zp["floor_applied"]
        snap_x = baseline_snapshot("X")
        assert snap_x["advance_closure_sec"] == 120 and not snap_x["floor_applied"]


# ===========================================================================
# 13. __main__ wiring
# ===========================================================================


class TestMainWiring:
    source = (SRC_ROOT / "traffic_oracle" / "__main__.py").read_text()

    def test_adaptive_router_included(self):
        assert "app.include_router(adaptive_router" in self.source

    def test_simulation_router_included(self):
        # the signalling stream's writer-gate wraps the include with
        # dependencies=_twin_gate_deps — pin on the call prefix, not the
        # exact argument list
        assert "app.include_router(simulation_router" in self.source
        assert "from traffic_oracle.simulation.router import" in self.source

    def test_engine_loop_started_in_lifespan(self):
        assert "start_situation_engine(" in self.source
        assert "app.state.adaptive = adaptive_service" in self.source

    def test_adaptive_service_built_with_ports(self):
        assert "AsyncpgPersister(pool)" in self.source
        assert "LoggingValkeySink(" in self.source
        assert "build_twin_port(app)" in self.source

    def test_app_builds_and_serves_fallback_params(self):
        import traffic_oracle.__main__ as main
        from fastapi.testclient import TestClient

        app = main.create_app()
        assert main.simulation_router is not None  # simulation stream landed
        client = TestClient(app)  # bare: lifespan NOT run → fallback path
        r = client.get("/api/v1/adaptive/params?movement_id=m&vvip_class=Z%2B")
        assert r.status_code == 200
        assert r.json()["params"]["advance_closure_sec"] == 180
        assert r.json()["evidence"]["floor_applied"] is True

    def test_calibration_primed_before_the_situation_engine_starts(self):
        """The loop's first decision must quote the FINAL calibration reason:
        the lifespan awaits the scheduler's prime before starting the loop."""
        prime = self.source.index("await calibration_scheduler.prime()")
        start = self.source.index("engine_loop = start_situation_engine(")
        assert prime < start
        assert "twin_readiness=CALIBRATION.readiness" in self.source

    def test_lifespan_wires_service(self):
        """Unit-level: with fakes injected, lifespan builds the service.

        We cannot run the real lifespan (needs PostGIS/Valkey); instead we
        assert the wiring symbolically via the fake-pipeline tests above
        and check lifespan() references the loop teardown.
        """
        assert "engine_loop.stop()" in self.source


# ===========================================================================
# 14. Migration 005 guards
# ===========================================================================


class TestMigration005:
    sql = (MIGRATIONS / "005_adaptive.sql").read_text()

    def test_file_exists_between_004_and_006(self):
        assert (MIGRATIONS / "004_memory_embeddings.sql").exists()
        assert (MIGRATIONS / "005_adaptive.sql").exists()
        assert (MIGRATIONS / "006_simulation_runs.sql").exists()

    def test_every_create_is_idempotent(self):
        import re

        creates = re.findall(r"CREATE\s+(TABLE|INDEX|SCHEMA)\s+(?!IF NOT EXISTS)", self.sql, re.I)
        assert creates == [], f"non-idempotent CREATE statements: {creates}"

    def test_no_destructive_operations(self):
        for keyword in ("DROP ", "TRUNCATE ", "DELETE FROM", "ALTER TABLE", "GRANT ", "INSERT INTO", "UPDATE "):
            assert keyword not in self.sql.upper(), f"forbidden keyword {keyword}"

    def test_tables_and_constraints(self):
        assert "adaptive.situation_log" in self.sql
        assert "adaptive.param_overrides" in self.sql
        assert "'baseline', 'retrieved', 'learned'" in self.sql
        assert "'passed', 'unavailable'" in self.sql
        assert "'GREEN', 'AMBER', 'RED', 'BLACK'" in self.sql
        assert "'Z+', 'Z', 'Y', 'X'" in self.sql
        assert "severity_score BETWEEN 0 AND 21" in self.sql


# ===========================================================================
# 15. Persistence (parameter binding)
# ===========================================================================


class RecordingPool:
    """Fake asyncpg pool yielding a shared RecordingConn."""

    def __init__(self):
        self.conn = RecordingConn()
        self.acquisitions = 0

    def acquire(self):
        self.acquisitions += 1
        return _Ctx(self.conn)


class _Ctx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *args):
        return None


class RecordingConn:
    def __init__(self):
        self.calls = []

    async def execute(self, query, *args):
        self.calls.append((query, args))


class TestPersistence:
    async def test_situation_insert_binds_all_values(self):
        from traffic_oracle.adaptive.persistence import AsyncpgPersister

        pool = RecordingPool()
        conn = pool.conn
        persister = AsyncpgPersister(pool)
        await persister.log_situation(
            {
                "movement_id": "m1",
                "vvip_class": "Z+",
                "situation_code": "Z+:HEAVY:RAIN:LOW",
                "situation_class": "AMBER",
                "severity_score": 7,
                "inputs": {"vvip_class": "Z+"},
                "observed_class": "AMBER",
                "transitioned": False,
            }
        )
        assert persister.written_situations == 1
        query, args = conn.calls[0]
        assert query.startswith("INSERT INTO adaptive.situation_log")
        assert "protection_class" in query  # 007: generalized tier column
        assert "$1" in query and "$10" not in query  # 9 bound params + NOW()
        assert args == ("m1", "Z+", "Z+:HEAVY:RAIN:LOW", "AMBER", 7, '{"vvip_class": "Z+"}', "AMBER", False, "Z+")
        # protection_class mirrors the tier code (the generalized name)
        assert args[-1] == args[1]
        # no value was interpolated into the SQL text itself
        for value in ("m1", "Z+:HEAVY:RAIN:LOW"):
            assert value not in query

    async def test_params_insert_binds_all_values(self):
        from traffic_oracle.adaptive.persistence import AsyncpgPersister

        pool = RecordingPool()
        conn = pool.conn
        persister = AsyncpgPersister(pool)
        landed = await persister.log_params(
            {
                "movement_id": "m1",
                "vvip_class": "Z",
                "situation_class": "GREEN",
                "params": {"advance_closure_sec": 120},
                "source": "baseline",
                "shadow_eval": "unavailable",
                "evidence": {"floor_applied": False},
            }
        )
        assert landed is True  # the service advances its signature only then
        assert persister.written_params == 1
        query, args = conn.calls[0]
        assert query.startswith("INSERT INTO adaptive.param_overrides")
        assert args[0] == "m1" and args[4] == "baseline"
        assert json.loads(args[3]) == {"advance_closure_sec": 120}

    async def test_pool_errors_swallowed(self):
        from traffic_oracle.adaptive.persistence import AsyncpgPersister

        class BoomPool:
            def acquire(self):
                raise RuntimeError("db down")

        persister = AsyncpgPersister(BoomPool())
        await persister.log_situation(
            {
                "movement_id": "m1", "vvip_class": "Z", "situation_code": "x",
                "situation_class": "GREEN", "severity_score": 0, "inputs": {},
                "observed_class": "GREEN", "transitioned": False,
            }
        )
        assert persister.written_situations == 0  # survived
        landed = await persister.log_params(
            {
                "movement_id": "m1", "vvip_class": "Z", "situation_class": "GREEN",
                "params": {}, "source": "baseline", "shadow_eval": "unavailable",
                "evidence": {},
            }
        )
        assert landed is False  # reported, so the service re-attempts it
        assert persister.written_params == 0

    async def test_none_pool_is_noop(self):
        from traffic_oracle.adaptive.persistence import AsyncpgPersister

        persister = AsyncpgPersister(None)
        await persister.log_situation({"movement_id": "m"})
        # disabled persistence is not a failure: never re-attempted
        assert await persister.log_params({"movement_id": "m"}) is None
        assert persister.written_situations == 0 and persister.written_params == 0


# ===========================================================================
# 16. Valkey mirror
# ===========================================================================


class FakeValkeyClient:
    def __init__(self):
        self.store = {}
        self.ttls = {}

    async def set(self, key, value, ex=None):
        self.store[key] = value
        self.ttls[key] = ex


class TestValkeyMirror:
    async def test_keys_and_ttl(self):
        from traffic_oracle.adaptive.valkey_mirror import (
            SITUATION_CHANGED_KEY,
            AdaptiveValkeySink,
            params_key,
        )

        client = FakeValkeyClient()
        sink = AdaptiveValkeySink(client)
        await sink.publish_params("m1", '{"advance_closure_sec": 120}')
        assert params_key("m1") == "adaptive:params:m1"
        assert client.store["adaptive:params:m1"] == '{"advance_closure_sec": 120}'
        assert client.ttls["adaptive:params:m1"] == 30
        await sink.publish_situation_change("m1", "GREEN", "RED", "{}")
        assert client.ttls[SITUATION_CHANGED_KEY] == 60
        assert client.store[SITUATION_CHANGED_KEY] == "{}"

    async def test_logging_sink_never_raises(self):
        from traffic_oracle.adaptive.valkey_mirror import LoggingValkeySink

        class BrokenClient:
            async def set(self, *a, **k):
                raise ConnectionError("valkey down")

        sink = LoggingValkeySink(BrokenClient())
        await sink.publish_params("m1", "{}")  # must not raise
        await sink.publish_situation_change("m1", "A", "B", "{}")
        assert sink.dropped_writes == 2


# ===========================================================================
# 17. Engine loop tick (fake pool + fake valkey)
# ===========================================================================

EVENT_A = "11111111-1111-4111-8111-111111111111"
EVENT_B = "22222222-2222-4222-8222-222222222222"
MOVEMENT_M = "33333333-3333-4333-8333-333333333333"


class LoopPool:
    """Fake asyncpg pool for the engine loop: canned rows per SQL family."""

    def __init__(
        self,
        events=None,
        spines=None,
        movements=None,
        routes=None,
        lengths=None,
    ):
        self.events = list(events or [])
        self.spines = list(spines or [])
        self.movements = list(movements or [])
        self.routes = list(routes or [])
        self.lengths = dict(lengths or {})
        self.acquisitions = 0
        self.queries: list[str] = []

    def acquire(self):
        self.acquisitions += 1
        return _Ctx(_LoopConn(self))


class _LoopConn:
    def __init__(self, pool: LoopPool):
        self.pool = pool

    async def fetch(self, query, *args):
        self.pool.queries.append(query)
        if "FROM events.corridor_events" in query:
            return [dict(r) for r in self.pool.events]
        if "FROM events.event_routes" in query:
            wanted = {str(u) for u in args[0]}
            return [r for r in self.pool.spines if str(r["event_id"]) in wanted]
        if "FROM convoy.movements" in query:
            return [dict(r) for r in self.pool.movements]
        if "FROM convoy.planned_routes" in query:
            wanted = {str(u) for u in args[0]}
            return [r for r in self.pool.routes if str(r["movement_id"]) in wanted]
        if "FROM corridor.road_segments" in query:
            return [
                {"segment_id": k, "length_m": v} for k, v in self.pool.lengths.items()
            ]
        raise AssertionError(f"unexpected query: {query}")


class LoopValkey:
    def __init__(self, docs=None, anomaly="0"):
        self.docs = dict(docs or {})
        self.anomaly = anomaly
        self.mget_calls = 0

    async def get(self, key):
        if key == "adaptive:anomaly:active":
            return self.anomaly
        return self.docs.get(key)

    async def mget(self, keys):
        self.mget_calls += 1
        return [self.docs.get(k) for k in keys]


def event_row(
    event_id=EVENT_A,
    protection_class="none",
    status="active",
    crowd_class="LOW",
    metadata=None,
    planned_start=None,
    movement_id=None,
):
    return {
        "event_id": event_id,
        "protection_class": protection_class,
        "crowd_class": crowd_class,
        "status": status,
        "event_kind": "procession",
        "planned_start": planned_start,
        "movement_id": movement_id,
        "metadata": json.dumps(metadata or {}),
    }


def spine_row(event_id, segment_ids, label="spine"):
    return {"event_id": event_id, "label": label, "segment_ids": list(segment_ids)}


def latest_doc(sid, congestion, age_sec=10.0, quality="synthetic", speed=30.0):
    import time as _time

    return json.dumps(
        {
            "timestamp_ms": int((_time.time() - age_sec) * 1000),
            "lon": 72.58,
            "lat": 23.03,
            "segment_id": sid,
            "speed_kmh": speed,
            "congestion_idx": congestion,
            "source": "government_traffic",
            "data_quality": quality,
            "confidence": 1.0,
        }
    )


def latest_docs(rows: dict[int, str]) -> dict[str, str]:
    return {f"traffic:latest:{sid}": doc for sid, doc in rows.items()}


class RecordingTwinPort:
    """Twin port recording every ShadowContext it was asked to evaluate."""

    def __init__(self, baseline_vh=100.0, candidate_vh=95.0):
        self.contexts: list[ShadowContext] = []
        self.baseline_vh = baseline_vh
        self.candidate_vh = candidate_vh

    def run_pair(self, baseline, candidate, ctx):
        from traffic_oracle.adaptive.twin import TwinPair

        self.contexts.append(ctx)
        return TwinPair(
            TwinRun(self.baseline_vh, True, False),
            TwinRun(self.candidate_vh, True, False),
            None,
        )


class TestEngineLoop:
    async def test_tick_classifies_active_subjects_from_inputs_docs(self):
        import json as _json

        from traffic_oracle.adaptive.engine_loop import (
            SituationEngineLoop,
            inputs_key,
        )

        service, clock, sink, pers = make_service()
        pool = LoopPool(
            movements=[
                {"movement_id": "m1", "vvip_class": "Z", "status": "active",
                 "planned_start": None},
                {"movement_id": "m2", "vvip_class": "Y", "status": "planning",
                 "planned_start": None},
                {"movement_id": "m3", "vvip_class": "BAD", "status": "active",
                 "planned_start": None},  # skipped, logged
            ]
        )
        valkey = LoopValkey(
            docs={
                inputs_key("m1"): _json.dumps(
                    {"predicted_congestion_idx": 0.2, "weather_severity": 0.1}
                ),
                inputs_key("m2"): _json.dumps({"predicted_congestion_idx": 0.95}),
                inputs_key("m3"): _json.dumps({"predicted_congestion_idx": 0.5}),
            }
        )
        loop = SituationEngineLoop(service, pool=pool, valkey_client=valkey)
        await loop._tick()
        assert service.movement_state("m1") is not None
        assert service.movement_state("m2") is not None
        assert service.movement_state("m3") is None  # bad tier skipped
        assert service.movement_state("m1").effective_class is SituationClass.GREEN
        assert service.movement_state("m2").effective_class is SituationClass.AMBER
        assert loop.health.movements_seen == 3
        assert len(pers.situations) == 2
        assert {r["inputs"]["source"] for r in pers.situations} == {"inputs_doc"}
        assert loop.health.subjects["m3"].last_skip_reason == "bad_protection_class"

    async def test_tick_without_inputs_fabricates_nothing(self):
        from traffic_oracle.adaptive.engine_loop import SituationEngineLoop

        service, _, _, pers = make_service()
        pool = LoopPool(
            movements=[{"movement_id": "m1", "vvip_class": "Z", "status": "active",
                        "planned_start": None}]
        )
        loop = SituationEngineLoop(service, pool=pool, valkey_client=LoopValkey())
        await loop._tick()
        assert service.movement_state("m1") is None
        assert pers.situations == []
        health = loop.health.snapshot()["subjects"]["m1"]
        assert health["skipped_no_inputs"] == 1
        assert health["last_skip_reason"] == "no_route_spine"
        assert health["last_inputs_age_sec"] is None

    async def test_live_cache_band_is_length_weighted_and_labelled(self):
        from traffic_oracle.adaptive.engine_loop import SituationEngineLoop

        service, _, _, pers = make_service()
        pool = LoopPool(
            events=[event_row(status="planning")],
            spines=[spine_row(EVENT_A, [1001, 1002])],
            lengths={1001: 1000.0, 1002: 3000.0},
        )
        valkey = LoopValkey(latest_docs({1001: latest_doc(1001, 0.2),
                                         1002: latest_doc(1002, 0.9)}))
        # robust (p90): 0.9 covers >= 90 % of the length -> GRIDLOCK (6) -> AMBER
        loop = SituationEngineLoop(service, pool=pool, valkey_client=valkey,
                                   robust_inputs=True)
        await loop._tick()
        row = pers.situations[-1]
        inputs = row["inputs"]
        assert inputs["source"] == "live_cache"
        assert inputs["congestion_aggregate"] == "length_weighted_p90"
        assert inputs["congestion_mean"] == pytest.approx(0.725)
        assert inputs["congestion_p90"] == pytest.approx(0.9)
        assert inputs["predicted_congestion_idx"] == pytest.approx(0.9)
        assert inputs["coverage"] == 1.0
        assert inputs["segments_used"] == 2 and inputs["segments_total"] == 2
        assert inputs["data_quality"] == "synthetic"
        assert inputs["simulated_feed"] is True
        assert inputs["weather_source"] == "none" and inputs["weather"] == "CLEAR"
        assert inputs["crowd_source"] == "event_row" and inputs["crowd"] == "LOW"
        assert inputs["traffic_regime"] == "GRIDLOCK"
        assert row["situation_class"] == "AMBER"
        assert valkey.mget_calls == 1  # one round trip for the whole spine
        # the band says what it stands on: the simulated feed
        status = service.status()
        assert status["movements"][EVENT_A]["simulated_feed"] is True
        assert status["movements"][EVENT_A]["inputs_data_quality"] == "synthetic"
        assert status["city_band"]["driver"] == EVENT_A
        assert status["city_band"]["simulated_feed"] is True

        # mean (robust off): 0.725 -> CONGESTED (4) -> GREEN
        service2, _, _, pers2 = make_service()
        loop2 = SituationEngineLoop(service2, pool=pool, valkey_client=valkey,
                                    robust_inputs=False)
        await loop2._tick()
        inputs2 = pers2.situations[-1]["inputs"]
        assert inputs2["congestion_aggregate"] == "length_weighted_mean"
        assert inputs2["predicted_congestion_idx"] == pytest.approx(0.725)
        assert pers2.situations[-1]["situation_class"] == "GREEN"

    async def test_no_fresh_usable_observation_skips_never_fabricates(self):
        from traffic_oracle.adaptive.engine_loop import SituationEngineLoop

        service, _, _, pers = make_service()
        pool = LoopPool(
            events=[event_row()],
            spines=[spine_row(EVENT_A, [1001, 1002, 1003])],
            lengths={1001: 1000.0, 1002: 1000.0, 1003: 1000.0},
        )
        valkey = LoopValkey(latest_docs({
            1001: latest_doc(1001, 0.5, age_sec=400.0),           # stale
            1002: latest_doc(1002, 0.5, quality="anomalous"),     # Tier-1 flagged
            1003: latest_doc(1003, 0.5, age_sec=-600.0),          # future-stamped
        }))
        loop = SituationEngineLoop(service, pool=pool, valkey_client=valkey)
        await loop._tick()
        assert service.movement_state(EVENT_A) is None
        assert pers.situations == []
        health = loop.health.subjects[EVENT_A]
        assert health.last_skip_reason == "no_fresh_observations"
        assert health.skipped_no_inputs == 1
        assert loop.health.subjects_skipped == 1

    async def test_anomalous_rows_never_weigh_the_band(self):
        from traffic_oracle.adaptive.engine_loop import SituationEngineLoop

        service, _, _, pers = make_service()
        pool = LoopPool(
            events=[event_row()],
            spines=[spine_row(EVENT_A, [1001, 1002])],
            lengths={1001: 1000.0, 1002: 1000.0},
        )
        valkey = LoopValkey(latest_docs({
            1001: latest_doc(1001, 0.1, quality="real"),
            1002: latest_doc(1002, 1.0, quality="anomalous"),
        }))
        loop = SituationEngineLoop(service, pool=pool, valkey_client=valkey)
        await loop._tick()
        inputs = pers.situations[-1]["inputs"]
        assert inputs["predicted_congestion_idx"] == pytest.approx(0.1)
        assert inputs["anomalous_excluded"] == 1
        assert inputs["coverage"] == pytest.approx(0.5)
        assert inputs["data_quality"] == "real" and inputs["simulated_feed"] is False

    async def test_inputs_doc_wins_and_is_labelled_emulator(self):
        from traffic_oracle.adaptive.engine_loop import (
            SituationEngineLoop,
            emulator_source_key,
            inputs_key,
        )

        service, _, _, pers = make_service()
        pool = LoopPool(
            events=[event_row()],
            spines=[spine_row(EVENT_A, [1001])],
            lengths={1001: 1000.0},
        )
        docs = latest_docs({1001: latest_doc(1001, 0.1)})
        docs[inputs_key(EVENT_A)] = json.dumps({"predicted_congestion_idx": 0.7})
        docs[emulator_source_key(EVENT_A)] = json.dumps(
            {"run_id": 42, "use_case_id": "rath_yatra_ahmedabad", "replay": False}
        )
        loop = SituationEngineLoop(service, pool=pool, valkey_client=LoopValkey(docs))
        await loop._tick()
        inputs = pers.situations[-1]["inputs"]
        assert inputs["source"] == "emulator"
        assert inputs["emulator_run_id"] == 42
        assert inputs["simulated_feed"] is True  # emulated, never field data
        assert inputs["predicted_congestion_idx"] == pytest.approx(0.7)
        assert inputs["traffic_regime"] == "CONGESTED"
        assert service.status()["movements"][EVENT_A]["simulated_feed"] is True

    async def test_primary_spine_preferred_over_alternates(self):
        from traffic_oracle.adaptive.engine_loop import SituationEngineLoop

        service, _, _, pers = make_service()
        pool = LoopPool(
            events=[event_row()],
            spines=[
                spine_row(EVENT_A, [1001], label="primary"),
                spine_row(EVENT_A, [1002], label="alt-1"),
            ],
            lengths={1001: 1000.0, 1002: 1000.0},
        )
        valkey = LoopValkey(latest_docs({1001: latest_doc(1001, 0.1),
                                         1002: latest_doc(1002, 0.95)}))
        loop = SituationEngineLoop(service, pool=pool, valkey_client=valkey)
        await loop._tick()
        inputs = pers.situations[-1]["inputs"]
        assert inputs["spine"] == "primary"
        assert inputs["segments_total"] == 1
        assert inputs["predicted_congestion_idx"] == pytest.approx(0.1)

    async def test_weather_and_crowd_come_from_the_event_row(self):
        from traffic_oracle.adaptive.engine_loop import SituationEngineLoop

        service, _, _, pers = make_service()
        pool = LoopPool(
            events=[event_row(crowd_class="EXTREME", metadata={"weather": "RAIN"})],
            spines=[spine_row(EVENT_A, [1001])],
            lengths={1001: 1000.0},
        )
        valkey = LoopValkey(latest_docs({1001: latest_doc(1001, 0.1)}))
        loop = SituationEngineLoop(service, pool=pool, valkey_client=valkey)
        await loop._tick()
        inputs = pers.situations[-1]["inputs"]
        assert inputs["weather"] == "RAIN"
        assert inputs["weather_source"] == "event_metadata"
        assert inputs["crowd"] == "EXTREME" and inputs["crowd_class"] == "EXTREME"
        assert inputs["event_active"] is True  # status 'active'

    async def test_legacy_movement_uses_its_ranked_planned_route(self):
        from traffic_oracle.adaptive.engine_loop import SituationEngineLoop

        service, _, _, pers = make_service()
        pool = LoopPool(
            movements=[{"movement_id": MOVEMENT_M, "vvip_class": "Z",
                        "status": "approved", "planned_start": None}],
            routes=[
                {"movement_id": MOVEMENT_M, "route_rank": 2, "segment_ids": [1002]},
                {"movement_id": MOVEMENT_M, "route_rank": 1, "segment_ids": [1001]},
            ],
            lengths={1001: 1000.0, 1002: 1000.0},
        )
        valkey = LoopValkey(latest_docs({1001: latest_doc(1001, 0.3),
                                         1002: latest_doc(1002, 0.99)}))
        loop = SituationEngineLoop(service, pool=pool, valkey_client=valkey)
        await loop._tick()
        inputs = pers.situations[-1]["inputs"]
        assert inputs["spine"] == "primary"
        assert inputs["predicted_congestion_idx"] == pytest.approx(0.3)
        assert inputs["crowd_source"] == "none"

    async def test_completed_subject_is_evicted_and_releases_the_city_band(self):
        from traffic_oracle.adaptive.engine_loop import SituationEngineLoop

        service, _, _, _ = make_service()
        pool = LoopPool(
            events=[event_row(EVENT_A, protection_class="Z+"), event_row(EVENT_B)],
            spines=[spine_row(EVENT_A, [1001]), spine_row(EVENT_B, [1002])],
            lengths={1001: 1000.0, 1002: 1000.0},
        )
        valkey = LoopValkey(latest_docs({1001: latest_doc(1001, 0.9),
                                         1002: latest_doc(1002, 0.1)}))
        loop = SituationEngineLoop(service, pool=pool, valkey_client=valkey)
        await loop._tick()
        # Z+ (6) + GRIDLOCK (6) + event (1) = 13 -> RED drives the city band
        assert service.city_band()["band"] == "RED"
        assert set(service.status()["movements"]) == {EVENT_A, EVENT_B}

        pool.events = [event_row(EVENT_B)]  # A completed: gone from the active set
        await loop._tick()
        status = service.status()
        assert set(status["movements"]) == {EVENT_B}
        assert service.city_band()["band"] == "GREEN"
        assert loop.health.evicted_last_tick == [EVENT_A]
        assert EVENT_A not in loop.health.snapshot()["subjects"]

    async def test_subject_with_vanished_inputs_is_evicted_when_stale(self):
        from datetime import UTC, datetime, timedelta

        from traffic_oracle.adaptive.engine_loop import (
            STALE_INPUTS_SEC,
            SituationEngineLoop,
        )

        wall = [datetime(2026, 10, 22, 12, 0, tzinfo=UTC)]
        service = AdaptiveService(clock=FakeClock(), wall_clock=lambda: wall[0])
        pool = LoopPool(
            events=[event_row()],
            spines=[spine_row(EVENT_A, [1001])],
            lengths={1001: 1000.0},
        )
        valkey = LoopValkey(latest_docs({1001: latest_doc(1001, 0.4)}))
        loop = SituationEngineLoop(service, pool=pool, valkey_client=valkey)
        await loop._tick()
        assert service.movement_state(EVENT_A) is not None
        valkey.docs.clear()  # the feed went dark
        wall[0] += timedelta(seconds=STALE_INPUTS_SEC - 10)
        await loop._tick()
        assert service.movement_state(EVENT_A) is not None  # not yet stale
        wall[0] += timedelta(seconds=20)
        await loop._tick()
        assert service.movement_state(EVENT_A) is None
        assert loop.health.evicted_last_tick == [EVENT_A]

    async def test_a_failing_subject_poll_still_retires_stale_bands(self):
        import asyncio as _asyncio
        from datetime import UTC, datetime, timedelta

        from traffic_oracle.adaptive.engine_loop import (
            STALE_INPUTS_SEC,
            SituationEngineLoop,
        )

        class DownPool:
            def acquire(self):
                raise ConnectionError("database unavailable")

        wall = [datetime(2026, 10, 22, 12, 0, tzinfo=UTC)]
        service = AdaptiveService(clock=FakeClock(), wall_clock=lambda: wall[0])
        pool = LoopPool(
            events=[event_row()],
            spines=[spine_row(EVENT_A, [1001])],
            lengths={1001: 1000.0},
        )
        valkey = LoopValkey(latest_docs({1001: latest_doc(1001, 0.95)}))
        loop = SituationEngineLoop(
            service, pool=pool, valkey_client=valkey, poll_interval_sec=0.01
        )
        await loop._tick()
        band = service.city_band()["band"]
        assert band != "GREEN" and service.movement_state(EVENT_A) is not None
        # The subject poll dies (DB outage) and the band's inputs age out:
        # the failing loop must not keep claiming the old band.
        loop._pool = DownPool()
        wall[0] += timedelta(seconds=STALE_INPUTS_SEC + 1)
        loop.start()
        for _ in range(200):
            await _asyncio.sleep(0.01)
            if service.movement_state(EVENT_A) is None:
                break
        await loop.stop()
        assert loop.health.consecutive_errors >= 1
        assert "ConnectionError" in (loop.health.last_error or "")
        assert service.movement_state(EVENT_A) is None
        assert loop.health.evicted_total == 1
        # GREEN with no subjects — but marked stale: unknown, not at rest
        band = service.city_band()
        assert band["band"] == "GREEN" and band["stale"] is True
        assert band["stale_since"] is not None
        # The database comes back: the first good tick clears the marker.
        loop._pool = pool
        loop.start()
        for _ in range(200):
            await _asyncio.sleep(0.01)
            if not service.city_band()["stale"]:
                break
        await loop.stop()
        assert service.city_band()["stale"] is False

    async def test_a_hung_database_is_a_failed_poll_not_a_frozen_loop(self):
        # A database that stops ANSWERING (paused host, silent partition)
        # raises nothing: without a bound the tick awaits forever, the band
        # freezes in place and the city band never reads stale.  Like
        # asyncpg, cancelling the hung query ALSO waits on the server (cancel
        # request, connection reset) — so the bound must abandon the tick,
        # not await its cleanup (asyncio.wait_for would block right here).
        import asyncio as _asyncio
        from datetime import UTC, datetime, timedelta

        from traffic_oracle.adaptive.engine_loop import (
            STALE_INPUTS_SEC,
            SituationEngineLoop,
        )

        database_back = _asyncio.Event()

        class _Hang:
            async def __aenter__(self):
                try:
                    await database_back.wait()
                except _asyncio.CancelledError:
                    await database_back.wait()  # cleanup talks to the server
                    raise
                raise ConnectionError("connection reset")

            async def __aexit__(self, *args):
                return None

        class HungPool:
            def acquire(self):
                return _Hang()

        wall = [datetime(2026, 10, 22, 12, 0, tzinfo=UTC)]
        service = AdaptiveService(clock=FakeClock(), wall_clock=lambda: wall[0])
        pool = LoopPool(
            events=[event_row()],
            spines=[spine_row(EVENT_A, [1001])],
            lengths={1001: 1000.0},
        )
        valkey = LoopValkey(latest_docs({1001: latest_doc(1001, 0.95)}))
        loop = SituationEngineLoop(
            service, pool=pool, valkey_client=valkey,
            poll_interval_sec=0.01, tick_timeout_sec=0.05,
        )
        await loop._tick()
        assert service.movement_state(EVENT_A) is not None
        loop._pool = HungPool()
        wall[0] += timedelta(seconds=STALE_INPUTS_SEC + 1)
        loop.start()
        for _ in range(300):
            await _asyncio.sleep(0.01)
            if loop.health.consecutive_errors >= 2:
                break
        health = loop.health.snapshot()
        band = service.city_band()
        assert health["consecutive_errors"] >= 2  # every interval: a failed poll
        assert loop.health.last_error in (
            "TimeoutError: tick abandoned after 0.05 s",
            "TimeoutError: previous tick still unwinding (database not answering)",
        )
        assert service.movement_state(EVENT_A) is None  # retired, not frozen
        assert band["band"] == "GREEN" and band["stale"] is True
        # The database answers again: the abandoned tick unwinds, the next
        # tick polls the good pool and the band is known again.
        loop._pool = pool
        database_back.set()
        for _ in range(300):
            await _asyncio.sleep(0.01)
            if not service.city_band()["stale"]:
                break
        await loop.stop()
        assert service.city_band()["stale"] is False
        assert loop.health.consecutive_errors == 0

    async def test_shadow_context_built_from_spine_and_planned_start(self):
        from datetime import UTC, datetime, timedelta

        from traffic_oracle.adaptive.engine_loop import SituationEngineLoop

        twin = RecordingTwinPort()
        service, _, _, pers = make_service(twin=twin)
        start = datetime.now(UTC) + timedelta(hours=2)
        pool = LoopPool(
            events=[event_row(protection_class="Z+", status="planning",
                              planned_start=start)],
            spines=[spine_row(EVENT_A, [1024, 1025], label="primary")],
            lengths={1024: 2441.0, 1025: 2440.0},
        )
        valkey = LoopValkey(latest_docs({1024: latest_doc(1024, 0.1),
                                         1025: latest_doc(1025, 0.1)}))
        loop = SituationEngineLoop(service, pool=pool, valkey_client=valkey)
        await loop._tick()
        # Z+ alone is AMBER: a non-baseline candidate -> twin shadow-eval
        assert twin.contexts, "the loop must hand the twin a ShadowContext"
        ctx = twin.contexts[-1]
        assert ctx.route_segment_ids == (1024, 1025)
        assert ctx.departure_time == start.isoformat()
        assert pers.param_rows[-1]["shadow_eval"] == "passed"

    async def test_movement_ref_alias_reaches_the_event_subject(self):
        from traffic_oracle.adaptive.engine_loop import SituationEngineLoop

        service, _, _, _ = make_service()
        pool = LoopPool(
            events=[event_row(metadata={"movement_ref": "mv-zplus-001"})],
            spines=[spine_row(EVENT_A, [1001])],
            lengths={1001: 1000.0},
        )
        valkey = LoopValkey(latest_docs({1001: latest_doc(1001, 0.1)}))
        loop = SituationEngineLoop(service, pool=pool, valkey_client=valkey)
        await loop._tick()
        by_ref = service.current_params("mv-zplus-001", SecurityTier.NONE)
        assert by_ref["evidence"]["subject_tracked"] is True
        assert by_ref["evidence"]["subject_id"] == EVENT_A

    async def test_run_loop_survives_db_failure_and_surfaces_health(self):
        """_tick may raise; the _run loop must catch, record, and continue."""
        from traffic_oracle.adaptive.engine_loop import SituationEngineLoop

        class BoomPool:
            def acquire(self):
                raise RuntimeError("db down")

        service, *_ = make_service()
        loop = SituationEngineLoop(
            service, pool=BoomPool(), valkey_client=LoopValkey({})
        )
        calls: list[int] = []

        async def boom_tick() -> None:
            calls.append(1)
            loop._stop.set()  # exit the loop after this iteration
            raise RuntimeError("db down")

        loop._tick = boom_tick  # type: ignore[method-assign]
        await loop._run()
        assert calls == [1]
        assert loop.health.consecutive_errors == 1
        assert "db down" in (loop.health.last_error or "")
        assert loop.health.last_tick_monotonic is not None
        assert loop.health.ticks == 1

    def test_loop_health_snapshot_shape(self):
        from traffic_oracle.adaptive.engine_loop import LoopHealth

        snap = LoopHealth().snapshot()
        assert {
            "running", "last_tick_monotonic", "last_error",
            "consecutive_errors", "movements_seen",
        } <= set(snap)
        assert {
            "last_tick_age_sec", "last_tick_duration_ms", "poll_interval_sec",
            "ticks", "subjects_active", "subjects_observed", "subjects_skipped",
            "evicted_last_tick", "evicted_total", "robust_inputs", "subjects",
        } <= set(snap)

    async def test_per_subject_inputs_age_and_skip_counters(self):
        from traffic_oracle.adaptive.engine_loop import SituationEngineLoop

        service, *_ = make_service()
        pool = LoopPool(
            events=[event_row(EVENT_A), event_row(EVENT_B)],
            spines=[spine_row(EVENT_A, [1001]), spine_row(EVENT_B, [1002])],
            lengths={1001: 1000.0, 1002: 1000.0},
        )
        valkey = LoopValkey(latest_docs({1001: latest_doc(1001, 0.2)}))
        loop = SituationEngineLoop(service, pool=pool, valkey_client=valkey)
        await loop._tick()
        await loop._tick()
        subjects = loop.health.snapshot()["subjects"]
        assert subjects[EVENT_A]["inputs_source"] == "live_cache"
        assert subjects[EVENT_A]["last_inputs_age_sec"] is not None
        assert subjects[EVENT_A]["skipped_no_inputs"] == 0
        assert subjects[EVENT_B]["skipped_no_inputs"] == 2
        assert subjects[EVENT_B]["consecutive_skips"] == 2
        assert subjects[EVENT_B]["last_skip_reason"] == "no_fresh_observations"


class TestLiveAggregation:
    def test_weighted_quantile_is_length_weighted(self):
        from traffic_oracle.adaptive.engine_loop import weighted_quantile

        # 10 % of the length at 0.9: the p90 is the 0.1 bulk only if it
        # covers >= 90 % of the length.
        assert weighted_quantile([(0.1, 900.0), (0.9, 100.0)], 0.9) == 0.1
        assert weighted_quantile([(0.1, 899.0), (0.9, 101.0)], 0.9) == 0.9
        assert weighted_quantile([(0.4, 1.0)], 0.9) == 0.4

    def test_aggregate_counts_exclusions_and_coverage(self):
        import time as _time

        from traffic_oracle.adaptive.engine_loop import aggregate_live_congestion

        now_ms = int(_time.time() * 1000)
        live = aggregate_live_congestion(
            [1, 2, 3, 4],
            [
                latest_doc(1, 0.2, age_sec=5),
                latest_doc(2, 0.6, age_sec=5, quality="real"),
                latest_doc(3, 0.9, age_sec=900),       # stale
                None,                                  # no cache key
            ],
            {1: 1000.0, 2: 1000.0, 3: 1000.0, 4: 1000.0},
            now_ms,
        )
        assert live is not None
        assert live.mean == pytest.approx(0.4)
        assert live.segments_used == 2 and live.segments_total == 4
        assert live.coverage == pytest.approx(0.5)
        assert live.stale_excluded == 1
        assert live.quality == {"synthetic": 1, "real": 1}

    def test_aggregate_returns_none_without_usable_rows(self):
        import time as _time

        from traffic_oracle.adaptive.engine_loop import aggregate_live_congestion

        now_ms = int(_time.time() * 1000)
        assert aggregate_live_congestion([1], [None], {1: 10.0}, now_ms) is None
        assert aggregate_live_congestion(
            [1], ["{not json"], {1: 10.0}, now_ms
        ) is None

    def test_robust_flag_env_override(self):
        from traffic_oracle.adaptive.engine_loop import robust_inputs_enabled

        assert robust_inputs_enabled({"VVIP_ADAPTIVE_ROBUST_INPUTS": "0"}) is False
        assert robust_inputs_enabled({"VVIP_ADAPTIVE_ROBUST_INPUTS": "true"}) is True
        # unset: the canonical robust knob (protocol params) decides
        from traffic_oracle.adaptive.protocol_params_gen import BASELINE_ROBUST_ROUTING

        assert robust_inputs_enabled({}) is bool(BASELINE_ROBUST_ROUTING["enabled"])


# ===========================================================================
# 18. Hysteresis streaks — the RED latch fix
# ===========================================================================


class TestHysteresisStreaks:
    def test_alternating_recovery_de_escalates_to_highest_of_the_streak(self):
        """Audit probe: after RED, 60 observations alternating AMBER/GREEN
        over 300 s used to leave the band latched at RED."""
        c = HysteresisController()
        c.evaluate(SituationClass.RED, 0.0)
        transitions = []
        for i in range(60):
            obs = SituationClass.AMBER if i % 2 == 0 else SituationClass.GREEN
            r = c.evaluate(obs, 5.0 * (i + 1))
            if r.transitioned:
                transitions.append((5.0 * (i + 1), r.effective))
        assert c.state.current is SituationClass.AMBER
        # exactly one step down — to the highest band seen in the streak,
        # once down_confirm (4) and the 120 s dwell were both met
        assert transitions == [(120.0, SituationClass.AMBER)]

    def test_steady_recovery_continues_to_green(self):
        c = HysteresisController()
        c.evaluate(SituationClass.RED, 0.0)
        feed(c, [SituationClass.AMBER] * 30, t0=5.0, dt=5.0)
        assert c.state.current is SituationClass.AMBER
        rs = feed(c, [SituationClass.GREEN] * 30, t0=400.0, dt=5.0)
        assert c.state.current is SituationClass.GREEN
        assert sum(r.transitioned for r in rs) == 1

    def test_down_streak_pending_reports_target_and_length(self):
        c = HysteresisController()
        c.evaluate(SituationClass.RED, 0.0)
        r1 = c.evaluate(SituationClass.GREEN, 1.0)
        r2 = c.evaluate(SituationClass.AMBER, 2.0)
        assert (r1.pending, r1.pending_count) == (SituationClass.GREEN, 1)
        assert (r2.pending, r2.pending_count) == (SituationClass.AMBER, 2)

    def test_up_streak_escalates_to_the_lowest_band_seen(self):
        """GREEN with readings flickering RED/AMBER is at least AMBER."""
        c = HysteresisController()
        c.evaluate(SituationClass.GREEN, 0.0)
        assert not c.evaluate(SituationClass.RED, 1.0).transitioned
        r = c.evaluate(SituationClass.AMBER, 2.0)
        assert r.transitioned and r.effective is SituationClass.AMBER

    def test_black_inside_an_up_streak_is_immediate(self):
        c = HysteresisController()
        c.evaluate(SituationClass.GREEN, 0.0)
        c.evaluate(SituationClass.AMBER, 1.0)
        r = c.evaluate(SituationClass.BLACK, 2.0)
        assert r.transitioned and r.effective is SituationClass.BLACK

    def test_observation_at_the_band_ends_the_streak(self):
        c = HysteresisController()
        c.evaluate(SituationClass.RED, 0.0)
        for t in (200.0, 201.0, 202.0):
            c.evaluate(SituationClass.GREEN, t)
        r = c.evaluate(SituationClass.RED, 203.0)  # agreement resets
        assert r.pending is None and r.pending_count == 0
        r = c.evaluate(SituationClass.GREEN, 204.0)
        assert r.pending_count == 1 and not r.transitioned

    async def test_recovered_movement_never_triggers_red_sustained_takeover(self):
        service, clock, *_ = make_service()
        red = inputs_for(vvip="Z", congestion=0.9, weather=0.3, crowd=0.6)
        await service.observe("m1", red)
        await service.observe("m1", red)
        assert service.movement_state("m1").effective_class is SituationClass.RED
        # noisy recovery: AMBER/GREEN readings alternate for 400 s
        amber = inputs_for(vvip="Z", weather=0.3, event=True)        # 6 -> AMBER
        green = inputs_for(vvip="Z")                                  # 4 -> GREEN
        for i in range(80):
            clock.advance(5)
            await service.observe("m1", amber if i % 2 == 0 else green)
        response = await service.params_for("m1", SecurityTier.Z)
        assert response["situation_class"] == "AMBER"
        assert "RED_SUSTAINED_HIGH_TIER" not in response["evidence"]["escalation"]["reasons"]


# ===========================================================================
# 19. Read-only GET, subject hygiene, city band
# ===========================================================================


class TestReadOnlyParams:
    def _app(self, service):
        from fastapi import FastAPI

        from traffic_oracle.adaptive.router import router as adaptive_router

        app = FastAPI()
        app.include_router(adaptive_router, prefix="/api/v1")
        app.state.adaptive = service
        return app

    async def test_twenty_gets_write_nothing_and_create_no_state(self):
        from fastapi.testclient import TestClient

        service, _, sink, pers = make_service()
        await service.observe("m1", inputs_for(vvip="Z", weather=0.3, event=True))
        await service.params_for("m1", SecurityTier.Z)  # the loop's publish
        rows_before = len(pers.param_rows)
        mirror_before = len(sink.params)
        client = TestClient(self._app(service))
        urls = [
            "/api/v1/adaptive/params",
            "/api/v1/adaptive/params?vvip_class=Z%2B",
            "/api/v1/adaptive/params?movement_id=m1&vvip_class=Z",
            "/api/v1/adaptive/params?movement_id=default&vvip_class=Z",
            "/api/v1/adaptive/params?movement_id=unknown-xyz&vvip_class=Y",
        ]
        for i in range(20):
            resp = client.get(urls[i % len(urls)])
            assert resp.status_code == 200
        assert len(pers.param_rows) == rows_before
        assert len(sink.params) == mirror_before
        assert service.tracked_ids() == ["m1"]  # no 'default', no 'unknown-xyz'
        assert "default" not in service.status()["movements"]

    async def test_get_returns_the_last_published_decision(self):
        from fastapi.testclient import TestClient

        service, *_ = make_service()
        await service.observe("m1", inputs_for(vvip="Z", weather=0.3, event=True))
        published = await service.params_for("m1", SecurityTier.Z)
        body = TestClient(self._app(service)).get(
            "/api/v1/adaptive/params?movement_id=m1&vvip_class=Z"
        ).json()
        for key in ("situation_class", "params", "source", "shadow_eval"):
            assert body[key] == published[key]
        assert body["evidence"]["published"] is True
        assert body["evidence"]["read_only"] is True
        assert body["evidence"]["published_at"]

    def test_missing_movement_id_answers_tier_baseline(self):
        from fastapi.testclient import TestClient

        service, *_ = make_service()
        body = TestClient(self._app(service)).get(
            "/api/v1/adaptive/params?vvip_class=Z%2B"
        ).json()
        assert body["source"] == "baseline"
        assert body["params"]["advance_closure_sec"] == 180  # Z+ floor
        assert body["evidence"]["subject_tracked"] is False
        assert service.tracked_ids() == []


class TestSubjectHygieneAndCityBand:
    async def test_retain_only_evicts_and_releases_the_band(self):
        service, *_ = make_service()
        await service.observe("hot", inputs_for(vvip="Z+", congestion=0.95, weather=0.9))
        await service.observe("calm", inputs_for(vvip="X"))
        assert service.city_band()["band"] in ("RED", "BLACK")
        evicted = service.retain_only(["calm"])
        assert evicted == ["hot"]
        assert list(service.status()["movements"]) == ["calm"]
        assert service.city_band()["band"] == "GREEN"
        assert service.status()["evicted_total"] == 1

    async def test_city_band_since_is_the_last_transition_time(self):
        from datetime import UTC, datetime, timedelta

        wall = [datetime(2026, 10, 22, 6, 0, tzinfo=UTC)]
        clock = FakeClock()
        service = AdaptiveService(clock=clock, wall_clock=lambda: wall[0])
        boot = service.city_band()["since"]
        wall[0] += timedelta(minutes=3)
        red = inputs_for(vvip="Z", congestion=0.9, weather=0.3, crowd=0.6)
        await service.observe("m1", red)  # first observation sets the band
        band = service.city_band()
        assert band["band"] == "RED"
        assert band["since"] == "2026-10-22T06:03:00.000Z"
        assert band["since"] != boot
        wall[0] += timedelta(minutes=2)
        await service.observe("m1", red)  # unchanged band: since unchanged
        assert service.city_band()["since"] == "2026-10-22T06:03:00.000Z"
        assert service.status()["city_band_since"] == "2026-10-22T06:03:00.000Z"
        wall[0] += timedelta(minutes=1)
        service.retain_only([])
        assert service.city_band() == {
            "band": "GREEN", "since": "2026-10-22T06:06:00.000Z",
            "subjects": 0, "driver": None, "simulated_feed": None,
            "stale": False, "stale_reason": None, "stale_since": None,
        }

    async def test_failing_poll_marks_the_city_band_stale_not_at_rest(self):
        from datetime import UTC, datetime, timedelta

        wall = [datetime(2026, 10, 22, 12, 0, tzinfo=UTC)]
        service = AdaptiveService(clock=FakeClock(), wall_clock=lambda: wall[0])
        service.note_poll(ok=True)
        assert service.city_band()["stale"] is False
        service.note_poll(ok=False)  # first failed poll: stale since now
        wall[0] += timedelta(seconds=5)
        service.note_poll(ok=False)
        band = service.city_band()
        assert band["band"] == "GREEN" and band["subjects"] == 0
        assert band["stale"] is True
        assert band["stale_since"] == "2026-10-22T12:00:00.000Z"
        assert band["stale_reason"] == "situation engine poll failing (2 in a row)"
        assert service.status()["city_band"]["stale"] is True
        service.note_poll(ok=True)  # one good poll: the band is known again
        assert service.city_band()["stale"] is False
        assert service.city_band()["stale_since"] is None

    async def test_signals_plans_band_subjects_count_only_tracked_subjects(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from traffic_oracle.signals.router import router as signals_router

        service, *_ = make_service()
        await service.observe("a", inputs_for(vvip="Z"))
        await service.observe("b", inputs_for(vvip="X"))
        service.current_params("phantom", SecurityTier.Z)  # a reader: no state
        service.retain_only(["a", "b"])
        app = FastAPI()
        app.include_router(signals_router)
        app.state.adaptive = service
        body = TestClient(app).get("/api/v1/signals/plans").json()
        assert body["band_subjects"] == 2 == len(service.status()["movements"])


class TestSameProvenanceInputs:
    """WP-19: the event-emulator's observations live in their own key space
    (traffic:emulated:latest:{sid}).  A LIVE event's band reads the live
    plane only; an EMULATED event prefers its scenario's rows per segment
    and falls back to the live one — and the audit row says which."""

    @staticmethod
    def _emulated(rows: dict[int, str]) -> dict[str, str]:
        return {f"traffic:emulated:latest:{sid}": doc for sid, doc in rows.items()}

    async def test_a_live_event_never_reads_the_emulated_plane(self):
        from traffic_oracle.adaptive.engine_loop import SituationEngineLoop

        service, _, _, pers = make_service()
        pool = LoopPool(
            events=[event_row(status="active")],
            spines=[spine_row(EVENT_A, [1001, 1002])],
            lengths={1001: 1000.0, 1002: 1000.0},
        )
        docs = latest_docs({1001: latest_doc(1001, 0.1), 1002: latest_doc(1002, 0.1)})
        docs.update(self._emulated({1001: latest_doc(1001, 0.99), 1002: latest_doc(1002, 0.99)}))
        loop = SituationEngineLoop(service, pool=pool, valkey_client=LoopValkey(docs),
                                   robust_inputs=True)
        await loop._tick()
        inputs = pers.situations[-1]["inputs"]
        assert inputs["predicted_congestion_idx"] == pytest.approx(0.1)  # never 0.99
        assert inputs["inputs_origin"] == "live" and inputs["emulated_segments"] == 0

    async def test_a_live_event_with_only_emulated_rows_is_skipped_not_driven(self):
        from traffic_oracle.adaptive.engine_loop import SituationEngineLoop

        service, _, _, pers = make_service()
        pool = LoopPool(events=[event_row(status="active")],
                        spines=[spine_row(EVENT_A, [1001])])
        valkey = LoopValkey(self._emulated({1001: latest_doc(1001, 0.99)}))
        loop = SituationEngineLoop(service, pool=pool, valkey_client=valkey)
        await loop._tick()
        assert pers.situations == []
        assert loop.health.subjects[EVENT_A].last_skip_reason == "no_fresh_observations"

    async def test_an_emulated_event_prefers_its_scenario_rows_and_says_so(self):
        from traffic_oracle.adaptive.engine_loop import SituationEngineLoop

        service, _, _, pers = make_service()
        pool = LoopPool(
            events=[event_row(status="active", metadata={"source": "event-emulator",
                                                         "mode": "use_case"})],
            spines=[spine_row(EVENT_A, [1001, 1002])],
            lengths={1001: 1000.0, 1002: 1000.0},
        )
        docs = latest_docs({1001: latest_doc(1001, 0.1, quality="real"),
                            1002: latest_doc(1002, 0.1, quality="real")})
        docs.update(self._emulated({1001: latest_doc(1001, 0.9)}))
        loop = SituationEngineLoop(service, pool=pool, valkey_client=LoopValkey(docs),
                                   robust_inputs=True)
        await loop._tick()
        inputs = pers.situations[-1]["inputs"]
        assert inputs["inputs_origin"] == "mixed" and inputs["emulated_segments"] == 1
        assert inputs["congestion_p90"] == pytest.approx(0.9)
        # one emulated row behind the band = a simulated feed, even beside
        # a real one, and the quality mix says so
        assert inputs["simulated_feed"] is True and inputs["data_quality"] == "mixed"

    async def test_an_emulated_event_falls_back_to_the_live_plane(self):
        from traffic_oracle.adaptive.engine_loop import SituationEngineLoop

        service, _, _, pers = make_service()
        pool = LoopPool(
            events=[event_row(status="active", metadata={"mode": "living_city"})],
            spines=[spine_row(EVENT_A, [1001])],
        )
        valkey = LoopValkey(latest_docs({1001: latest_doc(1001, 0.2, quality="real")}))
        loop = SituationEngineLoop(service, pool=pool, valkey_client=valkey)
        await loop._tick()
        inputs = pers.situations[-1]["inputs"]
        assert inputs["inputs_origin"] == "live" and inputs["simulated_feed"] is False

    async def test_a_stale_emulated_row_falls_back_to_the_fresh_live_one(self):
        from traffic_oracle.adaptive.engine_loop import SituationEngineLoop

        service, _, _, pers = make_service()
        pool = LoopPool(
            events=[event_row(status="active", metadata={"source": "event-emulator"})],
            spines=[spine_row(EVENT_A, [1001])],
        )
        docs = latest_docs({1001: latest_doc(1001, 0.2, quality="real")})
        docs.update(self._emulated({1001: latest_doc(1001, 0.9, age_sec=400.0)}))
        loop = SituationEngineLoop(service, pool=pool, valkey_client=LoopValkey(docs))
        await loop._tick()
        inputs = pers.situations[-1]["inputs"]
        assert inputs["inputs_origin"] == "live" and inputs["emulated_segments"] == 0
        assert inputs["predicted_congestion_idx"] == pytest.approx(0.2)

    def test_subject_provenance(self):
        from traffic_oracle.adaptive.engine_loop import Subject

        assert Subject("e", "Z", metadata={"source": "event-emulator"}).emulated
        assert Subject("e", "Z", metadata={"mode": "living_city"}).emulated
        assert not Subject("e", "Z", metadata={"source": "convoy-brain"}).emulated
        assert not Subject("m", "Z", kind="movement").emulated

    async def test_the_emulator_doc_path_is_labelled_synthetic(self):
        import json as _json

        from traffic_oracle.adaptive.engine_loop import (
            SituationEngineLoop,
            emulator_source_key,
            inputs_key,
        )

        service, _, _, pers = make_service()
        pool = LoopPool(events=[event_row(status="active")],
                        spines=[spine_row(EVENT_A, [1001])])
        valkey = LoopValkey({
            inputs_key(EVENT_A): _json.dumps({"predicted_congestion_idx": 0.4}),
            emulator_source_key(EVENT_A): _json.dumps({"run_id": 22, "use_case_id": "rath"}),
        })
        loop = SituationEngineLoop(service, pool=pool, valkey_client=valkey)
        await loop._tick()
        inputs = pers.situations[-1]["inputs"]
        assert inputs["source"] == "emulator" and inputs["simulated_feed"] is True
        assert inputs["data_quality"] == "synthetic" and inputs["inputs_origin"] == "emulated"


class TestPerSubjectShadow:
    async def test_shadow_failure_stays_with_its_own_subject(self):
        from traffic_oracle.adaptive.twin import TwinPair

        class RouteTwin:
            def run_pair(self, baseline, candidate, ctx):
                worse = 140.0 if ctx.route_segment_ids == (1, 2, 3) else 95.0
                return TwinPair(
                    TwinRun(100.0, True, False), TwinRun(worse, True, False), None
                )

        service, *_ = make_service(twin=RouteTwin())
        amber = inputs_for(vvip="Z", weather=0.3, event=True)
        await service.observe("failing", amber)
        await service.observe("passing", amber)
        r_fail = await service.params_for("failing", SecurityTier.Z, shadow_context=ctx())
        r_pass = await service.params_for(
            "passing", SecurityTier.Z, shadow_context=ctx((4, 5, 6))
        )
        assert "SHADOW_EVAL_FAILED" in r_fail["evidence"]["escalation"]["reasons"]
        assert "SHADOW_EVAL_FAILED" not in r_pass["evidence"]["escalation"]["reasons"]
        assert r_pass["shadow_eval"] == "passed"
        movements = service.status()["movements"]
        assert movements["failing"]["shadow_eval"]["status"] == "failed"
        assert movements["passing"]["shadow_eval"]["status"] == "passed"
        # re-deciding the failing subject after the pass keeps ITS verdict
        again = await service.params_for("failing", SecurityTier.Z, shadow_context=ctx())
        assert "SHADOW_EVAL_FAILED" in again["evidence"]["escalation"]["reasons"]

    async def test_uncalibrated_twin_short_circuits_with_its_reason(self):
        twin = RecordingTwinPort()
        service = AdaptiveService(
            clock=FakeClock(),
            twin_port=twin,
            twin_readiness=lambda: (False, "no field observations — simulated feed only"),
        )
        await service.observe("m1", inputs_for(vvip="Z", weather=0.3, event=True))
        response = await service.params_for("m1", SecurityTier.Z, shadow_context=ctx())
        assert twin.contexts == []  # a doomed pair is never run
        assert response["source"] == "baseline"
        assert response["evidence"]["shadow_eval_detail"] == (
            "twin_not_calibrated: no field observations — simulated feed only"
        )
        assert response["evidence"]["fail_closed_reason"] == (
            "shadow_eval_unavailable_fail_closed"
        )

    async def test_shadow_verdict_cached_then_reevaluated(self):
        from traffic_oracle.adaptive.service import SHADOW_REEVAL_SEC

        twin = RecordingTwinPort()
        service, clock, *_ = make_service(twin=twin)
        await service.observe("m1", inputs_for(vvip="Z", weather=0.3, event=True))
        for _ in range(3):
            await service.params_for("m1", SecurityTier.Z, shadow_context=ctx())
        assert len(twin.contexts) == 1
        clock.advance(SHADOW_REEVAL_SEC)
        await service.params_for("m1", SecurityTier.Z, shadow_context=ctx())
        assert len(twin.contexts) == 2

    def test_twin_route_not_modelled_is_never_a_pass(self):
        evaluator = ShadowEvaluator()
        empty = TwinRun(0.0, True, False, route_segments_modelled=0)
        outcome = evaluator.evaluate(AdaptiveParams.baseline(), empty, empty)
        assert outcome.status is ShadowEvalStatus.UNAVAILABLE
        assert outcome.detail == "twin_route_not_modelled"

    def test_payload_calibration_reason_reaches_the_refusal(self):
        payload = {
            "runs": [{"total_disruption_vehicle_hours": 10.0}],
            "calibrated": False,
            "calibration": {"reason": "no field observations — simulated feed only"},
            "route_coverage": {"modelled": 3, "requested": 3},
        }
        run = TwinRun.from_payload(payload)
        assert run.route_segments_modelled == 3
        outcome = ShadowEvaluator().evaluate(AdaptiveParams.baseline(), run, run)
        assert outcome.detail == (
            "twin_not_calibrated: no field observations — simulated feed only"
        )


class TestAuditTrailTruth:
    """param_overrides is the AMA audit trail (``/adaptive/overrides``, the
    ``/city/feed`` AMA items): every decision change lands exactly once,
    and a calibration refusal always carries the CURRENT reason."""

    async def test_calibration_reason_change_writes_exactly_one_verdict_row(self):
        # The restart race: the loop's first publish quoted the pre-check
        # reason; the scheduler's check then set the explicit one.  The
        # corrected reason must reach the audit trail once — not wait for
        # the next band change.
        reason = ["not yet checked for field observations"]
        twin = RecordingTwinPort()
        clock, pers = FakeClock(), FakePersister()
        service = AdaptiveService(
            clock=clock,
            persister=pers,
            twin_port=twin,
            twin_readiness=lambda: (False, reason[0]),
        )
        await service.observe("m1", inputs_for(vvip="Z", weather=0.3, event=True))
        for _ in range(3):
            await service.params_for("m1", SecurityTier.Z, shadow_context=ctx())
            clock.advance(5)
        assert len(pers.param_rows) == 1
        first = pers.param_rows[0]
        assert first["evidence"]["publish_reason"] == "first_publish"
        assert first["evidence"]["shadow_eval_detail"].endswith(
            "not yet checked for field observations"
        )
        reason[0] = "no field observations — simulated feed only"
        for _ in range(3):
            await service.params_for("m1", SecurityTier.Z, shadow_context=ctx())
            clock.advance(5)
        assert len(pers.param_rows) == 2  # exactly one new row
        row = pers.param_rows[1]
        assert row["evidence"]["publish_reason"] == "verdict_change"
        assert row["evidence"]["shadow_eval_detail"] == (
            "twin_not_calibrated: no field observations — simulated feed only"
        )
        assert row["source"] == "baseline" and row["shadow_eval"] == "unavailable"
        assert twin.contexts == []  # an uncalibrated twin is never even run
        # what GET serves is the corrected decision too
        body = service.current_params("m1", SecurityTier.Z)
        assert body["evidence"]["shadow_eval_detail"].endswith("simulated feed only")

    async def test_per_run_numbers_in_a_shadow_detail_never_mint_rows(self):
        # Only the calibration refusal counts by full text; a re-evaluated
        # pass with different disruption numbers is the same decision.
        from traffic_oracle.adaptive.service import SHADOW_REEVAL_SEC

        twin = passing_twin(baseline_vh=100.0, candidate_vh=95.0)
        service, clock, _, pers = make_service(twin=twin)
        await service.observe("m1", inputs_for(vvip="Z", weather=0.3, event=True))
        await service.params_for("m1", SecurityTier.Z, shadow_context=ctx())
        detail_before = service.movement_state("m1").shadow_detail
        twin.candidate_run = TwinRun(97.0, True, False)
        clock.advance(SHADOW_REEVAL_SEC)
        await service.params_for("m1", SecurityTier.Z, shadow_context=ctx())
        assert twin.calls == 2  # re-evaluated ...
        assert service.movement_state("m1").shadow_detail != detail_before
        assert len(pers.param_rows) == 1  # ... but no new audit row

    async def test_publish_reason_names_escalation_and_degradation_changes(self):
        service, clock, _, pers = make_service()
        amber = inputs_for(vvip="Z", weather=0.3, event=True)
        await service.observe("m1", amber)
        await service.params_for("m1", SecurityTier.Z)
        # same band and params; the convoy GPS goes stale -> takeover
        await service.observe("m1", amber, last_gps_age_sec=120.0)
        await service.params_for("m1", SecurityTier.Z)
        assert [r["evidence"]["publish_reason"] for r in pers.param_rows] == [
            "first_publish", "escalation_change",
        ]
        assert "GPS_STALE" in pers.param_rows[1]["evidence"]["escalation"]["reasons"]
        # a GREEN subject: the decider degrades, nothing else changes
        await service.observe("m2", inputs_for(vvip="X"))
        await service.params_for("m2", SecurityTier.X)
        service.feed_health(snap("decider", ok=False))
        service.feed_health(snap("decider", ok=False))
        assert service.status()["degradation"]["degradation_tier"] == "TACTICAL_FALLBACK"
        await service.params_for("m2", SecurityTier.X)
        m2_rows = [r for r in pers.param_rows if r["movement_id"] == "m2"]
        assert [r["evidence"]["publish_reason"] for r in m2_rows] == [
            "first_publish", "degradation_change",
        ]

    async def test_a_failed_audit_row_is_reattempted_not_lost(self):
        @dataclass
        class FlakyPersister(FakePersister):
            fail_next: int = 1
            attempts: int = 0

            async def log_params(self, record: dict) -> bool:
                self.attempts += 1
                if self.fail_next > 0:
                    self.fail_next -= 1
                    return False  # the row did not land (database blip)
                self.param_rows.append(record)
                return True

        pers = FlakyPersister()
        service, clock, sink, _ = make_service(persister=pers)
        await service.observe("m1", inputs_for(vvip="Z", weather=0.3, event=True))
        await service.params_for("m1", SecurityTier.Z)
        assert pers.attempts == 1 and pers.param_rows == []
        # the decision is live regardless (mirror + read-only GET) ...
        assert len(sink.params) == 1
        assert service.current_params("m1", SecurityTier.Z)["evidence"]["published"] is True
        assert service.status()["movements"]["m1"]["published"]["audit_failures"] == 1
        # ... and the next decision re-attempts the row
        clock.advance(5)
        await service.params_for("m1", SecurityTier.Z)
        assert pers.attempts == 2 and len(pers.param_rows) == 1
        assert pers.param_rows[0]["evidence"]["publish_reason"] == "first_publish"
        clock.advance(5)
        await service.params_for("m1", SecurityTier.Z)  # unchanged: no more rows
        assert pers.attempts == 2

    async def test_racing_decisions_for_one_subject_audit_once(self):
        # POST /adaptive/observe racing a loop tick on the same subject.
        gate = asyncio.Event()

        @dataclass
        class SlowPersister(FakePersister):
            async def log_params(self, record: dict) -> None:
                await gate.wait()
                self.param_rows.append(record)

        pers = SlowPersister()
        service, *_ = make_service(persister=pers)
        await service.observe("m1", inputs_for(vvip="Z", weather=0.3, event=True))
        racing = [
            asyncio.create_task(service.params_for("m1", SecurityTier.Z))
            for _ in range(2)
        ]
        await asyncio.sleep(0)  # both started; one holds the decision
        gate.set()
        first, second = await asyncio.gather(*racing)
        assert len(pers.param_rows) == 1
        assert first["params"] == second["params"]

    async def test_a_subject_evicted_mid_decision_is_never_audited(self):
        gate = asyncio.Event()

        @dataclass
        class SlowPersister(FakePersister):
            async def log_params(self, record: dict) -> None:
                await gate.wait()
                self.param_rows.append(record)

        pers = SlowPersister()
        service, *_ = make_service(persister=pers)
        await service.observe("m1", inputs_for(vvip="Z", weather=0.3, event=True))
        holder = asyncio.create_task(service.params_for("m1", SecurityTier.Z))
        await asyncio.sleep(0)
        waiter = asyncio.create_task(service.params_for("m1", SecurityTier.Z))
        await asyncio.sleep(0)
        service.evict("m1", reason="test")  # completed while the waiter queued
        gate.set()
        await holder
        late = await waiter
        assert len(pers.param_rows) == 1  # only the in-flight decision
        assert late["evidence"]["subject_tracked"] is False


class TestStatusEndpoint:
    async def test_status_exposes_loop_health_roles_and_subjects(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from traffic_oracle.adaptive.engine_loop import SituationEngineLoop
        from traffic_oracle.adaptive.router import router as adaptive_router

        service, *_ = make_service()
        pool = LoopPool(
            events=[event_row(EVENT_A), event_row(EVENT_B)],
            spines=[spine_row(EVENT_A, [1001]), spine_row(EVENT_B, [1002])],
            lengths={1001: 1000.0, 1002: 1000.0},
        )
        valkey = LoopValkey(latest_docs({1001: latest_doc(1001, 0.2)}))
        loop = SituationEngineLoop(service, pool=pool, valkey_client=valkey)
        await loop._tick()
        app = FastAPI()
        app.include_router(adaptive_router, prefix="/api/v1")
        app.state.adaptive = service
        app.state.adaptive_engine_loop = loop
        body = TestClient(app).get("/api/v1/adaptive/status").json()
        assert body["engine_loop"]["subjects_active"] == 2
        assert body["engine_loop"]["subjects_observed"] == 1
        assert body["roles"] == body["degradation"]["roles"]
        assert set(body["subjects"]) == {EVENT_A, EVENT_B}
        assert body["subjects"][EVENT_A]["band"] == "GREEN"
        assert body["subjects"][EVENT_B]["band"] is None
        assert body["subjects"][EVENT_B]["last_skip_reason"] == "no_fresh_observations"
        assert body["subjects"][EVENT_A]["simulated_feed"] is True
        assert body["subjects"][EVENT_B]["simulated_feed"] is None  # no band yet
        assert "default" not in body["movements"]
        assert body["city_band"]["band"] == "GREEN"
        assert body["city_band_since"]
        assert "twin_calibration" in body
