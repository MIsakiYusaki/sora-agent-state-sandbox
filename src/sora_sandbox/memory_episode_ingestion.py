from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .memory_schema import (
    EpisodeStatus,
    ExtractionJobStatus,
    JsonDict,
    PreFilterDecision,
    json_dumps,
    new_id,
    utc_now,
)
from .memory_store import (
    delete_propagation_from_row,
    event_from_row,
    episode_from_row,
    extraction_job_from_row,
)


def _event_hash(
    *,
    source_type: str,
    actor: str,
    session_id: str,
    turn_id: str | None,
    payload: JsonDict,
) -> str:
    digest = hashlib.sha256()
    digest.update(source_type.encode("utf-8"))
    digest.update(actor.encode("utf-8"))
    digest.update(session_id.encode("utf-8"))
    digest.update((turn_id or "").encode("utf-8"))
    digest.update(json_dumps(payload).encode("utf-8"))
    return digest.hexdigest()


def _compact(value: str, limit: int = 220) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _plus_seconds(value: str, seconds: int) -> str:
    parsed = _parse_timestamp(value) or datetime.now(timezone.utc)
    return (parsed + timedelta(seconds=seconds)).isoformat()


def _decision_payload(decision: PreFilterDecision) -> JsonDict:
    return {
        "should_extract": decision.should_extract,
        "reason": decision.reason,
        "priority": decision.priority,
        "archived": decision.archived,
    }


@dataclass
class EpisodeIngestionService:
    """Normalizes raw turn events into replayable episodes and extraction jobs."""

    conn: sqlite3.Connection
    max_open_events: int = 12
    max_open_age_seconds: int = 30 * 60
    max_job_attempts: int = 3

    def ingest_event(
        self,
        *,
        source_type: str,
        actor: str,
        payload: JsonDict,
        session_id: str = "default",
        turn_id: str | None = None,
        episode_id: str | None = None,
        raw_ref: str | None = None,
        visibility: str = "private",
        retention_policy: str = "persistent",
        sensitivity_hint: str = "low",
        created_at: str | None = None,
    ) -> JsonDict:
        event_hash = _event_hash(
            source_type=source_type,
            actor=actor,
            session_id=session_id,
            turn_id=turn_id,
            payload=payload,
        )
        row = self.conn.execute(
            "SELECT * FROM memory_event WHERE event_hash = ?",
            (event_hash,),
        ).fetchone()
        if row:
            return event_from_row(row)

        event_id = new_id("evt")
        self.conn.execute(
            """
            INSERT INTO memory_event (
                id, source_type, actor, session_id, turn_id, episode_id,
                payload_json, raw_ref, visibility, retention_policy,
                sensitivity_hint, event_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                source_type,
                actor,
                session_id,
                turn_id,
                episode_id,
                json_dumps(payload),
                raw_ref,
                visibility,
                retention_policy,
                sensitivity_hint,
                event_hash,
                created_at or utc_now(),
            ),
        )
        return self.get_event(event_id)

    def ingest_turn(
        self,
        *,
        user_input: str,
        reply: str,
        session_id: str = "default",
        turn_id: str | None = None,
        episode_strategy: str = "turn",
        seal: bool | None = None,
        visibility: str = "private",
        retention_policy: str = "persistent",
        sensitivity_hint: str = "low",
        now: str | None = None,
    ) -> JsonDict:
        timestamp = now or utc_now()
        turn_id = turn_id or new_id("turn")
        user_event = self.ingest_event(
            source_type="conversation",
            actor="user",
            payload={"role": "user", "content": user_input},
            session_id=session_id,
            turn_id=turn_id,
            visibility=visibility,
            retention_policy=retention_policy,
            sensitivity_hint=sensitivity_hint,
            created_at=timestamp,
        )
        assistant_event = self.ingest_event(
            source_type="conversation",
            actor="sora",
            payload={"role": "assistant", "content": reply},
            session_id=session_id,
            turn_id=turn_id,
            visibility=visibility,
            retention_policy=retention_policy,
            sensitivity_hint="low",
            created_at=timestamp,
        )
        event_ids = [user_event["event_id"], assistant_event["event_id"]]
        topic_hint = self.topic_hint(user_input)
        should_seal = True if seal is None and episode_strategy == "turn" else bool(seal)
        pre_filter: JsonDict | None = None

        if episode_strategy == "session_window":
            episode = self.append_or_create_open_episode(
                session_id=session_id,
                event_ids=event_ids,
                topic_hint=topic_hint,
                gist=self.turn_gist(user_input, reply),
                now=timestamp,
            )
            if should_seal or self.should_seal_episode(episode, now=timestamp):
                episode = self.seal_episode(
                    episode["episode_id"],
                    reason="window_boundary",
                    now=timestamp,
                )
                episode, job, decision = self.enqueue_extraction_if_eligible(
                    episode["episode_id"],
                    reason="window_boundary",
                    now=timestamp,
                )
                pre_filter = _decision_payload(decision)
            else:
                job = None
        elif episode_strategy == "turn":
            episode = self.create_episode(
                session_id=session_id,
                event_ids=event_ids,
                topic_hint=topic_hint,
                gist=self.turn_gist(user_input, reply),
                status=EpisodeStatus.SEALED.value if should_seal else EpisodeStatus.OPEN.value,
                sealed_reason="turn_complete" if should_seal else None,
                now=timestamp,
            )
            if should_seal:
                episode, job, decision = self.enqueue_extraction_if_eligible(
                    episode["episode_id"],
                    reason="turn_complete",
                    now=timestamp,
                )
                pre_filter = _decision_payload(decision)
            else:
                job = None
        else:
            raise ValueError(f"unknown episode strategy: {episode_strategy}")

        return {
            "turn_id": turn_id,
            "episode": episode,
            "events": [
                self.get_event(user_event["event_id"]),
                self.get_event(assistant_event["event_id"]),
            ],
            "extraction_job": job,
            "pre_filter": pre_filter,
        }

    def append_or_create_open_episode(
        self,
        *,
        session_id: str,
        event_ids: list[str],
        topic_hint: str | None,
        gist: str,
        now: str | None = None,
    ) -> JsonDict:
        episode = self.find_open_episode(session_id=session_id, topic_hint=topic_hint, now=now)
        if episode is None:
            return self.create_episode(
                session_id=session_id,
                event_ids=event_ids,
                topic_hint=topic_hint,
                gist=gist,
                status=EpisodeStatus.OPEN.value,
                now=now,
            )
        return self.append_events_to_episode(episode["episode_id"], event_ids, gist=gist, now=now)

    def find_open_episode(
        self,
        *,
        session_id: str,
        topic_hint: str | None,
        now: str | None = None,
    ) -> JsonDict | None:
        row = self.conn.execute(
            """
            SELECT * FROM memory_episode
            WHERE session_id = ? AND status = ? AND deleted_at IS NULL
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (session_id, EpisodeStatus.OPEN.value),
        ).fetchone()
        if not row:
            return None
        episode = episode_from_row(row)
        if self.idle_boundary_changed(episode, now=now):
            self.seal_episode(episode["episode_id"], reason="idle_timeout", now=now)
            self.enqueue_extraction_if_eligible(episode["episode_id"], reason="idle_timeout", now=now)
            return None
        if self.topic_boundary_changed(episode.get("topic_hint"), topic_hint):
            self.seal_episode(episode["episode_id"], reason="topic_boundary", now=now)
            self.enqueue_extraction_if_eligible(episode["episode_id"], reason="topic_boundary", now=now)
            return None
        return episode

    def append_events_to_episode(
        self,
        episode_id: str,
        event_ids: list[str],
        *,
        gist: str,
        now: str | None = None,
    ) -> JsonDict:
        episode = self.get_episode(episode_id)
        merged_event_ids = [*episode["event_ids"], *event_ids]
        merged_gist = _compact(" | ".join(part for part in [episode["gist"], gist] if part), limit=420)
        timestamp = now or utc_now()
        self.conn.execute(
            """
            UPDATE memory_episode
            SET event_ids_json = ?, gist = ?, updated_at = ?
            WHERE id = ?
            """,
            (json_dumps(merged_event_ids), merged_gist, timestamp, episode_id),
        )
        self.conn.execute(
            "UPDATE memory_event SET episode_id = ? WHERE id IN (%s)"
            % ",".join("?" for _ in event_ids),
            [episode_id, *event_ids],
        )
        return self.get_episode(episode_id)

    def create_episode(
        self,
        *,
        session_id: str,
        event_ids: list[str],
        gist: str,
        topic_hint: str | None = None,
        status: str = "sealed",
        sealed_reason: str | None = None,
        now: str | None = None,
    ) -> JsonDict:
        timestamp = now or utc_now()
        episode_id = new_id("ep")
        sealed_at = timestamp if status == EpisodeStatus.SEALED.value else None
        self.conn.execute(
            """
            INSERT INTO memory_episode (
                id, session_id, status, topic_hint, gist, event_ids_json,
                sealed_reason, created_at, sealed_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                episode_id,
                session_id,
                status,
                topic_hint,
                gist,
                json_dumps(event_ids),
                sealed_reason,
                timestamp,
                sealed_at,
                timestamp,
            ),
        )
        self.conn.execute(
            "UPDATE memory_event SET episode_id = ? WHERE id IN (%s)"
            % ",".join("?" for _ in event_ids),
            [episode_id, *event_ids],
        )
        return self.get_episode(episode_id)

    def seal_episode(
        self,
        episode_id: str,
        *,
        reason: str = "manual_seal",
        now: str | None = None,
    ) -> JsonDict:
        episode = self.get_episode(episode_id)
        if episode["status"] in {
            EpisodeStatus.SEALED.value,
            EpisodeStatus.EXTRACTED.value,
            EpisodeStatus.ARCHIVED.value,
            EpisodeStatus.DELETED.value,
        }:
            return episode
        timestamp = now or utc_now()
        self.conn.execute(
            """
            UPDATE memory_episode
            SET status = ?, sealed_reason = ?, sealed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (EpisodeStatus.SEALED.value, reason, timestamp, timestamp, episode_id),
        )
        return self.get_episode(episode_id)

    def should_seal_episode(self, episode: JsonDict, *, now: str | None = None) -> bool:
        return len(episode.get("event_ids") or []) >= self.max_open_events

    def enqueue_extraction_if_eligible(
        self,
        episode_id: str,
        *,
        reason: str,
        now: str | None = None,
    ) -> tuple[JsonDict, JsonDict | None, PreFilterDecision]:
        episode = self.get_episode(episode_id)
        decision = self.prefilter_episode(episode, reason=reason)
        if not decision.should_extract:
            self.archive_episode(episode_id, reason=decision.reason, now=now)
            return self.get_episode(episode_id), None, decision
        job = self.enqueue_extraction(
            episode_id,
            reason=reason,
            priority=decision.priority,
            now=now,
        )
        return self.get_episode(episode_id), job, decision

    def archive_episode(
        self,
        episode_id: str,
        *,
        reason: str,
        now: str | None = None,
    ) -> JsonDict:
        episode = self.get_episode(episode_id)
        if episode["status"] in {EpisodeStatus.DELETED.value, EpisodeStatus.EXTRACTED.value}:
            return episode
        timestamp = now or utc_now()
        self.conn.execute(
            """
            UPDATE memory_episode
            SET status = ?, sealed_reason = ?, sealed_at = COALESCE(sealed_at, ?), updated_at = ?
            WHERE id = ?
            """,
            (EpisodeStatus.ARCHIVED.value, reason, timestamp, timestamp, episode_id),
        )
        return self.get_episode(episode_id)

    def prefilter_episode(self, episode: JsonDict, *, reason: str) -> PreFilterDecision:
        events = [self.get_event(event_id) for event_id in episode.get("event_ids", [])]
        texts = []
        for event in events:
            payload = event.get("payload") or {}
            content = str(payload.get("content") or "")
            if content:
                texts.append(content)
        text = " ".join(texts).strip()
        lowered = text.lower()
        compacted = " ".join(lowered.split())
        if not compacted:
            return PreFilterDecision(False, "empty_episode", priority=0, archived=True)

        explicit_markers = (
            "remember",
            "记住",
            "記住",
            "记忆",
            "記憶",
            "别忘",
            "不要忘",
            "偏好",
            "喜欢",
            "希望",
            "原则",
            "约定",
            "boundary",
            "preference",
        )
        if any(marker in lowered for marker in explicit_markers):
            return PreFilterDecision(True, f"{reason}:explicit_memory_signal", priority=95)

        domain_or_relationship_markers = (
            "memory",
            "episode",
            "extract",
            "recall",
            "architecture",
            "workflow",
            "project",
            "relationship",
            "companionship",
            "shared experience",
            "记忆",
            "经历",
            "架构",
            "流程",
            "项目",
            "关系",
            "陪伴",
            "共同经历",
        )
        if any(marker in lowered for marker in domain_or_relationship_markers):
            return PreFilterDecision(True, f"{reason}:domain_or_relationship_signal", priority=75)

        short_ack = {
            "ok",
            "okay",
            "thanks",
            "thank you",
            "noted",
            "got it",
            "好的",
            "好",
            "嗯",
            "收到",
            "可以",
        }
        if compacted in short_ack or (len(compacted) <= 16 and len(events) <= 2):
            return PreFilterDecision(False, "low_signal_ack_or_chitchat", priority=0, archived=True)

        if len(events) >= 4 or len(compacted) >= 80:
            return PreFilterDecision(True, f"{reason}:episode_has_enough_context", priority=55)
        return PreFilterDecision(False, "low_signal_episode", priority=0, archived=True)

    def enqueue_extraction(
        self,
        episode_id: str,
        *,
        reason: str,
        priority: int = 50,
        now: str | None = None,
    ) -> JsonDict:
        episode = self.get_episode(episode_id)
        row = self.conn.execute(
            "SELECT * FROM extraction_job WHERE episode_id = ? AND status IN (?, ?)",
            (episode_id, ExtractionJobStatus.PENDING.value, ExtractionJobStatus.RUNNING.value),
        ).fetchone()
        if row:
            return extraction_job_from_row(row)

        timestamp = now or utc_now()
        job_id = new_id("xjob")
        self.conn.execute(
            """
            INSERT INTO extraction_job (
                id, episode_id, status, reason, attempts, input_summary,
                event_ids_json, priority, created_at, updated_at, available_at
            ) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                episode_id,
                ExtractionJobStatus.PENDING.value,
                reason,
                episode["gist"],
                json_dumps(episode["event_ids"]),
                priority,
                timestamp,
                timestamp,
                timestamp,
            ),
        )
        return self.get_extraction_job(job_id)

    def start_extraction_job(self, job_id: str) -> JsonDict:
        job = self.get_extraction_job(job_id)
        if job["status"] != ExtractionJobStatus.PENDING.value:
            raise ValueError(f"extraction job is not pending: {job_id}")
        now = utc_now()
        self.conn.execute(
            """
            UPDATE extraction_job
            SET status = ?, attempts = attempts + 1, updated_at = ?, claimed_at = ?, error = NULL
            WHERE id = ?
            """,
            (ExtractionJobStatus.RUNNING.value, now, now, job_id),
        )
        return self.get_extraction_job(job_id)

    def claim_next_extraction_job(self, *, now: str | None = None) -> JsonDict | None:
        timestamp = now or utc_now()
        row = self.conn.execute(
            """
            SELECT * FROM extraction_job
            WHERE status = ?
              AND (available_at IS NULL OR available_at <= ?)
            ORDER BY priority DESC, created_at ASC
            LIMIT 1
            """,
            (ExtractionJobStatus.PENDING.value, timestamp),
        ).fetchone()
        if not row:
            return None
        return self.start_extraction_job(row["id"])

    def complete_extraction_job(
        self,
        job_id: str,
        *,
        result_summary: str | None = None,
    ) -> JsonDict:
        job = self.get_extraction_job(job_id)
        if job["status"] not in {ExtractionJobStatus.PENDING.value, ExtractionJobStatus.RUNNING.value}:
            raise ValueError(f"extraction job cannot complete from {job['status']}: {job_id}")
        now = utc_now()
        self.conn.execute(
            """
            UPDATE extraction_job
            SET status = ?, updated_at = ?, completed_at = ?, error = NULL, result_summary = ?
            WHERE id = ?
            """,
            (ExtractionJobStatus.COMPLETED.value, now, now, result_summary, job_id),
        )
        episode = self.get_episode(job["episode_id"])
        if episode["status"] not in {EpisodeStatus.DELETED.value, EpisodeStatus.ARCHIVED.value}:
            self.conn.execute(
                "UPDATE memory_episode SET status = ?, updated_at = ? WHERE id = ?",
                (EpisodeStatus.EXTRACTED.value, now, job["episode_id"]),
            )
        return self.get_extraction_job(job_id)

    def fail_extraction_job(
        self,
        job_id: str,
        *,
        error: str,
        retry: bool = False,
        retry_delay_seconds: int = 60,
    ) -> JsonDict:
        job = self.get_extraction_job(job_id)
        if job["status"] not in {ExtractionJobStatus.PENDING.value, ExtractionJobStatus.RUNNING.value}:
            raise ValueError(f"extraction job cannot fail from {job['status']}: {job_id}")
        now = utc_now()
        if retry and job["attempts"] < self.max_job_attempts:
            self.conn.execute(
                """
                UPDATE extraction_job
                SET status = ?, updated_at = ?, available_at = ?, error = ?
                WHERE id = ?
                """,
                (
                    ExtractionJobStatus.PENDING.value,
                    now,
                    _plus_seconds(now, retry_delay_seconds),
                    error,
                    job_id,
                ),
            )
            return self.get_extraction_job(job_id)
        self.conn.execute(
            "UPDATE extraction_job SET status = ?, updated_at = ?, error = ? WHERE id = ?",
            (ExtractionJobStatus.FAILED.value, now, error, job_id),
        )
        return self.get_extraction_job(job_id)

    def cancel_extraction_jobs_for_episode(self, episode_id: str, *, reason: str) -> list[JsonDict]:
        now = utc_now()
        rows = self.conn.execute(
            "SELECT * FROM extraction_job WHERE episode_id = ? AND status IN (?, ?)",
            (episode_id, ExtractionJobStatus.PENDING.value, ExtractionJobStatus.RUNNING.value),
        ).fetchall()
        for row in rows:
            self.conn.execute(
                "UPDATE extraction_job SET status = ?, updated_at = ?, error = ? WHERE id = ?",
                (ExtractionJobStatus.CANCELLED.value, now, reason, row["id"]),
            )
        return [
            self.get_extraction_job(row["id"])
            for row in rows
        ]

    def propagate_delete(self, source_ref: str, *, reason: str = "source_deleted") -> JsonDict:
        event = self._resolve_event(source_ref)
        now = utc_now()
        self.conn.execute(
            "UPDATE memory_event SET deleted_at = ?, delete_reason = ? WHERE id = ?",
            (now, reason, event["event_id"]),
        )
        self.record_delete_propagation(
            source_ref=source_ref,
            source_type="memory_event",
            target_type="memory_event",
            target_id=event["event_id"],
            action="soft_delete",
            reason=reason,
        )
        episode_id = event.get("episode_id")
        cancelled_jobs: list[JsonDict] = []
        episode = None
        if episode_id:
            active_events = self.conn.execute(
                """
                SELECT COUNT(*) FROM memory_event
                WHERE episode_id = ? AND deleted_at IS NULL
                """,
                (episode_id,),
            ).fetchone()[0]
            if active_events == 0:
                self.conn.execute(
                    """
                    UPDATE memory_episode
                    SET status = ?, deleted_at = ?, delete_reason = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (EpisodeStatus.DELETED.value, now, reason, now, episode_id),
                )
                self.record_delete_propagation(
                    source_ref=source_ref,
                    source_type="memory_event",
                    target_type="memory_episode",
                    target_id=episode_id,
                    action="soft_delete",
                    reason=reason,
                )
                cancelled_jobs = self.cancel_extraction_jobs_for_episode(episode_id, reason=reason)
                for job in cancelled_jobs:
                    self.record_delete_propagation(
                        source_ref=source_ref,
                        source_type="memory_event",
                        target_type="extraction_job",
                        target_id=job["job_id"],
                        action="cancel",
                        reason=reason,
                    )
            episode = self.get_episode(episode_id)
        return {
            "deleted_event": self.get_event(event["event_id"]),
            "episode": episode,
            "cancelled_extraction_jobs": cancelled_jobs,
        }

    def record_delete_propagation(
        self,
        *,
        source_ref: str,
        source_type: str,
        target_type: str,
        target_id: str,
        action: str,
        reason: str,
        metadata: JsonDict | None = None,
    ) -> JsonDict:
        propagation_id = new_id("dp")
        self.conn.execute(
            """
            INSERT INTO delete_propagation (
                id, source_ref, source_type, target_type, target_id,
                action, reason, created_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                propagation_id,
                source_ref,
                source_type,
                target_type,
                target_id,
                action,
                reason,
                utc_now(),
                json_dumps(metadata or {}),
            ),
        )
        row = self.conn.execute(
            "SELECT * FROM delete_propagation WHERE id = ?",
            (propagation_id,),
        ).fetchone()
        return delete_propagation_from_row(row)

    def _resolve_event(self, source_ref: str) -> JsonDict:
        row = self.conn.execute("SELECT * FROM memory_event WHERE id = ?", (source_ref,)).fetchone()
        if row:
            return event_from_row(row)
        row = self.conn.execute("SELECT * FROM memory_event WHERE raw_ref = ?", (source_ref,)).fetchone()
        if row:
            return event_from_row(row)
        raise ValueError(f"memory event source not found: {source_ref}")

    def get_event(self, event_id: str) -> JsonDict:
        row = self.conn.execute("SELECT * FROM memory_event WHERE id = ?", (event_id,)).fetchone()
        if not row:
            raise ValueError(f"memory event not found: {event_id}")
        return event_from_row(row)

    def get_episode(self, episode_id: str) -> JsonDict:
        row = self.conn.execute("SELECT * FROM memory_episode WHERE id = ?", (episode_id,)).fetchone()
        if not row:
            raise ValueError(f"memory episode not found: {episode_id}")
        return episode_from_row(row)

    def get_extraction_job(self, job_id: str) -> JsonDict:
        row = self.conn.execute("SELECT * FROM extraction_job WHERE id = ?", (job_id,)).fetchone()
        if not row:
            raise ValueError(f"extraction job not found: {job_id}")
        return extraction_job_from_row(row)

    def list_events(self, *, episode_id: str | None = None, limit: int = 50) -> list[JsonDict]:
        if episode_id:
            rows = self.conn.execute(
                "SELECT * FROM memory_event WHERE episode_id = ? ORDER BY created_at ASC LIMIT ?",
                (episode_id, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM memory_event ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [event_from_row(row) for row in rows]

    def list_episodes(self, *, status: str | None = None, limit: int = 50) -> list[JsonDict]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM memory_episode WHERE status = ? ORDER BY updated_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM memory_episode ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [episode_from_row(row) for row in rows]

    def list_extraction_jobs(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> list[JsonDict]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM extraction_job WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM extraction_job ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [extraction_job_from_row(row) for row in rows]

    def list_delete_propagations(
        self,
        *,
        source_ref: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        limit: int = 100,
    ) -> list[JsonDict]:
        clauses: list[str] = []
        params: list[Any] = []
        if source_ref:
            clauses.append("source_ref = ?")
            params.append(source_ref)
        if target_type:
            clauses.append("target_type = ?")
            params.append(target_type)
        if target_id:
            clauses.append("target_id = ?")
            params.append(target_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self.conn.execute(
            f"""
            SELECT * FROM delete_propagation
            {where}
            ORDER BY created_at ASC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [delete_propagation_from_row(row) for row in rows]

    def get_episode_trace(self, episode_id: str) -> JsonDict:
        episode = self.get_episode(episode_id)
        jobs = self.conn.execute(
            "SELECT * FROM extraction_job WHERE episode_id = ? ORDER BY created_at ASC",
            (episode_id,),
        ).fetchall()
        events = self.list_events(episode_id=episode_id, limit=100)
        target_ids = [episode_id]
        target_ids.extend(event["event_id"] for event in events)
        target_ids.extend(row["id"] for row in jobs)
        placeholders = ",".join("?" for _ in target_ids)
        propagation_rows = self.conn.execute(
            f"""
            SELECT * FROM delete_propagation
            WHERE target_id IN ({placeholders}) OR source_ref IN ({placeholders})
            ORDER BY created_at ASC
            """,
            [*target_ids, *target_ids],
        ).fetchall()
        return {
            "episode": episode,
            "events": events,
            "extraction_jobs": [extraction_job_from_row(row) for row in jobs],
            "delete_propagations": [
                delete_propagation_from_row(row) for row in propagation_rows
            ],
        }

    @staticmethod
    def topic_hint(user_input: str) -> str | None:
        words = [part for part in user_input.replace(":", " ").replace(",", " ").split() if part]
        if not words:
            return None
        return _compact(" ".join(words[:8]), limit=80)

    @staticmethod
    def topic_boundary_changed(previous: str | None, current: str | None) -> bool:
        if not previous or not current:
            return False
        previous_tokens = set(previous.lower().split())
        current_tokens = set(current.lower().split())
        if not previous_tokens or not current_tokens:
            return False
        overlap = len(previous_tokens & current_tokens) / max(len(previous_tokens | current_tokens), 1)
        return overlap < 0.15

    def idle_boundary_changed(self, episode: JsonDict, *, now: str | None = None) -> bool:
        current = _parse_timestamp(now or utc_now())
        updated_at = _parse_timestamp(episode.get("updated_at"))
        if current is None or updated_at is None:
            return False
        return (current - updated_at).total_seconds() >= self.max_open_age_seconds

    @staticmethod
    def turn_gist(user_input: str, reply: str) -> str:
        if reply:
            return _compact(f"user: {user_input} | sora: {reply}")
        return _compact(f"user: {user_input}")
