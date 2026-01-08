"""
DeleGate - The Pure Planner

Pure planning component that decomposes high-level intent into structured execution Plans.
DeleGate brokers capability between principals (AI agents) and self-describing workers
(MCP servers), but NEVER executes work—it only produces Plans.

CRITICAL INVARIANT: If output is not a valid Plan, DeleGate has failed.
"""

__version__ = "0.1.0"
__author__ = "Technomancy Labs"

from delegate.models import (
    Plan,
    PlanStep,
    PlanMetadata,
    PlanRequest,
    PlanResponse,
    StepType,
    TrustTier,
    WorkerManifest,
    WorkerCapability,
    TrustInfo,
)
from delegate.planner import Planner
from delegate.registry import WorkerRegistry

__all__ = [
    "Plan",
    "PlanStep",
    "PlanMetadata",
    "PlanRequest",
    "PlanResponse",
    "StepType",
    "TrustTier",
    "WorkerManifest",
    "WorkerCapability",
    "TrustInfo",
    "Planner",
    "WorkerRegistry",
]
