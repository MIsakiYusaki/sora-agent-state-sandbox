from __future__ import annotations

import threading

from .agent import AgentRunner
from .commands import CommandError, handle_command
from .memory_console import MemoryConsole
from .memory_runtime import MemoryRuntime
from .proactive import ProactiveScheduler, ProactiveTrigger
from .render import render_trace_panel
from .state import StateStore


class SoraRepl:
    def __init__(
        self,
        store: StateStore,
        runner: AgentRunner,
        show_trace_panel: bool = True,
        memory_runtime: MemoryRuntime | None = None,
    ) -> None:
        self.store = store
        self.runner = runner
        self.state = store.load()
        self.show_trace_panel = show_trace_panel
        self.memory_runtime = memory_runtime
        if self.memory_runtime is not None:
            self.memory_runtime.import_legacy_memories(self.state["memory"].get("items", []))
            self.memory_runtime.sync_state_cache(self.state)
            self.store.save(self.state)
        self._runtime_lock = threading.RLock()
        self.scheduler = ProactiveScheduler(
            self._on_proactive_trigger,
            idle_seconds=int(self.state["initiative"].get("idle_threshold_sec", 180)),
        )

    def loop(self) -> None:
        print("Sora Agent State Sandbox")
        threshold = int(self.state["initiative"].get("idle_threshold_sec", 180))
        print(f"输入 /help 查看命令，/exit 退出。空闲 {threshold} 秒后会尝试一次主动搭话。")
        if self.state["initiative"].get("scheduler_enabled", True):
            self.scheduler.start()
        try:
            while True:
                try:
                    line = input("sora> ").strip()
                except (EOFError, KeyboardInterrupt):
                    print("\n再见。")
                    with self._runtime_lock:
                        self.store.save(self.state)
                    return
                self.scheduler.mark_activity()
                if not line:
                    continue
                if line.startswith("/simulate-idle"):
                    self._simulate_idle(line)
                    continue
                if line == "/memory":
                    self._memory_console()
                    continue
                if line.startswith("/"):
                    try:
                        with self._runtime_lock:
                            output, should_exit = handle_command(
                                line,
                                self.state,
                                memory_admin=self.memory_runtime,
                            )
                            if self.memory_runtime is not None:
                                self.memory_runtime.import_legacy_memories(
                                    self.state["memory"].get("items", [])
                                )
                                self.memory_runtime.sync_state_cache(self.state)
                            self._sync_scheduler_config()
                            self.store.save(self.state)
                        if output:
                            print(output)
                        if should_exit:
                            return
                    except (CommandError, ValueError, TypeError) as exc:
                        print(f"命令错误：{exc}")
                    continue

                with self._runtime_lock:
                    result = self.runner.run(self.state, line)
                    self.store.save(self.state)
                if self.show_trace_panel:
                    print(render_trace_panel(result))
                else:
                    print(f"Sora> {result['reply']}")
        finally:
            self.scheduler.stop()

    def run_once(self, text: str) -> dict:
        result = self.runner.run(self.state, text)
        self.store.save(self.state)
        return result

    def _on_proactive_trigger(self, trigger: ProactiveTrigger) -> None:
        with self._runtime_lock:
            result = self.runner.run_proactive(self.state, trigger.to_dict())
            self.store.save(self.state)
        if not result.get("emitted"):
            return
        print()
        if self.show_trace_panel:
            print(render_trace_panel(result))
        else:
            print(f"Sora> {result['reply']}")
        print("sora> ", end="", flush=True)

    def _simulate_idle(self, line: str) -> None:
        parts = line.split()
        raw = parts[1] if len(parts) > 1 else "3m"
        try:
            idle_seconds = parse_duration_seconds(raw)
        except ValueError as exc:
            print(f"命令错误：{exc}")
            return
        self._on_proactive_trigger(
            ProactiveTrigger(
                trigger_type="idle_simulation",
                idle_seconds=idle_seconds,
                fired_at=0,
                simulated=True,
            )
        )
        self.scheduler.mark_activity()

    def _sync_scheduler_config(self) -> None:
        self.scheduler.idle_seconds = int(
            self.state["initiative"].get("idle_threshold_sec", 180)
        )

    def _memory_console(self) -> None:
        if self.memory_runtime is None:
            print("Memory Runtime 未启用。")
            return
        console = MemoryConsole(self.memory_runtime)
        print(console.banner())
        while True:
            try:
                line = input("memory> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n返回 Sora 主界面。")
                return
            if not line:
                continue
            try:
                with self._runtime_lock:
                    output, should_back = console.execute(line)
                    self.memory_runtime.sync_state_cache(self.state)
                    self.store.save(self.state)
                if output:
                    print(output)
                if should_back:
                    return
            except (ValueError, TypeError) as exc:
                print(f"Memory Console 错误：{exc}")


def parse_duration_seconds(value: str) -> int:
    text = value.strip().lower()
    multipliers = {"s": 1, "m": 60, "h": 3600}
    if text[-1:] in multipliers:
        number = text[:-1]
        multiplier = multipliers[text[-1]]
    else:
        number = text
        multiplier = 60
    try:
        seconds = int(float(number) * multiplier)
    except (TypeError, ValueError):
        raise ValueError("用法：/simulate-idle 3m（也支持 30s、1h）") from None
    if seconds <= 0:
        raise ValueError("空闲时长必须大于 0。")
    return seconds
