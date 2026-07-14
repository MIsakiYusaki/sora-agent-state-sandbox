# Sora Subsystem Adapter Contract

The orchestrator treats Memory, Persona, WorldOps, Behavior, Relationship, and
other capabilities as replaceable subsystem adapters.

## Runtime Boundary

```text
Event Bus
  -> TurnContext
  -> SubsystemRegistry
      -> Local Function Adapter
      -> HTTP Service Adapter
  -> validated Signals
  -> Signal Pool / Arbiter
  -> Response Planner
```

Subsystems do not call each other directly. They observe the same immutable
`TurnContext` and return standard Signals. This keeps their storage, models,
deployment model, and release cycle independent from the orchestrator.

## Python Adapter

An adapter implements:

```python
class SubsystemAdapter:
    name: str
    timeout_ms: int

    def evaluate(self, context: TurnContext) -> list[dict]: ...
    def propose_updates(self, context: TurnContext) -> list[dict]: ...
    def commit(self, candidate_ids: list[str]) -> dict: ...
    def health(self) -> dict: ...
```

Use `FunctionSubsystemAdapter` for an in-process module. Use
`HttpSubsystemAdapter` for an independently deployed service.

To replace only Persona while retaining the other defaults:

```python
from sora_sandbox.agent import AgentRunner, build_default_subsystem_registry
from sora_sandbox.subsystems import HttpSubsystemAdapter

registry = build_default_subsystem_registry()
registry.register(
    HttpSubsystemAdapter(
        name="persona",
        base_url="http://127.0.0.1:8102",
        timeout_ms=500,
    ),
    replace=True,
)
runner = AgentRunner(llm, subsystem_registry=registry)
```

## HTTP Endpoints

`HttpSubsystemAdapter` uses JSON `POST` requests:

| Endpoint | Request | Response |
| --- | --- | --- |
| `/evaluate` | `{"context": TurnContext}` | `{"signals": [...]}` |
| `/propose-updates` | `{"context": TurnContext}` | `{"candidates": [...]}` |
| `/commit` | `{"candidate_ids": [...]}` | subsystem-defined commit result |
| `/health` | `{}` | subsystem health object |

The HTTP context excludes the complete local state by default. A service
receives the event bus, session context, turn frame, context policy, and its
prepared frames.

## Signal Contract

Every Signal must contain:

```json
{
  "id": "sig_memory_relevant_memory_12",
  "source": "memory",
  "type": "relevant_memory",
  "priority": 0.72,
  "confidence": 0.86,
  "visibility": "candidate"
}
```

The Registry validates required fields and verifies that `source` matches the
adapter name. Invalid output degrades that subsystem for the turn.

## Failure Policy

- Adapters are evaluated concurrently.
- Every adapter owns a timeout.
- Timeout, exception, or invalid Signal affects only that subsystem.
- The remaining Signals continue to Arbiter.
- `context_pack.subsystem_health` records status, latency, adapter type,
  signal count, and degradation detail.
- `/subsystems` displays the latest health report.

## State Mutation Boundary

`evaluate()` is read-only. Long-term changes follow:

```text
propose_updates()
  -> pending StateCandidate
  -> policy/manual approval
  -> commit(candidate_ids)
```

Memory and Persona writes remain candidate-first. Persona is now attached as an
independent canonical subsystem rather than a mutable section of `state.json`.

## Persona Projection Boundary

Committed Memory records with type `persona_delta` or `profile` are delivered by
Module 09 as `ProjectionEvent.v1`. `PersonaProjectionAdapter` persists the event
to the Persona inbox and returns `ProjectionAck.v1` immediately. It does not call
an LLM on the request path.

```text
Memory canonical commit
  -> Module 09 projector outbox
  -> PersonaProjectionAdapter (idempotent inbox)
  -> async real-LLM proposal provider
  -> deterministic Persona Gate and bounded delta
  -> append-only contribution
  -> PersonaSnapshot.v1
  -> PersonaFrame.v1 + PersonaUsePolicy.v1
```

Runtime reads use `PersonaFrame.v1`. `PersonaUsePolicy.v1` applies the most
restrictive Interruptibility and consent constraints before the frame reaches
the response planner. Relationship, Affect, WorldOps, and user-specific
adaptation remain separate ownership domains.
