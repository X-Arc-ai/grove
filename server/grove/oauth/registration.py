"""Client metadata: the body of a dynamic registration (RFC 7591) and the checks a Client ID
Metadata Document shares with it. docs/hosts.md.

An error names the rule a field breaks, never what the client sent: the client reads the error, and
a log of it must hold nothing a client chose.
"""

import unicodedata
from dataclasses import dataclass

from grove.oauth.redirects import redirect_uri_problem
from grove.writer.oauth import AUTH_METHODS, PUBLIC_CLIENT

MAX_REDIRECT_URIS = 10
MAX_CLIENT_NAME = 100
GRANT_TYPES = ("authorization_code", "refresh_token")
RESPONSE_TYPES = ("code",)
# RFC 7591 section 2: a registration that names no method gets client_secret_basic.
DEFAULT_AUTH_METHOD = "client_secret_basic"
MAX_REGISTRATION_BYTES = 16 * 1024

INVALID_CLIENT_METADATA = "invalid_client_metadata"
INVALID_REDIRECT_URI = "invalid_redirect_uri"


class InvalidMetadata(ValueError):
    """A registration or a document that breaks a rule: `error` is the RFC 7591 code."""

    def __init__(self, error: str, description: str) -> None:
        super().__init__(description)
        self.error = error
        self.description = description


@dataclass(frozen=True)
class ClientMetadata:
    redirect_uris: tuple[str, ...]
    client_name: str | None
    grant_types: tuple[str, ...]
    response_types: tuple[str, ...]
    auth_method: str

    @property
    def confidential(self) -> bool:
        return self.auth_method != PUBLIC_CLIENT


def _metadata_error(description: str) -> InvalidMetadata:
    return InvalidMetadata(INVALID_CLIENT_METADATA, description)


def clean_client_name(value: object) -> str | None:
    """The self-declared name with every control and format character removed, trimmed, and cut
    to 100 characters; None for no name. A format character (a direction override) could make the
    consent page show a name the host did not send in that order."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise _metadata_error("client_name must be a string")
    kept = "".join(char for char in value if unicodedata.category(char)[0] != "C")
    name = " ".join(kept.split())[:MAX_CLIENT_NAME].strip()
    return name or None


def check_redirect_uris(value: object) -> tuple[str, ...]:
    """1 to 10 redirect URIs, each one a URI that may be registered; each kept once, in order."""
    if not isinstance(value, list) or not value:
        raise InvalidMetadata(INVALID_REDIRECT_URI, "redirect_uris must be a non-empty list")
    if len(value) > MAX_REDIRECT_URIS:
        raise InvalidMetadata(
            INVALID_REDIRECT_URI, f"redirect_uris holds at most {MAX_REDIRECT_URIS} URIs"
        )
    for uri in value:
        problem = redirect_uri_problem(uri)
        if problem is not None:
            raise InvalidMetadata(INVALID_REDIRECT_URI, problem)
    return tuple(dict.fromkeys(value))


def _codes(body: dict, name: str, allowed: tuple[str, ...], default: tuple[str, ...]):
    value = body.get(name)
    if value is None:
        return default
    names = " and ".join(allowed)
    if not isinstance(value, list) or not value or not all(isinstance(v, str) for v in value):
        raise _metadata_error(f"{name} must be a non-empty list of {names}")
    if not set(value) <= set(allowed):
        raise _metadata_error(f"{name} may hold only {names}")
    return tuple(dict.fromkeys(value))


def parse_registration(body: object) -> ClientMetadata:
    """The metadata of a POST /register body. Fields Grove does not use are ignored."""
    if not isinstance(body, dict):
        raise _metadata_error("the body must be a JSON object")
    redirect_uris = check_redirect_uris(body.get("redirect_uris"))
    method = body.get("token_endpoint_auth_method", DEFAULT_AUTH_METHOD)
    if method not in AUTH_METHODS:
        names = ", ".join(AUTH_METHODS)
        raise _metadata_error(f"token_endpoint_auth_method must be one of {names}")
    return ClientMetadata(
        redirect_uris=redirect_uris,
        client_name=clean_client_name(body.get("client_name")),
        grant_types=_codes(body, "grant_types", GRANT_TYPES, GRANT_TYPES),
        response_types=_codes(body, "response_types", RESPONSE_TYPES, RESPONSE_TYPES),
        auth_method=method,
    )
