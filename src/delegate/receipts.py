"""
DeleGate Receipt Emission

Receipt emission with retry logic for ReceiptGate integration.
Per SPEC-DG-0000, DeleGate MUST emit plan_created receipts.
"""
import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from collections import deque

import httpx
import ulid

from delegate.models import Plan, PlanRequest
from delegate.config import get_receiptgate_url, get_receiptgate_api_key

logger = logging.getLogger(__name__)

# The canonical receipt model is a hard dependency, imported unguarded.
#
# This was a parent-directory walk wrapped in `except ImportError`, which found
# LegiVellum/shared in a checkout and nothing in a container. DeleGate happened
# to be the one emitter where the import succeeded -- it pinned python-ulid for
# an unrelated reason and got the demo mount -- so it validated its receipts by
# accident. That is not a property to rely on.
from legivellum.models import Receipt as CanonicalReceipt
from legivellum.ulid import derive_ulid

# In-memory retry queue (production: use Redis or database)
_retry_queue: deque = deque(maxlen=1000)
_retry_worker_running = False


class ReceiptEmissionError(Exception):
    """Receipt emission failed after retries"""
    pass


def _receiptgate_headers() -> dict[str, str]:
    api_key = get_receiptgate_api_key()
    if not api_key:
        raise ReceiptEmissionError("ReceiptGate API key not configured")
    return {"Authorization": f"Bearer {api_key}"}


def _normalize_mcp_endpoint(endpoint: str) -> str:
    normalized = endpoint.rstrip("/")
    if not normalized.endswith("/mcp"):
        normalized = f"{normalized}/mcp"
    return normalized


async def emit_plan_receipt(
    tenant_id: str,
    request: PlanRequest,
    created_at: datetime,
    plan: Plan | None = None,
    plan_id: str | None = None,
    phase: str = "accepted",
    status: str = "NA",
    receipt_id: str | None = None,
    caused_by_receipt_id: str = "NA",
    artifact_pointer: str | None = None,
    artifact_refs: list[dict[str, Any]] | None = None,
) -> str:
    """
    Emit a plan receipt to ReceiptGate (accepted or complete).

    Per SPEC-DG-0000: DeleGate MUST emit plan_created receipt when Plan is produced.
    """
    resolved_plan_id = plan.metadata.plan_id if plan else plan_id
    if not resolved_plan_id:
        raise ValueError("plan_id is required to emit plan receipt")

    intent_summary = plan.metadata.intent_summary if plan else request.intent.content
    task_summary = f"Plan: {intent_summary[:100]}"

    plan_steps = len(plan.steps) if plan else None
    plan_confidence = plan.metadata.confidence if plan else None
    plan_scope = plan.metadata.scope.value if plan else None

    outcome_kind = "NA"
    outcome_text = "NA"
    artifact_location = "NA"
    artifact_pointer_value = "NA"
    artifact_checksum = "NA"
    artifact_size_bytes = 0
    artifact_mime = "NA"
    completed_at = None

    if phase == "complete":
        outcome_kind = "artifact_pointer"
        outcome_text = "plan_stored"
        artifact_location = "delegate.plans"
        artifact_pointer_value = artifact_pointer or f"delegate://plans/{resolved_plan_id}"
        artifact_mime = "application/json"
        completed_at = created_at.isoformat()
        if status == "NA":
            status = "success"

    receipt_id = receipt_id or str(ulid.ULID())

    artifact_refs_list: list[dict[str, Any]] = []
    if phase == "complete" and artifact_pointer_value != "NA":
        artifact_refs_list = artifact_refs or [
            {
                "location": artifact_location,
                "pointer": artifact_pointer_value,
                "mime": artifact_mime,
                "checksum": artifact_checksum,
                "size_bytes": artifact_size_bytes,
            }
        ]

    body_payload = {
        "intent": request.intent.content,
        "steps": plan_steps,
        "confidence": plan_confidence,
        "scope": plan_scope,
        "artifact_pointer": artifact_pointer_value if phase == "complete" else None,
        "artifact_refs": artifact_refs_list or None,
    }

    receipt_data = {
        "schema_version": "1.0",
        "tenant_id": tenant_id,
        "receipt_id": receipt_id,
        "task_id": resolved_plan_id,
        # One plan is one planning obligation: accepted when planning is taken
        # on, complete when the plan exists.
        "obligation_id": derive_ulid("delegate.plan", resolved_plan_id),
        "parent_task_id": "NA",
        "caused_by_receipt_id": caused_by_receipt_id,
        "dedupe_key": "NA",
        "attempt": 0,
        "from_principal": "delegate",
        "for_principal": "delegate",
        "source_system": "delegate",
        "recipient_ai": "delegate",
        "trust_domain": "default",
        "phase": phase,
        "status": status,
        "realtime": False,
        "task_type": "plan.create",
        "task_summary": task_summary,
        "task_body": json.dumps({
            "intent": request.intent.content,
            "steps": plan_steps,
            "confidence": plan_confidence,
            "scope": plan_scope,
        }),
        "inputs": {
            "memorygate_refs": request.context.memorygate_refs,
            "asyncgate_task_refs": request.context.asyncgate_task_refs,
        },
        "expected_outcome_kind": "artifact_pointer",
        "expected_artifact_mime": "application/json",
        "outcome_kind": outcome_kind,
        "outcome_text": outcome_text,
        "artifact_location": artifact_location,
        "artifact_pointer": artifact_pointer_value,
        "artifact_checksum": artifact_checksum,
        "artifact_size_bytes": artifact_size_bytes,
        "artifact_mime": artifact_mime,
        "escalation_class": "NA",
        "escalation_reason": "NA",
        "escalation_to": "NA",
        "retry_requested": False,
        "body": body_payload,
        "artifact_refs": artifact_refs_list,
        "created_at": created_at.isoformat(),
        "stored_at": None,
        "started_at": None,
        "completed_at": completed_at,
        "read_at": None,
        "archived_at": None,
        "metadata": {
            "plan_id": resolved_plan_id,
            "delegate_id": plan.metadata.delegate_id if plan else "delegate",
            "workers_used": list(set(
                s.worker_id for s in plan.steps if s.worker_id
            )) if plan else [],
            "artifact_refs": artifact_refs_list,
        },
    }

    if CanonicalReceipt is not None:
        receipt_data = CanonicalReceipt.model_validate(receipt_data).model_dump(mode="json")

    return await emit_receipt_with_retry(
        receiptgate_url=get_receiptgate_url(),
        tenant_id=tenant_id,
        receipt_data=receipt_data,
    )


async def emit_escalation_receipt(
    tenant_id: str,
    reason: str,
    message: str,
    context: dict[str, Any],
    created_at: datetime,
) -> str:
    """
    Emit a plan_escalated receipt to ReceiptGate.

    Per SPEC-DG-0000: DeleGate MAY emit plan_escalated receipt when escalation occurs.
    """
    receipt_id = str(ulid.ULID())

    body_payload = {
        "reason": reason,
        "message": message,
        "context": context,
    }

    receipt_data = {
        "schema_version": "1.0",
        "tenant_id": tenant_id,
        "receipt_id": receipt_id,
        "task_id": f"escalation-{receipt_id}",
        # An escalation is a distinct responsibility from the plan that could
        # not be produced, so it carries its own obligation id.
        "obligation_id": derive_ulid("delegate.escalation", receipt_id),
        "parent_task_id": "NA",
        "caused_by_receipt_id": "NA",
        "dedupe_key": "NA",
        "attempt": 0,
        "from_principal": "delegate",
        "for_principal": "delegate",
        "source_system": "delegate",
        "recipient_ai": "principal",
        "trust_domain": "default",
        "phase": "escalate",
        "status": "NA",
        "realtime": False,
        "task_type": "plan.escalate",
        "task_summary": f"Planning escalation: {reason}",
        "task_body": json.dumps({
            "reason": reason,
            "message": message,
            "context": context,
        }),
        "inputs": context,
        "expected_outcome_kind": "NA",
        "expected_artifact_mime": "NA",
        "outcome_kind": "NA",
        "outcome_text": "NA",
        "artifact_location": "NA",
        "artifact_pointer": "NA",
        "artifact_checksum": "NA",
        "artifact_size_bytes": 0,
        "artifact_mime": "NA",
        "escalation_class": "capability",
        "escalation_reason": message,
        "escalation_to": "principal",
        "retry_requested": False,
        "body": body_payload,
        "artifact_refs": [],
        "created_at": created_at.isoformat(),
        "stored_at": None,
        "started_at": None,
        "completed_at": None,
        "read_at": None,
        "archived_at": None,
        "metadata": {"reason_code": reason},
    }

    if CanonicalReceipt is not None:
        receipt_data = CanonicalReceipt.model_validate(receipt_data).model_dump(mode="json")

    return await emit_receipt_with_retry(
        receiptgate_url=get_receiptgate_url(),
        tenant_id=tenant_id,
        receipt_data=receipt_data,
    )


async def emit_receipt_with_retry(
    receiptgate_url: str,
    tenant_id: str,
    receipt_data: dict,
    max_retries: int = 3,
    timeout: float = 10.0,
) -> str:
    """
    Emit receipt to ReceiptGate with retry logic.

    Raises ReceiptEmissionError if all retries fail.
    Failed receipts are queued for background retry.
    """
    receipt_id = receipt_data["receipt_id"]
    headers = _receiptgate_headers()
    endpoint = _normalize_mcp_endpoint(receiptgate_url)

    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    endpoint,
                    json={
                        "jsonrpc": "2.0",
                        "id": receipt_id,
                        "method": "tools/call",
                        "params": {
                            "name": "receiptgate.submit_receipt",
                            "arguments": {"receipt": receipt_data},
                        },
                    },
                    headers=headers,
                    timeout=timeout,
                )
                response.raise_for_status()
                data = response.json()
                if data.get("error"):
                    code = data["error"].get("code")
                    if code == "validation_failed":
                        raise ReceiptEmissionError(f"Receipt validation failed: {data['error']}")
                    raise ReceiptEmissionError(f"ReceiptGate error: {data['error']}")

            logger.info(
                "Receipt emitted successfully",
                extra={
                    "receipt_id": receipt_id,
                    "phase": receipt_data["phase"],
                    "task_id": receipt_data["task_id"],
                    "attempt": attempt + 1,
                },
            )
            return receipt_id

        except httpx.HTTPStatusError as e:
            logger.warning(
                f"Receipt emission attempt {attempt + 1} failed",
                extra={
                    "receipt_id": receipt_id,
                    "status_code": e.response.status_code,
                },
            )

        except (httpx.ConnectError, httpx.TimeoutException) as e:
            logger.warning(
                f"Receipt emission attempt {attempt + 1} failed (connection)",
                extra={"receipt_id": receipt_id, "error": str(e)}
            )

        except Exception as e:
            logger.error(
                f"Unexpected error emitting receipt",
                extra={"receipt_id": receipt_id, "error": str(e)}
            )

        if attempt < max_retries - 1:
            await asyncio.sleep(2 ** attempt)  # Exponential backoff

    # All retries failed - queue for background retry
    _queue_for_retry(receiptgate_url, tenant_id, receipt_data)

    raise ReceiptEmissionError(
        f"Failed to emit receipt {receipt_id} after {max_retries} attempts. Queued for retry."
    )


def _queue_for_retry(receiptgate_url: str, tenant_id: str, receipt_data: dict):
    """Queue failed receipt for background retry"""
    _retry_queue.append({
        "receiptgate_url": receiptgate_url,
        "tenant_id": tenant_id,
        "receipt_data": receipt_data,
        "queued_at": datetime.utcnow().isoformat(),
        "retry_count": 0,
    })

    logger.warning(
        f"Receipt queued for background retry",
        extra={
            "receipt_id": receipt_data["receipt_id"],
            "queue_size": len(_retry_queue),
        }
    )


async def retry_worker(interval_seconds: int = 60):
    """
    Background worker that retries failed receipt emissions.

    Run this as a background task in the application lifespan.
    """
    global _retry_worker_running
    _retry_worker_running = True

    logger.info("Receipt retry worker started")

    while _retry_worker_running:
        try:
            await asyncio.sleep(interval_seconds)

            if not _retry_queue:
                continue
            try:
                headers = _receiptgate_headers()
            except ReceiptEmissionError as e:
                logger.error(str(e))
                continue

            logger.info(f"Processing {len(_retry_queue)} queued receipts")

            # Process up to 10 receipts per cycle
            for _ in range(min(10, len(_retry_queue))):
                if not _retry_queue:
                    break

                item = _retry_queue.popleft()
                item["retry_count"] += 1

                try:
                    endpoint = _normalize_mcp_endpoint(item["receiptgate_url"])
                    async with httpx.AsyncClient() as client:
                        response = await client.post(
                            endpoint,
                            json={
                                "jsonrpc": "2.0",
                                "id": item["receipt_data"]["receipt_id"],
                                "method": "tools/call",
                                "params": {
                                    "name": "receiptgate.submit_receipt",
                                    "arguments": {"receipt": item["receipt_data"]},
                                },
                            },
                            headers=headers,
                            timeout=10.0,
                        )
                        response.raise_for_status()
                        data = response.json()
                        if data.get("error"):
                            raise ReceiptEmissionError(f"ReceiptGate error: {data['error']}")

                    logger.info(
                        f"Queued receipt successfully emitted",
                        extra={
                            "receipt_id": item["receipt_data"]["receipt_id"],
                            "retry_count": item["retry_count"],
                        }
                    )

                except Exception as e:
                    if item["retry_count"] < 10:
                        _retry_queue.append(item)
                        logger.warning(
                            f"Retry failed, re-queued",
                            extra={
                                "receipt_id": item["receipt_data"]["receipt_id"],
                                "retry_count": item["retry_count"],
                            },
                        )
                    else:
                        logger.error(
                            f"Giving up on receipt after 10 retries",
                            extra={"receipt_id": item["receipt_data"]["receipt_id"]},
                        )

        except Exception as e:
            logger.error(f"Error in retry worker: {e}")


def stop_retry_worker():
    """Stop the retry worker gracefully"""
    global _retry_worker_running
    _retry_worker_running = False
    logger.info("Receipt retry worker stopped")


def get_retry_queue_size() -> int:
    """Get current retry queue size for monitoring"""
    return len(_retry_queue)
