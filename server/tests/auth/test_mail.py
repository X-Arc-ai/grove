"""ResendMailer against a MockTransport: its request, and a failure that says only a status.

test_mail_live.py makes the one real send, and only when asked.
"""

import json

import httpx
import pytest

from grove.api.mail import MailFailed, ResendMailer, load_mailer, valid_address

ADDRESS = "maya.chen@example.test"
CODE = "204816"
KEY = "key-for-this-test"
SENDER = "Grove <grove@example.test>"


def _mailer(handler) -> ResendMailer:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return ResendMailer(KEY, SENDER, http=http)


def test_it_posts_the_message_to_the_provider():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"id": "an-id"})

    _mailer(handler).send_code(ADDRESS, CODE)
    request = seen[0]
    assert str(request.url) == "https://api.resend.com/emails"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert json.loads(request.content) == {
        "from": SENDER,
        "to": [ADDRESS],
        "subject": "Your Grove sign-in code",
        "text": (
            f"Your Grove sign-in code is {CODE}. It expires in 10 minutes. "
            "If you did not ask for this code, you can ignore this message."
        ),
    }


def _no_leak(failed: MailFailed) -> None:
    for text in (str(failed), repr(failed)):
        assert ADDRESS not in text and CODE not in text and KEY not in text


@pytest.mark.parametrize("status", [400, 401, 403, 422, 429, 500])
def test_a_refusal_raises_with_the_status_only(status):
    with pytest.raises(MailFailed) as raised:
        _mailer(lambda request: httpx.Response(status, json={"to": ADDRESS})).send_code(
            ADDRESS, CODE
        )
    assert raised.value.status == status
    _no_leak(raised.value)


def test_a_redirect_is_a_failure_and_is_not_followed():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://evil.example/"})

    with pytest.raises(MailFailed):
        _mailer(handler).send_code(ADDRESS, CODE)
    assert seen == ["https://api.resend.com/emails"]


def test_no_answer_raises_with_no_status_and_no_chain():
    def handler(request):
        raise httpx.ConnectTimeout("no answer", request=request)

    with pytest.raises(MailFailed) as raised:
        _mailer(handler).send_code(ADDRESS, CODE)
    assert raised.value.status is None
    assert raised.value.__cause__ is None and raised.value.__suppress_context__
    _no_leak(raised.value)


def test_the_environment_gives_a_mailer_only_with_both_values(monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("MAIL_FROM", raising=False)
    assert load_mailer() is None
    monkeypatch.setenv("RESEND_API_KEY", KEY)
    assert load_mailer() is None
    monkeypatch.setenv("MAIL_FROM", SENDER)
    assert isinstance(load_mailer(), ResendMailer)
    assert KEY not in repr(load_mailer())


@pytest.mark.parametrize(
    "address",
    [ADDRESS, "a@b.co", "a+tag@sub.example.test", "ä@example.test", "x" * 240 + "@example.test"],
)
def test_a_valid_address(address):
    assert valid_address(address) is True
