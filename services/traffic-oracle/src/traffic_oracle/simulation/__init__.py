"""Simulation twin — real-life-grade VVIP corridor simulator.

Public surface (stable for the other streams):
    - ``SimulationEngine`` / ``run_replication``   — seeded Monte-Carlo twin
    - ``TwinEnv``                                   — plain-Python RL wrapper
      (reset/step/is_stub; no gymnasium import — adaptive wraps it)
    - ``SCENARIO_LIBRARY`` / ``get_scenario``       — 14 named scenarios
    - ``router``                                    — FastAPI C5 endpoints
      (POST /api/v1/simulate/twin, POST /api/v1/simulate/replay)
    - ``CALIBRATION`` / ``run_replay``              — fail-closed calibration
    - ``SeededRng`` / ``derive_seed``               — deterministic seeding

Every random draw in the package flows through SeededRng; identical seeds
reproduce byte-identical event schedules and metrics.
"""

from traffic_oracle.simulation.calibration import CALIBRATION, CalibrationState, run_replay
from traffic_oracle.simulation.engine import (
    ClosurePolicy,
    ForecastShapes,
    ReplicationResult,
    SimulationConfig,
    SimulationEngine,
    run_replication,
    shapes_from_forecaster,
)
from traffic_oracle.simulation.events import (
    EventConfig,
    EventSchedule,
    EventType,
    RoadEvent,
    ScheduledEvent,
    sample_events,
)
from traffic_oracle.simulation.network import (
    SegmentSpec,
    SimNetwork,
    default_network,
    from_rows,
    from_seed_sql,
)
from traffic_oracle.simulation.rng import SeededRng, derive_seed
from traffic_oracle.simulation.scenarios import (
    SCENARIO_LIBRARY,
    ScenarioSpec,
    export_library,
    get_scenario,
    library_summary,
    list_scenarios,
)
from traffic_oracle.simulation.twin import TwinEnv

__all__ = [
    "CALIBRATION",
    "CalibrationState",
    "ClosurePolicy",
    "EventConfig",
    "EventSchedule",
    "EventType",
    "ForecastShapes",
    "ReplicationResult",
    "RoadEvent",
    "SCENARIO_LIBRARY",
    "ScheduledEvent",
    "ScenarioSpec",
    "SegmentSpec",
    "SeededRng",
    "SimNetwork",
    "SimulationConfig",
    "SimulationEngine",
    "TwinEnv",
    "default_network",
    "derive_seed",
    "export_library",
    "from_rows",
    "from_seed_sql",
    "get_scenario",
    "library_summary",
    "list_scenarios",
    "run_replay",
    "run_replication",
    "sample_events",
    "shapes_from_forecaster",
]
