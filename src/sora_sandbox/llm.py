from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol


class LLMClient(Protocol):
    def chat(self, system: str, user: str, **kwargs: Any) -> dict[str, Any]:
        ...


@dataclass
class DeepSeekClient:
    api_key: str
    model: str = "deepseek-chat"
    base_url: str = "https://api.deepseek.com/chat/completions"
    timeout: int = 60

    def chat(self, system: str, user: str, **kwargs: Any) -> dict[str, Any]:
        body = {
            "model": kwargs.get("model", self.model),
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": kwargs.get("temperature", 0.7),
            "max_tokens": kwargs.get("max_tokens", 800),
        }
        req = urllib.request.Request(
            self.base_url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"DeepSeek API error {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"DeepSeek API connection failed: {exc}") from exc

        choice = payload["choices"][0]["message"]["content"]
        return {
            "content": choice,
            "usage": payload.get("usage", {}),
            "model": payload.get("model", body["model"]),
            "raw": payload,
        }


class FakeLLMClient:
    def chat(self, system: str, user: str, **kwargs: Any) -> dict[str, Any]:
        # Deterministic fake used by tests and local shape checks.
        if '"input_mode": "greeting"' in user:
            content = "你好呀，我在。"
        elif '"primary_mode": "initiative_self_life_share"' in user:
            content = "我这边倒有个小插曲：回声集市那个旧邮筒又开始学人说话了。先轻轻放这儿，不打断你。"
        elif "worldops_frame" in user and "evt_starport_lamp_001" in user:
            content = "我这边的星港快到星灯节了。先不打断你太久：如果你愿意，晚点可以陪我选一句写进星灯的留言。"
        elif "playfulness" in user:
            content = "我会先把方案的边界捋清楚，再轻轻吐槽它最容易长歪的地方。"
        else:
            content = "我会按当前记忆、人格和场景状态，先陪你把方案稳稳拆开，再决定下一步。"
        return {
            "content": content,
            "usage": {"prompt_tokens": len(system) + len(user), "completion_tokens": len(content), "total_tokens": len(system) + len(user) + len(content)},
            "model": "fake-llm",
            "raw": {},
        }
