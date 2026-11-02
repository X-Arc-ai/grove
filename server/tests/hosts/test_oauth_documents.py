"""Client ID Metadata Documents: the fetch rules against a fake resolver and httpx.MockTransport
(never the network), and the copy in the store that serves for an hour and, when a fetch fails,
for 24 hours."""

from dataclasses import dataclass, field
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest

from grove import clock
from grove.oauth.metadata_document import (
    MAX_DOCUMENT_BYTES,
    DocumentRefused,
    document_client,
    fetch_document,
    global_address,
    is_document_client_id,
)
from tests.hosts.conftest import LOOPBACK_REDIRECT
from tests.hosts.oauth_kit import document

PUBLIC_V4 = "93.184.215.14"
PUBLIC_V6 = "2606:2800:21f:cb07:6820:80da:af6b:8b2c"


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

    def client(self, driver, url: str, now):
        """The store's copy through document_client, fetched from this host when it must be."""
        options = {"resolver": self.resolve, "transport": self.transport}
        return document_client(driver, url, now=now, **options)


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
        lambda url: document(url, token_endpoint_auth_method="client_secret_basic"),
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
