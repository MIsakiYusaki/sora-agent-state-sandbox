from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from sora_sandbox.credentials import CredentialManager
from sora_sandbox.llm import DeepSeekClient
from sora_sandbox.persona.eval import run_persona_eval
from sora_sandbox.persona.provider import LLMPersonaCandidateProvider
from sora_sandbox.persona.store import PersonaStore
from sora_sandbox.persona.system import PersonaSystem


def event() -> dict:
    return {
        "contract": "ProjectionEvent.v1",
        "event_id": "persona-live-eval-event",
        "target_subsystem": "persona",
        "event_type": "memory_upsert",
        "action": "upsert",
        "source": {"source_ref": "persona-live-eval-memory", "committed_only": True},
        "memory": {
            "id": "persona-live-eval-memory",
            "owner_user_id": "live-eval-user",
            "type": "persona_delta",
            "status": "active",
            "summary": "Across several reviewed interactions, Sora independently chose careful, warm responses without increasing follow-up pressure.",
            "tags": ["persona", "live_eval", "reviewed"],
            "confidence": 0.96,
            "sensitivity": "low",
            "evidence_ids": ["persona-live-evidence-1", "persona-live-evidence-2"],
        },
        "lifecycle": {"tier": "hot", "heat_score": 0.9},
        "idempotency": {"event_key": "persona:live-eval", "payload_hash": "persona-live-eval-v1"},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Persona System against the configured real DeepSeek provider.")
    parser.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "deepseek-v4-flash"))
    args = parser.parse_args()
    key = CredentialManager().get_api_key(prompt_if_missing=False)
    llm = DeepSeekClient(
        api_key=key,
        model=args.model,
        base_url=os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com"),
    )
    with tempfile.TemporaryDirectory(prefix="sora-persona-live-") as directory:
        system = PersonaSystem(
            PersonaStore(Path(directory) / "persona.db"),
            provider=LLMPersonaCandidateProvider(llm),
            start_worker=False,
        )
        try:
            ack = system.projection_adapter.deliver(event())
            processing = system.process_pending(limit=1)
            review = None
            first_result = (processing.get("results") or [{}])[0]
            if first_result.get("status") == "pending_review":
                review = system.approve_candidate(
                    str(first_result["candidate_id"]),
                    reason="live_eval_human_review_approved",
                    actor="live_eval_reviewer",
                )
            evaluation = run_persona_eval(system)
            payload = {
                "ack": ack,
                "processing": processing,
                "review": review,
                "evaluation": evaluation,
                "health": system.health(),
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            committed = bool(system.store.list_contributions(active=True, limit=1))
            return 0 if evaluation["status"] == "passed" and processing["processed_count"] == 1 and committed else 1
        finally:
            system.close()


if __name__ == "__main__":
    raise SystemExit(main())
