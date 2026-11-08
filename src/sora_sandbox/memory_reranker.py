from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from .memory_schema import FusedMemoryHit, JsonDict, RecallPlan
from .turn_observability import observe_http


class MemoryReranker(Protocol):
    name: str
    model: str

    def rerank(self, query_text: str, hits: list[FusedMemoryHit], plan: RecallPlan) -> JsonDict:
        ...


@dataclass
class FeatureMemoryReranker:
    name: str = "feature_reranker"
    model: str = "feature_reranker_v1"

    def rerank(self, query_text: str, hits: list[FusedMemoryHit], plan: RecallPlan) -> JsonDict:
        query = query_text.lower()
        reranked: list[FusedMemoryHit] = []
        score_details: list[JsonDict] = []
        for hit in hits:
            row = hit.row
            haystack = _candidate_text(hit).lower()
            score = hit.score
            score += float(row["importance"]) * 0.006
            score += float(row["confidence"]) * 0.004
            score += min(float(row["access_count"] or 0), 20.0) * 0.0004
            exact_salience = sum(
                1 for term in plan.salient_terms if term and term.lower() in haystack
            )
            if exact_salience:
                score += 0.006 * exact_salience
            if str(row["type"]) in {"shared_episode", "episode", "worldops_episode"} and any(
                term in query for term in ("previous", "last time", "together", "episode")
            ):
                score += 0.006
            reranked.append(_with_score(hit, score))
            score_details.append(
                {
                    "memory_id": hit.memory_id,
                    "base_score": hit.score,
                    "rerank_score": score,
                    "provider_score": None,
                }
            )
        reranked.sort(key=lambda item: item.score, reverse=True)
        return {
            "hits": reranked,
            "metadata": {
                "provider": self.name,
                "model": self.model,
                "degraded": False,
                "score_details": score_details[:50],
            },
        }


@dataclass
class HTTPCrossEncoderReranker:
    endpoint: str
    api_key: str = ""
    model: str = "cross-encoder"
    name: str = "http_cross_encoder"
    timeout_seconds: float = 20.0
    base_weight: float = 0.35
    provider_weight: float = 0.65

    def rerank(self, query_text: str, hits: list[FusedMemoryHit], plan: RecallPlan) -> JsonDict:
        if not hits:
            return {"hits": [], "metadata": self._metadata(score_details=[])}
        documents = [_document_for_hit(hit) for hit in hits]
        response = self._post_json(
            {
                "model": self.model,
                "query": query_text,
                "top_n": len(documents),
                "documents": documents,
                "metadata": {
                    "planner_mode": plan.planner_mode,
                    "explicit_recall": plan.explicit_recall,
                },
            }
        )
        provider_scores = _parse_rerank_scores(response, documents)
        normalized = _normalize_scores(provider_scores)
        reranked = []
        score_details = []
        for index, hit in enumerate(hits):
            provider_score = normalized.get(hit.memory_id)
            if provider_score is None:
                provider_score = normalized.get(str(index), 0.0)
            score = hit.score * self.base_weight + provider_score * self.provider_weight
            reranked.append(_with_score(hit, score))
            score_details.append(
                {
                    "memory_id": hit.memory_id,
                    "base_score": hit.score,
                    "provider_score": provider_score,
                    "rerank_score": score,
                }
            )
        reranked.sort(key=lambda item: item.score, reverse=True)
        return {
            "hits": reranked,
            "metadata": self._metadata(
                score_details=score_details[:50],
                usage=response.get("usage") or {},
            ),
        }

    def _post_json(self, payload: JsonDict) -> JsonDict:
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            self.endpoint,
            data=data,
            headers=headers,
            method="POST",
        )
        try:
            with observe_http("provider.http.compat_rerank"), urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:  # pragma: no cover - network/provider path.
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"reranker HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:  # pragma: no cover - network/provider path.
            raise RuntimeError(f"reranker unavailable: {exc.reason}") from exc
        try:
            return json.loads(body or "{}")
        except json.JSONDecodeError as exc:  # pragma: no cover - defensive provider path.
            raise RuntimeError(f"reranker returned non-json response: {body[:200]}") from exc

    def _metadata(self, *, score_details: list[JsonDict], usage: JsonDict | None = None) -> JsonDict:
        return {
            "provider": self.name,
            "model": self.model,
            "endpoint": self.endpoint,
            "degraded": False,
            "score_details": score_details,
            "usage": usage or {},
        }


@dataclass
class OpenAICompatibleLLMReranker:
    api_key: str
    base_url: str
    model: str
    name: str = "openai_compatible_llm_reranker"
    timeout_seconds: float = 30.0
    base_weight: float = 0.45
    provider_weight: float = 0.55

    def rerank(self, query_text: str, hits: list[FusedMemoryHit], plan: RecallPlan) -> JsonDict:
        if not hits:
            return {"hits": [], "metadata": self._metadata(score_details=[])}
        documents = [_document_for_hit(hit) for hit in hits]
        response = self._chat_json(
            {
                "query": query_text,
                "explicit_recall": plan.explicit_recall,
                "salient_terms": list(plan.salient_terms),
                "documents": documents,
            }
        )
        scores = _parse_llm_scores(response, documents)
        normalized = _normalize_scores(scores)
        reranked = []
        score_details = []
        for index, hit in enumerate(hits):
            provider_score = normalized.get(hit.memory_id)
            if provider_score is None:
                provider_score = normalized.get(str(index), 0.0)
            score = hit.score * self.base_weight + provider_score * self.provider_weight
            reranked.append(_with_score(hit, score))
            score_details.append(
                {
                    "memory_id": hit.memory_id,
                    "base_score": hit.score,
                    "provider_score": provider_score,
                    "rerank_score": score,
                }
            )
        reranked.sort(key=lambda item: item.score, reverse=True)
        return {
            "hits": reranked,
            "metadata": self._metadata(
                score_details=score_details[:50],
                usage=response.get("usage") or {},
            ),
        }

    def _chat_json(self, input_package: JsonDict) -> JsonDict:
        payload = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a memory reranker. Return JSON only: "
                        "{\"scores\":[{\"memory_id\":\"...\",\"score\":0.0,\"reason\":\"...\"}]}."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(input_package, ensure_ascii=False),
                },
            ],
        }
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            _chat_completions_url(self.base_url),
            data=data,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with observe_http("provider.http.compat_llm_rerank"), urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:  # pragma: no cover - network/provider path.
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"LLM reranker HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:  # pragma: no cover - network/provider path.
            raise RuntimeError(f"LLM reranker unavailable: {exc.reason}") from exc
        data = json.loads(body)
        content = data["choices"][0]["message"]["content"]
        parsed = _json_object_from_text(content)
        parsed["usage"] = data.get("usage") or {}
        return parsed

    def _metadata(self, *, score_details: list[JsonDict], usage: JsonDict | None = None) -> JsonDict:
        return {
            "provider": self.name,
            "model": self.model,
            "base_url": self.base_url,
            "degraded": False,
            "score_details": score_details,
            "usage": usage or {},
        }


def create_reranker_from_env() -> MemoryReranker:
    provider = (
        os.getenv("SORA_RERANK_PROVIDER", "").strip().lower()
        or os.getenv("SORA_MEMORY_RERANKER", "").strip().lower()
    )
    if provider in {"http", "cross_encoder", "http_cross_encoder"}:
        endpoint = (
            os.getenv("SORA_RERANK_URL", "").strip()
            or os.getenv("SORA_CROSS_ENCODER_URL", "").strip()
        )
        if not endpoint:
            raise RuntimeError("SORA_RERANK_PROVIDER=http requires SORA_RERANK_URL")
        return HTTPCrossEncoderReranker(
            endpoint=endpoint,
            api_key=os.getenv("SORA_RERANK_API_KEY", "").strip(),
            model=os.getenv("SORA_RERANK_MODEL", "").strip() or "cross-encoder",
            timeout_seconds=_env_float("SORA_RERANK_TIMEOUT_SECONDS", 20.0),
        )
    if provider in {"llm", "openai", "openai_compatible", "llm_openai_compatible"}:
        api_key = (
            os.getenv("SORA_RERANK_API_KEY", "").strip()
            or os.getenv("SORA_LLM_API_KEY", "").strip()
            or os.getenv("OPENAI_API_KEY", "").strip()
        )
        base_url = (
            os.getenv("SORA_RERANK_BASE_URL", "").strip()
            or os.getenv("SORA_LLM_BASE_URL", "").strip()
            or os.getenv("OPENAI_BASE_URL", "").strip()
            or "https://api.openai.com/v1"
        )
        model = (
            os.getenv("SORA_RERANK_MODEL", "").strip()
            or os.getenv("SORA_LLM_MODEL", "").strip()
            or "gpt-4.1-mini"
        )
        if not api_key:
            raise RuntimeError("LLM reranker requires SORA_RERANK_API_KEY, SORA_LLM_API_KEY, or OPENAI_API_KEY")
        return OpenAICompatibleLLMReranker(
            api_key=api_key,
            base_url=base_url,
            model=model,
            timeout_seconds=_env_float("SORA_RERANK_TIMEOUT_SECONDS", 30.0),
        )
    return FeatureMemoryReranker()


def _document_for_hit(hit: FusedMemoryHit) -> JsonDict:
    row = hit.row
    return {
        "id": hit.memory_id,
        "text": _candidate_text(hit),
        "base_score": hit.score,
        "matched_backends": list(hit.matched_backends),
        "metadata": {
            "type": str(row["type"]),
            "confidence": float(row["confidence"]),
            "importance": float(row["importance"]),
            "tags_json": str(row["tags_json"]),
        },
    }


def _candidate_text(hit: FusedMemoryHit) -> str:
    row = hit.row
    return " ".join(
        [
            str(row["summary"]),
            str(row["type"]),
            str(row["tags_json"]),
        ]
    )


def _parse_rerank_scores(response: JsonDict, documents: list[JsonDict]) -> dict[str, float]:
    scores: dict[str, float] = {}
    results = response.get("results")
    if isinstance(results, list):
        for item in results:
            if not isinstance(item, dict):
                continue
            index = item.get("index")
            memory_id = item.get("memory_id")
            document = item.get("document")
            if memory_id is None and isinstance(document, dict):
                memory_id = document.get("id") or document.get("memory_id")
            if memory_id is None and index is not None and 0 <= int(index) < len(documents):
                memory_id = documents[int(index)]["id"]
            score = item.get("relevance_score", item.get("score"))
            if score is None:
                continue
            if memory_id is not None:
                scores[str(memory_id)] = float(score)
            if index is not None:
                scores[str(index)] = float(score)
    raw_scores = response.get("scores")
    if isinstance(raw_scores, list):
        for index, value in enumerate(raw_scores):
            if index < len(documents):
                scores[documents[index]["id"]] = float(value)
                scores[str(index)] = float(value)
    if not scores:
        raise RuntimeError("reranker returned no usable scores")
    return scores


def _parse_llm_scores(response: JsonDict, documents: list[JsonDict]) -> dict[str, float]:
    scores: dict[str, float] = {}
    raw_scores = response.get("scores")
    if isinstance(raw_scores, list):
        for index, item in enumerate(raw_scores):
            if isinstance(item, dict):
                memory_id = item.get("memory_id") or item.get("id")
                score = item.get("score", item.get("relevance_score"))
                if memory_id is not None and score is not None:
                    scores[str(memory_id)] = float(score)
                if score is not None:
                    scores[str(index)] = float(score)
            elif index < len(documents):
                scores[documents[index]["id"]] = float(item)
                scores[str(index)] = float(item)
    if scores:
        return scores
    return _parse_rerank_scores(response, documents)


def _normalize_scores(scores: dict[str, float]) -> dict[str, float]:
    if not scores:
        return {}
    maximum = max(abs(value) for value in scores.values())
    if maximum <= 1.0:
        return {key: max(0.0, min(1.0, float(value))) for key, value in scores.items()}
    return {key: max(0.0, min(1.0, float(value) / maximum)) for key, value in scores.items()}


def _with_score(hit: FusedMemoryHit, score: float) -> FusedMemoryHit:
    return FusedMemoryHit(
        memory_id=hit.memory_id,
        score=score,
        row=hit.row,
        matched_backends=hit.matched_backends,
    )


def _json_object_from_text(text: str) -> JsonDict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        return json.loads(text[start : end + 1])


def _chat_completions_url(base_url: str) -> str:
    clean = base_url.rstrip("/")
    if clean.endswith("/chat/completions"):
        return clean
    return f"{clean}/chat/completions"


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default
