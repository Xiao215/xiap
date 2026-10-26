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


# Chat providers, shared by everyone. "auto" tries Claude, Gemini, then Cohere;
# set CHAT_PROVIDER=claude, =gemini or =cohere to make one primary.
CHAT_PROVIDER = os.getenv("CHAT_PROVIDER", "auto").lower()

COHERE_API_KEYS = _load_keys("COHERE")
COHERE_MODEL = os.getenv("COHERE_MODEL", "command-a-plus-05-2026")

# Claude and Gemini via the configured cli-bridge server
# (https://github.com/Xiao215/cli-bridge), which runs the `claude` CLI and, for
# gemini-* models, the Antigravity CLI (`agy`). All users can select them.
# Empty URL = off; failures fall back to other providers.
CLI_BRIDGE_URL = os.getenv("CLI_BRIDGE_URL", "http://127.0.0.1:8787")
CLI_BRIDGE_KEY = os.getenv("CLI_BRIDGE_KEY", "")  # only if cli-bridge has API_KEY set
CLI_BRIDGE_TIMEOUT = int(os.getenv("CLI_BRIDGE_TIMEOUT", "300"))  # per request; the CLIs can be slow
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "")  # haiku | sonnet | opus; empty = server default
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "")  # e.g. gemini-3.8-flash-high; empty = agy picks

# Seconds a Cohere model gets to answer (all tool rounds included)
# before the bot gives up on it and fails over to the next one.
CHAT_TIMEOUT = int(os.getenv("CHAT_TIMEOUT", "90"))

# Images on the pinging message, the one it replies to, then the last
# CHAT_BASE_CONTEXT messages (attached or embedded from a link, like Tenor GIFs)
# are shown to the model, up to this many, each at most CHAT_IMAGE_MAX_MB.
CHAT_MAX_IMAGES = int(os.getenv("CHAT_MAX_IMAGES", "4"))
CHAT_IMAGE_MAX_MB = float(os.getenv("CHAT_IMAGE_MAX_MB", "5"))
# PDFs go to a model that can read them (Claude or Gemini; Cohere can't, so
# it's skipped while one can answer). Up to CHAT_MAX_PDFS, each at most
# CHAT_PDF_MAX_MB. All images and PDFs in one ping together stay under ~14 MB,
# since the cli-bridge server caps a request at 20 MB once base64'd.
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

# self.mp3's doorman, which serves its share links' songs for /play
# (https://github.com/Xiao215/selfmp3, docs/features/share-links.md).
SELFMP3_DOORMAN_URL = os.getenv(
    "SELFMP3_DOORMAN_URL", "https://selfmp3-doorman.xiaozhang20030215.workers.dev"
).rstrip("/")

# Port for the tiny HTTP health-check server (Cloud Run & friends).
PORT = int(os.getenv("PORT", "8080"))

# Where /context, /optout, /model and /papers settings are saved.
DATA_FILE = os.getenv("DATA_FILE", "data.json")
