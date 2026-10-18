"""Agency registry — the closed set of dispatchable agencies.

Jan Rakshak (જન રક્ષક) is Gujarat's citizen-volunteer public-order force —
the ground-level eyes-and-ears arm alongside the police.  Making it a
first-class AGENCY ACTOR means: it is dispatchable through the same
channel as police/traffic, its units are emulated on the same graph, and
every dispatch of a Jan Rakshak unit carries the same human-approval and
audit trail as any other agency — Veer strengthens government capability;
volunteers are coordinated, never automated.

The registry is deliberately CLOSED (mirrors the migration 011 CHECK):
every unit surfaced anywhere carries one of these codes and labels, so
the canvas/ops board can badge deterministically.

Ethics (C6): agencies and units — never persons.  No name/face fields.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Agency:
    code: str          # migration 011 CHECK value
    label: str         # UI label (EN)
    label_native: str  # UI label (Gujarati)
    role: str


JAN_RAKSHAK = Agency(
    code="jan_rakshak",
    label="Jan Rakshak",
    label_native="જન રક્ષક",
    role="citizen volunteer coordination, first responder support",
)

AGENCIES: dict[str, Agency] = {
    a.code: a
    for a in (
        JAN_RAKSHAK,
        Agency("police", "Police Control", "પોલીસ કંટ્રોલ", "law & order"),
        Agency("traffic", "Traffic Police", "ટ્રાફિક પોલીસ", "traffic management"),
        Agency("ambulance", "108 Ambulance", "૧૦૮ એમ્બ્યુલન્સ", "emergency medical"),
        Agency("fire", "Fire & Emergency", "ફાયર એન્ડ ઇમરજન્સી", "fire & rescue"),
    )
}


def agency_label(code: str) -> str:
    agency = AGENCIES.get(code)
    return agency.label if agency else code


def is_agency(code: str) -> bool:
    return code in AGENCIES


__all__ = ["AGENCIES", "JAN_RAKSHAK", "Agency", "agency_label", "is_agency"]
