# The runner image: OpenCode at the pin and the server package (the loop, the brain CLI and the
# brain shell) with its locked dependencies, under the grove-runner user. Built on the box by compose.
FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337

COPY --from=ghcr.io/astral-sh/uv:0.12.1@sha256:cf4eedcaa81655197f625739489effcbe71b61ceb1506f332c3facae5deceded /uv /usr/local/bin/uv

ARG OPENCODE_VERSION=1.18.32
ARG OPENCODE_SHA256=3046e0404fdc60fb80307e7a47824ba07477364178a4d09baa8548496dd6d43b

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN python -c "import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], '/tmp/opencode.tar.gz')" \
        "https://github.com/anomalyco/opencode/releases/download/v${OPENCODE_VERSION}/opencode-linux-x64.tar.gz" \
    && echo "${OPENCODE_SHA256}  /tmp/opencode.tar.gz" | sha256sum --check --strict \
    && tar -xzf /tmp/opencode.tar.gz -C /usr/local/bin opencode \
    && rm /tmp/opencode.tar.gz \
    && test "$(/usr/local/bin/opencode --version)" = "${OPENCODE_VERSION}" \
    && groupadd --system --gid 10002 grove-runner \
    && useradd --system --uid 10002 --gid 10002 --no-create-home --home-dir /var/lib/grove-runner \
        --shell /usr/sbin/nologin grove-runner \
    && install -d -o 10002 -g 10002 -m 700 /var/lib/grove-runner

# The server package as in the API image. The loop finds `brain` and `grove-brain-shell`, its
# console scripts, next to the venv's python.
WORKDIR /app/server
COPY server/pyproject.toml server/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY server/ ./
RUN uv sync --frozen --no-dev

# OpenCode's config and the agents. The loop copies them into HOME before each pass. At its first
# run OpenCode tries to install @opencode-ai/plugin with npm into its config folder; the runner
# network has no route out, so that install fails closed there, and no plugin is configured, so
# nothing needs it.
COPY runner/ /opt/grove/runner/

ENV PATH=/app/server/.venv/bin:$PATH \
    GROVE_RUNNER_CONFIG=/opt/grove/runner \
    GROVE_OPENCODE_BIN=/usr/local/bin/opencode

# Without OPENCODE_DISABLE_CLAUDE_CODE, OpenCode reads Claude Code's files from HOME.
ENV HOME=/var/lib/grove-runner \
    XDG_CONFIG_HOME=/var/lib/grove-runner/.config \
    XDG_DATA_HOME=/var/lib/grove-runner/.local/share \
    XDG_STATE_HOME=/var/lib/grove-runner/.local/state \
    XDG_CACHE_HOME=/var/lib/grove-runner/.cache \
    OPENCODE_DISABLE_CLAUDE_CODE=1

USER grove-runner
WORKDIR /var/lib/grove-runner
CMD ["python", "-m", "grove.runner"]
