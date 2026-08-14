"""Mint AsyncGate obligations from a completed plan.

A plan that stays inside DeleGate is a document. The point of a planning
authority is that its output becomes work someone owes, so this is the step
that turns plan steps into real obligations on the async boundary.

Authority note, because this looks like orchestration and is not:
docs/canonical/DeleGate/alignment.md makes DeleGate one of only two things that
may mint obligations, alongside Principals. Minting is not executing -- DeleGate
creates the tasks and never runs them, never leases them, and never reports
their progress. AsyncGate owns the time boundary and workers own the execution.

Provenance is the part worth getting right. Every task carries
caused_by_receipt_id pointing at the plan receipt that produced it, so the
ledger answers "why does this obligation exist?" by traversal rather than by
inference:

    intent -> [planning obligation] accepted -> complete (plan in body)
                                                    |
                                                    | caused_by
                                                    v
                                          [work obligation] accepted -> ...

Dispatch is best-effort per step. A plan of five steps where AsyncGate rejects
the third should still produce four obligations and say so, rather than
discarding the plan.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


class DispatchResult:
    """Outcome of dispatching one plan.

    Returned rather than raised: a partial dispatch is a real state that the
    caller must be able to report, not an exception.
    """

    def __init__(self) -> None:
        self.dispatched: list[dict[str, Any]] = []
        self.failed: list[dict[str, Any]] = []

    @property
    def ok(self) -> bool:
        return bool(self.dispatched) and not self.failed

    def as_dict(self) -> dict[str, Any]:
        return {
            "dispatched_count": len(self.dispatched),
            "failed_count": len(self.failed),
            "tasks": self.dispatched,
            "failures": self.failed,
        }


class AsyncGateDispatcher:
    """Creates AsyncGate tasks for the steps of an approved plan."""

    def __init__(
        self,
        endpoint: Optional[str],
        *,
        api_key: Optional[str] = None,
        tenant_id: Optional[str] = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        self._endpoint = endpoint
        self._api_key = api_key
        self._tenant_id = tenant_id
        self._timeout = timeout_seconds

    @property
    def enabled(self) -> bool:
        return bool(self._endpoint)

    async def _create_task(
        self,
        client: httpx.AsyncClient,
        *,
        principal_id: str,
        task_type: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        url = self._endpoint if self._endpoint.endswith("/mcp") else f"{self._endpoint.rstrip('/')}/mcp"
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        arguments: dict[str, Any] = {
            "type": task_type,
            "payload": payload,
            "principal_ai": principal_id,
            "agent_id": principal_id,
        }
        if self._tenant_id:
            arguments["tenant_id"] = self._tenant_id

        response = await client.post(
            url,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "asyncgate.create_task", "arguments": arguments},
            },
            headers=headers,
        )
        response.raise_for_status()
        body = response.json()
        if "error" in body:
            raise RuntimeError(f"asyncgate.create_task: {body['error']}")
        result = body.get("result")
        if result is None:
            raise RuntimeError("asyncgate.create_task returned no result")
        return result

    async def dispatch_plan(
        self,
        *,
        steps: list[dict[str, Any]],
        principal_id: str,
        plan_id: str,
        plan_receipt_id: str,
        intent: str,
    ) -> DispatchResult:
        """Mint one AsyncGate obligation per plan step."""
        result = DispatchResult()
        if not self.enabled:
            logger.info("asyncgate_dispatch_disabled: no endpoint configured")
            return result
        if not steps:
            return result

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            for index, step in enumerate(steps, start=1):
                description = step.get("description") or f"Step {index}"
                payload = {
                    "task_summary": description,
                    "intent": intent,
                    "step_number": index,
                    "step_count": len(steps),
                    "plan_id": plan_id,
                    # The provenance link: this obligation exists because that
                    # plan receipt said so.
                    "caused_by_receipt_id": plan_receipt_id,
                    "params": step.get("params") or {},
                }
                try:
                    created = await self._create_task(
                        client,
                        principal_id=principal_id,
                        task_type=step.get("task_type") or "general",
                        payload=payload,
                    )
                except Exception as exc:  # noqa: BLE001 - one bad step must not void the plan
                    logger.warning(
                        "asyncgate_dispatch_step_failed plan=%s step=%d error=%s",
                        plan_id, index, exc,
                    )
                    result.failed.append({"step_number": index, "description": description, "error": str(exc)})
                    continue

                task_id = str(created.get("task_id")) if created.get("task_id") else None
                result.dispatched.append(
                    {"step_number": index, "description": description, "task_id": task_id}
                )

        logger.info(
            "asyncgate_dispatch_complete plan=%s dispatched=%d failed=%d",
            plan_id, len(result.dispatched), len(result.failed),
        )
        return result


def build_dispatcher(settings: Any) -> AsyncGateDispatcher:
    """Build a dispatcher from configuration."""
    return AsyncGateDispatcher(
        getattr(settings, "asyncgate_url", None),
        api_key=getattr(settings, "asyncgate_api_key", None),
        tenant_id=getattr(settings, "asyncgate_tenant_id", None),
    )
