from __future__ import annotations

from typing import Any, Mapping

from .memory_embedding import EmbeddingProvider
from .memory_extractor_provider import OpenAICompatibleExtractorProvider
from .memory_recall_planner import OpenAICompatibleRecallPlannerProvider
from .memory_reranker import (
    MemoryReranker,
    _document_for_hit,
    _normalize_scores,
    _parse_rerank_scores,
    _with_score,
)
from .memory_schema import FusedMemoryHit, JsonDict, RecallPlan
from .memory_surface_policy import OpenAICompatibleSurfacePolicyProvider
from .model_provider import ModelProviderSystem, Workload
from .model_provider.errors import ModelProviderError, ProviderErrorCode


def _chat_completion_payload(result: Mapping[str, Any]) -> JsonDict:
    return {
        "model": result.get("model"),
        "choices": [{"message": {"content": str(result.get("content") or "")}}],
        "usage": dict(result.get("usage") or {}),
    }


def _invoke_routed_chat(llm: Any, body: JsonDict, workload: Workload) -> JsonDict:
    messages = list(body.get("messages") or [])
    system_parts = [str(item.get("content") or "") for item in messages if item.get("role") == "system"]
    user_parts = [str(item.get("content") or "") for item in messages if item.get("role") != "system"]
    result = llm.chat(
        system="\n\n".join(system_parts),
        user="\n\n".join(user_parts),
        workload=workload.value,
        temperature=float(body.get("temperature", 0.0)),
        max_tokens=int(body.get("max_tokens", 1200)),
        response_format=body.get("response_format"),
        thinking={"type": "disabled"},
    )
    return _chat_completion_payload(result)


class RoutedMemoryExtractorProvider(OpenAICompatibleExtractorProvider):
    def __init__(self, llm: Any) -> None:
        super().__init__(api_key="", base_url="", model="provider-route")
        self.llm = llm

    def _post_chat_completion(self, body: JsonDict) -> JsonDict:
        return _invoke_routed_chat(self.llm, body, Workload.MEMORY_EXTRACTION)


class RoutedRecallPlannerProvider(OpenAICompatibleRecallPlannerProvider):
    def __init__(self, llm: Any) -> None:
        super().__init__(api_key="", base_url="", model="provider-route")
        self.llm = llm

    def _post_chat_completion(self, body: JsonDict) -> JsonDict:
        return _invoke_routed_chat(self.llm, body, Workload.MEMORY_RECALL_PLANNING)


class RoutedSurfacePolicyProvider(OpenAICompatibleSurfacePolicyProvider):
    def __init__(self, llm: Any) -> None:
        super().__init__(api_key="", base_url="", model="provider-route")
        self.llm = llm

    def _post_chat_completion(self, body: JsonDict) -> JsonDict:
        return _invoke_routed_chat(self.llm, body, Workload.MEMORY_SURFACE)


class RoutedEmbeddingProvider(EmbeddingProvider):
    def __init__(self, provider_system: ModelProviderSystem) -> None:
        self.provider_system = provider_system

    @property
    def model(self) -> str:
        return self._target(Workload.EMBEDDING_DOCUMENTS).model.model_id

    @property
    def dimension(self) -> int:
        return int(
            self._target(Workload.EMBEDDING_DOCUMENTS).model.capabilities.embedding_dimension
            or 0
        )

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        target = self._target(Workload.EMBEDDING_DOCUMENTS)
        adapter = self.provider_system.adapters.get(target.profile.protocol)
        embed = getattr(adapter, "embed", None)
        if not callable(embed):
            raise ModelProviderError(
                ProviderErrorCode.UNSUPPORTED_PROTOCOL,
                "configured embedding provider does not implement embeddings",
            )
        return embed(
            self.provider_system.broker,
            target,
            texts,
            timeout_seconds=30.0,
        )

    def embed_query(self, text: str) -> list[float]:
        target = self._target(Workload.EMBEDDING_QUERY)
        adapter = self.provider_system.adapters.get(target.profile.protocol)
        embed = getattr(adapter, "embed", None)
        if not callable(embed):
            raise ModelProviderError(
                ProviderErrorCode.UNSUPPORTED_PROTOCOL,
                "configured embedding provider does not implement embeddings",
            )
        return embed(
            self.provider_system.broker,
            target,
            [text],
            timeout_seconds=20.0,
        )[0]

    def _target(self, workload: Workload):
        return self.provider_system.router.resolve(workload).primary


class RoutedMemoryReranker(MemoryReranker):
    name = "provider_reranker"

    def __init__(
        self,
        provider_system: ModelProviderSystem,
        *,
        base_weight: float = 0.35,
        provider_weight: float = 0.65,
    ) -> None:
        self.provider_system = provider_system
        self.base_weight = base_weight
        self.provider_weight = provider_weight

    @property
    def model(self) -> str:
        return self.provider_system.router.resolve(Workload.MEMORY_RERANK).primary.model.model_id

    def rerank(
        self,
        query_text: str,
        hits: list[FusedMemoryHit],
        plan: RecallPlan,
    ) -> JsonDict:
        if not hits:
            return {"hits": [], "metadata": self._metadata([], {})}
        target = self.provider_system.router.resolve(Workload.MEMORY_RERANK).primary
        adapter = self.provider_system.adapters.get(target.profile.protocol)
        rerank = getattr(adapter, "rerank", None)
        if not callable(rerank):
            raise ModelProviderError(
                ProviderErrorCode.UNSUPPORTED_PROTOCOL,
                "configured rerank provider does not implement reranking",
            )
        documents = [_document_for_hit(hit) for hit in hits]
        response = rerank(
            self.provider_system.broker,
            target,
            query=query_text,
            documents=documents,
            timeout_seconds=20.0,
        )
        normalized = _normalize_scores(_parse_rerank_scores(response, documents))
        reranked: list[FusedMemoryHit] = []
        details: list[JsonDict] = []
        for index, hit in enumerate(hits):
            provider_score = normalized.get(hit.memory_id, normalized.get(str(index), 0.0))
            score = hit.score * self.base_weight + provider_score * self.provider_weight
            reranked.append(_with_score(hit, score))
            details.append(
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
            "metadata": self._metadata(details[:50], dict(response.get("usage") or {})),
        }

    def _metadata(self, score_details: list[JsonDict], usage: JsonDict) -> JsonDict:
        snapshot = self.provider_system.router.resolve(Workload.MEMORY_RERANK)
        return {
            "provider": self.name,
            "provider_profile_id": snapshot.primary.profile.profile_id,
            "model": snapshot.primary.model.model_id,
            "degraded": False,
            "score_details": score_details,
            "usage": usage,
        }
