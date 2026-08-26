from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator, Protocol
from urllib.parse import quote

from .broker import CredentialedHTTPBroker
from .contracts import ProviderProtocol, ResolvedTarget
from .errors import ModelProviderError, ProviderErrorCode


JsonDict = dict[str, Any]


class ProviderAdapter(Protocol):
    protocol: ProviderProtocol


@dataclass(frozen=True)
class OpenAIChatCompletionsAdapter:
    protocol: ProviderProtocol = ProviderProtocol.OPENAI_CHAT_COMPLETIONS

    def chat(
        self,
        broker: CredentialedHTTPBroker,
        target: ResolvedTarget,
        *,
        system: str,
        user: str,
        timeout_seconds: float,
        cancellation_token: Any | None = None,
        **kwargs: Any,
    ) -> JsonDict:
        body = self._body(target, system=system, user=user, stream=False, kwargs=kwargs)
        response = broker.post_json(
            target.profile,
            self._path(target),
            body,
            timeout_seconds=timeout_seconds,
            cancellation_token=cancellation_token,
        )
        try:
            message = response.payload["choices"][0]["message"]
            content = str(message.get("content") or "")
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelProviderError(
                ProviderErrorCode.INVALID_RESPONSE,
                "chat provider response is missing assistant content",
                provider_request_id=response.provider_request_id,
            ) from exc
        return {
            "content": content,
            "usage": dict(response.payload.get("usage") or {}),
            "model": str(response.payload.get("model") or target.model.model_id),
            "raw": {"provider_request_id": response.provider_request_id},
        }

    def stream_chat(
        self,
        broker: CredentialedHTTPBroker,
        target: ResolvedTarget,
        *,
        system: str,
        user: str,
        timeout_seconds: float,
        cancellation_token: Any | None = None,
        **kwargs: Any,
    ) -> Iterator[JsonDict]:
        body = self._body(target, system=system, user=user, stream=True, kwargs=kwargs)
        for payload in broker.stream_sse(
            target.profile,
            self._path(target),
            body,
            timeout_seconds=timeout_seconds,
            cancellation_token=cancellation_token,
        ):
            choices = payload.get("choices") or []
            delta = ""
            finish_reason = None
            if choices:
                first = choices[0] or {}
                delta = str((first.get("delta") or {}).get("content") or "")
                finish_reason = first.get("finish_reason")
            yield {
                "delta": delta,
                "finish_reason": finish_reason,
                "usage": dict(payload.get("usage") or {}),
                "model": str(payload.get("model") or target.model.model_id),
                "provider_request_id": payload.get("_provider_request_id"),
            }

    @staticmethod
    def _body(
        target: ResolvedTarget,
        *,
        system: str,
        user: str,
        stream: bool,
        kwargs: JsonDict,
    ) -> JsonDict:
        body: JsonDict = {
            "model": target.model.model_id,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": float(kwargs.get("temperature", 0.7)),
            "max_tokens": int(kwargs.get("max_tokens", 800)),
            "stream": stream,
        }
        if stream:
            body["stream_options"] = {"include_usage": True}
        for key in ("thinking", "response_format", "top_p", "stop"):
            value = kwargs.get(key)
            if value is not None:
                body[key] = value
        return body

    @staticmethod
    def _path(target: ResolvedTarget) -> str:
        base = target.profile.endpoint.base_url.rstrip("/")
        if base.endswith("/v1"):
            return "/chat/completions"
        return "/chat/completions"


@dataclass(frozen=True)
class OpenAIEmbeddingsAdapter:
    protocol: ProviderProtocol = ProviderProtocol.OPENAI_EMBEDDINGS

    def embed(
        self,
        broker: CredentialedHTTPBroker,
        target: ResolvedTarget,
        texts: list[str],
        *,
        timeout_seconds: float,
        cancellation_token: Any | None = None,
    ) -> list[list[float]]:
        response = broker.post_json(
            target.profile,
            "/embeddings",
            {"model": target.model.model_id, "input": texts},
            timeout_seconds=timeout_seconds,
            cancellation_token=cancellation_token,
        )
        rows = response.payload.get("data") or []
        if not isinstance(rows, list):
            raise ModelProviderError(
                ProviderErrorCode.INVALID_RESPONSE,
                "embedding provider returned an unexpected data shape",
                provider_request_id=response.provider_request_id,
            )
        try:
            ordered = sorted(rows, key=lambda item: int(item.get("index", 0)))
            vectors = [list(map(float, item["embedding"])) for item in ordered]
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelProviderError(
                ProviderErrorCode.INVALID_RESPONSE,
                "embedding provider returned invalid vectors",
                provider_request_id=response.provider_request_id,
            ) from exc
        if len(vectors) != len(texts):
            raise ModelProviderError(
                ProviderErrorCode.INVALID_RESPONSE,
                "embedding provider returned an unexpected vector count",
                provider_request_id=response.provider_request_id,
            )
        expected = (
            target.model.capabilities.embedding_dimension
            if target.model.source != "probe_provisional"
            else None
        )
        if expected and any(len(vector) != expected for vector in vectors):
            raise ModelProviderError(
                ProviderErrorCode.CAPABILITY_MISMATCH,
                "embedding dimension changed from the verified model manifest",
                provider_request_id=response.provider_request_id,
            )
        return vectors


@dataclass(frozen=True)
class OpenAIResponsesAdapter:
    protocol: ProviderProtocol = ProviderProtocol.OPENAI_RESPONSES

    def chat(
        self,
        broker: CredentialedHTTPBroker,
        target: ResolvedTarget,
        *,
        system: str,
        user: str,
        timeout_seconds: float,
        cancellation_token: Any | None = None,
        **kwargs: Any,
    ) -> JsonDict:
        response = broker.post_json(
            target.profile,
            "/responses",
            self._body(target, system, user, stream=False, kwargs=kwargs),
            timeout_seconds=timeout_seconds,
            cancellation_token=cancellation_token,
        )
        content = str(response.payload.get("output_text") or "")
        if not content:
            parts: list[str] = []
            for output in response.payload.get("output") or []:
                for item in output.get("content") or []:
                    if item.get("type") in {"output_text", "text"}:
                        parts.append(str(item.get("text") or ""))
            content = "".join(parts)
        if not content:
            raise ModelProviderError(
                ProviderErrorCode.INVALID_RESPONSE,
                "responses provider response is missing assistant content",
                provider_request_id=response.provider_request_id,
            )
        return {
            "content": content,
            "usage": dict(response.payload.get("usage") or {}),
            "model": str(response.payload.get("model") or target.model.model_id),
            "raw": {"provider_request_id": response.provider_request_id},
        }

    def stream_chat(
        self,
        broker: CredentialedHTTPBroker,
        target: ResolvedTarget,
        *,
        system: str,
        user: str,
        timeout_seconds: float,
        cancellation_token: Any | None = None,
        **kwargs: Any,
    ) -> Iterator[JsonDict]:
        for event in broker.stream_sse(
            target.profile,
            "/responses",
            self._body(target, system, user, stream=True, kwargs=kwargs),
            timeout_seconds=timeout_seconds,
            cancellation_token=cancellation_token,
        ):
            event_type = str(event.get("type") or "")
            if event_type == "response.failed":
                raise ModelProviderError(
                    ProviderErrorCode.INVALID_RESPONSE,
                    "responses provider reported a failed response",
                    provider_request_id=event.get("_provider_request_id"),
                )
            response = event.get("response") or {}
            yield {
                "delta": str(event.get("delta") or "") if event_type == "response.output_text.delta" else "",
                "finish_reason": "stop" if event_type == "response.completed" else None,
                "usage": dict(response.get("usage") or {}),
                "model": str(response.get("model") or target.model.model_id),
                "provider_request_id": event.get("_provider_request_id"),
            }

    @staticmethod
    def _body(
        target: ResolvedTarget,
        system: str,
        user: str,
        *,
        stream: bool,
        kwargs: JsonDict,
    ) -> JsonDict:
        body: JsonDict = {
            "model": target.model.model_id,
            "instructions": system,
            "input": user,
            "max_output_tokens": int(kwargs.get("max_tokens", 800)),
            "stream": stream,
        }
        if kwargs.get("response_format"):
            body["text"] = {"format": kwargs["response_format"]}
        return body


@dataclass(frozen=True)
class AnthropicMessagesAdapter:
    protocol: ProviderProtocol = ProviderProtocol.ANTHROPIC_MESSAGES

    def chat(
        self,
        broker: CredentialedHTTPBroker,
        target: ResolvedTarget,
        *,
        system: str,
        user: str,
        timeout_seconds: float,
        cancellation_token: Any | None = None,
        **kwargs: Any,
    ) -> JsonDict:
        response = broker.post_json(
            target.profile,
            self._path(target),
            self._body(target, system, user, stream=False, kwargs=kwargs),
            timeout_seconds=timeout_seconds,
            cancellation_token=cancellation_token,
        )
        content = "".join(
            str(item.get("text") or "")
            for item in response.payload.get("content") or []
            if item.get("type") == "text"
        )
        if not content:
            raise ModelProviderError(
                ProviderErrorCode.INVALID_RESPONSE,
                "anthropic response is missing assistant text",
                provider_request_id=response.provider_request_id,
            )
        return {
            "content": content,
            "usage": dict(response.payload.get("usage") or {}),
            "model": str(response.payload.get("model") or target.model.model_id),
            "raw": {"provider_request_id": response.provider_request_id},
        }

    def stream_chat(
        self,
        broker: CredentialedHTTPBroker,
        target: ResolvedTarget,
        *,
        system: str,
        user: str,
        timeout_seconds: float,
        cancellation_token: Any | None = None,
        **kwargs: Any,
    ) -> Iterator[JsonDict]:
        usage: JsonDict = {}
        for event in broker.stream_sse(
            target.profile,
            self._path(target),
            self._body(target, system, user, stream=True, kwargs=kwargs),
            timeout_seconds=timeout_seconds,
            cancellation_token=cancellation_token,
        ):
            event_type = str(event.get("type") or "")
            if event_type == "message_start":
                usage.update(dict((event.get("message") or {}).get("usage") or {}))
            elif event_type == "message_delta":
                usage.update(dict(event.get("usage") or {}))
            delta = event.get("delta") or {}
            yield {
                "delta": str(delta.get("text") or "") if event_type == "content_block_delta" else "",
                "finish_reason": delta.get("stop_reason") if event_type == "message_delta" else None,
                "usage": dict(usage),
                "model": target.model.model_id,
                "provider_request_id": event.get("_provider_request_id"),
            }

    @staticmethod
    def _path(target: ResolvedTarget) -> str:
        return "/messages" if target.profile.endpoint.base_url.rstrip("/").endswith("/v1") else "/v1/messages"

    @staticmethod
    def _body(
        target: ResolvedTarget,
        system: str,
        user: str,
        *,
        stream: bool,
        kwargs: JsonDict,
    ) -> JsonDict:
        return {
            "model": target.model.model_id,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "temperature": float(kwargs.get("temperature", 0.7)),
            "max_tokens": int(kwargs.get("max_tokens", 800)),
            "stream": stream,
        }


@dataclass(frozen=True)
class GeminiNativeAdapter:
    protocol: ProviderProtocol = ProviderProtocol.GEMINI_NATIVE

    def chat(
        self,
        broker: CredentialedHTTPBroker,
        target: ResolvedTarget,
        *,
        system: str,
        user: str,
        timeout_seconds: float,
        cancellation_token: Any | None = None,
        **kwargs: Any,
    ) -> JsonDict:
        response = broker.post_json(
            target.profile,
            self._path(target, stream=False),
            self._body(system, user, kwargs),
            timeout_seconds=timeout_seconds,
            cancellation_token=cancellation_token,
        )
        content = self._content(response.payload)
        if not content:
            raise ModelProviderError(
                ProviderErrorCode.INVALID_RESPONSE,
                "gemini response is missing assistant text",
                provider_request_id=response.provider_request_id,
            )
        return {
            "content": content,
            "usage": self._usage(response.payload),
            "model": target.model.model_id,
            "raw": {"provider_request_id": response.provider_request_id},
        }

    def stream_chat(
        self,
        broker: CredentialedHTTPBroker,
        target: ResolvedTarget,
        *,
        system: str,
        user: str,
        timeout_seconds: float,
        cancellation_token: Any | None = None,
        **kwargs: Any,
    ) -> Iterator[JsonDict]:
        for event in broker.stream_sse(
            target.profile,
            self._path(target, stream=True),
            self._body(system, user, kwargs),
            timeout_seconds=timeout_seconds,
            cancellation_token=cancellation_token,
        ):
            candidate = (event.get("candidates") or [{}])[0]
            yield {
                "delta": self._content(event),
                "finish_reason": candidate.get("finishReason"),
                "usage": self._usage(event),
                "model": target.model.model_id,
                "provider_request_id": event.get("_provider_request_id"),
            }

    @staticmethod
    def _path(target: ResolvedTarget, *, stream: bool) -> str:
        model = quote(target.model.model_id, safe="")
        suffix = ":streamGenerateContent?alt=sse" if stream else ":generateContent"
        return f"/models/{model}{suffix}"

    @staticmethod
    def _body(system: str, user: str, kwargs: JsonDict) -> JsonDict:
        config: JsonDict = {
            "temperature": float(kwargs.get("temperature", 0.7)),
            "maxOutputTokens": int(kwargs.get("max_tokens", 800)),
        }
        if kwargs.get("response_format"):
            config["responseMimeType"] = "application/json"
        return {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": config,
        }

    @staticmethod
    def _content(payload: JsonDict) -> str:
        candidates = payload.get("candidates") or []
        if not candidates:
            return ""
        return "".join(
            str(item.get("text") or "")
            for item in ((candidates[0].get("content") or {}).get("parts") or [])
        )

    @staticmethod
    def _usage(payload: JsonDict) -> JsonDict:
        usage = dict(payload.get("usageMetadata") or {})
        return {
            "prompt_tokens": int(usage.get("promptTokenCount") or 0),
            "completion_tokens": int(usage.get("candidatesTokenCount") or 0),
            "total_tokens": int(usage.get("totalTokenCount") or 0),
        }


@dataclass(frozen=True)
class HTTPRerankAdapter:
    protocol: ProviderProtocol = ProviderProtocol.HTTP_RERANK

    def rerank(
        self,
        broker: CredentialedHTTPBroker,
        target: ResolvedTarget,
        *,
        query: str,
        documents: list[JsonDict],
        timeout_seconds: float,
        cancellation_token: Any | None = None,
    ) -> JsonDict:
        response = broker.post_json(
            target.profile,
            "/rerank",
            {
                "model": target.model.model_id,
                "query": query,
                "documents": documents,
                "top_n": len(documents),
            },
            timeout_seconds=timeout_seconds,
            cancellation_token=cancellation_token,
        )
        return response.payload


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[ProviderProtocol, Any] = {
            ProviderProtocol.OPENAI_CHAT_COMPLETIONS: OpenAIChatCompletionsAdapter(),
            ProviderProtocol.OPENAI_RESPONSES: OpenAIResponsesAdapter(),
            ProviderProtocol.ANTHROPIC_MESSAGES: AnthropicMessagesAdapter(),
            ProviderProtocol.GEMINI_NATIVE: GeminiNativeAdapter(),
            ProviderProtocol.OPENAI_EMBEDDINGS: OpenAIEmbeddingsAdapter(),
            ProviderProtocol.HTTP_RERANK: HTTPRerankAdapter(),
        }

    def register(self, adapter: Any, *, replace: bool = False) -> None:
        protocol = ProviderProtocol(adapter.protocol)
        if protocol in self._adapters and not replace:
            raise ValueError(f"provider adapter already registered: {protocol.value}")
        self._adapters[protocol] = adapter

    def get(self, protocol: ProviderProtocol) -> Any:
        adapter = self._adapters.get(protocol)
        if adapter is None:
            raise ModelProviderError(
                ProviderErrorCode.UNSUPPORTED_PROTOCOL,
                f"provider protocol is not installed: {protocol.value}",
            )
        return adapter
