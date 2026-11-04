from contextlib import contextmanager
from threading import Event

import httpx
import pytest

from grove.cleanup.access import CleanupRefused
from grove.cleanup.fetch import MAX_PAGE_BYTES, fetch_page

HOSTS = frozenset({"public.example"})
URL = "https://public.example/status"


def fetch(handler, **kwargs):
    return fetch_page(
        URL,
        hosts=HOSTS,
        resolver=lambda *_: ["8.8.8.8"],
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def test_page_fetch_pins_the_checked_address_and_removes_active_markup():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<p>The ferry is open.</p><script>secret()</script>",
        )

    page = fetch(handler)
    assert page.text == "The ferry is open."
    assert requests[0].url.host == "8.8.8.8"
    assert requests[0].headers["host"] == "public.example"
    assert requests[0].extensions["sni_hostname"] == "public.example"
    assert "cookie" not in requests[0].headers and "authorization" not in requests[0].headers


@pytest.mark.parametrize(
    "destination",
    [
        "https://private.example/a",
        "http://public.example/a",
        "https://public.example:8443/a",
        "https://name:password@public.example/a",
    ],
)
def test_each_redirect_is_checked_before_another_socket(destination):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": destination})

    with pytest.raises(CleanupRefused):
        fetch(handler)
    assert len(requests) == 1


def test_redirect_rechecks_dns_and_refuses_a_private_address():
    addresses = iter((["8.8.8.8"], ["127.0.0.1"]))
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "/other"})

    with pytest.raises(CleanupRefused, match="not_public"):
        fetch_page(
            URL,
            hosts=HOSTS,
            resolver=lambda *_: next(addresses),
            transport=httpx.MockTransport(handler),
        )
    assert len(requests) == 1


@pytest.mark.parametrize(
    "headers,text",
    [
        ({"content-type": "application/octet-stream"}, "data"),
        ({"content-type": "text/plain", "content-encoding": "gzip"}, ""),
        ({"content-type": "text/plain"}, "x" * (MAX_PAGE_BYTES + 1)),
        ({"content-type": "text/plain"}, ""),
    ],
)
def test_invalid_content_is_never_returned(headers, text):
    with pytest.raises(CleanupRefused):
        fetch(lambda _: httpx.Response(200, headers=headers, text=text))


def test_timeout_keeps_authority_until_worker_stops_and_opens_no_late_socket(monkeypatch):
    from grove.cleanup import fetch as module

    monkeypatch.setattr(module, "FETCH_SECONDS", 0.02)
    release, held, finished = Event(), Event(), Event()
    requests = []

    @contextmanager
    def guard():
        held.set()
        try:
            yield
        finally:
            held.clear()
            finished.set()

    def resolver(*_):
        assert release.wait(2)
        return ["8.8.8.8"]

    try:
        with pytest.raises(CleanupRefused, match="deadline"):
            fetch_page(
                URL,
                hosts=HOSTS,
                resolver=resolver,
                guard=guard,
                transport=httpx.MockTransport(lambda request: requests.append(request)),
            )
        assert held.is_set()
    finally:
        release.set()
        assert finished.wait(2)
    assert not requests
