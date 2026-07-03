from __future__ import annotations

import argparse
import json
import os
import sys

from .agent import AgentRunner
from .credentials import CredentialError, CredentialManager
from .llm import DeepSeekClient
from .memory_system import MemorySystem
from .render import render_trace_panel
from .repl import SoraRepl
from .state import StateStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Project Sora Agent State Sandbox")
    parser.add_argument("--provider", choices=["deepseek"], default="deepseek")
    parser.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "deepseek-v4-flash"))
    parser.add_argument("--once", help="Run one user turn and exit.")
    parser.add_argument("--json", action="store_true", help="Print full JSON result for --once.")
    parser.add_argument("--trace", action="store_true", help="Print readable trace panel for --once.")
    parser.add_argument("--compact", action="store_true", help="In REPL, print only the Sora reply after each turn.")
    parser.add_argument("--reset-state", action="store_true", help="Reset sandbox state before running.")
    parser.add_argument("--no-prompt-key", action="store_true", help="Do not prompt for DeepSeek API key.")
    return parser


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8")
            except (OSError, ValueError):
                pass


def make_llm(provider: str, model: str, no_prompt_key: bool):
    manager = CredentialManager()
    key = manager.get_api_key(prompt_if_missing=not no_prompt_key)
    return DeepSeekClient(
        api_key=key,
        model=model,
        base_url=os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com"),
    )


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    store = StateStore.default()
    memory_runtime = MemorySystem.default()
    if args.reset_state:
        store.reset()
        memory_runtime.reset()

    try:
        llm = make_llm(args.provider, args.model, args.no_prompt_key)
    except CredentialError as exc:
        memory_runtime.close()
        print(f"凭据错误：{exc}", file=sys.stderr)
        return 2

    runner = AgentRunner(llm, memory_runtime=memory_runtime)
    repl = SoraRepl(
        store,
        runner,
        show_trace_panel=not args.compact,
        memory_runtime=memory_runtime,
    )

    try:
        if args.once:
            result = repl.run_once(args.once)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            elif args.trace:
                print(render_trace_panel(result))
            else:
                print(result["reply"])
            return 0

        repl.loop()
        return 0
    finally:
        memory_runtime.close()
