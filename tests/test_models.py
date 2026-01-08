"""
Tests for DeleGate models.

Validates plan structure invariants per SPEC-DG-0000.
"""
import pytest
from datetime import datetime

from delegate.models import (
    Plan,
    PlanMetadata,
    PlanStep,
    PlanReferences,
    StepType,
    TrustTier,
    TrustInfo,
    TrustPolicy,
    WaitCondition,
    WaitConditionType,
    EscalationReason,
    PlanScope,
    WorkerManifest,
    WorkerCapability,
    PerformanceHints,
    generate_plan_id,
    generate_step_id,
)


class TestPlanInvariants:
    """Test plan validation invariants per SPEC-DG-0000"""

    def test_valid_simple_plan(self):
        """A valid simple plan should pass validation"""
        step = PlanStep(
            step_id="step-001",
            step_type=StepType.CALL_WORKER,
            worker_id="worker-1",
            tool_name="process",
            trust=TrustInfo(declared_tier=TrustTier.VERIFIED),
        )

        plan = Plan(
            metadata=PlanMetadata(
                plan_id="plan-001",
                intent_summary="Test plan",
            ),
            steps=[step],
        )

        assert plan.metadata.plan_schema_version == "DG-PLAN-0001"
        assert len(plan.steps) == 1

    def test_unique_step_ids(self):
        """Step IDs must be unique within plan"""
        steps = [
            PlanStep(
                step_id="step-001",
                step_type=StepType.CALL_WORKER,
                worker_id="worker-1",
                tool_name="tool-1",
            ),
            PlanStep(
                step_id="step-001",  # Duplicate
                step_type=StepType.CALL_WORKER,
                worker_id="worker-2",
                tool_name="tool-2",
            ),
        ]

        with pytest.raises(ValueError, match="unique"):
            Plan(
                metadata=PlanMetadata(intent_summary="Test"),
                steps=steps,
            )

    def test_valid_dependencies(self):
        """Dependencies must reference existing steps"""
        step1 = PlanStep(
            step_id="step-001",
            step_type=StepType.CALL_WORKER,
            worker_id="worker-1",
            tool_name="tool-1",
        )
        step2 = PlanStep(
            step_id="step-002",
            step_type=StepType.CALL_WORKER,
            worker_id="worker-2",
            tool_name="tool-2",
            depends_on=["step-nonexistent"],  # Invalid reference
        )

        with pytest.raises(ValueError, match="non-existent"):
            Plan(
                metadata=PlanMetadata(intent_summary="Test"),
                steps=[step1, step2],
            )

    def test_acyclic_dependencies(self):
        """Dependency graph must be acyclic"""
        step1 = PlanStep(
            step_id="step-001",
            step_type=StepType.CALL_WORKER,
            worker_id="worker-1",
            tool_name="tool-1",
            depends_on=["step-002"],  # Cycle: step-001 -> step-002 -> step-001
        )
        step2 = PlanStep(
            step_id="step-002",
            step_type=StepType.CALL_WORKER,
            worker_id="worker-2",
            tool_name="tool-2",
            depends_on=["step-001"],
        )

        with pytest.raises(ValueError, match="cycles"):
            Plan(
                metadata=PlanMetadata(intent_summary="Test"),
                steps=[step1, step2],
            )

    def test_trust_policy_satisfiability(self):
        """Worker trust must meet policy requirements"""
        step = PlanStep(
            step_id="step-001",
            step_type=StepType.CALL_WORKER,
            worker_id="worker-1",
            tool_name="tool-1",
            trust=TrustInfo(
                declared_tier=TrustTier.SANDBOX,
                verified_tier=TrustTier.SANDBOX,
            ),
        )

        with pytest.raises(ValueError, match="trust tier"):
            Plan(
                metadata=PlanMetadata(
                    intent_summary="Test",
                    trust_policy=TrustPolicy(
                        minimum_worker_tier=TrustTier.VERIFIED,
                    ),
                ),
                steps=[step],
            )


class TestStepTypes:
    """Test step type validation"""

    def test_call_worker_requires_fields(self):
        """call_worker requires worker_id and tool_name"""
        with pytest.raises(ValueError, match="worker_id"):
            PlanStep(
                step_type=StepType.CALL_WORKER,
                tool_name="test",
                # Missing worker_id
            )

    def test_queue_execution_requires_fields(self):
        """queue_execution requires worker_id and tool_name"""
        with pytest.raises(ValueError, match="worker_id"):
            PlanStep(
                step_type=StepType.QUEUE_EXECUTION,
                # Missing required fields
            )

    def test_wait_for_requires_conditions(self):
        """wait_for requires at least one wait_condition"""
        with pytest.raises(ValueError, match="wait_condition"):
            PlanStep(
                step_type=StepType.WAIT_FOR,
                # Missing wait_conditions
            )

    def test_aggregate_requires_inputs(self):
        """aggregate requires at least one input reference"""
        with pytest.raises(ValueError, match="input"):
            PlanStep(
                step_type=StepType.AGGREGATE,
                # Missing inputs
            )

    def test_escalate_requires_reason_and_message(self):
        """escalate requires reason and message"""
        with pytest.raises(ValueError, match="reason"):
            PlanStep(
                step_type=StepType.ESCALATE,
                # Missing reason and message
            )

    def test_valid_wait_for(self):
        """Valid wait_for step"""
        step = PlanStep(
            step_type=StepType.WAIT_FOR,
            wait_conditions=[
                WaitCondition(
                    type=WaitConditionType.TASK_COMPLETION,
                    task_id="task-001",
                )
            ],
        )
        assert step.step_type == StepType.WAIT_FOR

    def test_valid_escalate(self):
        """Valid escalate step"""
        step = PlanStep(
            step_type=StepType.ESCALATE,
            reason=EscalationReason.AMBIGUOUS_INTENT,
            message="Need clarification",
        )
        assert step.step_type == StepType.ESCALATE


class TestTrustModel:
    """Test trust tier model"""

    def test_trust_tier_ordering(self):
        """Trust tiers have correct ordering"""
        assert TrustTier.UNTRUSTED < TrustTier.SANDBOX
        assert TrustTier.SANDBOX < TrustTier.VERIFIED
        assert TrustTier.VERIFIED < TrustTier.TRUSTED

    def test_trust_info_defaults(self):
        """TrustInfo has correct defaults"""
        trust = TrustInfo(declared_tier=TrustTier.SANDBOX)
        assert trust.verified_tier is None
        assert trust.verification_status.value == "unknown"

    def test_trust_policy_defaults(self):
        """TrustPolicy has correct defaults"""
        policy = TrustPolicy()
        assert policy.minimum_worker_tier == TrustTier.VERIFIED
        assert policy.require_signatures is False


class TestWorkerManifest:
    """Test worker manifest models"""

    def test_worker_manifest_creation(self):
        """Worker manifest can be created with required fields"""
        manifest = WorkerManifest(
            worker_id="worker-1",
            worker_name="Test Worker",
            trust=TrustInfo(declared_tier=TrustTier.SANDBOX),
            capabilities=[
                WorkerCapability(
                    tool_name="process",
                    description="Process documents",
                    semantic_tags=["document", "processing"],
                )
            ],
        )

        assert manifest.worker_id == "worker-1"
        assert len(manifest.capabilities) == 1
        assert manifest.availability.status.value == "ready"

    def test_performance_hints_defaults(self):
        """PerformanceHints has sensible defaults"""
        hints = PerformanceHints()
        assert hints.typical_latency_ms == 1000
        assert hints.cost_units == 1
        assert hints.max_runtime_seconds == 60


class TestIdGeneration:
    """Test ID generation utilities"""

    def test_plan_id_format(self):
        """Plan IDs have correct format"""
        plan_id = generate_plan_id()
        assert plan_id.startswith("plan-")
        assert len(plan_id) > 10

    def test_step_id_format(self):
        """Step IDs have correct format"""
        step_id = generate_step_id()
        assert step_id.startswith("step-")
        assert len(step_id) > 10

    def test_ids_are_unique(self):
        """Generated IDs are unique"""
        ids = {generate_plan_id() for _ in range(100)}
        assert len(ids) == 100
