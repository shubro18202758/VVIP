#!/usr/bin/env python3
"""gen_display_geometry.py — SUPERSEDED by scripts/gen_osm_network.py (WP-20).

This script used to draw road-true DISPLAY lines (migration 015's
``display_geom``) for a network whose routing ``geom`` was schematic:
hand-placed lines whose names sat a median ~1 km from the real roads.  Veer's
network now IS the real one — every segment's routing geom is the path along
its OpenStreetMap road between real intersections (geometry_basis 'osm', the
oracle's spatial router) — so there is nothing left to draw apart.

scripts/gen_osm_network.py generates the whole network from the reviewable
mapping table scripts/data/osm_network_map.json and writes, together with the
seed and the network step, services/corridor-store/seed/display_geometry_osm.sql
(now a guarded no-op that only clears a display line a store still carries).
This shim keeps the old entry point working: it runs that generator with the
arguments it was given (--fetch / --cache DIR / --verify).

Data: (c) OpenStreetMap contributors, ODbL 1.0.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

GENERATOR = Path(__file__).resolve().parent / "gen_osm_network.py"

if __name__ == "__main__":
    print("gen_display_geometry.py is superseded: running scripts/gen_osm_network.py",
          file=sys.stderr)
    sys.argv[0] = str(GENERATOR)
    runpy.run_path(str(GENERATOR), run_name="__main__")
