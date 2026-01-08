"""
DeleGate MCP Server

MCP (Model Context Protocol) interface for DeleGate.
Provides tools for plan creation and worker registry access.
"""
import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from delegate.models import (
    Plan,
    PlanRequest,
    PlanResponse,
    IntentInput,
    PlanContext,
    PlanningOptions,
    WorkerManifest,
    WorkerCapability,
    TrustInfo,
    TrustTier,
    TrustPolicy,
    PerformanceHints,
    WorkerAvailabilityInfo,
)
from delegate.planner import Planner
from delegate.registry import get_registry, init_registry
from delegate.receipts import emit_plan_receipt
from delegate.config import get_settings

logger = logging.getLogger(__name__)

# Initialize MCP server
mcp = Server("delegate")


# =============================================================================
# MCP Tools
# =============================================================================

@mcp.tool()
async def create_delegation_plan(
    intent: str,
    context_memorygate_refs: list[int] = None,
    context_asyncgate_refs: list[str] = None,
    user_constraints: list[str] = None,
    max_steps: int = 20,
    allow_escalation: bool = True,
    prefer_sync: bool = False,
    minimum_trust_tier: str = "verified",
) -> dict[str, Any]:
    """
    Create a delegation plan from an intent.

    Transforms natural language intent into a structured plan with:
    - Execution steps (queue via AsyncGate or direct calls)
    - Dependencies between steps
    - Trust metadata for each step
    - Escalation points

    Args:
        intent: What should be accomplished (natural language)
        context_memorygate_refs: MemoryGate observation IDs for context
        context_asyncgate_refs: AsyncGate task IDs for context
        user_constraints: Constraints to respect during planning
        max_steps: Maximum steps allowed in plan
        allow_escalation: Whether escalation is allowed if planning fails
        prefer_sync: Prefer synchronous calls over async when possible
        minimum_trust_tier: Minimum worker trust tier (untrusted, sandbox, verified, trusted)

    Returns:
        Created plan with steps or escalation response
    """
    settings = get_settings()

    # Map trust tier string to enum
    tier_map = {
        "untrusted": TrustTier.UNTRUSTED,
        "sandbox": TrustTier.SANDBOX,
        "verified": TrustTier.VERIFIED,
        "trusted": TrustTier.TRUSTED,
    }
    min_tier = tier_map.get(minimum_trust_tier.lower(), TrustTier.VERIFIED)

    # Build request
    request = PlanRequest(
        intent=IntentInput(content=intent),
        context=PlanContext(
            memorygate_refs=context_memorygate_refs or [],
            asyncgate_task_refs=context_asyncgate_refs or [],
            user_constraints=user_constraints or [],
        ),
        planning_options=PlanningOptions(
            max_steps=max_steps,
            allow_escalation=allow_escalation,
            prefer_sync=prefer_sync,
            trust_policy=TrustPolicy(minimum_worker_tier=min_tier),
        ),
    )

    # Create planner and generate plan
    planner = Planner()
    response = await planner.create_plan(request)
    created_at = datetime.utcnow()

    # Emit receipt if plan was created
    if response.status == "plan_created" and response.plan:
        try:
            await emit_plan_receipt(
                tenant_id=settings.default_tenant_id,
                plan=response.plan,
                request=request,
                created_at=created_at,
            )
        except Exception as e:
            logger.warning(f"Failed to emit plan receipt: {e}")

    # Convert response to dict for MCP
    if response.plan:
        return {
            "status": response.status,
            "plan_id": response.plan.metadata.plan_id,
            "confidence": response.plan.metadata.confidence,
            "scope": response.plan.metadata.scope.value,
            "steps": [s.model_dump() for s in response.plan.steps],
            "planning_metadata": response.planning_metadata.model_dump() if response.planning_metadata else None,
        }
    else:
        return {
            "status": response.status,
            "reason": response.reason,
            "message": response.message,
            "suggested_actions": response.suggested_actions,
            "context": response.context,
        }


@mcp.tool()
async def analyze_intent(intent: str) -> dict[str, Any]:
    """
    Analyze an intent without creating a plan.

    Useful for understanding how DeleGate would decompose an intent
    and what workers are available.

    Args:
        intent: The intent to analyze

    Returns:
        Analysis including detected task type, complexity, and worker matches
    """
    from delegate.planner import detect_task_type, estimate_complexity, detect_scope

    registry = get_registry()

    task_type = detect_task_type(intent)
    complexity = estimate_complexity(intent, {})
    scope = detect_scope(intent, complexity)

    # Find matching workers
    workers = await registry.match_intent(intent)

    return {
        "intent": intent,
        "detected_task_type": task_type,
        "complexity": complexity,
        "scope": scope.value,
        "matching_workers": [
            {
                "worker_id": w.worker_id,
                "relevance": w.relevance_score,
                "capabilities": w.matched_capabilities,
            }
            for w in workers[:5]
        ],
        "worker_count": len(workers),
    }


@mcp.tool()
async def register_worker(
    worker_id: str,
    worker_name: str,
    capabilities: list[dict],
    trust_tier: str = "sandbox",
    version: str = "1.0.0",
) -> dict[str, Any]:
    """
    Register a worker with DeleGate.

    Workers are used for task routing during plan creation.

    Args:
        worker_id: Unique worker identifier
        worker_name: Human-readable worker name
        capabilities: List of capability dicts with tool_name, description, semantic_tags
        trust_tier: Trust tier (untrusted, sandbox, verified, trusted)
        version: Worker version

    Returns:
        Registration status
    """
    tier_map = {
        "untrusted": TrustTier.UNTRUSTED,
        "sandbox": TrustTier.SANDBOX,
        "verified": TrustTier.VERIFIED,
        "trusted": TrustTier.TRUSTED,
    }
    tier = tier_map.get(trust_tier.lower(), TrustTier.SANDBOX)

    caps = []
    for cap in capabilities:
        caps.append(WorkerCapability(
            tool_name=cap.get("tool_name", "unknown"),
            description=cap.get("description", ""),
            semantic_tags=cap.get("semantic_tags", []),
            performance_hints=PerformanceHints(
                typical_latency_ms=cap.get("latency_ms", 1000),
                cost_units=cap.get("cost_units", 1),
                max_runtime_seconds=cap.get("max_runtime", 60),
            ),
        ))

    manifest = WorkerManifest(
        worker_id=worker_id,
        worker_name=worker_name,
        version=version,
        trust=TrustInfo(declared_tier=tier),
        capabilities=caps,
    )

    registry = get_registry()
    registered = await registry.register(manifest)

    return {
        "status": "registered",
        "worker_id": registered.worker_id,
        "capabilities_registered": len(registered.capabilities),
        "trust_tier": tier.name.lower(),
    }


@mcp.tool()
async def search_workers(
    query: str,
    min_trust_tier: str = None,
    limit: int = 10,
) -> dict[str, Any]:
    """
    Search for workers by capability.

    Args:
        query: Search query for capability matching
        min_trust_tier: Minimum trust tier filter
        limit: Maximum results to return

    Returns:
        List of matching workers with relevance scores
    """
    tier_map = {
        "untrusted": TrustTier.UNTRUSTED,
        "sandbox": TrustTier.SANDBOX,
        "verified": TrustTier.VERIFIED,
        "trusted": TrustTier.TRUSTED,
    }
    min_tier = tier_map.get(min_trust_tier.lower()) if min_trust_tier else None

    registry = get_registry()
    results = await registry.search(query, min_trust_tier=min_tier, limit=limit)

    return {
        "count": len(results),
        "workers": [
            {
                "worker_id": r.worker_id,
                "worker_name": r.worker_name,
                "relevance_score": r.relevance_score,
                "matched_capabilities": r.matched_capabilities,
                "trust_tier": (r.trust.verified_tier or r.trust.declared_tier).name.lower(),
                "availability": r.availability.status.value,
            }
            for r in results
        ],
    }


@mcp.tool()
async def list_workers() -> dict[str, Any]:
    """
    List all registered workers.

    Returns:
        List of registered workers with their capabilities
    """
    registry = get_registry()
    workers = await registry.list_all()

    return {
        "count": len(workers),
        "workers": [
            {
                "worker_id": w.worker_id,
                "worker_name": w.worker_name,
                "version": w.version,
                "capabilities": [c.tool_name for c in w.capabilities],
                "trust_tier": (w.trust.verified_tier or w.trust.declared_tier).name.lower(),
                "availability": w.availability.status.value,
            }
            for w in workers
        ],
    }


@mcp.tool()
async def get_registry_stats() -> dict[str, Any]:
    """
    Get worker registry statistics.

    Returns:
        Registry stats including worker counts, trust tier distribution
    """
    registry = get_registry()
    return registry.get_stats()


# =============================================================================
# Bootstrap Tool
# =============================================================================

@mcp.tool()
async def delegate_bootstrap() -> dict[str, Any]:
    """
    Bootstrap DeleGate session.

    Returns configuration and current state for initializing a session.
    """
    settings = get_settings()
    registry = get_registry()
    stats = registry.get_stats()

    return {
        "service": "delegate",
        "version": "0.1.0",
        "instance_id": settings.instance_id,
        "config": {
            "max_plan_steps": settings.max_plan_steps,
            "planning_timeout_seconds": settings.planning_timeout_seconds,
            "default_trust_tier": settings.default_trust_tier,
            "memorygate_url": settings.memorygate_url,
            "asyncgate_url": settings.asyncgate_url,
        },
        "registry_stats": stats,
        "available_tools": [
            "create_delegation_plan",
            "analyze_intent",
            "register_worker",
            "search_workers",
            "list_workers",
            "get_registry_stats",
        ],
    }


# =============================================================================
# Main Entry Point
# =============================================================================

async def main():
    """Run the MCP server"""
    # Initialize registry
    await init_registry()

    logger.info("Starting DeleGate MCP server")

    async with stdio_server() as (read_stream, write_stream):
        await mcp.run(
            read_stream,
            write_stream,
            mcp.create_initialization_options(),
        )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    asyncio.run(main())
