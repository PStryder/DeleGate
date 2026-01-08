"""
Tests for DeleGate planner.
"""
import pytest
from datetime import datetime

from delegate.models import (
    PlanRequest,
    IntentInput,
    PlanContext,
    PlanningOptions,
    TrustPolicy,
    TrustTier,
    TrustInfo,
    WorkerManifest,
    WorkerCapability,
    PerformanceHints,
    StepType,
)
from delegate.planner import (
    Planner,
    detect_task_type,
    estimate_complexity,
    detect_scope,
    validate_plan,
)
from delegate.registry import WorkerRegistry


class TestIntentAnalysis:
    """Test intent analysis functions"""

    def test_detect_code_generation(self):
        """Detect code generation intent"""
        assert detect_task_type("generate a function") == "code.generate"
        assert detect_task_type("write some code") == "code.generate"
        assert detect_task_type("create implementation") == "code.generate"

    def test_detect_text_summarize(self):
        """Detect text summarization intent"""
        assert detect_task_type("summarize this document") == "text.summarize"
        assert detect_task_type("give me a tldr") == "text.summarize"

    def test_detect_document_ocr(self):
        """Detect OCR intent"""
        assert detect_task_type("extract text from image") == "document.ocr"
        assert detect_task_type("ocr this pdf") == "document.ocr"

    def test_detect_generic(self):
        """Unknown intents map to generic"""
        assert detect_task_type("do something random") == "generic"

    def test_complexity_simple(self):
        """Simple intents are detected"""
        assert estimate_complexity("just do one thing", {}) == "simple"
        assert estimate_complexity("single task", {}) == "simple"

    def test_complexity_complex(self):
        """Complex intents are detected"""
        assert estimate_complexity("analyze all files in the project", {}) == "complex"
        assert estimate_complexity("comprehensive review of multiple documents", {}) == "complex"

    def test_complexity_with_conjunctions(self):
        """Conjunctions increase complexity"""
        assert estimate_complexity("do A and then B and then C", {}) == "complex"


class TestPlannerWithRegistry:
    """Test planner with mock registry"""

    @pytest.fixture
    def registry(self):
        """Create a registry with test workers"""
        reg = WorkerRegistry()
        return reg

    @pytest.fixture
    async def populated_registry(self, registry):
        """Create a registry with registered workers"""
        # Register OCR worker
        await registry.register(WorkerManifest(
            worker_id="ocr-worker",
            worker_name="OCR Service",
            trust=TrustInfo(
                declared_tier=TrustTier.VERIFIED,
                verified_tier=TrustTier.VERIFIED,
            ),
            capabilities=[
                WorkerCapability(
                    tool_name="extract_text",
                    description="Extract text from documents using OCR",
                    semantic_tags=["ocr", "document", "text-extraction"],
                ),
            ],
        ))

        # Register code generator
        await registry.register(WorkerManifest(
            worker_id="code-gen-worker",
            worker_name="Code Generator",
            trust=TrustInfo(
                declared_tier=TrustTier.VERIFIED,
                verified_tier=TrustTier.VERIFIED,
            ),
            capabilities=[
                WorkerCapability(
                    tool_name="generate_code",
                    description="Generate code from specifications",
                    semantic_tags=["code", "generation", "programming"],
                ),
            ],
        ))

        return registry

    @pytest.mark.asyncio
    async def test_create_simple_plan(self, populated_registry):
        """Create a simple plan with available worker"""
        planner = Planner(registry=populated_registry)

        request = PlanRequest(
            intent=IntentInput(content="extract text from invoice.pdf"),
        )

        response = await planner.create_plan(request)

        assert response.status == "plan_created"
        assert response.plan is not None
        assert len(response.plan.steps) >= 1
        assert response.planning_metadata is not None
        assert response.planning_metadata.workers_considered > 0

    @pytest.mark.asyncio
    async def test_escalation_no_workers(self):
        """Escalate when no workers available"""
        registry = WorkerRegistry()  # Empty registry
        planner = Planner(registry=registry)

        request = PlanRequest(
            intent=IntentInput(content="process invoice"),
            planning_options=PlanningOptions(allow_escalation=True),
        )

        response = await planner.create_plan(request)

        assert response.status == "requires_escalation"
        assert response.reason is not None
        assert len(response.suggested_actions) > 0

    @pytest.mark.asyncio
    async def test_planning_failure_without_escalation(self):
        """Fail when escalation disabled and no workers"""
        registry = WorkerRegistry()
        planner = Planner(registry=registry)

        request = PlanRequest(
            intent=IntentInput(content="process invoice"),
            planning_options=PlanningOptions(allow_escalation=False),
        )

        response = await planner.create_plan(request)

        assert response.status == "planning_failed"
        assert response.error_code is not None

    @pytest.mark.asyncio
    async def test_complex_plan_has_aggregation(self, populated_registry):
        """Complex plans include aggregation step"""
        planner = Planner(registry=populated_registry)

        request = PlanRequest(
            intent=IntentInput(content="analyze all documents and create comprehensive report"),
        )

        response = await planner.create_plan(request)

        assert response.status == "plan_created"
        assert response.plan is not None

        # Should have aggregate step
        step_types = [s.step_type for s in response.plan.steps]
        assert StepType.AGGREGATE in step_types

    @pytest.mark.asyncio
    async def test_trust_policy_filtering(self, populated_registry):
        """Trust policy filters workers"""
        planner = Planner(registry=populated_registry)

        # Request requiring TRUSTED tier (higher than our VERIFIED workers)
        request = PlanRequest(
            intent=IntentInput(content="extract text"),
            planning_options=PlanningOptions(
                trust_policy=TrustPolicy(minimum_worker_tier=TrustTier.TRUSTED),
                allow_escalation=True,
            ),
        )

        response = await planner.create_plan(request)

        # Should escalate because no TRUSTED workers
        assert response.status == "requires_escalation"


class TestPlanValidation:
    """Test plan validation"""

    def test_validate_empty_plan(self):
        """Empty plan with no steps generates warning"""
        from delegate.models import Plan, PlanMetadata

        plan = Plan(
            metadata=PlanMetadata(intent_summary="Test"),
            steps=[],
        )

        is_valid, errors, warnings = validate_plan(plan)
        assert is_valid  # Empty plan is valid
        assert "terminal" in warnings[0].lower()  # But warns about no terminal step

    def test_validate_plan_with_terminal(self):
        """Plan with terminal step is valid"""
        from delegate.models import Plan, PlanMetadata, PlanStep, EscalationReason

        plan = Plan(
            metadata=PlanMetadata(intent_summary="Test"),
            steps=[
                PlanStep(
                    step_type=StepType.ESCALATE,
                    reason=EscalationReason.OTHER,
                    message="Done",
                ),
            ],
        )

        is_valid, errors, warnings = validate_plan(plan)
        assert is_valid
        assert len([w for w in warnings if "terminal" in w.lower()]) == 0
