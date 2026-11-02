"""One real send through ResendMailer, to the provider's test address. Only when asked:

    cd server && uv run pytest -m live tests/auth/test_mail_live.py -q -s -p no:cacheprovider

The key and the sender come from ~/.grove/resend.env, read in the test and never printed. A default
run deselects this test and never opens that file. The test prints the provider's status, nothing
else: the address is the provider's own, and the code is a made-up one.
"""

from pathlib import Path

import httpx
import pytest

from grove.api.mail import API_KEY_ENV, MAIL_FROM_ENV, ResendMailer

pytestmark = pytest.mark.live

ENV_FILE = Path.home() / ".grove" / "resend.env"
TEST_ADDRESS = "delivered@resend.dev"
MADE_UP_CODE = "000000"
SENT = 200


def _read_env_file() -> dict[str, str]:
    values = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        name, separator, value = line.strip().removeprefix("export ").partition("=")
        if separator:
            values[name] = value.strip().strip("'\"")
    return values


def test_one_real_send_reaches_the_provider():
    if not ENV_FILE.is_file():
        pytest.fail("the live mail test needs ~/.grove/resend.env", pytrace=False)
    values = _read_env_file()
    for name in (API_KEY_ENV, MAIL_FROM_ENV):
        if not values.get(name):
            pytest.fail(f"~/.grove/resend.env holds no {name}", pytrace=False)

    # The hook sees the provider's answer: "no error" alone would also pass a mailer that sent none.
    statuses: list[int] = []

    def keep_status(response: httpx.Response) -> None:
        statuses.append(response.status_code)

    with httpx.Client(event_hooks={"response": [keep_status]}) as http:
        ResendMailer(values[API_KEY_ENV], values[MAIL_FROM_ENV], http=http).send_code(
            TEST_ADDRESS, MADE_UP_CODE
        )

    print(f"the provider answered {statuses}")
    assert statuses == [SENT]
