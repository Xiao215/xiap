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
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

# How many recent channel messages to feed the model as context.
CHAT_HISTORY_LIMIT = int(os.getenv("CHAT_HISTORY_LIMIT", "30"))

# Daily papers digest: local hour to post at, and the timezone it's in.
PAPERS_HOUR = int(os.getenv("PAPERS_HOUR", "9"))
TIMEZONE = os.getenv("TIMEZONE", "America/Toronto")

# Port for the tiny HTTP health-check server (Cloud Run & friends).
PORT = int(os.getenv("PORT", "8080"))
