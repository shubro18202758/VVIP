"""CLI for the simulation package.

    python -m traffic_oracle.simulation export [--out PATH]
        Write the scenario library JSON (default VVIP/scripts/scenario_library.json)
        — the artifact scripts/impact_run.py consumes: same seed-network
        segment facts it parses itself, plus closure/demand parameters that
        map onto commuter_delay_min(hold_min, lam, n_junctions).

    python -m traffic_oracle.simulation run SCENARIO_ID [--replications N]
        Run one library scenario with its own seed and print the per-replication
        metrics table (deterministic — identical on every invocation).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_SCRIPTS = Path(__file__).resolve().parents[5] / "scripts"


def _cmd_export(out: str | None) -> int:
    from traffic_oracle.simulation.scenarios import export_library

    payload = export_library()
    target = Path(out) if out else _REPO_SCRIPTS / "scenario_library.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"wrote {target} ({payload['n_scenarios']} scenarios)")
    return 0


def _cmd_run(scenario_id: str, replications: int | None) -> int:
    from traffic_oracle.simulation.engine import ClosurePolicy, SimulationEngine
    from traffic_oracle.simulation.scenarios import get_scenario

    sc = get_scenario(scenario_id)
    engine = SimulationEngine()
    results = engine.run(
        route_segment_ids=list(sc.route_segment_ids),
        departure_time=sc.departure_dt(),
        closure_policy=ClosurePolicy(**sc.closure_policy),
        horizon_min=sc.horizon_min,
        n_replications=replications or sc.n_replications,
        seed=sc.seed,
        weather=sc.weather_state(),
        event_config=sc.event_config(),
        scheduled_events=sc.scheduled_events(),
    )
    print(f"scenario: {sc.scenario_id} ({sc.name})  seed={sc.seed}")
    hdr = (
        f"{'rep':>3} {'disruption_vh':>14} {'max_queue_m':>11} "
        f"{'avg_clos_s':>10} {'transit_s':>9} {'recovery_s':>10} {'amb_s':>7} {'events':>6}"
    )
    print(hdr)
    for r in results:
        print(
            f"{r.replication:>3} {r.total_disruption_vehicle_hours:>14.3f} "
            f"{r.max_queue_length_m:>11.1f} {r.avg_closure_duration_sec:>10.1f} "
            f"{r.convoy_transit_time_sec:>9.1f} {r.recovery_sec:>10.1f} "
            f"{r.ambulance_delay_sec:>7.1f} {r.n_events:>6}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="traffic_oracle.simulation")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_export = sub.add_parser("export", help="write scenario_library.json")
    p_export.add_argument("--out", default=None)

    p_run = sub.add_parser("run", help="run one library scenario")
    p_run.add_argument("scenario_id")
    p_run.add_argument("--replications", type=int, default=None)

    args = parser.parse_args(argv)
    if args.cmd == "export":
        return _cmd_export(args.out)
    return _cmd_run(args.scenario_id, args.replications)


if __name__ == "__main__":
    sys.exit(main())
