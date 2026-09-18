"""LangChain conversion isolated from provider-independent runtime messages."""
from __future__ import annotations

import hashlib
import json
import inspect
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agent_runtime.errors import ToolModelError
from agent_runtime.security import canonical_json
from agent_runtime.tools.messages import (
    AgentMessage,
    NormalizedToolCall,
    TokenUsage,
    ToolModelResponse,
)


class LangChainToolModelAdapter:
    def __init__(self, model: Any, *, config: Any = None) -> None:
        self._model = model
        self._config = config

    def invoke(
        self,
        messages: list[AgentMessage],
        tools: list[dict[str, Any]],
        *,
        timeout_seconds: float | None = None,
    ) -> ToolModelResponse:
        provider_tools = [
            {
                "type": "function",
                "function": {
                    "name": item["name"],
                    "description": item["description"],
                    "parameters": item["input_schema"],
                },
            }
            for item in tools
        ]
        try:
            bound = self._model.bind_tools(provider_tools) if provider_tools else self._model
            invoke = bound.invoke
            signature = inspect.signature(invoke)
            supports_timeout = "timeout" in signature.parameters or any(
                item.kind == inspect.Parameter.VAR_KEYWORD
                for item in signature.parameters.values()
            )
            kwargs = {"config": self._config}
            if timeout_seconds is not None and supports_timeout:
                kwargs["timeout"] = timeout_seconds
            response = invoke(
                [self._to_provider(item) for item in messages], **kwargs
            )
            return self._from_provider(response)
        except ToolModelError:
            raise
        except Exception as exc:
            raise ToolModelError(
                "The model could not complete the tool-calling turn."
            ) from exc

    @staticmethod
    def _to_provider(message: AgentMessage):
        if message.role == "system":
            return SystemMessage(content=message.content, id=message.message_id)
        if message.role == "user":
            return HumanMessage(content=message.content, id=message.message_id)
        if message.role == "tool":
            return ToolMessage(
                content=message.content,
                tool_call_id=message.tool_call_id,
                name=message.tool_name,
                id=message.message_id,
            )
        calls = [
            {
                "name": item.tool_name,
                "args": item.arguments,
                "id": item.tool_call_id,
                "type": "tool_call",
            }
            for item in message.tool_calls
        ]
        return AIMessage(content=message.content, tool_calls=calls, id=message.message_id)

    @classmethod
    def _from_provider(cls, response: Any) -> ToolModelResponse:
        raw_calls = cls._raw_calls(response)
        content = cls._content(response)
        provider_message_id = getattr(response, "id", None)
        message_id = str(
            provider_message_id
            or cls._stable_id(
                "assistant",
                {
                    "content": content,
                    "tool_calls": [cls._call_fingerprint(item) for item in raw_calls],
                },
            )
        )
        calls = [cls._normalize_call(item, index, message_id) for index, item in enumerate(raw_calls)]
        return ToolModelResponse(
            message=AgentMessage(
                message_id=message_id,
                role="assistant",
                content=content,
                tool_calls=calls,
            ),
            usage=cls._usage(response),
        )

    @staticmethod
    def _raw_calls(response: Any) -> list[Any]:
        calls = getattr(response, "tool_calls", None)
        if calls:
            return list(calls)
        additional = getattr(response, "additional_kwargs", None) or {}
        return list(additional.get("tool_calls") or [])

    @classmethod
    def _normalize_call(
        cls, item: Any, index: int, message_id: str
    ) -> NormalizedToolCall:
        raw = item if isinstance(item, dict) else {}
        function = raw.get("function") if isinstance(raw.get("function"), dict) else {}
        name = raw.get("name") or function.get("name") or "__invalid_tool_call__"
        arguments = raw.get("args", function.get("arguments", {}))
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (json.JSONDecodeError, TypeError):
                arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        call_id = raw.get("id") or cls._stable_id(
            "tool-call",
            {
                "message_id": message_id,
                "index": index,
                "tool_name": str(name),
                "arguments": arguments,
            },
        )
        return NormalizedToolCall(
            tool_call_id=str(call_id), tool_name=str(name), arguments=arguments
        )

    @staticmethod
    def _call_fingerprint(item: Any) -> dict[str, Any]:
        raw = item if isinstance(item, dict) else {}
        function = raw.get("function") if isinstance(raw.get("function"), dict) else {}
        arguments = raw.get("args", function.get("arguments", {}))
        if not isinstance(arguments, (dict, list, str, int, float, bool, type(None))):
            arguments = "invalid"
        return {
            "id": str(raw.get("id") or ""),
            "name": str(raw.get("name") or function.get("name") or ""),
            "arguments": arguments,
        }

    @staticmethod
    def _content(response: Any) -> str:
        content = getattr(response, "content", "")
        if isinstance(content, str):
            return content
        try:
            return canonical_json(content)
        except (TypeError, ValueError):
            return ""

    @staticmethod
    def _stable_id(prefix: str, value: Any) -> str:
        digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
        return f"{prefix}-{digest[:24]}"

    @staticmethod
    def _usage(response: Any) -> TokenUsage:
        usage = getattr(response, "usage_metadata", None) or {}
        if not usage:
            metadata = getattr(response, "response_metadata", None) or {}
            usage = metadata.get("token_usage") or metadata.get("usage") or {}
        input_tokens = usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0
        output_tokens = usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0
        total_tokens = usage.get("total_tokens", input_tokens + output_tokens) or 0
        return TokenUsage(
            input_tokens=int(input_tokens),
            output_tokens=int(output_tokens),
            total_tokens=int(total_tokens),
        )
