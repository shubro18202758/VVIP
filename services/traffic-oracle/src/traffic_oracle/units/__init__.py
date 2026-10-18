"""Agency units & dispatch channel — package surface (signalling-janrakshak).

Jan Rakshak is a FIRST-CLASS agency actor: it appears in the agency
registry (``agencies.py``), can be dispatched through the approve/defer
channel (``router.py``), and its emulated units move deterministically
along the corridor graph (``projection.py``) for the ops board / canvas.
"""

from traffic_oracle.units.agencies import AGENCIES, JAN_RAKSHAK

__all__ = ["AGENCIES", "JAN_RAKSHAK"]
