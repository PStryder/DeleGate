"""
DeleGate Plan Generation

Pure planning logic that transforms intent into structured Plans.
Per SPEC-DG-0000 Planning Workflow section.
"""
import asyncio
import logging
import re
import time
from datetime import datetime
from typing import Any, Optional

from delegate.models import (
    Plan,
    PlanMetadata,
    PlanStep,
    PlanReferences,
    PlanReference,
    PlanRequest,
    PlanResponse,
    PlanningMetadata,
    StepType,
    TrustTier,
    TrustInfo,
    TrustPolicy,
    WaitCondition,
    WaitConditionType,
    EscalationReason,
    PlanScope,
    generate_plan_id,
    generate_step_id,
)
from delegate.registry import WorkerRegistry, get_registry
from delegate.config import get_settings, get_instance_id
from delegate.ai_planner import PlanningUnavailable, build_planner

logger = logging.getLogger(__name__)


class CognitionUnavailable(RuntimeError):
    """Cognition was expected for this plan and could not be reached.

    Distinct from PlanningUnavailable, which reports that one provider call
    failed. This says the failure is not being papered over: under
    planning_fallback=escalate it travels up to create_plan and becomes an
    escalation, which DeleGate's README treats as a first-class output --
    "Plan OR Escalation (cannot plan)".
    """


# =============================================================================
# Intent Analysis
# =============================================================================

# Keyword patterns for intent detection
INTENT_PATTERNS = {
    # Code operations
    r"generate|create|write|implement\s+code": "code.generate",
    r"review|check|analyze\s+code": "code.review",
    r"refactor|improve|optimize\s+code": "code.refactor",

    # Data operations
    r"analyze|examine|investigate\s+data": "data.analyze",
    r"transform|convert|process\s+data": "data.transform",
    r"extract|parse": "data.extract",

    # Text operations
    r"summarize|summary|tldr": "text.summarize",
    r"translate|translation": "text.translate",

    # Document operations
    r"ocr|extract\s+text|scan": "document.ocr",
    r"invoice|receipt|bill": "document.invoice",
    r"pdf|document": "document.process",

    # Search/research
    r"search|find|lookup|research": "search",

    # Image operations
    r"generate\s+image|create\s+image|draw": "image.generate",
    r"analyze\s+image|image\s+analysis": "image.analyze",
    r"enhance|improve\s+quality": "image.enhance",
}

COMPLEXITY_COMPLEX_WORDS = {
    "multiple", "several", "all", "entire", "complete",
    "analyze", "research", "comprehensive", "full",
    "pipeline", "workflow", "sequence",
}

COMPLEXITY_SIMPLE_WORDS = {
    "single", "one", "simple", "quick", "just",
    "only", "basic",
}


def detect_task_type(intent: str) -> str:
    """Detect primary task type from intent"""
    intent_lower = intent.lower()

    if re.search(r"ocr|extract\s+text|scan", intent_lower):
        return "document.ocr"

    for pattern, task_type in INTENT_PATTERNS.items():
        if re.search(pattern, intent_lower):
            return task_type

    return "generic"


def estimate_complexity(intent: str, context: dict) -> str:
    """Estimate task complexity: simple, medium, complex"""
    intent_lower = intent.lower()
    intent_words = set(intent_lower.split())

    complex_score = len(intent_words & COMPLEXITY_COMPLEX_WORDS)
    simple_score = len(intent_words & COMPLEXITY_SIMPLE_WORDS)

    # Check for explicit multi-step indicators
    if " and " in intent_lower or "," in intent:
        complex_score += 2

    # Check for context size
    if len(context) > 3:
        complex_score += 1

    if complex_score > simple_score + 1:
        return "complex"
    elif simple_score > complex_score:
        return "simple"
    else:
        return "medium"


def detect_scope(intent: str, complexity: str) -> PlanScope:
    """Detect plan scope from intent and complexity"""
    intent_lower = intent.lower()

    if any(word in intent_lower for word in ["campaign", "project", "initiative"]):
        return PlanScope.CAMPAIGN
    elif complexity == "complex" or "workflow" in intent_lower:
        return PlanScope.WORKFLOW
    else:
        return PlanScope.SINGLE_TASK


# =============================================================================
# Planner Class
# =============================================================================

class Planner:
    """
    Pure planner that converts intent to structured Plans.

    CRITICAL: Planner NEVER executes work - only produces Plans.
    """

    def __init__(self, registry: Optional[WorkerRegistry] = None):
        self.registry = registry or get_registry()
        self.settings = get_settings()
        # Decomposition is a cognitive act. None means the cognitive path is
        # disabled and the heuristic planner is used directly.
        self.ai_planner = build_planner(self.settings)

    async def create_plan(self, request: PlanRequest) -> PlanResponse:
        """
        Create a delegation plan from an intent.

        Returns either a Plan or an Escalation response.
        """
        start_time = time.time()
        warnings = []

        try:
            intent = request.intent.content
            context_data = {
                "memorygate_refs": request.context.memorygate_refs,
                "asyncgate_task_refs": request.context.asyncgate_task_refs,
                "constraints": request.context.user_constraints,
            }

            # Analyze intent
            task_type = detect_task_type(intent)
            complexity = estimate_complexity(intent, context_data)
            scope = detect_scope(intent, complexity)

            logger.info(
                f"Planning intent",
                extra={
                    "intent": intent[:100],
                    "task_type": task_type,
                    "complexity": complexity,
                    "scope": scope.value,
                }
            )

            # Find capable workers
            workers = await self.registry.match_intent(
                intent,
                trust_policy=request.planning_options.trust_policy,
            )

            if not workers:
                # Check if we should escalate or fail
                if request.planning_options.allow_escalation:
                    return self._escalation_response(
                        reason=EscalationReason.NO_CAPABLE_WORKERS,
                        message=f"No workers available for task type: {task_type}",
                        suggested_actions=[
                            "Register a worker with matching capabilities",
                            "Modify intent to match available workers",
                            "Lower trust tier requirements",
                        ],
                        context={"task_type": task_type, "intent": intent},
                    )
                else:
                    return PlanResponse(
                        status="planning_failed",
                        error_code="NO_CAPABLE_WORKERS",
                        message=f"No workers available for: {intent[:100]}",
                        suggestions=["Register appropriate workers"],
                    )

            # Check for degraded workers
            for w in workers:
                if w.availability.status.value == "degraded":
                    warnings.append(f"Worker {w.worker_id} has degraded availability")

            # Build the plan based on complexity
            if complexity == "simple":
                plan = await self._create_simple_plan(
                    request, task_type, workers, scope
                )
            elif complexity == "medium":
                plan = await self._create_medium_plan(
                    request, task_type, workers, scope
                )
            else:
                plan = await self._create_complex_plan(
                    request, task_type, workers, scope
                )

            # Calculate planning duration
            duration_ms = int((time.time() - start_time) * 1000)

            # Determine confidence based on worker matches and complexity
            confidence = self._calculate_confidence(workers, complexity)

            return PlanResponse(
                status="plan_created",
                plan=plan,
                planning_metadata=PlanningMetadata(
                    workers_considered=len(workers),
                    planning_duration_ms=duration_ms,
                    confidence=confidence,
                    warnings=warnings,
                ),
            )

        except CognitionUnavailable as e:
            # Not a planning error: DeleGate declines to invent a plan it could
            # not think through. Reported as its own reason so an operator can
            # tell "cognition is down" from "the planner broke".
            logger.warning("planning_escalated_cognition_unavailable: %s", e)

            if request.planning_options.allow_escalation:
                return self._escalation_response(
                    reason=EscalationReason.RESOURCE_UNAVAILABLE,
                    message=f"Cognition unavailable, refusing to plan heuristically: {e}",
                    suggested_actions=[
                        "Check CogniGate availability",
                        "Set DELEGATE_PLANNING_FALLBACK=heuristic to accept a coarse plan",
                        "Retry once cognition is restored",
                    ],
                    context={"error": str(e), "planning_fallback": self.settings.planning_fallback},
                )
            return PlanResponse(
                status="planning_failed",
                error_code="COGNITION_UNAVAILABLE",
                message=str(e),
                suggestions=["Restore cognition, or allow heuristic fallback"],
            )

        except Exception as e:
            logger.error(f"Planning failed: {e}", exc_info=True)

            if request.planning_options.allow_escalation:
                return self._escalation_response(
                    reason=EscalationReason.OTHER,
                    message=f"Planning failed: {str(e)}",
                    suggested_actions=["Retry with simpler intent", "Check system status"],
                    context={"error": str(e)},
                )
            else:
                return PlanResponse(
                    status="planning_failed",
                    error_code="PLANNING_ERROR",
                    message=str(e),
                )

    async def _create_simple_plan(
        self,
        request: PlanRequest,
        task_type: str,
        workers: list,
        scope: PlanScope,
    ) -> Plan:
        """Create a simple single-step plan"""
        intent = request.intent.content
        worker = workers[0]  # Best matching worker

        # Get the matching tool from the worker
        tool_name = worker.matched_capabilities[0] if worker.matched_capabilities else task_type

        # Get full worker manifest for trust info
        manifest = await self.registry.get(worker.worker_id)

        steps = []
        assumptions = [f"Worker {worker.worker_id} can handle {task_type}"]

        # A simple plan is one step by definition, so cognition cannot change
        # its shape here -- it refines what that step says. If cognition comes
        # back with several steps, the heuristic classifier that labelled this
        # "simple" disagrees with the thing that actually read the intent, and
        # that disagreement is recorded rather than resolved silently in either
        # direction.
        subtasks = await self._ai_subtasks(intent, task_type, "simple")
        if subtasks:
            step_intent = subtasks[0].get("description") or intent
            if len(subtasks) > 1:
                logger.info(
                    "cognition_disagrees_with_scope classified=simple steps=%d",
                    len(subtasks),
                )
                assumptions.append(
                    f"Classified simple, but cognition proposed {len(subtasks)} "
                    f"steps; only the first is planned here"
                )
        else:
            step_intent = intent

        # Determine if we should use sync or async
        prefer_sync = request.planning_options.prefer_sync
        step_type = StepType.CALL_WORKER if prefer_sync else StepType.QUEUE_EXECUTION

        # Step 1: Execute the task
        step1_id = generate_step_id()
        steps.append(PlanStep(
            step_id=step1_id,
            step_type=step_type,
            worker_id=worker.worker_id,
            tool_name=tool_name,
            parameters={"intent": step_intent},
            trust=manifest.trust if manifest else worker.trust,
            output_binding="result",
            timeout_seconds=300 if step_type == StepType.QUEUE_EXECUTION else None,
        ))

        # If async, add wait step
        if step_type == StepType.QUEUE_EXECUTION:
            step2_id = generate_step_id()
            steps.append(PlanStep(
                step_id=step2_id,
                step_type=StepType.WAIT_FOR,
                depends_on=[step1_id],
                wait_conditions=[WaitCondition(
                    type=WaitConditionType.TASK_COMPLETION,
                    task_id="${" + step1_id + ".output.task_id}",
                )],
                timeout_seconds=600,
                output_binding="wait_result",
            ))

        return Plan(
            metadata=PlanMetadata(
                plan_id=generate_plan_id(),
                delegate_id=get_instance_id(),
                intent_summary=intent[:200],
                scope=scope,
                confidence=0.9,
                trust_policy=request.planning_options.trust_policy,
                assumptions=assumptions,
            ),
            steps=steps,
            references=self._build_references(request),
        )

    async def _create_medium_plan(
        self,
        request: PlanRequest,
        task_type: str,
        workers: list,
        scope: PlanScope,
    ) -> Plan:
        """Create a medium complexity plan with async work + aggregation"""
        intent = request.intent.content
        worker = workers[0]
        tool_name = worker.matched_capabilities[0] if worker.matched_capabilities else task_type
        manifest = await self.registry.get(worker.worker_id)

        steps = []
        step_ids = []

        # When cognition is wired to this scope it decides what the queued work
        # actually is. Without it the intent is queued whole, which is the
        # heuristic behaviour: one opaque task the worker has to interpret.
        subtasks = await self._ai_subtasks(intent, task_type, "medium")
        queued = subtasks or [{"description": intent, "task_type": task_type}]

        # Step 1..N: Queue the work
        for subtask in queued:
            step_id = generate_step_id()
            step_ids.append(step_id)
            steps.append(PlanStep(
                step_id=step_id,
                step_type=StepType.QUEUE_EXECUTION,
                worker_id=worker.worker_id,
                tool_name=subtask.get("task_type") if subtasks else tool_name,
                parameters={
                    "intent": subtask.get("description", intent),
                    **({"params": subtask["params"]} if subtask.get("params") else {}),
                },
                trust=manifest.trust if manifest else worker.trust,
                output_binding=f"result_{len(step_ids)}",
                timeout_seconds=300,
            ))

        # Step N+1: Wait for all of them
        wait_id = generate_step_id()
        steps.append(PlanStep(
            step_id=wait_id,
            step_type=StepType.WAIT_FOR,
            depends_on=list(step_ids),
            wait_conditions=[
                WaitCondition(
                    type=WaitConditionType.TASK_COMPLETION,
                    task_id="${" + step_id + ".output.task_id}",
                )
                for step_id in step_ids
            ],
            timeout_seconds=600,
            output_binding="wait_result",
        ))

        # Step N+2: Aggregate results
        aggregate_id = generate_step_id()
        steps.append(PlanStep(
            step_id=aggregate_id,
            step_type=StepType.AGGREGATE,
            depends_on=[wait_id],
            inputs=["${" + step_id + ".output}" for step_id in step_ids],
            aggregation_instruction="Summarize and validate the results",
            output_binding="summary",
        ))

        return Plan(
            metadata=PlanMetadata(
                plan_id=generate_plan_id(),
                delegate_id=get_instance_id(),
                intent_summary=intent[:200],
                scope=scope,
                confidence=0.8,
                trust_policy=request.planning_options.trust_policy,
                assumptions=[
                    f"Worker {worker.worker_id} can handle {task_type}",
                    "Results can be aggregated into summary",
                ],
            ),
            steps=steps,
            references=self._build_references(request),
        )

    async def _create_complex_plan(
        self,
        request: PlanRequest,
        task_type: str,
        workers: list,
        scope: PlanScope,
    ) -> Plan:
        """Create a complex plan with parallel tasks + aggregation"""
        intent = request.intent.content

        # Split intent into subtasks
        subtasks = await self._ai_subtasks(
            intent, task_type, "complex"
        ) or self._split_into_subtasks(intent, task_type)

        steps = []
        execution_step_ids = []

        # Create parallel execution steps
        for i, subtask in enumerate(subtasks):
            # Find best worker for this subtask
            subtask_workers = await self.registry.match_intent(
                subtask["description"],
                trust_policy=request.planning_options.trust_policy,
            )

            if subtask_workers:
                worker = subtask_workers[0]
                manifest = await self.registry.get(worker.worker_id)
            elif workers:
                worker = workers[0]
                manifest = await self.registry.get(worker.worker_id)
            else:
                continue

            step_id = generate_step_id()
            execution_step_ids.append(step_id)

            tool_name = (
                worker.matched_capabilities[0]
                if worker.matched_capabilities
                else subtask.get("task_type", task_type)
            )

            steps.append(PlanStep(
                step_id=step_id,
                step_type=StepType.QUEUE_EXECUTION,
                worker_id=worker.worker_id,
                tool_name=tool_name,
                parameters=subtask.get("params", {}),
                trust=manifest.trust if manifest else worker.trust,
                output_binding=f"subtask_{i}_result",
                timeout_seconds=subtask.get("timeout", 300),
            ))

        if not execution_step_ids:
            # No executable steps - escalate
            steps.append(PlanStep(
                step_id=generate_step_id(),
                step_type=StepType.ESCALATE,
                reason=EscalationReason.NO_CAPABLE_WORKERS,
                message="Could not find workers for any subtask",
                context={"subtasks": subtasks},
                suggested_options=["Register additional workers", "Simplify request"],
            ))
        else:
            # Wait for all parallel tasks
            wait_step_id = generate_step_id()
            steps.append(PlanStep(
                step_id=wait_step_id,
                step_type=StepType.WAIT_FOR,
                depends_on=execution_step_ids,
                wait_conditions=[
                    WaitCondition(
                        type=WaitConditionType.TASK_COMPLETION,
                        task_id="${" + sid + ".output.task_id}",
                    )
                    for sid in execution_step_ids
                ],
                timeout_seconds=900,
                output_binding="all_results",
            ))

            # Aggregate all results
            aggregate_step_id = generate_step_id()
            steps.append(PlanStep(
                step_id=aggregate_step_id,
                step_type=StepType.AGGREGATE,
                depends_on=[wait_step_id],
                inputs=["${" + sid + ".output}" for sid in execution_step_ids],
                aggregation_instruction="Combine results from all subtasks into coherent output",
                output_binding="final_result",
            ))

        return Plan(
            metadata=PlanMetadata(
                plan_id=generate_plan_id(),
                delegate_id=get_instance_id(),
                intent_summary=intent[:200],
                scope=scope,
                confidence=0.7,  # Lower confidence for complex plans
                estimated_cost_units=len(subtasks) * 50,
                trust_policy=request.planning_options.trust_policy,
                assumptions=[
                    f"Intent can be decomposed into {len(subtasks)} subtasks",
                    "Subtask results can be aggregated",
                ],
            ),
            steps=steps,
            references=self._build_references(request),
        )

    def _cognition_enabled_for(self, complexity: str) -> bool:
        """Whether this scope should consult cognition.

        Two separate reasons to skip it, and they are not the same thing: the
        provider can be switched off entirely (ai_provider=none), or cognition
        can be on but not wired to this scope. Neither is a failure, so neither
        escalates -- nothing was promised.
        """
        if self.ai_planner is None:
            return False
        return complexity in self.settings.cognition_scope_set()

    async def _ai_subtasks(
        self, intent: str, task_type: str, complexity: str
    ) -> Optional[list[dict[str, Any]]]:
        """Decompose intent with the configured provider.

        Returns None when the caller should use the heuristic splitter, either
        because cognition is not wired to this scope or because it failed and
        planning_fallback allows a coarse plan.

        Raises CognitionUnavailable when cognition was expected and could not
        be reached under planning_fallback=escalate. That is the default,
        because a heuristic plan is structurally indistinguishable from a
        reasoned one: substituting it silently tells the caller a decomposition
        was thought through when it was produced by splitting on " and ".
        """
        if not self._cognition_enabled_for(complexity):
            return None
        try:
            result = await self.ai_planner.plan(intent, task_type=task_type)
        except PlanningUnavailable as exc:
            return self._handle_cognition_failure(exc, complexity)
        except Exception as exc:  # noqa: BLE001
            return self._handle_cognition_failure(exc, complexity)
        logger.info(
            "cognitive_plan_produced steps=%d scope=%s confidence=%.2f",
            len(result["steps"]), result["scope"], result["confidence"],
        )
        return result["steps"]

    def _handle_cognition_failure(
        self, exc: Exception, complexity: str
    ) -> Optional[list[dict[str, Any]]]:
        """Apply planning_fallback to a cognition failure."""
        if self.settings.planning_fallback == "heuristic":
            logger.warning(
                "cognitive_planning_unavailable scope=%s falling back to "
                "heuristic: %s",
                complexity, exc,
            )
            return None
        logger.warning(
            "cognitive_planning_unavailable scope=%s escalating: %s",
            complexity, exc,
        )
        raise CognitionUnavailable(str(exc)) from exc

    def _split_into_subtasks(
        self,
        intent: str,
        task_type: str,
    ) -> list[dict[str, Any]]:
        """Split a complex intent into subtasks (heuristic fallback)."""
        intent_lower = intent.lower()

        # Check for explicit conjunctions
        if " and " in intent_lower or "," in intent:
            parts = re.split(r",\s*| and ", intent, flags=re.IGNORECASE)
            subtasks = []
            for i, part in enumerate(parts):
                part = part.strip()
                if part:
                    subtasks.append({
                        "description": part,
                        "task_type": detect_task_type(part) or task_type,
                        "params": {"subtask": part, "part_number": i + 1},
                        "timeout": 300,
                    })
            return subtasks if subtasks else self._default_subtasks(intent, task_type)

        # Check for "all" or "multiple" indicators
        if any(word in intent_lower for word in ["all", "multiple", "several", "every"]):
            return [
                {
                    "description": f"Gather data for: {intent}",
                    "task_type": "search" if "search" in intent_lower else task_type,
                    "params": {"phase": "gather", "intent": intent},
                    "timeout": 300,
                },
                {
                    "description": "Analyze gathered data",
                    "task_type": "data.analyze",
                    "params": {"phase": "analyze", "intent": intent},
                    "timeout": 300,
                },
                {
                    "description": "Generate final output",
                    "task_type": task_type,
                    "params": {"phase": "generate", "intent": intent},
                    "timeout": 300,
                },
            ]

        return self._default_subtasks(intent, task_type)

    def _default_subtasks(self, intent: str, task_type: str) -> list[dict]:
        """Default subtask split for unrecognized patterns"""
        return [
            {
                "description": f"Primary task: {intent}",
                "task_type": task_type,
                "params": {"intent": intent},
                "timeout": 300,
            },
            {
                "description": "Verify and validate results",
                "task_type": "generic",
                "params": {"phase": "verify", "intent": intent},
                "timeout": 120,
            },
        ]

    def _build_references(self, request: PlanRequest) -> PlanReferences:
        """Build plan references from request context"""
        input_sources = []
        expected_outputs = []

        # Add MemoryGate observation references
        for obs_id in request.context.memorygate_refs:
            input_sources.append(PlanReference(
                type="memorygate_observation",
                observation_id=obs_id,
                relevance="context",
            ))

        # Add AsyncGate task references
        for task_id in request.context.asyncgate_task_refs:
            input_sources.append(PlanReference(
                type="asyncgate_task",
                task_id=task_id,
                relevance="related_task",
            ))

        return PlanReferences(
            input_sources=input_sources,
            expected_outputs=expected_outputs,
        )

    def _calculate_confidence(self, workers: list, complexity: str) -> float:
        """Calculate plan confidence based on workers and complexity"""
        base_confidence = {
            "simple": 0.9,
            "medium": 0.8,
            "complex": 0.7,
        }.get(complexity, 0.75)

        # Adjust based on worker quality
        if workers:
            best_score = max(w.relevance_score for w in workers)
            base_confidence = base_confidence * (0.5 + 0.5 * best_score)

        return min(1.0, base_confidence)

    def _escalation_response(
        self,
        reason: EscalationReason,
        message: str,
        suggested_actions: list[str],
        context: dict[str, Any],
    ) -> PlanResponse:
        """Create an escalation response"""
        return PlanResponse(
            status="requires_escalation",
            reason=reason.value,
            message=message,
            suggested_actions=suggested_actions,
            context=context,
        )


# =============================================================================
# Validation
# =============================================================================

def validate_plan(plan: Plan) -> tuple[bool, list[str], list[str]]:
    """
    Validate a plan structure.
    Returns (is_valid, errors, warnings).
    """
    errors = []
    warnings = []

    try:
        # The Plan model validator already checks invariants
        # This function provides additional checks

        # Check step count
        settings = get_settings()
        if len(plan.steps) > settings.max_plan_steps:
            errors.append(
                f"Plan has {len(plan.steps)} steps, exceeds maximum {settings.max_plan_steps}"
            )

        # Check for orphaned steps (no path to completion)
        has_terminal = any(
            s.step_type in (StepType.AGGREGATE, StepType.ESCALATE)
            for s in plan.steps
        )
        if not has_terminal:
            warnings.append("Plan has no terminal step (aggregate or escalate)")

        # Check trust consistency
        for step in plan.steps:
            if step.trust:
                if step.trust.verified_tier is None and step.trust.declared_tier >= TrustTier.VERIFIED:
                    warnings.append(
                        f"Step {step.step_id} claims verified tier but has no verification"
                    )

        return len(errors) == 0, errors, warnings

    except Exception as e:
        errors.append(str(e))
        return False, errors, warnings
