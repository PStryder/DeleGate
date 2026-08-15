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
export DELEGATE_RECEIPTGATE_URL="http://localhost:8003"
export DELEGATE_RECEIPTGATE_API_KEY="dg_your-secret-api-key-here"
export DELEGATE_MEMORYGATE_URL="http://localhost:8001"  # deprecated
export DELEGATE_ASYNCGATE_URL="http://localhost:8002"

# Run database migrations
alembic upgrade head

# Start the server
python -m delegate.main

```

## Project Structure

```
src/delegate/
- __init__.py      # Package exports
- models.py        # Pydantic models (Plan, Steps, Workers, Trust)
- config.py        # Configuration via environment
- database.py      # PostgreSQL async connection
- registry.py      # Worker registry with capability matching
- planner.py       # Plan generation logic
- receipts.py      # ReceiptGate receipt emission
- mcp_http.py      # MCP HTTP JSON-RPC interface
- main.py          # Application entry point
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

## MCP Tools

Names are namespaced `delegate.*` per `mcp.naming.md`. This list is the full
surface reported by `tools/list`.

Planning:
- `delegate.create_delegation_plan` - Create plan from intent
- `delegate.analyze_intent` - Analyze intent without creating a plan
- `delegate.validate_plan` - Validate plan structure
- `delegate.get_plan` - Retrieve a plan by id
- `delegate.list_plans` - List stored plans

Workers:
- `delegate.register_worker` - Register worker with capabilities
- `delegate.search_workers` - Search workers by capability
- `delegate.match_workers` - Match workers to an intent
- `delegate.list_workers` - List all registered workers
- `delegate.worker_status` - Availability for one worker
- `delegate.delete_worker` - Remove a worker from the registry

Operations:
- `delegate.health` - Health check
- `delegate.stats` - Planning and registry counters
- `delegate.cache_clear` - Drop cached worker matches

## MCP HTTP (JSON-RPC)

DeleGate exposes MCP over HTTP at `/mcp` with JSON-RPC methods:
- `tools/list`
- `tools/call`

## Cognition

Decomposing an intent is a cognitive act. DeleGate does not perform it inline:
`DELEGATE_AI_PROVIDER` selects where the decomposition comes from.

| Provider | Behaviour |
|----------|-----------|
| `cognigate` | Ask CogniGate via `cognigate.plan`. The primitive that owns bounded cognition plans under an instruction profile, and returns a plan document; DeleGate still mints the obligations. |
| `openrouter` | Call an OpenAI-compatible endpoint directly. |
| `stub` | Answer locally and deterministically, for tests and CI. No reasoning is performed. |
| `none` | No cognition. Plans come from the heuristic splitter. |

`cognigate.plan` runs CogniGate's planning phase and stops, so asking for a
plan executes nothing. That matters here: DeleGate's invariant is that it never
executes work, and calling `cognigate.execute_job` instead would have CogniGate
performing the work while DeleGate believed it was still deciding what the work
is.

### Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `DELEGATE_AI_PROVIDER` | `stub` | Where cognition comes from (table above) |
| `DELEGATE_COGNIGATE_ENDPOINT` | *(unset)* | CogniGate MCP endpoint. Required when the provider is `cognigate`; startup fails without it rather than failing on the first request. |
| `DELEGATE_COGNIGATE_AUTH_TOKEN` | *(unset)* | Bearer token for CogniGate |
| `DELEGATE_COGNIGATE_PROFILE` | `default` | Instruction profile to plan under |
| `DELEGATE_COGNITION_SCOPES` | `all` | Which classified scopes consult cognition: `all`, `none`, or a subset of `simple,medium,complex` |
| `DELEGATE_PLANNING_FALLBACK` | `escalate` | What to do when cognition is unreachable: `escalate` or `heuristic` |

### When cognition is unavailable

The default is to escalate rather than plan anyway. A heuristic plan produced
by splitting on `" and "` is structurally indistinguishable from a reasoned
one, so substituting it silently would claim thinking that did not happen.
Escalation is a first-class output here — the contract is **Plan OR Escalation
(cannot plan)** — and the response carries reason `resource_unavailable`.

Set `DELEGATE_PLANNING_FALLBACK=heuristic` to degrade quietly instead, which is
appropriate where availability matters more than plan quality.

Neither a disabled provider (`none`) nor a scope outside
`DELEGATE_COGNITION_SCOPES` escalates. Nothing was promised in those cases, so
nothing failed; the heuristic splitter is used directly.

## Configuration

Environment variables (prefix `DELEGATE_`). Generated from the `Settings`
class; MetaGate bootstrap variables are documented in their own section below.

`DELEGATE_API_KEY` is **required** unless `DELEGATE_ALLOW_INSECURE_DEV=true`; startup fails without it.

See `.env.example` for a working starting point.

### Server

| Variable | Default | Description |
|----------|---------|-------------|
| `DELEGATE_DEBUG` | `false` | Enable debug logging |
| `DELEGATE_HOST` | `0.0.0.0` | Server bind address |
| `DELEGATE_INSTANCE_ID` | `delegate-1` | Instance identifier for this DeleGate |
| `DELEGATE_PORT` | `8000` | Server port |
| `DELEGATE_RELOAD` | `false` | Enable hot reload (dev only) |

### Database

| Variable | Default | Description |
|----------|---------|-------------|
| `DELEGATE_DATABASE_URL` | `postgresql+asyncpg://postgres:postgres@localhost:5432/delegate` | PostgreSQL connection URL |
| `DELEGATE_SQL_ECHO` | `false` | Echo SQL queries |

### Authentication

| Variable | Default | Description |
|----------|---------|-------------|
| `DELEGATE_ALLOW_INSECURE_DEV` | `false` | Allow unauthenticated access (dev only) |
| `DELEGATE_API_KEY` | *(empty)* | API key for MCP endpoint authentication |
| `DELEGATE_DEFAULT_TENANT_ID` | `default` | Default tenant ID for unauthenticated requests |

### Upstream services

| Variable | Default | Description |
|----------|---------|-------------|
| `DELEGATE_ASYNCGATE_TENANT_ID` | `00000000-0000-0000-0000-000000000000` | Tenant for minted AsyncGate obligations |
| `DELEGATE_ASYNCGATE_URL` | `http://localhost:8002` | AsyncGate MCP endpoint for async task execution |
| `DELEGATE_COGNIGATE_AUTH_TOKEN` | *(unset)* | Auth token for CogniGate |
| `DELEGATE_COGNIGATE_ENDPOINT` | *(empty)* | CogniGate MCP endpoint |
| `DELEGATE_COGNIGATE_PROFILE` | `default` | Instruction profile to plan under |
| `DELEGATE_MEMORYGATE_URL` | *(empty)* | Deprecated: MemoryGate URL (use receiptgate_url) |
| `DELEGATE_RECEIPTGATE_API_KEY` | *(empty)* | ReceiptGate API key for receipt emission |
| `DELEGATE_RECEIPTGATE_URL` | `http://localhost:8003` | ReceiptGate MCP endpoint for receipt emission |

### AI and cognition

| Variable | Default | Description |
|----------|---------|-------------|
| `DELEGATE_AI_API_KEY` | *(unset)* | Provider API key |
| `DELEGATE_AI_ENDPOINT` | `https://openrouter.ai/api/v1` | OpenAI-compatible endpoint |
| `DELEGATE_AI_MAX_TOKENS` | `2048` | Max tokens for a plan |
| `DELEGATE_AI_MODEL` | `anthropic/claude-sonnet-4-20250514` | Planning model |
| `DELEGATE_AI_PROVIDER` | `stub` | Planning provider: stub | openrouter | none |
| `DELEGATE_AI_TIMEOUT_SECONDS` | `30.0` | Planning request timeout |
| `DELEGATE_COGNITION_SCOPES` | `all` | Scopes that use cognition: all | none | subset of simple,medium,complex |
| `DELEGATE_PLANNING_FALLBACK` | `escalate` | When cognition is unavailable: escalate | heuristic |

### Rate limiting

| Variable | Default | Description |
|----------|---------|-------------|
| `DELEGATE_RATE_LIMIT_ENABLED` | `true` | Enable rate limiting |
| `DELEGATE_RATE_LIMIT_REQUESTS_PER_MINUTE` | `200` | Rate limit per minute |

### CORS

| Variable | Default | Description |
|----------|---------|-------------|
| `DELEGATE_CORS_ALLOW_CREDENTIALS` | `true` | Allow credentials in CORS requests |
| `DELEGATE_CORS_ALLOWED_HEADERS` | `['Authorization', 'Content-Type', 'X-Tenant-ID']` | Allowed request headers |
| `DELEGATE_CORS_ALLOWED_METHODS` | `['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS']` | Allowed HTTP methods |
| `DELEGATE_CORS_ALLOWED_ORIGINS` | `['http://localhost:3000', 'http://localhost:8080']` | Allowed CORS origins (explicit allowlist for security) |

### Behaviour and limits

| Variable | Default | Description |
|----------|---------|-------------|
| `DELEGATE_ALLOW_UNTRUSTED_WORKERS` | `false` | Enable untrusted worker registration |
| `DELEGATE_CAPABILITY_CACHE_TTL_SECONDS` | `600` | Worker capability cache TTL |
| `DELEGATE_DEFAULT_TRUST_TIER` | `verified` | Minimum trust tier if not specified (untrusted, sandbox, verified, trusted) |
| `DELEGATE_ENABLE_METRICS` | `false` | Enable Prometheus metrics |
| `DELEGATE_MAX_PLAN_STEPS` | `20` | Maximum steps per Plan |
| `DELEGATE_METRICS_PORT` | `9090` | Prometheus metrics port |
| `DELEGATE_PLANNING_TIMEOUT_SECONDS` | `30` | Max time for Plan generation |
| `DELEGATE_REQUIRE_SIGNATURES_PRODUCTION` | `false` | Require cryptographic signatures in production |
| `DELEGATE_WORKER_HEALTH_CHECK_INTERVAL_SECONDS` | `60` | Interval for worker health checks |

## Testing

```bash
pytest tests/ -v
```

## MetaGate Bootstrap

On startup this gate asks MetaGate for the topology it belongs to and fills in
endpoints the operator did not configure. It resolves: `receiptgate` → `receiptgate_url`, `memorygate` → `memorygate_url`, `asyncgate` → `asyncgate_url`.

| Variable | Default | Meaning |
|----------|---------|---------|
| `DELEGATE_METAGATE_ENDPOINT` | *(unset)* | MetaGate MCP endpoint. Unset disables bootstrap; the gate starts on configured values alone. |
| `DELEGATE_METAGATE_API_KEY` | *(unset)* | Credential presented to MetaGate |
| `DELEGATE_METAGATE_COMPONENT_KEY` | `delegate` | Which component in the manifest this process is |
| `DELEGATE_METAGATE_BOOTSTRAP_TIMEOUT_SECONDS` | `5.0` | Per-call timeout |

Bootstrap never prevents startup. Every failure — unreachable, timeout, auth
rejected, no binding, malformed packet — degrades to a logged warning and
"carry on with configured values", because a bootstrap authority that can take
the mesh down would be a hidden master. Explicit configuration always wins;
bootstrap fills gaps and logs when the mesh disagrees rather than overriding.

See `LegiVellum/docs/canonical/metagate.bootstrap.md` for the full contract.

## License

Proprietary - Technomancy Labs
