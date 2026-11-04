"""Bounded public-page fetch; retained data is a separate writer operation.

Every hop passes AllowlistTransport and pins a checked global address. There is
no proxy, credential, cookie jar, compressed body, model call or implicit retry.
"""

import threading
from contextlib import nullcontext
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

from grove import clock
from grove.cleanup.access import CleanupRefused
from grove.models.egress import AllowlistTransport, EgressPolicy
from grove.oauth.metadata_document import global_address, system_resolver

MAX_PAGE_BYTES = 64 * 1024
FETCH_SECONDS = 10.0
_SLOTS = threading.BoundedSemaphore(2)


@dataclass(frozen=True)
class Page:
    url: str = field(repr=False)
    text: str = field(repr=False)


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "template", "noscript"}:
            self.hidden.append(tag)
        elif not self.hidden:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if self.hidden and self.hidden[-1] == tag:
            self.hidden.pop()
        elif not self.hidden:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


class _Pinned(httpx.BaseTransport):
    def __init__(self, resolver, inner, deadline):
        self.resolver = resolver
        self.inner = inner or httpx.HTTPTransport(retries=0)
        self.deadline = deadline

    def handle_request(self, request):
        if clock.monotonic() >= self.deadline:
            raise CleanupRefused("fetch_deadline")
        addresses = self.resolver(request.url.host, 443)
        if not addresses or len(addresses) > 32:
            raise CleanupRefused("fetch_address_invalid")
        checked = [global_address(value) for value in addresses]
        if any(value is None for value in checked):
            raise CleanupRefused("fetch_address_not_public")
        if clock.monotonic() >= self.deadline:
            raise CleanupRefused("fetch_deadline")
        headers = {
            "Host": request.url.host,
            "Accept": "text/plain, text/html",
            "Accept-Encoding": "identity",
        }
        left = max(0.01, self.deadline - clock.monotonic())
        pinned = httpx.Request(
            "GET",
            request.url.copy_with(host=str(checked[0])),
            headers=headers,
            extensions={
                "sni_hostname": request.url.host,
                "timeout": dict.fromkeys(("connect", "read", "write", "pool"), left),
            },
        )
        return self.inner.handle_request(pinned)

    def close(self):
        self.inner.close()


def _url(value: str) -> None:
    if (
        type(value) is not str
        or not 0 < len(value) <= 2048
        or not value.isascii()
        or not value.isprintable()
    ):
        raise CleanupRefused("fetch_url_invalid")
    if " " in value or "#" in value or "\\" in value:
        raise CleanupRefused("fetch_url_invalid")
    try:
        parts = urlsplit(value)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.port not in (None, 443)
        ):
            raise CleanupRefused("fetch_url_invalid")
    except ValueError:
        raise CleanupRefused("fetch_url_invalid") from None


def _read(url, hosts, resolver, inner, deadline):
    transport = AllowlistTransport(
        EgressPolicy({"curator": hosts}), "curator", _Pinned(resolver, inner, deadline)
    )
    with httpx.Client(transport=transport, trust_env=False, follow_redirects=False) as client:
        for hop in range(4):
            _url(url)
            with client.stream("GET", url) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    if hop == 3 or "location" not in response.headers:
                        raise CleanupRefused("fetch_redirect_limit")
                    url = urljoin(url, response.headers["location"])
                    continue
                if response.status_code != 200:
                    raise CleanupRefused("fetch_status_refused")
                if response.headers.get("content-encoding", "identity").lower() not in (
                    "",
                    "identity",
                ):
                    raise CleanupRefused("fetch_compression_refused")
                media = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if media not in ("text/plain", "text/html"):
                    raise CleanupRefused("fetch_content_type_refused")
                raw = bytearray()
                for chunk in response.iter_bytes():
                    if len(raw) + len(chunk) > MAX_PAGE_BYTES:
                        raise CleanupRefused("fetch_body_too_large")
                    raw.extend(chunk)
                    if clock.monotonic() >= deadline:
                        raise CleanupRefused("fetch_deadline")
                try:
                    text = bytes(raw).decode("utf-8", errors="strict")
                except UnicodeError:
                    raise CleanupRefused("fetch_encoding_refused") from None
                if media == "text/html":
                    parser = _Text()
                    parser.feed(text)
                    parser.close()
                    text = " ".join("".join(parser.parts).split())
                if not text.strip() or "\x00" in text:
                    raise CleanupRefused("fetch_empty_or_binary")
                return Page(url, text)
    raise CleanupRefused("fetch_redirect_limit")


def fetch_page(
    url: str, *, hosts: frozenset[str], resolver=system_resolver, transport=None, guard=None
) -> Page:
    """Fetch one page. A guard factory holds owner authority in the actual network worker."""
    _url(url)
    if not hosts or not isinstance(hosts, frozenset):
        raise CleanupRefused("fetch_allowlist_missing")
    if not _SLOTS.acquire(blocking=False):
        raise CleanupRefused("fetch_busy")
    result = []
    deadline = clock.monotonic() + FETCH_SECONDS

    def work():
        try:
            with guard() if guard is not None else nullcontext():
                result.append(_read(url, hosts, resolver, transport, deadline))
        except Exception as error:
            result.append(error)
        finally:
            _SLOTS.release()

    thread = threading.Thread(target=work, name="grove-curator-fetch", daemon=True)
    try:
        thread.start()
    except RuntimeError:
        _SLOTS.release()
        raise CleanupRefused("fetch_busy") from None
    thread.join(FETCH_SECONDS)
    if not result:
        raise CleanupRefused("fetch_deadline")
    if isinstance(result[0], Exception):
        # Network exceptions can contain a URL. Never return those values to a caller.
        if isinstance(result[0], CleanupRefused):
            raise result[0]
        raise CleanupRefused("fetch_transport_refused") from None
    return result[0]
