"""
DeleGate REST API

FastAPI routes for plan creation and worker registry.
Per SPEC-DG-0000 API Specification.
"""
import json
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from delegate.models import (
    Plan,
    PlanRequest,
    PlanResponse,
    ValidatePlanRequest,
    ValidatePlanResponse,
    WorkerManifest,
    WorkerRegisterResponse,
    WorkerSearchRequest,
    WorkerSearchResponse,
    WorkerMatchRequest,
    WorkerMatchResponse,
    WorkerStatusResponse,
    TrustTier,
)
from delegate.planner import Planner, validate_plan
from delegate.registry import get_registry, WorkerRegistry
from delegate.database import get_session_dependency
from delegate.receipts import emit_plan_receipt, emit_escalation_receipt, get_retry_queue_size
from delegate.config import get_settings
from delegate.auth import verify_api_key

logger = logging.getLogger(__name__)

router = APIRouter()


# =============================================================================
# Dependencies
# =============================================================================

def get_planner() -> Planner:
    """Get planner instance"""
    return Planner()


def get_tenant_id() -> str:
    """Get tenant ID (placeholder for auth integration)"""
    return get_settings().default_tenant_id


# =============================================================================
# Health Endpoints
# =============================================================================

@router.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "version": "0.1.0"}


@router.get("/")
async def service_info():
    """Service information and capabilities"""
    settings = get_settings()
    registry = get_registry()
    stats = registry.get_stats()

    return {
        "service": "delegate",
        "version": "0.1.0",
        "description": "Pure planning and capability brokering for LegiVellum",
        "instance_id": settings.instance_id,
        "capabilities": [
            "plan_creation",
            "worker_registry",
            "capability_matching",
        ],
        "registry_stats": stats,
    }


# =============================================================================
# Planning Endpoints
# =============================================================================

@router.post("/v1/plan", response_model=PlanResponse, status_code=status.HTTP_200_OK, dependencies=[Depends(verify_api_key)])
async def create_plan(
    request: PlanRequest,
    planner: Planner = Depends(get_planner),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session_dependency),
):
    """
    Create a delegation plan from an intent.

    Per SPEC-DG-0000:
    - Input: Intent (natural language or structured) + Context (optional)
    - Output: Plan (structured, validated) OR Escalation (cannot plan)
    """
    response = await planner.create_plan(request)
    created_at = datetime.utcnow()

    # Store plan if created
    if response.status == "plan_created" and response.plan:
        plan = response.plan

        try:
            insert_sql = text("""
                INSERT INTO plans (
                    plan_id, tenant_id, delegate_id, intent_summary,
                    scope, confidence, steps, references,
                    trust_policy, assumptions, created_at, status
                ) VALUES (
                    :plan_id, :tenant_id, :delegate_id, :intent_summary,
                    :scope, :confidence, :steps, :references,
                    :trust_policy, :assumptions, :created_at, :status
                )
            """)

            await session.execute(insert_sql, {
                "plan_id": plan.metadata.plan_id,
                "tenant_id": tenant_id,
                "delegate_id": plan.metadata.delegate_id,
                "intent_summary": plan.metadata.intent_summary,
                "scope": plan.metadata.scope.value,
                "confidence": plan.metadata.confidence,
                "steps": json.dumps([s.model_dump() for s in plan.steps]),
                "references": json.dumps(plan.references.model_dump()),
                "trust_policy": json.dumps(plan.metadata.trust_policy.model_dump()),
                "assumptions": json.dumps(plan.metadata.assumptions),
                "created_at": created_at,
                "status": "created",
            })
            await session.commit()
        except Exception as e:
            logger.warning(f"Failed to store plan: {e}")
            # Continue even if storage fails

        # Emit plan_created receipt
        try:
            await emit_plan_receipt(
                tenant_id=tenant_id,
                plan=plan,
                request=request,
                created_at=created_at,
            )
        except Exception as e:
            logger.warning(f"Failed to emit plan receipt: {e}")

    elif response.status == "requires_escalation":
        # Emit escalation receipt
        try:
            await emit_escalation_receipt(
                tenant_id=tenant_id,
                reason=response.reason or "unknown",
                message=response.message or "Planning escalation",
                context=response.context,
                created_at=created_at,
            )
        except Exception as e:
            logger.warning(f"Failed to emit escalation receipt: {e}")

    return response


@router.post("/v1/plan/validate", response_model=ValidatePlanResponse, dependencies=[Depends(verify_api_key)])
async def validate_plan_endpoint(request: ValidatePlanRequest):
    """
    Validate a plan structure.

    Checks:
    - Schema version match
    - Unique step IDs
    - Acyclic dependencies
    - Valid dependency references
    - Trust policy satisfiability
    """
    try:
        is_valid, errors, warnings = validate_plan(request.plan)
        return ValidatePlanResponse(
            valid=is_valid,
            errors=errors,
            warnings=warnings,
        )
    except Exception as e:
        return ValidatePlanResponse(
            valid=False,
            errors=[str(e)],
            warnings=[],
        )


@router.get("/v1/plan/{plan_id}", dependencies=[Depends(verify_api_key)])
async def get_plan(
    plan_id: str,
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session_dependency),
):
    """Get a plan by ID"""
    query = text("""
        SELECT * FROM plans
        WHERE tenant_id = :tenant_id AND plan_id = :plan_id
    """)

    result = await session.execute(query, {
        "tenant_id": tenant_id,
        "plan_id": plan_id,
    })
    row = result.mappings().first()

    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "message": "Plan not found"}
        )

    return _row_to_plan_dict(row)


@router.get("/v1/plans", dependencies=[Depends(verify_api_key)])
async def list_plans(
    limit: int = Query(default=20, ge=1, le=100),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    tenant_id: str = Depends(get_tenant_id),
    session: AsyncSession = Depends(get_session_dependency),
):
    """List plans with optional filtering"""
    conditions = ["tenant_id = :tenant_id"]
    params = {"tenant_id": tenant_id, "limit": limit}

    if status_filter:
        conditions.append("status = :status")
        params["status"] = status_filter

    where_clause = " AND ".join(conditions)

    query = text(f"""
        SELECT * FROM plans
        WHERE {where_clause}
        ORDER BY created_at DESC
        LIMIT :limit
    """)

    result = await session.execute(query, params)
    rows = result.mappings().all()

    return {
        "count": len(rows),
        "plans": [_row_to_plan_dict(row) for row in rows],
    }


# =============================================================================
# Worker Registry Endpoints
# =============================================================================

@router.post(
    "/v1/workers/register",
    response_model=WorkerRegisterResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(verify_api_key)]
)
async def register_worker(
    manifest: WorkerManifest,
    registry: WorkerRegistry = Depends(get_registry),
):
    """
    Register a worker with DeleGate.

    Workers self-register by providing:
    - MCP tool manifest (capabilities)
    - Trust tier declaration
    - Performance hints
    - Availability status
    """
    try:
        registered = await registry.register(manifest)
        return WorkerRegisterResponse(
            worker_id=registered.worker_id,
            status="registered",
            registered_at=registered.registered_at or datetime.utcnow(),
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "registration_failed", "message": str(e)}
        )


@router.get("/v1/workers/search", response_model=WorkerSearchResponse, dependencies=[Depends(verify_api_key)])
async def search_workers(
    query: str,
    trust_tier: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=100),
    registry: WorkerRegistry = Depends(get_registry),
):
    """
    Search workers by capability description.

    Returns ranked list of workers matching query with relevance scores.
    """
    min_tier = None
    if trust_tier:
        tier_map = {
            "untrusted": TrustTier.UNTRUSTED,
            "sandbox": TrustTier.SANDBOX,
            "verified": TrustTier.VERIFIED,
            "trusted": TrustTier.TRUSTED,
        }
        min_tier = tier_map.get(trust_tier.lower())

    results = await registry.search(query, min_trust_tier=min_tier, limit=limit)

    return WorkerSearchResponse(
        results=results,
        count=len(results),
    )


@router.post("/v1/workers/match", response_model=WorkerMatchResponse, dependencies=[Depends(verify_api_key)])
async def match_workers(
    request: WorkerMatchRequest,
    registry: WorkerRegistry = Depends(get_registry),
):
    """
    Match workers to an intent with constraint filtering.

    Returns ranked list of workers that can fulfill the intent.
    """
    results = await registry.match_intent(
        request.intent,
        constraints=request.constraints,
        trust_policy=request.trust_policy,
    )

    return WorkerMatchResponse(
        matches=results,
        count=len(results),
    )


@router.get("/v1/workers/{worker_id}/status", response_model=WorkerStatusResponse, dependencies=[Depends(verify_api_key)])
async def get_worker_status(
    worker_id: str,
    registry: WorkerRegistry = Depends(get_registry),
):
    """Get current worker health, availability, and load"""
    manifest = await registry.get(worker_id)

    if not manifest:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "message": "Worker not found"}
        )

    return WorkerStatusResponse(
        worker_id=manifest.worker_id,
        worker_name=manifest.worker_name,
        availability=manifest.availability,
        trust=manifest.trust,
        last_seen=manifest.last_seen,
    )


@router.get("/v1/workers", dependencies=[Depends(verify_api_key)])
async def list_workers(
    registry: WorkerRegistry = Depends(get_registry),
):
    """List all registered workers"""
    workers = await registry.list_all()

    return {
        "count": len(workers),
        "workers": [
            {
                "worker_id": w.worker_id,
                "worker_name": w.worker_name,
                "version": w.version,
                "capabilities": len(w.capabilities),
                "trust_tier": w.trust.verified_tier or w.trust.declared_tier,
                "availability": w.availability.status.value,
                "last_seen": w.last_seen.isoformat() if w.last_seen else None,
            }
            for w in workers
        ],
    }


@router.delete("/v1/workers/{worker_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(verify_api_key)])
async def unregister_worker(
    worker_id: str,
    registry: WorkerRegistry = Depends(get_registry),
):
    """Unregister a worker (admin only)"""
    success = await registry.unregister(worker_id)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "message": "Worker not found"}
        )


# =============================================================================
# Admin Endpoints
# =============================================================================

@router.get("/v1/stats", dependencies=[Depends(verify_api_key)])
async def get_stats(
    registry: WorkerRegistry = Depends(get_registry),
    session: AsyncSession = Depends(get_session_dependency),
):
    """Get planning statistics and registry info"""
    registry_stats = registry.get_stats()

    # Get plan counts by status
    try:
        query = text("""
            SELECT status, COUNT(*) as count
            FROM plans
            GROUP BY status
        """)
        result = await session.execute(query)
        plan_stats = {row["status"]: row["count"] for row in result.mappings()}
    except Exception:
        plan_stats = {}

    return {
        "registry": registry_stats,
        "plans": plan_stats,
        "receipt_retry_queue": get_retry_queue_size(),
    }


@router.post("/v1/cache/clear", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(verify_api_key)])
async def clear_cache():
    """Clear capability matching cache (placeholder)"""
    # In production, this would clear any capability caches
    pass


# =============================================================================
# Helpers
# =============================================================================

def _row_to_plan_dict(row) -> dict:
    """Convert database row to plan dict"""
    steps_data = row["steps"]
    if isinstance(steps_data, str):
        steps_data = json.loads(steps_data)

    refs_data = row["references"]
    if isinstance(refs_data, str):
        refs_data = json.loads(refs_data)

    trust_data = row["trust_policy"]
    if isinstance(trust_data, str):
        trust_data = json.loads(trust_data)

    assumptions = row["assumptions"]
    if isinstance(assumptions, str):
        assumptions = json.loads(assumptions)

    return {
        "plan_id": row["plan_id"],
        "delegate_id": row["delegate_id"],
        "intent_summary": row["intent_summary"],
        "scope": row["scope"],
        "confidence": row["confidence"],
        "steps": steps_data,
        "references": refs_data,
        "trust_policy": trust_data,
        "assumptions": assumptions,
        "status": row["status"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    }
