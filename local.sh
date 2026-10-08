#!/bin/bash
# Build and run the bot in Docker with the local .env.
set -euo pipefail

docker build -t discord-bot .
docker run --env-file .env -p 8080:8080 discord-bot
