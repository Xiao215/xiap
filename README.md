# xiap 🤖

A friendly AI Discord bot for the Sunday Social group chat. Mention it (or reply to it) and it chats back using Gemini or Cohere, complete with your server's custom emojis — plus a bag of party tricks for the group.

## Features

**AI chat** — @mention the bot or reply to one of its messages and it responds in character with your server's custom emojis. It sees the last few messages up front and pulls more context itself when it needs it — older channel history, a linked message, or a long reply chain — within the limits set by `/context` and `/optout`. Attach a file (or reply to a message with one) and it reads it: images and PDFs directly, and text files (`.txt`, `.md`, `.csv`, code…) pasted into the prompt. It also sees images in the last few messages, including pasted image links and Tenor/Giphy GIFs (via Discord's embeds); animated GIFs are shown as a grid of frames. Cohere can't read PDFs, so a PDF goes to a model that can, even if you picked a Cohere model with `/model`; the tag under the reply says when that happened. Powered by Gemini Flash (free tier) and/or Cohere — configure either or both; keys round-robin and providers fail over automatically.

**Claude for everyone** — every user can see and select Claude models from the configured [cli-bridge](https://github.com/Xiao215/cli-bridge) server, with the same context tools and image/PDF support. All users share the same provider access; model picks remain personal. `CHAT_PROVIDER=auto` defaults to Claude when configured, then falls back to Gemini and Cohere. Gemini remains selectable through `/model`, and a personal model pick takes priority over the default. If a provider is down or rate-limited, the bot tries the next one. Temporary model-list failures keep the last known options (or Claude's `sonnet`/`opus`/`haiku` aliases on first startup) and retry after a minute. `OWNER_ID` is no longer used.

**Slash commands**

| Command | What it does |
|---|---|
| `/stock [symbol] [period]` | Price chart image for any ticker (default: SPY / S&P 500), data from Yahoo Finance |
| `/papers today`, `/papers subscribe` | Trending AI papers from Hugging Face, on demand or as a daily digest in a channel |
| `/poll` | Button-based poll (up to 5 options) with a live results bar |
| `/remind 1h30m <text>` | Pings you in the channel when time's up |
| `/choose a, b, c` | Can't decide? The bot picks |
| `/context 0-30` | Per-channel cap on how many recent messages the AI may read (0 = none) |
| `/models`, `/model` | See available AI models and pick one per person. All users have the same choices, including Claude. Type a name in `/model` to filter its 25 suggestions. Every AI reply has a gray tag underneath naming the model that wrote it |
| `/optout`, `/optin` | Per-user: exclude your messages from AI context everywhere |
| `/help` | Feature overview card, generated from the live command list |

## Setup

1. Create a bot at the [Discord Developer Portal](https://discord.com/developers/applications). Under **Bot**, enable the **Message Content Intent** and copy the token.
2. Get a free Gemini API key from [Google AI Studio](https://aistudio.google.com/apikey) and/or Cohere keys from the [Cohere dashboard](https://dashboard.cohere.com/api-keys).
3. Configure and run:

```bash
cp .env.example .env   # fill in DISCORD_TOKEN and GEMINI_API_KEYS and/or COHERE_API_KEYS
pip install -r requirements.txt
python app.py
```

Or with Docker:

```bash
docker build -t xiap .
docker run --env-file .env -p 8080:8080 xiap
```

The bot serves a health-check endpoint on `:8080` for hosting platforms.

Invite URL: OAuth2 → URL Generator → scopes `bot` + `applications.commands`, permissions **Send Messages**, **Read Message History**, **Embed Links**.

## Hosting

A Discord bot needs to run 24/7 (it holds a websocket open), so "scale-to-zero" platforms don't fit. Options that are actually free:

- **Google Cloud e2-micro VM** — always-free tier (1 e2-micro in `us-west1`/`us-central1`/`us-east1`, 30 GB disk). Install Docker, `docker run --restart unless-stopped --env-file .env xiap`, done.
- **Oracle Cloud Always Free** — up to 4 ARM OCPUs / 24 GB RAM, permanently free. Overkill for this bot in the best way.
- **Cloud Run** (current `deploy.sh`) works, but the websocket needs *CPU always allocated* + min-instances 1, which falls outside the free tier — a small VM is the better fit.

## Project layout

```
app.py                 entry point
xiap/
  bot.py               XiapBot, cog loading, slash-command sync
  config.py            env/config
  store.py             JSON-file settings store (data.json)
  llm.py               tool-calling agent loop over Gemini/Cohere/Claude
  discord_md.py        fixes model markdown for Discord (tables, rules), fence-safe splitting
  web.py               aiohttp health-check server
  cogs/
    chat.py            AI chat (mentions & replies) and its context tools
    models.py          /models, /model: per-user model choice
    privacy.py         /context, /optout, /optin
    papers.py          /papers: Hugging Face daily papers digest
    stocks.py          /stock price charts
    fun.py             /choose
    polls.py           button polls
    reminders.py       /remind
    help.py            /help
```
