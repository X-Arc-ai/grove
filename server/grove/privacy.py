"""D14's public data-flow statement from a small, value-free configuration projection.

This module uses the standard library and supports the web builder's Python 3.9 runtime.
It never returns configuration values, secret paths or custom endpoint URLs. A setting's
presence is not a successful connection, a completed operation, or deployment acceptance.
"""

import os

STATIC = "Deployment not checked"
PRESENT = "Settings present"
ABSENT = "Not configured"
PENDING = "Implementation pending"

_ROWS = (
    (
        "email",
        "Your email address and a sign-in code",
        "Resend, to send the requested sign-in mail",
        "Resend Privacy Policy and Data Processing Addendum apply.",
    ),
    (
        "google",
        "The Google sign-in request",
        "Google; Grove receives your verified address and profile name",
        "Scopes `openid`, `email` and `profile` only.",
    ),
    (
        "store",
        "Items, links, history and source content",
        "Grove's Neo4j store on the configured server",
        "Access follows your identity and world. No text embedding service is used.",
    ),
    (
        "backups",
        "Backups of the store",
        "Offline dumps on the server",
        "The deployment policy keeps dumps for 30 days. "
        "Latest-ledger replay is required before restored data is served.",
    ),
    (
        "calendar",
        "Calendar events, if you connect a calendar",
        "Google Calendar to Grove, with separate read-only consent",
        "The later consent requests `calendar.readonly`; sign-in does not request it.",
    ),
    (
        "uploads",
        "Supported conversation exports you upload",
        "Grove's server; code screens content before the `sources` table",
        "The upload is temporary. There is no local runner. "
        "Ambiguous sensitive content is dropped or masked, never sent to a model for screening.",
    ),
    (
        "files",
        "Voice notes, documents and images",
        "The configured upload and model paths",
        "Support depends on the installed connector. "
        "A listed model is not proof of a completed import.",
    ),
    (
        "spark",
        "Screened text for extraction, verification and agent work",
        "Meta Model API, Muse Spark 1.3",
        "Provider terms apply. Contributor model ids are refused. "
        "This run uses synthetic data only.",
    ),
    (
        "voice",
        "Audio selected for transcription",
        "Meta Model API, Voice Transcribe",
        "Provider terms apply. Audio leaves the server when this operation runs.",
    ),
    (
        "glimmer",
        "Screened text and images for judgments",
        "Muse Glimmer on Modal",
        "Modal's Terms of Service and Privacy Policy apply. This run uses synthetic data only.",
    ),
    (
        "hosts",
        "Evidence returned to an authorized AI host",
        "The connected host's systems",
        "The host's own retention and training terms apply. "
        "Grove cannot delete copies already received by that host.",
    ),
    (
        "forget",
        "Forget targets and per-request approvals",
        "Grove's writer and a separate private forget ledger",
        "The ledger keeps ids, locators and counts, never the forgotten value. "
        "Downloaded exports and provider copies are outside this operation.",
    ),
)


def flow_statement(env=None, *, runtime=False, google_configured=None):
    """A fixed vocabulary derived only from whether named settings exist."""
    env = os.environ if env is None else env

    def present(*names):
        return all(bool(env.get(name)) for name in names)

    checks = {
        "email": present("RESEND_API_KEY", "MAIL_FROM"),
        "google": present("GROVE_GOOGLE_OAUTH_FILE")
        if google_configured is None
        else bool(google_configured),
        "store": present("NEO4J_AUTH"),
        "uploads": env.get("GROVE_ENV") != "production"
        or present("GROVE_RENAME_MAP", "GROVE_DENYLIST"),
        "spark": present("MODEL_API_KEY"),
        "voice": present("MODEL_API_KEY"),
        "glimmer": present("GROVE_GLIMMER_BASE_URL", "GROVE_GLIMMER_KEY"),
        "hosts": True,
        "forget": present("GROVE_FORGET_LEDGER_DIR"),
    }
    rows = []
    for key, data, destination, terms in _ROWS:
        if key in {"calendar", "files"}:
            status = PENDING
        elif not runtime or key == "backups":
            status = STATIC
        else:
            status = PRESENT if checks[key] else ABSENT
        rows.append(
            {"id": key, "data": data, "destination": destination, "terms": terms, "status": status}
        )
    return {
        "runtime": runtime,
        "rows": rows,
        "notice": "Settings present means configuration exists. "
        "It does not prove that the path completed an operation.",
    }


def markdown_statement():
    statement = flow_statement({}, runtime=False)
    lines = [
        "# Where the data goes",
        "",
        "Generated from `server/grove/privacy.py`. Do not edit this file directly.",
        "",
        "This checked-in statement describes the installed paths. Deployment is not checked here.",
        "The privacy page reads `/v1/privacy` for this server's current configuration status.",
        "Settings present does not prove a successful connection, "
        "import, model call or deployment.",
        "",
        "| Data | Where it goes | Terms | Status |",
        "|---|---|---|---|",
    ]
    for row in statement["rows"]:
        lines.append(
            "| " + " | ".join(row[key] for key in ("data", "destination", "terms", "status")) + " |"
        )
    lines += [
        "",
        "## Controls and limits",
        "",
        "There is no local runner. Code screens uploads on the server before storage.",
        "Stored text is untrusted data, never an instruction or authorization.",
        "A person must approve each forget request in a signed-in session. "
        "An AI host also needs the sensitive-write scope.",
        "A missing private ledger configuration makes forget unavailable. "
        "An old healthy build does not prove that the new runner serves.",
        "The clean next-dump reconstruction is an isolated CI proof. "
        "It does not install a production backup procedure.",
        "Provider-side copies and downloaded exports remain outside Grove's deletion control.",
        "",
        "## Provider policies",
        "",
        "- Resend: https://resend.com/legal/privacy-policy and https://resend.com/legal/dpa",
        "- Google API Services User Data Policy: https://developers.google.com/terms/api-services-user-data-policy",
        "- Modal: https://modal.com/legal/terms and https://modal.com/legal/privacy-policy",
        "- Meta Model API: https://dev.meta.ai/legal/terms-of-service",
        "",
        "Grove's use and transfer of information received from Google APIs adheres to the "
        "Google API Services User Data Policy, including the Limited Use requirements.",
        "",
    ]
    return "\n".join(lines)
