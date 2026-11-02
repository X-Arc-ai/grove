"""The sign-in mail: one protocol, one provider, and the rule of what counts as an address.

A failure says only the provider's status. The address, the code and the key stay out of every
message, so nothing in a log or an error can repeat them.
"""

import os
import unicodedata
from typing import Protocol

import httpx

RESEND_URL = "https://api.resend.com/emails"
API_KEY_ENV = "RESEND_API_KEY"
MAIL_FROM_ENV = "MAIL_FROM"
MAX_ADDRESS_LENGTH = 254
SUBJECT = "Your Grove sign-in code"
_BODY = (
    "Your Grove sign-in code is {code}. It expires in 10 minutes. "
    "If you did not ask for this code, you can ignore this message."
)
_TIMEOUT_SECONDS = 10.0


class MailFailed(Exception):
    """The provider did not take the mail. status is None when no answer came."""

    def __init__(self, status: int | None) -> None:
        super().__init__(f"the mail provider answered {status}")
        self.status = status


class Mailer(Protocol):
    def send_code(self, to: str, code: str) -> None: ...


class ResendMailer:
    def __init__(self, api_key: str, sender: str, *, http: httpx.Client | None = None) -> None:
        self._api_key = api_key
        self._sender = sender
        self._http = http

    def _post(self, http: httpx.Client, to: str, code: str) -> httpx.Response:
        return http.post(
            RESEND_URL,
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "from": self._sender,
                "to": [to],
                "subject": SUBJECT,
                "text": _BODY.format(code=code),
            },
            timeout=_TIMEOUT_SECONDS,
            follow_redirects=False,
        )

    def send_code(self, to: str, code: str) -> None:
        try:
            if self._http is not None:
                response = self._post(self._http, to, code)
            else:
                with httpx.Client() as http:
                    response = self._post(http, to, code)
        except httpx.HTTPError:
            # No chain: the request the error carries names the address's mail.
            raise MailFailed(None) from None
        if not response.is_success:
            raise MailFailed(response.status_code)


def load_mailer() -> Mailer | None:
    """The mailer of the environment, or None when it holds no key or sender."""
    api_key, sender = os.environ.get(API_KEY_ENV), os.environ.get(MAIL_FROM_ENV)
    if not api_key or not sender:
        return None
    return ResendMailer(api_key, sender)


def valid_address(value: object) -> bool:
    """At most 254 characters, one @, no whitespace or control character, a dot in the domain."""
    if not isinstance(value, str) or not 0 < len(value) <= MAX_ADDRESS_LENGTH:
        return False
    if any(c.isspace() or unicodedata.category(c).startswith("C") for c in value):
        return False
    local, at, domain = value.partition("@")
    labels = domain.split(".")
    return bool(at and local and "@" not in domain and len(labels) >= 2 and all(labels))
