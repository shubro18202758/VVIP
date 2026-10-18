"""Agent sub-package — specialized tactical agents (Gemma 4 12B tier)."""

from convoy_brain.agents.base import AgentSpec, structured_agent_call
from convoy_brain.agents.diversion_coordinator import DiversionCoordinatorAgent
from convoy_brain.agents.route_planner import RoutePlannerAgent
from convoy_brain.agents.security_liaison import SecurityLiaisonAgent
from convoy_brain.agents.traffic_analyst import TrafficAnalystAgent

# C0 coordination contract: this __init__ is one of the two shared
# registration sites.  The community_liaison agent (watchlist-anpr stream)
# registers through a guarded import so the tree always boots; a missing
# module leaves the name None (the same "not deployed" tolerance as the
# oracle's coordination stubs in traffic_oracle/coordination.py).
try:
    from convoy_brain.agents.community_liaison import CommunityLiaisonAgent
except ImportError:  # pragma: no cover - stream-ordering tolerance (C0)
    CommunityLiaisonAgent = None  # type: ignore[assignment,misc]

__all__ = [
    "AgentSpec",
    "structured_agent_call",
    "DiversionCoordinatorAgent",
    "RoutePlannerAgent",
    "SecurityLiaisonAgent",
    "TrafficAnalystAgent",
    "CommunityLiaisonAgent",
]
