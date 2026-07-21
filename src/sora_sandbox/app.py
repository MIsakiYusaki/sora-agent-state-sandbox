from __future__ import annotations

import argparse
import json
import os
import sys

from .agent import AgentRunner
from .affect.provider import LLMAffectAppraisalProvider
from .affect.system import AffectSystem
from .credentials import CredentialError, CredentialManager
from .interruptibility import InterruptibilityService, LLMInterruptibilityResolutionProvider
from .interruptibility_eval import run_interruptibility_eval
from .llm import DeepSeekClient
from .memory_system import MemorySystem
from .persona.provider import LLMPersonaCandidateProvider
from .persona.system import PersonaSystem
from .relationship.provider import LLMRelationshipEvidenceProvider
from .relationship.system import RelationshipAffectAdapter, RelationshipSystem
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

    interruptibility = InterruptibilityService(
        provider=LLMInterruptibilityResolutionProvider(llm),
    )
    persona_runtime = PersonaSystem.default(
        provider=LLMPersonaCandidateProvider(llm),
        start_worker=True,
    )
    affect_runtime = AffectSystem.default(
        provider=LLMAffectAppraisalProvider(llm),
        memory_runtime=memory_runtime,
        start_worker=True,
        rollout_mode=os.environ.get("SORA_AFFECT_MODE", "active").strip().lower(),
    )
    relationship_runtime = RelationshipSystem.default(
        provider=LLMRelationshipEvidenceProvider(llm),
        start_worker=True,
        rollout_mode=os.environ.get("SORA_RELATIONSHIP_MODE", "active").strip().lower(),
    )
    relationship_runtime.register_domain_event_consumer(
        "affect",
        RelationshipAffectAdapter(affect_runtime, relationship_runtime).deliver,
    )
    memory_runtime.register_memory_change_listener(
        "affect",
        affect_runtime.handle_memory_change,
    )
    if args.reset_state:
        persona_runtime.reset()
        affect_runtime.reset()
        relationship_runtime.reset()
    memory_runtime.projector_bus.register_adapter(
        "persona",
        persona_runtime.projection_adapter,
    )
    memory_runtime.projector_bus.register_adapter(
        "relationship",
        relationship_runtime.projection_adapter,
    )
    runner = AgentRunner(
        llm,
        memory_runtime=memory_runtime,
        interruptibility=interruptibility,
        persona_runtime=persona_runtime,
        affect_runtime=affect_runtime,
        relationship_runtime=relationship_runtime,
    )
    repl = SoraRepl(
        store,
        runner,
        show_trace_panel=not args.compact,
        memory_runtime=memory_runtime,
        interruptibility_runtime=interruptibility,
        persona_runtime=persona_runtime,
        affect_runtime=affect_runtime,
        relationship_runtime=relationship_runtime,
    )
    memory_runtime.register_quality_eval_suite(
        "interruptibility",
        run_interruptibility_eval,
        source_module="interruptibility_system",
    )
    memory_runtime.register_quality_export_section(
        "interruptibility",
        lambda: interruptibility.export_traces(repl.state, limit=500),
    )
    persona_runtime.register_quality_ops(memory_runtime)
    affect_runtime.register_quality_ops(memory_runtime)
    relationship_runtime.register_quality_ops(memory_runtime)

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
        try:
            relationship_runtime.close()
        finally:
            try:
                affect_runtime.close()
            finally:
                try:
                    persona_runtime.close()
                finally:
                    memory_runtime.close()
