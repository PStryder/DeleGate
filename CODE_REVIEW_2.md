<!-- Generated 2026-08-15. Stack-level context: ../LV_STACK_REVIEW.md -->

> **Review 2 — DeleGate**
> Part of a full-stack review of LV_Stack (11 repos, ~97k LOC) conducted 2026-08-15.
> Stack-wide findings that affect this repo but are not fixable inside it are in
> `../LV_STACK_REVIEW.md` and `../_CROSS_REPO_ANALYSIS.md`. Read the stack report first —
> several findings below have a shared root cause.

---

# DeleGate — Code Review

Reviewed: `/home/claude/lv/DeleGate/` @ ~6.5k LOC src + tests. Reviewer pass 2 (delta vs `CODE_REVIEW_1.md`, 2026-01-08).

## Verdict

DeleGate is the authority boundary of the whole system and it does not implement authority at all. There is no caller principal anywhere in the codebase — no `SYSTEM_PRINCIPAL_ID`, no `SERVICE_PRINCIPAL_ID`, no way for a caller to assert who they are — so every obligation it mints is owned by the literal configuration constant `settings.default_tenant_id` (`"default"`), and every planning receipt is addressed to the string `"delegate"`. The plan→work provenance link that the dispatcher's docstring promises does not exist in the ledger: `caused_by_receipt_id` is written into an opaque AsyncGate task *payload*, while AsyncGate independently populates the real field from its own obligation. On top of that, an escalated or failed plan leaves its `accepted` obligation permanently open in ReceiptGate, because the escalation receipt is emitted under a different `task_id`. **Not v1-taggable.** Sections 3, 4, 6, 7, 9, 10 of the Exit Criteria are FAIL, and section 3 is the one this repo exists to satisfy.

## Exit Criteria Scorecard

| § | Section | Score | Justification |
|---|---------|-------|---------------|
| 1 | Build & Run | **FAIL** | No `run_local.sh`/`Makefile` (siblings have one), no HTTP `/health` despite `SPEC-DG-0000.txt:640` specifying `GET /health`, the `delegate-mcp` console script raises `AttributeError` on import, and `.env.example` documents a variable (`DELEGATE_CORS_ORIGINS`) that is not a setting. Docker build itself is fine. |
| 2 | API & Contract Stability | **PARTIAL** | Tool names are canonically namespaced and alias-tested, but every entry in `MCP_TOOLS` declares `"inputSchema": {"type":"object","properties":{}}` — the advertised contract is empty — and error codes mix JSON-RPC ints, `"AUTH_FAILED"`, and `"ERROR"`. |
| 3 | Canonical Principals | **FAIL** | Neither constant is defined anywhere in the repo (AsyncGate has `src/asyncgate/principals.py`; DeleGate has no equivalent). Internal vs external origin is never branched. Ownership is a tenant-id constant. |
| 4 | Receipt Model Invariants | **FAIL** | No `TERMINAL_RECEIPT_TYPES` set in this repo; escalation and failure leave the planning obligation open forever; provenance link absent from the ledger; `dedupe_key` hardcoded `"NA"` on every receipt. |
| 5 | Persistence & Migration | **PARTIAL** | Single head, clean from empty DB, unique `(tenant_id, plan_id)` index. But `workers`/`capability_index` tables are unused dead schema, `status` and `updated_at` are never written, and no constraint encodes any stated invariant. |
| 6 | Core Behavioral Guarantees | **FAIL** | No golden-path script. Receipt emission blocks the request path for up to ~66 s worst case with no overall timeout. Partial dispatch leaves state that cannot be reconciled — minted task IDs are never persisted. |
| 7 | Test Requirements | **FAIL** | 9 test files, none exercising `mcp_http.py`, auth, receipts, persistence, or the mint path end-to-end. `tests/conftest.py:10` globally sets `DELEGATE_ALLOW_INSECURE_DEV=true`, so no auth test could be meaningful. Coverage gate is 40%. |
| 8 | Observability | **PARTIAL** | `plan_id`/`receipt_id` appear in some log lines; `list_plans`/`get_plan`/`stats` exist. But two `except Exception: pass` blocks hide receipt failures, and `enable_metrics` is a flag with no implementation. |
| 9 | v1 Lock Rules | **FAIL** | You cannot freeze principal conventions and ownership rules that do not exist, and the repo's own SPEC contradicts the implementation on the authority question. |
| 10 | Open Issues / Deferred | **FAIL** | No `V1_EXIT_CRITERIA.md` in this repo, unlike its siblings. Nothing is explicitly deferred, so every gap above reads as an oversight rather than a decision. |

## Authority Enforcement Audit

| Mint path | file:line | owner derived from? | guarded? |
|---|---|---|---|
| `asyncgate.create_task` per plan step | `src/delegate/mcp_http.py:387-389` → `src/delegate/dispatcher.py:98-99` | `settings.default_tenant_id` — a config constant. The `request.intent.principal_id` branch is dead: `IntentInput` (`models.py:483-493`) has no such field. | Shared API key only. No per-principal authz, no caller identity. |
| `accepted` planning obligation in ReceiptGate | `src/delegate/receipts.py:144-147` | Hardcoded `"delegate"` for `from_principal`, `for_principal`, `recipient_ai`. | Same shared API key. |
| `escalate` receipt | `src/delegate/receipts.py:234-237` | Hardcoded `"delegate"` / `"principal"` (a literal placeholder, not a principal id). | Same shared API key. |
| `mcp_server.py` stdio `create_delegation_plan` | `src/delegate/mcp_server.py:114-143` | Same hardcoded constants; never dispatches, never persists. | Process-level only — but the module does not import (see H4). |
| Child-obligation inheritance | — | **Not implemented.** No parent/child obligation relationship exists in DeleGate; every step is a flat sibling. | N/A |
| Housekeeping rehome | — | **Not implemented.** No rehome path exists, therefore also unguarded-by-absence. | N/A |

Bottom line: **`owner_principal_id` is neither derived from the authenticated principal nor taken from the request body — it is a constant.** That is not the classic client-spoofing CRITICAL, it is worse for an accountability substrate: no obligation minted by DeleGate is attributable to anyone.

---

## Critical & High Findings

### C1 — CRITICAL — Every minted obligation is owned by a configuration constant; caller principal does not exist
`src/delegate/mcp_http.py:387-389`
```python
principal_id=request.intent.principal_id
if getattr(request.intent, "principal_id", None)
else settings.default_tenant_id,
```
`IntentInput` (`src/delegate/models.py:483-493`) declares only `type`, `content`, `urgency`. The `getattr` guard is therefore always `None` and the fallback always fires. `default_tenant_id` defaults to `"default"` (`config.py:141-144`). That value is sent to AsyncGate as both `principal_ai` and `agent_id` (`dispatcher.py:98-99`), and AsyncGate stores it verbatim as the obligation owner (`AsyncGate/src/asyncgate/mcp/server.py:76`, `engine/core.py:225-260`).

**Failure scenario:** Principals A and B each hold the DeleGate API key. A submits `delegate.create_delegation_plan` with intent "delete the Q3 archive"; B submits "generate the Q3 report". Both produce AsyncGate obligations with `principal_ai="default"`. Nothing in the ledger, the inbox query, or the task table distinguishes them. When the destructive task completes, no receipt chain identifies A. Attacker capability required: possession of the single shared API key — i.e. any legitimate integration is also a full impersonation of every other one.

**Also:** Exit Criteria §3 requires `SYSTEM_PRINCIPAL_ID = "sys:legivellum"` and `SERVICE_PRINCIPAL_ID = "svc:delegate"` as constants in code. `grep -rn "SYSTEM_PRINCIPAL\|SERVICE_PRINCIPAL" DeleGate/` returns nothing. AsyncGate ships `src/asyncgate/principals.py` with exactly these. DeleGate — the repo whose defining invariant is authority — has no principals module at all.

### C2 — CRITICAL — Single shared API key is the entire authorization model; "admin" tools are not privileged
`src/delegate/auth.py:56-67`, `src/delegate/mcp_http.py:599-603`
```python
auth_token = _extract_auth_token(arguments, request)
try:
    validate_api_key_value(auth_token)
```
`validate_api_key_value` does one `secrets.compare_digest` against `settings.api_key` and returns `True`. There is no principal, role, scope, or tenant attached to the result. Every tool — `create_delegation_plan`, `register_worker`, `delete_worker` (README: "Remove a worker"), `cache_clear` (README: "(admin)"), `get_plan`, `list_plans` — is behind that one boolean.

**Failure scenario:** A read-only dashboard integration is given the API key to call `delegate.list_plans`. That same key calls `delegate.delete_worker` and removes every registered worker, after which every subsequent plan escalates `NO_CAPABLE_WORKERS` (registry is in-memory, `registry.py:38-43`, so there is no recovery short of re-registration). Attacker capability: any key holder.

`CODE_REVIEW_1.md` CRIT-001 (no authn) is fixed; CRIT-002 (admin endpoints unprotected) is **still open** — authentication was added, authorization was not.

### C3 — CRITICAL — Escalated or failed plans leave a permanently open obligation in the canonical ledger
`src/delegate/mcp_http.py:277-287` (accepted), `396-406` (escalate), `src/delegate/receipts.py:229`

The `accepted` receipt is emitted with `task_id = resolved_plan_id` (`receipts.py:139`). The escalation receipt is emitted with a *different* task id:
```python
"task_id": f"escalation-{receipt_id}",
...
"caused_by_receipt_id": "NA",
```
ReceiptGate closes obligations by matching `task_id` against a terminal phase (`ReceiptGate/src/receiptgate/ledger_v1.py:148-165`: `WHERE phase='accepted' AND NOT EXISTS (SELECT 1 ... WHERE t.task_id = r.task_id AND t.phase IN :terminal_phases)`). Because the two receipts carry different `task_id`s, the accepted obligation is never satisfied.

Three paths produce the same leak:
1. `requires_escalation` → escalation receipt under a foreign `task_id`.
2. `planning_failed` → **no receipt is emitted at all** (`mcp_http.py:396` is an `elif`; there is no `else`).
3. `plan_stored == False` (DB insert failed, `mcp_http.py:330-335`) → the `complete` receipt is skipped entirely by the `if plan_stored:` guard at line 337.

**Failure scenario:** CogniGate is down and `DELEGATE_PLANNING_FALLBACK=escalate` (the documented default). Every plan request emits an `accepted` obligation and then an escalation under an unrelated task id. After an hour of traffic, DeleGate's inbox holds thousands of open planning obligations that no receipt can ever close, and the derived-state invariant ("inbox state is derived by query") reports the system as permanently overdue.

### H1 — HIGH — `caused_by_receipt_id` never reaches the ledger; plan→work provenance is decorative
`src/delegate/dispatcher.py:143-153`
```python
payload = {
    ...
    # The provenance link: this obligation exists because that
    # plan receipt said so.
    "caused_by_receipt_id": plan_receipt_id,
```
This is a key inside the free-form `payload` object. `asyncgate.create_task` has no `caused_by_receipt_id` parameter (`AsyncGate/src/asyncgate/mcp/server.py:69-88` — the accepted properties are `type`, `payload`, `payload_pointer`, `principal_ai`, `requirements`, `expected_outcome_kind`, `expected_artifact_mime`, `priority`, `idempotency_key`, `max_attempts`, `retry_backoff_seconds`, `delay_seconds`, `agent_id`, `tenant_id`). AsyncGate sets the real field from its own obligation receipt: `AsyncGate/src/asyncgate/engine/core.py:551` — `caused_by_receipt_id = str(obligation.receipt_id) if obligation else "NA"`.

**Failure scenario:** An auditor asks "why does obligation T exist?" and traverses `caused_by_receipt_id` from T's receipts. The chain terminates at AsyncGate's own accepted receipt and stops — the DeleGate plan receipt is not reachable by traversal, only by JSON-grepping task payload blobs. Invariant 4 (Provenance: "receipts form complete causality chains") is unsatisfied for every obligation DeleGate mints. `tests/test_dispatcher.py:52-68` asserts the payload key is present and therefore *certifies the broken behaviour as correct*.

### H2 — HIGH — No idempotency at any layer; double-submit mints duplicate obligations
`src/delegate/mcp_http.py:275` (`plan_id = generate_plan_id()`), `src/delegate/dispatcher.py:95-102`, `src/delegate/receipts.py:142` (`"dedupe_key": "NA"`)

- The plan id is minted fresh per request, so the unique index `ix_plans_tenant_plan` (`migrations/versions/001_initial_schema.py:40`) never collides.
- `asyncgate.create_task` accepts an `idempotency_key`; DeleGate never sends one.
- Every receipt sets `dedupe_key: "NA"`, disabling ledger-side dedupe.
- No `SELECT ... FOR UPDATE`, no unique constraint on `(tenant_id, intent_hash)`.

**Failure scenario:** A client with a 30 s timeout submits a plan. Receipt emission alone can consume ~33 s (F-M13), so the client times out and retries. Both requests complete: two plan rows, two `accepted` obligations, two `complete` receipts, and 2×N AsyncGate obligations for the same intent — meaning the work is executed twice. If the intent is "issue the refund", the refund is issued twice.

### H3 — HIGH — Dispatcher never authenticates to AsyncGate; the setting it reads does not exist
`src/delegate/dispatcher.py:181-187`
```python
return AsyncGateDispatcher(
    getattr(settings, "asyncgate_url", None),
    api_key=getattr(settings, "asyncgate_api_key", None),
```
`Settings` (`src/delegate/config.py:12-199`) has `receiptgate_api_key`, `cognigate_auth_token`, `metagate_api_key`, `ai_api_key` — but **no `asyncgate_api_key`**. The `getattr` default silently yields `None`, so `dispatcher.py:93-94` never adds the `Authorization` header. The test fake declares the attribute (`tests/test_dispatcher.py:16: asyncgate_api_key = None`), which is why nothing notices.

**Failure scenario:** In any deployment where AsyncGate enforces auth, every `create_task` returns 401. `_create_task` raises, the per-step `except Exception` at `dispatcher.py:161` swallows it into `result.failed`, and the tool returns `status: "plan_created"` with a `dispatch` summary the caller is not required to read. The plan is stored, both receipts are emitted, and **zero obligations exist** — DeleGate reports success while having minted nothing. There is also no way to configure the credential even if you notice.

### H4 — HIGH — `delegate.mcp_server` does not import; the `delegate-mcp` entry point is dead
`src/delegate/mcp_server.py:41,48` and `pyproject.toml` (`delegate-mcp = "delegate.mcp_server:main"`)
```python
mcp = Server("delegate")
...
@mcp.tool()
async def create_delegation_plan(...)
```
`mcp.server.Server` has no `tool` attribute — that decorator belongs to `FastMCP`. Verified against the installed SDK:
```
$ python -c "from mcp.server import Server; s=Server('x'); s.tool()"
AttributeError: 'Server' object has no attribute 'tool'
```
`uv.lock` resolves `mcp==2.0.0`; the pin is `mcp>=1.0.0` (unbounded).

CI does not catch this: `compileall` is syntax-only, ruff runs `--select E9,F63,F7,F82`, and `.mypy-ci.ini` explicitly lists `attr-defined` in `disable_error_code` — precisely the check that would flag it.

**Failure scenario:** An operator follows `LegiVellum/docs/canonical/DeleGate/README.md:41` (`python -m delegate.mcp_server`) or installs the package and runs `delegate-mcp`. Immediate `AttributeError` at import, before any logging is configured. 417 lines of documented, packaged, entry-pointed code have never run.

### H5 — HIGH — Workers self-certify their trust tier; `verified_tier` is set equal to `declared_tier`
`src/delegate/registry.py:333-337`
```python
if manifest.trust.verified_tier is None:
    # In Phase 1, we accept declared tier but mark verification as unknown
    manifest.trust.verified_tier = manifest.trust.declared_tier
    manifest.trust.verification_status = VerificationStatus.UNKNOWN
```
`SPEC-DG-0000.txt:482` states "Principals MUST NOT assume declared_tier == verified_tier". The code makes them equal. The plan-level trust check (`models.py:443-449`) compares `step.trust.verified_tier < min_tier` — which now always passes — and the `verification_status=UNKNOWN` flag that would betray it is never consulted by any gate.

**Failure scenario:** An API-key holder calls `delegate.register_worker` with `trust: {declared_tier: 3}` (TRUSTED). The registry promotes it to `verified_tier=3`. Its capabilities index against common keywords, so `registry.search` ranks it first (`registry.py:151-163`), and `_create_simple_plan` picks `workers[0]` unconditionally (`planner.py:311`). Every subsequent plan routes to the attacker's worker, and the plan's own `trust_policy.minimum_worker_tier=VERIFIED` check certifies it as verified. "Trust is NOT transitive" (README:100) is asserted in prose and inverted in code.

### H6 — HIGH — Plan step count is unbounded; delegation-bomb fan-out
`src/delegate/mcp_http.py:245,266`, `src/delegate/planner.py:493-527`, `src/delegate/planner.py:785`

`max_steps` is threaded into `PlanningOptions` and then **never read by the planner**. `grep -rn "max_steps" src/` shows the only consumer is `validate_plan()` at `planner.py:785`, which is reachable only via the separate `delegate.validate_plan` tool and is never called on the create path. `_create_complex_plan` iterates over whatever `_ai_subtasks` returns; `AIPlanner._normalize` (`ai_planner.py:114-140`) imposes no length cap.

**Failure scenario:** A caller submits an intent containing 5,000 comma-separated clauses. `_split_into_subtasks` (`planner.py:653-665`) splits on `,\s*| and ` and returns 5,000 subtasks. For each, `_create_complex_plan` calls `registry.match_intent` (an O(workers × capabilities) scan) and appends a step. The resulting plan has ~5,002 steps, is stored as a single JSONB blob, and dispatches ~5,000 sequential `create_task` calls inside one HTTP request — each an obligation someone now owes. `DELEGATE_MAX_PLAN_STEPS=20` is documented in the README and enforces nothing. `CODE_REVIEW_1.md` MED-001 (intent not length-limited) is **still open**: `models.py:489` has no `max_length`.

### H7 — HIGH — The repo's own SPEC forbids what the implementation does
`SPEC-DG-0000.txt:526`
```
DeleGate NEVER calls AsyncGate directly—only Principals execute Plans.
```
vs. `src/delegate/dispatcher.py:104-121`, which POSTs `asyncgate.create_task` from inside the request handler. `LegiVellum/docs/canonical/DeleGate/alignment.md:5` says the opposite ("Only component (besides Principals) that may mint obligations"), and `README.md:64` argues the dispatcher's case. The conflict is unresolved in the repo that owns the authority boundary.

Exit Criteria §9 freezes "Principal conventions and ownership rules" at v1. Two normative documents shipped in this repo disagree about whether the single most consequential code path is permitted. That is not taggable regardless of which one is right.

---

## Medium Findings

### M1 — Tenant isolation is a placeholder; any key holder reads every plan
`src/delegate/mcp_http.py:430,441,562`. Every query binds `settings.default_tenant_id`. `X-Tenant-ID` is in the CORS allowlist (`config.py:126`) but is never read anywhere. `delegate.stats` (`mcp_http.py:554-562`) has no tenant filter at all. `CODE_REVIEW_1.md` MED-002 **still open**. Failure: a two-tenant deployment shares one plan table partition; tenant A's `list_plans` returns tenant B's intents verbatim.

### M2 — Bare `except Exception: pass` on both receipt paths
`src/delegate/mcp_http.py:286-287` and `405-406`:
```python
except Exception:
    pass
```
The `accepted`-receipt failure is completely silent — no log line, no counter. Planning then proceeds and the `complete` receipt is emitted with `caused_by_receipt_id="NA"` (`mcp_http.py:347`), fabricating an orphaned completion. The escalation-receipt failure is equally silent. `CODE_REVIEW_1.md` §4.2 "Swallowed Exceptions" was **fixed for plan persistence** (`mcp_http.py:334` now logs) and **regressed/never fixed** for receipts.

### M3 — Raw exception text returned to callers
`src/delegate/mcp_http.py:609-610`: `return _jsonrpc_error(request_body.id, getattr(exc, "code", "ERROR"), str(exc))`. Any `SQLAlchemyError` surfaces the failing SQL, table names, and connection details to an unauthenticated-at-transport client. `CODE_REVIEW_1.md` LOW "Error Message Leakage" **still open**.

### M4 — Unbounded input on multiple axes
- `models.py:489` — `content: str` with no `max_length`.
- `mcp_http.py:438` — `limit = int(arguments.get("limit", 20))` with no upper bound; `limit=10_000_000` streams the whole table. Non-numeric input raises `ValueError`, caught by M3 and echoed.
- `mcp_http.py:245` — `int(arguments.get("max_steps") or ...)` then `PlanningOptions(max_steps=...)` which enforces `le=100`; a value of 500 raises a pydantic `ValidationError` returned as raw text.
- `mcp_http.py:483` — `WorkerManifest(**arguments)` with no cap on the capabilities list.

### M5 — `planning_timeout_seconds` documented, configured, never enforced
`config.py:69-74` defines it; `grep -rn planning_timeout src/` finds only the definition and a `bootstrap` echo at `mcp_server.py:376`. `planner.py` imports `asyncio` (line 7) and never uses it. `CODE_REVIEW_1.md` MED-005 **still open** after 7 months. Failure: `_create_complex_plan` issues one `registry.match_intent` per subtask with no deadline; a large intent pins a worker thread indefinitely.

### M6 — Dispatch outcome is not persisted; partial dispatch is unrecoverable
`src/delegate/mcp_http.py:394` assigns `dispatch_summary` to the response only. The `plans` table has no `dispatched_task_ids` column, `status` stays `'created'` forever (`mcp_http.py:325`), and `updated_at` is never written. Failure: 5 of 7 steps mint successfully, 2 fail (H3 makes this the normal case). The plan row is indistinguishable from a fully-dispatched one. Retrying the whole plan re-mints all 7 (H2). There is no query that answers "which obligations belong to plan X".

### M7 — Registry is in-memory while its tables sit unused
`registry.py:38-43` vs `migrations/versions/001_initial_schema.py:45-85` (`workers`, `capability_index`). `CODE_REVIEW_1.md` HIGH-001 and LOW-005 **both still open**. Failure: a rolling restart or a second replica behind a load balancer sees an empty registry; `planner.create_plan` returns `NO_CAPABLE_WORKERS` escalations for every request until every worker re-registers — and each of those escalations leaks an open obligation via C3.

### M8 — `tools/list` served before authentication
`src/delegate/mcp_http.py:587-588` returns the full tool catalogue before `validate_api_key_value` is reached at line 601. Low value given the schemas are empty, but it is an unauthenticated surface on a service whose whole point is gated authority.

### M9 — No HTTP health endpoint; Docker HEALTHCHECK is fragile
The only route in the app is `@router.post("")` on `/mcp` (`mcp_http.py:579`). `SPEC-DG-0000.txt:640` specifies `GET /health`. The Dockerfile HEALTHCHECK POSTs `delegate.health` and adds `Authorization` only if `DELEGATE_API_KEY` is set *in the container env* — in a deployment that injects the key by another name or via a secret file, the healthcheck gets a 401 whose body has no `result`, the assert fails, and the container is marked unhealthy while serving correctly.

### M10 — `mcp_server.py` is a divergent second implementation of the mint path
Beyond H4 (it doesn't import), `mcp_server.py:114-143` emits both `accepted` and `complete` receipts with `artifact_pointer=f"delegate://plans/{plan_id}"` — for a plan it never inserts into the `plans` table and never dispatches. If it were fixed to import, it would immediately produce completion receipts pointing at nonexistent artifacts. It should be deleted or rewritten to delegate to `mcp_http._handle_tool`.

### M11 — Naive `datetime.utcnow()` throughout
8 occurrences (`mcp_http.py:276,294`, `receipts.py:375`, `registry.py:62,220`, `models.py:348`, `mcp_server.py:110,128`). Written into `TIMESTAMP WITH TIME ZONE` columns (`001_initial_schema.py:34`) and serialized via `.isoformat()` into receipt `created_at`/`completed_at` with no offset. Postgres interprets the naive value as the server's timezone. `CODE_REVIEW_1.md` MED-004 **still open**.

### M12 — Receipt retry queue silently drops receipts
`receipts.py:41`: `_retry_queue: deque = deque(maxlen=1000)`. On overflow the oldest entry is discarded with no log. After 10 failed retries the receipt is logged and dropped (`receipts.py:461-465`). Process restart loses the queue entirely. Failure: ReceiptGate is down for 20 minutes under load; receipts past the 1000th are destroyed, permanently breaking the append-only chain for those plans. `CODE_REVIEW_1.md` MED-006 **still open**.

### M13 — Receipt emission blocks the request path for up to ~66 s
`receipts.py:284-366`: `max_retries=3`, `timeout=10.0` per attempt, plus `asyncio.sleep(2**attempt)` backoff = up to 10+1+10+2+10 ≈ 33 s per call. `create_delegation_plan` makes two such calls (accepted + complete). No `asyncio.wait_for` wraps the handler. Failure: ReceiptGate degrades to slow-but-not-down; every plan request takes a minute, clients time out and retry (feeding H2), and the rate limiter's 200/min window fills with in-flight requests.

---

## Low / Nits

- **L1** `middleware/rate_limit.py:50` keys on `request.client.host`. Behind any reverse proxy every caller shares one bucket (200/min total). `get_rate_limiter` (line 68) memoizes the first call's parameters, so config changes after first request are ignored.
- **L2** `mcp_http.py:136` — `token = arguments.pop("auth_token", None)` accepts the API key inside the JSON-RPC request body, where proxies and access logs capture it.
- **L3** `.env.example` sets `DELEGATE_CORS_ORIGINS`, which is not a `Settings` field (the field is `cors_allowed_origins`). It also omits every AI/cognition/MetaGate variable the README documents, so copying it produces `ai_provider=stub` silently.
- **L4** `LegiVellum/docs/canonical/DeleGate/alignment.md:11` requires "Store plans in DepotGate and reference by pointer". Plans go to local Postgres with a fabricated `delegate://plans/{id}` pointer (`mcp_http.py:348`), and there is no DepotGate setting in `config.py`. The `artifact_pointer` in every `complete` receipt is unresolvable by any other component.
- **L5** `receipts.py:144-147` — `from_principal`/`for_principal`/`source_system`/`recipient_ai` are all the bare string `"delegate"`, not `svc:delegate`. `receipts.py:237,261` use `"principal"` as a literal escalation target.
- **L6** `plans.status` and `plans.updated_at` are declared and never written; `delegate.list_plans` exposes a `status` filter that can only ever match `'created'`.
- **L7** `.gitignore` lists `uv.lock` while `uv.lock` is present in the tree; `.coverage` is committed.
- **L8** `MCP_TOOLS` inputSchemas are all `{"type":"object","properties":{}}` — a conformant MCP client cannot construct a valid call from `tools/list`.
- **L9 (NIT)** `mcp_http.py:459` and `mcp_server.py:181` do a function-local `from delegate.planner import ...` inside the handler on every call.

---

## Test Coverage Gaps

9 test files, ~1,300 lines. What they cover: model invariants (DAG, unique step ids, step-type requirements), registry CRUD and search, intent classification, MCP tool naming, AI-planner normalization, dispatcher partial-failure semantics. What they do not cover, in priority order:

1. **Authority.** No test asserts who owns a minted obligation. `tests/test_dispatcher.py` passes `principal_id="p"` as a literal, so the fact that the production value is a config constant (C1) is untestable by construction. **Missing regression: "an obligation minted for caller X is owned by X, not by a constant."**
2. **Auth.** `tests/conftest.py:10` sets `DELEGATE_ALLOW_INSECURE_DEV=true` for the whole session, which makes `validate_api_key_value` return `True` unconditionally (`auth.py:26-27`). Any auth test written under this conftest would pass vacuously. **Missing regressions: "a request with no key is rejected 401"; "a request with a wrong key is rejected"; "`delegate.delete_worker` requires more than a read key."**
3. **`mcp_http.py` — 610 lines, zero tests.** The entire mint path, plan persistence, receipt sequencing, and dispatch wiring are unexercised. Every one of C1/C3/H2/H6 lives here.
4. **`receipts.py` — 480 lines, zero tests.** **Missing regressions: "escalation closes the planning obligation" (would catch C3); "receipt fields validate against `receipt.schema.v1.json`"; "retry exhaustion is observable."**
5. **Idempotency.** **Missing regression: "submitting the same plan request twice produces one set of obligations."** Would catch H2.
6. **Provenance.** `test_dispatcher.py:52-68` asserts the payload key exists, which certifies the broken behaviour (H1). **Missing regression: the AsyncGate obligation's ledger-level `caused_by_receipt_id` resolves to the DeleGate plan receipt.**
7. **Fan-out bounds.** **Missing regression: "a plan never exceeds `max_plan_steps` steps."** Would catch H6.
8. **Trust.** `test_registry.py` covers registration but not the promotion at `registry.py:334`. **Missing regression: "a worker declaring TRUSTED without a signature does not satisfy a `minimum_worker_tier=TRUSTED` policy."**
9. **Import smoke test.** A single `import delegate.mcp_server` in the suite would have caught H4 immediately.
10. **Migrations.** No test runs `alembic upgrade head` against an empty DB.
11. `tests/test_dispatcher.py:13-17` — the `_Settings` fake declares `asyncgate_api_key`, actively masking H3. Fakes of `Settings` should be built from the real class.

Coverage gate is `--cov-fail-under=40` (`.github/workflows/ci.yml`), which is satisfied by the model/registry tests alone.

---

## Delta vs `CODE_REVIEW_1.md`

**Fixed:**
- CRIT-001 (no authentication) — API key auth added (`auth.py`, `mcp_http.py:599`).
- HIGH-002 (hardcoded `dev-key-{tenant_id}`) — now `receiptgate_api_key` from config, and `_receiptgate_headers` raises rather than defaulting (`receipts.py:50-54`).
- HIGH-003 (CORS wildcard) — default is now an explicit localhost allowlist (`config.py:113-116`).
- MED-003 (plan storage error silently swallowed) — now logged and rolled back (`mcp_http.py:330-335`), and the `complete` receipt is correctly gated on `plan_stored`.
- No rate limiting → `middleware/rate_limit.py` added.
- Contract tests → `test_mcp_contract.py` added (naming only).

**Still open (unchanged after 7 months):**
- CRIT-002 admin endpoints unprotected → **C2**. Authn added, authz not.
- HIGH-001 registry not persistent → **M7**.
- HIGH-004 missing API tests → gap #3 above. `mcp_http.py` still has zero tests.
- MED-001 intent not length-limited → **M4**.
- MED-002 tenant isolation placeholder → **M1**.
- MED-004 `datetime.utcnow()` deprecated → **M11** (8 sites).
- MED-005 `PLANNING_TIMEOUT_SECONDS` not enforced → **M5**.
- MED-006 retry queue not persistent → **M12**.
- LOW-003 health check has no dependency checks → **M9** (worse: there is no HTTP health route at all).
- LOW-005 unused DB tables → **M7**.
- §4.2 swallowed exceptions → **M2** (fixed for persistence, still `except: pass` for receipts).

**Regressed / newly introduced since review 1:**
- The dispatcher (`dispatcher.py`) did not exist at review 1. It introduced C1, H1, H2, and H3 in one file, and contradicts `SPEC-DG-0000.txt:526` (H7).
- `mcp_server.py` was reported at review 1 as "Clean MCP tool definitions… proper async initialization". It is now non-importable (H4) — either the MCP SDK moved under an unbounded `>=1.0.0` pin, or it never worked and review 1 did not check.
- `.mypy-ci.ini` disables `attr-defined`, which is what suppresses H4 in CI.
- Review 1 was silent on principals/ownership entirely. That gap (C1, Exit Criteria §3) has therefore gone unexamined through two reviews.

---

## Cross-repo Observations

1. **AsyncGate has the principals module DeleGate needs.** `AsyncGate/src/asyncgate/principals.py:3-10` defines `SYSTEM_PRINCIPAL_ID = "sys:legivellum"`, `SERVICE_PRINCIPAL_ID = "svc:asyncgate"`, and `is_system()`. This belongs in `LegiVellum/shared/legivellum/` so DeleGate, ReceiptGate, and the rest cannot diverge. Today `grep -rn SYSTEM_PRINCIPAL_ID` matches only AsyncGate.
2. **AsyncGate already supports `idempotency_key`** (`AsyncGate/src/asyncgate/mcp/server.py:81`). DeleGate ignores it. This is a one-line fix for H2's obligation-duplication half.
3. **`caused_by_receipt_id` has no wire representation between gates.** AsyncGate's `create_task` schema has no such parameter, so no upstream minter can express causality at creation time; AsyncGate derives it from its own obligation (`engine/core.py:551`). Either `create_task` gains the field, or the whole-system provenance invariant is unenforceable at every mint boundary, not just DeleGate's.
4. **Tenant identifiers are inconsistent across the mesh.** DeleGate sends `tenant_id="default"` to ReceiptGate (`config.py:141`) and `tenant_id="00000000-0000-0000-0000-000000000000"` to AsyncGate (`config.py:165`). The same logical plan therefore lands in two different tenants, so no cross-gate query can join a plan to its obligations.
5. **ReceiptGate's obligation-closure query is `task_id`-keyed** (`ReceiptGate/src/receiptgate/ledger_v1.py:148-165`). Any gate that emits a terminal receipt under a synthesized `task_id` leaks an open obligation. DeleGate does this (C3); this is worth auditing in every other gate.
6. **ReceiptGate dedupes on `receipt_id` + canonical hash, not `dedupe_key`** (`ledger_v1.py:100-140`). The `dedupe_key` field in the canonical schema is currently decorative; DeleGate sets it to `"NA"` everywhere and would gain nothing by setting it correctly until ReceiptGate enforces it.
7. **`uv.lock` resolves `mcp==2.0.0` with a dependency on `httpx2`.** Worth checking whether sibling repos pin the same major, since `mcp>=1.0.0` is unbounded across the stack.
8. **Canonical alignment requires DepotGate for plan storage** (`docs/canonical/DeleGate/alignment.md:11`); DeleGate has no DepotGate integration and emits unresolvable `delegate://` pointers into the shared ledger.

---

## What's solid

- **Plan model validation is genuinely good.** `models.py:407-476` — Kahn's-algorithm DAG check, unique step ids, dependency-reference validation, and trust-policy satisfiability, all enforced by a pydantic `model_validator` that runs on every construction. A plan cannot reference its own ancestor; the cycle question in the brief is answered correctly.
- **Per-step-type field requirements** (`models.py:304-327`) are enforced rather than documented.
- **The cognition/fallback design is principled.** `planning_fallback=escalate` as the default, with the reasoning written down (`config.py:191-199`, `planner.py:598-642`) — refusing to substitute a regex split for reasoning and saying so is the right call, and `_normalize` (`ai_planner.py:106-153`) means a CogniGate plan is validated identically to any other provider's.
- **Partial-dispatch semantics are deliberate and tested.** `DispatchResult` returning rather than raising (`dispatcher.py:39-60`) is the right shape for the problem, and `test_dispatcher.py:84-101` covers it.
- **The authority-boundary test** (`test_dispatcher.py:126-146`) asserting the dispatcher only ever calls `create_task` — never lease/complete/report — is exactly the kind of test this repo needs more of.
- **Migration is clean:** one head, no branch labels, correct unique indexes, working `downgrade()`.
- **MetaGate bootstrap** (`metagate_client.py`) is careful — path-based module loading with a documented rationale, and non-blocking by design so the bootstrap authority cannot become a hidden master.
- **MCP naming conformance** is fixed, tested, and backward-compatible (`mcp_http.py:189-219`, `test_mcp_contract.py`).
