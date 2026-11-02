"""Each class in several realistic formats, and Grove's own dev text left untouched.

The cases run through the whole screener, so a case fails when its detector is removed and when
the screener stops applying it.
"""

import pytest

from grove.screening import detectors
from tests.screening._plants import (
    ANTHROPIC_KEY,
    AWS_KEY_ID,
    CARD_DIGITS,
    CARD_VISA,
    DB_SECRET,
    ENTROPY_TOKEN,
    GITHUB_PAT,
    GITHUB_TOKEN,
    GITLAB_TOKEN,
    GOOGLE_KEY,
    HF_TOKEN,
    JWT,
    NPM_TOKEN,
    OPENAI_KEY,
    PEM_BLOCK,
    SLACK_TOKEN,
    STRIPE_KEY,
    luhn_check_digit,
    screen_one,
    screener,
)

CRED = {"credential": 1}

CREDENTIALS = [
    pytest.param(
        f"here is the key {OPENAI_KEY} use it",
        "here is the key [credential] use it",
        id="openai-project-key",
    ),
    pytest.param(
        f"ANTHROPIC_API_KEY={ANTHROPIC_KEY}",
        "ANTHROPIC_API_KEY=[credential]",
        id="anthropic-key-in-env-line",
    ),
    pytest.param(f"aws id {AWS_KEY_ID}.", "aws id [credential].", id="aws-access-key-id"),
    pytest.param(f"token: {GITHUB_TOKEN}", "token: [credential]", id="github-token"),
    pytest.param(f"push with {GITHUB_PAT}", "push with [credential]", id="github-fine-grained"),
    pytest.param(f"slack bot {SLACK_TOKEN} ok", "slack bot [credential] ok", id="slack-token"),
    pytest.param(f"stripe {STRIPE_KEY}", "stripe [credential]", id="stripe-live-key"),
    pytest.param(f"maps {GOOGLE_KEY}", "maps [credential]", id="google-api-key"),
    pytest.param(f"gitlab {GITLAB_TOKEN}", "gitlab [credential]", id="gitlab-token"),
    pytest.param(f"hub {HF_TOKEN}", "hub [credential]", id="hugging-face-token"),
    pytest.param(f"registry {NPM_TOKEN}", "registry [credential]", id="npm-token"),
    pytest.param(f"session {JWT} expires", "session [credential] expires", id="jwt"),
    pytest.param(
        "Authorization: Bearer abc123def456ghi789jkl",
        "Authorization: Bearer [credential]",
        id="bearer",
    ),
    pytest.param(
        f"postgres://grove:{DB_SECRET}@db.example:5432/grove",
        "postgres://grove:[credential]@db.example:5432/grove",
        id="url-userinfo-keeps-user-and-host",
    ),
    pytest.param(
        "https://api.example.com/v1/items?key=AbC123xyz&page=2",
        "https://api.example.com/v1/items?key=[credential]&page=2",
        id="url-query-key",
    ),
    pytest.param('{"api_key": "abcd1234efgh"}', '{"api_key": "[credential]"}', id="json-api-key"),
    pytest.param(
        "client_secret: Zk9-qW2_eR7", "client_secret: [credential]", id="yaml-client-secret"
    ),
    pytest.param("DB_PASSWORD=hunter2!", "DB_PASSWORD=[credential]", id="env-password"),
    pytest.param(
        "const apiKey = 'xyz98765';", "const apiKey = '[credential]';", id="camel-case-api-key"
    ),
    pytest.param(
        "my password is Correct-Horse-9", "my password is [credential]", id="phrase-password"
    ),
    pytest.param("the PIN is 4821, ok?", "the PIN is [credential], ok?", id="phrase-pin"),
    pytest.param("PIN: 004821", "PIN: [credential]", id="pin-label"),
    pytest.param(
        f"the secret for staging: {ENTROPY_TOKEN}",
        "the secret for staging: [credential]",
        id="entropy-with-context",
    ),
    pytest.param(
        "datadog api key 3f2a9c1d8e7b6a5f4e3d2c1b0a9f8e7d",
        "datadog api key [credential]",
        id="hex-key-right-after-context",
    ),
    pytest.param(
        f"before\n{PEM_BLOCK}\nafter", "before\n[credential]\nafter", id="pem-private-key-block"
    ),
    pytest.param(
        "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIEpAIBAAKCAQEA",
        "[credential]",
        id="pem-without-end-line",
    ),
]


@pytest.mark.parametrize("text, expected", CREDENTIALS)
def test_credentials(text, expected):
    assert screen_one(screener(), text) == (expected, CRED)


def card(prefix: str) -> str:
    return prefix + luhn_check_digit(prefix)


CARDS = [
    pytest.param(f"card {CARD_VISA} exp 12/27", "card [card] exp 12/27", 1, id="spaced"),
    pytest.param("4111-1111-1111-1111", "[card]", 1, id="dashed"),
    pytest.param(f"no. {CARD_DIGITS}.", "no. [card].", 1, id="unbroken"),
    pytest.param("amex 3782 822463 10005", "amex [card]", 1, id="amex-4-6-5"),
    pytest.param("visa13 4222222222222", "visa13 [card]", 1, id="thirteen-digits"),
    pytest.param(
        f"{card('6011' + '1111' * 3 + '12')} long", "[card] long", 1, id="nineteen-digits"
    ),
    pytest.param("5555 5555 5555 4444 cvv 123", "[card] cvv 123", 1, id="cvv-word-stays"),
    pytest.param("4111 1111 1111 1111 123", "[card] 123", 1, id="cvv-group-after-card"),
    pytest.param(
        "４１１１ １１１１ １１１１ １１１１",
        "[card]",
        1,
        id="full-width-digits",
    ),
    pytest.param("4111​1111​1111​1111", "[card]", 1, id="zero-width-separators"),
    pytest.param("4111111111111111 and 5555555555554444", "[card] and [card]", 2, id="two-cards"),
]


@pytest.mark.parametrize("text, expected, count", CARDS)
def test_payment_cards(text, expected, count):
    assert screen_one(screener(), text) == (expected, {"payment_card": count})


NOT_CARDS = [
    pytest.param("4111 1111 1111 1112", id="fails-luhn"),
    pytest.param("+4111111111111111", id="after-a-plus"),
    pytest.param("41111111111111111111111", id="longer-digit-run"),
    pytest.param("2026-11-02 2026-11-03", id="two-dates"),
    pytest.param("Order 112-3456789-1234567", id="order-number-shape"),
    pytest.param("41111111-1111-4111-8111-111111111111", id="uuid-of-digits"),
]


@pytest.mark.parametrize("text", NOT_CARDS)
def test_not_cards(text):
    kept, counts = screen_one(screener(), text)
    assert "payment_card" not in counts


def test_luhn():
    assert detectors.luhn_valid("4111111111111111")
    assert detectors.luhn_valid("79927398713")
    assert not detectors.luhn_valid("4111111111111112")


ONE_TIME_CODES = [
    pytest.param("Your verification code is 482910", None, id="verification-code-is"),
    pytest.param("482910 is your Grove login code", None, id="code-before-phrase"),
    pytest.param("G-482910 is your Google verification code.", None, id="lettered-code"),
    pytest.param("Use 7731 as your one-time password", None, id="one-time-password"),
    pytest.param("OTP: 55120934", None, id="otp"),
    pytest.param("Your 2FA code: 391 204", None, id="split-code"),
    pytest.param("Enter the code 8841 to continue", None, id="enter-the-code"),
    pytest.param("Your code is 1234.", None, id="your-code-is"),
    pytest.param("security code - 662913 - do not share", None, id="security-code"),
    pytest.param("code is 7720", None, id="code-is-at-the-end"),
    pytest.param("Code 6612 expires soon", "noreply@accounts.example", id="noreply-sender"),
    pytest.param("Here: 552901", "Security", id="security-sender"),
    pytest.param("verification code: 2026", None, id="year-said-as-the-code"),
]


@pytest.mark.parametrize("text, sender", ONE_TIME_CODES)
def test_one_time_codes_drop_the_message(text, sender):
    assert screen_one(screener(), text, sender=sender) == (None, {"one_time_code": 1})


RESET_LINKS = [
    pytest.param(
        "Reset here: https://app.example/reset-password?token=Zx81kQ9vLm2Pq7Rt",
        id="reset-token-query",
    ),
    pytest.param("https://auth.example/magic/0193a2b4c5d6e7f8a9b0", id="magic-path"),
    pytest.param(
        "https://app.example/verify-email/0193a2b4-5c6d-7e8f-9a0b-1c2d3e4f5a6b", id="verify-path"
    ),
    pytest.param("https://app.example/resetPassword/Zx81kQ9vLm2Pq7Rt9", id="camel-case-path"),
    pytest.param("https://app.example/oauth/callback?code=4/0AeanS0aBcD123xyz", id="oauth-code"),
    pytest.param("Here is your magic link: https://grove.example/l/x", id="magic-link-phrase"),
    pytest.param("Sign in with this link https://grove.example/s", id="sign-in-with-this-link"),
    pytest.param("Reset your password: www.example.com/r", id="reset-phrase-www"),
    pytest.param("join https://team.example/invite/7f3KqP92xLm4Wd8Z", id="invite-token"),
]


@pytest.mark.parametrize("text", RESET_LINKS)
def test_reset_and_magic_links_drop_the_message(text):
    assert screen_one(screener(), text) == (None, {"reset_link": 1})


CONTACTS = [
    pytest.param(
        "mail orla@brightwell-mail.example now",
        "mail [email 1] now",
        {"contact_email": 1},
        id="email",
    ),
    pytest.param(
        "TAMSIN@Reyes-Studio.co.uk", "[email 1]", {"contact_email": 1}, id="email-any-case"
    ),
    pytest.param(
        "mailto:orla@brightwell-mail.example", "mailto:[email 1]", {"contact_email": 1}, id="mailto"
    ),
    pytest.param(
        "<orla+grove@brightwell-mail.example>", "<[email 1]>", {"contact_email": 1}, id="plus-tag"
    ),
    pytest.param(
        "call +44 7700 900123", "call [phone 1]", {"contact_phone": 1}, id="plus-international"
    ),
    pytest.param("+1 (415) 555-0134", "[phone 1]", {"contact_phone": 1}, id="us-format"),
    pytest.param("0044 20 7946 0958", "[phone 1]", {"contact_phone": 1}, id="double-zero"),
    pytest.param("06 12 34 56 78", "[phone 1]", {"contact_phone": 1}, id="pairs"),
    pytest.param("06.12.34.56.78", "[phone 1]", {"contact_phone": 1}, id="dots"),
    pytest.param("(415) 555-0134", "[phone 1]", {"contact_phone": 1}, id="parens-local"),
    pytest.param("0612345678", "[phone 1]", {"contact_phone": 1}, id="unbroken-local"),
    pytest.param(
        "02.11.2026 0612345678", "02.11.2026 [phone 1]", {"contact_phone": 1}, id="after-a-date"
    ),
    pytest.param("+212-600-123456", "[phone 1]", {"contact_phone": 1}, id="dashes"),
    pytest.param(
        "call me (+44 7700 900123)", "call me ([phone 1])", {"contact_phone": 1}, id="in-parens"
    ),
    pytest.param(
        "0612345678 0698765432",
        "[phone 1] [phone 2]",
        {"contact_phone": 2},
        id="two-numbers-one-space",
    ),
    pytest.param(
        "+44 7700 900123 +44 7700 900124",
        "[phone 1] [phone 2]",
        {"contact_phone": 2},
        id="two-plus-numbers",
    ),
    pytest.param("ping @tamsin_r", "ping [handle 1]", {"contact_handle": 1}, id="handle"),
    pytest.param(
        "ask @tamsin.reyes about it",
        "ask [handle 1] about it",
        {"contact_handle": 1},
        id="dotted-handle",
    ),
    pytest.param("(@brightwell_ops)", "([handle 1])", {"contact_handle": 1}, id="handle-in-parens"),
    pytest.param(
        "@447700900123 see this", "[handle 1] see this", {"contact_handle": 1}, id="numeric-mention"
    ),
    pytest.param(
        "@tamsin_r\ncan you check",
        "[handle 1]\ncan you check",
        {"contact_handle": 1},
        id="handle-alone-on-a-line",
    ),
]


@pytest.mark.parametrize("text, expected, counts", CONTACTS)
def test_contact_identifiers(text, expected, counts):
    assert screen_one(screener(), text) == (expected, counts)


PAY = [
    pytest.param("my salary is 30k MAD", "my salary is [amount]", id="salary-k-mad"),
    pytest.param(
        "they pay me $4,500 per month", "they pay me [amount] per month", id="dollar-per-month"
    ),
    pytest.param("a bonus of €2.000 this year", "a bonus of [amount] this year", id="euro-bonus"),
    pytest.param("my rate is £65 per hour", "my rate is [amount] per hour", id="pound-rate"),
    pytest.param("retainer: 12000 dhs monthly", "retainer: [amount] monthly", id="dirham-retainer"),
    pytest.param("EUR 3,200 monthly", "[amount] monthly", id="iso-code-before"),
    pytest.param("a raise to 9 500 MAD", "a raise to [amount]", id="space-thousands"),
    pytest.param("I earn about 45k a year", "I earn about [amount] a year", id="k-suffix-alone"),
]


@pytest.mark.parametrize("text, expected", PAY)
def test_pay_amounts_with_the_class_on(text, expected):
    assert screen_one(screener(pay=True), text) == (expected, {"pay_amount": 1})


@pytest.mark.parametrize("text, expected", PAY)
def test_pay_amounts_are_untouched_without_the_class(text, expected):
    assert screen_one(screener(), text) == (text, {})


DEV_TEXT = [
    pytest.param(
        "commit 3f2a9c1d8e7b6a5f4e3d2c1b0a9f8e7d6c5b4a39 fixes the key lookup",
        id="git-sha-near-key",
    ),
    pytest.param("git show 3f2a9c1d8e7b6a5f4e3d2c1b0a9f8e7d6c5b4a39", id="git-sha"),
    pytest.param("item 0193a2b4-5c6d-7e8f-9a0b-1c2d3e4f5a6b in the api log", id="uuid-near-api"),
    pytest.param("01934567-8901-7abc-8def-0123456789ab", id="uuid-v7-digits-first"),
    pytest.param("on 2026-11-02 and 02.11.2026 and 02/11/2026", id="dates"),
    pytest.param("on 02.11.2026 0930 we meet, 2026-11-02 0930 too", id="date-then-time-digits"),
    pytest.param(
        "at 2026-11-02T10:15:00Z or 2026-11-02T10:15:00.123456+01:00", id="iso-timestamps"
    ),
    pytest.param("at 10:15, 09:30:15, 14:05 on 2026-11-02 10:15", id="times"),
    pytest.param(
        "v0.12.3, 1.2.3456.7890, neo4j:2026.10.1-community, python 3.12.13", id="versions"
    ),
    pytest.param("@pytest.fixture\ndef world():\n    pass", id="decorator-before-def"),
    pytest.param("@dataclass(frozen=True)\nclass Row:", id="decorator-with-call"),
    pytest.param("    @property\n    def size(self):", id="indented-decorator"),
    pytest.param("@app.route('/v1/items')", id="dotted-decorator"),
    pytest.param("npm i @types/node and import x from '@scope/pkg'", id="npm-scopes"),
    pytest.param("icon@2x.png and git@github.com:X-Arc-ai/grove.git", id="file-and-ssh-remote"),
    pytest.param("the code is 1200 lines", id="code-is-lines"),
    pytest.param("error code 500, HTTP status code 404, exit code 2", id="error-codes"),
    pytest.param(
        "order #48213 shipped; Order 112-3456789-1234567; PO-2026-00417", id="order-numbers"
    ),
    pytest.param(
        "Plan: the cpx32 box is €14.50, the domain $12, budget $3,000 for Q1", id="prices-in-a-plan"
    ),
    pytest.param(
        "1,234,567 rows and 1.234.567 rows in 0.0001234567 s; 123456789012 ops", id="big-numbers"
    ),
    pytest.param("0.5 0.25 0.125 0.0625", id="decimals"),
    pytest.param("the max_tokens is 4096 and the token count 1200", id="token-counts"),
    pytest.param("GROVE_SLICE_ID=screening uv run pytest; MODAL_TOKEN_ID is set", id="env-names"),
    pytest.param("export GH_TOKEN=$(gh auth token)", id="env-reference"),
    pytest.param("api_key = settings.model_api_key; token = os.environ['X']", id="code-references"),
    pytest.param("the password is wrong; password: required", id="password-words"),
    pytest.param("pin the version: 1.2.3; the compose pin is a digest", id="pin-a-version"),
    pytest.param("see https://github.com/X-Arc-ai/grove/pull/12", id="pull-request-link"),
    pytest.param(
        "https://linear.app/x-arc/issue/LUX-123/fix-login-redirect", id="issue-link-with-login"
    ),
    pytest.param(
        "https://github.com/X-Arc-ai/grove/blob/3f2a9c1d8e7b6a5f4e3d2c1b0a9f8e7d6c5b4a39/server/verify_x.py",
        id="blob-link-with-verify",
    ),
    pytest.param(
        "https://app.grove.x-arc.ai/v1/auth/google/callback", id="auth-route-without-token"
    ),
    pytest.param("server/grove/screening/detectors.py and tests/test_api_keys_2026.py", id="paths"),
    pytest.param("Bearer authentication is out of scope; the bearer of news", id="bearer-prose"),
    pytest.param(
        "task-force-alpha-bravo-charlie-delta and risk-assessment-2026", id="hyphen-words"
    ),
    pytest.param("the verification code feature ships in 2026", id="year-near-a-phrase"),
    pytest.param("we hit 30k downloads in the first week", id="k-number-without-pay-word"),
    pytest.param("the rate limit is 60 per minute", id="pay-word-without-amount"),
    pytest.param("meet @ 10 in room 0", id="lone-at-sign"),
]


@pytest.mark.parametrize("text", DEV_TEXT)
def test_dev_text_stays_untouched(text):
    # With the pay class on, the widest setting: nothing here is a value.
    assert screen_one(screener(pay=True), text) == (text, {})


def test_shannon_bits():
    assert detectors.shannon_bits("aaaa") == 0
    assert detectors.shannon_bits("abcd") == 2
    assert detectors.shannon_bits(ENTROPY_TOKEN) >= detectors.ENTROPY_BITS


def test_a_hit_repr_holds_offsets_and_class_only():
    hits = detectors.contacts("mail orla@brightwell-mail.example")
    assert len(hits) == 1
    assert "orla" not in repr(hits[0]) and "brightwell" not in repr(hits[0])
