"""Tool-calling agent loop over Gemini, Cohere and (owner only) Claude.

The model gets a system prompt, the user's message (plus any images) and a set
of tools. Each
round it either answers or asks for tools; we run them, hand back the results
and repeat until it answers (or runs out of rounds, at which point tools are
switched off so it has to answer with what it has).

Gemini and Cohere round-robin across their configured API keys. Claude goes
through the owner's local claude-api server (Anthropic Messages format) and is
only ever used for the owner's own messages — a Pro/Max plan is for personal
use. If a provider fails outright the whole run restarts on the next. Tools are
read-only, so re-running them on failover is harmless.
"""

import base64
import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import aiohttp
import cohere

from xiap import config

log = logging.getLogger(__name__)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Tool results are truncated to this many characters so one call can't blow up the prompt.
MAX_TOOL_RESULT_CHARS = 6000


@dataclass
class Image:
    mime: str  # e.g. "image/png"
    data: bytes

    def b64(self) -> str:
        return base64.b64encode(self.data).decode()


GEMINI_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/heic", "image/heif"}
CLAUDE_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


def _image_note(images: list[Image], supported: set[str] | None) -> str:
    """Tell the model about attached images it won't be able to see."""
    skipped = len(images) if supported is None else sum(i.mime not in supported for i in images)
    if not skipped:
        return ""
    return f"\n\n({skipped} attached image(s) couldn't be shown to you — don't pretend to see them.)"


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict  # JSON schema for the arguments object
    run: Callable[..., Awaitable[str]]


class KeyRing:
    """Round-robin over any number of keys/clients, starting each request one
    position later than the last so load spreads evenly."""

    def __init__(self, items: list):
        self.items = items
        self._start = 0

    def rotation(self) -> list:
        """All items, beginning at the current start position, then advance it."""
        ordered = self.items[self._start:] + self.items[: self._start]
        self._start = (self._start + 1) % len(self.items)
        return ordered


async def _run_tool(tools: dict[str, Tool], name: str, args: dict) -> str:
    tool = tools.get(name)
    if tool is None:
        return f"Error: unknown tool {name!r}"
    log.info("Tool call: %s(%s)", name, json.dumps(args, ensure_ascii=False))
    try:
        result = await tool.run(**args)
    except Exception as e:  # noqa: BLE001 — report tool errors back to the model
        log.warning("Tool %s failed: %s", name, e)
        result = f"Error: {e}"
    if len(result) > MAX_TOOL_RESULT_CHARS:
        result = result[:MAX_TOOL_RESULT_CHARS] + "\n…(truncated)"
    return result


class Agent:
    def __init__(self, http: aiohttp.ClientSession) -> None:
        self.http = http
        self.gemini_keys = KeyRing(config.GEMINI_API_KEYS) if config.GEMINI_API_KEYS else None
        self.cohere_clients = (
            KeyRing([cohere.AsyncClientV2(api_key=k) for k in config.COHERE_API_KEYS])
            if config.COHERE_API_KEYS
            else None
        )

        # Ordered provider list: primary first, the other as fallback. Claude is
        # kept separate and only put in front for the owner's messages.
        self.claude = self._run_claude if config.CLAUDE_API_URL else None
        available = {}
        if self.gemini_keys:
            available["gemini"] = self._run_gemini
        if self.cohere_clients:
            available["cohere"] = self._run_cohere
        if config.CHAT_PROVIDER in available:
            order = [config.CHAT_PROVIDER] + [n for n in available if n != config.CHAT_PROVIDER]
        else:  # "auto": prefer Gemini's daily-resetting free tier
            order = list(available)
        self.providers = [(name, available[name]) for name in order]
        log.info("Chat providers (in order): %s", [n for n, _ in self.providers] or "NONE")

    async def run(
        self, system: str, prompt: str, tools: list[Tool], images: list[Image] = (), owner: bool = False
    ) -> str | None:
        """Run the agent loop; None if every provider failed."""
        by_name = {t.name: t for t in tools}
        providers = self.providers
        if owner and self.claude:
            providers = [("claude", self.claude)] + providers
        for name, provider in providers:
            try:
                answer = await provider(system, prompt, by_name, list(images))
                log.info("Answered by %s", name)
                return answer
            except Exception as e:  # noqa: BLE001 — fall through to the next provider
                log.error("Provider %s failed: %s", name, e)
        return None

    # --- Gemini ---------------------------------------------------------------

    async def _gemini_request(self, payload: dict) -> dict:
        url = GEMINI_URL.format(model=config.GEMINI_MODEL)
        last_error: Exception | None = None
        for key in self.gemini_keys.rotation():
            try:
                async with self.http.post(url, headers={"x-goog-api-key": key}, json=payload) as resp:
                    data = await resp.json()
                    if resp.status != 200:
                        raise RuntimeError(data.get("error", {}).get("message", f"HTTP {resp.status}"))
                    return data["candidates"][0]["content"]
            except Exception as e:  # noqa: BLE001 — rotate to the next key on any API failure
                last_error = e
                log.warning("Gemini request failed, rotating key: %s", e)
        raise RuntimeError(f"all Gemini keys failed: {last_error}")

    async def _run_gemini(self, system: str, prompt: str, tools: dict[str, Tool], images: list[Image]) -> str:
        parts = [
            {"inline_data": {"mime_type": i.mime, "data": i.b64()}}
            for i in images
            if i.mime in GEMINI_IMAGE_TYPES
        ]
        parts.append({"text": prompt + _image_note(images, GEMINI_IMAGE_TYPES)})
        contents: list[dict] = [{"role": "user", "parts": parts}]
        declarations = [
            {"name": t.name, "description": t.description, "parameters": t.parameters}
            for t in tools.values()
        ]
        for round_ in range(config.CHAT_MAX_TOOL_ROUNDS + 1):
            payload: dict[str, Any] = {
                "system_instruction": {"parts": [{"text": system}]},
                "contents": contents,
            }
            if declarations:
                payload["tools"] = [{"functionDeclarations": declarations}]
                if round_ == config.CHAT_MAX_TOOL_ROUNDS:  # out of rounds: answer now
                    payload["toolConfig"] = {"functionCallingConfig": {"mode": "NONE"}}
            content = await self._gemini_request(payload)
            parts = content.get("parts", [])
            calls = [p["functionCall"] for p in parts if "functionCall" in p]
            if not calls:
                text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                if not text.strip():
                    raise RuntimeError("empty response")
                return text

            # Echo the model turn back verbatim: it carries thought signatures
            # that Gemini requires to continue a function-calling conversation.
            contents.append(content)
            responses = []
            for call in calls:
                result = await _run_tool(tools, call["name"], call.get("args") or {})
                response = {"name": call["name"], "response": {"result": result}}
                if "id" in call:
                    response["id"] = call["id"]
                responses.append({"functionResponse": response})
            contents.append({"role": "user", "parts": responses})
        raise RuntimeError("no answer after tool rounds")

    # --- Cohere ---------------------------------------------------------------

    async def _cohere_request(self, **kwargs) -> Any:
        last_error: Exception | None = None
        for client in self.cohere_clients.rotation():
            try:
                return (await client.chat(model=config.COHERE_MODEL, **kwargs)).message
            except Exception as e:  # noqa: BLE001 — rotate to the next key on any API failure
                last_error = e
                log.warning("Cohere request failed, rotating key: %s", e)
        raise RuntimeError(f"all Cohere keys failed: {last_error}")

    async def _run_cohere(self, system: str, prompt: str, tools: dict[str, Tool], images: list[Image]) -> str:
        messages: list[dict] = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt + _image_note(images, None)},
        ]
        specs = [
            {
                "type": "function",
                "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
            }
            for t in tools.values()
        ]
        for round_ in range(config.CHAT_MAX_TOOL_ROUNDS + 1):
            kwargs: dict[str, Any] = {"messages": messages}
            if specs:
                kwargs["tools"] = specs
                if round_ == config.CHAT_MAX_TOOL_ROUNDS:  # out of rounds: answer now
                    kwargs["tool_choice"] = "NONE"
            msg = await self._cohere_request(**kwargs)
            if not msg.tool_calls:
                if msg.content:
                    return "".join(c.text for c in msg.content if getattr(c, "type", "text") == "text")
                raise RuntimeError("empty response")

            messages.append({
                "role": "assistant",
                **({"tool_plan": msg.tool_plan} if msg.tool_plan else {}),
                "tool_calls": [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.function.name, "arguments": c.function.arguments},
                    }
                    for c in msg.tool_calls
                ],
            })
            for call in msg.tool_calls:
                args = json.loads(call.function.arguments or "{}")
                result = await _run_tool(tools, call.function.name, args)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps({"result": result})})
        raise RuntimeError("no answer after tool rounds")

    # --- Claude (owner only, via the local claude-api server) ---------------------

    async def _claude_request(self, body: dict) -> dict:
        headers = {"anthropic-version": "2023-06-01"}
        if config.CLAUDE_API_KEY:
            headers["x-api-key"] = config.CLAUDE_API_KEY
        # Each round runs the `claude` CLI, which can take a while (and longer
        # with web search), so allow more than the shared session's default.
        timeout = aiohttp.ClientTimeout(total=config.CLAUDE_TIMEOUT)
        url = config.CLAUDE_API_URL.rstrip("/") + "/v1/messages"
        async with self.http.post(url, headers=headers, json=body, timeout=timeout) as resp:
            data = await resp.json()
            if resp.status != 200:
                raise RuntimeError(data.get("error", {}).get("message", f"HTTP {resp.status}"))
            return data

    async def _run_claude(self, system: str, prompt: str, tools: dict[str, Tool], images: list[Image]) -> str:
        content: list[dict] = [
            {"type": "image", "source": {"type": "base64", "media_type": i.mime, "data": i.b64()}}
            for i in images
            if i.mime in CLAUDE_IMAGE_TYPES
        ]
        content.append({"type": "text", "text": prompt + _image_note(images, CLAUDE_IMAGE_TYPES)})
        messages: list[dict] = [{"role": "user", "content": content}]
        specs = [
            {"name": t.name, "description": t.description, "input_schema": t.parameters}
            for t in tools.values()
        ]
        for round_ in range(config.CHAT_MAX_TOOL_ROUNDS + 1):
            body: dict[str, Any] = {"max_tokens": 4096, "system": system, "messages": messages}
            if config.CLAUDE_MODEL:
                body["model"] = config.CLAUDE_MODEL
            if specs:
                body["tools"] = specs
                if round_ == config.CHAT_MAX_TOOL_ROUNDS:  # out of rounds: answer now
                    body["tool_choice"] = {"type": "none"}
            blocks = (await self._claude_request(body))["content"]
            calls = [b for b in blocks if b["type"] == "tool_use"]
            if not calls:
                text = "".join(b.get("text", "") for b in blocks if b["type"] == "text")
                if not text.strip():
                    raise RuntimeError("empty response")
                return text

            messages.append({"role": "assistant", "content": blocks})
            results = []
            for call in calls:
                result = await _run_tool(tools, call["name"], call.get("input") or {})
                results.append({"type": "tool_result", "tool_use_id": call["id"], "content": result})
            messages.append({"role": "user", "content": results})
        raise RuntimeError("no answer after tool rounds")
