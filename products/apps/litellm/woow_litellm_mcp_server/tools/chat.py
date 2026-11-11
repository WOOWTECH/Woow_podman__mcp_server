"""Chat / inference tools (category: chat)."""

from __future__ import annotations

import json
from typing import Any

from fastmcp import Context

from ..deps import litellm_client
from ..errors import ToolError
from ..gating import ToolGate
from ..registry import OP_CREATE
from ._common import prune_none, writing

def _assemble_stream(raw: str) -> dict[str, Any]:
    """Re-assemble an SSE ``chat.completion.chunk`` stream into one response.

    LiteLLM answers ``stream: true`` with ``text/event-stream``; the JSON
    decoder in ``errors.json_body`` cannot parse that and hands back
    ``{"raw": "<concatenated SSE frames>"}``. Returning that to the caller is a
    silent contract break — no assembled message, no usage, no cost, and no
    error. Merge the deltas back into the ordinary chat.completion envelope so
    the tool's documented shape is what callers actually receive.
    """
    envelope: dict[str, Any] = {}
    # index -> accumulated choice
    choices: dict[int, dict[str, Any]] = {}
    saw_chunk = False

    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            chunk = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(chunk, dict):
            continue
        saw_chunk = True

        for key in ("id", "created", "model", "system_fingerprint"):
            if chunk.get(key) is not None:
                envelope[key] = chunk[key]
        # Usage only appears on the final chunk (and only with stream_options).
        if isinstance(chunk.get("usage"), dict):
            envelope["usage"] = chunk["usage"]

        for choice in chunk.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            index = int(choice.get("index") or 0)
            acc = choices.setdefault(
                index,
                {"index": index, "message": {"role": "assistant", "content": ""},
                 "finish_reason": None},
            )
            delta = choice.get("delta") or {}
            if isinstance(delta, dict):
                if delta.get("role"):
                    acc["message"]["role"] = delta["role"]
                if delta.get("content"):
                    acc["message"]["content"] += str(delta["content"])
                if delta.get("reasoning_content"):
                    acc["message"]["reasoning_content"] = (
                        acc["message"].get("reasoning_content", "")
                        + str(delta["reasoning_content"])
                    )
                if delta.get("tool_calls"):
                    acc["message"].setdefault("tool_calls", []).extend(
                        delta["tool_calls"]
                    )
            if choice.get("finish_reason"):
                acc["finish_reason"] = choice["finish_reason"]

    if not saw_chunk:
        raise ToolError(
            "LiteLLM returned a streaming response that could not be parsed "
            "into a chat completion. Retry with stream=false."
        )

    envelope["object"] = "chat.completion"
    envelope["choices"] = [choices[i] for i in sorted(choices)]
    return envelope


def register(mcp: Any, gate: ToolGate) -> None:
    if gate.is_tool_enabled("litellm_chat_completion"):

        @mcp.tool(
            name="litellm_chat_completion",
            annotations=writing("Chat completion"),
        )
        async def litellm_chat_completion(
            ctx: Context,
            model: str,
            messages: list[dict[str, Any]],
            temperature: float | None = None,
            max_tokens: int | None = None,
            top_p: float | None = None,
            stream: bool = False,
            extra_body: dict[str, Any] | None = None,
        ) -> dict:
            """Run an OpenAI-compatible chat completion through LiteLLM.

            ``messages`` is the OpenAI chat array (``[{"role", "content"}]``).

            ``stream`` is accepted for parity: the upstream SSE stream is
            re-assembled here, so this tool ALWAYS returns the full
            ``chat.completion`` JSON object regardless of its value.

            ``extra_body`` is merged UNDER the explicit arguments — ``model``,
            ``messages`` and ``stream`` always win, so an extra_body key can add
            provider-specific options but can never redirect the request to a
            different model.
            """
            gate.require_operation("litellm_chat_completion", OP_CREATE)
            # extra_body FIRST so the typed arguments below overwrite it, never
            # the other way round. extra_body is an untyped passthrough a
            # calling LLM may populate from user-supplied text; merged last it
            # silently redirected a model="gpt-4o-mini" call to another provider
            # (and that provider's price) while reporting success.
            body: dict[str, Any] = dict(extra_body or {})
            body.update(
                {
                    "model": model,
                    "messages": messages,
                    "stream": bool(stream),
                }
            )
            body.update(
                prune_none(
                    {
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                        "top_p": top_p,
                    }
                )
            )
            result = await litellm_client(ctx).post(
                "/v1/chat/completions", json_data=body
            )
            # json_body() wraps a non-JSON payload as {"raw": ...}; for this
            # endpoint that means an SSE stream, which is a failure to honour
            # the documented return shape rather than a success value.
            if isinstance(result, dict) and set(result) == {"raw"}:
                return _assemble_stream(str(result["raw"]))
            return result

    if gate.is_tool_enabled("litellm_token_counter"):

        @mcp.tool(
            name="litellm_token_counter",
            annotations=writing("Token counter"),
        )
        async def litellm_token_counter(
            ctx: Context,
            model: str,
            prompt: str | None = None,
            messages: list[dict[str, Any]] | None = None,
        ) -> dict:
            """Count prompt/message tokens for a model without inference.

            Tokenization can fetch remote images or tokenizer resources.
            Requires active-operation permission; does not request the optional
            provider token-count endpoint.

            Provide either ``prompt`` (a raw string) or ``messages`` (the chat
            array). Returns the token count and the tokenizer used.
            """
            gate.require_operation("litellm_token_counter", OP_CREATE)
            body = prune_none(
                {"model": model, "prompt": prompt, "messages": messages}
            )
            return await litellm_client(ctx).post(
                "/utils/token_counter", json_data=body
            )
