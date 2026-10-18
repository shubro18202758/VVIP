"""Evaluation sub-package — scenario simulation and what-if analysis.

Additive (C13): ``scenario_sim`` and the ``/api/v1/evaluate/scenarios``
response shape are FROZEN. The real-life-grade twin and the named scenario
library live in :mod:`traffic_oracle.simulation`; they are re-exported here
so evaluate-side consumers get a single import point.
"""

from traffic_oracle.simulation.scenarios import (  # noqa: F401  (re-export)
    SCENARIO_LIBRARY,
    ScenarioSpec,
    export_library,
    get_scenario,
    library_summary,
    list_scenarios,
)
