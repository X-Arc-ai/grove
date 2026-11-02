"""Muse Glimmer 30B on Modal, behind vLLM's OpenAI-compatible server, scaled to zero.

Grove's open-weights model (decision 5): triage, ambiguous spans, images, pairwise judgments and
the second opinion go to this server through the model-call layer (server/grove/models/). Only
synthetic or screened text reaches it.

What runs: vLLM 0.32.0 (native Glimmer support since 0.28.0) with the BF16 weights at a pinned
revision, on one 80 GB GPU (A100 80 GB, then H100). The weights need about 60 GB, so a 48 GB card
does not fit them. No container runs while there is no traffic (min_containers 0); an idle one
stops after one minute. A server scaled to zero answers 503 until a container is up and queues
nothing, so the layer retries 503 on this route for about ten minutes. The first cold start took
about three minutes on an H100. So one call after a quiet time costs about 0.27 USD of GPU: 0.20
for the cold start and 0.07 for the idle minute. A call every four minutes bills a GPU all day.

Effort: Glimmer's template reads `reasoning_strength` (low, medium, high, xhigh; default high).
The layer sends it in `chat_template_kwargs` on every call; the server default is low. vLLM
ignores a top-level `reasoning_effort` without an error.

Tools: `--tool-call-parser muse_glimmer` with `--reasoning-parser muse_glimmer`, and tool_choice
"auto" only. Glimmer emits one tool call per message.

Auth: Modal's proxy auth stays on (the default for a Server). A caller sends one header,
`Authorization: Bearer <proxy token id>.<proxy token secret>`; vLLM runs with no key of its own.

Deploy, with MODAL_TOKEN_ID and MODAL_TOKEN_SECRET in the environment (modal 1.6.3):

    modal run deploy/modal/glimmer.py::download     # fills the weights volume, once
    modal deploy deploy/modal/glimmer.py
    modal workspace proxy-tokens create --name grove-glimmer

The deploy does not print the server URL. Read it with
`modal.Server.from_name("grove-glimmer", "GlimmerServer").get_url()`. Then set
GROVE_GLIMMER_BASE_URL to that URL followed by /v1, and GROVE_GLIMMER_KEY to
`<token id>.<token secret>`.

The spend: `modal billing report --for "this month"`. `model_calls` does not hold cold starts or
idle GPU time, so this report is the true figure.
"""

import subprocess

import modal

APP_NAME = "grove-glimmer"
# Not gated on Hugging Face (its API said gated false on 2026-11-02), so no token is needed.
MODEL_REPO = "meta-models/Muse-Glimmer-30B"
# 2026-10-22: the revision with the fixed chat template. A moving revision could change both.
MODEL_REVISION = "cec94fcfd6512f2cfe708138c32533b346ea4f69"
SERVED_MODEL_NAME = "muse-glimmer-30b"
# By digest, as the store's image is: a tag can move.
VLLM_IMAGE = (
    "vllm/vllm-openai:v0.32.0"
    "@sha256:befccf0e7e4089aa23926bb6f887a85c0d46c2a99e6de5c5ffdf5cb7957c5b34"
)
PORT = 8000
HF_CACHE = "/root/.cache/huggingface"
VLLM_CACHE = "/root/.cache/vllm"
# Short on purpose: the workspace has no spend limit, and the meter does not see idle GPU time.
IDLE_SECONDS = 60
STARTUP_SECONDS = 30 * 60
MAX_MODEL_LEN = 131072

app = modal.App(APP_NAME)
hf_cache = modal.Volume.from_name("grove-glimmer-hf-cache", create_if_missing=True)
vllm_cache = modal.Volume.from_name("grove-glimmer-vllm-cache", create_if_missing=True)
volumes = {HF_CACHE: hf_cache, VLLM_CACHE: vllm_cache}

image = (
    modal.Image.from_registry(VLLM_IMAGE)
    .entrypoint([])
    # Modal runs a function on the image's `python`, and this image has only `python3`: with no
    # link, Modal refuses it ("unable to determine the version of Python"). Modal's own vLLM
    # example adds the same link.
    .run_commands('ln -sf "$(which python3)" /usr/bin/python')
    .env(
        {
            "HF_HUB_CACHE": HF_CACHE,
            "HF_XET_HIGH_PERFORMANCE": "1",
            "VLLM_MEDIA_URL_ALLOW_REDIRECTS": "0",
            # vLLM reports usage stats by default: this host sends nothing to any other host.
            "VLLM_NO_USAGE_STATS": "1",
            "DO_NOT_TRACK": "1",
        }
    )
)

SERVE_COMMAND = [
    "vllm",
    "serve",
    MODEL_REPO,
    "--revision",
    MODEL_REVISION,
    "--served-model-name",
    SERVED_MODEL_NAME,
    "--host",
    "0.0.0.0",
    "--port",
    str(PORT),
    "--max-model-len",
    str(MAX_MODEL_LEN),
    "--generation-config",
    "auto",
    "--enable-auto-tool-choice",
    "--tool-call-parser",
    "muse_glimmer",
    "--reasoning-parser",
    "muse_glimmer",
    "--default-chat-template-kwargs",
    '{"reasoning_strength": "low"}',
    # No remote media: an image comes inline as a data URL, so the server fetches no URL for a
    # caller (vLLM's guidance against SSRF). The gateway refuses other image parts too.
    "--allowed-media-domains",
    "media.invalid",
]


@app.function(image=image, volumes=volumes, timeout=60 * 60)
def download() -> None:
    """Fetch the pinned weights into the volume, so a cold start loads them instead."""
    from huggingface_hub import snapshot_download

    snapshot_download(MODEL_REPO, revision=MODEL_REVISION)
    hf_cache.commit()


@app.server(
    image=image,
    gpu=["A100-80GB", "H100"],
    port=PORT,
    volumes=volumes,
    min_containers=0,
    # Two, not one: a redeploy rolls over onto a second container. A burst cannot start more.
    max_containers=2,
    # Explicit, though it is the default for a Server: no call runs without the proxy token.
    unauthenticated=False,
    scaledown_window=IDLE_SECONDS,
    startup_timeout=STARTUP_SECONDS,
    routing_region="eu-west",
    exit_grace_period=60,
)
class GlimmerServer:
    @modal.enter()
    def start(self) -> None:
        self.process = subprocess.Popen(SERVE_COMMAND)

    @modal.exit()
    def stop(self) -> None:
        self.process.terminate()
        self.process.wait(timeout=60)
