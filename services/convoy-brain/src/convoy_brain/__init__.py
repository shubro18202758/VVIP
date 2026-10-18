"""Convoy Brain — two-tier agentic orchestration for VVIP convoy planning.

Strategic tier: pplx-decider-v1-27b via model-host (typed choice/noul/score
questions; tool selection, escalation policy, confidence distributions).
Tactical tier: aditi-ps18-v7h Gemma 4 12B via llama-server (the four domain
agents, tool-argument drafting, all operator-facing prose).
Deterministic floor: heuristics that keep the platform answering when either
model tier is slow or unavailable.
"""

__version__ = "0.2.0"
