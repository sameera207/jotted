# rmtasks for Railway: the web app, background scheduler and rmapi in one container.
FROM python:3.12-slim

# rmapi (ddvk fork): cloud access. Pinned, with the checksum GitHub publishes for the release.
ARG RMAPI_VERSION=v0.0.35
ARG RMAPI_SHA256=117616151d11937446ead6972b0934f97155443087f8406141db38fd1ac8fb25
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
 && curl -fsSL -o /tmp/rmapi.tar.gz "https://github.com/ddvk/rmapi/releases/download/${RMAPI_VERSION}/rmapi-linux-amd64.tar.gz" \
 && echo "${RMAPI_SHA256}  /tmp/rmapi.tar.gz" | sha256sum -c - \
 && tar -xzf /tmp/rmapi.tar.gz -C /usr/local/bin rmapi && chmod 755 /usr/local/bin/rmapi \
 && rm /tmp/rmapi.tar.gz && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv==0.11.14
WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/app/.venv UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Dependencies first, so code changes don't reinstall them.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

COPY config.railway.toml docker-entrypoint.sh ./
RUN chmod 755 docker-entrypoint.sh

ENV PATH="/app/.venv/bin:${PATH}" \
    RMTASKS_CONFIG=/app/config.railway.toml \
    RMTASKS_SECURE_COOKIES=1 \
    HOME=/data/home \
    PYTHONUNBUFFERED=1

EXPOSE 8080
ENTRYPOINT ["/app/docker-entrypoint.sh"]
