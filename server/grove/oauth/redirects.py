"""Redirect URIs: what a client may register, and when an authorization request's redirect_uri
matches one of them. docs/hosts.md.

A client may register an https URI (any host), an http URI on a loopback host (127.0.0.1, ::1,
localhost), or a private-use scheme of a native app (RFC 8252 section 7.1). No URI has a fragment.

A redirect_uri matches when it equals a registered URI exactly, string for string. The one other
match is the loopback rule of RFC 8252 section 7.3: an http loopback redirect may differ in its
port only, so the scheme, the host, the path and the query stay the same. `localhost` matches only
`localhost`, never 127.0.0.1.
"""

import re
from collections.abc import Iterable
from urllib.parse import SplitResult, urlencode, urlsplit

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
MAX_REDIRECT_URI_LENGTH = 2000
# Schemes a native app may not claim: the web's own, and those that run or read in the browser.
BLOCKED_SCHEMES = frozenset(
    {"http", "https", "javascript", "data", "file", "vbscript", "about", "blob"}
)

_SCHEME = re.compile(r"[a-z][a-z0-9+.\-]*")
# The characters RFC 3986 allows in a URI. Any other one would reach the Location header changed.
_URI_CHARS = re.compile(r"[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")


def _split(uri: str) -> SplitResult | None:
    try:
        parts = urlsplit(uri)
        _ = parts.port  # a port that is not a number raises here
    except ValueError:
        return None
    return parts


def _has_user(parts: SplitResult) -> bool:
    return parts.username is not None or parts.password is not None


def redirect_uri_problem(uri: object) -> str | None:
    """None when the URI may be registered, else the rule it breaks. The rule never quotes it."""
    if not isinstance(uri, str) or not uri:
        return "each redirect URI must be a non-empty string"
    if len(uri) > MAX_REDIRECT_URI_LENGTH:
        return f"a redirect URI is at most {MAX_REDIRECT_URI_LENGTH} characters"
    if _URI_CHARS.fullmatch(uri) is None:
        return "a redirect URI holds only the characters of RFC 3986"
    if "#" in uri:
        return "a redirect URI has no fragment"
    parts = _split(uri)
    if parts is None or _SCHEME.fullmatch(parts.scheme) is None:
        return "a redirect URI must be an absolute URI"
    if parts.scheme == "https":
        if not parts.hostname or _has_user(parts):
            return "an https redirect URI needs a host and no user part"
        return None
    if parts.scheme == "http":
        if parts.hostname not in LOOPBACK_HOSTS or _has_user(parts):
            return "http is for a loopback host only: 127.0.0.1, ::1 or localhost"
        return None
    if parts.scheme in BLOCKED_SCHEMES:
        return "that scheme cannot be a redirect URI"
    return None


def _loopback_key(uri: str) -> tuple[str, str, str, str] | None:
    parts = _split(uri)
    if parts is None or parts.scheme != "http" or parts.hostname not in LOOPBACK_HOSTS:
        return None
    return parts.scheme, parts.hostname, parts.path, parts.query


def redirect_matches(given: object, registered: Iterable[str]) -> bool:
    """An exact match with a registered URI, or a loopback redirect that differs in port only."""
    if not isinstance(given, str):
        return False
    registered = tuple(registered)
    if given in registered:
        return True
    if redirect_uri_problem(given) is not None:
        return False
    key = _loopback_key(given)
    if key is None:
        return False
    return any(_loopback_key(uri) == key for uri in registered)


def is_loopback(uri: str) -> bool:
    return _loopback_key(uri) is not None


def return_place(uri: str) -> str:
    """Where the redirect takes the person, in words for the consent page."""
    if is_loopback(uri):
        return "this computer"
    parts = _split(uri)
    if parts is None:
        return "the app"
    if parts.scheme == "https" and parts.hostname:
        return parts.hostname
    return f"an app on this device ({parts.scheme}:)"


def with_params(uri: str, params: dict[str, str]) -> str:
    """The URI with the parameters added to its query: `&` when it has a query already."""
    query = urlencode(params)
    if uri.endswith(("?", "&")):
        return uri + query
    return uri + ("&" if "?" in uri else "?") + query
