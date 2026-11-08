from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from time import perf_counter
from pathlib import Path
from typing import Protocol

from .memory_extractor_schema import EXTRACTOR_RESULT_SCHEMA_VERSION
from .memory_extractor_repair import repair_extractor_json
from .memory_schema import JsonDict, new_id
from .turn_observability import observe_call, observe_http


class MemoryExtractorProvider(Protocol):
    def extract(self, input_package: JsonDict) -> JsonDict:
        ...


@dataclass(frozen=True)
class GoldenExtractorCase:
    case_id: str
    match_text: str
    expected: JsonDict


class GoldenSetExtractorProvider:
    """Deterministic provider backed by extractor golden cases.

    The fixture provider intentionally reads expected output from a golden set
    so unit tests can stay deterministic while production uses a real LLM.
    """

    def __init__(self, cases: list[GoldenExtractorCase] | None = None) -> None:
        self.cases = cases if cases is not None else default_golden_cases()

    @classmethod
    def from_jsonl(cls, path: Path) -> "GoldenSetExtractorProvider":
        cases: list[GoldenExtractorCase] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            cases.append(
                GoldenExtractorCase(
                    case_id=str(payload["case_id"]),
                    match_text=str(payload["match_text"]),
                    expected=dict(payload["expected"]),
                )
            )
        return cls(cases)

    def extract(self, input_package: JsonDict) -> JsonDict:
        source_text = input_text(input_package)
        for case in self.cases:
            if case.match_text.lower() in source_text.lower():
                return bind_expected_result(case.expected, input_package, case.case_id)
        return noop_result(input_package, reason="golden_set_no_match")


@dataclass
class OpenAICompatibleExtractorProvider:
    api_key: str
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-v4-flash"
    timeout: int = 90
    prompt_version: str = "extractor_prompt.v1"
    prompt_path: Path | None = None
    json_repair_pass_enabled: bool = True

    @classmethod
    def from_env(cls) -> "OpenAICompatibleExtractorProvider":
        api_key = (
            os.environ.get("SORA_LLM_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or os.environ.get("DEEPSEEK_API_KEY")
        )
        if not api_key:
            raise RuntimeError("missing OPENAI_API_KEY/DEEPSEEK_API_KEY for real extractor")
        return cls(
            api_key=api_key,
            base_url=(
                os.environ.get("SORA_LLM_BASE_URL")
                or os.environ.get("OPENAI_BASE_URL")
                or "https://api.deepseek.com"
            ),
            model=(
                os.environ.get("SORA_LLM_MODEL")
                or os.environ.get("OPENAI_MODEL")
                or "deepseek-v4-flash"
            ),
            prompt_version=os.environ.get("SORA_EXTRACTOR_PROMPT_VERSION") or "extractor_prompt.v1",
            json_repair_pass_enabled=os.environ.get("SORA_EXTRACTOR_JSON_REPAIR_PASS", "1") != "0",
        )

    def extract(self, input_package: JsonDict) -> JsonDict:
        started = perf_counter()
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self._system_prompt()},
                {"role": "user", "content": json.dumps(self._llm_payload(input_package), ensure_ascii=False)},
            ],
            "temperature": 0.1,
            "max_tokens": 3000,
            "response_format": {"type": "json_object"},
        }
        payload = self._post_chat_completion(body)

        content = payload["choices"][0]["message"].get("content") or ""
        repair_pass: JsonDict = {"used": False}
        try:
            result = _parse_json_object(content)
        except (json.JSONDecodeError, ValueError) as exc:
            if not self.json_repair_pass_enabled:
                raise
            repair_started = perf_counter()
            repair_payload = observe_call("memory.extractor.json_repair", self._repair_output_with_llm,
                                          content, input_package, error=str(exc))
            repair_content = repair_payload["choices"][0]["message"].get("content") or ""
            result = _parse_json_object(repair_content)
            repair_pass = {
                "used": True,
                "error": str(exc),
                "usage": repair_payload.get("usage", {}),
                "latency_ms": round((perf_counter() - repair_started) * 1000, 2),
            }
        result, repairs = repair_extractor_json(result, input_package)
        result["schema_version"] = EXTRACTOR_RESULT_SCHEMA_VERSION
        result["job_id"] = input_package["job"]["job_id"]
        result["episode_id"] = input_package["episode"]["episode_id"]
        result["model"] = {
            "provider": "openai_compatible",
            "model": payload.get("model") or self.model,
            "prompt_version": self.prompt_version,
        }
        usage = _combine_usage(payload.get("usage", {}), repair_pass.get("usage", {}))
        result.setdefault("trace", {})["usage"] = usage
        result["trace"]["cost"] = estimate_llm_cost(usage)
        result["trace"]["latency_ms"] = round((perf_counter() - started) * 1000, 2)
        result["trace"]["provider"] = "openai_compatible"
        result["trace"]["json_repairs"] = repairs
        result["trace"]["json_repair_pass"] = repair_pass
        return result

    def _post_chat_completion(self, body: JsonDict) -> JsonDict:
        req = urllib.request.Request(
            self.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.api_key,
            },
            method="POST",
        )
        try:
            with observe_http("provider.http.compat_extraction"), urllib.request.urlopen(req, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"extractor LLM API error {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"extractor LLM connection failed: {exc}") from exc

    def _repair_output_with_llm(self, content: str, input_package: JsonDict, *, error: str) -> JsonDict:
        body = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You repair malformed JSON for a domain-neutral memory extractor. "
                        "Do not reinterpret, add, remove, or improve the semantic content. "
                        "Return one valid JSON object only. Preserve candidates, contradictions, "
                        "evidence_refs, operation, memory_type, summary, scores, and target_memory_id."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "task": "repair_extractor_json_only",
                            "schema_version": EXTRACTOR_RESULT_SCHEMA_VERSION,
                            "job_id": input_package["job"]["job_id"],
                            "episode_id": input_package["episode"]["episode_id"],
                            "parse_error": error,
                            "malformed_output": content,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "temperature": 0.0,
            "max_tokens": 3000,
            "response_format": {"type": "json_object"},
        }
        return self._post_chat_completion(body)

    def _llm_payload(self, input_package: JsonDict) -> JsonDict:
        return {
            "task": "extract_memory_candidates",
            "schema_version": EXTRACTOR_RESULT_SCHEMA_VERSION,
            "job": input_package["job"],
            "episode": input_package["episode"],
            "events": input_package["events"],
            "related_memories": input_package.get("related_memories", []),
            "policy": input_package.get("policy", {}),
            "allowed_operations": ["ADD", "UPDATE", "MERGE", "DELETE", "NOOP", "REVIEW"],
            "allowed_memory_types": [
                "preference",
                "goal",
                "shared_episode",
                "persona_delta",
                "relationship_signal",
                "domain_principle",
                "project_principle",
                "world_state",
                "world_ops",
                "procedural_hint",
                "episode",
                "worldops_episode",
            ],
        }

    def _system_prompt(self) -> str:
        prompt_path = self.prompt_path or _default_prompt_path(self.prompt_version)
        if prompt_path.exists():
            return prompt_path.read_text(encoding="utf-8")
        return (
            "You are a domain-neutral async memory extractor. Return only JSON. "
            "Extract durable, evidence-backed memory candidates from the episode. "
            "Use ADD for novel durable memory, UPDATE when an existing related memory should be superseded, "
            "MERGE when the new evidence strengthens an existing memory, DELETE when the user/source invalidates memory, "
            "NOOP for jokes/low-value/unsupported inferences, REVIEW for risky uncertain cases. "
            "Every non-NOOP candidate must include evidence_refs using event_id from the provided events. "
            "Do not invent memories without evidence. Relationship signals are allowed for companionship context, "
            "but mark them as relationship_signal and keep them evidence-backed and governable. "
            "Output fields: candidates[], contradictions[], trace{}. Each candidate must include operation, memory_type, "
            "summary, evidence_refs, confidence, importance, stability, specificity, novelty, conflict_score, "
            "sensitivity, reason, and target_memory_id when operation is UPDATE/MERGE/DELETE."
        )


def estimate_llm_cost(usage: JsonDict) -> JsonDict:
    prompt_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or prompt_tokens + completion_tokens)
    input_cost_per_1k = float(os.environ.get("SORA_LLM_INPUT_COST_PER_1K", "0") or 0)
    output_cost_per_1k = float(os.environ.get("SORA_LLM_OUTPUT_COST_PER_1K", "0") or 0)
    estimated = (prompt_tokens / 1000.0 * input_cost_per_1k) + (
        completion_tokens / 1000.0 * output_cost_per_1k
    )
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "input_cost_per_1k": input_cost_per_1k,
        "output_cost_per_1k": output_cost_per_1k,
        "estimated_cost": round(estimated, 8),
        "currency": os.environ.get("SORA_LLM_COST_CURRENCY", "USD"),
    }


def _combine_usage(primary: JsonDict, repair: JsonDict) -> JsonDict:
    if not repair:
        return primary
    prompt_tokens = int(primary.get("prompt_tokens") or primary.get("input_tokens") or 0) + int(
        repair.get("prompt_tokens") or repair.get("input_tokens") or 0
    )
    completion_tokens = int(primary.get("completion_tokens") or primary.get("output_tokens") or 0) + int(
        repair.get("completion_tokens") or repair.get("output_tokens") or 0
    )
    total_tokens = int(primary.get("total_tokens") or 0) + int(repair.get("total_tokens") or 0)
    if total_tokens <= 0:
        total_tokens = prompt_tokens + completion_tokens
    return {
        **primary,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "repair_prompt_tokens": int(repair.get("prompt_tokens") or repair.get("input_tokens") or 0),
        "repair_completion_tokens": int(repair.get("completion_tokens") or repair.get("output_tokens") or 0),
    }


def _default_prompt_path(prompt_version: str) -> Path:
    root = Path(__file__).resolve().parents[2]
    return root / "prompts" / f"{prompt_version}.md"


def input_text(input_package: JsonDict) -> str:
    parts: list[str] = []
    for event in input_package.get("events", []):
        payload = event.get("payload", {})
        if isinstance(payload, dict):
            parts.append(str(payload.get("content") or ""))
        else:
            parts.append(str(payload))
    return "\n".join(part for part in parts if part)


def _parse_json_object(content: str) -> JsonDict:
    json_text = content.strip()
    try:
        parsed = json.loads(json_text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", json_text, flags=re.DOTALL)
        if not match:
            raise
        extracted = match.group(0)
        try:
            parsed = json.loads(extracted)
        except json.JSONDecodeError:
            parsed = json.loads(_repair_json_text(extracted))
    if not isinstance(parsed, dict):
        raise ValueError("extractor LLM output must be a JSON object")
    parsed.setdefault("candidates", [])
    parsed.setdefault("contradictions", [])
    parsed.setdefault("trace", {})
    return parsed


def _repair_json_text(text: str) -> str:
    repaired = text.strip()
    repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
    repaired = re.sub(r"}\s*{", "},{", repaired)
    repaired = re.sub(r"]\s*(\")", r"], \1", repaired)
    repaired = re.sub(r"(?<=[\"}\]0-9])\s*\n\s*(?=\"[A-Za-z_][A-Za-z0-9_]*\"\s*:)", ",\n", repaired)
    return repaired


def bind_expected_result(expected: JsonDict, input_package: JsonDict, case_id: str) -> JsonDict:
    job = input_package["job"]
    episode = input_package["episode"]
    events = input_package.get("events", [])
    first_event = next((event for event in events if event.get("actor") == "user"), events[0] if events else {})
    related = input_package.get("related_memories", [])

    result = json.loads(json.dumps(expected))
    result["schema_version"] = EXTRACTOR_RESULT_SCHEMA_VERSION
    result["job_id"] = job["job_id"]
    result["episode_id"] = episode["episode_id"]
    result.setdefault("model", {"provider": "golden_fixture", "model": "golden-set", "prompt_version": "fixture.v1"})
    result.setdefault("trace", {})["golden_case_id"] = case_id

    for candidate in result.get("candidates", []):
        candidate.setdefault("candidate_id", new_id("draft"))
        if not candidate.get("evidence_refs"):
            candidate["evidence_refs"] = [
                {
                    "event_id": first_event.get("event_id"),
                    "episode_id": episode["episode_id"],
                    "speaker": first_event.get("actor"),
                    "span": [0, len(str(first_event.get("payload", {}).get("content") or ""))],
                }
            ]
        for ref in candidate["evidence_refs"]:
            ref["episode_id"] = episode["episode_id"]
            if ref.get("event_id") in {"$first_user_event", None, ""}:
                ref["event_id"] = first_event.get("event_id")
            if not ref.get("speaker"):
                ref["speaker"] = first_event.get("actor")
        if candidate.get("target_memory_id") == "$first_related_memory":
            memory_type = str(candidate.get("memory_type") or "")
            target = next(
                (item for item in related if str(item.get("type") or "") == memory_type),
                related[0] if related else None,
            )
            candidate["target_memory_id"] = target["id"] if target else None
    return result


def noop_result(input_package: JsonDict, *, reason: str) -> JsonDict:
    job = input_package["job"]
    episode = input_package["episode"]
    return {
        "schema_version": EXTRACTOR_RESULT_SCHEMA_VERSION,
        "job_id": job["job_id"],
        "episode_id": episode["episode_id"],
        "model": {"provider": "golden_fixture", "model": "golden-set", "prompt_version": "fixture.v1"},
        "candidates": [
            {
                "candidate_id": new_id("draft"),
                "operation": "NOOP",
                "memory_type": "procedural_hint",
                "summary": "No durable memory candidate.",
                "evidence_refs": [],
                "confidence": 0.0,
                "importance": 0.0,
                "stability": 0.0,
                "specificity": 0.0,
                "novelty": 1.0,
                "conflict_score": 0.0,
                "sensitivity": "low",
                "reason": reason,
            }
        ],
        "contradictions": [],
        "trace": {"golden_case_id": None},
    }


def default_golden_cases() -> list[GoldenExtractorCase]:
    return [
        GoldenExtractorCase(
            case_id="explicit_async_extractor_preference",
            match_text="prefer async extractor design",
            expected={
                "candidates": [
                    {
                        "operation": "ADD",
                        "memory_type": "domain_principle",
                        "summary": "User prefers memory extraction to run asynchronously outside the response hot path.",
                        "confidence": 0.95,
                        "importance": 0.82,
                        "stability": 0.80,
                        "specificity": 0.88,
                        "novelty": 1.0,
                        "conflict_score": 0.0,
                        "sensitivity": "low",
                        "tags": ["async_extractor", "memory_architecture"],
                        "reason": "explicit_user_statement",
                    }
                ],
                "contradictions": [],
            },
        ),
        GoldenExtractorCase(
            case_id="negative_implicit_joke",
            match_text="just kidding do not remember that",
            expected={
                "candidates": [
                    {
                        "operation": "NOOP",
                        "memory_type": "procedural_hint",
                        "summary": "The user explicitly framed this as a joke and not durable memory.",
                        "evidence_refs": [],
                        "confidence": 0.20,
                        "importance": 0.0,
                        "stability": 0.0,
                        "specificity": 0.4,
                        "novelty": 1.0,
                        "conflict_score": 0.0,
                        "sensitivity": "low",
                        "reason": "negative_implicit_joke",
                    }
                ],
                "contradictions": [],
            },
        ),
        GoldenExtractorCase(
            case_id="update_from_related_memory",
            match_text="I no longer want minimal loops",
            expected={
                "candidates": [
                    {
                        "operation": "UPDATE",
                        "memory_type": "domain_principle",
                        "summary": "User wants product development to pursue the current best design rather than minimal loops.",
                        "target_memory_id": "$first_related_memory",
                        "confidence": 0.94,
                        "importance": 0.88,
                        "stability": 0.85,
                        "specificity": 0.86,
                        "novelty": 0.7,
                        "conflict_score": 0.82,
                        "sensitivity": "low",
                        "tags": ["development_principle"],
                        "reason": "explicit_correction_of_existing_memory",
                    }
                ],
                "contradictions": [],
            },
        ),
        GoldenExtractorCase(
            case_id="multi_memory_current_best_merge",
            match_text="merge it into our current-best architecture direction",
            expected={
                "candidates": [
                    {
                        "operation": "MERGE",
                        "memory_type": "domain_principle",
                        "summary": "User wants module work merged into the current-best architecture direction rather than optimized for a tiny loop.",
                        "target_memory_id": "$first_related_memory",
                        "confidence": 0.92,
                        "importance": 0.86,
                        "stability": 0.82,
                        "specificity": 0.84,
                        "novelty": 0.35,
                        "conflict_score": 0.0,
                        "sensitivity": "low",
                        "tags": ["development_principle"],
                        "reason": "multi_memory_merge_signal",
                    }
                ],
                "contradictions": [],
            },
        ),
        GoldenExtractorCase(
            case_id="relationship_signal_companionship",
            match_text="closeness and shared history matter",
            expected={
                "candidates": [
                    {
                        "operation": "ADD",
                        "memory_type": "relationship_signal",
                        "summary": "User wants the companion to remember that companionship closeness and shared history matter to them.",
                        "confidence": 0.91,
                        "importance": 0.84,
                        "stability": 0.74,
                        "specificity": 0.83,
                        "novelty": 1.0,
                        "conflict_score": 0.0,
                        "sensitivity": "medium",
                        "tags": ["companionship", "relationship_signal"],
                        "reason": "explicit_companionship_expectation",
                    }
                ],
                "contradictions": [],
            },
        ),
        GoldenExtractorCase(
            case_id="gradual_preference_no_overextract_single_turn",
            match_text="leaning toward richer eval reports",
            expected={
                "candidates": [
                    {
                        "operation": "NOOP",
                        "memory_type": "procedural_hint",
                        "summary": "The user expressed an uncertain, not-yet-stable preference.",
                        "evidence_refs": [],
                        "confidence": 0.35,
                        "importance": 0.2,
                        "stability": 0.1,
                        "specificity": 0.5,
                        "novelty": 1.0,
                        "conflict_score": 0.0,
                        "sensitivity": "low",
                        "reason": "gradual_preference_requires_more_evidence",
                    }
                ],
                "contradictions": [],
            },
        ),
    ]
