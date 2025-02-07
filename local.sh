docker build -t discord-bot .
docker run --env-file .env -p 8080:8080 discord-bot