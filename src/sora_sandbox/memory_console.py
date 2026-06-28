from __future__ import annotations

import json
import shlex
from typing import Any

from .memory_runtime import MemoryRuntime


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _clean(value: str) -> str:
    return value.strip().strip('"')


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
  candidates [pending|committed|rejected|all]
  show <memory_id>
  search <关键词>
  add <内容> [--type preference] [--tags tag1,tag2]
  commit <candidate_id>
  reject <candidate_id>
  revise <memory_id> <新内容>
  disable <memory_id>
  delete <memory_id>
  restore <memory_id>
  evidence <memory_id>
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
