# The runner image: OpenCode at the pin, under the grove-runner user. A placeholder process runs
# until the runner lane lands its loop.
FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337

ARG OPENCODE_VERSION=1.18.32
ARG OPENCODE_SHA256=3046e0404fdc60fb80307e7a47824ba07477364178a4d09baa8548496dd6d43b

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

COPY deploy/runner-placeholder.sh /usr/local/bin/grove-runner-placeholder

# Without OPENCODE_DISABLE_CLAUDE_CODE, OpenCode reads Claude Code's files from HOME.
ENV HOME=/var/lib/grove-runner \
    XDG_CONFIG_HOME=/var/lib/grove-runner/.config \
    XDG_DATA_HOME=/var/lib/grove-runner/.local/share \
    XDG_STATE_HOME=/var/lib/grove-runner/.local/state \
    XDG_CACHE_HOME=/var/lib/grove-runner/.cache \
    OPENCODE_DISABLE_CLAUDE_CODE=1

USER grove-runner
WORKDIR /var/lib/grove-runner
CMD ["grove-runner-placeholder"]
