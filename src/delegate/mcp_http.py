"""DeleGate MCP server (HTTP JSON-RPC)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from delegate.auth import validate_api_key_value
from delegate.config import get_settings
from delegate.database import get_session_dependency
from delegate.middleware import get_rate_limiter
from delegate.models import (
    IntentInput,
    PlanContext,
    PlanRequest,
    PlanningOptions,
    TrustPolicy,
    TrustTier,
    ValidatePlanRequest,
    WorkerManifest,
    WorkerMatchRequest,
    WorkerSearchRequest,
    generate_plan_id,
)
from delegate.planner import Planner, validate_plan
from delegate.receipts import emit_escalation_receipt, emit_plan_receipt, get_retry_queue_size
from delegate.registry import get_registry, WorkerRegistry


class MCPRequest(BaseModel):
    """JSON-RPC request envelope for MCP."""

    jsonrpc: str = Field(default="2.0")
    method: str
    params: dict[str, Any] = Field(default_factory=dict)
    id: Any = None


def _jsonrpc_result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _jsonrpc_error(request_id: Any, code: Any, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


MCP_TOOLS = [
    {
        "name": "delegate.health",
        "description": "Health check / service info",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_delegation_plan",
        "description": "Create plan from intent",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "validate_plan",
        "description": "Validate a plan structure",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_plan",
        "description": "Get a plan by ID",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_plans",
        "description": "List plans with optional filtering",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "analyze_intent",
        "description": "Analyze intent without creating a plan",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "register_worker",
        "description": "Register worker with capabilities",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "search_workers",
        "description": "Search workers by capability",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "match_workers",
        "description": "Match workers to intent",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_workers",
        "description": "List registered workers",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "worker_status",
        "description": "Get worker status by ID",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "delete_worker",
        "description": "Remove a worker from registry",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "stats",
        "description": "Registry and planning statistics",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "cache_clear",
        "description": "Clear registry cache (admin)",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


router = APIRouter(prefix="/mcp", tags=["mcp"])


def _extract_auth_token(arguments: dict[str, Any], request: Request) -> str | None:
    token = arguments.pop("auth_token", None)
    if token:
        return token
    auth_header = request.headers.get("authorization")
    api_key_header = request.headers.get("x-api-key")
    if auth_header and auth_header.lower().startswith("bearer "):
        return auth_header.split(" ", 1)[1]
    if api_key_header:
        return api_key_header
    return None


async def _rate_limit(request: Request) -> None:
    settings = get_settings()
    limiter = get_rate_limiter(
        calls_per_minute=settings.rate_limit_requests_per_minute,
        enabled=settings.rate_limit_enabled,
    )
    await limiter.check_request(request)


def _row_to_plan_dict(row) -> dict:
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


async def _handle_tool(
    name: str,
    arguments: dict[str, Any],
    session: AsyncSession,
    registry: WorkerRegistry,
) -> dict[str, Any]:
    settings = get_settings()

    if name in {"delegate.health", "delegategate.health"}:
        return {
            "status": "healthy",
            "service": "DeleGate",
            "version": "0.1.0",
            "instance_id": settings.instance_id,
        }

    if name == "create_delegation_plan":
        intent = arguments.get("intent", "")
        context_memorygate_refs = arguments.get("context_memorygate_refs") or []
        context_asyncgate_refs = arguments.get("context_asyncgate_refs") or []
        user_constraints = arguments.get("user_constraints") or []
        max_steps = int(arguments.get("max_steps") or settings.max_plan_steps)
        allow_escalation = bool(arguments.get("allow_escalation", True))
        prefer_sync = bool(arguments.get("prefer_sync", False))
        minimum_trust_tier = arguments.get("minimum_trust_tier", settings.default_trust_tier)

        tier_map = {
            "untrusted": TrustTier.UNTRUSTED,
            "sandbox": TrustTier.SANDBOX,
            "verified": TrustTier.VERIFIED,
            "trusted": TrustTier.TRUSTED,
        }
        min_tier = tier_map.get(str(minimum_trust_tier).lower(), TrustTier.VERIFIED)

        request = PlanRequest(
            intent=IntentInput(content=intent),
            context=PlanContext(
                memorygate_refs=context_memorygate_refs,
                asyncgate_task_refs=context_asyncgate_refs,
                user_constraints=user_constraints,
            ),
            planning_options=PlanningOptions(
                max_steps=max_steps,
                allow_escalation=allow_escalation,
                prefer_sync=prefer_sync,
                trust_policy=TrustPolicy(minimum_worker_tier=min_tier),
            ),
        )

        accepted_receipt_id: str | None = None
        plan_id = generate_plan_id()
        accepted_at = datetime.utcnow()
        try:
            accepted_receipt_id = await emit_plan_receipt(
                tenant_id=settings.default_tenant_id,
                request=request,
                created_at=accepted_at,
                plan_id=plan_id,
                phase="accepted",
                status="NA",
            )
        except Exception:
            pass

        planner = Planner()
        response = await planner.create_plan(request)
        created_at = datetime.utcnow()

        if response.status == "plan_created" and response.plan:
            plan = response.plan
            plan.metadata.plan_id = plan_id
            try:
                insert_sql = text(
                    """
                    INSERT INTO plans (
                        plan_id, tenant_id, delegate_id, intent_summary,
                        scope, confidence, steps, references,
                        trust_policy, assumptions, created_at, status
                    ) VALUES (
                        :plan_id, :tenant_id, :delegate_id, :intent_summary,
                        :scope, :confidence, :steps, :references,
                        :trust_policy, :assumptions, :created_at, :status
                    )
                    """
                )
                await session.execute(
                    insert_sql,
                    {
                        "plan_id": plan_id,
                        "tenant_id": settings.default_tenant_id,
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
                    },
                )
                await session.commit()
            except Exception:
                await session.rollback()

            try:
                await emit_plan_receipt(
                    tenant_id=settings.default_tenant_id,
                    plan=plan,
                    request=request,
                    created_at=created_at,
                    phase="complete",
                    status="success",
                    caused_by_receipt_id=accepted_receipt_id or "NA",
                    artifact_pointer=f"delegate://plans/{plan_id}",
                )
            except Exception:
                pass

        elif response.status == "requires_escalation":
            try:
                await emit_escalation_receipt(
                    tenant_id=settings.default_tenant_id,
                    reason=response.reason or "unknown",
                    message=response.message or "Planning escalation",
                    context=response.context or {},
                    created_at=created_at,
                )
            except Exception:
                pass

        return response.model_dump()

    if name == "validate_plan":
        request = ValidatePlanRequest(**arguments)
        is_valid, errors, warnings = validate_plan(request.plan)
        return {"valid": is_valid, "errors": errors, "warnings": warnings}

    if name == "get_plan":
        plan_id = arguments.get("plan_id")
        if not plan_id:
            raise ValueError("plan_id is required")
        query = text(
            """
            SELECT * FROM plans
            WHERE tenant_id = :tenant_id AND plan_id = :plan_id
            """
        )
        result = await session.execute(
            query,
            {"tenant_id": settings.default_tenant_id, "plan_id": plan_id},
        )
        row = result.mappings().first()
        if not row:
            raise ValueError("Plan not found")
        return _row_to_plan_dict(row)

    if name == "list_plans":
        limit = int(arguments.get("limit", 20))
        status_filter = arguments.get("status")
        conditions = ["tenant_id = :tenant_id"]
        params = {"tenant_id": settings.default_tenant_id, "limit": limit}
        if status_filter:
            conditions.append("status = :status")
            params["status"] = status_filter
        where_clause = " AND ".join(conditions)
        query = text(
            f"""
            SELECT * FROM plans
            WHERE {where_clause}
            ORDER BY created_at DESC
            LIMIT :limit
            """
        )
        result = await session.execute(query, params)
        rows = result.mappings().all()
        return {"count": len(rows), "plans": [_row_to_plan_dict(row) for row in rows]}

    if name == "analyze_intent":
        from delegate.planner import detect_task_type, estimate_complexity, detect_scope

        intent = arguments.get("intent", "")
        task_type = detect_task_type(intent)
        complexity = estimate_complexity(intent, {})
        scope = detect_scope(intent, complexity)
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

    if name == "register_worker":
        manifest = WorkerManifest(**arguments)
        registered = await registry.register(manifest)
        return {
            "status": "registered",
            "worker_id": registered.worker_id,
            "capabilities_registered": len(registered.capabilities),
            "trust_tier": (registered.trust.verified_tier or registered.trust.declared_tier).name.lower(),
        }

    if name == "search_workers":
        request = WorkerSearchRequest(**arguments)
        results = await registry.search(
            request.query,
            min_trust_tier=request.trust_tier,
            limit=request.limit,
        )
        return {"count": len(results), "workers": [r.model_dump() for r in results]}

    if name == "match_workers":
        request = WorkerMatchRequest(**arguments)
        results = await registry.match_intent(
            request.intent,
            constraints=request.constraints,
            trust_policy=request.trust_policy,
        )
        return {"count": len(results), "matches": [m.model_dump() for m in results]}

    if name == "list_workers":
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

    if name == "worker_status":
        worker_id = arguments.get("worker_id")
        if not worker_id:
            raise ValueError("worker_id is required")
        manifest = await registry.get(worker_id)
        if not manifest:
            raise ValueError("Worker not found")
        return {
            "worker_id": manifest.worker_id,
            "worker_name": manifest.worker_name,
            "availability": manifest.availability,
            "trust": manifest.trust,
            "last_seen": manifest.last_seen,
        }

    if name == "delete_worker":
        worker_id = arguments.get("worker_id")
        if not worker_id:
            raise ValueError("worker_id is required")
        success = await registry.unregister(worker_id)
        if not success:
            raise ValueError("Worker not found")
        return {"deleted": True}

    if name == "stats":
        registry_stats = registry.get_stats()
        try:
            query = text(
                """
                SELECT status, COUNT(*) as count
                FROM plans
                GROUP BY status
                """
            )
            result = await session.execute(query)
            plan_stats = {row["status"]: row["count"] for row in result.mappings()}
        except Exception:
            plan_stats = {}

        return {
            "registry": registry_stats,
            "plans": plan_stats,
            "receipt_retry_queue": get_retry_queue_size(),
        }

    if name == "cache_clear":
        await registry.clear_cache()
        return {"cleared": True}

    raise ValueError(f"Unknown tool: {name}")


@router.post("")
async def mcp_entry(
    request_body: MCPRequest,
    request: Request,
    session: AsyncSession = Depends(get_session_dependency),
):
    await _rate_limit(request)

    if request_body.method == "tools/list":
        return _jsonrpc_result(request_body.id, {"tools": MCP_TOOLS})

    if request_body.method != "tools/call":
        return _jsonrpc_error(request_body.id, -32601, f"Method not found: {request_body.method}")

    params = request_body.params or {}
    tool_name = params.get("name")
    arguments = params.get("arguments") or {}
    if not tool_name:
        return _jsonrpc_error(request_body.id, -32602, "Missing tool name")

    auth_token = _extract_auth_token(arguments, request)
    try:
        validate_api_key_value(auth_token)
    except Exception as exc:
        return _jsonrpc_error(request_body.id, "AUTH_FAILED", str(exc))

    registry = get_registry()
    try:
        result = await _handle_tool(tool_name, arguments, session, registry)
        return _jsonrpc_result(request_body.id, result)
    except Exception as exc:
        return _jsonrpc_error(request_body.id, getattr(exc, "code", "ERROR"), str(exc))
