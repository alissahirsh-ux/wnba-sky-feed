FROM python:3.12-slim
WORKDIR /app
COPY feed_bot.py .
CMD ["python", "-u", "feed_bot.py"]
