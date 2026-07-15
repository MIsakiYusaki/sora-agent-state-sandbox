from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from sora_sandbox.affect.contracts import AffectStimulusEvent
from sora_sandbox.affect.eval import run_affect_eval
from sora_sandbox.affect.provider import LLMAffectAppraisalProvider
from sora_sandbox.affect.store import AffectStore
from sora_sandbox.affect.system import AffectSystem
from sora_sandbox.credentials import CredentialManager
from sora_sandbox.llm import DeepSeekClient


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run Affect v2 against the configured real DeepSeek provider."
    )
    parser.add_argument("--model", default=os.environ.get("OPENAI_MODEL", "deepseek-v4-flash"))
    args = parser.parse_args()
    key = CredentialManager().get_api_key(prompt_if_missing=False)
    llm = DeepSeekClient(
        api_key=key,
        model=args.model,
        base_url=os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com"),
    )
    with tempfile.TemporaryDirectory(prefix="sora-affect-live-") as directory:
        system = AffectSystem(
            AffectStore(Path(directory) / "affect.db"),
            provider=LLMAffectAppraisalProvider(llm),
            start_worker=False,
        )
        try:
            now = datetime.now(timezone.utc).isoformat()
            event = AffectStimulusEvent(
                event_id="affect-live-eval-event",
                sora_id=system.store.sora_id,
                source_type="live_eval",
                source_ref="live-eval:reviewed-shared-milestone",
                source_revision="1",
                occurred_at=now,
                actor_user_id="live-eval-user",
                target_kind="user",
                target_ref="live-eval-user",
                relationship_ref="live-eval-relationship",
                conversation_scope="live-eval",
                source_confidence=0.98,
                event_kind="goal_completed",
                payload={
                    "goal_importance": 0.9,
                    "novelty_modifier": 0.7,
                    "structured_context": "The user reports a completed shared project milestone.",
                },
                evidence_refs=("live-eval:evidence:1",),
                untrusted_text="We finished the project milestone together, and I am relieved and happy.",
                correlation_id="affect-live-eval-correlation",
            )
            ingestion = system.ingest_structured_event(event, semantic_enabled=True)
            processing = system.process_pending(limit=1, force=True)
            evaluation = run_affect_eval(system)
            stored_event = system.store.event(event.event_id)
            payload = {
                "ingestion": ingestion,
                "processing": processing,
                "evaluation": evaluation,
                "health": system.health(),
                "privacy": {
                    "raw_text_exported": False,
                    "payload_protection": system.store.pragma_health()["payload_protection"],
                    "semantic_status": stored_event["semantic_status"],
                },
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            processed = (processing.get("results") or [{}])[0]
            return 0 if (
                evaluation["status"] == "passed"
                and processing["processed_count"] == 1
                and processed.get("status") in {"applied", "skipped"}
                and "untrusted_text" not in json.dumps(payload, ensure_ascii=False)
            ) else 1
        finally:
            system.close()


if __name__ == "__main__":
    raise SystemExit(main())
