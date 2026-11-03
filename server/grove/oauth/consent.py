"""The consent page: the fragment /authorize puts in web/consent.html, the error fragment, and the
csrf value that ties a decision to the page and the session it was shown in. docs/hosts.md.

The template holds two marks, each exactly once: `<!--grove:title-->` in its <title> and
`<!--grove:consent-->` where the fragment goes. Every value that reaches the page goes through
html.escape, the client's self-declared name first of all.

A document client with a loopback redirect gets the hedged words of a dynamic client and a warning.
Its document proves who wrote the document, not who asks: any program on this computer can name that
URL as its client_id and listen on the port (MCP 2025-11-25, Localhost Redirect URI Risks).
"""

import hashlib
import hmac
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from html import escape
from urllib.parse import quote

from grove.oauth.scopes import SCOPE_WORDS

TITLE_MARK = "<!--grove:title-->"
CONSENT_MARK = "<!--grove:consent-->"
DECISION_PATH = "/authorize/decision"
APPROVE = "approve"
DENY = "deny"

# The parameters the csrf value covers, as the design names them.
CSRF_FIELDS = (
    "client_id",
    "redirect_uri",
    "redirect_given",
    "scope",
    "code_challenge",
    "state",
    "resource",
)


class TemplateBroken(Exception):
    """The built page does not hold each mark exactly once."""


@dataclass(frozen=True)
class ConsentView:
    """What the page says about one request. `document_host` is set for a document client, and
    `loopback` when the redirect goes to this computer."""

    client_name: str | None
    document_host: str | None
    return_place: str
    scopes: Sequence[str]
    person_title: str | None
    loopback: bool


def csrf_token(session_token: str, fields: Mapping[str, str]) -> str:
    """Hex HMAC-SHA256 keyed by the session cookie value over the sorted `k=v` lines of the
    canonical parameters. Each value is percent-encoded, so no value can forge a line."""
    lines = "\n".join(f"{name}={quote(fields.get(name, ''), safe='')}" for name in sorted(fields))
    key = session_token.encode("utf-8")
    return hmac.new(key, lines.encode("utf-8"), hashlib.sha256).hexdigest()


def same_csrf(given: object, expected: str) -> bool:
    if not isinstance(given, str):
        return False
    return hmac.compare_digest(given.encode("utf-8", "replace"), expected.encode("utf-8"))


def _who(view: ConsentView) -> str:
    name = escape(view.client_name) if view.client_name else None
    if view.document_host is not None:
        host = f"<strong>{escape(view.document_host)}</strong>"
        if view.loopback:
            claim = f"An app that says it is <strong><q>{name}</q></strong>" if name else "An app"
            return (
                f'<p class="consent-who">{claim} asks to use your Grove. It names a description '
                f"on {host}.</p>"
                '<p class="consent-who">Any program on this computer can claim this name. Approve '
                "only if you started this connection yourself.</p>"
            )
        called = f" It calls itself <q>{name}</q>." if name else ""
        return f'<p class="consent-who">{host} asks to use your Grove.{called}</p>'
    if name:
        return (
            '<p class="consent-who">An app that says it is '
            f"<strong><q>{name}</q></strong> asks to use your Grove.</p>"
        )
    return '<p class="consent-who">An app with no name asks to use your Grove.</p>'


def _hidden(fields: Mapping[str, str]) -> str:
    # name before value, one space apart: the order a reader of the form may rely on.
    return "".join(
        f'<input type="hidden" name="{escape(name)}" value="{escape(value)}">'
        for name, value in fields.items()
    )


def consent_fragment(view: ConsentView, fields: Mapping[str, str], csrf: str) -> str:
    """The question, what the host will be able to do, and the form with Deny and Approve. Deny
    comes first, so a person who presses Enter refuses."""
    person = (
        f"Signed in as <strong>{escape(view.person_title)}</strong>"
        if view.person_title
        else "Signed in"
    )
    scopes = "".join(f"<li>{escape(SCOPE_WORDS[scope])}</li>" for scope in view.scopes)
    hidden = _hidden({**fields, "csrf": csrf})
    return (
        '<h1 id="consent-title">Connect an app to your Grove</h1>'
        f"{_who(view)}"
        '<dl class="consent-facts">'
        f"<div><dt>You</dt><dd>{person}</dd></div>"
        f"<div><dt>Afterwards</dt><dd>Grove sends you back to {escape(view.return_place)}"
        "</dd></div>"
        "</dl>"
        '<h2 class="consent-label">If you approve, it can</h2>'
        f'<ul class="consent-scopes">{scopes}</ul>'
        f'<form class="consent-form" method="post" action="{DECISION_PATH}">{hidden}'
        '<div class="consent-actions">'
        f'<button class="consent-deny" type="submit" name="decision" value="{DENY}">Deny</button>'
        '<button class="consent-approve" type="submit" name="decision" '
        f'value="{APPROVE}">Approve</button>'
        "</div></form>"
        '<p class="consent-note">Approve only an app you trust. It reaches your Grove until you '
        "disconnect it in Grove.</p>"
    )


def error_fragment(title: str, message: str) -> str:
    return (
        f'<h1 id="consent-title">{escape(title)}</h1>'
        f'<p class="consent-who">{escape(message)}</p>'
        '<p class="consent-note">Go back to the app and connect it again.</p>'
    )


def render_page(template: str, *, title: str, fragment: str) -> str:
    """The template with its title and its fragment. The title is escaped here; the fragment is
    escaped where it is built."""
    if template.count(TITLE_MARK) != 1 or template.count(CONSENT_MARK) != 1:
        raise TemplateBroken("the consent page must hold each mark exactly once")
    page = template.replace(TITLE_MARK, escape(title), 1)
    return page.replace(CONSENT_MARK, fragment, 1)
