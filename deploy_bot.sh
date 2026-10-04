cd /root/bots/Inviter_bot/ || exit 1

docker build -t inviter_bot .
docker rm -f inviter_bot 2>/dev/null || true
docker run -d --name inviter_bot --restart always \
  -v "$(pwd)/data":/app/data \
  -v "$(pwd)/sessions":/app/sessions \
  -v "$(pwd)/logs":/app/logs \
  -v "$(pwd)/.env":/app/.env \
  inviter_bot

echo "✅ Deployed! Logs: docker logs -f  inviter_bot "