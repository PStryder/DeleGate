"""
DeleGate Models

Plan creation, worker registry, and trust models per SPEC-DG-0000.
"""
from datetime import datetime
from enum import Enum, IntEnum
from typing import Any, Optional
from pydantic import BaseModel, Field, field_validator, model_validator
import ulid


# =============================================================================
# ID Generation
# =============================================================================

def generate_plan_id() -> str:
    """Generate a new plan ID using ULID"""
    return f"plan-{ulid.new()}"


def generate_step_id() -> str:
    """Generate a new step ID using ULID"""
    return f"step-{ulid.new()}"


# =============================================================================
# Enums
# =============================================================================

class StepType(str, Enum):
    """Five step types per SPEC-DG-0000"""
    CALL_WORKER = "call_worker"           # Direct synchronous execution
    QUEUE_EXECUTION = "queue_execution"   # Async execution via AsyncGate
    WAIT_FOR = "wait_for"                 # Block until tasks complete
    AGGREGATE = "aggregate"               # Request synthesis by principal
    ESCALATE = "escalate"                 # Cannot proceed, request decision


class TrustTier(IntEnum):
    """Trust tiers per SPEC-DG-0000 (increasing trust)"""
    UNTRUSTED = 0   # Manual approval, full audit, sandboxed
    SANDBOX = 1     # Basic verification, isolated, auto-approve low-risk
    VERIFIED = 2    # Code audit, signed, organization-approved
    TRUSTED = 3     # Root authority signed, production-grade


class VerificationStatus(str, Enum):
    """Worker verification status"""
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"


class WorkerAvailability(str, Enum):
    """Worker availability status"""
    READY = "ready"
    DEGRADED = "degraded"
    MAINTENANCE = "maintenance"
    OFFLINE = "offline"


class PlanScope(str, Enum):
    """Plan scope classification"""
    SINGLE_TASK = "single_task"
    WORKFLOW = "workflow"
    CAMPAIGN = "campaign"


class EscalationReason(str, Enum):
    """Reasons for escalation"""
    AMBIGUOUS_INTENT = "ambiguous_intent"
    NO_CAPABLE_WORKERS = "no_capable_workers"
    TRUST_VIOLATION = "trust_violation"
    POLICY_VIOLATION = "policy_violation"
    CONSTRAINT_CONFLICT = "constraint_conflict"
    RESOURCE_UNAVAILABLE = "resource_unavailable"
    OTHER = "other"


class WaitConditionType(str, Enum):
    """Types of wait conditions"""
    TASK_COMPLETION = "task_completion"
    RECEIPT_PHASE = "receipt_phase"


# =============================================================================
# Trust Models
# =============================================================================

class TrustInfo(BaseModel):
    """Trust information for a worker per SPEC-DG-0000"""
    declared_tier: TrustTier = Field(
        ...,
        description="Trust tier declared by the worker"
    )
    verified_tier: Optional[TrustTier] = Field(
        default=None,
        description="Trust tier verified by DeleGate (null if not verified)"
    )
    verification_status: VerificationStatus = Field(
        default=VerificationStatus.UNKNOWN,
        description="Verification status"
    )
    signature: Optional[str] = Field(
        default=None,
        description="Cryptographic signature (base64-encoded)"
    )
    verified_at: Optional[datetime] = Field(
        default=None,
        description="When verification occurred"
    )
    verified_by: Optional[str] = Field(
        default=None,
        description="Who performed verification"
    )


class TrustPolicy(BaseModel):
    """Trust requirements for a plan"""
    minimum_worker_tier: TrustTier = Field(
        default=TrustTier.VERIFIED,
        description="Minimum trust tier required for workers"
    )
    require_signatures: bool = Field(
        default=False,
        description="Whether cryptographic signatures are required"
    )
    allow_cross_department: bool = Field(
        default=True,
        description="Whether cross-department delegation is allowed"
    )


# =============================================================================
# Worker Registry Models
# =============================================================================

class PerformanceHints(BaseModel):
    """Performance hints for a worker capability"""
    typical_latency_ms: int = Field(default=1000, ge=0)
    cost_units: int = Field(default=1, ge=0)
    max_runtime_seconds: int = Field(default=60, ge=1)


class WorkerCapability(BaseModel):
    """A capability (tool) exposed by a worker"""
    tool_name: str = Field(..., description="Name of the tool")
    description: str = Field(..., description="Human-readable description")
    semantic_tags: list[str] = Field(
        default_factory=list,
        description="Semantic tags for capability matching"
    )
    input_schema: dict[str, Any] = Field(
        default_factory=dict,
        description="JSON Schema for tool input"
    )
    output_schema: dict[str, Any] = Field(
        default_factory=dict,
        description="JSON Schema for tool output"
    )
    performance_hints: PerformanceHints = Field(
        default_factory=PerformanceHints,
        description="Performance characteristics"
    )


class WorkerAvailabilityInfo(BaseModel):
    """Current availability status of a worker"""
    status: WorkerAvailability = Field(default=WorkerAvailability.READY)
    current_load: float = Field(default=0.0, ge=0.0, le=1.0)
    max_concurrent: int = Field(default=10, ge=1)


class WorkerManifest(BaseModel):
    """
    Worker manifest provided by MCP server for registration.
    Per SPEC-DG-0000 Worker Registry section.
    """
    worker_id: str = Field(..., description="Unique worker identifier")
    worker_name: str = Field(..., description="Human-readable worker name")
    version: str = Field(default="1.0.0", description="Worker version")

    trust: TrustInfo = Field(..., description="Trust information")
    capabilities: list[WorkerCapability] = Field(
        default_factory=list,
        description="List of capabilities (tools)"
    )
    availability: WorkerAvailabilityInfo = Field(
        default_factory=WorkerAvailabilityInfo,
        description="Availability status"
    )
    constraints: list[str] = Field(
        default_factory=list,
        description="Worker constraints and limitations"
    )

    # Registration metadata
    registered_at: Optional[datetime] = Field(default=None)
    last_seen: Optional[datetime] = Field(default=None)


class WorkerSearchResult(BaseModel):
    """Result from worker search/match operations"""
    worker_id: str
    worker_name: str
    relevance_score: float = Field(ge=0.0, le=1.0)
    matched_capabilities: list[str]
    trust: TrustInfo
    availability: WorkerAvailabilityInfo


# =============================================================================
# Plan Step Models
# =============================================================================

class WaitCondition(BaseModel):
    """Condition for wait_for step"""
    type: WaitConditionType
    task_id: Optional[str] = Field(
        default=None,
        description="Task ID to wait for (can use ${step-id.output.task_id} reference)"
    )
    acceptable_phases: list[str] = Field(
        default_factory=lambda: ["complete", "escalate"]
    )


class PlanStep(BaseModel):
    """
    A single step in a delegation plan.
    Five step types per SPEC-DG-0000.
    """
    step_id: str = Field(default_factory=generate_step_id)
    step_type: StepType
    depends_on: list[str] = Field(
        default_factory=list,
        description="Step IDs that must complete first"
    )
    timeout_seconds: Optional[int] = Field(
        default=None,
        description="Timeout for queue_execution and wait_for steps"
    )

    # For call_worker and queue_execution
    worker_id: Optional[str] = None
    tool_name: Optional[str] = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    priority: str = Field(default="normal")  # low, normal, high, critical
    output_binding: Optional[str] = Field(
        default=None,
        description="Variable name to bind output to"
    )

    # Trust metadata (for call_worker and queue_execution)
    trust: Optional[TrustInfo] = None

    # For wait_for
    wait_conditions: list[WaitCondition] = Field(default_factory=list)

    # For aggregate
    inputs: list[str] = Field(
        default_factory=list,
        description="Step output references to aggregate"
    )
    aggregation_instruction: Optional[str] = None

    # For escalate
    reason: Optional[EscalationReason] = None
    message: Optional[str] = None
    context: dict[str, Any] = Field(default_factory=dict)
    suggested_options: list[str] = Field(default_factory=list)

    @model_validator(mode='after')
    def validate_step_type_requirements(self) -> 'PlanStep':
        """Validate step has required fields for its type"""
        if self.step_type == StepType.CALL_WORKER:
            if not self.worker_id or not self.tool_name:
                raise ValueError("call_worker requires worker_id and tool_name")

        elif self.step_type == StepType.QUEUE_EXECUTION:
            if not self.worker_id or not self.tool_name:
                raise ValueError("queue_execution requires worker_id and tool_name")

        elif self.step_type == StepType.WAIT_FOR:
            if not self.wait_conditions:
                raise ValueError("wait_for requires at least one wait_condition")

        elif self.step_type == StepType.AGGREGATE:
            if not self.inputs:
                raise ValueError("aggregate requires at least one input reference")

        elif self.step_type == StepType.ESCALATE:
            if not self.reason or not self.message:
                raise ValueError("escalate requires reason and message")

        return self


# =============================================================================
# Plan Models
# =============================================================================

class PlanMetadata(BaseModel):
    """Plan metadata section per SPEC-DG-0000"""
    plan_schema_version: str = Field(
        default="DG-PLAN-0001",
        description="Plan schema version identifier"
    )
    plan_id: str = Field(
        default_factory=generate_plan_id,
        description="Unique plan identifier"
    )
    delegate_id: str = Field(
        default="delegate-1",
        description="DeleGate instance that created this plan"
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="ISO 8601 timestamp"
    )
    intent_summary: str = Field(
        ...,
        description="Human-readable intent summary"
    )
    scope: PlanScope = Field(
        default=PlanScope.SINGLE_TASK,
        description="Plan scope classification"
    )
    confidence: float = Field(
        default=0.8,
        ge=0.0,
        le=1.0,
        description="Plan quality estimate"
    )
    estimated_cost_units: Optional[int] = Field(
        default=None,
        ge=0,
        description="Resource estimate"
    )
    assumptions: list[str] = Field(
        default_factory=list,
        description="Explicit planning assumptions"
    )
    trust_policy: TrustPolicy = Field(
        default_factory=TrustPolicy,
        description="Trust requirements"
    )


class PlanReference(BaseModel):
    """Reference to external data"""
    type: str  # memorygate_observation, asyncgate_task, etc.
    id: Optional[str] = None
    observation_id: Optional[int] = None
    task_id: Optional[str] = None
    step_id: Optional[str] = None
    relevance: Optional[str] = None
    description: Optional[str] = None


class PlanReferences(BaseModel):
    """References section of a plan"""
    input_sources: list[PlanReference] = Field(default_factory=list)
    expected_outputs: list[PlanReference] = Field(default_factory=list)


class Plan(BaseModel):
    """
    Complete delegation plan per SPEC-DG-0000.
    Three sections: metadata, steps, references.
    """
    metadata: PlanMetadata
    steps: list[PlanStep] = Field(default_factory=list)
    references: PlanReferences = Field(default_factory=PlanReferences)

    @model_validator(mode='after')
    def validate_plan_invariants(self) -> 'Plan':
        """
        Validate plan invariants per SPEC-DG-0000:
        1. Schema version match
        2. Unique step IDs
        3. Acyclic dependencies
        4. Valid dependency references
        5. Trust policy satisfiability
        """
        # 1. Schema version
        if self.metadata.plan_schema_version != "DG-PLAN-0001":
            raise ValueError(
                f"Unsupported schema version: {self.metadata.plan_schema_version}"
            )

        # 2. Unique step IDs
        step_ids = [s.step_id for s in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("Step IDs must be unique within plan")

        # 3 & 4. Acyclic dependencies and valid references
        step_id_set = set(step_ids)
        for step in self.steps:
            for dep_id in step.depends_on:
                if dep_id not in step_id_set:
                    raise ValueError(
                        f"Step {step.step_id} depends on non-existent step {dep_id}"
                    )

        # Check for cycles using topological sort
        if not self._is_dag():
            raise ValueError("Dependency graph contains cycles")

        # 5. Trust policy satisfiability
        min_tier = self.metadata.trust_policy.minimum_worker_tier
        for step in self.steps:
            if step.trust and step.trust.verified_tier is not None:
                if step.trust.verified_tier < min_tier:
                    raise ValueError(
                        f"Step {step.step_id} worker trust tier {step.trust.verified_tier} "
                        f"is below minimum required {min_tier}"
                    )

        return self

    def _is_dag(self) -> bool:
        """Check if dependency graph is a DAG using Kahn's algorithm"""
        # Build adjacency list and in-degree count
        in_degree = {s.step_id: 0 for s in self.steps}
        graph = {s.step_id: [] for s in self.steps}

        for step in self.steps:
            for dep_id in step.depends_on:
                graph[dep_id].append(step.step_id)
                in_degree[step.step_id] += 1

        # Start with nodes that have no dependencies
        queue = [sid for sid, deg in in_degree.items() if deg == 0]
        processed = 0

        while queue:
            node = queue.pop(0)
            processed += 1
            for neighbor in graph[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        return processed == len(self.steps)


# =============================================================================
# Request/Response Models
# =============================================================================

class IntentInput(BaseModel):
    """Intent specification for plan creation"""
    type: str = Field(
        default="natural_language",
        description="natural_language or structured_task"
    )
    content: str = Field(..., description="The intent to fulfill")
    urgency: str = Field(
        default="normal",
        description="low, normal, high, critical"
    )


class PlanContext(BaseModel):
    """Context for plan creation"""
    memorygate_refs: list[int] = Field(
        default_factory=list,
        description="MemoryGate observation IDs for context"
    )
    asyncgate_task_refs: list[str] = Field(
        default_factory=list,
        description="AsyncGate task IDs for context"
    )
    user_constraints: list[str] = Field(
        default_factory=list,
        description="User-specified constraints"
    )


class PlanningOptions(BaseModel):
    """Options for plan generation"""
    max_steps: int = Field(default=20, ge=1, le=100)
    allow_escalation: bool = Field(default=True)
    prefer_sync: bool = Field(
        default=False,
        description="Prefer sync calls over async when possible"
    )
    trust_policy: TrustPolicy = Field(default_factory=TrustPolicy)


class PlanRequest(BaseModel):
    """
    Request to create a delegation plan.
    Per SPEC-DG-0000 Request Envelope.
    """
    intent: IntentInput
    context: PlanContext = Field(default_factory=PlanContext)
    planning_options: PlanningOptions = Field(default_factory=PlanningOptions)


class PlanningMetadata(BaseModel):
    """Metadata about the planning process"""
    workers_considered: int = Field(default=0)
    planning_duration_ms: int = Field(default=0)
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    warnings: list[str] = Field(default_factory=list)


class PlanResponse(BaseModel):
    """
    Response after creating a plan.
    Per SPEC-DG-0000 Response Envelope.
    """
    status: str = Field(
        ...,
        description="plan_created, requires_escalation, or planning_failed"
    )
    plan: Optional[Plan] = None
    planning_metadata: Optional[PlanningMetadata] = None

    # For escalation
    reason: Optional[str] = None
    message: Optional[str] = None
    suggested_actions: list[str] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)

    # For errors
    error_code: Optional[str] = None
    suggestions: list[str] = Field(default_factory=list)


class ValidatePlanRequest(BaseModel):
    """Request to validate a plan structure"""
    plan: Plan


class ValidatePlanResponse(BaseModel):
    """Response from plan validation"""
    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# =============================================================================
# Worker API Models
# =============================================================================

class WorkerRegisterResponse(BaseModel):
    """Response from worker registration"""
    worker_id: str
    status: str = "registered"
    registered_at: datetime


class WorkerSearchRequest(BaseModel):
    """Request to search workers"""
    query: str
    trust_tier: Optional[TrustTier] = None
    limit: int = Field(default=10, ge=1, le=100)


class WorkerSearchResponse(BaseModel):
    """Response from worker search"""
    results: list[WorkerSearchResult]
    count: int


class WorkerMatchRequest(BaseModel):
    """Request to match workers to intent"""
    intent: str
    constraints: dict[str, Any] = Field(default_factory=dict)
    trust_policy: Optional[TrustPolicy] = None


class WorkerMatchResponse(BaseModel):
    """Response from worker matching"""
    matches: list[WorkerSearchResult]
    count: int


class WorkerStatusResponse(BaseModel):
    """Response from worker status query"""
    worker_id: str
    worker_name: str
    availability: WorkerAvailabilityInfo
    trust: TrustInfo
    last_seen: Optional[datetime]
