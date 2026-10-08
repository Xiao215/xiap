#!/bin/bash
# Build, push and deploy the bot to Cloud Run, passing .env as environment variables.
set -euo pipefail

IMAGE=gcr.io/xiap-450116/discord-bot

gcloud auth configure-docker
docker build -t "$IMAGE" .
docker push "$IMAGE"

ENV_VARS=$(grep -v '^#' .env | xargs | sed "s/ /,/g")

gcloud run deploy discord-bot \
  --image "$IMAGE" \
  --platform managed \
  --region us-central1 \
  --allow-unauthenticated \
  --set-env-vars "$ENV_VARS" \
  --port 8080
