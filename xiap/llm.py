"""Tool-calling agent loop over Claude, Gemini and Cohere.

The model gets a system prompt, the user's message (plus any images and PDFs)
and a set of tools. Each round it either answers or asks for tools; we run
them, hand back the results and repeat until it answers (or runs out of
rounds, at which point tools are switched off so it has to answer with what it
has).

Claude and Gemini both go through the configured cli-bridge server (Anthropic
Messages format), which runs them on the `claude` and `agy` CLIs. Cohere
round-robins across its configured API keys. All configured providers are
available to everyone. Cohere can't read PDFs, so
when one is attached, providers that can read it go first. If a provider fails
outright, or takes longer than its deadline, the whole run restarts on the
next. Tools are read-only, so re-running them on failover is harmless.
"""

import asyncio
import base64
import functools
import json
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

import aiohttp
import cohere

from xiap import config

log = logging.getLogger(__name__)

MODEL_LIST_TTL = 3600  # seconds to cache a provider's model list
# A failed listing (e.g. the cli-bridge server isn't up yet) is retried this
# soon instead of hiding that provider's models for the full hour.
MODEL_LIST_RETRY = 60
# Leave room to send Discord's autocomplete response within its 3s deadline.
MODEL_LIST_TIMEOUT = 2

# Tool results are truncated to this many characters so one call can't blow up the prompt.
MAX_TOOL_RESULT_CHARS = 6000

PDF = "application/pdf"
BRIDGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", PDF}
# Providers served by cli-bridge; they all read images and PDFs.
BRIDGE_PROVIDERS = {"claude", "gemini"}
PDF_PROVIDERS = BRIDGE_PROVIDERS


@dataclass
class Attachment:
    """An image or PDF shown to the model as-is."""

    mime: str  # e.g. "image/png" or "application/pdf"
    data: bytes
    filename: str = ""

    def b64(self) -> str:
        return base64.b64encode(self.data).decode()


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict  # JSON schema for the arguments object
    run: Callable[..., Awaitable[str]]


def reads_pdfs(label: str) -> bool:
    """Whether a "provider:model" label (or bare provider) can read PDFs."""
    return label.partition(":")[0] in PDF_PROVIDERS


def _describe(e: BaseException) -> str:
    """Error text for logs; timeouts and some network errors have an empty str()."""
    return str(e) or type(e).__name__


async def _read_json(resp: aiohttp.ClientResponse) -> dict:
    """The JSON body of a successful response; raises with the API's error message otherwise."""
    data = await resp.json()
    if resp.status != 200:
        raise RuntimeError(data.get("error", {}).get("message", f"HTTP {resp.status}"))
    return data


def _bridge_url(endpoint: str) -> str:
    return f"{config.CLI_BRIDGE_URL.rstrip('/')}/v1/{endpoint}"


def _bridge_headers() -> dict[str, str]:
    headers = {"anthropic-version": "2023-06-01"}
    if config.CLI_BRIDGE_KEY:
        headers["x-api-key"] = config.CLI_BRIDGE_KEY
    return headers


def _attachment_note(attachments: list[Attachment], supported: set[str]) -> str:
    """Tell the model about attached files it won't be able to see."""
    skipped = [a for a in attachments if a.mime not in supported]
    if not skipped:
        return ""
    pdfs = sum(a.mime == PDF for a in skipped)
    images = len(skipped) - pdfs
    what = " and ".join(f"{n} {kind}" for n, kind in ((images, "image(s)"), (pdfs, "PDF(s)")) if n)
    return (
        f"\n\n({what} attached couldn't be shown to you — don't pretend to see them. If they matter "
        "to the question, say you can't read them.)"
    )


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
        self.cohere_clients = (
            KeyRing([cohere.AsyncClientV2(api_key=k, timeout=60) for k in config.COHERE_API_KEYS])
            if config.COHERE_API_KEYS
            else None
        )

        self.bridge_enabled = bool(config.CLI_BRIDGE_URL)

        # Shared default order; a person's /model pick takes priority.
        available = [p for p in ("claude", "gemini", "cohere") if self.configured(p)]  # "auto" prefers Claude
        if config.CHAT_PROVIDER in available:
            available.remove(config.CHAT_PROVIDER)
            available.insert(0, config.CHAT_PROVIDER)
        self.providers: list[str] = available
        log.info("Chat providers (in order): %s", self.providers or "NONE")
        self._runners = {
            "claude": self._run_bridge,
            # cli-bridge sends any "gemini*" name to agy; plain "gemini" lets agy pick.
            "gemini": functools.partial(self._run_bridge, default="gemini"),
            "cohere": self._run_cohere,
        }
        # provider -> (monotonic time it expires, models)
        self._model_lists: dict[str, tuple[float, list[str]]] = {}

    def configured(self, provider: str) -> bool:
        return bool(
            {"claude": self.bridge_enabled, "gemini": self.bridge_enabled, "cohere": self.cohere_clients}.get(provider)
        )

    @staticmethod
    def default_model(provider: str) -> str:
        return {"claude": config.CLAUDE_MODEL, "gemini": config.GEMINI_MODEL, "cohere": config.COHERE_MODEL}[provider]

    def chain(self, pick: str | None = None, pdf: bool = False) -> list[tuple[str, str]]:
        """(provider, model) pairs to try in order: the user's pick first (if
        any), then the default providers. With a PDF attached, the ones that
        can read it move to the front; the rest stay as a last resort."""
        chain = []
        if pick:
            provider, _, model = pick.partition(":")
            if self.configured(provider):
                chain.append((provider, model))
        chain += [(name, self.default_model(name)) for name in self.providers]
        chain = list(dict.fromkeys(chain))  # dedupe, keep order
        if pdf:  # stable sort: PDF readers first, order otherwise kept
            chain.sort(key=lambda pm: pm[0] not in PDF_PROVIDERS)
        return chain

    async def list_models(self) -> dict[str, list[str]]:
        """Chat models each configured provider offers, each cached for an hour
        (a failed listing only for a minute, so it's retried soon).
        Keep the last known list during outages; all users see the same models."""
        # Claude first so it isn't buried beneath a long Gemini catalogue.
        fetchers = {"claude": self._list_claude, "gemini": self._list_gemini, "cohere": self._list_cohere}
        providers = [p for p in fetchers if self.configured(p)]
        now = time.monotonic()
        stale = [p for p in providers if self._model_lists.get(p, (0.0, []))[0] <= now]
        # Fetch in parallel: /model autocomplete must answer Discord within 3s.
        results = await asyncio.gather(
            *(asyncio.wait_for(fetchers[p](), timeout=MODEL_LIST_TIMEOUT) for p in stale),
            return_exceptions=True,
        )
        for provider, result in zip(stale, results, strict=True):
            if isinstance(result, BaseException) or not result:
                reason = _describe(result) if isinstance(result, BaseException) else "empty model list"
                log.warning("Couldn't list %s models: %s", provider, reason)
                fallback = self._model_lists.get(provider, (0.0, []))[1]
                if not fallback:
                    fallback = [m for m in [self.default_model(provider)] if m]
                    if provider == "claude":
                        # cli-bridge accepts these aliases even before discovery succeeds.
                        fallback = list(dict.fromkeys(fallback + ["sonnet", "opus", "haiku"]))
                self._model_lists[provider] = (now + MODEL_LIST_RETRY, fallback)
            else:
                self._model_lists[provider] = (now + MODEL_LIST_TTL, result)
        return {p: self._model_lists[p][1] for p in providers}

    async def _list_cohere(self) -> list[str]:
        res = await self.cohere_clients.items[0].models.list(endpoint="chat", page_size=100)
        return sorted((m.name for m in res.models or [] if m.name), reverse=True)

    async def _list_bridge(self) -> list[str]:
        async with self.http.get(_bridge_url("models"), headers=_bridge_headers()) as resp:
            data = await _read_json(resp)
        return [m["id"] for m in data.get("data", [])]

    async def _list_claude(self) -> list[str]:
        return [m for m in await self._list_bridge() if not m.startswith("gemini")]

    async def _list_gemini(self) -> list[str]:
        # Only there when agy (the Antigravity CLI) is set up next to cli-bridge.
        return [m for m in await self._list_bridge() if m.startswith("gemini")]

    async def run(
        self,
        system: str,
        prompt: str,
        tools: list[Tool],
        attachments: Sequence[Attachment] = (),
        pick: str | None = None,
    ) -> tuple[str, str, str] | None:
        """Run the agent loop; returns (answer, "provider:model" from the chain,
        the model id that actually answered) or None if every provider failed.
        `pick` is the user's chosen "provider:model"."""
        by_name = {t.name: t for t in tools}
        pdf = any(a.mime == PDF for a in attachments)
        for provider, model in self.chain(pick=pick, pdf=pdf):
            label = f"{provider}:{model}" if model else provider
            # A slow model (thinking, preview, a stuck connection) shouldn't
            # leave the chat staring at "typing…" — give up and fail over.
            deadline = config.CLI_BRIDGE_TIMEOUT if provider in BRIDGE_PROVIDERS else config.CHAT_TIMEOUT
            started = time.monotonic()
            try:
                async with asyncio.timeout(deadline):
                    answer, used = await self._runners[provider](system, prompt, by_name, list(attachments), model)
                log.info("Answered by %s (%s) in %.1fs", label, used, time.monotonic() - started)
                return answer, label, used
            except Exception as e:  # noqa: BLE001 — fall through to the next provider
                log.error("%s failed after %.1fs: %s", label, time.monotonic() - started, _describe(e))
        return None

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

    # --- Claude and Gemini (via the configured cli-bridge server) -------------

    async def _bridge_request(self, body: dict) -> dict:
        # Each round runs the `claude` or `agy` CLI, which can take a while (and
        # longer with web search), so allow more than the shared session's default.
        timeout = aiohttp.ClientTimeout(total=config.CLI_BRIDGE_TIMEOUT)
        async with self.http.post(
            _bridge_url("messages"), headers=_bridge_headers(), json=body, timeout=timeout
        ) as resp:
            return await _read_json(resp)

    async def _run_bridge(
        self,
        system: str,
        prompt: str,
        tools: dict[str, Tool],
        attachments: list[Attachment],
        model: str,
        default: str = "",
    ) -> tuple[str, str]:
        """`default` is sent when no model is chosen; empty = the server's default."""
        model = model or default
        content: list[dict] = [
            {
                "type": "document" if a.mime == PDF else "image",
                "source": {"type": "base64", "media_type": a.mime, "data": a.b64()},
                **({"title": a.filename} if a.mime == PDF and a.filename else {}),
            }
            for a in attachments
            if a.mime in BRIDGE_TYPES
        ]
        content.append({"type": "text", "text": prompt + _attachment_note(attachments, BRIDGE_TYPES)})
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
            data = await self._bridge_request(body)
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
