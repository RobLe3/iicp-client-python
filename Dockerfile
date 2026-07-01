# IICP Python node — runs an iicp-client provider node out of the box.
#
#   docker build -t iicp-node-py .
#   docker run --restart on-failure -p 8020:8020 \
#     -e IICP_BACKEND_URL=http://host.docker.internal:11434 \
#     -e IICP_BACKEND_MODEL=qwen2.5:0.5b \
#     -e IICP_PUBLIC_ENDPOINT=http://<your-public-ip>:8020 \
#     iicp-node-py
#
# Required env vars:
#   IICP_BACKEND_URL    — OpenAI-compatible backend (Ollama / vLLM / LM Studio)
#   IICP_BACKEND_MODEL  — model name (e.g. qwen2.5:0.5b)
#
# Optional:
#   IICP_PUBLIC_ENDPOINT — externally reachable URL of this node. If omitted,
#                          the node tries automatic reachability (Quick Tunnel
#                          first, relay last-resort) before staying local.
#   IICP_TUNNEL_DEAD_POLICY — auto|retry|exit|log-only; default auto exits when
#                          supervised so Docker can restart, manual runs retry.
#   IICP_SUPERVISED   — default 1 in this image; keep with --restart on-failure.
#   IICP_DIRECTORY_URL  — default: https://iicp.network/api
#   IICP_REGION         — default: eu-central
#   IICP_MAX_CONCURRENT — default: 4
#   IICP_NODE_ID        — auto-generated if absent
#   IICP_INTENT         — default: urn:iicp:intent:llm:chat:v1
#
# See https://iicp.network/docs/sdk-quickstart-docker for the full setup guide.

FROM python:3.12-slim AS build
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir ".[metrics,iicp-tcp]"

FROM python:3.12-slim AS runtime
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
  && arch="$(dpkg --print-architecture)" \
  && case "$arch" in \
      amd64) cf_arch=amd64 ;; \
      arm64) cf_arch=arm64 ;; \
      *) echo "unsupported architecture for cloudflared: $arch" >&2; exit 1 ;; \
    esac \
  && curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${cf_arch}" -o /usr/local/bin/cloudflared \
  && chmod +x /usr/local/bin/cloudflared \
  && cloudflared --version >/dev/null \
  && rm -rf /var/lib/apt/lists/*
COPY --from=build /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=build /usr/local/bin/iicp-node /usr/local/bin/iicp-node
COPY --from=build /app/src /app/src
ENV PYTHONPATH=/app/src
ENV IICP_SUPERVISED=1 \
    IICP_TUNNEL_DEAD_POLICY=auto \
    IICP_PORT=8020
EXPOSE 8020
HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=5 \
  CMD python3 -c "import urllib.request,sys; r=urllib.request.urlopen('http://localhost:8020/iicp/health',timeout=5); sys.exit(0 if r.status==200 else 1)"
CMD ["python", "-m", "iicp_client.cli", "serve"]
