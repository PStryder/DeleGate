"""DeleGate sourcing its cognition from CogniGate.

CogniGate is the primitive that owns bounded cognition, so ai_provider=cognigate
asks it for the decomposition rather than DeleGate holding a second, unbounded
AI client. The authority split is unchanged: CogniGate returns a plan document,
DeleGate remains the only thing that mints obligations from it.

What these pin down is the boundary translation. CogniGate's step shape is its
own (step_type, tool_name, instructions), the planner's is different
(description, task_type, params, timeout), and everything downstream of
_normalize assumes the latter. A mismatch there parses fine and then plans
nothing, which looks like success.
"""

from __future__ import annotations

import json

import httpx
import pytest

from delegate.ai_planner import (
    CogniGatePlanner,
    PlanningUnavailable,
    build_planner,
)


def _mcp_result(payload: dict) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "result": payload}


class _StubTransport(httpx.AsyncBaseTransport):
    """Answers the cognigate.plan call without a network."""

    def __init__(self, response: dict | None = None, *, status: int = 200):
        self.response = response
        self.status = status
        self.requests: list[dict] = []
        self.headers: list[dict] = []
        self.urls: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        self.headers.append(dict(request.headers))
        self.urls.append(str(request.url))
        return httpx.Response(
            self.status,
            json=self.response,
            request=request,
        )


@pytest.fixture()
def patched_client(monkeypatch):
    """Route the planner's httpx.AsyncClient through a stub transport."""

    def _install(transport: _StubTransport):
        original = httpx.AsyncClient

        def _factory(*args, **kwargs):
            kwargs["transport"] = transport
            return original(*args, **kwargs)

        monkeypatch.setattr("delegate.ai_planner.httpx.AsyncClient", _factory)

    return _install


class TestCogniGatePlanner:
    @pytest.mark.asyncio
    async def test_cognigate_steps_are_mapped_to_the_planner_shape(self, patched_client):
        transport = _StubTransport(
            _mcp_result(
                {
                    "status": "plan_created",
                    "steps": [
                        {
                            "step_number": 1,
                            "step_type": "cognitive",
                            "description": "Research competitors",
                            "instructions": "Look for pricing pages",
                            "tool_name": "search",
                        },
                        {
                            "step_number": 2,
                            "step_type": "output_generation",
                            "description": "Draft the summary",
                            "instructions": None,
                            "tool_name": None,
                        },
                    ],
                    "is_stub": False,
                }
            )
        )
        patched_client(transport)

        result = await CogniGatePlanner(endpoint="http://cognigate.test").plan(
            "research competitors and draft a summary"
        )

        assert [s["description"] for s in result["steps"]] == [
            "Research competitors",
            "Draft the summary",
        ]
        # Every step must carry what _create_complex_plan consumes.
        for step in result["steps"]:
            assert {"description", "task_type", "params", "timeout"} <= set(step)
        # tool_name becomes task_type; absent means fall back to the default.
        assert result["steps"][0]["task_type"] == "search"
        assert result["steps"][1]["task_type"] == "general"

    @pytest.mark.asyncio
    async def test_it_calls_the_plan_tool_not_execute_job(self, patched_client):
        """Asking CogniGate to execute would break DeleGate's never-executes rule."""
        transport = _StubTransport(
            _mcp_result(
                {"steps": [{"description": "Do the thing", "step_type": "cognitive"}]}
            )
        )
        patched_client(transport)

        await CogniGatePlanner(endpoint="http://cognigate.test").plan("do the thing")

        assert len(transport.requests) == 1
        assert transport.requests[0]["params"]["name"] == "cognigate.plan"

    @pytest.mark.asyncio
    async def test_profile_is_passed_through(self, patched_client):
        """Planning under no profile would be unbounded cognition."""
        transport = _StubTransport(
            _mcp_result({"steps": [{"description": "Step", "step_type": "cognitive"}]})
        )
        patched_client(transport)

        await CogniGatePlanner(
            endpoint="http://cognigate.test", profile="research"
        ).plan("something")

        assert transport.requests[0]["params"]["arguments"]["profile"] == "research"

    @pytest.mark.asyncio
    async def test_mcp_error_is_unavailability(self, patched_client):
        transport = _StubTransport({"jsonrpc": "2.0", "id": 1, "error": {"code": -32000}})
        patched_client(transport)

        with pytest.raises(PlanningUnavailable):
            await CogniGatePlanner(endpoint="http://cognigate.test").plan("x")

    @pytest.mark.asyncio
    async def test_transport_failure_is_unavailability(self):
        """A real unreachable endpoint, not a simulated one."""
        planner = CogniGatePlanner(endpoint="http://127.0.0.1:9", timeout_seconds=0.25)
        with pytest.raises(PlanningUnavailable):
            await planner.plan("x")

    @pytest.mark.asyncio
    async def test_a_plan_with_no_usable_steps_is_refused(self, patched_client):
        """An empty plan is worse than no plan: it looks like a decomposition."""
        transport = _StubTransport(_mcp_result({"steps": []}))
        patched_client(transport)

        with pytest.raises(PlanningUnavailable):
            await CogniGatePlanner(endpoint="http://cognigate.test").plan("x")

    @pytest.mark.asyncio
    async def test_stub_cognition_is_announced(self, patched_client, caplog):
        """Canned output must not pass for reasoning unremarked."""
        transport = _StubTransport(
            _mcp_result(
                {
                    "steps": [{"description": "[stub] do it", "step_type": "cognitive"}],
                    "is_stub": True,
                }
            )
        )
        patched_client(transport)

        with caplog.at_level("WARNING"):
            await CogniGatePlanner(endpoint="http://cognigate.test").plan("do it")

        assert any("stub" in record.message.lower() for record in caplog.records)

    @pytest.mark.asyncio
    async def test_auth_token_is_sent(self, patched_client):
        transport = _StubTransport(
            _mcp_result({"steps": [{"description": "Step", "step_type": "cognitive"}]})
        )
        patched_client(transport)

        planner = CogniGatePlanner(endpoint="http://cognigate.test", auth_token="t0ken")
        await planner.plan("x")

        assert transport.headers[0]["authorization"] == "Bearer t0ken"
        assert transport.urls[0] == "http://cognigate.test/mcp"

    @pytest.mark.asyncio
    async def test_no_auth_header_when_no_token(self, patched_client):
        transport = _StubTransport(
            _mcp_result({"steps": [{"description": "Step", "step_type": "cognitive"}]})
        )
        patched_client(transport)

        await CogniGatePlanner(endpoint="http://cognigate.test").plan("x")

        assert "authorization" not in transport.headers[0]


class TestBuildPlanner:
    def test_cognigate_provider_is_selected(self):
        class _S:
            ai_provider = "cognigate"
            cognigate_endpoint = "http://cognigate.test"
            cognigate_auth_token = None
            cognigate_profile = "default"
            ai_model = "unused"
            ai_max_tokens = 2048
            ai_timeout_seconds = 30.0

        planner = build_planner(_S())
        assert isinstance(planner, CogniGatePlanner)

    def test_cognigate_without_an_endpoint_is_refused_at_startup(self):
        """Failing here beats discovering it on the first planning request."""

        class _S:
            ai_provider = "cognigate"
            cognigate_endpoint = ""
            cognigate_auth_token = None
            cognigate_profile = "default"
            ai_model = "unused"
            ai_max_tokens = 2048
            ai_timeout_seconds = 30.0

        with pytest.raises(ValueError, match="cognigate_endpoint"):
            build_planner(_S())

    def test_trailing_slash_does_not_double_the_mcp_suffix(self):
        class _S:
            ai_provider = "cognigate"
            cognigate_endpoint = "http://cognigate.test/"
            cognigate_auth_token = None
            cognigate_profile = "default"
            ai_model = "unused"
            ai_max_tokens = 2048
            ai_timeout_seconds = 30.0

        assert build_planner(_S())._mcp_url == "http://cognigate.test/mcp"


class TestCognitionConfig:
    """The two knobs: which scopes think, and what happens when they cannot."""

    @staticmethod
    def _settings(**overrides):
        from delegate.config import Settings

        base = {"database_url": "postgresql://u:p@localhost/db"}
        base.update(overrides)
        return Settings(**base)

    def test_all_expands_to_every_scope(self):
        assert self._settings(cognition_scopes="all").cognition_scope_set() == {
            "simple",
            "medium",
            "complex",
        }

    def test_none_disables_cognition_everywhere(self):
        assert self._settings(cognition_scopes="none").cognition_scope_set() == set()

    def test_a_subset_is_honoured(self):
        settings = self._settings(cognition_scopes="medium,complex")
        assert settings.cognition_scope_set() == {"medium", "complex"}

    def test_subsets_are_order_and_space_insensitive(self):
        settings = self._settings(cognition_scopes=" COMPLEX , medium ")
        assert settings.cognition_scope_set() == {"medium", "complex"}

    def test_an_unknown_scope_is_rejected_at_startup(self):
        """A typo must not silently mean 'no cognition for anything'."""
        with pytest.raises(ValueError, match="unknown scopes"):
            self._settings(cognition_scopes="complx")

    def test_planning_fallback_defaults_to_escalate(self):
        assert self._settings().planning_fallback == "escalate"

    def test_planning_fallback_accepts_heuristic(self):
        assert self._settings(planning_fallback="HEURISTIC").planning_fallback == "heuristic"

    def test_an_unknown_fallback_is_rejected(self):
        with pytest.raises(ValueError, match="planning_fallback"):
            self._settings(planning_fallback="pretend")
