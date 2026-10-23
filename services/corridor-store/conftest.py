"""corridor-store test root.

``pytest`` run from services/corridor-store collects both suites:
    * tests/            migration grammar + graph-integrity checks
    * importer/tests/   the OSM importer (package ``corridor_importer``
                        lives in importer/src, which is not installed)
Putting importer/src on sys.path here lets the importer suite import its
package without a PYTHONPATH incantation.
"""

from __future__ import annotations

import sys
from pathlib import Path

_IMPORTER_SRC = Path(__file__).resolve().parent / "importer" / "src"
if str(_IMPORTER_SRC) not in sys.path:
    sys.path.insert(0, str(_IMPORTER_SRC))
