# Project Sora Live Text Conversation Gateway

This document is the engineering runbook for the local-first text conversation
gateway. The detailed product and architecture rationale remains in the Feishu
design document. This file records the executable contract that ships with the
repository.

## 1. Runtime topology

The stable desktop topology is one supervised Python child process per active
local profile:

```text
Desktop Shell
  -> private bootstrap pipe (one capability token)
  -> 127.0.0.1 ephemeral port
  -> FastAPI Gateway
  -> ConversationGatewayService
  -> ConversationRuntimePort
  -> ConversationRuntime
  -> Memory / Persona / Affect / Relationship / WorldOps
```

The Gateway and Conversation Runtime are separate logical modules but share one
process in this deployment. `LocalConversationRuntimeAdapter` is the only bridge;
it can later be replaced by an IPC adapter without changing the public protocol.

Each production child is bound to exactly one `(profile_id, subject_id,
agent_id)` scope. Its state root is a hash-derived profile directory. Identity
values are propagated to Memory ownership, Persona/Affect/Relationship agent
scope, WorldOps timeline scope, and every turn state.

## 2. Starting from the Desktop Shell

Use `GatewayProcessSupervisor`; do not place the capability token in command-line
arguments, URLs, log fields, or persistent configuration.

```python
from sora_sandbox.conversation_gateway import GatewayProcessSupervisor

supervisor = GatewayProcessSupervisor()
child = supervisor.start(
    profile_id="profile-opaque-id",
    subject_id="user-opaque-id",
    agent_id="sora",
    client_instance_id="desktop-installation-id",
)

try:
    with child.client() as client:
        # Submit TurnSubmit.v1 and consume the returned event stream.
        ...
finally:
    supervisor.stop(child)
```

The supervisor launches an ephemeral loopback port, reads exactly one
`ConversationGatewayBootstrap.v1` JSON line from the child's stdout, polls
`/health/ready`, and requests authenticated drain/shutdown through the local
control plane. A process-group signal and forced kill are bounded fallbacks only.

Manual development launch:

```powershell
python -m sora_sandbox.conversation_gateway.cli `
  --profile-id profile-dev `
  --subject-id user-dev `
  --agent-id sora `
  --client-instance-id desktop-dev `
  --allow-missing-origin
```

The first stdout line contains a secret bootstrap token. Pipe it directly to the
Desktop Shell; do not paste it into tickets or console logs.

## 3. Authentication and transport

- Bind address is fixed to `127.0.0.1`; proxy headers are disabled.
- Every `/v1` call requires `Authorization: Bearer <capability>`.
- Tokens are opaque, short-lived, revocable, stored in memory as SHA-256 digests,
  and bound to profile, user, agent, client instance, permissions, and origin.
- Capability rotation is issue-new, switch-client, then revoke-old. These are
  random bearer capabilities rather than encrypted or signed tokens, so there is
  no cryptographic `key_id` to select; overlapping issuance provides zero-downtime
  rotation within the bounded TTL.
- Browser origins require exact allowlisting. Native clients must be explicitly
  issued an `allow_missing_origin` capability.
- WebSocket tokens are sent in headers, never query strings.
- `X-Sora-Protocol-Version: 1.0` is required. WebSocket uses subprotocol
  `sora.v1.0`.
- Request models are strict and reject unknown fields. Frames are capped at 64
  KiB by default.
- HTTP and WebSocket handshakes both enforce the configured Host allowlist.
- The service and profile-state repository require the same constructor-time
  identity scope; unscoped production construction is impossible.

Permissions are `stream.connect`, `turn.submit`, `turn.read`, `turn.cancel`,
`history.read`, and the process-owner-only `gateway.shutdown`. A missing or
foreign process capability is rejected before resource lookup. Within the bound
identity, a missing or foreign object is returned as not found so object
existence is not disclosed.

## 4. REST endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health/live` | Process liveness |
| `GET` | `/health/ready` | Runtime, registry, drain readiness |
| `POST` | `/v1/turns` | Idempotent turn submission |
| `GET` | `/v1/turns/{turn_id}` | Runtime/product status |
| `GET` | `/v1/turns/{turn_id}/events` | SSE replay and live continuation |
| `POST` | `/v1/turns/{turn_id}/cancel` | Cooperative cancellation |
| `GET` | `/v1/conversations/{id}/history` | Committed canonical short history |

`request_id` is the idempotency key inside one external conversation. Its
versioned fingerprint covers text, branch, and optional `client_message_id`.
Reusing the same request returns the original `turn_id`; changing a semantic
input returns `IDEMPOTENCY_CONFLICT`. Concurrent duplicates share a per-key
single-flight guard, while the Registry and Runtime retain independent SQLite
uniqueness constraints. `expected_conversation_version` provides
optimistic concurrency after the previous turn commits. Only one turn can write
one conversation at a time.

## 5. Event contract and reconnect

Gateway events have a durable, monotonically increasing `sequence`. Important
types are:

```text
turn.accepted
turn.progress
response.start
response.delta       payload.delta contains validated text
turn.committed
turn.cancelled
turn.failed
```

The Runtime remains authoritative for `accepted -> preparing -> generating ->
streaming -> committing -> committed`, plus cancellation and failure states.
Gateway maps a committed `response.completed` event to `turn.committed`.

SSE clients resume with `Last-Event-ID` or `after_sequence`. WebSocket clients
send `stream.resume` and may persist an `event.ack`. The client SDK drops any
event whose sequence is not greater than its cursor. Terminal events are
repairable from the Runtime status and are emitted exactly once even if a crash
occurs after the atomic commit but before client-event append.

The current production mode is buffered validated streaming: model output is not
visible until the disclosure and response validation gates complete.

An active stream has a five-minute default lifetime. At expiry the Gateway first
requests cooperative Runtime cancellation, waits a bounded cancellation grace
period, and then emits `TURN_TIMEOUT` and closes if the Runtime remains stuck.
The policy is deployment-configurable for longer-running text workloads.

## 6. Commit, cancellation, and recovery

The Runtime transaction commits these four items atomically:

1. final result;
2. canonical user/Sora short-history pair;
3. post-turn outbox records;
4. committed turn status.

Cancellation is cooperative and reaches the active provider. A cancelled or
failed turn cannot write partial history or canonical post-turn effects. Startup
recovery marks stale incomplete turns failed and repairs their terminal client
event. Gateway ownership/idempotency bindings are durable SQLite WAL records and
contain hashes and opaque IDs, never message text.

During shutdown, readiness changes to `not_ready`, new submissions receive
`DRAINING`, and existing turns receive a grace period before the process exits.

## 7. WebSocket behavior

After handshake, the server emits `connection.ready`. Supported commands are:

```text
turn.submit
turn.cancel
stream.resume
event.ack
ping
```

Each connection has a bounded outgoing queue. A slow consumer that remains full
beyond the grace window is disconnected with code `1013`. Resuming the same turn
replaces the earlier per-connection pump so duplicate server streams do not run
in parallel.

## 8. Observability and retention

Metrics use low-cardinality labels only. Profile, user, conversation, request,
turn, correlation, and client-instance IDs are rejected as metric labels. The
remaining label values are length-bounded and limited to 64 distinct values per
metric/label pair; excess values collapse into `__overflow__`. REST status and
history reads default to 600 requests/minute per profile, while SSE/WebSocket
opens default to 60/minute in addition to the active-connection cap. The
quality export includes readiness and latency/counter distributions. Gateway
event acknowledgements have a 30-day rolling retention; Runtime content follows
its own encrypted artifact, client-event, outbox, and dead-letter policies.

The Gateway registers `conversation_gateway` with Module 10. Change scopes
`gateway`, `conversation_gateway`, `live_text`, and `e2e` select the appropriate
Gateway and Runtime suites.

## 9. Release verification

```powershell
python -m unittest tests.test_conversation_gateway -v
python -m unittest tests.test_conversation_runtime -v
python -m unittest discover -s tests
```

The packaged `eval_data/golden_v1.jsonl` covers authentication, identity
isolation, protocol strictness, idempotency, SSE resume, terminal repair,
cancellation, history, concurrency, backpressure, drain, and restart recovery.
The test suite executes every packaged case through `run_gateway_cases`; schema
coverage validation alone is not considered a release signal.
`GatewayReleaseGate` produces an HMAC-signed release decision and blocks on auth,
idempotency, resume, terminal-delivery, history, or submit-ack regressions.

## 10. G1-G8 delivery map

| Segment | Implementation |
|---|---|
| G1 Contracts | strict Pydantic protocol, error taxonomy, version negotiation |
| G2 Security | scoped capability tokens, origin/host checks, quotas, no token URLs |
| G3 Transports | REST, SSE, WebSocket, heartbeat, backpressure |
| G4 Runtime parity | one composition root, Runtime Port, correlation propagation |
| G5 Reliability | durable ID map, idempotency, event repair, recovery, drain |
| G6 Observability | low-cardinality metrics, health, retention, quality export |
| G7 Desktop integration | typed client SDK and child-process supervisor |
| G8 Release | executable Golden Set, signed gate, runbook, full regression suite |
