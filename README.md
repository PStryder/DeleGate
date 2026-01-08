# DeleGate

**Pure Planning and Capability Brokering for LegiVellum**

DeleGate is a task delegation framework that decomposes high-level intent into structured execution Plans. It brokers capability between principals (AI agents) and self-describing workers (MCP servers), but DeleGate itself **never executes work**—it only produces Plans.

## Status

**Specification:** v0 (DRAFT)
**Implementation:** Phase 1 MVP (Initial Draft)

See: `SPEC-DG-0000.txt` for complete specification.

## Quick Start

```bash
# Install dependencies
pip install -e ".[dev]"

# Set environment variables
export DELEGATE_DATABASE_URL="postgresql+asyncpg://postgres:postgres@localhost:5432/delegate"
export DELEGATE_MEMORYGATE_URL="http://localhost:8001"
export DELEGATE_ASYNCGATE_URL="http://localhost:8002"

# Run database migrations
alembic upgrade head

# Start the server
python -m delegate.main

# Or run as MCP server
python -m delegate.mcp_server
```

## Project Structure

```
src/delegate/
├── __init__.py      # Package exports
├── models.py        # Pydantic models (Plan, Steps, Workers, Trust)
├── config.py        # Configuration via environment
├── database.py      # PostgreSQL async connection
├── registry.py      # Worker registry with capability matching
├── planner.py       # Plan generation logic
├── receipts.py      # MemoryGate receipt emission
├── api.py           # FastAPI REST endpoints
├── mcp_server.py    # MCP server interface
└── main.py          # Application entry point
```

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

## API Endpoints

- `POST /v1/plan` - Create plan from intent
- `POST /v1/plan/validate` - Validate plan structure
- `GET /v1/plan/{plan_id}` - Get plan by ID
- `POST /v1/workers/register` - Register worker with manifest
- `GET /v1/workers/search` - Semantic capability search
- `POST /v1/workers/match` - Match workers to intent
- `GET /v1/stats` - Registry and planning statistics

## MCP Tools

- `create_delegation_plan` - Create plan from intent
- `analyze_intent` - Analyze intent without creating plan
- `register_worker` - Register worker with capabilities
- `search_workers` - Search workers by capability
- `list_workers` - List all registered workers

## Testing

```bash
pytest tests/ -v
```

## License

Proprietary - Technomancy Labs
