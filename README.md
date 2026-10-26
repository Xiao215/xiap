# xiap 🤖

A friendly AI Discord bot for the Sunday Social group chat. Mention it (or reply to it) and it chats back using Claude, Gemini or Cohere, complete with your server's custom emojis — plus a bag of party tricks for the group.

## Features

**AI chat** — @mention the bot or reply to one of its messages and it responds in character with your server's custom emojis. It sees the last few messages up front and pulls more context itself when it needs it — older channel history, a linked message, or a long reply chain — within the limits set by `/context` and `/optout`. Attach a file (or reply to a message with one) and it reads it: images and PDFs directly, and text files (`.txt`, `.md`, `.csv`, code…) pasted into the prompt. It also sees images in the last few messages, including pasted image links and Tenor/Giphy GIFs (via Discord's embeds); animated GIFs are shown as a grid of frames. Cohere can't read PDFs, so a PDF goes to a model that can, even if you picked a Cohere model with `/model`; the tag under the reply says when that happened. Powered by Claude and Gemini through [cli-bridge](https://github.com/Xiao215/cli-bridge) and/or Cohere — configure either or both; Cohere keys round-robin and providers fail over automatically.

**Claude and Gemini for everyone** — every user can see and select the Claude and Gemini models the configured [cli-bridge](https://github.com/Xiao215/cli-bridge) server offers (Gemini needs the Antigravity CLI set up next to it), with the same context tools and image/PDF support. All users share the same provider access; model picks remain personal. `CHAT_PROVIDER=auto` defaults to Claude when configured, then falls back to Gemini and Cohere. A personal model pick takes priority over the default. If a provider is down or rate-limited, the bot tries the next one. Temporary model-list failures keep the last known options (or Claude's `sonnet`/`opus`/`haiku` aliases on first startup) and retry after a minute. `OWNER_ID` is no longer used.

**Music from self.mp3** — `/play` a [self.mp3](https://github.com/Xiao215/selfmp3) share link and the bot joins your voice channel and plays it: one song, or every song of a shared tag or playlist, in order. A link shared from a line (`?t=0:42`) starts there. More `/play`s add to the queue. Each song gets a now-playing panel — its cover, a progress bar that moves every 15 seconds, what's up next — with **Pause/Resume**, **Skip**, **Stop** and **Leave** buttons for whoever's in the voice channel; when the song ends, the panel keeps how it ended (played, skipped by…) and a new one is posted. It leaves after two minutes with nothing queued, ten minutes paused, or a minute after everyone else leaves. Each song counts as a play on the sender's link, so it keeps the link alive like any listener would.

**Slash commands**

| Command | What it does |
|---|---|
| `/stock [symbol] [period]` | Price chart image for any ticker (default: SPY / S&P 500), data from Yahoo Finance |
| `/papers today`, `/papers subscribe` | Trending AI papers from Hugging Face, on demand or as a daily digest in a channel |
| `/poll` | Button-based poll (up to 5 options) with a live results bar |
| `/remind 1h30m <text>` | Pings you in the channel when time's up |
| `/play <link>` | Join your voice channel and play a self.mp3 share link (more links queue up) |
| `/pause`, `/resume`, `/skip`, `/stop`, `/leave` | The panel's buttons as commands: pause, carry on, skip the song, stop and clear the queue, or leave (you have to be in the bot's channel) |
| `/queue` | What's playing and what's up next |
| `/choose a, b, c` | Can't decide? The bot picks |
| `/context 0-30` | Per-channel cap on how many recent messages the AI may read (0 = none) |
| `/models`, `/model` | See available AI models and pick one per person. All users have the same choices, including Claude. Type a name in `/model` to filter its 25 suggestions. Every AI reply has a gray tag underneath naming the model that wrote it |
| `/optout`, `/optin` | Per-user: exclude your messages from AI context everywhere |
| `/help` | Feature overview card, generated from the live command list |

## Setup

1. Create a bot at the [Discord Developer Portal](https://discord.com/developers/applications). Under **Bot**, enable the **Message Content Intent** and copy the token.
2. Run [cli-bridge](https://github.com/Xiao215/cli-bridge) for Claude (and Gemini, with the Antigravity CLI), and/or get Cohere keys from the [Cohere dashboard](https://dashboard.cohere.com/api-keys).
3. Configure and run:

```bash
cp .env.example .env   # fill in DISCORD_TOKEN, plus CLI_BRIDGE_URL and/or COHERE_API_KEYS
pip install -r requirements.txt   # /play also needs ffmpeg on PATH (brew/apt install ffmpeg)
python app.py
```

Or with Docker:

```bash
docker build -t xiap .
docker run --env-file .env -p 8080:8080 xiap
```

The bot serves a health-check endpoint on `:8080` for hosting platforms. If it starts before the network is up, it waits until it can look up Discord rather than exiting.

Invite URL: OAuth2 → URL Generator → scopes `bot` + `applications.commands`, permissions **Send Messages**, **Read Message History**, **Embed Links**, plus **Connect** and **Speak** for `/play`.

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
  llm.py               tool-calling agent loop over Claude/Gemini/Cohere
  discord_md.py        fixes model markdown for Discord (tables, rules), fence-safe splitting
  web.py               aiohttp health-check server
  cogs/
    chat.py            AI chat (mentions & replies) and its context tools
    models.py          /models, /model: per-user model choice
    music.py           /play and its now-playing panel, /pause, /resume, /skip, /stop, /leave, /queue
    privacy.py         /context, /optout, /optin
    papers.py          /papers: Hugging Face daily papers digest
    stocks.py          /stock price charts
    fun.py             /choose
    polls.py           button polls
    reminders.py       /remind
    help.py            /help
```
