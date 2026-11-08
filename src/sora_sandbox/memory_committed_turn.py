from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .memory_schema import JsonDict, json_dumps, utc_now
from .memory_gate import detect_sensitivity

if TYPE_CHECKING:
    from .memory_runtime import MemoryRuntime


@dataclass
class CommittedTurnIngestion:
    """Persist the source receipt with events, rule candidates and the job atomically.

    Gate admission is a resumable second step; the receipt contains identifiers,
    never another copy of conversation text. Keep it after source deletion so an
    outbox replay cannot resurrect a forgotten episode.
    """

    runtime: MemoryRuntime

    def observe(self, event: JsonDict) -> JsonDict:
        runtime = self.runtime
        payload = event.get("payload")
        if event.get("event_type") != "conversation.turn_committed" or not isinstance(payload, dict):
            raise ValueError("memory requires a committed-turn event")
        source_id = self._required(event.get("event_id"), "source_event_id")
        conversation_id = self._required(payload.get("conversation_id"), "conversation_id")
        turn_id = self._required(payload.get("runtime_turn_id") or event.get("turn_id"), "runtime_turn_id")
        if event.get("turn_id") and str(event["turn_id"]) != turn_id:
            raise ValueError("memory source turn identity mismatch")
        for key, expected in (("user_id", runtime.owner_user_id), ("agent_id", runtime.agent_id)):
            if payload.get(key) and payload[key] != expected:
                raise ValueError(f"memory source {key} mismatch")
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                           separators=(",", ":")).encode()).hexdigest()
        identity = {"source_event_id": source_id, "conversation_id": conversation_id,
                    "turn_id": turn_id, "correlation_id": payload.get("correlation_id"),
                    "owner_user_id": runtime.owner_user_id, "agent_id": runtime.agent_id}
        user_input, reply = str(payload.get("user_input") or ""), str(payload.get("reply") or "")
        with runtime._lock:
            conn = runtime._conn
            with conn:
                row = conn.execute(
                    """SELECT * FROM memory_turn_receipt WHERE source_event_id = ?
                       OR (owner_user_id = ? AND agent_id = ? AND conversation_id = ? AND turn_id = ?)""",
                    (source_id, runtime.owner_user_id, runtime.agent_id, conversation_id, turn_id),
                ).fetchone()
                if row:
                    if (row["payload_hash"] != digest or row["owner_user_id"] != runtime.owner_user_id
                            or row["agent_id"] != runtime.agent_id or row["conversation_id"] != conversation_id
                            or row["turn_id"] != turn_id):
                        raise ValueError("memory committed event replay conflict")
                    source_id = row["source_event_id"]
                    result = json.loads(row["result_json"])
                else:
                    result = {"source_event_id": source_id, "conversation_id": conversation_id,
                              "turn_id": turn_id, "candidate_ids": [], "candidate_count": 0,
                              "response_plan_candidate": None, "episode_id": None, "job_id": None}
                    status = "completed"
                    if user_input.strip() and detect_sensitivity(user_input + "\n" + reply) != "forbidden":
                        trace = runtime.ingestion.ingest_turn(
                            user_input=user_input, reply=reply, session_id=conversation_id,
                            turn_id=turn_id, source_event_id=source_id,
                        )
                        trace["source_identity"] = identity
                        candidates = runtime.observe_turn_candidates(
                            user_input, reply, ingest_trace=trace, source_key=source_id, _commit=False,
                        )
                        hook = runtime.observe_response_plan(
                            dict(payload.get("response_plan") or {}), source_key=source_id,
                            source_identity=identity, _commit=False,
                        )
                        ids = [c["candidate_id"] for c in candidates if c.get("candidate_id")]
                        if hook and hook.get("candidate_id"):
                            ids.append(hook["candidate_id"])
                        result.update(candidate_ids=ids, candidate_count=len(candidates),
                                      response_plan_candidate=hook.get("candidate_id") if hook else None,
                                      episode_id=trace["episode"]["episode_id"],
                                      job_id=(trace["extraction_job"] or {}).get("job_id"))
                        status = "ingested"
                    else:
                        result["suppressed"] = "empty_or_forbidden_source"
                    conn.execute(
                        """INSERT INTO memory_turn_receipt
                           (source_event_id, payload_hash, owner_user_id, agent_id, conversation_id,
                            turn_id, correlation_id, episode_id, status, result_json, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (source_id, digest, runtime.owner_user_id, runtime.agent_id, conversation_id,
                         turn_id, identity["correlation_id"], result["episode_id"], status,
                         json_dumps(result), utc_now()),
                    )
        # Admission can be retried independently without regenerating rule output.
        for candidate_id in result["candidate_ids"]:
            with runtime._lock:
                with conn:
                    row = conn.execute("SELECT status FROM memory_turn_receipt WHERE source_event_id = ?",
                                       (source_id,)).fetchone()
                    if row["status"] != "completed" and self._source_active(result["episode_id"]):
                        runtime.run_candidate_gate(candidate_id, actor="committed_turn")
        with runtime._lock:
            conn.execute("UPDATE memory_turn_receipt SET status = 'completed' WHERE source_event_id = ?",
                         (source_id,))
            conn.commit()
        return result

    def _source_active(self, episode_id: str | None) -> bool:
        if not episode_id:
            return False
        events = self.runtime.ingestion.list_events(episode_id=episode_id)
        return bool(events) and all(not item.get("deleted_at") for item in events)

    @staticmethod
    def _required(value: object, name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"memory source requires {name}")
        return value
