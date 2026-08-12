# User-scoped MCP Routing and Execution Design

## Status

Approved for implementation on 2026-08-12. This design implements phase 2 of
`docs/prd/MCP/user-scoped-on-demand/02-MCP两级路由授权与任务执行闭环PRD.md`.

## Goal and boundary

Provide an authenticated, user-scoped MCP execution path:

```text
Main Planner -> mcp.dispatch -> Server Router -> Tool Selector
             -> approval -> task-scoped Gateway -> safe result
             -> main_agent.respond
```

The global capability registry contains one public `mcp.dispatch` capability.
User servers and tools never become global capability descriptors. The phase-1
configuration, credential, endpoint-policy, protocol-adapter, temporary-result,
and Gateway implementations remain the foundation.

Phase 2 keeps the legacy MCP runtime available for compatibility. A default-off
`MAF_USER_MCP_ROUTING_ENABLED` flag selects the new path for newly-created tasks.
The selected path is persisted with the task and cannot change while the task is
running. Shadow rollout and legacy removal belong to phase 3.

## Architecture

### Trusted planner context

The API runtime derives the authenticated owner from the task's conversation and
builds `available_mcp_servers`. Profiles contain only `server_id`, display name,
routing description, and transport. Endpoints, credentials, tools, schemas, and
protocol identifiers are excluded.

The planner may create `mcp.dispatch` with a payload containing only `server_id`.
The executor revalidates owner, enabled status, availability, and security version
before any connection is created. A constrained Server Router may select another
server only from the remaining trusted profiles.

### Discovery and resource lifetimes

Capacity admission happens before credential decryption, connection creation, or
`tools/list`. Scheduling is round-robin across users and FIFO within a user.

A task discovers a server once during normal execution. The resulting catalog and
schema hashes remain only in task memory. Network clients and scopes exist only
during discovery or remote calls; they are closed during selector, approval, and
elicitation waits so capacity is released. Returning to a server recreates the
client and reuses its catalog snapshot without another discovery.

Discovery retries once after failure. Invalid or unsupported tool schemas are
removed from the selectable catalog. If no usable tools remain, the coordinator
routes to another server or completes with the results already obtained.

### Tool selection and authorization

The Tool Selector reuses the planner model profile and emits one structured action:
`call_tool`, `finish`, `route_another_server`, or `stop`. Invalid output receives
one repair attempt. It sees only the active catalog, safe upstream facts, completed
result references, rejected/failed fingerprints, and the remaining call budget.

The task has a persistent, cross-server budget of 20 remote `tools/call` requests.
Discovery, model selection, approval waits, and local chunk reads do not count.
MRTR retries do count. Calls are serialized per task using a database CAS boundary.

Before dispatch, one transaction acquires the task call slot, checks and reserves
budget, records the canonical call fingerprint, and marks the call as potentially
dispatched. Adapter invocation occurs only after this transaction commits. A crash
after that boundary produces `mcp.execution_status_unknown`; ordinary calls are
never replayed automatically.

Authorization uses the existing persistent Interrupt transport. The answer is a
typed `allow_once`, `always_allow`, or `deny` decision. `always_allow` grants match
owner, server, tool, server security version, and canonical input-schema hash.
Grant validity is checked again immediately before the remote call.

### Results and untrusted content

Input arguments must validate against `inputSchema`. If `outputSchema` is present,
the returned structured result must validate before it can become a successful
business fact. Invalid output is quarantined in task temporary storage and exposed
only as a safe failure.

Temporary output is retained independently from network scopes. The coordinator
returns a safe summary, owner/task-bound `result_ref`, content type, byte size, and
optional Artifact reference. Local paths are never public. Main Agent input always
marks MCP output as untrusted external business data, not system instructions.

## Persistence and recovery

New persistent records cover:

- branch and call ledgers with safe identifiers, states, budget, fingerprints,
  approval result, schema/security versions, safe errors, and timestamps;
- grant invalidation time and reason;
- encrypted MRTR original target, arguments, and opaque request state;
- encrypted 2026 remote-task identifiers and minimal recovery binding;
- SSE connection leases and a persisted branch safe result;
- dedicated MCP audit events with a configurable 30-day default retention.

Tool catalogs, schemas, clients, sessions, raw protocol identifiers, selector
reasoning, full arguments, and full results are not stored in these records.

Startup reconciliation requeues eligible branches, preserves existing approval or
input Interrupts, resumes finalization from persisted safe results, and marks
ordinary in-flight calls unknown. MRTR recovery may perform one recovery discovery;
it continues only when the original schema hash and server security version match.
Standard remote tasks may be queried, updated, or cancelled but never recreated.

The 2025 experimental Tasks and 2026 Tasks Extension use separate adapters, DTOs,
method tables, and recovery handlers.

## Presence, long calls, and cancellation

Authorized task SSE connections form an online lease. The final subscriber leaving
starts a five-minute grace period; any authenticated reconnect clears it. Logout or
authentication-generation invalidation cancels immediately. Lease records include
instance and expiry data so multi-instance orphan connections converge.

Long calls have no hard timeout. At 120-second intervals, the backend updates one
call-status card. Continue resets the presentation interval. Cancel first attempts
protocol cancellation and otherwise closes the scope. If remote termination cannot
be proved, the public state remains `remote_stop_unknown`.

Cancelling or losing presence removes queued work and stops the current MCP call.
The coordinator may still select a safe alternative. Cancelling the platform task
closes every MCP scope and terminates the branch.

## Public interfaces and UI

New APIs:

- `GET /api/v1/mcp/grants`
- `DELETE /api/v1/mcp/grants/{grant_id}`
- `DELETE /api/v1/mcp/servers/{server_id}/grants`
- `POST /api/v1/tasks/{task_id}/mcp-calls/{call_ref}/continue`
- `POST /api/v1/tasks/{task_id}/mcp-calls/{call_ref}/cancel`

Unknown, cross-owner, or mismatched resources return 404. Invalid state transitions
return 409. Only platform safe references are accepted.

The frontend adds server configuration/testing, grant management, approval,
discovery/queue/call status, MRTR input, remote-task status, and recovery from the
backend ledger and task event stream. Catalogs, schemas, and decisions are never
authoritatively stored in localStorage. Approval and call controls support keyboard
operation, focus restoration, and live status announcements.

## Rollback, observability, and acceptance

Disabling the feature flag blocks new tasks from entering phase 2. Running tasks do
not switch to the legacy path or replay calls. Database changes are additive and
older code may ignore the new tables and nullable columns; destructive down
migrations are not required for application rollback.

Metrics cover discovery, queue depth/wait/fairness, active scopes, approval delay,
call outcomes and duration, schema failures, cancellations, unknown states, SSE
offline cancellation, recovery, and audit cleanup.

Implementation is complete only when MCP-USER-P2-001 through MCP-USER-P2-020 are
mapped one-to-one to automated tests, backend and frontend suites pass, sensitive
canary scans pass, and the relevant AGENTS indexes and CHANGELOG are updated.

## Recorded assumptions

- Schema grant validity reflects the most recent discovery; a later changed schema
  atomically invalidates the old grant.
- The Gateway instance capacity is deployment configuration determined by load
  testing, not a product constant.
- Waiting for approval or business input releases the network capacity slot while
  retaining the task catalog snapshot and persistent branch state.
- Recovery discovery is the only allowed exception to the normal one-discovery rule.
