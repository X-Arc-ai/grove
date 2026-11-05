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
CODE_POLICY = "Code policy available"

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
        "The separate consent requests `calendar.readonly` and `openid`; "
        "sign-in does not request Calendar access. Provider credentials are encrypted "
        "in the store; the key stays outside it.",
    ),
    (
        "git",
        "Commit metadata from a configured Git repository",
        "A world-approved local Git clone to Grove's screened sources",
        "Grove reads commit objects without fetching, hooks or worktree files. "
        "A commit is not proof of deployment.",
    ),
    (
        "pipedream",
        "An opaque owner identifier, the selected app and account connection status",
        "Pipedream's hosted authorization and account-status API",
        "Provider consent occurs on the hosted page. This component imports no source content "
        "and executes no provider actions. A configured path does not prove owner consent.",
    ),
    (
        "uploads",
        "Supported conversation exports you upload",
        "Grove's server; code screens content before the `sources` table",
        "The upload is temporary. There is no local runner. "
        "A code-supplied account policy drops or masks sensitive content before storage, "
        "without a model call. Personal exclusions and identifier exemptions are not configured "
        "by this upload route; operator and connector policies are separate. "
        "Eligible retained Claude and ChatGPT messages enter the Learner queue. "
        "The server sends that screened text to Meta for extraction.",
    ),
    (
        "agent_files",
        "Files and notes you add in setup",
        "Grove's server, in your account's working folder for its agent; "
        "Meta Model API when the Learner reads them",
        "Stored as you add them, without screening. What the Learner reads goes to the model "
        "without screening. You can remove them from the same screen.",
    ),
    (
        "files",
        "Notes and supported text, Markdown, PDF, DOCX and PPTX files",
        "Grove's screened Sources and bounded material revision metadata",
        "Original binaries are not retained. Imported dates and authors remain unknown. "
        "Files are retained pending typed graph admission; notes use the ordinary Learner queue. "
        "Material deletion covers all revisions through the private forget ledger.",
    ),
    (
        "spark",
        "Screened text for extraction, verification and agent work",
        "Meta Model API, Muse Spark 1.3",
        "Provider terms apply. Contributor model ids are refused. "
        "Eligible screened uploads may be processed by the server Learner. "
        "Retained input, queued work and completed extraction are different states.",
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
    """A fixed vocabulary for installed code policy and the presence of named settings."""
    env = os.environ if env is None else env

    def present(*names):
        return all(bool(env.get(name)) for name in names)

    google_present = (
        present("GROVE_GOOGLE_OAUTH_FILE") if google_configured is None else bool(google_configured)
    )
    screening_present = env.get("GROVE_ENV") != "production" or present(
        "GROVE_RENAME_MAP", "GROVE_DENYLIST"
    )
    checks = {
        "email": present("RESEND_API_KEY", "MAIL_FROM"),
        "google": google_present,
        "store": present("NEO4J_AUTH"),
        "calendar": google_present and present("GROVE_CONNECTOR_KEY_FILE") and screening_present,
        "git": present("GROVE_GIT_REPOSITORIES_FILE") and screening_present,
        "pipedream": present(
            "PIPEDREAM_PROJECT_ID", "PIPEDREAM_CLIENT_ID", "PIPEDREAM_CLIENT_SECRET"
        )
        and env.get("PIPEDREAM_ENVIRONMENT") == "production",
        "spark": present("MODEL_API_KEY"),
        "voice": present("MODEL_API_KEY"),
        "glimmer": present("GROVE_GLIMMER_BASE_URL", "GROVE_GLIMMER_KEY"),
        "hosts": True,
        "forget": present("GROVE_FORGET_LEDGER_DIR"),
        "agent_files": present("GROVE_AGENT_FILES_DIR"),
    }
    rows = []
    for key, data, destination, terms in _ROWS:
        if not runtime or key == "backups":
            status = STATIC
        elif key in {"uploads", "files"}:
            status = CODE_POLICY
        else:
            status = PRESENT if checks[key] else ABSENT
        rows.append(
            {"id": key, "data": data, "destination": destination, "terms": terms, "status": status}
        )
    return {
        "runtime": runtime,
        "rows": rows,
        "notice": "Settings present means configuration exists. "
        "It does not prove that the path completed an operation. "
        "Setting validity, file contents and owner consent are not checked. "
        "Code policy available means screening code is installed, not that an import "
        "completed or owner consent was checked.",
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
        "Code policy available describes installed screening code, not a completed import "
        "or owner consent.",
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
        "There is no local runner. Code screens conversation exports on the server before storage.",
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
