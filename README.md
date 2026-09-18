# JobHunterAgent

JobHunterAgent analyzes a resume and job description,
matches requirements to quoted resume evidence, writes a tailored resume, verifies
every generated claim, and pauses for human approval. Unsupported claims enter a
bounded revision loop before review.

```text
Analyze → Match → Write → Verify → Revise when needed → Human Review
```

## Run locally

Create `.env` with `LLM_MODEL_ID`, `LLM_API_KEY`, and optionally `LLM_BASE_URL`,
`LLM_TIMEOUT`, and `LLM_MAX_RETRIES`, then run. Retry defaults to `0` so one
logical model call corresponds to one provider request during evaluations.

```powershell
.\.venv\Scripts\python.exe main.py resume.txt job.txt `
  --thread-id application-001 --output result.json
```

Run unit tests and quality evaluations with:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m evals.run_evals
.\.venv\Scripts\python.exe -m evals.run_ablation
.\.venv\Scripts\python.exe -m evals.run_stability_evals
```

## FastAPI v0.4

Install dependencies and start the synchronous development API:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn api.main:app --reload
```

The OpenAPI UI is available at `http://127.0.0.1:8000/docs`. The API stores run
records and backend state in SQLite so interrupted reviews can resume after the API
process restarts.

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/health` | Check service health |
| `POST` | `/runs` | Run the custom agent until human review |
| `GET` | `/runs/{run_id}` | Read status and generated result |
| `POST` | `/runs/{run_id}/review` | Approve or request a revision |

`POST /runs` waits for the custom agent to reach human review and returns
`awaiting_review`. A rejected review requires feedback, runs the revision and
verification loop, and returns to `awaiting_review`.

By default, API run metadata and custom-agent state are stored in `job_agent.db`.
Frozen LangGraph checkpoints remain in `job_agent_checkpoints.sqlite` for historical
runs. Override them with
`JOB_AGENT_DATABASE_URL` and `JOB_AGENT_CHECKPOINT_PATH` when needed. SQLite is
intended for a single API process; use one shared service instance per process.

## Backend lifecycle

All newly created API runs are persisted with `backend=custom`. The backend stored
on each `runs` row is authoritative for every later review: custom runs resume from
the custom state tables, while historical LangGraph runs resume from their existing
checkpoint. The current environment and application defaults never select the
backend of an existing run.

Migration `0004_backend_cutover` classifies rows with persisted custom state as
`custom`. At startup, a read-only checkpoint scan classifies a remaining row as
`langgraph` only when its `thread_id` exists in the configured checkpoint database.
Rows with evidence for both backends are marked `unknown/ambiguous`; rows with no
evidence are marked `unknown/no_evidence`. Unknown runs remain readable, but review
is rejected until an operator resolves their backend. Checkpoint data is not moved,
rewritten, or deleted during classification.

LangGraph is a frozen reproducible baseline. It remains available only for
historical run recovery, frozen smoke tests, and explicit evaluation runners. New
features belong to the custom runtime. Both backends continue to share schemas,
prompts, domain rules, model configuration, result projection, rendering, and
evaluation datasets. Do not add Tool Runtime, sessions, memory, skills, MCP,
multi-agent, or interviewer behavior to the frozen LangGraph backend.

## Evaluation baseline

The current baseline uses Gemini 3.7 Flash at temperature 0, five synthetic workflow
cases, and three synthetic adversarial cases. Four workflow cases are valid inputs;
one case checks input validation.

Across four valid workflows, the verification layer added one model call, about 425
tokens, $0.00013 estimated cost, and 1.65 seconds of latency per workflow on average.
In three synthetic adversarial cases, it detected and removed all four injected
unsupported claims with no false positives among the supported control claims.

| Average per valid workflow | Writer-only | Full agent | Increase |
|---|---:|---:|---:|
| Latency | 7.78s | 9.43s | 1.65s |
| Model calls | 4 | 5 | 1 |
| Tokens | 2,053.8 | 2,478.3 | 424.5 |
| Estimated cost | $0.003586 | $0.003716 | $0.000130 |

The four-workflow total increase was 4 model calls, 1,698 tokens, and $0.0005205.
Running the additional adversarial suite cost approximately $0.00503. These are
evaluation expenses; the adversarial-suite cost is not a per-request production cost.

## Custom agent v0.1

`custom_agent` contains a persisted sequential loop that does not import LangGraph.
It supports the existing analysis, writing, verification, bounded revision, and
human-review transitions. State snapshots, audit events, and the `runs` projection
are committed in one SQLAlchemy transaction with optimistic version checks.

Apply the database schema with:

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
```

For a v0.4 SQLite database that already has the `runs` table but no
`alembic_version`, first verify that its schema matches migration `0001`, then run:

```powershell
.\.venv\Scripts\python.exe -m alembic stamp 0001_create_runs
.\.venv\Scripts\python.exe -m alembic upgrade head
```

Application startup applies Alembic migrations before opening repositories.
`create_all()` is exposed only through the explicit `create_schema_for_tests=True`
option used by isolated tests.

The API now creates runs only with the custom backend. Session execution leases and
deterministic recovery are implemented in the session runtime; API exposure,
cancellation, and deadlines remain outside this version.

`max_revisions` limits consecutive automatic verifier-driven revisions. Human
feedback may initiate another revision after the automatic limit is reached; that
revision must pass verification before it can be approved.

The local SQLite MVP stores the full resume, job description, human feedback, and
generated state in `state_json`. Before exposing this service to extension users,
add user-scoped access control, deletion and retention policies, protected database
storage, and logging rules that prevent raw resume content from being recorded.

Custom-backend tracing context and API-level resume/JD size limits remain deferred.
They should be added before accepting extracted full-page content from an extension.

The v0.1.1 parity evaluation ran the same 20 synthetic workflow cases once through
each backend with Gemini 3.7 Flash, temperature 0, prompt v1, and dataset v2. Both
backends reached human review in 20/20 cases, achieved 100% canonical missing-
requirement recall, and generated zero configured forbidden claims. Nineteen cases
matched on every recorded behavior check. In one case LangGraph performed one
successful verification revision while the independent custom-agent model call
passed immediately; both final verification results passed. Generated text is not
used as a parity criterion.

The cost estimate uses the Gemini 3.7 Flash paid-tier list price through December 31,
2026. Provider charges may differ. Results are based on a small synthetic dataset and
one run per case, so the observed 100% detection and revision rates should not be
treated as estimates of real-world accuracy.

See [`evals/results/langgraph_v0.1_baseline.json`](evals/results/langgraph_v0.1_baseline.json)
and [`evals/results/ablation_v0.1.json`](evals/results/ablation_v0.1.json) for complete
case-level results, token counts, pricing assumptions, and run metadata.

## Repeated stability evaluation

The v0.2 stability evaluation runs 20 synthetic workflow cases three times each.
Backend, Data Analyst, Business Analyst, and AI Engineer each contribute five cases.
The dataset includes four prompt-injection cases, four synonym cases, and four
numeric-constraint cases. A separate set contains 18 injected unsupported claims
across ten adversarial cases, also repeated three times.

| Metric | Measured result |
|---|---:|
| Workflows reaching human review | 60/60 |
| Mean latency | 9.27s |
| P50 latency | 8.94s |
| P95 latency | 11.82s |
| Mean tokens per workflow | 2,360.4 |
| Mean estimated cost per workflow | $0.003608 |
| Canonical recall, macro / micro | 100% / 100% |
| Unsupported-claim detection recall | 100% |
| Supported-control false-positive rate | 0% |
| Revision success rate | 100% |
| Decision consistency across three runs | 95% of cases |
| Exact tailored-resume consistency | 5% of cases |

All 12 prompt-injection runs reached human review without a forbidden claim and
recalled all expected missing requirements. The 12 synonym runs produced no
unexpected missing requirements. The 12 numeric-constraint runs recalled all
expected constrained requirements.

The one decision inconsistency occurred in a Business Analyst perfect-match case:
all three runs passed, but one run needed an automatic revision while two did not.
Only one of 20 cases produced byte-equivalent normalized resume JSON across all
three runs. Temperature 0 therefore improved reproducibility but did not make the
generated wording deterministic. These results remain a small synthetic benchmark
and should not be generalized to production traffic.

See [`evals/results/stability_v0.2.json`](evals/results/stability_v0.2.json) for all
90 run records and their consistency signatures.

## Local read-only tools

The tool runtime includes dependency-injected read-only tools for listing recent
runs, reading an existing public result, comparing canonical requirements, and
rendering an already-grounded tailored resume. These tools query only the local
`runs` database through a `RunReader`; they do not call an LLM and do not read the
stored raw resume or job description columns.

These tools currently support only the local single-user deployment. The `runs`
table has no owner or tenant metadata, so it cannot enforce which user may read a
run. Do not expose these tools in a shared or multi-user service until ownership
fields and authorization checks are added at the `RunReader` boundary.

The provider-neutral tool loop uses serializable internal messages and delegates
provider conversion to `LangChainToolModelAdapter`. Tool results are returned to
the model only as `untrusted_tool_data`, with bounded iterations, bounded call
counts, deterministic runtime-generated idempotency keys, and persisted approval
pauses. The existing structured-output resume workflow remains separate.

The initial real-model tool-selection smoke test is opt-in and is not part of
normal pytest execution:

```powershell
.\.venv\Scripts\python.exe -m evals.run_tool_selection --smoke-test
```

## Session runtime durability

The custom session coordinator persists the user message before invoking a model,
persists each assistant decision before executing its requested tools, and persists
each individual tool result message before the next model call. Model and tool calls
run outside session database transactions. Approval and rejection decisions are
also persisted before execution resumes.

This provides replay-safe recovery after a completed tool-call record has been
committed but before its session tool message is committed: the coordinator reuses
the persisted result and does not execute the tool again. It does not provide
exactly-once external side effects. A process can still terminate after an external
tool has performed its side effect but before the tool-call repository records the
outcome. Without provider idempotency or a provider-side operation identifier, a
later retry may repeat that side effect. Likewise, a model call interrupted before
its assistant message is committed can be invoked again and incur additional cost.

Session execution is protected by a conditional SQLite claim on
`agent_sessions.active_attempt_id` and `lease_until`. Heartbeats update only these
operational columns and the attempt history; they do not rewrite `state_json` or
increment the business-state version. The coordinator heartbeats before and after
each model or tool operation and releases the claim on every stable, paused, failed,
or terminal return.

Recovery uses persisted messages, pending call order, and tool-call status. A
completed tool missing its session `ToolMessage` is replayed from its stored
`ToolResult`. An expired read-only, side-effect-free, idempotent call may retry
within its persisted attempt limit. An expired write or otherwise uncertain call
becomes `outcome_unknown` and is never retried automatically. Recovery refuses to
steal an active session or tool lease.

The remaining model boundary is at-least-once: a provider may complete a response
after the lease expires but before the assistant message is committed, so recovery
may call the model again. For external tools, the process can still die after the
provider performs work and before the tool repository commits the result; this is
reported as uncertain rather than claimed as exactly-once execution. SQLite also
serializes writers at database scope and has no row-level locks, so this lease
scheme is suitable for the current local deployment rather than high-throughput
multi-worker production use.

Session turns may carry a persisted deadline. Before and after every model or
tool operation, the coordinator reloads cancellation and deadline state. The
effective operation timeout is the minimum of the configured provider timeout,
remaining turn time, remaining session lifetime, and remaining lease time minus
the safety margin. Human approval and ordinary user-waiting time are excluded by
clearing the turn deadline while paused and starting a new deadline on resumption.

Cancellation of an active or user-waiting session is immediate. A running session
stores a durable request so its worker can discard an unpersisted model or tool
result. Read-only results can be discarded safely. If a write races cancellation
or a deadline and its external effect cannot be trusted as final, the call becomes
`outcome_unknown` and the session pauses in `awaiting_user` with a manual-recovery
call ID.

Dynamic model timeouts are passed through `LangChainToolModelAdapter` when the
provider runnable accepts invocation keyword arguments. Some providers ignore or
do not expose per-request timeouts; the coordinator still checks the persisted
deadline after the call returns, but Python cannot forcibly stop an in-flight
provider request. External systems must provide their own cancellation or
idempotency guarantees for stronger side-effect semantics.
## Persistent session API (local development)

The custom runtime exposes durable session endpoints under `/sessions`. Clients
select the server-defined `job_assistant_readonly` capability profile; they cannot
supply tool names, model settings, system instructions, API keys, or permission
metadata. The profile enables only `list_recent_runs`, `get_run_result`,
`compare_run_requirements`, and `render_tailored_resume`.

This is currently a trusted, single-user local deployment. Persisted runs and
sessions have no ownership or tenant authorization boundary, so do not expose the
service to an untrusted network. Closing an HTTP connection does not cancel work;
clients must call `POST /sessions/{session_id}/cancel` explicitly.

## Memory runtime persistence

The phase 3A memory store is a separate, versioned data domain. It records
candidate, confirmed, rejected, superseded, deleted, and expired values with an
append-only audit trail. Agent-derived values always remain candidates until an
explicit user confirmation; suspicious instruction-like content is rejected by
a basic deterministic filter. Only confirmed, unexpired values are eligible for
future retrieval, and this phase does not inject memory into model context.

The server resolves the local owner/profile identifier. The Chrome client does
not provide it. This separation prevents accidental cross-profile queries in the
local application, but it is not authentication or multi-user authorization.
Memory may contain personal or sensitive data in the local SQLite database, so a
deployed multi-user version still needs identity enforcement, access control,
retention/deletion policy, encryption, and safe logging. Confirming memory does
not create resume evidence or authorize generated resume claims.
## Context management API

The local API exposes governed Memory and Skill lifecycle operations plus a
privacy-filtered Session context summary. Memory ownership and user, project,
or session scope IDs are derived by the server. A new Memory remains a candidate
until explicitly confirmed, and replacing a confirmed value requires the atomic
supersede endpoint. Normal deletion is a soft delete so immutable context
snapshots retain their historical references.

Skill approval and activation are distinct lifecycle transitions. Activation
revalidates the package hash, resource manifest, instructions, and tool
restrictions. Generated Skills additionally require a passing evaluation result
persisted by the server. Skill `allowed-tools` continues to narrow Session tools
and cannot grant permissions.

`GET /sessions/{session_id}/context` returns snapshot metadata, selected Skill
names and versions, Memory keys and display text, effective tool names, token
estimates, and timestamps. It does not return system prompts, assembled context,
source evidence, raw tool results, owner IDs, or private messages. This remains
a trusted local single-user model rather than an authentication boundary.

## MCP client integration (custom runtime)

The custom runtime can discover tools from trusted, locally configured MCP
servers over stdio. It uses the official Python MCP SDK 2.x and keeps one
negotiated connection alive per enabled server until the host shuts the
`McpToolManager` down. HTTP, SSE, OAuth, MCP server hosting, and MCP sampling are
outside this phase.

MCP configuration is server-owned. Each server has a stable lowercase ID,
command, argument list, validated working directory, explicit environment
overrides, allow/deny filters, and startup/call timeouts. Environment values are
never included in tool schemas, errors, audit events, or persisted results.
Server stderr is discarded because it may contain credentials or local paths.
Do not accept MCP commands, paths, environment variables, or allowlists from a
browser or model.

Discovered names use `mcp__{server_id}__{remote_tool_name}`. Tools pass through
the existing `ToolRegistry`, allowlist policy, Pydantic/JSON Schema argument
validation, persistent approval binding, idempotency, timeout classification,
and tool-call audit tables. MCP annotations are untrusted hints. A tool requires
persisted approval unless trusted local configuration explicitly lists its
remote name in `read_only_tools`; annotations cannot reduce that requirement.

MCP output is untrusted external data. Text, structured content, resource/image
metadata, error flags, and `mcp_server` provenance are normalized before the
runtime sees them. Oversized output is deterministically reduced with an
explicit truncation marker. A remote `isError` result becomes a failed tool call,
and connection/protocol/timeout failures use stable safe error codes without
forwarding raw provider diagnostics.

Example host initialization:

```python
config = McpRuntimeConfig(
    servers=[
        McpStdioServerConfig(
            server_id="local-tools",
            command="python",
            args=["path/to/server.py"],
            tool_allowlist={"lookup"},
            read_only_tools={"lookup"},
        )
    ]
)
registry = ToolRegistry()
manager = McpToolManager(config=config, registry=registry)
manager.start()
try:
    # Construct ToolExecutor with this registry and the existing repository.
    ...
finally:
    manager.stop()
```

### Local FastAPI MCP test on PowerShell

Copy `examples/mcp/local_test_config.json` to a local file and replace its three
placeholder paths with absolute paths for this checkout. The JSON contains no
secret values. If a child process needs a secret, map its child variable name to
an existing backend environment variable with `env_var_names`, for example
`{"REMOTE_TOKEN": "MY_BACKEND_TOKEN"}`.

```powershell
$env:MCP_CONFIG_PATH = "C:\absolute\path\to\local_test_config.json"
.\.venv\Scripts\python.exe -m uvicorn api.main:app --host 127.0.0.1 --port 8000
```

In another terminal, check the sanitized readiness response:

```powershell
Invoke-RestMethod http://localhost:8000/health | ConvertTo-Json -Depth 4
```

Reload the unpacked Chrome extension, create an Assistant session, and ask it to
use `mcp__local-test__echo_text` or `mcp__local-test__add_numbers`. Stop Uvicorn
with `Ctrl+C`; application shutdown waits for active MCP calls for a bounded
period, closes the stdio connection and subprocess, and unregisters its tools.
For this example, confirm that no test server remains:

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -like "*examples*mcp*local_test_server.py*" }
```
