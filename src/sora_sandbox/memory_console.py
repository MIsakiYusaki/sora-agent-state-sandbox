from __future__ import annotations

import json
import shlex
from typing import Any

from .memory_runtime import MemoryRuntime


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _clean(value: str) -> str:
    return value.strip().strip('"')


def _flag_int(args: list[str], flag: str, *, default: int) -> int:
    if flag not in args:
        return default
    index = args.index(flag)
    if index + 1 >= len(args):
        raise ValueError(f"Usage: {flag} <number>")
    return int(_clean(args[index + 1]))


class MemoryConsole:
    """Small administrative console for the in-process memory runtime."""

    def __init__(self, runtime: MemoryRuntime) -> None:
        self.runtime = runtime

    @staticmethod
    def banner() -> str:
        return (
            "Memory Console（本地 SQLite）\n"
            "输入 help 查看命令，输入 back 返回 Sora 主界面。"
        )

    @staticmethod
    def help_text() -> str:
        return """Memory Console 命令

  list [active|disabled|deleted|superseded|all]
  candidates [pending|needs_review|deferred|committed|rejected|all]
  show <memory_id>
  search <关键词>
  add <内容> [--type preference] [--tags tag1,tag2]
  gate <candidate_id>
  batch-gate <candidate_id> [candidate_id...]
  decisions [candidate_id] [--limit N]
  reviews [open|resolved|all] [--limit N]
  approve-review <review_id>
  reject-review <review_id> [reason]
  defer-review <review_id> [--days N]
  quarantines [active|all] [--limit N]
  expire-deferred
  process-expired-deferrals
  commit <candidate_id>
  reject <candidate_id>
  rollback-candidate <candidate_id> [reason]
  revise <memory_id> <新内容>
  disable <memory_id>
  delete <memory_id>
  restore <memory_id>
  evidence <memory_id>
  episodes [open|sealed|extracted|archived|deleted|all]
  events [episode_id]
  jobs [pending|running|completed|failed|cancelled|all]
  episode-trace <episode_id>
  replay-episode <episode_id>
  propagations [source_ref]
  seal-episode <episode_id>
  delete-event <event_id>
  claim-job
  run-job [--real]
  backfill-jobs [--limit N] [--force]
  observability [--limit N]
  start-job <job_id>
  complete-job <job_id>
  fail-job <job_id> <error> [--retry]
  revisions <memory_id>
  traces [数量]
  health
  back
"""

    def execute(self, line: str) -> tuple[str, bool]:
        parts = shlex.split(line, posix=False)
        if not parts:
            return "", False
        command = parts[0].lower().lstrip("/")
        args = parts[1:]

        if command in {"back", "exit", "quit", "q"}:
            return "返回 Sora 主界面。", True
        if command in {"help", "?"}:
            return self.help_text(), False
        if command == "health":
            return _dump(self.runtime.health()), False
        if command == "list":
            status = args[0].lower() if args else "active"
            return _dump(self.runtime.list_memories(None if status == "all" else status)), False
        if command == "candidates":
            status = args[0].lower() if args else "pending"
            return _dump(self.runtime.list_candidates(None if status == "all" else status)), False
        if command == "gate":
            candidate_id = self._required(args, "gate <candidate_id>")
            return _dump(self.runtime.run_candidate_gate(candidate_id)), False
        if command == "batch-gate":
            candidate_ids = self._positional_args(args)
            if not candidate_ids:
                raise ValueError("用法：batch-gate <candidate_id> [candidate_id...]")
            return _dump(self.runtime.run_batch_gate(candidate_ids)), False
        if command == "decisions":
            limit = _flag_int(args, "--limit", default=50)
            candidate_id = self._first_positional(args)
            return _dump(
                self.runtime.list_gate_decisions(candidate_id=candidate_id, limit=limit)
            ), False
        if command == "reviews":
            limit = _flag_int(args, "--limit", default=50)
            status = (self._first_positional(args) or "open").lower()
            return _dump(
                self.runtime.list_review_items(
                    status=None if status == "all" else status,
                    limit=limit,
                )
            ), False
        if command == "approve-review":
            review_id = self._required(args, "approve-review <review_id>")
            return _dump(self.runtime.approve_review(review_id)), False
        if command == "reject-review":
            review_id = self._required(args, "reject-review <review_id> [reason]")
            reason = _clean(" ".join(args[1:])) if len(args) > 1 else "manual_review_reject"
            return _dump(self.runtime.reject_review(review_id, reason=reason)), False
        if command == "defer-review":
            review_id = self._required(args, "defer-review <review_id> [--days N]")
            days = _flag_int(args, "--days", default=30)
            return _dump(self.runtime.defer_review(review_id, days=days)), False
        if command == "quarantines":
            limit = _flag_int(args, "--limit", default=50)
            status = (self._first_positional(args) or "active").lower()
            return _dump(
                self.runtime.list_quarantines(
                    status=None if status == "all" else status,
                    limit=limit,
                )
            ), False
        if command == "expire-deferred":
            return _dump(self.runtime.expire_deferred_candidates()), False
        if command == "process-expired-deferrals":
            return _dump(self.runtime.process_expired_deferrals()), False
        if command == "show":
            return _dump(self.runtime.get_memory(self._required(args, "show <memory_id>"))), False
        if command == "search":
            query = self._joined(args, "search <关键词>")
            return _dump(self.runtime.search(query)), False
        if command == "add":
            return _dump(self._add(args)), False
        if command == "commit":
            candidate_id = self._required(args, "commit <candidate_id>")
            return _dump(self.runtime.commit([candidate_id])), False
        if command == "reject":
            candidate_id = self._required(args, "reject <candidate_id>")
            return _dump(self.runtime.reject(candidate_id)), False
        if command == "rollback-candidate":
            candidate_id = self._required(args, "rollback-candidate <candidate_id> [reason]")
            reason = _clean(" ".join(args[1:])) if len(args) > 1 else "manual_rollback"
            return _dump(self.runtime.rollback_committed_candidate(candidate_id, reason=reason)), False
        if command == "revise":
            if len(args) < 2:
                raise ValueError("用法：revise <memory_id> <新内容>")
            return _dump(
                self.runtime.revise(_clean(args[0]), _clean(" ".join(args[1:])))
            ), False
        if command == "disable":
            return _dump(self.runtime.disable(self._required(args, "disable <memory_id>"))), False
        if command == "delete":
            return _dump(self.runtime.delete(self._required(args, "delete <memory_id>"))), False
        if command == "restore":
            return _dump(self.runtime.restore(self._required(args, "restore <memory_id>"))), False
        if command == "evidence":
            memory_id = self._required(args, "evidence <memory_id>")
            return _dump(self.runtime.evidence_for_memory(memory_id)), False
        if command == "episodes":
            status = _clean(args[0]).lower() if args else "all"
            return _dump(self.runtime.list_episodes(status=None if status == "all" else status)), False
        if command == "events":
            episode_id = _clean(args[0]) if args else None
            return _dump(self.runtime.list_events(episode_id=episode_id)), False
        if command == "jobs":
            status = _clean(args[0]).lower() if args else "all"
            return _dump(self.runtime.list_extraction_jobs(status=None if status == "all" else status)), False
        if command == "episode-trace":
            episode_id = self._required(args, "episode-trace <episode_id>")
            return _dump(self.runtime.get_episode_trace(episode_id)), False
        if command == "replay-episode":
            episode_id = self._required(args, "replay-episode <episode_id>")
            return _dump(self.runtime.replay_episode(episode_id)), False
        if command == "propagations":
            source_ref = _clean(args[0]) if args else None
            return _dump(self.runtime.list_delete_propagations(source_ref=source_ref)), False
        if command == "seal-episode":
            episode_id = self._required(args, "seal-episode <episode_id>")
            return _dump(self.runtime.seal_episode(episode_id)), False
        if command == "delete-event":
            event_id = self._required(args, "delete-event <event_id>")
            return _dump(self.runtime.propagate_event_delete(event_id)), False
        if command == "claim-job":
            return _dump(self.runtime.claim_next_extraction_job()), False
        if command == "run-job":
            if "--real" in args:
                from .memory_extractor_provider import OpenAICompatibleExtractorProvider

                return _dump(
                    self.runtime.run_extraction_once(
                        provider=OpenAICompatibleExtractorProvider.from_env()
                    )
                ), False
            return _dump(self.runtime.run_extraction_once()), False
        if command == "backfill-jobs":
            force = "--force" in args
            limit = _flag_int(args, "--limit", default=50)
            return _dump(self.runtime.backfill_extraction_jobs(limit=limit, force=force)), False
        if command == "observability":
            limit = _flag_int(args, "--limit", default=10)
            return _dump(self.runtime.extraction_observability(limit=limit)), False
        if command == "start-job":
            job_id = self._required(args, "start-job <job_id>")
            return _dump(self.runtime.start_extraction_job(job_id)), False
        if command == "complete-job":
            job_id = self._required(args, "complete-job <job_id>")
            return _dump(self.runtime.complete_extraction_job(job_id)), False
        if command == "fail-job":
            if len(args) < 2:
                raise ValueError("Usage: fail-job <job_id> <error> [--retry]")
            retry = "--retry" in args
            clean_args = [arg for arg in args if arg != "--retry"]
            return _dump(
                self.runtime.fail_extraction_job(
                    _clean(clean_args[0]),
                    error=_clean(" ".join(clean_args[1:])),
                    retry=retry,
                )
            ), False
        if command == "revisions":
            memory_id = self._required(args, "revisions <memory_id>")
            return _dump(self.runtime.revisions_for_memory(memory_id)), False
        if command == "traces":
            limit = int(_clean(args[0])) if args else 20
            return _dump(self.runtime.list_recall_traces(limit)), False

        raise ValueError(f"未知 Memory Console 命令：{command}。输入 help 查看命令。")

    def _add(self, args: list[str]) -> dict[str, Any]:
        if not args:
            raise ValueError("用法：add <内容> [--type preference] [--tags tag1,tag2]")
        memory_type = "preference"
        tags: list[str] = []
        content_parts: list[str] = []
        index = 0
        while index < len(args):
            if args[index] == "--type" and index + 1 < len(args):
                memory_type = _clean(args[index + 1])
                index += 2
            elif args[index] == "--tags" and index + 1 < len(args):
                tags = [item.strip() for item in _clean(args[index + 1]).split(",") if item.strip()]
                index += 2
            else:
                content_parts.append(args[index])
                index += 1
        summary = _clean(" ".join(content_parts))
        if not summary:
            raise ValueError("记忆内容不能为空。")
        return self.runtime.add_manual(summary, memory_type=memory_type, tags=tags)

    @staticmethod
    def _required(args: list[str], usage: str) -> str:
        if not args:
            raise ValueError(f"用法：{usage}")
        return _clean(args[0])

    @staticmethod
    def _joined(args: list[str], usage: str) -> str:
        if not args:
            raise ValueError(f"用法：{usage}")
        return _clean(" ".join(args))

    @staticmethod
    def _first_positional(args: list[str]) -> str | None:
        skip_next = False
        for arg in args:
            if skip_next:
                skip_next = False
                continue
            if arg.startswith("--"):
                skip_next = True
                continue
            return _clean(arg)
        return None

    @staticmethod
    def _positional_args(args: list[str]) -> list[str]:
        values: list[str] = []
        skip_next = False
        for arg in args:
            if skip_next:
                skip_next = False
                continue
            if arg.startswith("--"):
                skip_next = True
                continue
            values.append(_clean(arg))
        return values
