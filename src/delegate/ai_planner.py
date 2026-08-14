"""LLM-backed decomposition for DeleGate's planning authority.

DeleGate is the component that turns intent into obligations, which is a
cognitive act: deciding what work a request implies, in what order, and by
whom. The existing planner does it with regular expressions -- splitting on
" and ", matching keywords like "all" or "multiple" -- which produces a plan
shaped by punctuation rather than meaning.

This adds the cognitive path. The heuristic planner remains as the fallback,
because planning sits on the request path and a planning authority that cannot
plan when a provider is unreachable is worse than one that plans coarsely.

Two providers, selected by DELEGATE_AI_PROVIDER:

  stub        answers locally and deterministically, so the plan -> receipt ->
              downstream path runs in CI with no model and no API key
  openrouter  an OpenAI-compatible chat completions endpoint

The stub is not a mock framework. Its decomposition is derived from the intent
so tests can assert on it, and stable across runs so a CI failure means
something changed rather than the model felt different today.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

STUB_MODEL_NAME = "stub/planner"

PLANNING_SYSTEM_PROMPT = """You decompose a stated intent into an ordered plan.

Return JSON only, of the form:
{"steps": [{"description": "...", "task_type": "...", "rationale": "..."}],
 "scope": "simple|medium|complex",
 "confidence": 0.0-1.0}

Each step must be independently executable work, not a restatement of the
intent. Do not invent capabilities that were not asked for."""


class PlanningUnavailable(RuntimeError):
    """Raised when the provider cannot produce a plan; callers fall back."""


class AIPlanner:
    """Decomposes intent into plan steps via an OpenAI-compatible provider."""

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: Optional[str],
        model: str,
        max_tokens: int = 2048,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.timeout = timeout_seconds

    async def _chat(self, messages: list[dict[str, Any]]) -> str:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.endpoint}/chat/completions", json=payload, headers=headers
            )
            response.raise_for_status()
            data = response.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise PlanningUnavailable(f"provider returned an unusable response: {exc}") from exc

    @staticmethod
    def _parse(content: str) -> dict[str, Any]:
        """Parse the model's JSON, tolerating a fenced code block."""
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", content)
            if fenced:
                try:
                    return json.loads(fenced.group(1))
                except json.JSONDecodeError as exc:
                    raise PlanningUnavailable(f"plan was not valid JSON: {exc}") from exc
            raise PlanningUnavailable("plan was not valid JSON and had no JSON block")

    @staticmethod
    def _normalize(parsed: dict[str, Any], intent: str) -> dict[str, Any]:
        """Coerce provider output into the planner's step shape.

        A model can return something structurally plausible but useless -- no
        steps, or steps that are bare strings. Normalizing here means the rest
        of DeleGate never sees a half-formed plan.
        """
        raw_steps = parsed.get("steps") or []
        if not isinstance(raw_steps, list) or not raw_steps:
            raise PlanningUnavailable("plan contained no steps")

        steps: list[dict[str, Any]] = []
        for index, item in enumerate(raw_steps):
            if isinstance(item, str):
                item = {"description": item}
            if not isinstance(item, dict):
                continue
            description = str(item.get("description") or "").strip()
            if not description:
                continue
            steps.append(
                {
                    "description": description,
                    "task_type": str(item.get("task_type") or "general"),
                    "params": {
                        "step_number": index + 1,
                        "rationale": item.get("rationale"),
                        "intent": intent,
                    },
                    "timeout": 300,
                }
            )
        if not steps:
            raise PlanningUnavailable("plan contained no usable steps")

        confidence = parsed.get("confidence")
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            confidence = 0.5
        confidence = min(max(confidence, 0.0), 1.0)

        scope = str(parsed.get("scope") or "medium").lower()
        if scope not in {"simple", "medium", "complex"}:
            scope = "medium"

        return {"steps": steps, "scope": scope, "confidence": confidence}

    async def plan(self, intent: str, *, task_type: str = "general") -> dict[str, Any]:
        """Return {steps, scope, confidence}. Raises PlanningUnavailable."""
        messages = [
            {"role": "system", "content": PLANNING_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"Intent: {intent}\nDefault task type: {task_type}",
            },
        ]
        try:
            content = await self._chat(messages)
        except PlanningUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - any provider failure falls back
            raise PlanningUnavailable(f"provider call failed: {exc}") from exc
        return self._normalize(self._parse(content), intent)


class StubAIPlanner(AIPlanner):
    """Answers locally and deterministically; performs no reasoning."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("endpoint", "http://stub.invalid")
        kwargs.setdefault("api_key", None)
        kwargs.setdefault("model", STUB_MODEL_NAME)
        super().__init__(**kwargs)
        self.call_count = 0
        logger.warning(
            "delegate_stub_planner_active model=%s: plans are canned and no "
            "reasoning is performed",
            STUB_MODEL_NAME,
        )

    async def _chat(self, messages: list[dict[str, Any]]) -> str:
        """Produce a plan without touching the network."""
        self.call_count += 1
        intent = ""
        for message in reversed(messages):
            if message.get("role") == "user":
                intent = str(message.get("content", ""))
                break
        # Strip the framing the prompt adds so the echo is the intent itself.
        intent = intent.replace("Intent: ", "", 1).split("\nDefault task type:")[0].strip()

        return json.dumps(
            {
                "steps": [
                    {
                        "description": f"[stub] Analyse: {intent}",
                        "task_type": "analysis",
                        "rationale": "Stub planner: no reasoning performed.",
                    },
                    {
                        "description": f"[stub] Execute: {intent}",
                        "task_type": "general",
                        "rationale": "Stub planner: no reasoning performed.",
                    },
                ],
                "scope": "medium",
                "confidence": 0.5,
            }
        )


def build_planner(settings: Any) -> Optional[AIPlanner]:
    """Return a planner for the configured provider, or None if disabled."""
    provider = (getattr(settings, "ai_provider", "") or "").lower()
    if provider in {"", "none", "off", "disabled"}:
        return None
    common = {
        "model": getattr(settings, "ai_model", STUB_MODEL_NAME),
        "max_tokens": getattr(settings, "ai_max_tokens", 2048),
        "timeout_seconds": getattr(settings, "ai_timeout_seconds", 30.0),
    }
    if provider == "stub":
        return StubAIPlanner(**common)
    return AIPlanner(
        endpoint=getattr(settings, "ai_endpoint", "https://openrouter.ai/api/v1"),
        api_key=getattr(settings, "ai_api_key", None),
        **common,
    )
