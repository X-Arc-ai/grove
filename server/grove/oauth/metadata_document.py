"""Client ID Metadata Documents: a client_id that is an https URL with a path names a JSON document
that describes the client. docs/hosts.md.

The fetch is the one request Grove makes to a URL a stranger chose, so it keeps to these rules:
https only, port 443 unless the URL names one, no user part, no dot segment, a host that resolves
only to global addresses, a connection to the address that was checked with the original host as
SNI and as Host (no second DNS lookup, so a rebinding answer cannot move it), no proxy, no redirect,
and at most 5 KiB. The document must be strict JSON: an object whose client_id is the URL exactly,
with a client_name and valid redirect_uris, no secret, and no token_endpoint_auth_method other than
"none".

One fetch_document call takes at most FETCH_TIMEOUT_SECONDS in all: the lookup, every address, the
headers and the body. The work runs in a daemon thread, because neither getaddrinfo nor a per-read
timeout gives a total limit, and the caller is a thread of the API's pool that /token and /mcp need
too. The caller waits for at most the limit, then stops the worker. At most
MAX_CONCURRENT_DOCUMENT_FETCHES workers run in the process: over that the call refuses at once, so
no request queues on a slow host.

A document's copy is the client row (register_client with registration_type metadata_document). It
serves for an hour; after that the next request fetches it again, and when that fetch fails a copy
fetched in the last 24 hours still serves. A URL with no row takes a slot of the registration window
before it is stored, and a refetch takes none. The resolver and the transport are injectable: tests
use a fake resolver and httpx.MockTransport, never the network.
"""

import ipaddress
import socket
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit

import httpx

from grove import clock
from grove.oauth import METADATA_DOCUMENT_CACHE, METADATA_DOCUMENT_STALE_LIMIT
from grove.oauth.registration import InvalidMetadata, check_redirect_uris, clean_client_name
from grove.oauth.store import ClientRow, find_client
from grove.store.connection import Driver
from grove.strict_json import loads_strict
from grove.writer.oauth import METADATA_DOCUMENT, PUBLIC_CLIENT, register_client

MAX_DOCUMENT_BYTES = 5 * 1024
# The total for one fetch_document call: the lookup, every address, the headers and the body.
FETCH_TIMEOUT_SECONDS = 5.0
MAX_CONCURRENT_DOCUMENT_FETCHES = 4
HTTPS_PORT = 443
USER_AGENT = "Grove (client metadata)"
# NAT64's well-known prefix (RFC 6052) carries an IPv4 address in its last 32 bits.
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
# IPv6 forms that carry an address to another network, or that no global host uses: the deprecated
# IPv4-compatible form, the IPv4-translated form of SIIT (RFC 2765), and site-local. Python calls
# them global.
_NOT_GLOBAL_V6 = tuple(
    ipaddress.ip_network(network) for network in ("::/96", "::ffff:0:0:0/96", "fec0::/10")
)
# An ISATAP interface id (RFC 5214): 0000:5efe or 0200:5efe, then an IPv4 address.
_ISATAP_ID = 0x00005EFE
_ISATAP_MASK = 0xFDFFFFFF

_fetch_slots = threading.BoundedSemaphore(MAX_CONCURRENT_DOCUMENT_FETCHES)

Resolver = Callable[[str, int], Sequence[str]]
Address = ipaddress.IPv4Address | ipaddress.IPv6Address


class RegistrationSlots(Protocol):
    """The window of POST /register, which new document rows share (grove.api.auth.CodeWindow)."""

    def take(self) -> int | None: ...

    def give_back(self) -> None: ...


class DocumentRefused(Exception):
    """The document could not be fetched, or it does not describe a client Grove accepts. The
    message names the rule, never a byte of the answer."""


@dataclass(frozen=True)
class ClientDocument:
    client_id: str
    client_name: str
    redirect_uris: tuple[str, ...]


@dataclass(frozen=True)
class _Target:
    host: str
    port: int
    host_header: str
    path: str


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


def _is_isatap(address: ipaddress.IPv6Address) -> bool:
    return (int(address) >> 32) & _ISATAP_MASK == _ISATAP_ID


def global_address(text: object) -> Address | None:
    """The address when it is global unicast and carries no address that is not; else None."""
    if not isinstance(text, str):
        return None
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        # A zone id (fe80::1%en0) lands here too: only a link-local address has one.
        return None
    if isinstance(address, ipaddress.IPv6Address):
        if _is_isatap(address) or any(address in network for network in _NOT_GLOBAL_V6):
            return None
    for each in (address, *_embedded(address)):
        if not each.is_global or each.is_multicast:
            return None
    return address


def _target(url: str) -> _Target:
    """Where a document URL points, or DocumentRefused."""
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
    # A server or a proxy may read %2e as a dot, so an encoded dot segment counts as one.
    if any(unquote(segment) in (".", "..") for segment in parts.path.split("/")):
        raise DocumentRefused("a metadata document URL has no dot segment")
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    return _Target(parts.hostname, port, parts.netloc, path)


class _Abort:
    """How a caller that stopped waiting stops its worker.

    A close of the httpx client from another thread does not wake a read that is blocked on the
    socket: the read sleeps until its own timeout. So the worker keeps a second descriptor of its
    socket, and the abort shuts the connection down through it, which wakes a read, a write or a
    TLS handshake at once. The worker then closes its own client: a close from the caller's thread
    could free a descriptor number that the worker's TLS read still holds. With no socket yet (the
    lookup, the connect, or a transport of a test) the abort closes the client itself, and the
    worker stops before the next address.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._fired = False
        self._client: httpx.Client | None = None
        self._connection: socket.socket | None = None

    @property
    def fired(self) -> bool:
        with self._lock:
            return self._fired

    def fire(self) -> None:
        with self._lock:
            self._fired = True
            if self._connection is not None:
                _shut_down(self._connection)
                return
            client = self._client
        if client is not None:
            client.close()

    def attach(self, client: httpx.Client) -> None:
        with self._lock:
            self._client = client
            if self._fired:
                client.close()

    def detach(self) -> None:
        with self._lock:
            self._client = None
            connection, self._connection = self._connection, None
        if connection is not None:
            connection.close()

    def trace(self, name: str, info: Mapping[str, Any]) -> None:
        """httpx's trace extension. Runs on the worker's thread and never raises into httpcore."""
        if name != "connection.connect_tcp.complete":
            return
        opened = info["return_value"].get_extra_info("socket")
        if opened is None:
            return
        try:
            second = opened.dup()
        except OSError:
            return
        with self._lock:
            self._connection = second
            if self._fired:
                _shut_down(second)


def _shut_down(connection: socket.socket) -> None:
    try:
        connection.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass  # The peer has closed it already.


def _read_capped(response: httpx.Response, abort: _Abort) -> bytes:
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
        if abort.fired:
            raise DocumentRefused("the metadata document took too long")
    return bytes(body)


def _fetch_from(
    address: Address,
    target: _Target,
    *,
    transport: httpx.BaseTransport | None,
    abort: _Abort,
    timeout: float,
) -> bytes:
    literal = f"[{address}]" if address.version == 6 else str(address)
    # trust_env=False: a proxy from the environment would resolve the name again on its own.
    with httpx.Client(
        transport=transport,
        timeout=httpx.Timeout(timeout),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        abort.attach(client)
        try:
            request = client.build_request(
                "GET",
                f"https://{literal}:{target.port}{target.path}",
                headers={
                    "Host": target.host_header,
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                    "User-Agent": USER_AGENT,
                },
                extensions={"sni_hostname": target.host, "trace": abort.trace},
            )
            response = client.send(request, stream=True)
            try:
                return _read_capped(response, abort)
            finally:
                response.close()
        finally:
            abort.detach()


def parse_document(data: object, url: str) -> ClientDocument:
    """The client a fetched document describes, or DocumentRefused."""
    if not isinstance(data, dict):
        raise DocumentRefused("the metadata document must be a JSON object")
    if data.get("client_id") != url:
        raise DocumentRefused("the document's client_id must equal its URL exactly")
    if "client_secret" in data or "client_secret_expires_at" in data:
        raise DocumentRefused("a metadata document holds no client_secret")
    if data.get("token_endpoint_auth_method", PUBLIC_CLIENT) != PUBLIC_CLIENT:
        raise DocumentRefused('a metadata document client uses token_endpoint_auth_method "none"')
    try:
        redirect_uris = check_redirect_uris(data.get("redirect_uris"))
        client_name = clean_client_name(data.get("client_name"))
    except InvalidMetadata as refused:
        raise DocumentRefused(refused.description) from None
    if client_name is None:
        raise DocumentRefused("a metadata document needs a client_name")
    return ClientDocument(client_id=url, client_name=client_name, redirect_uris=redirect_uris)


def _read_document(
    target: _Target,
    *,
    resolver: Resolver,
    transport: httpx.BaseTransport | None,
    abort: _Abort,
    deadline: float,
) -> bytes:
    """The worker's job: the bytes of the document, from the first address that answers."""
    try:
        resolved = list(dict.fromkeys(resolver(target.host, target.port)))
    # A host with an empty label or a label over 63 characters is a UnicodeError in getaddrinfo.
    except (OSError, UnicodeError):
        raise DocumentRefused("the metadata document's host does not resolve") from None
    if not resolved:
        raise DocumentRefused("the metadata document's host does not resolve")
    addresses = [global_address(text) for text in resolved]
    if any(address is None for address in addresses):
        raise DocumentRefused("the metadata document's host has an address that is not global")
    for address in addresses:
        assert address is not None
        left = deadline - clock.monotonic()
        if abort.fired or left <= 0:
            raise DocumentRefused("the metadata document took too long")
        try:
            return _fetch_from(address, target, transport=transport, abort=abort, timeout=left)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            continue
        except (httpx.HTTPError, httpx.InvalidURL):
            raise DocumentRefused("the metadata document could not be read") from None
    raise DocumentRefused("the metadata document's host cannot be reached")


def _read_within_limit(
    target: _Target, *, resolver: Resolver, transport: httpx.BaseTransport | None
) -> bytes:
    """Runs _read_document in a worker and waits for at most FETCH_TIMEOUT_SECONDS. The worker
    holds a slot until it ends, also after the caller has left."""
    slots = _fetch_slots
    if not slots.acquire(blocking=False):
        raise DocumentRefused("too many metadata documents are being fetched")
    limit = FETCH_TIMEOUT_SECONDS
    abort = _Abort()
    outcome: list[bytes | Exception] = []

    def work() -> None:
        try:
            outcome.append(
                _read_document(
                    target,
                    resolver=resolver,
                    transport=transport,
                    abort=abort,
                    deadline=clock.monotonic() + limit,
                )
            )
        except Exception as failed:
            outcome.append(failed)
        finally:
            slots.release()

    worker = threading.Thread(target=work, name="grove-document-fetch", daemon=True)
    try:
        worker.start()
    except RuntimeError:
        slots.release()
        raise DocumentRefused("too many metadata documents are being fetched") from None
    worker.join(limit)
    if not outcome:
        abort.fire()
        raise DocumentRefused("the metadata document took too long")
    if isinstance(outcome[0], Exception):
        raise outcome[0]
    return outcome[0]


def fetch_document(
    url: str, *, resolver: Resolver = system_resolver, transport: httpx.BaseTransport | None = None
) -> ClientDocument:
    """Fetch and check the document at url under the rules above."""
    target = _target(url)
    raw = _read_within_limit(target, resolver=resolver, transport=transport)
    try:
        data = loads_strict(raw)
    except ValueError:
        raise DocumentRefused("the metadata document is not strict JSON") from None
    return parse_document(data, url)


def document_client(
    driver: Driver,
    client_id: str,
    *,
    now: datetime,
    window: RegistrationSlots,
    resolver: Resolver = system_resolver,
    transport: httpx.BaseTransport | None = None,
) -> ClientRow | None:
    """The client row of a document client_id: the cached copy while it is fresh, else a new fetch
    stored through register_client. None when the fetch fails and no copy of the last 24 hours is
    left, when the id belongs to a client of another type, or when the URL has no row and the
    registration window is full."""
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
    is_new = row is None
    if is_new and window.take() is not None:
        return None
    try:
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
    except Exception:
        # A registration that stored nothing spends no slot.
        if is_new:
            window.give_back()
        raise
    return find_client(driver, client_id)
