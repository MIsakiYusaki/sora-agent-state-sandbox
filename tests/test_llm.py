from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sora_sandbox.agent import LLMArbitrationJudger, LLMTurnInterpreter
from sora_sandbox.llm import DeepSeekClient, FakeLLMClient


class _RecordingFakeLLM(FakeLLMClient):
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def chat(self, system: str, user: str, **kwargs):
        self.calls.append({"system": system, "user": user, "kwargs": kwargs})
        return super().chat(system, user, **kwargs)


class _FakeHTTPResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(
            {
                "choices": [{"message": {"content": "{}"}}],
                "usage": {},
                "model": "deepseek-v4-flash",
            }
        ).encode("utf-8")


class LLMThinkingModeTests(unittest.TestCase):
    def test_interpreter_and_arbiter_request_non_thinking_mode(self) -> None:
        llm = _RecordingFakeLLM()
        LLMTurnInterpreter(llm).interpret(
            {
                "user_input": "你好",
                "recent_turns": [],
                "proactive_trigger": None,
            }
        )
        LLMArbitrationJudger(llm).score(
            {"turn_frame": {}, "behavior_frame": {}},
            [{"id": "sig_test", "source": "memory", "type": "memory_recollection"}],
        )

        self.assertEqual(len(llm.calls), 2)
        for call in llm.calls:
            self.assertEqual(call["kwargs"]["thinking"], {"type": "disabled"})

    def test_deepseek_client_only_sends_thinking_when_requested(self) -> None:
        captured_bodies: list[dict[str, object]] = []

        def fake_urlopen(request, timeout):
            captured_bodies.append(json.loads(request.data.decode("utf-8")))
            return _FakeHTTPResponse()

        client = DeepSeekClient(api_key="test-key")
        with patch("sora_sandbox.llm.urllib.request.urlopen", side_effect=fake_urlopen):
            client.chat("system", "user", thinking={"type": "disabled"})
            client.chat("system", "user")

        self.assertEqual(captured_bodies[0]["thinking"], {"type": "disabled"})
        self.assertNotIn("thinking", captured_bodies[1])


if __name__ == "__main__":
    unittest.main()
