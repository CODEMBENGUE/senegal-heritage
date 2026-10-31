FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    espeak-ng \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY senegal_heritage.py .

RUN mkdir -p /data && chmod 777 /data

ENV STUDIO_HOME=/data
ENV PORT=8080
ENV MAX_WORKERS=1
ENV PYTHONUNBUFFERED=1
ENV AUTO_ACQUIRE=1

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8080/health || exit 1

CMD ["python", "senegal_heritage.py", "serve", "--host", "0.0.0.0", "--port", "8080"]