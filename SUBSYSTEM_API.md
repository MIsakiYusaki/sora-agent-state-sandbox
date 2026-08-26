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

## Affect Runtime Boundary

Affect owns one global internal state per Sora instance. Per-user and per-topic
differences live in typed emotion episodes and target contexts; they do not
fork the canonical core state. Runtime integration uses four contracts:

| Contract | Direction | Semantics |
| --- | --- | --- |
| `AffectStimulusEvent.v2` | Orchestrator/structured systems -> Affect | Evidence-bearing stimulus; raw text is transient and encrypted. |
| `AffectFrame.v2` | Affect -> Agent Runtime | Sanitized bands, expression bias, regulation limits, and consent policy. |
| `AffectSignal.v1` | Affect -> Arbiter | Bounded expression/regulation candidate; never an interruption permission. |
| `MemoryCandidateProposal.v1` | Affect -> Memory Gate | Qualified episode proposal only; never a direct canonical write or Module 09 projection. |

The synchronous lane contains deterministic classification, bounded dynamics,
SQLite commit, and frame generation only. Semantic LLM appraisal is asynchronous
and forward-only: it cannot rewrite an emitted response, calculate canonical
intensity, mutate policy, or invent evidence. Failure keeps the deterministic
state and degrades to the last verified or neutral-safe frame.

The persistence contract is `affect_schema.v4`: a single serialized writer owns
canonical transactions, thread-local query-only connections serve runtime reads,
and opaque evidence-reference indexes support bounded Forget/source lookups.
Transient semantic text is protected with DPAPI or AES-GCM and is removed after
terminal processing. Provider traces may contain enum-only content risk
indicators, but never the source text.

`AffectFrame.v2` deliberately omits exact intensity, VAD values, capacity values,
and evidence references. If memory recall receives Affect context, it receives
the domain-neutral `RecallContextFeatures.v1` contract. Memory applies at most a
10% relative score boost and a diversity guard at `top_k >= 4`; base semantic
relevance and governance remain authoritative.

Forget and rollback deactivate append-only contributions and deterministically
rebuild snapshots. Affect-generated memory candidates are rejected or forgotten
when their source is forgotten. Memory tombstones can also notify Affect through
the registered memory-change listener when that memory was an Affect source.

## Model Provider and BYOK Boundary

`ModelProviderSystem` is the only component allowed to combine a provider
credential with an outbound model request. Domain systems receive a routed LLM,
embedding, or rerank client and never read API keys. Provider metadata, verified
capabilities, workload bindings, probes, invocation traces, and audit events live
in `provider.db`; secret material lives only in the operating-system credential
store. There is no plaintext fallback.

The runtime routes by typed workload rather than by a global model name. The
supported workloads include foreground interpretation and generation, Memory,
Persona, Affect, Relationship, WorldOps, document/query embedding, and Memory
reranking. A turn pins one immutable `RoutingSnapshot`, so a settings change
cannot switch providers halfway through a response.

The Desktop Shell configures BYOK through the local Conversation Gateway:

| Method | Path | Capability | Purpose |
| --- | --- | --- | --- |
| `GET` | `/v1/model-providers` | `model_provider.read` | Read redacted profiles, models, and bindings. |
| `POST` | `/v1/model-providers` | `model_provider.write` | Create provider metadata without a secret. |
| `PUT` | `/v1/model-providers/{id}/credential` | `model_provider.write` | Save a key or bounded custom auth headers. |
| `POST` | `/v1/model-providers/{id}/verify` | `model_provider.verify` | Probe and persist measured model capabilities. |
| `PUT` | `/v1/model-provider-bindings/{workload}` | `model_provider.bind` | Atomically bind a verified model and explicit fallbacks. |
| `POST` | `/v1/model-providers/{id}/credential/rotate` | `model_provider.write` | Verify a new secret before deleting the previous one. |
| `GET` | `/v1/model-providers/health` | `model_provider.read` | Report registry and active-route credential health. |
| `POST` | `/v1/model-providers/maintenance` | `model_provider.write` | Apply trace, probe, and audit retention. |
| `DELETE` | `/v1/model-providers/{id}` | `model_provider.delete` | Delete after an exact `delete:{id}` confirmation. |

The setup transaction is deliberately ordered:

```text
create draft profile
  -> store credential in OS keyring
  -> capability probe
  -> bind selected workloads
  -> activate on the next turn snapshot
```

Profiles for OpenAI, DeepSeek, Anthropic, and Gemini must use their canonical API
host unless the user explicitly marks the endpoint as custom. Remote endpoints
require HTTPS, public DNS answers, and an allowed port; local endpoints are
loopback-only. Redirects are never followed, environment proxy inheritance is
disabled, response sizes are bounded, and errors/traces are redacted.

The first startup with a complete legacy DeepSeek configuration migrates it to a
provider profile and the OS keyring, then creates the missing workload bindings.
Legacy environment variables remain a one-way bootstrap source, not the runtime
credential store. An incomplete BYOK draft never breaks an otherwise valid
runtime; only a missing credential on an active route degrades readiness.
