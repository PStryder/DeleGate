# DeleGate

**Pure Planning and Capability Brokering for LegiVellum**

DeleGate is a task delegation framework that decomposes high-level intent into structured execution Plans. It brokers capability between principals (AI agents) and self-describing workers (MCP servers), but DeleGate itself **never executes work**—it only produces Plans.

## Status

**Specification:** v0 (DRAFT)  
**Implementation:** Not yet started

See: `SPEC-DG-0000.txt` for complete specification.

## Core Doctrine

**CRITICAL INVARIANT:** If output is not a valid Plan, DeleGate has failed.

DeleGate is a pure planner:
- **Input:** Intent (natural language or structured) + optional context
- **Output:** Plan (structured, validated) OR Escalation (cannot plan)
- **Never:** Executes work, tracks progress, retries, or makes decisions for principals

## Plan Structure

Plans consist of three sections:
1. **Metadata** - plan_id, confidence, scope, trust policy
2. **Steps** - Five step types: call_worker, queue_execution, wait_for, aggregate, escalate
3. **References** - Input sources (MemoryGate) and expected outputs (AsyncGate)

## Worker Registry

DeleGate maintains a live registry of available workers through MCP introspection:
- Workers self-register with tool manifests
- Semantic capability matching
- Trust tier validation (trusted, verified, sandbox, untrusted)
- Performance hints (latency, cost, availability)

## Fractal Composition

DeleGates can delegate to other DeleGates using the same MCP contract.

**Essential invariant:** All DeleGate-to-DeleGate communication uses the PUBLIC MCP interface—no special internal protocols.

This enables arbitrary nesting (General → Department → Team → Specialist) without tight coupling.

## Integration Points

- **AsyncGate:** Used for async step execution via queue_execution steps
- **MemoryGate:** Context retrieval, plan templates, worker registry persistence
- **InterroGate:** Optional admission control for recursion limits and policy checks

## Five Step Types

1. **call_worker** - Direct synchronous execution
2. **queue_execution** - Async execution via AsyncGate  
3. **wait_for** - Block until receipts/tasks complete
4. **aggregate** - Request synthesis by principal
5. **escalate** - Cannot proceed, deliver report and request decision

## Trust Model

**Trust is NOT transitive.** Principal trusting DeleGate ≠ auto-trusting Workers.

Trust tiers:
- **Trusted** (tier 3): Signed by root authority, full access
- **Verified** (tier 2): Code audit, organization-approved
- **Sandbox** (tier 1): Isolated execution, limited resources
- **Untrusted** (tier 0): Manual approval, full audit

## API Endpoints (Planned)

- `POST /v1/plan` - Create plan from intent
- `POST /v1/plan/validate` - Validate plan structure
- `POST /v1/workers/register` - Register worker with manifest
- `GET /v1/workers/search` - Semantic capability search
- `POST /v1/workers/match` - Match workers to intent

## Non-Goals (Hard Prohibitions)

DeleGate MUST NOT:
- Execute any work directly
- Track Plan execution progress
- Mutate Plans after creation
- Make decisions for principals
- Store execution state between requests

## Implementation Phases

1. **MVP** - Core planning with 5 step types, basic worker registry
2. **Worker Discovery** - MCP introspection, semantic search, trust validation
3. **Advanced Planning** - Plan templates, cost optimization, dependency resolution
4. **Production Hardening** - Signatures, sandboxing, audit trail

## License

Proprietary - Technomancy Labs
