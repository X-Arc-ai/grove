# The API image: the server package with its locked dependencies. Built on the box by compose.
FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337

# The read-only local-clone connector reads Git objects without hooks or network access.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12.1@sha256:cf4eedcaa81655197f625739489effcbe71b61ceb1506f332c3facae5deceded /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app/server
COPY server/pyproject.toml server/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
# The whole server/ folder, as in a checkout: the migrations are not in the wheel, and the
# editable install keeps grove.writer.migrate pointed at /app/server/migrations.
COPY server/ ./
# The pages the API serves: the views, the homepage and the privacy page. The privacy page's table
# comes from docs/data-flow.md, the one file of docs/ that reaches the image.
COPY web/ /app/web/
COPY docs/data-flow.md /app/docs/data-flow.md
RUN python /app/web/build.py
ENV GROVE_WEB_DIST=/app/web/dist
RUN uv sync --frozen --no-dev \
    && groupadd --system --gid 10001 grove-api \
    && useradd --system --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin grove-api

ENV PATH=/app/server/.venv/bin:$PATH
USER grove-api
EXPOSE 8000
CMD ["uvicorn", "grove.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
