"""`brain`: the agents' only way into Grove (section 4, "brain CLI").

It calls the API at GROVE_API_URL with the pass token in GROVE_PASS_TOKEN, and never the store.
The token binds one world and one pass, so no verb takes a world. Every answer is one JSON object
on stdout; stored text is inside its `data` field, in labelled blocks. A refusal is a JSON object
too, with exit status 1, so an agent reads the reason from the same place.

`brain propose` reads its batch from stdin (a quoted heredoc) or from --file, never from its
arguments: an argument would put the batch through the shell's word rules.
"""

import json
import os
import sys
from typing import Any

import click
import httpx

API_URL_ENV = "GROVE_API_URL"
TOKEN_ENV = "GROVE_PASS_TOKEN"
MAX_BATCH_BYTES = 2 * 1024 * 1024
_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


class BrainError(click.ClickException):
    """A refusal, printed as JSON on stdout with exit status 1."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.payload = {"error": {"code": code, "message": message, **details}}

    def show(self, file: Any = None) -> None:
        click.echo(json.dumps(self.payload, indent=1, sort_keys=True))


def _emit(doc: Any) -> None:
    click.echo(json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False))


def _client() -> httpx.Client:
    base = os.environ.get(API_URL_ENV, "").rstrip("/")
    token = os.environ.get(TOKEN_ENV, "")
    if not base:
        raise BrainError("not_configured", f"{API_URL_ENV} is not set")
    if not token:
        raise BrainError("not_configured", f"{TOKEN_ENV} is not set")
    return httpx.Client(
        base_url=f"{base}/v1/internal/brain",
        headers={"Authorization": f"Bearer {token}"},
        timeout=_TIMEOUT,
        trust_env=False,
        follow_redirects=False,
    )


def _call(method: str, path: str, **kwargs: Any) -> Any:
    try:
        with _client() as client:
            response = client.request(method, path, **kwargs)
    except httpx.HTTPError as exc:
        raise BrainError(
            "unavailable", f"the Grove API did not answer ({type(exc).__name__})"
        ) from None
    try:
        doc = response.json()
    except ValueError:
        doc = None
    if response.status_code >= 400 and not (
        isinstance(doc, dict) and doc.get("state") == "declined"
    ):
        error = doc.get("error") if isinstance(doc, dict) else None
        if isinstance(error, dict):
            raise BrainError(
                str(error.get("code") or "error"),
                str(error.get("message") or "refused"),
                status=response.status_code,
            )
        raise BrainError("error", "the Grove API refused the call", status=response.status_code)
    if doc is None:
        raise BrainError("error", "the Grove API answered with no JSON")
    if response.status_code >= 400:
        _emit(doc)
        sys.exit(1)
    return doc


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def brain() -> None:
    """Grove for agents: read the world of this pass, and propose changes to it."""


@brain.command()
def status() -> None:
    """This pass: its world, its agent, its inputs, and the world's counts."""
    _emit(_call("GET", "/status"))


@brain.command(name="pass")
def pass_() -> None:
    """The input of this pass: its requests with their source text, and the batch form."""
    _emit(_call("GET", "/pass"))


@brain.command()
@click.argument("text", nargs=-1, required=True)
@click.option("--limit", type=click.IntRange(1, 20), default=10, show_default=True)
def recall(text: tuple[str, ...], limit: int) -> None:
    """Items whose title, summary or search terms match TEXT."""
    _emit(_call("GET", "/recall", params={"q": " ".join(text), "limit": limit}))


@brain.command()
@click.argument("item_id")
def get(item_id: str) -> None:
    """One item: its fields, its active links and its active evidence rows."""
    _emit(_call("GET", f"/items/{item_id}"))


def _read_batch(path: str | None) -> Any:
    if path is not None:
        try:
            with open(path, "rb") as handle:
                raw = handle.read(MAX_BATCH_BYTES + 1)
        except OSError:
            raise BrainError("invalid_arguments", "the batch file cannot be read") from None
    else:
        if sys.stdin is None or sys.stdin.isatty():
            raise BrainError(
                "invalid_arguments", "pass the batch on stdin (a quoted heredoc) or with --file"
            )
        raw = sys.stdin.buffer.read(MAX_BATCH_BYTES + 1)
    if len(raw) > MAX_BATCH_BYTES:
        raise BrainError("invalid_arguments", "the batch is larger than 2 MiB")
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise BrainError("invalid_arguments", f"the batch is not valid JSON ({exc})") from None
    if not isinstance(doc, dict):
        raise BrainError("invalid_arguments", "the batch is a JSON object with ops and evidence")
    return doc


@brain.command()
@click.option("--request", "request_id", help="The request of this pass that the batch answers.")
@click.option("--file", "path", type=click.Path(dir_okay=False), help="Read the batch from a file.")
def propose(request_id: str | None, path: str | None) -> None:
    """Propose one batch. The writer checks it and commits it whole, or declines it whole."""
    batch = _read_batch(path)
    # The batch form of `brain pass` wraps the batch with its request id; take either form.
    if "batch" in batch and isinstance(batch["batch"], dict):
        request_id = request_id or batch.get("request_id")
        batch = batch["batch"]
    body: dict[str, Any] = {"batch": batch}
    if request_id is not None:
        body["request_id"] = request_id
    _emit(_call("POST", "/propose", json=body))


@brain.command()
@click.argument("request_id")
def finish(request_id: str) -> None:
    """Close a request of this pass that holds nothing to save."""
    _emit(_call("POST", "/finish", json={"request_id": request_id}))


def main() -> None:
    brain(prog_name="brain")


if __name__ == "__main__":
    main()
