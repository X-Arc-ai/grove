"""Client ID Metadata Documents: a client_id that is an https URL with a path names a JSON document
that describes the client. docs/hosts.md.

The fetch is the one request Grove makes to a URL a stranger chose, so it keeps to these rules:
https only, port 443 unless the URL names one, no user part, a host that resolves only to global
addresses, a connection to the address that was checked with the original host as SNI and as Host
(no second DNS lookup, so a rebinding answer cannot move it), no proxy, no redirect, 5 seconds, and
at most 5 KiB. The document must be a JSON object whose client_id is the URL exactly, with valid
redirect_uris, no secret, and no token_endpoint_auth_method other than "none".

A document's copy is the client row (register_client with registration_type metadata_document). It
serves for an hour; after that the next request fetches it again, and when that fetch fails a copy
fetched in the last 24 hours still serves. The resolver and the transport are injectable: tests use
a fake resolver and httpx.MockTransport, never the network.
"""

import ipaddress
import json
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

import httpx

from grove import clock
from grove.oauth import METADATA_DOCUMENT_CACHE, METADATA_DOCUMENT_STALE_LIMIT
from grove.oauth.registration import InvalidMetadata, check_redirect_uris, clean_client_name
from grove.oauth.store import ClientRow, find_client
from grove.store.connection import Driver
from grove.writer.oauth import METADATA_DOCUMENT, PUBLIC_CLIENT, register_client

MAX_DOCUMENT_BYTES = 5 * 1024
FETCH_TIMEOUT_SECONDS = 5.0
HTTPS_PORT = 443
USER_AGENT = "Grove (client metadata)"
# NAT64's well-known prefix (RFC 6052) carries an IPv4 address in its last 32 bits.
_NAT64 = ipaddress.ip_network("64:ff9b::/96")

Resolver = Callable[[str, int], Sequence[str]]
Address = ipaddress.IPv4Address | ipaddress.IPv6Address


class DocumentRefused(Exception):
    """The document could not be fetched, or it does not describe a client Grove accepts. The
    message names the rule, never a byte of the answer."""


@dataclass(frozen=True)
class ClientDocument:
    client_id: str
    client_name: str | None
    redirect_uris: tuple[str, ...]


def is_document_client_id(client_id: object) -> bool:
    """An https URL with a path other than `/`: a client that names its own metadata document."""
    if not isinstance(client_id, str) or not client_id.startswith("https://"):
        return False
    try:
        parts = urlsplit(client_id)
    except ValueError:
        return False
    return parts.scheme == "https" and bool(parts.netloc) and parts.path not in ("", "/")


def system_resolver(host: str, port: int) -> list[str]:
    """Every address the system resolver gives for the host, each once."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def _embedded(address: Address) -> list[Address]:
    """The IPv4 addresses an IPv6 address carries to another network."""
    if not isinstance(address, ipaddress.IPv6Address):
        return []
    carried: list[Address] = []
    if address.ipv4_mapped is not None:
        carried.append(address.ipv4_mapped)
    if address.sixtofour is not None:
        carried.append(address.sixtofour)
    if address.teredo is not None:
        carried.extend(address.teredo)
    if address in _NAT64:
        carried.append(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF))
    return carried


def global_address(text: object) -> Address | None:
    """The address when it is global unicast and carries no address that is not; else None."""
    if not isinstance(text, str):
        return None
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        # A zone id (fe80::1%en0) lands here too: only a link-local address has one.
        return None
    for each in (address, *_embedded(address)):
        if not each.is_global or each.is_multicast:
            return None
    return address


def _target(url: str) -> tuple[str, int, str, str]:
    """(host, port, Host header, path and query) of a document URL, or DocumentRefused."""
    try:
        parts = urlsplit(url)
        port = parts.port or HTTPS_PORT
    except ValueError:
        raise DocumentRefused("the client_id is not a valid URL") from None
    if parts.scheme != "https" or not parts.hostname:
        raise DocumentRefused("a metadata document is fetched over https only")
    if parts.username is not None or parts.password is not None:
        raise DocumentRefused("a metadata document URL has no user part")
    if "#" in url:
        raise DocumentRefused("a metadata document URL has no fragment")
    if not url.isascii() or not url.isprintable() or " " in url:
        raise DocumentRefused("a metadata document URL holds printable ASCII only")
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    return parts.hostname, port, parts.netloc, path


def _read_capped(response: httpx.Response, deadline: float) -> bytes:
    if response.status_code != 200:
        raise DocumentRefused("the metadata document did not answer 200")
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in ("", "identity"):
        raise DocumentRefused("the metadata document must not be compressed")
    declared = response.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_DOCUMENT_BYTES:
        raise DocumentRefused(f"the metadata document is over {MAX_DOCUMENT_BYTES} bytes")
    body = bytearray()
    # Identity only (checked above), so these are the bytes as sent.
    for chunk in response.iter_bytes():
        body += chunk
        if len(body) > MAX_DOCUMENT_BYTES:
            raise DocumentRefused(f"the metadata document is over {MAX_DOCUMENT_BYTES} bytes")
        if clock.monotonic() > deadline:
            raise DocumentRefused("the metadata document took too long")
    return bytes(body)


def _fetch_from(
    address: Address,
    *,
    host: str,
    port: int,
    host_header: str,
    path: str,
    transport: httpx.BaseTransport | None,
) -> bytes:
    literal = f"[{address}]" if address.version == 6 else str(address)
    deadline = clock.monotonic() + FETCH_TIMEOUT_SECONDS
    # trust_env=False: a proxy from the environment would resolve the name again on its own.
    with httpx.Client(
        transport=transport,
        timeout=httpx.Timeout(FETCH_TIMEOUT_SECONDS),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        request = client.build_request(
            "GET",
            f"https://{literal}:{port}{path}",
            headers={
                "Host": host_header,
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "User-Agent": USER_AGENT,
            },
            extensions={"sni_hostname": host},
        )
        response = client.send(request, stream=True)
        try:
            return _read_capped(response, deadline)
        finally:
            response.close()


def _refuse_constant(name: str) -> None:
    raise ValueError(f"{name} is not a number JSON allows")


def parse_document(data: object, url: str) -> ClientDocument:
    """The client a fetched document describes, or DocumentRefused."""
    if not isinstance(data, dict):
        raise DocumentRefused("the metadata document must be a JSON object")
    if data.get("client_id") != url:
        raise DocumentRefused("the document's client_id must equal its URL exactly")
    if "client_secret" in data:
        raise DocumentRefused("a metadata document holds no client_secret")
    if data.get("token_endpoint_auth_method", PUBLIC_CLIENT) != PUBLIC_CLIENT:
        raise DocumentRefused('a metadata document client uses token_endpoint_auth_method "none"')
    try:
        redirect_uris = check_redirect_uris(data.get("redirect_uris"))
        client_name = clean_client_name(data.get("client_name"))
    except InvalidMetadata as refused:
        raise DocumentRefused(refused.description) from None
    return ClientDocument(client_id=url, client_name=client_name, redirect_uris=redirect_uris)


def fetch_document(
    url: str, *, resolver: Resolver = system_resolver, transport: httpx.BaseTransport | None = None
) -> ClientDocument:
    """Fetch and check the document at url under the rules above."""
    host, port, host_header, path = _target(url)
    try:
        resolved = list(dict.fromkeys(resolver(host, port)))
    except OSError:
        raise DocumentRefused("the metadata document's host does not resolve") from None
    if not resolved:
        raise DocumentRefused("the metadata document's host does not resolve")
    addresses = [global_address(text) for text in resolved]
    if any(address is None for address in addresses):
        raise DocumentRefused("the metadata document's host has an address that is not global")
    for address in addresses:
        assert address is not None
        try:
            raw = _fetch_from(
                address,
                host=host,
                port=port,
                host_header=host_header,
                path=path,
                transport=transport,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout):
            continue
        except httpx.HTTPError:
            raise DocumentRefused("the metadata document could not be read") from None
        try:
            data = json.loads(raw, parse_constant=_refuse_constant)
        except (ValueError, RecursionError):
            raise DocumentRefused("the metadata document is not JSON") from None
        return parse_document(data, url)
    raise DocumentRefused("the metadata document's host cannot be reached")


def document_client(
    driver: Driver,
    client_id: str,
    *,
    now: datetime,
    resolver: Resolver = system_resolver,
    transport: httpx.BaseTransport | None = None,
) -> ClientRow | None:
    """The client row of a document client_id: the cached copy while it is fresh, else a new fetch
    stored through register_client. None when the fetch fails and no copy of the last 24 hours is
    left, or when the id belongs to a client of another type."""
    row = find_client(driver, client_id)
    if row is not None and row.registration_type != METADATA_DOCUMENT:
        return None
    fetched = row.fetched_at if row is not None else None
    if row is not None and fetched is not None and fetched > now - METADATA_DOCUMENT_CACHE:
        return row
    try:
        document = fetch_document(client_id, resolver=resolver, transport=transport)
    except DocumentRefused:
        stale_ok = fetched is not None and fetched > now - METADATA_DOCUMENT_STALE_LIMIT
        return row if row is not None and stale_ok else None
    register_client(
        driver,
        client_id=document.client_id,
        host=document.client_name,
        redirect_uris=list(document.redirect_uris),
        registration_type=METADATA_DOCUMENT,
        auth_method=PUBLIC_CLIENT,
        secret_hash=None,
        at=now,
    )
    return find_client(driver, client_id)
