# vobiz-vllm-omni-bridge — Vobiz <Stream> ⇄ self-hosted vLLM-Omni duplex bridge
#
#   docker build -t vobiz-vllm-omni-bridge .
#   docker run --env-file .env -p 8000:8000 -v bridge-data:/app/data vobiz-vllm-omni-bridge
#
# WITH_CLOUDFLARED=true bakes the cloudflared binary in so the container can open a
# quick tunnel by itself when PUBLIC_URL is empty (development). Production images
# behind a real hostname can build with --build-arg WITH_CLOUDFLARED=false.

FROM python:3.12-slim AS base

ARG WITH_CLOUDFLARED=true

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HTTP_PORT=8000

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

RUN if [ "$WITH_CLOUDFLARED" = "true" ]; then \
      arch="$(dpkg --print-architecture)"; \
      curl -fsSL -o /usr/local/bin/cloudflared \
        "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${arch}" \
      && chmod +x /usr/local/bin/cloudflared \
      && cloudflared --version; \
    fi

WORKDIR /app

# Dependencies first so source edits do not invalidate the pip layer.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Run as an unprivileged user. /app/data holds logs, per-call JSON and runtime state.
RUN useradd --system --uid 10001 --create-home bridge \
 && mkdir -p /app/data \
 && chown -R bridge:bridge /app
USER bridge

EXPOSE 8000
VOLUME ["/app/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('HTTP_PORT','8000'), timeout=4).status == 200 else 1)"

CMD ["python", "app.py"]
