from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sora_sandbox.agent import AgentRunner
from sora_sandbox.credentials import CredentialManager
from sora_sandbox.interruptibility import (
    InterruptibilityService,
    LLMInterruptibilityResolutionProvider,
)
from sora_sandbox.llm import DeepSeekClient
from sora_sandbox.state import seed_state


SCENARIOS = (
    ("direct_task", "请直接说明这个方案的两个主要风险"),
    ("brief_control", "简短点，只说结论"),
    ("no_question", "解释一下，但不要追问我"),
    ("open_invitation", "陪我聊会儿"),
    ("response_mute", "这条不用回复"),
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run redacted real-DeepSeek Interruptibility E2E checks.")
    parser.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "deepseek-v4-flash"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    api_key = CredentialManager().get_api_key(prompt_if_missing=False)
    llm = DeepSeekClient(
        api_key=api_key,
        model=args.model,
        base_url=os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com"),
    )
    service = InterruptibilityService(provider=LLMInterruptibilityResolutionProvider(llm))
    results = []
    for case_name, user_input in SCENARIOS:
        state = seed_state()
        started = time.perf_counter()
        result = AgentRunner(llm, interruptibility=service).run(state, user_input)
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        frame = result["context_pack"]["interruptibility_frame"]
        enforcement = state["interruptibility"].get("last_enforcement", {})
        results.append(
            {
                "case_name": case_name,
                "status": "passed" if enforcement.get("compliant") else "failed",
                "emitted": result["emitted"],
                "response_obligation": frame["response_obligation"],
                "response_shape": frame["response_shape"],
                "question_budget": frame["budgets"]["question_budget"],
                "decision_path": frame["decision_path"],
                "model_assist": frame["model_assist"],
                "enforcement": enforcement,
                "reply_hash": hashlib.sha256(result["reply"].encode("utf-8")).hexdigest(),
                "reply_char_count": len(result["reply"]),
                "latency_ms": latency_ms,
            }
        )
    report = {
        "schema_version": "interruptibility_live_eval.v1",
        "model": args.model,
        "status": "passed" if all(item["status"] == "passed" for item in results) else "failed",
        "cases": results,
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
