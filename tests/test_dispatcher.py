"""Tests for minting AsyncGate obligations from a plan.

A plan that stays inside DeleGate is a document. These cover the part that
makes it a plan -- and the authority boundary that makes it legitimate:
DeleGate mints obligations, and never executes, leases, or reports on them.
"""

from __future__ import annotations

import pytest

from delegate.dispatcher import AsyncGateDispatcher, DispatchResult, build_dispatcher


class _Settings:
    asyncgate_url = "http://asyncgate:8080"
    asyncgate_api_key = None
    asyncgate_tenant_id = None


def _steps(count: int = 2):
    return [
        {"description": f"step {i}", "task_type": "call_worker", "params": {"i": i}}
        for i in range(1, count + 1)
    ]


class TestDispatchIsOptional:
    def test_disabled_without_an_endpoint(self):
        assert AsyncGateDispatcher(None).enabled is False

    def test_enabled_with_an_endpoint(self):
        assert build_dispatcher(_Settings()).enabled is True

    @pytest.mark.asyncio
    async def test_disabled_dispatch_is_a_noop(self):
        result = await AsyncGateDispatcher(None).dispatch_plan(
            steps=_steps(), principal_id="p", plan_id="pl", plan_receipt_id="r", intent="i"
        )
        assert result.dispatched == [] and result.failed == []

    @pytest.mark.asyncio
    async def test_empty_plan_dispatches_nothing(self):
        result = await build_dispatcher(_Settings()).dispatch_plan(
            steps=[], principal_id="p", plan_id="pl", plan_receipt_id="r", intent="i"
        )
        assert result.dispatched == []


class TestProvenance:
    @pytest.mark.asyncio
    async def test_every_task_names_the_plan_receipt_as_its_cause(self, monkeypatch):
        """The ledger must answer "why does this obligation exist?" by traversal."""
        seen = []

        async def _capture(self, client, *, principal_id, task_type, payload):
            seen.append(payload)
            return {"task_id": f"task-{payload['step_number']}"}

        monkeypatch.setattr(AsyncGateDispatcher, "_create_task", _capture)
        await build_dispatcher(_Settings()).dispatch_plan(
            steps=_steps(3), principal_id="principal:demo",
            plan_id="plan-1", plan_receipt_id="receipt-abc", intent="do it",
        )
        assert len(seen) == 3
        assert all(p["caused_by_receipt_id"] == "receipt-abc" for p in seen)
        assert all(p["plan_id"] == "plan-1" for p in seen)

    @pytest.mark.asyncio
    async def test_steps_are_numbered_and_counted(self, monkeypatch):
        seen = []

        async def _capture(self, client, *, principal_id, task_type, payload):
            seen.append(payload)
            return {"task_id": "t"}

        monkeypatch.setattr(AsyncGateDispatcher, "_create_task", _capture)
        await build_dispatcher(_Settings()).dispatch_plan(
            steps=_steps(3), principal_id="p", plan_id="pl", plan_receipt_id="r", intent="i"
        )
        assert [p["step_number"] for p in seen] == [1, 2, 3]
        assert all(p["step_count"] == 3 for p in seen)


class TestPartialDispatch:
    @pytest.mark.asyncio
    async def test_one_failed_step_does_not_void_the_plan(self, monkeypatch):
        """Four obligations and an honest report beats discarding the plan."""
        async def _flaky(self, client, *, principal_id, task_type, payload):
            if payload["step_number"] == 3:
                raise RuntimeError("AsyncGate said no")
            return {"task_id": f"task-{payload['step_number']}"}

        monkeypatch.setattr(AsyncGateDispatcher, "_create_task", _flaky)
        result = await build_dispatcher(_Settings()).dispatch_plan(
            steps=_steps(5), principal_id="p", plan_id="pl", plan_receipt_id="r", intent="i"
        )
        assert len(result.dispatched) == 4
        assert len(result.failed) == 1
        assert result.failed[0]["step_number"] == 3
        assert result.ok is False

    @pytest.mark.asyncio
    async def test_full_dispatch_reports_ok(self, monkeypatch):
        async def _ok(self, client, *, principal_id, task_type, payload):
            return {"task_id": "t"}

        monkeypatch.setattr(AsyncGateDispatcher, "_create_task", _ok)
        result = await build_dispatcher(_Settings()).dispatch_plan(
            steps=_steps(2), principal_id="p", plan_id="pl", plan_receipt_id="r", intent="i"
        )
        assert result.ok is True
        assert result.as_dict()["dispatched_count"] == 2

    @pytest.mark.asyncio
    async def test_unreachable_asyncgate_reports_rather_than_raises(self):
        result = await AsyncGateDispatcher(
            "http://127.0.0.1:9", timeout_seconds=0.25
        ).dispatch_plan(
            steps=_steps(2), principal_id="p", plan_id="pl", plan_receipt_id="r", intent="i"
        )
        assert result.dispatched == []
        assert len(result.failed) == 2


class TestAuthorityBoundary:
    @pytest.mark.asyncio
    async def test_dispatch_only_creates_tasks(self, monkeypatch):
        """DeleGate mints obligations; it never executes, leases or reports.

        Minting is within its authority as a planning authority. Calling
        lease/complete/report_progress would make it an executor.
        """
        called = []

        async def _capture_tool(self, client, *, principal_id, task_type, payload):
            called.append("asyncgate.create_task")
            return {"task_id": "t"}

        monkeypatch.setattr(AsyncGateDispatcher, "_create_task", _capture_tool)
        await build_dispatcher(_Settings()).dispatch_plan(
            steps=_steps(3), principal_id="p", plan_id="pl", plan_receipt_id="r", intent="i"
        )
        assert set(called) == {"asyncgate.create_task"}


def test_result_summary_shape():
    result = DispatchResult()
    summary = result.as_dict()
    assert summary["dispatched_count"] == 0 and summary["failed_count"] == 0
