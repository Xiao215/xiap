# xiap 🤖

A friendly AI Discord bot for the Sunday Social group chat. Mention it (or reply to it) and it chats back using Cohere, complete with your server's custom emojis — plus a bag of party tricks for the group.

## Features

**AI chat** — @mention the bot or reply to one of its messages and it responds in character, using the last 30 messages of the channel as context and your server's custom emojis. Powered by Gemini Flash (free tier) and/or Cohere — configure either or both; keys round-robin and providers fail over automatically.

**Slash commands**

| Command | What it does |
|---|---|
| `/stock [symbol] [period]` | Price chart image for any ticker (default: SPY / S&P 500), data from Yahoo Finance |
| `/poll` | Button-based poll (up to 5 options) with a live results bar |
| `/remind 1h30m <text>` | Pings you in the channel when time's up |
| `/choose a, b, c` | Can't decide? The bot picks |
| `/context 0-30` | Per-channel cap on how many recent messages the AI may read (0 = none) |
| `/optout`, `/optin` | Per-user: exclude your messages from AI context everywhere |
| `/help` | Feature overview card, generated from the live command list |

## Setup

1. Create a bot at the [Discord Developer Portal](https://discord.com/developers/applications). Under **Bot**, enable the **Message Content Intent** and copy the token.
2. Get a free Gemini API key from [Google AI Studio](https://aistudio.google.com/apikey) and/or Cohere keys from the [Cohere dashboard](https://dashboard.cohere.com/api-keys).
3. Configure and run:

```bash
cp .env.example .env   # fill in DISCORD_TOKEN and COHERE_API_KEYS
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
  web.py               aiohttp health-check server
  cogs/
    chat.py            Cohere AI chat (mentions & replies)
    fun.py             rps, roll, 8ball, coinflip, choose, ship
    polls.py           button polls
    reminders.py       /remind
    newsletter.py      Firestore subscribe/unsubscribe (optional)
```
