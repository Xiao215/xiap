"""Tool-calling agent loop over Gemini, Cohere and (owner only) Claude.

The model gets a system prompt, the user's message (plus any images and PDFs)
and a set of tools. Each round it either answers or asks for tools; we run them, hand back the results
and repeat until it answers (or runs out of rounds, at which point tools are
switched off so it has to answer with what it has).

Gemini and Cohere round-robin across their configured API keys. Claude goes
through the owner's local claude-api server (Anthropic Messages format) and is
only ever used for the owner's own messages — a Pro/Max plan is for personal
use. Cohere can't read PDFs, so when one is attached the providers that can
go first. If a provider fails outright, or takes longer than its deadline, the whole
run restarts on the next. Tools are read-only, so re-running them on failover
is harmless.
"""

import asyncio
import base64
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import aiohttp
import cohere

from xiap import config

log = logging.getLogger(__name__)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000"
# The Gemini catalogue also lists TTS, image, music, robotics… models; keep the
# chat ones: gemini-<version>-pro/flash/flash-lite, optionally -preview, and the -latest aliases.
GEMINI_CHAT_RE = re.compile(r"^gemini-(\d[\d.]*-)?(pro|flash|flash-lite)(-preview)?(-latest)?$")
MODEL_LIST_TTL = 3600  # seconds to cache a provider's model list
# A failed listing (e.g. the claude-api server isn't up yet) is retried this
# soon instead of hiding that provider's models for the full hour.
MODEL_LIST_RETRY = 60

# Tool results are truncated to this many characters so one call can't blow up the prompt.
MAX_TOOL_RESULT_CHARS = 6000


def _describe(e: BaseException) -> str:
    """Error text for logs; timeouts and some network errors have an empty str()."""
    return str(e) or type(e).__name__


PDF = "application/pdf"


@dataclass
class Attachment:
    """An image or PDF shown to the model as-is."""

    mime: str  # e.g. "image/png" or "application/pdf"
    data: bytes
    filename: str = ""

    def b64(self) -> str:
        return base64.b64encode(self.data).decode()


GEMINI_TYPES = {"image/png", "image/jpeg", "image/webp", "image/heic", "image/heif", PDF}
CLAUDE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", PDF}
# Providers that read PDFs (every Gemini chat model does, and Claude does).
PDF_PROVIDERS = {"gemini", "claude"}


def reads_pdfs(label: str) -> bool:
    """Whether a "provider:model" label (or bare provider) can read PDFs."""
    return label.partition(":")[0] in PDF_PROVIDERS


def _attachment_note(attachments: list[Attachment], supported: set[str]) -> str:
    """Tell the model about attached files it won't be able to see."""
    skipped = [a for a in attachments if a.mime not in supported]
    pdfs = sum(a.mime == PDF for a in skipped)
    images = len(skipped) - pdfs
    if not skipped:
        return ""
    what = " and ".join(f"{n} {kind}" for n, kind in ((images, "image(s)"), (pdfs, "PDF(s)")) if n)
    return (
        f"\n\n({what} attached couldn't be shown to you — don't pretend to see them. If they matter "
        "to the question, say you can't read them.)"
    )


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
            KeyRing([cohere.AsyncClientV2(api_key=k, timeout=60) for k in config.COHERE_API_KEYS])
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
        self._runners = {"gemini": self._run_gemini, "cohere": self._run_cohere, "claude": self._run_claude}
        # provider -> (monotonic time it expires, models)
        self._model_lists: dict[str, tuple[float, list[str]]] = {}

    def configured(self, provider: str) -> bool:
        return bool({"gemini": self.gemini_keys, "cohere": self.cohere_clients, "claude": self.claude}.get(provider))

    @staticmethod
    def default_model(provider: str) -> str:
        return {"gemini": config.GEMINI_MODEL, "cohere": config.COHERE_MODEL, "claude": config.CLAUDE_MODEL}[provider]

    def chain(self, owner: bool = False, pick: str | None = None, pdf: bool = False) -> list[tuple[str, str]]:
        """(provider, model) pairs to try in order: the user's pick first (if
        any), then Claude for the owner, then the default providers. With a PDF
        attached, the ones that can read it move to the front; the rest stay as
        a last resort."""
        chain = []
        if pick:
            provider, _, model = pick.partition(":")
            if self.configured(provider) and (provider != "claude" or owner):
                chain.append((provider, model))
        if owner and self.claude:
            chain.append(("claude", config.CLAUDE_MODEL))
        chain += [(name, self.default_model(name)) for name, _ in self.providers]
        chain = list(dict.fromkeys(chain))  # dedupe, keep order
        if pdf:  # stable sort: PDF readers first, order otherwise kept
            chain.sort(key=lambda pm: pm[0] not in PDF_PROVIDERS)
        return chain

    async def list_models(self) -> dict[str, list[str]]:
        """Chat models each configured provider offers, each cached for an hour
        (a failed listing only for a minute, so it's retried soon).
        Includes Claude when configured — callers must only show it to the owner."""
        # Claude first: it's the owner's default, and in /model's autocomplete
        # it would otherwise sit below a dozen Gemini models, out of view.
        fetchers = {"claude": self._list_claude, "gemini": self._list_gemini, "cohere": self._list_cohere}
        providers = [p for p in fetchers if self.configured(p)]
        now = time.monotonic()
        stale = [p for p in providers if self._model_lists.get(p, (0.0, []))[0] <= now]
        # Fetch in parallel: /model autocomplete must answer Discord within 3s.
        results = await asyncio.gather(*(fetchers[p]() for p in stale), return_exceptions=True)
        for provider, result in zip(stale, results):
            if isinstance(result, BaseException):  # a provider being down shouldn't hide the others
                log.warning("Couldn't list %s models: %s", provider, _describe(result))
                fallback = [m for m in [self.default_model(provider)] if m]
                self._model_lists[provider] = (now + MODEL_LIST_RETRY, fallback)
            else:
                self._model_lists[provider] = (now + MODEL_LIST_TTL, result)
        return {p: self._model_lists[p][1] for p in providers}

    async def _list_gemini(self) -> list[str]:
        headers = {"x-goog-api-key": self.gemini_keys.items[0]}
        async with self.http.get(GEMINI_MODELS_URL, headers=headers) as resp:
            data = await resp.json()
            if resp.status != 200:
                raise RuntimeError(data.get("error", {}).get("message", f"HTTP {resp.status}"))
        names = [m["name"].removeprefix("models/") for m in data.get("models", [])]
        names = [n for n in names if GEMINI_CHAT_RE.match(n)]
        if not config.GEMINI_LIST_PRO:
            names = [n for n in names if "-pro" not in n]
        return sorted(names, reverse=True)

    async def _list_cohere(self) -> list[str]:
        res = await self.cohere_clients.items[0].models.list(endpoint="chat", page_size=100)
        return sorted((m.name for m in res.models or [] if m.name), reverse=True)

    async def _list_claude(self) -> list[str]:
        url = config.CLAUDE_API_URL.rstrip("/") + "/v1/models"
        headers = {"x-api-key": config.CLAUDE_API_KEY} if config.CLAUDE_API_KEY else {}
        async with self.http.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=2)) as resp:
            data = await resp.json()
            if resp.status != 200:
                raise RuntimeError(data.get("error", {}).get("message", f"HTTP {resp.status}"))
        return [m["id"] for m in data.get("data", [])]

    async def run(
        self,
        system: str,
        prompt: str,
        tools: list[Tool],
        attachments: list[Attachment] = (),
        owner: bool = False,
        pick: str | None = None,
    ) -> tuple[str, str, str] | None:
        """Run the agent loop; returns (answer, "provider:model" from the chain,
        the model id that actually answered) or None if every provider failed.
        `pick` is the user's chosen "provider:model"."""
        by_name = {t.name: t for t in tools}
        pdf = any(a.mime == PDF for a in attachments)
        for provider, model in self.chain(owner, pick, pdf):
            label = f"{provider}:{model}" if model else provider
            # A slow model (thinking, preview, a stuck connection) shouldn't
            # leave the chat staring at "typing…" — give up and fail over.
            deadline = config.CLAUDE_TIMEOUT if provider == "claude" else config.CHAT_TIMEOUT
            started = time.monotonic()
            try:
                async with asyncio.timeout(deadline):
                    answer, used = await self._runners[provider](system, prompt, by_name, list(attachments), model)
                log.info("Answered by %s (%s) in %.1fs", label, used, time.monotonic() - started)
                return answer, label, used
            except Exception as e:  # noqa: BLE001 — fall through to the next provider
                log.error("%s failed after %.1fs: %s", label, time.monotonic() - started, _describe(e))
        return None

    # --- Gemini ---------------------------------------------------------------

    async def _gemini_request(self, payload: dict, model: str) -> dict:
        url = GEMINI_URL.format(model=model)
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
                log.warning("Gemini request failed, rotating key: %s", _describe(e))
        raise RuntimeError(f"all Gemini keys failed: {last_error}")

    async def _run_gemini(
        self, system: str, prompt: str, tools: dict[str, Tool], attachments: list[Attachment], model: str
    ) -> tuple[str, str]:
        parts = [
            {"inline_data": {"mime_type": a.mime, "data": a.b64()}}
            for a in attachments
            if a.mime in GEMINI_TYPES
        ]
        parts.append({"text": prompt + _attachment_note(attachments, GEMINI_TYPES)})
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
            content = await self._gemini_request(payload, model)
            parts = content.get("parts", [])
            calls = [p["functionCall"] for p in parts if "functionCall" in p]
            if not calls:
                text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                if not text.strip():
                    raise RuntimeError("empty response")
                return text, model

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

    async def _cohere_request(self, model: str, **kwargs) -> Any:
        last_error: Exception | None = None
        for client in self.cohere_clients.rotation():
            try:
                return (await client.chat(model=model, **kwargs)).message
            except Exception as e:  # noqa: BLE001 — rotate to the next key on any API failure
                last_error = e
                log.warning("Cohere request failed, rotating key: %s", _describe(e))
        raise RuntimeError(f"all Cohere keys failed: {last_error}")

    async def _run_cohere(
        self, system: str, prompt: str, tools: dict[str, Tool], attachments: list[Attachment], model: str
    ) -> tuple[str, str]:
        messages: list[dict] = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt + _attachment_note(attachments, set())},
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
            msg = await self._cohere_request(model, **kwargs)
            if not msg.tool_calls:
                # Reasoning models also return "thinking" items; only text is the answer.
                text = "".join(c.text for c in msg.content or [] if getattr(c, "type", "text") == "text")
                if not text.strip():
                    raise RuntimeError("empty response")
                return text, model

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

    async def _run_claude(
        self, system: str, prompt: str, tools: dict[str, Tool], attachments: list[Attachment], model: str
    ) -> tuple[str, str]:
        content: list[dict] = [
            {
                "type": "document" if a.mime == PDF else "image",
                "source": {"type": "base64", "media_type": a.mime, "data": a.b64()},
                **({"title": a.filename} if a.mime == PDF and a.filename else {}),
            }
            for a in attachments
            if a.mime in CLAUDE_TYPES
        ]
        content.append({"type": "text", "text": prompt + _attachment_note(attachments, CLAUDE_TYPES)})
        messages: list[dict] = [{"role": "user", "content": content}]
        specs = [
            {"name": t.name, "description": t.description, "input_schema": t.parameters}
            for t in tools.values()
        ]
        for round_ in range(config.CHAT_MAX_TOOL_ROUNDS + 1):
            body: dict[str, Any] = {"max_tokens": 4096, "system": system, "messages": messages}
            if model:
                body["model"] = model
            if specs:
                body["tools"] = specs
                if round_ == config.CHAT_MAX_TOOL_ROUNDS:  # out of rounds: answer now
                    body["tool_choice"] = {"type": "none"}
            data = await self._claude_request(body)
            blocks = data["content"]
            calls = [b for b in blocks if b["type"] == "tool_use"]
            if not calls:
                text = "".join(b.get("text", "") for b in blocks if b["type"] == "text")
                if not text.strip():
                    raise RuntimeError("empty response")
                # The server reports the full id ("claude-opus-5-5") even when
                # we asked for an alias like "opus" or the server default.
                return text, data.get("model") or model

            messages.append({"role": "assistant", "content": blocks})
            results = []
            for call in calls:
                result = await _run_tool(tools, call["name"], call.get("input") or {})
                results.append({"type": "tool_result", "tool_use_id": call["id"], "content": result})
            messages.append({"role": "user", "content": results})
        raise RuntimeError("no answer after tool rounds")
