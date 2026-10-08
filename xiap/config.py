"""Central configuration loaded from environment variables / .env file."""

import os

from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")


def _load_keys(prefix: str) -> list[str]:
    """Collect API keys: PREFIX_API_KEYS="k1,k2,..." and/or PREFIX_API_KEY_1, _2, ...

    Any number of keys works — the bot rotates through all of them.
    """
    keys = [k.strip() for k in os.getenv(f"{prefix}_API_KEYS", "").split(",") if k.strip()]
    if single := os.getenv(f"{prefix}_API_KEY"):
        keys.append(single)
    i = 1
    while key := os.getenv(f"{prefix}_API_KEY_{i}"):
        keys.append(key)
        i += 1
    return keys


# Chat providers. "auto" prefers Gemini (higher free-tier limits) and falls back
# to Cohere; set CHAT_PROVIDER=cohere or =gemini to force one as primary.
CHAT_PROVIDER = os.getenv("CHAT_PROVIDER", "auto").lower()

COHERE_API_KEYS = _load_keys("COHERE")
COHERE_MODEL = os.getenv("COHERE_MODEL", "command-a-plus-05-2026")

GEMINI_API_KEYS = _load_keys("GEMINI")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.7-flash")
# Gemini Pro models have no free-tier quota, so /models hides them unless your
# key has billing enabled.
GEMINI_LIST_PRO = os.getenv("GEMINI_LIST_PRO", "").lower() in ("1", "true", "yes")

# Owner-only Claude: the bot owner's messages go to the owner's local
# claude-api server (https://github.com/Xiao215/claude-api), which runs the
# `claude` CLI on their own Pro/Max subscription. Nobody else ever reaches it —
# the plan is for personal use. If the server isn't running (e.g. in Docker)
# the owner falls back to Gemini/Cohere like everyone else. Empty URL = off.
# The owner is OWNER_ID (a Discord user id) if set, else whoever owns the bot
# application in the Developer Portal.
OWNER_ID = int(os.getenv("OWNER_ID") or 0)
CLAUDE_API_URL = os.getenv("CLAUDE_API_URL", "http://127.0.0.1:8787")
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY", "")  # only if claude-api has API_KEY set
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "")  # haiku | sonnet | opus; empty = server default
CLAUDE_TIMEOUT = int(os.getenv("CLAUDE_TIMEOUT", "300"))  # per request; the CLI can be slow

# Seconds a Gemini/Cohere model gets to answer (all tool rounds included)
# before the bot gives up on it and fails over to the next one.
CHAT_TIMEOUT = int(os.getenv("CHAT_TIMEOUT", "90"))

# Images attached to the pinging message (or the one it replies to) are shown
# to the model, up to this many, each at most CHAT_IMAGE_MAX_MB.
CHAT_MAX_IMAGES = int(os.getenv("CHAT_MAX_IMAGES", "4"))
CHAT_IMAGE_MAX_MB = float(os.getenv("CHAT_IMAGE_MAX_MB", "5"))
# PDFs go to a model that can read them (Gemini or Claude; Cohere can't, so
# it's skipped while one can answer). Up to CHAT_MAX_PDFS, each at most
# CHAT_PDF_MAX_MB. All images and PDFs in one ping together stay under ~14 MB,
# since Gemini and the claude-api server cap a request at 20 MB once base64'd.
CHAT_MAX_PDFS = int(os.getenv("CHAT_MAX_PDFS", "2"))
CHAT_PDF_MAX_MB = float(os.getenv("CHAT_PDF_MAX_MB", "10"))
# Text files (.txt, .md, .csv, code…) are pasted into the prompt for every
# model: up to CHAT_MAX_TEXT_FILES, each cut off after CHAT_TEXT_MAX_CHARS.
CHAT_MAX_TEXT_FILES = int(os.getenv("CHAT_MAX_TEXT_FILES", "3"))
CHAT_TEXT_MAX_CHARS = int(os.getenv("CHAT_TEXT_MAX_CHARS", "20000"))

# Default cap on how far back the AI may read channel history (per-channel
# override via /context). It only sees CHAT_BASE_CONTEXT messages up front and
# pulls more with tools when it decides it needs them.
CHAT_HISTORY_LIMIT = int(os.getenv("CHAT_HISTORY_LIMIT", "30"))
CHAT_BASE_CONTEXT = int(os.getenv("CHAT_BASE_CONTEXT", "8"))
# Max rounds of tool calls per reply before the model must answer.
CHAT_MAX_TOOL_ROUNDS = int(os.getenv("CHAT_MAX_TOOL_ROUNDS", "4"))

# Daily papers digest: local hour to post at, and the timezone it's in.
PAPERS_HOUR = int(os.getenv("PAPERS_HOUR", "9"))
TIMEZONE = os.getenv("TIMEZONE", "America/Toronto")

# Port for the tiny HTTP health-check server (Cloud Run & friends).
PORT = int(os.getenv("PORT", "8080"))
