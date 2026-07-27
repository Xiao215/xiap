"""Central configuration loaded from environment variables / .env file."""

import os

from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# Cohere keys: either COHERE_API_KEYS="key1,key2" or COHERE_API_KEY_1, COHERE_API_KEY_2, ...
def _load_cohere_keys() -> list[str]:
    keys = [k.strip() for k in os.getenv("COHERE_API_KEYS", "").split(",") if k.strip()]
    i = 1
    while key := os.getenv(f"COHERE_API_KEY_{i}"):
        keys.append(key)
        i += 1
    return keys


COHERE_API_KEYS = _load_cohere_keys()
COHERE_MODEL = os.getenv("COHERE_MODEL", "command-a-03-2025")

# How many recent channel messages to feed the model as context.
CHAT_HISTORY_LIMIT = int(os.getenv("CHAT_HISTORY_LIMIT", "30"))

# Port for the tiny HTTP health-check server (Cloud Run & friends).
PORT = int(os.getenv("PORT", "8080"))
