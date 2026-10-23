"""Server-side registration-plate normalization (vehicles only — C6).

Every plate that enters the oracle — an earmark request's path parameter,
an ANPR read's body — goes through :func:`normalize_plate` BEFORE it
touches SQL, so 'GJ 27 EM 9001', 'gj-27-em-9001' and 'GJ27EM9001' all
resolve to the ONE corridor.vehicles row the ANPR plane writes (the
table's UNIQUE(plate) can no longer fork a vehicle on formatting).

Accepted shapes (after upper-casing and stripping spaces, hyphens, dots):
    * standard series  SS RR [AAA] NNNN — state code, 1-2 digit RTO code,
      0-3 letter series, 1-4 digit number   e.g. GJ27EM9001, DL3CAB1234
    * Bharat (BH)      YY BH NNNN AA        e.g. 22BH1234AA

Anything else is refused (ValueError -> HTTP 422): the watchlist never
holds free text that merely looks like a plate.
"""

from __future__ import annotations

import re

__all__ = ["InvalidPlate", "normalize_plate", "PLATE_PATTERNS"]

_SEPARATORS = re.compile(r"[\s\-.]+")

PLATE_PATTERNS = (
    re.compile(r"^[A-Z]{2}[0-9]{1,2}[A-Z]{0,3}[0-9]{1,4}$"),  # standard series
    re.compile(r"^[0-9]{2}BH[0-9]{4}[A-Z]{1,2}$"),             # Bharat series
)

#: Longest raw input considered (spaces/hyphens included).
_MAX_RAW_LEN = 32


class InvalidPlate(ValueError):
    """The input is not a recognisable Indian registration plate."""


def normalize_plate(raw: str) -> str:
    """Canonical plate: upper case, separators stripped, pattern-checked.

    Pure.  Raises :class:`InvalidPlate` for anything that is not a
    standard-series or BH-series plate.
    """
    if not isinstance(raw, str) or not raw.strip() or len(raw) > _MAX_RAW_LEN:
        raise InvalidPlate(f"not a registration plate: {raw!r}")
    plate = _SEPARATORS.sub("", raw.strip().upper())
    if not any(p.match(plate) for p in PLATE_PATTERNS):
        raise InvalidPlate(
            f"not a registration plate: {raw!r} (expected e.g. GJ27EM9001 "
            "or a BH-series plate such as 22BH1234AA)"
        )
    return plate
