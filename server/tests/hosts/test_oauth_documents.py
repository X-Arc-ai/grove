"""Client ID Metadata Documents: the fetch rules against a fake resolver and httpx.MockTransport
(the network never; a loopback socket for the real read), the total limit and the slots of a fetch,
and the copy in the store that serves for an hour and, when a fetch fails, for 24 hours."""

import socket
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest

from grove import clock
from grove.api.auth import CodeWindow
from grove.oauth import metadata_document
from grove.oauth.metadata_document import (
    MAX_CONCURRENT_DOCUMENT_FETCHES,
    MAX_DOCUMENT_BYTES,
    DocumentRefused,
    document_client,
    fetch_document,
    global_address,
    is_document_client_id,
    system_resolver,
)
from grove.oauth.store import find_client
from tests.hosts.conftest import LOOPBACK_REDIRECT
from tests.hosts.oauth_kit import document

PUBLIC_V4 = "93.184.215.14"
PUBLIC_V6 = "2606:2800:21f:cb07:6820:80da:af6b:8b2c"
# A total limit that a slow host passes at once, and what a test accepts as "about that limit".
SMALL_LIMIT = 0.3
WITHIN_LIMIT = 1.5
# How long the slow hosts of these tests stay slow when nothing stops them.
SLOW_HOST_SECONDS = 4.0


def doc_url() -> str:
    return f"https://lumen.example/clients/{uuid4()}.json"


@dataclass
class FakeHost:
    """The resolver and the server of a document host. `answer` builds each response."""

    addresses: list[str] = field(default_factory=lambda: [PUBLIC_V4])
    answer: object = None
    asked: list[tuple[str, int]] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)
    fail: bool = False

    def resolve(self, host: str, port: int) -> list[str]:
        self.asked.append((host, port))
        if self.fail:
            raise OSError("no such host")
        return list(self.addresses)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if callable(self.answer):
            return self.answer(request)
        raise AssertionError("no answer set")

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def fetch(self, url: str):
        return fetch_document(url, resolver=self.resolve, transport=self.transport)

    def client(self, driver, url: str, now, window: CodeWindow | None = None):
        """The store's copy through document_client, fetched from this host when it must be."""
        options = {"resolver": self.resolve, "transport": self.transport}
        window = window or CodeWindow(limit=10, seconds=3600.0)
        return document_client(driver, url, now=now, window=window, **options)


def serving(body: bytes, status: int = 200, headers: dict | None = None):
    return lambda request: httpx.Response(status, content=body, headers=headers or {})


# What counts as a document client, and as a global address -----------------------------------


@pytest.mark.parametrize(
    ("client_id", "is_document"),
    [
        ("https://lumen.example/client.json", True),
        ("https://lumen.example/a/b?v=1", True),
        ("https://lumen.example/", False),
        ("https://lumen.example", False),
        ("http://lumen.example/client.json", False),
        ("0194f0a2-7c3e-7d1a-9b2c-3d4e5f607182", False),
        (None, False),
    ],
)
def test_a_document_client_id_is_an_https_url_with_a_path(client_id, is_document):
    assert is_document_client_id(client_id) is is_document


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.8",
        "172.16.4.4",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",
        "224.0.0.1",
        "255.255.255.255",
        "::1",
        "fe80::1",
        "fe80::1%en0",
        "fc00::1",
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
        "2002:7f00:1::1",
        "64:ff9b::7f00:1",
        "ff02::1",
        "2001:db8::1",
        "not an address",
        # IPv4-compatible (::/96), with a public and with an inner address.
        "::93.184.215.14",
        "::127.0.0.1",
        # IPv4-translated (::ffff:0:0:0/96).
        "::ffff:0:93.184.215.14",
        "::ffff:0:127.0.0.1",
        # ISATAP, on a public prefix, both forms of the interface id.
        "2606:2800:21f:cb07:0:5efe:7f00:1",
        "2606:2800:21f:cb07:200:5efe:7f00:1",
        "2606:2800:21f:cb07:0:5efe:5db8:d70e",
        # Site-local (fec0::/10).
        "fec0::1",
        "feff::1",
    ],
)
def test_an_address_that_is_not_global_is_refused(address):
    assert global_address(address) is None


def test_global_addresses_pass():
    assert str(global_address(PUBLIC_V4)) == PUBLIC_V4
    assert global_address(PUBLIC_V6) is not None
    assert global_address("::ffff:93.184.215.14") is not None


# The fetch -------------------------------------------------------------------------------------


def test_the_fetch_goes_to_the_checked_address_with_the_host_as_sni_and_host():
    url = "https://lumen.example/clients/desk.json?v=2"
    host = FakeHost(answer=serving(document(url)))
    found = host.fetch(url)
    assert found.client_id == url
    assert found.client_name == "Lumen Desk"
    assert found.redirect_uris == (LOOPBACK_REDIRECT,)
    assert host.asked == [("lumen.example", 443)]
    (request,) = host.requests
    # httpx leaves the default port out of the URL: 443, on the checked address.
    assert request.url == httpx.URL(f"https://{PUBLIC_V4}/clients/desk.json?v=2")
    assert request.headers["host"] == "lumen.example"
    assert request.extensions["sni_hostname"] == "lumen.example"
    assert request.headers["accept-encoding"] == "identity"


def test_a_port_in_the_url_is_the_port_resolved_and_asked():
    url = "https://lumen.example:8443/desk.json"
    host = FakeHost(addresses=[PUBLIC_V6], answer=serving(document(url)))
    host.fetch(url)
    assert host.asked == [("lumen.example", 8443)]
    (request,) = host.requests
    assert request.url.host == PUBLIC_V6 and request.url.port == 8443
    assert request.headers["host"] == "lumen.example:8443"


def test_an_address_that_refuses_the_connection_gives_way_to_the_next():
    url = doc_url()
    tried = []

    def answer(request):
        tried.append(request.url.host)
        if len(tried) == 1:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, content=document(url))

    host = FakeHost(addresses=[PUBLIC_V6, PUBLIC_V4], answer=answer)
    assert host.fetch(url).client_id == url
    assert tried == [PUBLIC_V6, PUBLIC_V4]


@pytest.mark.parametrize(
    "addresses",
    [["127.0.0.1"], ["10.1.2.3"], [PUBLIC_V4, "192.168.0.9"], ["169.254.169.254"], []],
)
def test_a_host_with_any_address_that_is_not_global_is_never_asked(addresses):
    host = FakeHost(addresses=addresses, answer=serving(b"{}"))
    with pytest.raises(DocumentRefused):
        host.fetch(doc_url())
    assert host.requests == []


@pytest.mark.parametrize(
    "url",
    [
        "http://lumen.example/desk.json",
        "https://user@lumen.example/desk.json",
        "https://user:pw@lumen.example/desk.json",
        "https://lumen.example/desk.json#top",
        "https://lumen.example:99999/desk.json",
        "https://lumen.example/./desk.json",
        "https://lumen.example/a/../desk.json",
        "https://lumen.example/a/..",
        "https://lumen.example/a/.",
        "https://lumen.example/%2e%2e/desk.json",
        "https://lumen.example/%2E/desk.json",
        "https://lumen.example/.%2e/desk.json",
    ],
)
def test_a_url_outside_the_rules_is_refused_before_any_lookup(url):
    host = FakeHost(answer=serving(b"{}"))
    with pytest.raises(DocumentRefused):
        host.fetch(url)
    assert host.asked == [] and host.requests == []


def test_a_host_that_does_not_resolve_is_refused():
    host = FakeHost(fail=True)
    with pytest.raises(DocumentRefused):
        host.fetch(doc_url())


def test_dots_inside_a_name_are_no_dot_segment():
    url = "https://lumen.example/.well-known/a..b/desk.v2.json"
    assert FakeHost(answer=serving(document(url))).fetch(url).client_id == url


@pytest.mark.parametrize(
    "url",
    ["https://a..b/x", "https://.a/x", f"https://{'a' * 64}.example/x"],
    ids=["empty label", "empty first label", "label over 63 characters"],
)
def test_a_host_that_getaddrinfo_cannot_encode_does_not_resolve(url):
    with pytest.raises(UnicodeError):
        system_resolver(url.split("/")[2], 443)
    with pytest.raises(DocumentRefused, match="does not resolve"):
        fetch_document(url, transport=httpx.MockTransport(lambda request: httpx.Response(200)))


def test_a_redirect_is_refused_and_never_followed():
    host = FakeHost(answer=serving(b"", 302, {"location": "https://127.0.0.1/inner.json"}))
    with pytest.raises(DocumentRefused):
        host.fetch(doc_url())
    assert len(host.requests) == 1


@pytest.mark.parametrize("status", [201, 204, 304, 404, 500])
def test_an_answer_other_than_200_is_refused(status):
    url = doc_url()
    host = FakeHost(answer=serving(document(url), status))
    with pytest.raises(DocumentRefused):
        host.fetch(url)


def test_a_body_over_5_kib_is_refused_whether_or_not_it_says_its_length():
    url = doc_url()
    big = document(url, client_name="x" * MAX_DOCUMENT_BYTES)
    assert len(big) > MAX_DOCUMENT_BYTES
    with pytest.raises(DocumentRefused):
        FakeHost(answer=serving(big)).fetch(url)

    def streamed(request):
        # An iterator is sent in chunks with no Content-Length.
        return httpx.Response(200, content=iter([big[:4000], big[4000:]]))

    with pytest.raises(DocumentRefused):
        FakeHost(answer=streamed).fetch(url)
    fits = document(url, client_name="x" * 100)
    assert FakeHost(answer=serving(fits)).fetch(url).client_name == "x" * 100


def test_a_compressed_body_is_refused():
    url = doc_url()
    host = FakeHost(answer=serving(document(url), headers={"content-encoding": "gzip"}))
    with pytest.raises(DocumentRefused):
        host.fetch(url)


@pytest.mark.parametrize(
    "body",
    [
        lambda url: b"not json",
        lambda url: b"[1, 2]",
        lambda url: b'{"client_id": NaN}',
        lambda url: document(url + "x"),
        lambda url: document(url.upper()),
        lambda url: document(url, client_secret="gvs_" + "s" * 43),
        lambda url: document(url, client_secret_expires_at=0),
        lambda url: document(url, token_endpoint_auth_method="client_secret_basic"),
        lambda url: document(url, client_name=None),
        lambda url: document(url, client_name=""),
        lambda url: document(url, client_name="  \u200b\x07 "),
        lambda url: document(url).replace(b'"Lumen Desk"', b'"Lumen \\ud800 Desk"'),
        lambda url: document(url).replace(b'"Lumen Desk"', b'"Lumen", "x": 1e999'),
        lambda url: document(url).decode().encode("utf-16"),
        lambda url: document(url, redirect_uris=["http://lumen.example/cb"]),
        lambda url: document(url, redirect_uris=[]),
        lambda url: document(url, redirect_uris=None),
        lambda url: document(url, client_name=["Lumen"]),
    ],
)
def test_a_document_that_is_not_a_valid_public_client_is_refused(body):
    url = doc_url()
    with pytest.raises(DocumentRefused) as refused:
        FakeHost(answer=serving(body(url))).fetch(url)
    assert "lumen.example" not in str(refused.value)


def test_a_document_may_say_none_as_its_method_and_its_name_is_cleaned():
    url = doc_url()
    body = document(url, token_endpoint_auth_method="none", client_name="Lumen‮ Desk")
    assert FakeHost(answer=serving(body)).fetch(url).client_name == "Lumen Desk"


# The total limit of one fetch ------------------------------------------------------------------


@pytest.fixture
def small_limit(monkeypatch):
    """A small total limit, and slots that belong to this test: a held worker of another test takes
    none of them."""
    monkeypatch.setattr(metadata_document, "FETCH_TIMEOUT_SECONDS", SMALL_LIMIT)
    slots = threading.BoundedSemaphore(MAX_CONCURRENT_DOCUMENT_FETCHES)
    monkeypatch.setattr(metadata_document, "_fetch_slots", slots, raising=False)
    return slots


class HeldTransport(httpx.BaseTransport):
    """A host that holds every request until `release`, and then answers `after`."""

    def __init__(self, after: Callable[[httpx.Request], httpx.Response]) -> None:
        self.release = threading.Event()
        self.after = after
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.release.wait(SLOW_HOST_SECONDS)
        return self.after(request)


def _timed_refusal(url: str, **options) -> float:
    start = clock.monotonic()
    with pytest.raises(DocumentRefused):
        fetch_document(url, **options)
    return clock.monotonic() - start


def _public(host: str, port: int) -> list[str]:
    return [PUBLIC_V4]


def _wait_for_every_slot(slots: threading.BoundedSemaphore, seconds: float) -> float:
    """How long it took until every worker had ended and given its slot back, or a failure after
    `seconds`. One free slot proves nothing while the others are free: it takes all of them, and
    then gives them back."""
    start = clock.monotonic()
    taken = 0
    try:
        for _ in range(MAX_CONCURRENT_DOCUMENT_FETCHES):
            left = max(0.0, seconds - (clock.monotonic() - start))
            assert slots.acquire(timeout=left), "a worker kept its slot"
            taken += 1
    finally:
        for _ in range(taken):
            slots.release()
    return clock.monotonic() - start


def test_a_transport_that_blocks_past_the_limit_is_refused_within_about_the_limit(small_limit):
    url = doc_url()
    held = HeldTransport(lambda request: httpx.Response(200, content=document(url)))
    try:
        assert _timed_refusal(url, resolver=_public, transport=held) < WITHIN_LIMIT
    finally:
        held.release.set()


def test_a_slow_lookup_is_refused_within_about_the_limit_and_asks_no_host(small_limit):
    url = doc_url()
    answered = threading.Event()
    done = threading.Event()
    seen: list[httpx.Request] = []

    def slow_resolver(host: str, port: int) -> list[str]:
        answered.wait(SLOW_HOST_SECONDS)
        done.set()
        return [PUBLIC_V4]

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=document(url))

    transport = httpx.MockTransport(handle)
    try:
        assert _timed_refusal(url, resolver=slow_resolver, transport=transport) < WITHIN_LIMIT
    finally:
        answered.set()
    assert done.wait(WITHIN_LIMIT)
    assert _wait_for_every_slot(small_limit, WITHIN_LIMIT) < WITHIN_LIMIT
    assert seen == [], "the worker stops before its first address"


def test_a_drip_of_body_chunks_is_refused_within_about_the_limit(small_limit):
    url = doc_url()
    stop = threading.Event()

    def drip() -> Iterator[bytes]:
        for _ in range(int(SLOW_HOST_SECONDS / 0.05)):
            if stop.wait(0.05):
                return
            yield b" "

    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=drip()))
    try:
        assert _timed_refusal(url, resolver=_public, transport=transport) < WITHIN_LIMIT
        assert _wait_for_every_slot(small_limit, WITHIN_LIMIT) < WITHIN_LIMIT
    finally:
        stop.set()


class Dripper:
    """A real socket on loopback that sends the start of an answer and then one header byte at a
    time, each gap shorter than any read timeout. It stops by itself after SLOW_HOST_SECONDS."""

    GAP = 0.05

    def __init__(self) -> None:
        self.stop = threading.Event()
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        connection, _ = self.server.accept()
        with connection:
            connection.recv(65536)
            try:
                connection.sendall(b"HTTP/1.1 200 OK\r\nX-Slow: ")
                for _ in range(int(SLOW_HOST_SECONDS / self.GAP)):
                    if self.stop.wait(self.GAP):
                        return
                    connection.sendall(b"a")
            except OSError:
                return

    def close(self) -> None:
        self.stop.set()
        self.server.close()


class ToLoopback(httpx.BaseTransport):
    """Sends a fetch over plain http to the dripper with httpx's real transport, so the read that
    blocks is a read on a real socket."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.real = httpx.HTTPTransport(trust_env=False)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        request.url = request.url.copy_with(scheme="http", host="127.0.0.1", port=self.port)
        return self.real.handle_request(request)

    def close(self) -> None:
        self.real.close()


def test_a_drip_of_headers_is_refused_within_about_the_limit_and_the_read_ends_at_once(small_limit):
    """No read ever times out here: each byte starts a new read timeout. The abort must reach the
    socket, because a close of the client from another thread does not wake the read."""
    dripper = Dripper()
    try:
        transport = ToLoopback(dripper.port)
        assert _timed_refusal(doc_url(), resolver=_public, transport=transport) < WITHIN_LIMIT
        assert _wait_for_every_slot(small_limit, 1.0) < 1.0
    finally:
        dripper.close()


class FakeStream:
    """What httpcore hands the trace extension when a connection is open."""

    def __init__(self, opened: socket.socket) -> None:
        self.opened = opened

    def get_extra_info(self, name: str) -> socket.socket | None:
        return self.opened if name == "socket" else None


def _blocked_read(far_end: socket.socket) -> tuple[threading.Thread, threading.Event]:
    """A read that nothing answers and whose own timeout is far away, as httpcore's read is."""
    ended = threading.Event()

    def read() -> None:
        far_end.settimeout(SLOW_HOST_SECONDS)
        try:
            far_end.recv(1)
        except OSError:
            pass
        ended.set()

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    return reader, ended


def test_the_abort_wakes_a_read_that_is_blocked_on_the_socket():
    """A close of the client alone leaves this read asleep until its timeout, also on the box."""
    near, far = socket.socketpair()
    abort = metadata_document._Abort()
    abort.trace("connection.connect_tcp.complete", {"return_value": FakeStream(near)})
    _, ended = _blocked_read(near)
    try:
        assert not ended.wait(0.1)
        abort.fire()
        assert ended.wait(1.0), "the read did not end"
        assert abort.fired
    finally:
        abort.detach()
        near.close()
        far.close()


def test_a_connection_that_opens_after_the_abort_is_shut_down_at_once():
    near, far = socket.socketpair()
    abort = metadata_document._Abort()
    abort.fire()
    abort.trace("connection.connect_tcp.complete", {"return_value": FakeStream(near)})
    _, ended = _blocked_read(near)
    try:
        assert ended.wait(1.0), "the read did not end"
    finally:
        abort.detach()
        near.close()
        far.close()


def test_the_abort_closes_the_client_when_it_has_no_socket():
    closed: list[bool] = []

    class Recorded(httpx.BaseTransport):
        def close(self) -> None:
            closed.append(True)

    abort = metadata_document._Abort()
    client = httpx.Client(transport=Recorded())
    abort.attach(client)
    abort.fire()
    assert closed == [True] and client.is_closed
    late = httpx.Client(transport=Recorded())
    abort.attach(late)
    assert closed == [True, True] and late.is_closed


def test_a_slow_first_address_stops_the_worker_before_the_next_address(small_limit):
    url = doc_url()

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("too slow", request=request)

    held = HeldTransport(refuse)

    def resolver(host: str, port: int) -> list[str]:
        return [PUBLIC_V4, PUBLIC_V6]

    try:
        assert _timed_refusal(url, resolver=resolver, transport=held) < WITHIN_LIMIT
    finally:
        held.release.set()
    assert _wait_for_every_slot(small_limit, WITHIN_LIMIT) < WITHIN_LIMIT
    assert [request.url.host for request in held.requests] == [PUBLIC_V4]


def test_every_address_shares_one_limit(small_limit):
    url = doc_url()
    tried: list[str] = []

    def slow_connect(request: httpx.Request) -> httpx.Response:
        tried.append(request.url.host)
        threading.Event().wait(SMALL_LIMIT / 2)
        raise httpx.ConnectTimeout("too slow", request=request)

    def resolver(host: str, port: int) -> list[str]:
        return [f"93.184.215.{n}" for n in range(1, 40)]

    elapsed = _timed_refusal(url, resolver=resolver, transport=httpx.MockTransport(slow_connect))
    assert elapsed < WITHIN_LIMIT
    assert _wait_for_every_slot(small_limit, WITHIN_LIMIT) < WITHIN_LIMIT
    assert len(tried) < 39


def test_the_limits_are_five_seconds_a_call_and_four_calls_at_a_time():
    assert metadata_document.FETCH_TIMEOUT_SECONDS == 5.0
    assert MAX_CONCURRENT_DOCUMENT_FETCHES == 4


# The slots -------------------------------------------------------------------------------------


def _held_workers(url: str, count: int) -> tuple[HeldTransport, list[threading.Thread]]:
    """`count` fetches whose workers stay held after their callers leave at the limit."""
    held = HeldTransport(lambda request: httpx.Response(200, content=document(url)))

    def one() -> None:
        with pytest.raises(DocumentRefused):
            fetch_document(url, resolver=_public, transport=held)

    callers = [threading.Thread(target=one) for _ in range(count)]
    for caller in callers:
        caller.start()
    return held, callers


def test_with_every_worker_held_the_next_call_refuses_at_once_and_a_slot_comes_back(small_limit):
    url = doc_url()
    held, callers = _held_workers(url, MAX_CONCURRENT_DOCUMENT_FETCHES)
    try:
        for caller in callers:
            caller.join()
        assert len(held.requests) == MAX_CONCURRENT_DOCUMENT_FETCHES

        # Every caller has left at the limit, and every worker still holds its slot.
        seen = len(held.requests)
        start = clock.monotonic()
        with pytest.raises(DocumentRefused, match="too many"):
            fetch_document(url, resolver=_public, transport=held)
        assert clock.monotonic() - start < SMALL_LIMIT / 2, "a busy refusal waits for nothing"
        assert len(held.requests) == seen, "a busy refusal reaches no host"
    finally:
        held.release.set()

    assert _wait_for_every_slot(small_limit, WITHIN_LIMIT) < WITHIN_LIMIT
    fine = httpx.MockTransport(lambda request: httpx.Response(200, content=document(url)))
    assert fetch_document(url, resolver=_public, transport=fine).client_id == url


def test_a_refused_url_takes_no_slot(small_limit):
    for _ in range(MAX_CONCURRENT_DOCUMENT_FETCHES + 1):
        with pytest.raises(DocumentRefused, match="user part"):
            fetch_document("https://user@lumen.example/desk.json")
    assert small_limit.acquire(blocking=False)
    small_limit.release()


def test_a_busy_refusal_is_a_failed_fetch_for_the_copy_in_the_store(
    driver, migrated, hosts_plant, small_limit
):
    url = hosts_plant.track_client(doc_url())
    new_url = hosts_plant.track_client(doc_url())
    now = clock.now()
    assert FakeHost(answer=serving(document(url))).client(driver, url, now)
    held, callers = _held_workers(url, MAX_CONCURRENT_DOCUMENT_FETCHES)
    try:
        for caller in callers:
            caller.join()
        busy = FakeHost(answer=serving(document(url)))
        kept = busy.client(driver, url, now + timedelta(hours=2))
        assert kept is not None and kept.fetched_at == now and busy.requests == []
        assert busy.client(driver, new_url, now) is None and busy.requests == []
    finally:
        held.release.set()
    assert _wait_for_every_slot(small_limit, WITHIN_LIMIT) < WITHIN_LIMIT


# The cap on document rows ----------------------------------------------------------------------


def test_a_new_row_takes_a_slot_of_the_window_and_a_full_window_refuses_it(
    driver, migrated, hosts_plant
):
    first = hosts_plant.track_client(doc_url())
    second = hosts_plant.track_client(doc_url())
    window = CodeWindow(limit=1, seconds=3600.0)
    now = clock.now()
    assert FakeHost(answer=serving(document(first))).client(driver, first, now, window)
    host = FakeHost(answer=serving(document(second)))
    assert host.client(driver, second, now, window) is None
    assert find_client(driver, second) is None
    assert window.take() is not None, "the window stays full"


def test_a_refetch_of_a_known_row_takes_no_slot(driver, migrated, hosts_plant):
    url = hosts_plant.track_client(doc_url())
    window = CodeWindow(limit=1, seconds=3600.0)
    now = clock.now()
    host = FakeHost(answer=serving(document(url)))
    assert host.client(driver, url, now, window)
    later = now + timedelta(hours=2)
    refreshed = host.client(driver, url, later, window)
    assert refreshed is not None and refreshed.fetched_at == later and len(host.requests) == 2


def test_a_failed_fetch_and_a_failed_store_spend_no_slot(
    driver, migrated, hosts_plant, monkeypatch
):
    url = hosts_plant.track_client(doc_url())
    window = CodeWindow(limit=1, seconds=3600.0)
    now = clock.now()
    assert FakeHost(answer=serving(b"", 503)).client(driver, url, now, window) is None

    def broken(*args, **kwargs):
        raise RuntimeError("the store is down")

    monkeypatch.setattr(metadata_document, "register_client", broken)
    with pytest.raises(RuntimeError):
        FakeHost(answer=serving(document(url))).client(driver, url, now, window)
    assert window.take() is None, "the slot is still free"


# The copy in the store -------------------------------------------------------------------------


def test_the_copy_serves_for_an_hour_then_a_new_fetch_refreshes_it(driver, migrated, hosts_plant):
    url = hosts_plant.track_client(doc_url())
    host = FakeHost(answer=serving(document(url)))
    now = clock.now()
    first = host.client(driver, url, now)
    assert first is not None and first.registration_type == "metadata_document"
    assert first.host == "Lumen Desk" and first.redirect_uris == (LOOPBACK_REDIRECT,)
    assert first.auth_method == "none" and first.secret_hash is None
    assert len(host.requests) == 1

    later = now + timedelta(minutes=59)
    again = host.client(driver, url, later)
    assert again == first and len(host.requests) == 1, "a fresh copy is not fetched again"

    renamed = "https://lumen.example/cb"
    host.answer = serving(document(url, client_name="Lumen Desk 2", redirect_uris=[renamed]))
    stale = now + timedelta(minutes=61)
    fresh = host.client(driver, url, stale)
    assert len(host.requests) == 2
    assert fresh.host == "Lumen Desk 2" and fresh.redirect_uris == (renamed,)
    assert fresh.fetched_at == stale and fresh.registered_at == first.registered_at


def test_a_failed_fetch_keeps_a_copy_of_the_last_24_hours_and_no_older(
    driver, migrated, hosts_plant
):
    url = hosts_plant.track_client(doc_url())
    host = FakeHost(answer=serving(document(url)))
    now = clock.now()
    assert host.client(driver, url, now)
    host.answer = serving(b"", 503)
    two_hours = now + timedelta(hours=2)
    kept = host.client(driver, url, two_hours)
    assert kept is not None and kept.fetched_at == now and len(host.requests) == 2
    a_day = now + timedelta(hours=24, minutes=1)
    assert host.client(driver, url, a_day) is None


def test_a_document_that_never_fetched_is_no_client(driver, migrated, hosts_plant):
    url = hosts_plant.track_client(doc_url())
    host = FakeHost(addresses=["127.0.0.1"], answer=serving(document(url)))
    found = host.client(driver, url, clock.now())
    assert found is None and host.requests == []
