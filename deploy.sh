gcloud auth configure-docker

docker build -t gcr.io/xiap-450116/discord-bot .
docker push gcr.io/xiap-450116/discord-bot

#!/bin/bash

# Load environment variables from .env and format them for gcloud
ENV_VARS=$(grep -v '^#' .env | xargs | sed "s/ /,/g")

# Deploy to Cloud Run with all environment variables
gcloud run deploy discord-bot \
  --image gcr.io/xiap-450116/discord-bot \
  --platform managed \
  --region us-central1 \
  --allow-unauthenticated \
  --set-env-vars "$ENV_VARS"\
  --port 8080