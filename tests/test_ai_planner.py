"""Tests for DeleGate's cognitive planning path.

DeleGate turns intent into obligations, which is a cognitive act. The previous
planner did it with regular expressions, so a plan was shaped by punctuation
rather than meaning: "research X and write Y" split on " and ", while
"research X then write Y" produced one step.

The behaviour that matters most here is the fallback. Planning sits on the
request path, and a planning authority that cannot plan when a provider is
unreachable is worse than one that plans coarsely -- so every provider failure
must degrade to the heuristic splitter rather than failing the request.
"""

from __future__ import annotations

import json

import pytest

from delegate.ai_planner import (
    STUB_MODEL_NAME,
    AIPlanner,
    PlanningUnavailable,
    StubAIPlanner,
    build_planner,
)


class _Settings:
    def __init__(self, **kwargs):
        self.ai_provider = kwargs.get("ai_provider", "stub")
        self.ai_endpoint = kwargs.get("ai_endpoint", "https://unused.invalid")
        self.ai_api_key = kwargs.get("ai_api_key")
        self.ai_model = kwargs.get("ai_model", "test/model")
        self.ai_max_tokens = kwargs.get("ai_max_tokens", 2048)
        self.ai_timeout_seconds = kwargs.get("ai_timeout_seconds", 5.0)


class TestProviderSelection:
    def test_stub_is_selected_by_configuration(self):
        assert isinstance(build_planner(_Settings(ai_provider="stub")), StubAIPlanner)

    def test_real_provider_is_selected(self):
        planner = build_planner(_Settings(ai_provider="openrouter"))
        assert isinstance(planner, AIPlanner)
        assert not isinstance(planner, StubAIPlanner)

    @pytest.mark.parametrize("value", ["", "none", "off", "disabled"])
    def test_cognition_can_be_disabled(self, value):
        """None means the heuristic planner is used directly."""
        assert build_planner(_Settings(ai_provider=value)) is None


class TestStubPlanning:
    @pytest.mark.asyncio
    async def test_produces_multiple_steps(self):
        steps = (await StubAIPlanner().plan("research the thing"))["steps"]
        assert len(steps) >= 2

    @pytest.mark.asyncio
    async def test_steps_derive_from_the_intent(self):
        """Tests can assert on the plan because it echoes the intent."""
        result = await StubAIPlanner().plan("distinctive-intent-77")
        assert any("distinctive-intent-77" in s["description"] for s in result["steps"])

    @pytest.mark.asyncio
    async def test_plans_are_marked_as_stubbed(self):
        result = await StubAIPlanner().plan("do a thing")
        assert all(s["description"].startswith("[stub]") for s in result["steps"])

    @pytest.mark.asyncio
    async def test_planning_is_deterministic(self):
        """A CI failure should mean something changed, not that a model varied."""
        first = await StubAIPlanner().plan("same intent")
        second = await StubAIPlanner().plan("same intent")
        assert first == second

    @pytest.mark.asyncio
    async def test_no_network_call_is_made(self, monkeypatch):
        import httpx

        def _explode(*args, **kwargs):
            raise AssertionError("stub planner attempted a network call")

        monkeypatch.setattr(httpx, "AsyncClient", _explode)
        assert await StubAIPlanner().plan("offline") is not None

    @pytest.mark.asyncio
    async def test_steps_carry_ordering_and_intent(self):
        steps = (await StubAIPlanner().plan("ordered work"))["steps"]
        assert [s["params"]["step_number"] for s in steps] == list(range(1, len(steps) + 1))
        assert all(s["params"]["intent"] == "ordered work" for s in steps)


class TestNormalisation:
    """A provider can return something plausible but unusable."""

    def _normalize(self, parsed):
        return AIPlanner._normalize(parsed, "an intent")

    def test_string_steps_are_accepted(self):
        result = self._normalize({"steps": ["do the thing"]})
        assert result["steps"][0]["description"] == "do the thing"

    def test_blank_descriptions_are_dropped(self):
        result = self._normalize({"steps": [{"description": " "}, {"description": "real"}]})
        assert len(result["steps"]) == 1

    def test_no_steps_is_refused(self):
        with pytest.raises(PlanningUnavailable, match="no steps"):
            self._normalize({"steps": []})

    def test_only_unusable_steps_is_refused(self):
        with pytest.raises(PlanningUnavailable, match="no usable steps"):
            self._normalize({"steps": [{"description": ""}, 42]})

    def test_confidence_is_clamped(self):
        assert self._normalize({"steps": ["s"], "confidence": 5})["confidence"] == 1.0
        assert self._normalize({"steps": ["s"], "confidence": -2})["confidence"] == 0.0

    def test_unparseable_confidence_defaults(self):
        assert self._normalize({"steps": ["s"], "confidence": "high"})["confidence"] == 0.5

    def test_unknown_scope_defaults(self):
        assert self._normalize({"steps": ["s"], "scope": "enormous"})["scope"] == "medium"


class TestParsing:
    def test_plain_json_is_parsed(self):
        assert AIPlanner._parse('{"steps": []}') == {"steps": []}

    def test_fenced_json_is_recovered(self):
        """Models wrap JSON in code fences even when told not to."""
        content = 'Here you go:\n```json\n{"steps": [{"description": "x"}]}\n```'
        assert AIPlanner._parse(content)["steps"][0]["description"] == "x"

    def test_non_json_is_refused(self):
        with pytest.raises(PlanningUnavailable):
            AIPlanner._parse("I'm afraid I can't do that.")


class TestFallbackBehaviour:
    """Provider failure must never fail the planning request."""

    @pytest.mark.asyncio
    async def test_unreachable_provider_raises_planning_unavailable(self):
        planner = AIPlanner(
            endpoint="http://127.0.0.1:9",
            api_key=None,
            model="test/model",
            timeout_seconds=0.25,
        )
        with pytest.raises(PlanningUnavailable):
            await planner.plan("something")

    @pytest.mark.asyncio
    async def test_planner_falls_back_to_heuristic(self, monkeypatch):
        """The Planner degrades rather than failing when cognition is down."""
        from delegate.planner import Planner

        planner = Planner()

        async def _fail(*args, **kwargs):
            raise PlanningUnavailable("provider down")

        if planner.ai_planner is not None:
            monkeypatch.setattr(planner.ai_planner, "plan", _fail)
        assert await planner._ai_subtasks("a and b", "general") is None

    @pytest.mark.asyncio
    async def test_disabled_cognition_returns_none(self, monkeypatch):
        from delegate.planner import Planner

        planner = Planner()
        monkeypatch.setattr(planner, "ai_planner", None)
        assert await planner._ai_subtasks("anything", "general") is None


@pytest.mark.asyncio
async def test_cognitive_steps_are_shaped_for_the_planner():
    """Steps must match what _create_complex_plan consumes.

    A differently-shaped plan parses fine and then produces nothing usable,
    which looks like success while planning nothing.
    """
    steps = (await StubAIPlanner().plan("build it"))["steps"]
    for step in steps:
        assert {"description", "task_type", "params", "timeout"} <= set(step)
