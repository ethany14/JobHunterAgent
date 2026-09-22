# Harness comparison: JobHunterAgent and learn-claude-code

This note records the September 2026 review of
[shareAI-lab/learn-claude-code](https://github.com/shareAI-lab/learn-claude-code).
The upstream repository is a teaching harness. JobHunterAgent keeps its
domain-specific persistence, evidence, approval, and recovery contracts rather
than copying the tutorial runtime.

| Harness capability | JobHunterAgent equivalent | Decision |
| --- | --- | --- |
| One model/tool loop | `ToolCallingLoop` plus `SessionCoordinator` | Keep the provider-independent loop and durable step boundaries. |
| Tool registry | `ToolRegistry` and built-in/MCP registration | Keep dependency injection and explicit allowlists. |
| Permissions | `ToolPolicy`, persisted approvals, argument hashes | Keep the stronger durable approval binding. |
| Hooks | Repository and coordinator lifecycle boundaries | Do not add mutable global callbacks. Domain events and typed dependencies remain the extension boundary. |
| Todo/task graph | persisted Multi-Agent tasks and dependencies | Keep database state, optimistic concurrency, leases, and typed artifacts. |
| Subagents/teams | Multi-Agent Runtime and Job Workflow | Keep scoped task context and server-owned worker roles. |
| Skill loading | governed Skill registry, routing, activation, snapshots | Keep progressive activation with version/hash pinning. |
| Context compaction | context budgets and immutable snapshots | Retain whole conversation groups. Only tool results from the active user turn are mandatory; historical tool groups are optional and evicted atomically. |
| Memory | governed candidate/confirmed Memory | Keep confirmation, ownership, sensitivity, expiry, and snapshot pinning. |
| Background work | execution claims and leases | Background workers remain out of scope until there is an operational worker model. |
| Cron | none | Not useful for the current user-triggered local product. |
| MCP | governed stdio MCP tools in the common registry | Keep provenance, redaction, persistence, and approval policy. |
| Workflow runtime | custom Agent, Application Pack, Job Workflow | Keep code-defined workflows where the business sequence is fixed. |
| Goal loop | verifiers, bounded revision, human review | Keep domain-specific completion gates; do not add an ungrounded generic evaluator. |

## Context retention rule

Before a model call, the current user message and every complete tool-call group
created after that message are mandatory. Older conversation is selected from
newest to oldest within the remaining budget. An assistant tool-call message and
all of its ordered tool results are always included or excluded together.

This retains the useful principle from the upstream context-compaction lesson
without truncating arbitrary JSON, rewriting persisted messages, or introducing
an extra summarization model call. Context snapshots continue to record the
exact included and excluded message IDs and can be rebuilt deterministically
during recovery.

## Deliberate differences

JobHunterAgent is a persistent, evidence-sensitive application rather than a
disposable coding CLI. Tool outputs remain untrusted data. Memory does not become
resume evidence. Skills cannot expand permissions. Approvals survive restarts and
are bound to tool version and canonical arguments. External side effects retain
outcome-unknown semantics. These constraints take precedence over reducing the
runtime to the smallest possible loop.
