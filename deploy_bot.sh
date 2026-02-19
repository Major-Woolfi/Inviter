#!/bin/bash

cd /root/bots/Inviter_bot/

cat > requirements.txt << EOF
aiogram
telethon
aiofiles
aiosqlite
python-dotenv
EOF

# ✅ Правильный Dockerfile
cat > Dockerfile << EOF
FROM python:3.13.9
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
CMD ["python", "main.py"]
EOF

# Собери и запусти
sudo docker build -t inviter_bot .
sudo docker rm -f inviter_bot || true
sudo docker run -d \
  --name inviter_bot \
  --restart always \
  -v /root/bots/Inviter_bot:/app  \
  inviter_bot

echo "✅ Готово!"
echo "Логи: sudo docker logs -f inviter_bot"