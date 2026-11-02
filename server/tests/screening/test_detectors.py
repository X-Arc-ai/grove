"""Each class in several realistic formats, and Grove's own dev text left untouched.

The cases run through the whole screener, so a case fails when its detector is removed and when
the screener stops applying it.
"""

import random
import string

import pytest

from grove.screening import detectors
from tests.screening._plants import (
    ANTHROPIC_KEY,
    AWS_KEY_ID,
    CARD_DIGITS,
    CARD_VISA,
    DB_SECRET,
    DISCORD_WEBHOOK_PATH,
    ENTROPY_TOKEN,
    GITHUB_OAUTH_TOKEN,
    GITHUB_PAT,
    GITHUB_SERVER_TOKEN,
    GITHUB_TOKEN,
    GITLAB_TOKEN,
    GOOGLE_KEY,
    HF_TOKEN,
    JWT,
    NPM_TOKEN,
    OPENAI_KEY,
    PEM_BLOCK,
    PGP_BLOCK,
    PROVIDER_KEYS,
    SLACK_TOKEN,
    SLACK_WEBHOOK_PATH,
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
    pytest.param(f"key\n{PGP_BLOCK}\nend", "key\n[credential]\nend", id="pgp-private-key-block"),
    # Each of these has no context word, so only the prefix pattern can mask it.
    pytest.param(f"push with {GITHUB_TOKEN} now", "push with [credential] now", id="ghp-alone"),
    pytest.param(
        f"push with {GITHUB_OAUTH_TOKEN} now", "push with [credential] now", id="gho-alone"
    ),
    pytest.param(
        f"push with {GITHUB_SERVER_TOKEN} now", "push with [credential] now", id="ghs-alone"
    ),
    *(
        pytest.param(f"use this one {key} thanks", "use this one [credential] thanks", id=name)
        for name, key in PROVIDER_KEYS.items()
    ),
    pytest.param(
        f"https://hooks.slack.com/services/{SLACK_WEBHOOK_PATH}",
        "https://hooks.slack.com/services/[credential]",
        id="slack-webhook",
    ),
    pytest.param(
        f"https://discord.com/api/webhooks/{DISCORD_WEBHOOK_PATH}",
        "https://discord.com/api/webhooks/[credential]",
        id="discord-webhook",
    ),
    # The value right after "=": the entropy rule reads it, whatever the name.
    pytest.param(f"key={ENTROPY_TOKEN}", "key=[credential]", id="key-equals-token"),
    pytest.param(f"signing_key={ENTROPY_TOKEN}", "signing_key=[credential]", id="signing-key"),
    pytest.param(f"license_key={ENTROPY_TOKEN}", "license_key=[credential]", id="license-key"),
    pytest.param(
        f"the license key={ENTROPY_TOKEN}", "the license key=[credential]", id="spaced-name"
    ),
    pytest.param(f"--license-key={ENTROPY_TOKEN}", "--license-key=[credential]", id="cli-flag"),
    # A suffix after the secret word, with a value too short for the entropy rule.
    pytest.param("API_KEY_PROD=hunter2pass", "API_KEY_PROD=[credential]", id="key-prod-suffix"),
    pytest.param(
        "SECRET_KEY_BASE=hunter2pass", "SECRET_KEY_BASE=[credential]", id="rails-key-base"
    ),
    pytest.param("OPENAI_KEY_2=hunter2pass", "OPENAI_KEY_2=[credential]", id="numbered-key"),
    pytest.param("token_v2=hunter2pass", "token_v2=[credential]", id="lowercase-suffix"),
    pytest.param(
        "DD_APP_KEY_RO=3f9a8c7d6e5b4a3f2e1d0c9b8a7f6e5d",
        "DD_APP_KEY_RO=[credential]",
        id="hex-suffix",
    ),
    pytest.param(
        f"export API_KEY_PROD={ENTROPY_TOKEN}", "export API_KEY_PROD=[credential]", id="export-line"
    ),
    pytest.param(
        "JWT_SECRET=correct horse battery staple",
        "JWT_SECRET=[credential]",
        id="env-value-runs-to-the-line-end",
    ),
    pytest.param(
        "DB_PASSWORD=hunter2! # the staging box",
        "DB_PASSWORD=[credential] # the staging box",
        id="env-value-keeps-a-comment",
    ),
    # URL userinfo with an empty user, and curl.
    pytest.param(
        "redis://:Kq8vN2xP7mZ4tR9w@cache.example.net:6379/0",
        "redis://:[credential]@cache.example.net:6379/0",
        id="redis-empty-user",
    ),
    pytest.param(
        "REDIS_URL=rediss://:Kq8vN2xP7mZ4tR9w@cache.example.net:6380",
        "REDIS_URL=rediss://:[credential]@cache.example.net:6380",
        id="redis-url-in-env-line",
    ),
    pytest.param(
        "curl -u admin:Kq8vN2xP7mZ4tR9w https://api.example.net/v1",
        "curl -u admin:[credential] https://api.example.net/v1",
        id="curl-u",
    ),
    pytest.param(
        "curl --user admin:Kq8vN2xP7mZ4tR9w https://example.net/v1",
        "curl --user admin:[credential] https://example.net/v1",
        id="curl-user",
    ),
    # Passwords the way a chat writes them.
    pytest.param("pw: Tr0ub4dor3", "pw: [credential]", id="pw"),
    pytest.param("pass: Tr0ub4dor3", "pass: [credential]", id="pass"),
    pytest.param("Wifi pass is BlueFox2024", "Wifi pass is [credential]", id="pass-is"),
    pytest.param("password is: Kq8vN2xP", "password is: [credential]", id="password-is-colon"),
    pytest.param("Password - Kq8vN2xP7m", "Password - [credential]", id="password-dash"),
    pytest.param("password -> Kq8vN2xP7m", "password -> [credential]", id="password-arrow"),
    pytest.param("login: orla / Tr0ub4dor3", "login: orla / [credential]", id="login-pair"),
    pytest.param("creds: admin / Tr0ub4dor3", "creds: admin / [credential]", id="creds-pair"),
    pytest.param("user: orla pass: Tr0ub4dor3", "user: orla pass: [credential]", id="user-pass"),
    pytest.param("the passcode is Zebra-42-Lamp", "the passcode is [credential]", id="passcode"),
    # A plain word is a password only when it is said as someone's own.
    pytest.param("my password is sunshine", "my password is [credential]", id="own-plain-word"),
    pytest.param(
        "wifi password is lemonade", "wifi password is [credential]", id="wifi-plain-word"
    ),
    pytest.param("password=sunshine", "password=[credential]", id="assigned-plain-word"),
    # Basic auth: the value decodes to user:password.
    pytest.param(
        "Authorization: Basic dXNlcjpwYXNzd29yZDEyMw==",
        "Authorization: Basic [credential]",
        id="basic-header",
    ),
    pytest.param(
        "curl -H 'Authorization: Basic dXNlcjpwYXNzd29yZDEyMw==' https://example.net/v1/items",
        "curl -H 'Authorization: Basic [credential]' https://example.net/v1/items",
        id="basic-header-in-curl",
    ),
    pytest.param(
        "the basic auth header is Basic dXNlcjpwYXNzd29yZDEyMw==",
        "the basic auth header is Basic [credential]",
        id="basic-in-prose",
    ),
    pytest.param(
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyzABCDEF",
        "Authorization: Bearer [credential]",
        id="bearer-with-no-digit",
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
    pytest.param("4111.1111.1111.1111", "[card]", 1, id="dot-separators"),
    pytest.param("4111  1111  1111  1111", "[card]", 1, id="double-spaces"),
    pytest.param("4111\t1111\t1111\t1111", "[card]", 1, id="tabs"),
    pytest.param("4111 - 1111 - 1111 - 1111", "[card]", 1, id="spaced-dashes"),
    # The 16-digit head also passes Luhn: the longest form must win, or 3 digits would stay.
    pytest.param("card 6011 1111 1111 1117 000", "card [card]", 1, id="longest-form-wins"),
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
    # These pass Luhn. An unbroken run needs a network's first digit.
    pytest.param('{"ts": 1730541312030, "event": "tool_use"}', id="epoch-milliseconds"),
    pytest.param("the store time is 1730541312030 now", id="store-timestamp"),
    pytest.param("time.time_ns() = 1730541312031456789", id="epoch-nanoseconds"),
    pytest.param("https://example.com/status/1302448938567139324", id="snowflake-in-a-url"),
    pytest.param("4111.1111-1111 1111  1111", id="a-mix-of-odd-separators"),
]


@pytest.mark.parametrize("text", NOT_CARDS)
def test_not_cards(text):
    kept, counts = screen_one(screener(), text)
    assert "payment_card" not in counts


@pytest.mark.parametrize("digits", ["1730541312030", "1730541312031456789", "1302448938567139324"])
def test_the_unbroken_runs_above_pass_luhn(digits):
    assert detectors.luhn_valid(digits)


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
    # What the services send. A takeover scam asks a person to forward exactly these.
    pytest.param("Your WhatsApp code: 482-910", None, id="whatsapp"),
    pytest.param(
        "<#> Your WhatsApp code: 482-910\nYou can also tap on this link to verify your phone: "
        "v.whatsapp.com/482910\nDon't share this code with others",
        None,
        id="whatsapp-full",
    ),
    pytest.param(
        "Your WhatsApp account is being registered on a new device. "
        "Do not share this code: 482-910",
        None,
        id="whatsapp-do-not-share",
    ),
    pytest.param("WhatsApp code 482-913", None, id="whatsapp-short"),
    pytest.param("Use 482910 to verify your Instagram account.", None, id="instagram-verify"),
    pytest.param("482910 is your Instagram code. Don't share it.", None, id="instagram-code"),
    pytest.param(
        "Telegram code: 48291\n\nYou can also tap on this link to log in:\nhttps://t.me/login/48291",
        None,
        id="telegram",
    ),
    pytest.param("Your Uber code: 4829. Never share this code.", None, id="uber"),
    pytest.param(
        "Here's your GitHub launch code, @tamsin!\n"
        "Continue signing up for GitHub by entering the code below:\n48291037",
        None,
        id="github-launch-code",
    ),
    pytest.param("Your Signal registration code: 482-910", None, id="signal"),
    pytest.param("482910 is your Snapchat code", None, id="snapchat"),
    pytest.param("482913 is your code", None, id="is-your-code"),
    pytest.param("Use 482913 to verify your Discord account", None, id="discord"),
    pytest.param("Kestrel code: 482913", None, id="unknown-brand-on-its-own-line"),
    pytest.param("482-910", "WhatsApp", id="service-as-the-sender"),
    pytest.param("Your auth code: 482910", None, id="auth-code-with-the-code-right-after"),
    # No space after the colon: bank texts write it this way.
    pytest.param("OTP:482910", None, id="otp-no-space"),
    pytest.param("otp:482910", None, id="otp-lower-no-space"),
    pytest.param("Your OTP is:482910", None, id="otp-is-no-space"),
    pytest.param("Your verification code:482910", None, id="verification-no-space"),
    pytest.param("Code:482910 is your verification", None, id="code-label-no-space"),
    pytest.param("Use OTP:482910 to log in", None, id="otp-in-a-sentence"),
    pytest.param("verification code:G-482910", None, id="lettered-no-space"),
    pytest.param(
        "Dear customer, OTP:482910 for txn of MAD 500 at ACME. Do not share.", None, id="bank-text"
    ),
    # A code with letters, right after a strong phrase.
    pytest.param("Your verification code is K7X9QZ", None, id="letters-and-digits"),
    pytest.param("Your verification code is ABC123", None, id="letters-then-digits"),
    pytest.param("Your X confirmation code is 4u7xkz2m", None, id="lowercase-code"),
    pytest.param("Slack confirmation code: ABC-123", None, id="dashed-letter-code"),
    pytest.param("Your login code: 7KQ2-M9PX", None, id="dashed-mixed-code"),
    pytest.param("Your sign-in code is a1b2c3", None, id="short-mixed-code"),
    pytest.param("Your code: K7X9QZ", "noreply@example.com", id="letter-code-from-noreply"),
]


@pytest.mark.parametrize("text, sender", ONE_TIME_CODES)
def test_one_time_codes_drop_the_message(text, sender):
    assert screen_one(screener(), text, sender=sender) == (None, {"one_time_code": 1})


def test_a_year_from_a_security_sender_is_no_code():
    text = "Your account review is due in 2026"
    assert screen_one(screener(), text, sender="Security") == (text, {})


@pytest.mark.parametrize("sender", ["Tamsin (via Google Docs)", "Amazon Rivers Book Club"])
def test_a_service_word_inside_a_name_is_no_service_sender(sender):
    text = "the room holds 1400 people"
    assert screen_one(screener(), text, sender=sender) == (text, {})


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
    # No scheme: a phone shows these as links.
    pytest.param(
        "Here is your magic link: app.example.com/m/Kq8vN2xP7mZ4tR9wLm3sXa", id="phrase-no-scheme"
    ),
    pytest.param("app.example.com/magic/Kq8vN2xP7mZ4tR9wLm3sXa", id="path-no-scheme"),
    # A hash-mode app keeps its route after "#".
    pytest.param("https://example.com/#/reset-password/Kq8vN2xP7mZ4tR9wLm3sXa", id="hash-reset"),
    pytest.param("https://app.example.com/#/magic/c4k2x1-9f8e7d6c5b4a3f2e1d0c", id="hash-magic"),
    pytest.param(
        "Forgot your password? https://example.com/forgot/Kq8vN2xP7mZ4tR9wLm3sXa", id="forgot"
    ),
    pytest.param("Use this link to reset: https://x.co/r/Kq8vN2xP7mZ4tR9wLm3sXa", id="to-reset"),
    pytest.param("https://example.com/activate/Kq8vN2xP7mZ4tR9wLm3sXa", id="activate"),
    pytest.param(
        "Please activate your account: https://example.com/accounts/activate/MTIz/c4k2x1-9f8e7d6c5b4a3f2e1d0c/",
        id="activation-with-two-parts",
    ),
    # A real reset page on a code host is no code page.
    pytest.param("https://github.com/password_reset/Kq8vN2xP7mZ4tR9wLm3sXa", id="code-host-reset"),
    # A route the code cannot clear stays a link: a UUID after "verify".
    pytest.param(
        "curl http://127.0.0.1:8000/v1/verify/0193a2b4-5c6d-7e8f-9a0b-1c2d3e4f5a6b",
        id="local-verify",
    ),
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
    # An email right after a label or inside chat markup.
    pytest.param(
        "Email:tamsin@brightwell-mail.example", "Email:[email 1]", {"contact_email": 1}, id="label"
    ),
    pytest.param(
        "from:tamsin@brightwell-mail.example", "from:[email 1]", {"contact_email": 1}, id="from"
    ),
    pytest.param(
        "EMAIL;type=INTERNET:tamsin@brightwell-mail.example",
        "EMAIL;type=INTERNET:[email 1]",
        {"contact_email": 1},
        id="vcard-email",
    ),
    pytest.param(
        "send it to _tamsin@brightwell-mail.example_ please",
        "send it to _[email 1]_ please",
        {"contact_email": 1},
        id="whatsapp-italics",
    ),
    pytest.param(
        "orla_corp@brightwell-mail.example",
        "[email 1]",
        {"contact_email": 1},
        id="underscore-inside",
    ),
    # A phone right after a label, in a tel: link, in a vCard, in a WhatsApp link.
    pytest.param("tel:+212612345678", "tel:[phone 1]", {"contact_phone": 1}, id="tel-uri"),
    pytest.param("Tel:0612345678", "Tel:[phone 1]", {"contact_phone": 1}, id="tel-label-local"),
    pytest.param("phone:+33612345678", "phone:[phone 1]", {"contact_phone": 1}, id="phone-label"),
    pytest.param(
        "My number:0612345678", "My number:[phone 1]", {"contact_phone": 1}, id="number-label"
    ),
    pytest.param(
        "Tel:+44 7700 900123", "Tel:[phone 1]", {"contact_phone": 1}, id="tel-label-spaced"
    ),
    pytest.param(
        "[call](tel:+447700900123)",
        "[call](tel:[phone 1])",
        {"contact_phone": 1},
        id="markdown-tel",
    ),
    pytest.param(
        "TEL;TYPE=CELL:0612345678", "TEL;TYPE=CELL:[phone 1]", {"contact_phone": 1}, id="vcard-tel"
    ),
    pytest.param(
        "TEL;type=CELL;waid=212612345678:+212 6 12 34 56 78",
        "TEL;type=CELL;waid=[phone 1]:[phone 1]",
        {"contact_phone": 2},
        id="vcard-whatsapp-field",
    ),
    pytest.param("wa.me/212612345678", "wa.me/[phone 1]", {"contact_phone": 1}, id="wa-me"),
    pytest.param(
        "https://wa.me/+212612345678",
        "https://wa.me/[phone 1]",
        {"contact_phone": 1},
        id="wa-me-plus",
    ),
    pytest.param(
        "https://api.whatsapp.com/send?phone=212612345678",
        "https://api.whatsapp.com/send?phone=[phone 1]",
        {"contact_phone": 1},
        id="whatsapp-send-link",
    ),
    # The US local form, one separator throughout.
    pytest.param("415-555-0132", "[phone 1]", {"contact_phone": 1}, id="us-dashes"),
    pytest.param("415.555.0132", "[phone 1]", {"contact_phone": 1}, id="us-dots"),
    pytest.param("1-415-555-0132", "[phone 1]", {"contact_phone": 1}, id="us-with-one"),
    pytest.param(
        "my cell is 415 555 0132", "my cell is [phone 1]", {"contact_phone": 1}, id="us-spaces"
    ),
    pytest.param(
        "\u0660\u0666\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668",
        "[phone 1]",
        {"contact_phone": 1},
        id="arabic-indic-digits",
    ),
    # A hyphen in a handle, GitHub style: no tail stays.
    pytest.param("ask @tamsin-reyes", "ask [handle 1]", {"contact_handle": 1}, id="hyphen-handle"),
    pytest.param(
        "PR review from @dorian-kell-dev please",
        "PR review from [handle 1] please",
        {"contact_handle": 1},
        id="two-hyphens",
    ),
    # A chat line of only handles is no stack of decorators.
    pytest.param(
        "@tamsin_r\n@dorian hi",
        "[handle 1]\n[handle 2] hi",
        {"contact_handle": 2},
        id="two-handles",
    ),
    pytest.param(
        "- @tamsin_r\n- @dorian",
        "- [handle 1]\n- [handle 2]",
        {"contact_handle": 2},
        id="handle-list",
    ),
    # A profile link holds the handle.
    pytest.param(
        "https://t.me/tamsinreyes",
        "https://t.me/[handle 1]",
        {"contact_handle": 1},
        id="telegram-link",
    ),
    pytest.param("t.me/tamsinreyes", "t.me/[handle 1]", {"contact_handle": 1}, id="telegram-bare"),
    pytest.param(
        "instagram.com/tamsinreyes",
        "instagram.com/[handle 1]",
        {"contact_handle": 1},
        id="instagram",
    ),
    pytest.param(
        "https://x.com/tamsinreyes", "https://x.com/[handle 1]", {"contact_handle": 1}, id="x-link"
    ),
    pytest.param(
        "https://twitter.com/tamsinreyes",
        "https://twitter.com/[handle 1]",
        {"contact_handle": 1},
        id="twitter-link",
    ),
    pytest.param(
        "https://www.linkedin.com/in/tamsin-reyes-123abc/",
        "https://www.linkedin.com/in/[handle 1]/",
        {"contact_handle": 1},
        id="linkedin-link",
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
    # The amount seven words from the pay word: the far edge of the window.
    pytest.param(
        "my salary for next year will be about 30k MAD",
        "my salary for next year will be about [amount]",
        id="window-far-edge",
    ),
    pytest.param("salary 25000 mad", "salary [amount]", id="lowercase-code-after"),
    pytest.param("salary 25000mad", "salary [amount]", id="lowercase-code-glued"),
    pytest.param("salary 25000 Mad", "salary [amount]", id="title-case-code"),
    pytest.param("salary 25000 usd", "salary [amount]", id="lowercase-usd"),
    pytest.param("salary US$4,500", "salary [amount]", id="us-dollar-sign"),
    pytest.param(
        "they pay me US$4,500 per month",
        "they pay me [amount] per month",
        id="us-dollar-in-a-sentence",
    ),
    pytest.param("salary 1.2M", "salary [amount]", id="bare-big-m"),
    pytest.param("salary 1.2m", "salary [amount]", id="bare-small-m"),
    pytest.param("my salary is 25000", "my salary is [amount]", id="figure-after-is"),
    pytest.param("I earn 25,000/month", "I earn [amount]/month", id="figure-after-earn"),
    pytest.param("I make $4,500/month", "I make [amount]/month", id="make-and-slash-month"),
    pytest.param("salaire 25000 dh", "salaire [amount]", id="french-salary"),
    pytest.param(
        "salaire: 25 000 MAD par mois", "salaire: [amount] par mois", id="french-per-month"
    ),
    pytest.param(
        "25000 MAD is what I make every month",
        "[amount] is what I make every month",
        id="amount-before-the-pay-word",
    ),
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
    # Decorators in diffs, in numbered tool output and in prose.
    pytest.param("+@pytest.fixture\n+def world(neo4j):", id="decorator-in-a-diff"),
    pytest.param("-@pytest.fixture\n-def world(neo4j):", id="removed-decorator-in-a-diff"),
    pytest.param("+@dataclass(frozen=True)\n+class Row:", id="decorator-call-in-a-diff"),
    pytest.param("+    @app.route('/v1/items')", id="indented-decorator-in-a-diff"),
    pytest.param("+@my_guard\n+def world(neo4j):", id="any-decorator-in-a-diff"),
    pytest.param("+@my_guard\n+@other_guard\n+def world(neo4j):", id="decorator-stack-in-a-diff"),
    pytest.param(
        "@@ -10,6 +10,8 @@ class Foo:\n+    @property\n+    def size(self):", id="diff-hunk"
    ),
    pytest.param(
        "    41\t    @my_guard\n    42\t    def folded(self) -> Normalised:", id="numbered-tab"
    ),
    pytest.param(
        "    41\u2192    @my_guard\n    42\u2192    def _drop_class(sender, text):",
        id="numbered-arrow",
    ),
    pytest.param("the `@property` on `folded` builds the copy once", id="decorator-in-backticks"),
    pytest.param("@my_guard  # shared\ndef world():", id="decorator-with-a-comment"),
    pytest.param("use @pytest.fixture for the world", id="decorator-in-prose"),
    pytest.param("wrap it with @functools.cache", id="dotted-decorator-in-prose"),
    pytest.param("make it a @classmethod", id="classmethod-in-prose"),
    # npm scopes with a hyphen.
    pytest.param('import * as Dialog from "@radix-ui/react-dialog";', id="scope-radix"),
    pytest.param("npm install -g @example-ai/some-code", id="scope-global-install"),
    pytest.param('"@typescript-eslint/parser": "^8.12.2",', id="scope-in-package-json"),
    pytest.param("import { render } from '@testing-library/react'", id="scope-testing-library"),
    pytest.param("pnpm add @floating-ui/react-dom@2.1.2", id="scope-with-version"),
    # CSS at-rules, TypeScript directives and JSDoc tags.
    pytest.param("@layer base {", id="css-layer"),
    pytest.param("@font-face {", id="css-font-face"),
    pytest.param("@container (min-width: 400px) {", id="css-container"),
    pytest.param("@theme inline {", id="css-theme"),
    pytest.param("@custom-variant dark (&:is(.dark *));", id="css-custom-variant"),
    pytest.param("@media (min-width: 600px) {", id="css-media"),
    pytest.param("// @ts-expect-error the type is wrong upstream", id="ts-expect-error"),
    pytest.param("// @ts-ignore", id="ts-ignore"),
    pytest.param(" * @default false", id="jsdoc-default"),
    pytest.param(" * @param name the item name", id="jsdoc-param"),
    pytest.param(" * See {@link renderBlocks} for the escaping.", id="jsdoc-link"),
    pytest.param("@everyone hello", id="chat-everyone"),
    # Prose about a key is no key: the value after "is" is a plain word.
    pytest.param("the API key is working now", id="key-is-working"),
    pytest.param("the password was updated yesterday", id="password-was-updated"),
    pytest.param("the API key is configured in the env file", id="key-is-configured"),
    pytest.param("the secret key is loaded from the file", id="secret-key-is-loaded"),
    pytest.param("the password is optional for the dev store", id="password-is-optional"),
    pytest.param("the API key is per-world", id="key-is-per-world"),
    pytest.param("the API key is unset in CI", id="key-is-unset"),
    pytest.param("my password is working again", id="own-password-is-working"),
    pytest.param("pass: 1200, fail: 3", id="test-counts-after-pass"),
    pytest.param("the test pass is flaky", id="pass-is-a-word"),
    pytest.param("a basic understanding of basic authentication helps", id="basic-in-a-sentence"),
    pytest.param("your code is python3 only", id="word-with-a-number"),
    pytest.param("your error code is 1045", id="your-error-code"),
    pytest.param("Error code: 1045\nStatus code: 5030\nZip code: 10001", id="labelled-codes"),
    pytest.param("2730541312030.5 and 3730541312036.25", id="long-decimals"),
    # Python and TypeScript names and types after a secret name.
    pytest.param('password = os.environ["NEO4J_PASSWORD"]', id="password-from-environ"),
    pytest.param("password = None", id="password-none"),
    pytest.param("def connect(host, password=None):", id="password-default-none"),
    pytest.param(
        "driver = GraphDatabase.driver(uri, user=user, password=password)", id="password-passed-on"
    ),
    pytest.param("password = settings.neo4j_password", id="password-from-settings"),
    pytest.param('password = input("Password: ")', id="password-from-input"),
    pytest.param('user, _, password = value.partition("/")', id="password-from-a-call"),
    pytest.param('    "password": password,', id="password-in-a-dict"),
    pytest.param("client = SomeClient(api_key=api_key)", id="api-key-passed-on"),
    pytest.param("return Session(token=token, expires_at=at)", id="token-passed-on"),
    pytest.param("self.token = token", id="token-on-self"),
    pytest.param("token = await client.get_token()", id="token-awaited"),
    pytest.param("const token = await getToken({ req });", id="token-awaited-ts"),
    pytest.param("interface Session {\n  token: string;\n}", id="token-type-ts"),
    pytest.param("type Props = { apiKey: string; onDone: () => void }", id="api-key-type-ts"),
    pytest.param("    client_secret: SecretStr", id="secret-annotation"),
    pytest.param("const apiKey = process.env.MODEL_API_KEY;", id="api-key-from-process-env"),
    # Model ids, slugs, class names and test world ids next to a context word.
    pytest.param('{"model": "claude-opus-4-1-20250805", "max_tokens": 8192}', id="model-request"),
    pytest.param(
        'client.messages.stream(model="claude-sonnet-4-5-20250929", max_tokens=1024)',
        id="model-request-in-code",
    ),
    pytest.param("the embedding API uses text-embedding-3-large", id="embedding-model"),
    pytest.param(
        'Spark via the Model API: {"model": "Llama-4-Maverick-17B-128E-Instruct-FP8", '
        '"max_tokens": 2048}',
        id="model-id-with-tags",
    ),
    pytest.param(
        "Model: claude-opus-4-1-20250805 \u00b7 12,431 tokens \u00b7 $0.42", id="status-line"
    ),
    pytest.param(
        "the token limit for gpt-4o-mini-2024-07-18 is lower", id="dated-model-id-near-token"
    ),
    pytest.param(
        "world t_screening_0193a2b4-5c6d-7e8f-9a0b-1c2d3e4f5a6b has 12 keys", id="test-world-id"
    ),
    pytest.param("the API returns ScreeningReceiptV2Response", id="class-name-near-api"),
    pytest.param("the api work is on feat-foundation-20261101", id="branch-near-api"),
    # A sha or a UUID with other words between it and the context word.
    pytest.param("the api fix is 3f2a9c1d8e7b6a5f4e3d2c1b0a9f8e7d6c5b4a39", id="sha-after-fix-is"),
    pytest.param(
        "fix token refresh in 3f2a9c1d8e7b6a5f4e3d2c1b0a9f8e7d6c5b4a39", id="sha-after-in"
    ),
    pytest.param("the key commit is 3f2a9c1d8e7b6a5f4e3d2c1b0a9f8e7d6c5b4a39", id="key-commit-is"),
    pytest.param("token id 0193a2b4-5c6d-7e8f-9a0b-1c2d3e4f5a6b expired", id="token-id-uuid"),
    # A random-looking token with no context word on its line stays.
    pytest.param("run 01HZX3K9Q4M7V2P8R6T1Y5W0NB failed", id="ulid-without-context"),
    pytest.param(
        "integrity sha384-oqVuAfXRKap7fdgcCY5uykM6+R9GqQ8K/uxy9rx7HNQlGYl1kPzQho1wx4JwY8wC",
        id="sri-hash-without-context",
    ),
    pytest.param("id aGVsbG9Xb3JsZDEyMzQ1Njc4OTBhYmNk in the log", id="base64-id-without-context"),
    pytest.param("the value aB3dE5fG7hJ9kL1mN3pQ5rS7tU9wX here", id="token-without-context"),
    # A token of low entropy next to a context word is a fixture.
    pytest.param("the key a1a1a1a1a1a1a1a1a1a1a1 is a test fixture", id="low-entropy-token"),
    # Code hosts and trackers: a sha, a branch, a file name or a slug there is no token.
    pytest.param(
        "https://github.com/X-Arc-ai/grove/blob/develop/server/grove/auth/session_tokens_v1.py",
        id="github-file-under-auth",
    ),
    pytest.param(
        "https://github.com/X-Arc-ai/grove/blob/develop/server/grove/signin/oauth2_callback.py#L42",
        id="github-line-link",
    ),
    pytest.param(
        "https://github.com/X-Arc-ai/grove/tree/feat/signin-oauth-20261102", id="github-branch"
    ),
    pytest.param(
        "https://github.com/X-Arc-ai/grove/compare/develop...feat/auth-tokens-20261103",
        id="github-compare",
    ),
    pytest.param(
        "the fix is https://github.com/nextauthjs/next-auth/commit/"
        "3f2a9c1d8e7b6a5f4e3d2c1b0a9f8e7d6c5b4a39",
        id="github-commit",
    ),
    pytest.param(
        "https://github.com/nextauthjs/next-auth/releases/tag/next-auth%40v5.0.0-beta.25",
        id="github-release",
    ),
    pytest.param(
        "https://github.com/nextauthjs/next-auth/blob/3f2a9c1d8e7b6a5f4e3d2c1b0a9f8e7d6c5b4a39/"
        "packages/core/src/index.ts#L120",
        id="github-permalink",
    ),
    pytest.param(
        "https://linear.app/x-arc/issue/LUX-301/login-fails-for-oauth2-users", id="linear-oauth2"
    ),
    pytest.param(
        "https://linear.app/x-arc/issue/LUX-310/invite-link-expires-in-24h", id="linear-invite"
    ),
    pytest.param("see https://docs.grove.x-arc.ai/hosts/invite-a-host-v2", id="docs-slug"),
    pytest.param(
        "github.com/nextauthjs/next-auth/commit/3f2a9c1d8e7b6a5f4e3d2c1b0a9f8e7d6c5b4a39",
        id="github-commit-no-scheme",
    ),
    pytest.param("auth.example.com/0193a2b4c5d6e7f8a9b0", id="link-word-only-in-the-host"),
    # Dev talk about the login feature, with a number that names something else.
    pytest.param("refactor the auth code, PR #1234 has it", id="auth-code-pr"),
    pytest.param("the auth code now listens on port 8000", id="auth-code-port"),
    pytest.param("bug in the login code at line 1420", id="login-code-line"),
    pytest.param("the login code is about 1200 lines", id="login-code-lines"),
    pytest.param(
        "exchange the OAuth auth code for a token at line 1188 of client.py", id="oauth-exchange"
    ),
    pytest.param("the sign-in code moved to web/ in PR 1203", id="sign-in-code-pr"),
    pytest.param("authentication code runs in 1200 ms on cold start", id="auth-code-ms"),
    pytest.param("the auth code change is LUX-1234", id="auth-code-ticket"),
    pytest.param("the OTP flow test takes 1500 ms", id="otp-ms"),
    pytest.param("security code review found 0 issues in 1200 lines", id="security-review"),
    pytest.param("the 2fa code path broke again, see PR 1234", id="2fa-pr"),
    pytest.param("the login code flow is in auth.py, see issue 4821", id="login-issue"),
    pytest.param("the verification code check fails on PR 1234", id="verification-pr"),
    pytest.param("fixed the login code bug in #4821", id="login-hash-number"),
    pytest.param("the sign-in code screen is ticket 5521", id="sign-in-ticket"),
    pytest.param("login code tests take 2500 ms now", id="login-tests-ms"),
    pytest.param("mfa code retries: we allow 5 then lock for 3600 s", id="mfa-seconds"),
    pytest.param("the OTP fix is LUX-1234", id="otp-ticket-key"),
    pytest.param("the code is 1200, the tests are 800", id="code-is-then-a-comma"),
    pytest.param("(the code is 1200)", id="code-is-in-parens"),
    pytest.param("the error code is 1045.", id="error-code-is"),
    pytest.param(
        "the verification code screen shipped last week, and after a long review the team "
        "closed 4821",
        id="number-past-the-window",
    ),
    pytest.param("update your code at line 1420", id="your-code-in-dev-talk"),
    # Digit runs that are no phone.
    pytest.param("Wrote 1 file (148273645 bytes)", id="size-in-parens"),
    pytest.param("(123456789 ns)", id="time-in-parens"),
    pytest.param("ids 0 1 2 3 4 5 6 7 8 9 10", id="list-of-ids"),
    pytest.param("run 000123456 finished", id="zero-padded-counter"),
    pytest.param("ping 010.000.000.001", id="padded-address"),
    # By design: a pure digit run with no + or 0 and no WhatsApp place.
    pytest.param("4155550132", id="pure-digit-run"),
    # Pay: an amount past the window, and the words with no amount.
    pytest.param(
        "my salary review is on Friday, and separately the venue for the offsite costs 30k MAD",
        id="amount-past-the-pay-window",
    ),
    pytest.param("the salary review is in 2026", id="year-after-salary-words"),
    pytest.param("the salary 2026 budget is done", id="year-right-after-salary"),
    # A GitHub profile or repository link stays: dev text is full of them.
    pytest.param("https://github.com/tamsinreyes", id="github-link"),
    pytest.param("https://box.com/tamsinreyes", id="host-that-ends-in-x-com"),
    # A file path is no bare host.
    pytest.param("see grove.screening.py/detectors and auth.py/tokens", id="file-path-is-no-host"),
]


@pytest.mark.parametrize("text", DEV_TEXT)
def test_dev_text_stays_untouched(text):
    # With the pay class on, the widest setting: nothing here is a value.
    assert screen_one(screener(pay=True), text) == (text, {})


@pytest.mark.parametrize(
    "token",
    [
        "claude-opus-4-1-20250805",
        "text-embedding-3-large",
        "Llama-4-Maverick-17B-128E-Instruct-FP8",
        "gpt-4o-mini-2024-07-18",
        "ScreeningReceiptV2Response",
        "feat-foundation-20261101",
        "login-fails-for-oauth2-users",
        "invite-link-expires-in-24h",
    ],
)
def test_words_and_numbers_read_as_words(token):
    assert detectors.wordy(token)


@pytest.mark.parametrize(
    "token",
    [
        ENTROPY_TOKEN,
        "Kq8vN2xP7mZ4tR9wLm3sXaHj",
        "Zq7Lm2Xv9Rt4Wp6Ny8Ks3Hd5Gb1Fc",
        "01HZX3K9Q4M7V2P8R6T1Y5W0NB",
        "c4k2x1-9f8e7d6c5b4a3f2e1d0c",
        "0193a2b4-5c6d-7e8f-9a0b-1c2d3e4f5a6b",
        "ABCDE-12345-FGHIJ-67890",
        "dXNlcjpwYXNzd29yZDEyMw==",
    ],
)
def test_a_key_does_not_read_as_words(token):
    assert not detectors.wordy(token)


@pytest.mark.parametrize(
    "alphabet",
    [
        pytest.param(string.ascii_letters + string.digits, id="base62"),
        pytest.param(string.ascii_lowercase + string.digits, id="base36"),
        pytest.param(string.ascii_letters + string.digits + "-_", id="url-safe"),
    ],
)
def test_a_random_key_almost_never_reads_as_words(alphabet):
    # A miss only reaches the entropy rule, the last fallback, so a small rate is the bar.
    pick = random.Random(14)
    tokens = ["".join(pick.choices(alphabet, k=24)) for _ in range(5000)]
    assert sum(detectors.wordy(token) for token in tokens) / len(tokens) < 0.005


def test_shannon_bits():
    assert detectors.shannon_bits("aaaa") == 0
    assert detectors.shannon_bits("abcd") == 2
    assert detectors.shannon_bits(ENTROPY_TOKEN) >= detectors.ENTROPY_BITS


def test_a_hit_repr_holds_offsets_and_class_only():
    hits = detectors.contacts("mail orla@brightwell-mail.example")
    assert len(hits) == 1
    assert "orla" not in repr(hits[0]) and "brightwell" not in repr(hits[0])
