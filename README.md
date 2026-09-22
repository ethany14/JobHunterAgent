# JobHunterAgent

## Two frontends

The product now has two deliberately separate surfaces:

- **Web workspace** at `http://localhost:8000/app`: the complete local Career Copilot for saved jobs, conversation history, application materials, Copilot-launched interview practice, evidence, governed learning, and PDF resume management.
- **Northstar Job Lens** Chrome extension: a focused current-page extractor that returns a resume match score and suggestions. Saving or analyzing a page creates or reopens its web Workspace, preserves the original job URL, and offers a direct link to that saved application. It uses the default resume held by the web service and never stores the resume in Chrome.

Start both surfaces with:

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m uvicorn api.main:app --host 127.0.0.1 --port 8000
```

Open `/app` first and upload a selectable-text PDF under **Settings**. The server stores extracted text and document metadata in SQLite; the original PDF bytes are not retained. The local deployment still assumes one trusted user and is not an authentication boundary.

Copilot conversation history appears in a persistent left sidebar and can be
reopened or archived. Tailored resume artifacts can be downloaded as searchable,
ATS-friendly Letter-size PDFs using the compact one-column style of the uploaded
resume. Opening Job Lens from a regular LinkedIn job tab automatically extracts the
visible title, company, location, description, and source URL; **Extract page** remains
available when LinkedIn updates the selected job without a full navigation.

JobHunterAgent analyzes a resume and job description,
matches requirements to quoted resume evidence, writes a tailored resume, verifies
every generated claim, and pauses for human approval. Unsupported claims enter a
bounded revision loop before review.

## Conversation-first Career Copilot

The web workspace owns durable conversations, saved jobs, materials, resume uploads,
Evidence, Memory, and Skills. Previous conversations can be selected or archived from
the Copilot header. Interview practice starts through Copilot rather than a separate
navigation page. The Chrome Side Panel intentionally contains only extraction, saving,
fit analysis, and a link into the full web Workspace.

Completed Copilot turns feed the governed conversation-learning observer. Explicit
user preferences and user-stated facts become reviewable Memory candidates; the web UI
does not ask users to manufacture Memory records by hand. Confirmed Memory and active
Skills can be soft-deleted or retired without erasing audit history.

Only the server-owned registered action set can leave ordinary conversation and
invoke a workflow. Deterministic domain services validate the current state before
execution. Multi-Agent progress is presented as one compact expandable activity
row; the former feature panels remain behind the local Developer Mode setting
`ENABLE_LEGACY_EXTENSION_UI=true` while parity testing continues.

The presentation tables are managed by Alembic migration
`0024_conversation_first_assistant`. The public endpoints are:

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/api/assistant-sessions/{session_id}/timeline` | Read safe ordered activity; accepts `after_sequence` |
| `POST` | `/api/assistant-sessions/{session_id}/actions` | Classify or execute one registered, version-checked action |

Timeline payloads never include system messages, raw tool output, secrets,
filesystem paths, unrestricted domain JSON, or internal reasoning. This remains a
trusted local single-user application; the endpoints do not establish tenant or
account authorization.

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

### MCP lifecycle and observability

FastAPI owns one `McpToolManager` for the application lifespan. Each configured
server moves through `disabled`, `starting`, `ready`, `degraded`, `stopping`,
and `stopped`; shutdown first rejects new calls, waits a bounded time for active
calls, closes clients, and unregisters their tools. An optional startup failure
is `degraded`, while a required startup failure aborts runtime initialization.

`GET /health` includes a sanitized server list. Server status, active/completed/
failed/timeout counters, and last success/failure diagnostics are process-local
and reset on restart. They are operational diagnostics rather than the source of
truth for calls. Tool-call rows, safe transition events, redacted arguments,
normalized results, and provenance are persisted by Tool Runtime and survive a
restart. Reusing an idempotency key does not execute or increment MCP counters.

Persisted MCP provenance has one form: `server_id`, `remote_tool_name`,
`transport` (`stdio` only), and `public_tool_name`. It is attached to normalized
`ToolResult` provenance and safe call events. Built-in tools retain their own
`internal_database` provenance and are never inferred to be MCP tools from their
names. The Session API exposes only safe call metadata for the collapsible Side
Panel display; it does not expose arguments, results, commands, process paths,
environment configuration, stderr, or raw exceptions.

This release still supports only client-side stdio tool discovery and calls. It
does not implement Streamable HTTP, SSE, OAuth, reconnection, resources, prompts,
sampling, elicitation, or external MCP service configuration from clients.

## Job Workspace v0.2

The Job Workspace is the internal source of truth for saved opportunities and
application progress. A **Job** stores stable posting identity and metadata. A
**JobSnapshot** stores one immutable, hashed capture of the untrusted job text.
An **Application** points to a Job and its current snapshot while tracking the
user's workflow. Analysis and resume output live in immutable, versioned
**ApplicationArtifacts**, and application changes append safe **ApplicationEvents**.

Application status transitions are deterministic:

```text
saved -> analyzing -> needs_evidence | materials_ready | analysis_failed
analysis_failed -> analyzing | archived
needs_evidence -> analyzing | materials_ready
materials_ready -> ready_to_apply | analyzing
ready_to_apply -> applied | analyzing
applied -> interviewing | rejected | withdrawn
interviewing -> offer | rejected | withdrawn
offer | rejected | withdrawn -> archived
```

Entering `applied` requires `applied_at`. Same-state transitions are idempotent.
Application mutations use an expected version, update the projection, increment
the version, and append ordered events in one transaction. Artifact content and
version are immutable; approval can supersede an older approved version.

Existing `runs` and `agent_sessions` remain unchanged. Association tables allow
many runs and sessions per Application. The focused run adapter attaches a
completed run and idempotently projects its existing public Job Analysis, Match
Report, and Tailored Resume into artifacts; it does not alter resume generation.

URL normalization lowercases HTTP(S) scheme/host, removes fragments and known
tracking parameters, and preserves job-identifying query parameters. Exact URLs
or exact manual-content fingerprints can be reused. Company/title similarity is
reported as duplicate candidates and never silently merged. Normalization does
not fetch URLs or follow redirects, so redirects and site-specific aliases can
still produce separate Jobs.

Migration `0013_job_workspace` creates the workspace schema and
`0014_workspace_failure` adds the safe analysis-failure state. Both are
forward-only for production use. Back up the
SQLite database before upgrading; automated tests never downgrade user data.
Captured descriptions, resume-derived artifacts, and application history may
contain personal data, so deployment still needs retention, deletion, encryption,
and user authorization policies. This version intentionally defers scraping,
autofill, job-board integrations, Interviewer/Multi-Agent features, cover-letter
generation, and automatic application submission.

The Side Panel saves a Job through `POST /api/workspaces` without starting a
model call. One transaction finds or creates the Job and immutable snapshot and
reopens an active Application when one exists. The Jobs tab loads compact list
rows, then fetches detail, recent safe events, and artifact version metadata for
the selected Application.

Workspace analysis reuses the existing custom Run workflow and its human review
behavior. The attached Run's public Job Analysis, Match Report, and Tailored
Resume are projected idempotently into immutable artifacts. A failed run moves
the Application to `analysis_failed` with a safe code and retry action.

Assistant Sessions opened from a Workspace are explicitly attached through
`application_sessions`. Context assembly includes a compact summary only for
that Application and selected artifact references; immutable Context Snapshots
record those source IDs. Full raw JDs, all events, and unrelated Workspaces are
not injected.

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

Career Evidence Vault v0.1 is documented in [agent_runtime/evidence/README.md](agent_runtime/evidence/README.md). Its confirmed/source-grounded records are distinct from Memory, Skills, job requirements, and generated artifacts. The Chrome Context panel supports manually attested candidates and confirmation; resume imports require the original resume on the server.

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
# Interviewer Agent v0.1 (local evidence discovery)

For a saved Application with a current Job Snapshot and a MATCH_REPORT, open its
Workspace detail and choose **Improve Evidence**. The interviewer prioritizes
unresolved required requirements, asks one short question at a time, and saves
answers and questions as immutable turns. It uses an existing local Agent Session
for messages and a Context Snapshot for each model decision. Close and reopen the
Side Panel, then reopen the same Application to restore the interview. A failed
model decision can be resumed; a confirmed gap applies only to that Application.

The state flow is `planning → awaiting_answer → planning →
awaiting_evidence_confirmation → planning`, ending in `completed`, `cancelled`,
or `failed`. The controller caps questions (10 by default, 20 maximum) and
follow-ups (2 by default, 3 maximum). An exhausted clarification remains
unresolved; it is not treated as a user skip or as proof of no experience.
Questions may not suggest accomplishments or demographic answers. Generated
claims must be literal spans of the saved user answer. An Evidence Candidate is
not reusable confirmed evidence: the user must explicitly confirm or edit and
confirm it. A rejection does not confirm it. An edit must remain grounded in the
original answer. An explicit no-experience answer records only an
Application-specific gap.

The interviewer sees only the current Application, its current Snapshot and
requirement, the matching artifact, a few related confirmed Career Evidence
items, and recent turns. Its effective tool set is empty. It does not receive
other Applications, unrelated evidence, or MCP tools. Snapshot references record
the exact source and evidence versions; `interviewer-v1` identifies the trusted
prompt procedure. A saved answer is not repeated after a recoverable model
failure. The immutable interview turns are authoritative if a crash occurs
before their Session message mirrors are synchronized. The first restore or
resume repairs those mirrors. Model calls may be repeated if a process crashes
before a question or classification is persisted; no external write tools run.

Local APIs use `/api/applications/{application_id}/interviews` to start and
`/api/interviews/{interview_session_id}` for state. Answer, skip, confirm-gap,
candidate confirm/reject, cancel, and resume are POST subroutes. Mutations
require `expected_version` and `idempotency_key`. This local deployment does
not provide multi-user authorization. Resume/JD and interview answers remain in
the local SQLite database; protect and delete that file according to your own
privacy needs.

Run deterministic safety evaluation with
`python -m evals.run_interviewer_evals`. Its versioned artifact reports only
deterministic claim-validation and scripted-model controller cases. Additional
integration checks are in `tests/test_interviewer.py`. Model tokens, cost, and real-world interview
quality are not measured by this fake-model suite. A live-model evaluation is
still needed before claiming general accuracy. Mock behavioral interviews,
automatic resume rewriting, and automatic evidence confirmation are deferred.

## Application Pack v0.1

An Application Pack belongs to one saved Application and one immutable Job
Snapshot. It captures selected **confirmed** Career Evidence versions, their
content hashes, relevant confirmed writing preferences, and the Pack prompt
version before generating a tailored resume, cover letter, or an answer to an
explicitly supplied application question. The Pack uses the existing custom
resume writer and verifier; the LangGraph baseline remains unchanged. The
cover-letter and answer writer use the shared model configuration. No job
requirement or model-generated artifact is treated as candidate evidence.
When the current analysis run is attached to the Application, its extracted
resume quotes are checked against that run's original resume and imported as
confirmed Career Evidence before Pack selection. Interviewer evidence is
available only after confirmation and an explicit link to the Application.

Pack status moves through `draft`, `generating`, `verifying`,
`needs_revision`, `awaiting_review`, and `approved` (or `failed`/`stale`).
Each item has its own status, version and bounded three-revision loop. The
Pack and item state, immutable ApplicationArtifact version, and audit event
are saved transactionally. A crash after a model response but before the
artifact transaction can cause the model call to be repeated; saved artifacts
are not overwritten. A `verifying` item resumes from its saved artifact.
Review and edits use optimistic versions and idempotency keys. The old Pack
remains readable if its Job Snapshot, selected evidence, preference, or prompt
version changes, but cannot be edited or approved; create a new Pack explicitly.
Edit and review events record block IDs and accepted, edited, or rejected
decisions without repeating block text. The generation snapshot stores the
selected evidence and preference versions plus a safe model ID, temperature,
retry count, and endpoint hash; it does not store API keys or endpoint URLs.

The deterministic verifier requires each factual block to cite a confirmed
evidence version captured in that Pack and to be a literal supported excerpt.
It checks the full block even when the model labels it `motivation` or
`transition`. This deliberately favors rejecting a useful paraphrase over
publishing an unsupported claim. A confirmed summary sentence preference is
checked during verification. Human edits create a new artifact version and
are verified again. Legal/identity, demographic, logistics, and unknown
application questions require a manual answer; the agent does not guess
sponsorship, disability, salary, availability, or similar facts. A manual
question does not block approval of the required resume and cover letter.

The local API uses `POST /api/applications/{id}/packs` to create a Pack,
`GET /api/applications/{id}/packs` for history and `GET /api/packs/{id}` for
detail. Item generation uses `/resume`, `/cover-letter`, or `/questions` under
the Pack URL. Item actions use `/items/{item_id}/edit`, `/approve`, `/reject`,
and `/regenerate`; read-only `/evidence`, `/versions`, and Pack `/events`
support review. A creation `expected_version` is the Application version;
generation uses the Pack version; item actions use the item version. All
mutations require an idempotency key. Application Pack tables are created by
Alembic revision `0017_application_pack`; production does not call
`create_all()`.

To inspect the bounded synthetic safety evaluation, run
`python -m evals.run_application_pack_evals`. Its versioned result is
`evals/results/application_pack_v0.1_deterministic.json`. It makes no model
calls; generation quality, token use, cost, and live revision success are not
measured by that artifact. With a configured model endpoint, run the optional
synthetic smoke test using `python -m evals.run_application_pack_live_smoke`.
Its unedited model outputs and observed latency/token counts are saved in
timestamped `evals/results/application_pack_v0.1_live_smoke_*.json` files.
Cost is reported only
when the provider or configured per-token rates supply it. Neither artifact
is a real-world accuracy estimate. Normal unit tests use fake models and
separate temporary SQLite databases.

For a manual end-to-end check, upgrade the database, start the API, reload
the unpacked Chrome extension, then open a saved and analyzed Application.
In **Improve Evidence**, answer one Interviewer question and confirm the
resulting Evidence Candidate. Click **Generate new Pack** in Workspace and
generate **Resume**. Inspect its **Evidence** citation and **Verification**
details, then generate **Cover Letter** and an explicit question such as
“Why are you a good fit?”. Add a sponsorship question; it must display
“Manual answer required” with no generated answer. Edit a cited resume
block and inspect **Versions**; the older version must remain. Restart the
API, reopen the same Workspace and continue reviewing the saved Pack. Approve
only versions whose latest verification passed. The Side Panel renders Pack
content with safe DOM text APIs and does not store Pack content in Chrome
storage. This remains a trusted local single-user deployment without account
authorization or automatic application submission.

## Multi-Agent Runtime v0.1 (deterministic infrastructure)

The custom runtime can persist a server-approved task DAG under a parent Session.
`AgentPlanValidator` checks registered worker types, dependencies, cycles, depth,
task count, tool permissions, active Skill versions, and explicit input artifact
references before the repository creates a plan. Each task has its own status,
version, event stream, attempts, deadline, and immutable output links. The
parent receives bounded status/summary/artifact references; child Session
messages and context are hidden from the public Session API.

The task state path is `blocked/pending -> ready -> claimed -> running ->
succeeded/failed`, with separate `awaiting_approval`, `awaiting_input`,
`cancel_requested`, `cancelled`, and `timed_out` branches. Only the current
attempt can finish a task. A successful output and `succeeded` state commit in
one transaction. Dependency conditions are evaluated in Python from persisted
task states; a failed `requires_success` predecessor leaves its child blocked.
An explicit retry uses a fresh attempt and child Session. An expired claim can
be reclaimed after restart; an approval/input pause is never automatically
re-executed.

`AgentContextPolicy` starts from explicit input artifact links and a bounded
selection of parent user/assistant turns. It can include explicitly selected,
confirmed Memory and Career Evidence, plus active Skill versions. It hashes an
immutable attempt manifest containing IDs/versions/hashes, effective tools,
included message IDs, and a token estimate. It never copies a sibling's
conversation. Effective tools are the intersection of task, parent Session,
registered, task-type, and current MCP availability; Skill restrictions narrow
that set further. Workers receive this scoped context and a `ToolContext`
factory with `task_id` and `attempt_id`; ToolExecutor remains the authority for
approval and idempotency. Skill procedures cannot override system policy,
permissions, evidence rules, cancellation, or limits.

One in-process scheduler starts after Alembic and MCP initialization in FastAPI
lifespan. It uses a conditional SQLite update with expected version to claim
work, heartbeats without changing the business version, and scans expired
claims. Shutdown stops new claims before MCP shutdown and waits up to a grace
period for active workers. Transactions are short and SQLite connections use a
bounded busy timeout. This is **single-process/local infrastructure**: no
distributed worker queue or multi-user authorization. Python threads cannot be
forcibly stopped. A worker/model/tool may run more than once after a crash,
while task finalization is fenced by attempt ID. External writes retain Tool
Runtime's `outcome_unknown` semantics; this task layer cannot prove exactly-once
side effects. Model/tool usage budgets rely on trusted worker instrumentation;
the opt-in Job workflow registers a fixed business template; browser clients
cannot launch arbitrary worker code. Automatic LLM planning remains deferred.

Read-focused routes are `POST /api/agent-plans`, `GET /api/agent-tasks/{id}`,
`GET /api/agent-tasks/{id}/children|events|artifacts`, and versioned
`cancel|retry|resume` mutations. Plan creation accepts only a server-registered
template ID. The Workspace **Task Activity** details are diagnostic and render
text with safe DOM APIs. No prompts, raw tool results, secrets, or unrestricted
context snapshots are returned. For a no-model infrastructure benchmark run
`python -m evals.run_multi_agent_evals`; it writes
`evals/results/multi_agent_v0.1_fake.json`. The benchmark's fake workers do not
establish real-model latency, cost, or production concurrency behavior.

## Multi-Agent Job Workflow v0.1 (opt-in)

Workspace keeps **Standard** as the default. Choosing **Multi-Agent** calls the
server-owned `job_application_multi_agent_v1` template. The selected mode is
persisted on the plan, Pack, and generated artifacts, so restarting the backend
does not reinterpret an existing execution. Historic Standard Packs remain
Standard. The REST entry point is
`POST /api/applications/{application_id}/multi-agent-runs` with the current
Application version, an idempotency key, a requested artifact set, up to three
explicit questions, and an optional interview flag. The client cannot submit
worker definitions, prompts, tools, Skills, or dependencies. Read-only run,
task, and timeline endpoints plus versioned cancel/resume endpoints live under
`/api/multi-agent-runs/{root_task_id}`.

The fixed task order is source validation, parallel Candidate and Job analyses,
matching, evidence-gap classification, optional evidence interview, evidence
freeze, independent requested writers, verification/revision for each draft,
and deterministic Pack assembly. Candidate analysis receives the original
resume without the JD; Job analysis receives the current JobSnapshot without
the resume. Matching sees only the two typed analyses and up to 50 explicitly
linked, confirmed Career Evidence items. Writers see one frozen confirmed-evidence snapshot and
relevant writing preferences. The verifier sees only its draft and that
snapshot. The assembler sees final draft/report references, not raw source
text. All worker tool allowlists are empty in this release. Existing business
prompts, schemas, score rules, evidence selection, writer/verifier services,
and public Pack schemas are reused.

Each artifact has at most three revisions. Revision tasks are predeclared in
the bounded DAG and become no-ops after verification passes. An unsupported
claim at the limit remains `needs_revision`; it cannot be approved as a
verified artifact. Required failure blocks assembly; optional artifact failure
permits a partial Pack without erasing a verified required item. Restricted
application questions stay manual. When an interview requires input, the task
pauses without holding a worker thread or transaction; its persisted interview
ID is used after backend restart. The user can complete or cancel that
interview, then explicitly continue without clarification or cancel the root.

One immutable GenerationEvidenceSnapshot is shared by writers and revisions.
If the current JobSnapshot, confirmed evidence, or selected preferences change
after freezing, assembly refuses to publish the old result; start a new
execution to use the new facts. Task claims fence stale attempts, but model
calls may be repeated after a crash. No exactly-once model-execution guarantee
is claimed. Task status and bounded summaries appear in Workspace; complete
worker prompts, child conversations, and raw provider errors do not.

Behavioral parity gates for a future live paired evaluation are declared before
looking at model output: no additional forbidden claims, citation validity at
least as high as Standard, no lower required-artifact review rate, no increase
in unsupported-claim false negatives, and no restricted question answered by
the model. Latency, token use, and estimated cost are reported as observed,
without a claimed improvement threshold. The existing deterministic runtime
benchmark uses fake workers and does not establish these live-model gates.
Until a same-dataset, same-model paired evaluation passes, Multi-Agent remains
opt-in and Standard remains the production default.
Run `python -m evals.run_job_workflow_component_evals` for the shared,
deterministic 14-case safety gate. Its result is
`evals/results/multi_agent_job_workflow_v0.1_component.json`. This runs the
same verifier and restricted-question rules used by both modes, with zero
model calls; it explicitly does not measure end-to-end parity, parallel
speedup, generation quality, latency, tokens, or cost.
The current release does not yet record per-worker provider token/cost usage
or write task lifecycle messages into the parent Session; the task/timeline
API is the supervisor progress source. Revision steps are predeclared rather
than inserted after a failed verifier result. Live paired parity and ablation
measurements, including parallel speedup, remain outstanding release gates.

Manual check: run Alembic upgrade, start FastAPI, reload the unpacked extension,
and open a saved Application with an attached analyzed run and confirmed Career
Evidence. Select **Multi-Agent**, choose resume, cover letter, and one ordinary
application question, then start. In the task view, confirm Candidate and Job
analyses become ready together and matching waits for both. With Interview
enabled, answer a question and confirm or skip its Evidence Candidate; the
paused task must resume from the same interview after a backend restart. Watch
the frozen snapshot, independent writer tasks, and each verification report;
review the resulting Pack and its artifact provenance. Also try a restricted
question, cancel an interview and continue without clarification, and verify
that an approved artifact can still use the existing explicit MCP export path.

### Mock Interview v0.1

The Workspace now has a **Mock Interview** section for a current saved Application.
It requires a current JobSnapshot, Job Analysis, an approved Pack for that
snapshot, and unchanged confirmed Career Evidence pinned by the Pack. This is
practice and coaching, distinct from **Improve Evidence**, which asks questions
to clarify missing resume evidence. Modes are recruiter screen, behavioral,
project deep dive, role specific, and mixed. The server builds an immutable,
deterministic 3–12 question plan, then asks one question at a time. Each main
question permits at most two follow-ups; the default is one. The model proposes
questions and coaching, while Python enforces the plan, score bounds (1–5),
exact answer quotes, and follow-up count. Scores assess an individual practice
answer, not employability or offer likelihood. No demographic, emotion, accent,
or personality assessment is performed.

The interview stores its question, original answer, evaluation, event, and
version separately. The panel can close while awaiting an answer; reopening
the Application restores the active interview. After a backend restart, a
saved answer is evaluated once and an evaluated answer is not asked again.
A failed model step may be retried with **Recover**. Model calls can repeat if
the process stops before the response is saved; this is not exactly-once
execution. The final immutable Interview Report includes dimension averages,
practice priorities, coverage, and candidate evidence references. New facts
quoted exactly from an answer enter Career Evidence only as **candidate**;
confirm or reject them explicitly in the report or Evidence Vault. Viewing an
approved Pack later refreshes staleness if confirmed evidence changed; the
interview never regenerates the Pack itself.

The optional `mock_interviewer` Multi-Agent worker uses the same persisted
controller and pauses at `awaiting_input` with an interview ID. Its runtime
tool allowlist is empty. The independently started Workspace interview does
not rerun the application workflow. No external research, export, document
rewrite, evidence confirmation, or application submission is available to
this worker. Candidate, Job, Pack, and evidence excerpts are pinned to this
Application and treated as untrusted source data; unrelated interviews and
private session messages are not sent to the model.

Run `python -m pytest tests/test_mock_interview.py` for the fake-model and
migration checks. The 13-case synthetic dataset is
`evals/mock_interview_cases_v0.1.json`; run
`python -m evals.run_mock_interview_evals --live --runs-per-case 3` for an
optional real-model result. The runner saves raw evaluations and validity
checks without repairing outputs. Token use and cost remain unavailable until
the structured-output adapter exposes provider usage. A deterministic dry run
without `--live` checks dataset wiring only, not answer quality.

Manual check: apply Alembic migrations, start FastAPI, reload the extension,
and open a completed Application with an approved Pack. Start a five-question
mixed interview. Give an incomplete answer and inspect whether a relevant
follow-up appears. Close and reopen the panel; the same question must remain.
Give a new real experience with an exact quote, finish early or complete all
questions, and inspect the report. Confirm its Evidence Candidate explicitly,
then reload the Pack to observe staleness. For recovery, start another
interview, stop the backend while awaiting an answer, restart it, reopen the
same Application, and verify the answer and question sequence are preserved.
Model-based coaching can still be inconsistent or overinterpret a response;
review any candidate and suggested phrasing before relying on it.
## Harness design comparison

The current custom runtime has been compared with the progressive harness
patterns in `learn-claude-code`. The capability mapping, deliberate differences,
and context-retention rule are documented in
[`docs/learn-claude-code-comparison.md`](docs/learn-claude-code-comparison.md).

# Governed Feedback Learning v0.1

Feedback learning here means recording explicit user actions as immutable source events and producing reviewable candidates. It does **not** train a model, change prompts or policies at runtime, publish Skills, or automatically confirm Memory or Career Evidence. The local server owns the profile ID; this is a single-user local deployment, not authentication.

`POST /api/feedback` accepts explicit instructions or manual feedback. The server also records selected Pack approvals, edits, rejections, regeneration requests (with an optional user reason), Evidence decisions, application status changes, and explicit Evidence/Mock Interview feedback. One acceptance and application outcomes are weak signals; they do not prove a writing strategy worked. Explicit instructions are strongest. Edits are recorded as observed differences, without inferring a broad rule from them.

The deterministic router separates user preferences, user-stated career facts, reusable Skill procedure proposals, and product/policy issues. Candidates stay in `collecting` until their configured evidence threshold is met. An explicit preference or career fact becomes ready for review; a Skill needs an explicit request or at least three consistent events across two applications. Same-key contradictory values remain separate candidates with conflict links. Application-scoped overrides do not invalidate a user-scoped preference.

The Learning panel lets the user inspect sources and conflicts, edit, reject, or confirm a candidate. Preference confirmation uses governed Memory; career-fact confirmation uses governed Career Evidence. Application-scoped preferences are stored under an application-specific project scope and are not injected into live Session context until a later context update supports that scope. Skill approval only marks it ready for future evaluation; it never writes `SKILL.md` or activates a Skill. Policy acknowledgment never modifies runtime code. Feedback deletion is a soft delete that removes support from future candidate calculations, while historical context snapshots remain unchanged. Deleting a source event does not automatically revoke a separately confirmed Memory or Career Evidence record; delete or archive that record through its own manager if needed.

Feedback can contain sensitive resume or interview details. The server redacts common credentials on ingestion and personal identifiers from Skill proposals. The current regex filters are limited; keep this SQLite service local and do not expose it to untrusted users. Confirming a candidate and writing to the separate Memory/Evidence repository are not one cross-repository transaction; a retry uses linked IDs where available.

Synthetic routing evaluation: `python -m evals.run_feedback_learning`. It records raw expected/actual classification in `evals/results/feedback_learning_v0.1.json` and uses no model calls. The recorded accuracy is a small deterministic baseline, not real-world learning quality.

## Governed Skill Evolution v0.2

The Feedback candidate, staged package, published version, and active version are distinct. Only a user-approved procedural Skill candidate in a global or role-type scope may be staged. User preferences and career facts cannot become a global Skill through this path. Generated packages contain a concise `SKILL.md` and optional Markdown references; no generated scripts, executables, assets, plugins, MCP definitions, or policy files are accepted. YAML is parsed safely, local links and package hashes are checked, and simple PII/secret/policy-bypass patterns are rejected. These pattern checks are conservative and cannot prove the absence of all personal data or prompt injection.

The staging tree under `skills/generated/staging/` is outside the active registry. The current immutable synthetic evaluation dataset is `evals/skill_evolution_v0.2.json`; it covers positive, negative, boundary, injection, tool-policy, regression, and holdout cases. A candidate requires a matching server-registered dataset ID and name. A paired run uses the same configured model, temperature, prompts, repetition count, and no tools or external writes for baseline and candidate. It stores output hashes, redacted synthetic outputs, and metrics. Deterministic term checks and lexical activation scores are useful screening signals, not a comprehensive semantic or safety evaluation. Hard thresholds are frozen in each run's `config_json`; a changed package hash prevents publication against old results. Soft token, latency, and cost regressions require explicit reviewer acknowledgement. A human must inspect the preview, passing gates, and forward-test summary, then explicitly publish. Publishing copies to `skills/generated/<skill-name>/versions/<semantic-version>/SKILL.md` and inserts the formal Skill version and audit records in one database transaction. Publication defaults to **inactive**.

Activation is a separate action. Only `active` versions enter normal Skill discovery and new server-owned Session skill profiles; existing Session allowlists and ContextSnapshots retain their pinned version/hash. A `canary` version can be selected only by creating an explicit test Session with `test_canary: true`; matching uses the same deterministic lexical overlap as active Skill routing, pins the exact version/hash, and narrows tools. A `shadow` version is checked for lexical selection in new Sessions and recorded only after a response is saved; its instructions are never sent to the model. Shadow selection is not a second model run or a paired quality comparison. The Chrome UI does not yet offer the test-canary Session toggle. Explicit rollback moves the database pointer to an older immutable version or no Skill and leaves published files and historical snapshots untouched. The monitoring table records ContextSnapshot selection counts; task success, verifier, acceptance, token, cost, and rollback warnings are not yet automatically wired to every product task. Do not infer causality from one result.

To check manually, apply `alembic upgrade head`, start the local API, and reload the extension. In **Learning → Skill Candidates**, approve a harmless reusable procedure such as `application-answer-structure`, materialize it, inspect the `SKILL.md`, run three paired repetitions, review hard gates and holdout results, and explicitly publish. Publication is inactive. Select `active` only after review, then create a new Session and inspect its ContextSnapshot version/hash. For a changed staged file, rerunning evaluation or publishing against the old hash is blocked. To revert, use the version's Rollback control with a reason. This local API uses a server-owned profile ID but has no authentication; do not bind it to an untrusted network.

The optional synthetic live smoke command is `.venv\\Scripts\\python.exe -m evals.run_skill_evolution --runs-per-case 3` on Windows. It uses a temporary Alembic database and the configured model and never publishes. The first result is preserved in `evals/results/skill_evolution_v0.2_live.json`; the updated-gate run is `evals/results/skill_evolution_v0.2_gates2_live.json`. In that second 8-case, three-repeat run, baseline and candidate both had 100% simple term-check task success; the candidate used more mean tokens (182.3 versus 151.9 in non-holdout cases), triggering a soft regression acknowledgement. These small synthetic checks do not establish real-world improvement. Estimated cost is unavailable without provider pricing metadata.
## Conversation learning

The active custom runtime can learn from ordinary completed Assistant turns without
turning a single message into an active instruction. After the user-facing response is
durably saved, a background observer records a versioned conversation experience that
references the exact user and assistant message IDs.

- Explicit user preferences and user-stated facts may create governed Memory candidates.
  They remain candidates until the user confirms them, and they do not become resume
  evidence.
- Corrections and clear success/failure signals may contribute to a reusable procedural
  pattern. A pattern needs evidence from at least three turns across two sessions before
  it can create a Skill learning candidate.
- Skill candidates still use the existing evaluation, approval, activation, versioning,
  and rollback lifecycle. Conversation learning never edits a live prompt or activates a
  Skill directly.
- Tool output, job descriptions, assistant assertions, and instruction-like content do
  not prove user preferences or career facts. Observer failures are stored with a safe
  error code and do not fail the completed user turn.

The observer uses the configured model and therefore adds a separate model call after a
completed API response. This first version has durable observations and idempotent turn
keys, but no dedicated retry worker for failed observation calls.
