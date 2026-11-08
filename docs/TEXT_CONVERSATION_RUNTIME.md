# Project Sora Text Conversation Runtime

This document describes the implemented runtime boundary for the text-input to
text-output path. The canonical product design remains in Feishu; this file is
the operational contract that lives with the code.

## Runtime boundary

`ConversationRuntime` owns turn acceptance, request idempotency, per-conversation
execution leases, cancellation, model-call budgets, encrypted journaling, client
events, atomic turn commit, post-turn outbox delivery, and Module 10 quality
registration. `AgentRunner` remains a compatibility planner for Frames, Signals,
arbitration, output guards, and expression routing.

The runtime installs `ConversationContextPacker.pack_for_generation` as a
mandatory runner gateway. The packed context carries a versioned
`GenerationContextGate.v1` marker. A runner result without that marker is
rejected before client output or commit, which makes disclosure and token
packing a fail-closed pre-generation contract. The response route receives
`response_reserve` as its hard `max_tokens` value.

When the runtime wraps `AgentRunner`, domain observation and memory writes are
disabled on the response hot path. A committed `TurnCommittedEvent.v1` is written
to the same SQLite transaction as the final result and short conversation
history. Memory, Affect, Relationship, and WorldOps consume that event through
the idempotent post-turn dispatcher.

## Storage and privacy

The default database is `.sandbox/conversation_runtime.db`. SQLite runs in WAL
mode with full synchronous durability, foreign keys, a 30-second busy timeout,
and explicit `BEGIN IMMEDIATE` transactions.

User input, replies, result artifacts, context artifacts, client deltas, short
history, and outbox payloads are encrypted with AES-256-GCM. On Windows, the
installation key is protected by DPAPI in
`.sandbox/conversation_runtime.key`. Deployments may inject a base64 32-byte key
through `SORA_CONVERSATION_MASTER_KEY`. Plaintext history from the legacy state
file is migrated once and removed from the persisted compatibility state.

Ciphertext envelopes contain a `key_id`, and decryption resolves that exact key
instead of silently trying the current key. This permits a local DPAPI key and
an environment-provided key to coexist during rotation and produces an explicit
`KeyMismatchError` when a historical key is unavailable. On macOS and Linux,
the installation key is stored through the OS keyring. The runtime fails closed
when no keyring is available; it no longer writes a plaintext master-key file.

Default retention is seven days for raw Wave A/Wave B artifacts, thirty days
for reconnectable client events and delivered outbox records, and ninety days
for dead letters. Call `runtime.maintain()` from scheduled maintenance. Hashed
turn audit metadata remains available.

## Model routing

Every foreground turn has at most three logical model calls. The final slot is
reserved for response generation. Clear turns use deterministic interpretation;
ambiguous references may use the interpreter. Semantic arbitration is advisory
and degrades to rules when its budget is unavailable.

Interpreter, semantic arbiter, and bounded interruptibility resolution always
send `thinking={"type":"disabled"}`. Response generation uses the response
route. `SORA_FAST_MODEL` may select a separate low-latency model; otherwise the
configured response model is reused in non-thinking mode for fast tasks.

The DeepSeek adapter supports official SSE streaming and provider cancellation.
Model output is buffered until the existing deterministic output guards have
validated it, then exposed as ordered `response.delta` events. This avoids
showing content that a later guard would have to retract.

Buffered foreground response streams must also provide a recognized normal
terminal reason (`stop`, `end_turn` or `stop_sequence`, case-insensitive).
Reported length limits, blocked/tool-continuation results, unknown reasons,
missing terminal evidence, conflicting terminals and text after a terminal fail
with `IncompleteModelResponse` before any client response delta or successful
history/outbox commit. A trailing usage-only event is still consumed; reported
usage is preserved on the failed runtime model-call record. Token counts alone
never determine truncation. EOF cancellation is checked before accepting the
buffer. This does not change budgets, add retries or expose provider text in
errors. Non-streaming compatibility delegates are outside this stream contract.

The model-call journal preserves provider-reported token usage exactly. Missing
usage remains `NULL` and is projected to Developer Mode as unreported rather
than being fabricated as zero. Schema v2 migrates ambiguous zero values from
older journals to `NULL`; an explicit zero reported after migration remains a
real zero. Input/output token aliases are accepted without replacing a primary
explicit zero. DDL, table replacement and version writes share one transaction;
an interruption rolls back the upgrade, and reopening safely retries it.

## Calling the runtime

Synchronous compatibility API:

```python
result = runtime.run(
    state,
    "我们继续刚才的话题",
    request_id="client-generated-stable-id",
)
print(result["reply"])
```

Reconnectable event stream:

```python
for event in runtime.stream_turn(state, "你好", request_id="request-42"):
    send_to_client(event.to_dict())

missed = runtime.reconnect(turn_id, after_sequence=last_seen_sequence)
```

The client must reuse the same `request_id` when retrying an uncertain request.
Reusing it with different input is rejected. A committed duplicate is replayed
without another model call or domain side effect.

Cancel the active turn with:

```python
runtime.cancel(conversation_id, reason="user_cancelled")
```

Cancellation is cooperative between stages and closes the provider stream when
generation is active. An uncommitted working-state copy is discarded. Validated
text already emitted to the client is encrypted as an `aborted_response`
artifact and can be retrieved with `runtime.aborted_response(turn_id)`. Provider
buffer text that had not passed output guards is audit-only and is never exposed
through that API.

`runtime.run_proactive(...)` is deliberately unavailable in this text-first
release and raises `NotImplementedError`. The schema value remains reserved for
a later initiative-lease release.

## Operations

- `runtime.health()` reports schema/WAL status, outbox backlog, dead letters,
  worker state, and the foreground model-call ceiling.
- `runtime.recover()` marks stale interrupted turns as failed while preserving
  their encrypted audit trail. A live matching execution lease excludes a turn;
  both startup and periodic Gateway calls use the neutral `stale_recovery` stage.
  Nonterminal status, stale timestamp and matching live lease are checked again
  inside the failure write transaction. Terminal turns cannot acquire or renew
  execution leases.
  Recovery never guesses whether a provider side effect completed. Clients
  reload authoritative history before deciding whether to submit a new request;
  the desktop does not automatically resend a retained draft.
- `runtime.post_turn.process_once()` supports deterministic worker execution in
  jobs and tests.
- `runtime.export_quality_report()` exposes low-cardinality metrics, health, and
  Golden Set validation.
- `runtime.register_quality_ops(memory_runtime)` adds the runtime suite and
  export section to the Module 10 registry and release-gate scope.

Outbox delivery is at-least-once. Consumer receipts make each
`consumer_name + event_id` pair idempotent. Memory, Affect, Relationship, and
WorldOps use separate consumer identities, so one domain failure does not block
the others and successful domains are skipped on retry. Failed delivery uses
exponential backoff and enters `dead_letter` after eight attempts.

## Release verification

The packaged, versioned Golden Set is
`src/sora_sandbox/conversation_runtime/eval_data/golden_v1.jsonl`. It includes basic reply,
idempotency, explicit control, disclosure, ambiguous reference, correction,
cancellation, provider failure, and concurrency cases.

Run the full local verification suite:

```powershell
python -m unittest discover -s tests -p "test_*.py" -q
```

`ConversationReleaseGate` evaluates E2E success, idempotency, control
compliance, disclosure violations, commit atomicity, and p95 latency. Decisions
are HMAC-signed. A break-glass override requires an approver, reason, and future
expiry; it never changes the underlying failed checks.

`run_golden_set(runtime_factory, state_factory, path)` executes the complete
trajectory set against isolated runtime instances. The factory selects normal
or fault-injection providers from each case's `policy_profile`; the harness runs
multi-turn, cancellation, idempotency, provider-failure, and concurrency paths,
evaluates expectations, closes every runtime, and returns per-case evidence plus
release metrics. `validate_golden_set()` remains the fast schema/category check
and must not be treated as an E2E result.

Low-cardinality metrics include context degradation, stale domain snapshots,
provider cancellation waste, and outbox dead letters. Identifiers such as turn,
request, correlation, conversation, and user IDs are filtered from metric
labels.
