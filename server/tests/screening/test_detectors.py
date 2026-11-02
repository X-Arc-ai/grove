"""Each class in several realistic formats, and Grove's own dev text left untouched.

The cases run through the whole screener, so a case fails when its detector is removed and when
the screener stops applying it.
"""

import json
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
    HEX_32,
    HF_TOKEN,
    JWT,
    LONG_TOKEN,
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
LINK_TOKEN = "Kq8vN2xP7mZ4tR9wLm3s"
RESET_URL = f"https://app.example.com/reset-password/{LINK_TOKEN}"
# A mail service's tracking link: no link word, so only the sentence around it can tell.
WRAPPED_LINK = f"https://u1234.ct.sendgrid.net/ls/click?upn={LINK_TOKEN}{LINK_TOKEN}"

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
    # A context word and base64 with a slash: a random part keeps it a candidate.
    pytest.param(
        "api key Kq8vN2xP7mZ4tR9w/Lm3sXaB1cD5fG7hJ9",
        "api key [credential]",
        id="base64-with-a-slash",
    ),
    pytest.param(
        "aws secret wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "aws secret [credential]",
        id="aws-secret-with-slashes",
    ),
    # The ids of a model API still go after a word that says secret.
    pytest.param(
        "token: req_011CabcDEFghiJKLmno123", "token: [credential]", id="id-shape-right-after-token"
    ),
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
    # A PIN or a security code with no word between the label and the digits.
    pytest.param("PIN 4821", "PIN [credential]", id="pin-space"),
    pytest.param(
        "New debit card arrived, PIN 7316.",
        "New debit card arrived, PIN [credential].",
        id="pin-in-a-chat",
    ),
    pytest.param("PIN-4821", "PIN-[credential]", id="pin-dash"),
    pytest.param("pin4821", "pin[credential]", id="pin-glued"),
    pytest.param("CVV: 123", "CVV: [credential]", id="cvv"),
    pytest.param("cvc 1234", "cvc [credential]", id="cvc"),
    # French and Darija, and the English form with only a space.
    pytest.param("Mot de passe : Tr0ub4dor3", "Mot de passe : [credential]", id="french-password"),
    pytest.param(
        "le mot de passe est Tr0ub4dor3", "le mot de passe est [credential]", id="french-est"
    ),
    pytest.param("le mdp c'est Tr0ub4dor3", "le mdp c'est [credential]", id="french-cest"),
    pytest.param(
        "identifiant: orla mdp: Tr0ub4dor3", "identifiant: orla mdp: [credential]", id="french-pair"
    ),
    pytest.param("mdp Tr0ub4dor3", "mdp [credential]", id="french-space"),
    pytest.param("password Tr0ub4dor3", "password [credential]", id="password-space"),
    pytest.param("code wifi: Tr0ub4dor3", "code wifi: [credential]", id="french-wifi"),
    pytest.param("password dyali: Tr0ub4dor3", "password dyali: [credential]", id="darija-mine"),
    pytest.param(
        "l password dyal wifi howa Tr0ub4dor3",
        "l password dyal wifi howa [credential]",
        id="darija-is",
    ),
    pytest.param("jeton: Kq8vN2xP7mZ4tR9wLm3s", "jeton: [credential]", id="french-token"),
    pytest.param(
        "la cl\u00e9 est Kq8vN2xP7mZ4tR9wLm3s", "la cl\u00e9 est [credential]", id="french-key"
    ),
]


@pytest.mark.parametrize("text, expected", CREDENTIALS)
def test_credentials(text, expected):
    assert screen_one(screener(), text) == (expected, CRED)


PASSWORD = "Tr0ub4dor3"
# Each form holds VALUE once. The whole value goes, one span, and every word around it stays.
VALUE_FORMS = [
    # Words between the password word and its value, or no separator at all.
    ("the password for prod is VALUE", PASSWORD),
    ("password for root: VALUE", PASSWORD),
    ("password to the admin panel: VALUE", PASSWORD),
    ("Password of the wifi is VALUE", PASSWORD),
    ("password? it's VALUE", PASSWORD),
    ("the password will be VALUE", PASSWORD),
    ("password should be VALUE", PASSWORD),
    ("changed the password to VALUE", PASSWORD),
    ("I set the password to VALUE", PASSWORD),
    ("the password for prod is VALUE.", "482910"),
    ("wifi password VALUE", PASSWORD),
    ("admin password VALUE", PASSWORD),
    ("use password VALUE", PASSWORD),
    ("log in with password VALUE", PASSWORD),
    ("username orla password VALUE", PASSWORD),
    ("pwd VALUE", PASSWORD),
    ("user orla pass VALUE", PASSWORD),
    ("u: orla p: VALUE", PASSWORD),
    ("creds are admin / VALUE", "hunter2"),
    ("credentials are admin:VALUE", "hunter2"),
    ("login: admin:VALUE", "hunter2"),
    ("log in with admin / VALUE", "hunter2"),
    # A secret word glued to the word before it.
    ('const openaiKey = "VALUE";', LONG_TOKEN),
    ('masterKey: "VALUE"', LONG_TOKEN),
    ('{"appKey":"VALUE"}', LONG_TOKEN),
    ("XApiKey: VALUE", LONG_TOKEN),
    ("apikey VALUE", LONG_TOKEN),
    ("authtoken=VALUE", LONG_TOKEN),
    ("accesstoken: VALUE", LONG_TOKEN),
    ('mytoken="VALUE"', LONG_TOKEN),
    ("clientsecret=VALUE", LONG_TOKEN),
    ("OPENAIKEY=VALUE", LONG_TOKEN),
    ("openaikey=VALUE", HEX_32),
    ("DefaultEndpointsProtocol=https;AccountName=foo;AccountKey=VALUE;EndpointSuffix=x", "Ab3dE=="),
    (
        "Endpoint=sb://x.servicebus.windows.net/;SharedAccessKeyName=root;SharedAccessKey=VALUE",
        "A1b2",
    ),
    ("//registry.npmjs.org/:_authToken=VALUE", "3f2a1b4c-5d6e-7f80-9a1b-2c3d4e5f6a7b"),
    ("PGPASSWORD=VALUE", "S3cr3tPw"),
    ("DBPASSWORD=VALUE", "S3cr3tPw"),
    ("ADMINPASS=VALUE", "S3cr3tPw"),
    ("dbpassword=VALUE", "S3cr3tPw"),
    ("adminpassword: VALUE", "S3cr3tPw"),
    ("mysqlpass=VALUE", "S3cr3tPw"),
    ("GITHUBTOKEN=VALUE", "abc123xyz"),
    # A value that reads like code to a loose guard, and is not.
    ("DB_PASSWORD=VALUE", "hunter2.Bank1"),
    ("JWT_SECRET=VALUE", "mysecret.Value2026"),
    ("DB_PASSWORD=VALUE", "rabat.summer"),
    ("password=VALUE", "s3cret.Value9"),
    ("token: VALUE", "abc123.DEF456ghi"),
    ("api_key=VALUE", "prod.Kq8vN2xP7mZ4tR9w"),
    ("the password is VALUE", "hunter2.Bank1"),
    ("DB_PASSWORD=VALUE", "$ecret1234"),
    ("password=VALUE", "$uperS3cret"),
    ("password: VALUE", "$3cr3tP@ss"),
    ("my password is VALUE", "$uperS3cret"),
    ("password: VALUE", "Tr0ub(4)dor3"),
    ("password=VALUE", "abcd1234(x)"),
    ("password=VALUE", "abcd1234[x]"),
    ("password=VALUE", "abcd1234<x"),
    ("wifi password: VALUE", "Summer(2024)!"),
    # A symbol inside the value, and a bracket around it.
    ("password=VALUE", "P@ss,w0rd9"),
    ("the password is VALUE", "P@ss,w0rd9"),
    ("docker run -e DB_PASSWORD=VALUE img", "P@ss;w0rd9"),
    ("password: VALUE", "Tr0ub'4dor3"),
    ("password: VALUE", 'Tr0ub"4dor3'),
    ("password: VALUE", "Tr0ub{4}dor3"),
    ("password: VALUE", "O'Brien1!"),
    ("DB_PASSWORD=VALUE", "O'Brien1!"),
    ("password: VALUE", "{Tr0ub4dor3}"),
    ("password: VALUE", "<Tr0ub4dor3>"),
    ("password: VALUE", "[Tr0ub4dor3]"),
    ("password: VALUE", "(Tr0ub4dor3)"),
    ("DB_PASSWORD=VALUE", "(Tr0ub4dor3)"),
    ("password: VALUE", "Tr0ub<4>dor3"),
    ("password: VALUE", "Tr0ub[4]dor3"),
    ("api_key=VALUE", "Ab3,Xy9Zq7Lm2"),
    ("f(password=VALUE)", "hunter2"),
    ("password=VALUE,user=orla", "pw1x"),
    ("Password=VALUE;Server=db", "pw1x"),
    # A label says the rest of the line is the value.
    ("password: VALUE", "correct horse battery staple"),
    ("passphrase = VALUE", "correct horse battery staple"),
    ("DB_PASSWORD: VALUE", "correct horse battery"),
    ("Password: VALUE", "My Dog Rex 2024"),
    ("wifi password: VALUE", "my dog rex"),
    ("password - VALUE", "my dog rex"),
    ("password=VALUE", "correct horse battery staple"),
    ("passphrase: VALUE", "a cat on mars"),
    ("Password: VALUE", "Rex 2024"),
    ("password: VALUE, user: orla", "my dog rex"),
    ("password: VALUE  # the staging box", "my dog rex"),
    ("password: VALUE // the staging box", "my dog rex"),
    ("password: VALUE host: db", "my dog rex"),
    ("mysql --password=VALUE -h db", "my dog rex"),
    # CLI flags.
    ("mysql -u root -pVALUE db", "S3cr3tPw"),
    ("sshpass -p VALUE ssh host", "S3cr3tPw"),
    ("docker login -u me -p VALUE", "S3cr3tPw"),
    ("docker login --password VALUE", "S3cr3tPw"),
    ("psql --pass VALUE", "S3cr3tPw"),
    ("--api-key VALUE", "abc123xyz"),
    ("--token VALUE", "abc123xyz"),
    ("--client-secret VALUE", "abc123xyz"),
    ("--secret-key VALUE", "abc123xyz"),
    ("--access-key VALUE", "abc123xyz"),
    ("cypher-shell -u neo4j -p VALUE", "S3cr3tPw"),
    ("neo4j-admin dbms set-initial-password VALUE", "S3cr3tPw"),
    ("redis-cli -a VALUE", "S3cr3tPw"),
    ("mongosh -u orla -p VALUE", "S3cr3tPw"),
    ("machine api.example.com login me password VALUE", "S3cr3tPw"),
    # SQL and Cypher.
    ("ALTER USER orla WITH PASSWORD 'VALUE';", "S3cr3tPw"),
    ("CREATE USER orla IDENTIFIED BY 'VALUE';", "S3cr3tPw"),
    ("CREATE USER orla SET PASSWORD 'VALUE'", "S3cr3tPw"),
    ("ALTER USER orla SET PASSWORD 'VALUE' CHANGE NOT REQUIRED", "S3cr3tPw"),
    # Code.
    ('GraphDatabase.driver(uri, auth=("neo4j", "VALUE"))', "S3cr3tPw"),
    ('requests.get(url, auth=("orla", "VALUE"))', "S3cr3tPw"),
    ('HTTPBasicAuth("orla", "VALUE")', "S3cr3tPw"),
    ('login("orla", "VALUE")', "S3cr3tPw"),
    ('config["password"] = "VALUE"', "S3cr3tPw"),
    ('cfg.get("password", "VALUE")', "S3cr3tPw"),
    ('os.getenv("DB_PASSWORD", "VALUE")', "S3cr3tPw"),
    ('setenv("DB_PASSWORD", "VALUE")', "S3cr3tPw"),
    # Markup.
    ("<password>VALUE</password>", "S3cr3tPw"),
    ("<apiKey>VALUE</apiKey>", LONG_TOKEN),
    ('<input name="password" value="VALUE">', "S3cr3tPw"),
    # The name on one field and the value on the next.
    ("- name: DB_PASSWORD\n  value: VALUE", "S3cr3tPw"),
    ('env:\n  - name: API_KEY\n    value: "VALUE"', LONG_TOKEN),
    ('{"name": "DB_PASSWORD", "value": "VALUE"}', "S3cr3tPw"),
    ("- name: DB_PASSWORD value: VALUE", "S3cr3tPw"),
    ("[{name: DB_PASSWORD, value: VALUE}]", "S3cr3tPw"),
    ("password: >\n  VALUE", "S3cr3tPw"),
    # A token alone on its line, under a line that says what it is.
    ("the token:\n```\nVALUE\n```", LONG_TOKEN),
    ("here is the api key\n```\nVALUE\n```", LONG_TOKEN),
    ("API key below\nVALUE", LONG_TOKEN),
    ("here is the key\nVALUE", LONG_TOKEN),
    ("my key:\n\nVALUE", LONG_TOKEN),
    ("Secret\nVALUE", LONG_TOKEN),
    ("cat <<EOF\ntoken\nVALUE\nEOF", LONG_TOKEN),
    ("the api key:\nVALUE", "3f2a9c1d8e7b6a5f4e3d2c1b0a9f8e7d"),
    # Formats with no context word, in the places they appear.
    (
        "Use this token to access the HTTP API:\nVALUE\nKeep your token secure",
        PROVIDER_KEYS["telegram-bot"],
    ),
    ("https://api.telegram.org/VALUE/sendMessage", "bot" + PROVIDER_KEYS["telegram-bot"]),
    (
        "https://b.s3.amazonaws.com/k?X-Amz-Date=20261102T000000Z&X-Amz-Signature=VALUE",
        HEX_32 + HEX_32,
    ),
    (
        "https://acme.webhook.office.com/webhookb2/VALUE",
        f"{LONG_TOKEN}/IncomingWebhook/{LONG_TOKEN}",
    ),
    # Session cookies, and an Authorization line whatever its scheme.
    ("Cookie: sessionid=VALUE", LONG_TOKEN),
    ("Cookie: session=VALUE", LONG_TOKEN),
    ("Set-Cookie: sid=VALUE; HttpOnly", LONG_TOKEN),
    ("Set-Cookie: JSESSIONID=VALUE; Path=/", "0123456789ABCDEF0123456789ABCDEF"),
    ("cookie: connect.sid=VALUE", f"s%3A{LONG_TOKEN}"),
    ("-b 'session=VALUE'", LONG_TOKEN),
    ('curl https://app.example.net/dashboard --cookie "session=VALUE"', LONG_TOKEN),
    ("curl 'https://app.example.net/x' -b 'sessionid=VALUE; theme=dark' --compressed", LONG_TOKEN),
    ("Set-Cookie: laravel_session=VALUE; path=/; httponly", LONG_TOKEN),
    ("SESSION_COOKIE=VALUE", LONG_TOKEN),
    ("Authorization: Bearer VALUE", "abc123def456"),
    ("authorization: bearer VALUE", "abc123def456"),
    ("Bearer VALUE", "abc123def456"),
    ("Authorization: VALUE", "abc123def456"),
    ("Authorization: Token VALUE", "abc123def456"),
    ("Authorization: token VALUE", "0123456789abcdef"),
    ("Authorization: Token VALUE", "9944b09199c62bcf9418ad846dd0e4bbdfc6ee4b"),
    ("Bearer: VALUE", LONG_TOKEN),
    ("bearer=VALUE", LONG_TOKEN),
]


@pytest.mark.parametrize("template, value", VALUE_FORMS)
def test_a_value_goes_whole_in_every_form(template, value):
    text = template.replace("VALUE", value)
    expected = template.replace("VALUE", "[credential]")
    assert screen_one(screener(), text) == (expected, CRED)


@pytest.mark.parametrize(
    "text, expected, counts",
    [
        pytest.param(
            f"orla.vance@brightwell.example:{PASSWORD}",
            "[email 1]:[credential]",
            {"credential": 1, "contact_email": 1},
            id="address-and-password",
        ),
        pytest.param(
            "tamsin@brightwell.example:hunter2 is the login",
            "[email 1]:[credential] is the login",
            {"credential": 1, "contact_email": 1},
            id="address-and-password-in-a-sentence",
        ),
        pytest.param(
            "tamsin@brightwell.example:8080",
            "[email 1]:8080",
            {"contact_email": 1},
            id="address-and-port",
        ),
        pytest.param(
            "tamsin@brightwell.example:10:30",
            "[email 1]:10:30",
            {"contact_email": 1},
            id="address-and-time",
        ),
        pytest.param(
            "tamsin@brightwell.example:0612345678",
            "[email 1]:[phone 1]",
            {"contact_email": 1, "contact_phone": 1},
            id="address-and-phone",
        ),
        pytest.param(
            "admin:hunter2@db.example.com:5432/app",
            "admin:[credential]@db.example.com:5432/app",
            CRED,
            id="userinfo-with-no-scheme",
        ),
        pytest.param(
            f"user:{PASSWORD}@10.0.0.5:5432",
            "user:[credential]@10.0.0.5:5432",
            CRED,
            id="userinfo-with-an-address-host",
        ),
        pytest.param(
            "Email:tamsin2@brightwell.example",
            "Email:[email 1]",
            {"contact_email": 1},
            id="a-label-is-no-user",
        ),
        pytest.param(
            f"passwords are {PASSWORD} and Hunt3r2",
            "passwords are [credential] and [credential]",
            {"credential": 2},
            id="a-list-of-passwords",
        ),
        pytest.param(
            f"Cookie: _ga=GA1.2.111.222; sessionid={LONG_TOKEN}",
            "Cookie: _ga=[credential]; sessionid=[credential]",
            {"credential": 2},
            id="two-cookies",
        ),
    ],
)
def test_a_pair_masks_each_part(text, expected, counts):
    assert screen_one(screener(), text) == (expected, counts)


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


def test_a_security_code_after_a_card_goes_too():
    assert screen_one(screener(), "5555 5555 5555 4444 cvv 123") == (
        "[card] cvv [credential]",
        {"credential": 1, "payment_card": 1},
    )


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
    # A verb, a code and a sign-in purpose.
    pytest.param("Use 482910 to sign in to Kestrel.", None, id="use-to-sign-in"),
    pytest.param("Use ABC123 to log in.", None, id="use-letters-to-log-in"),
    pytest.param("Use 482 910 to log in", None, id="use-split-to-log-in"),
    pytest.param("Enter 482910 on the login page", None, id="enter-on-the-page"),
    pytest.param("Input 482910 to verify", None, id="input-to-verify"),
    pytest.param("Reply with 482910 to confirm", None, id="reply-to-confirm"),
    pytest.param("To log in, use 482910", None, id="purpose-first"),
    pytest.param("To authorise this transaction enter 482910.", None, id="purpose-first-bank"),
    pytest.param("Sign in with 482910", None, id="sign-in-with"),
    pytest.param("Log in using code 482910", None, id="log-in-using-code"),
    # The verb and the word "code".
    pytest.param("Use code 482910 to approve your payment of 200 MAD.", None, id="use-code"),
    pytest.param("Enter code 482910", None, id="enter-code"),
    pytest.param("Please use this code 482910", None, id="use-this-code"),
    pytest.param("Use the following code to sign in: 482910", None, id="use-the-following-code"),
    pytest.param("Here is the code to log in: 482910", None, id="code-to-log-in"),
    # How long it lasts, and do not share it.
    pytest.param("482910 expires in 10 minutes", None, id="expires-in"),
    pytest.param("482910 is valid for 10 minutes.", None, id="valid-for"),
    pytest.param("Code: 482910. Valid for 5 min.", None, id="valid-for-after-the-code"),
    pytest.param("This code is valid for 5 minutes: 482910", None, id="valid-for-before-the-code"),
    pytest.param("482910. Do not share it with anyone.", None, id="do-not-share-it"),
    pytest.param("Do not give this code to anyone: 482910", None, id="do-not-give"),
    # Two soft signals settle a number that one signal cannot.
    pytest.param(
        "Use 5521 to log in to Fable. It expires in 5 minutes.", None, id="owner-two-signals"
    ),
    pytest.param("Your number is 482910. It expires in 5 minutes.", None, id="number-expires"),
    # The labels that name the code.
    pytest.param("3D Secure code: 482910", None, id="secure-code"),
    pytest.param(
        "Apple Account Code: 482910. Don't share it with anyone.", None, id="account-code"
    ),
    pytest.param("Steam Guard code: F7KQ2", None, id="guard-code"),
    pytest.param("2FA 482910", None, id="2fa"),
    pytest.param("MFA: 482910", None, id="mfa"),
    pytest.param("TOTP 482910", None, id="totp"),
    pytest.param("Fable login: 482910", None, id="login-label"),
    pytest.param("Your login token is 482910", None, id="login-token"),
    pytest.param("Verification: 482910", None, id="verification-label"),
    pytest.param("482910 is your Larkspire login", None, id="is-your-login"),
    pytest.param("482910 - Kestrel login", None, id="code-then-login"),
    # A line below that starts with a unit word says nothing about the code.
    pytest.param(
        "Your verification code: 482910\nMinutes until it expires: 10", None, id="unit-below"
    ),
    pytest.param("Your OTP: 482910\nTime left: 5 minutes", None, id="time-below"),
    pytest.param("Security code: 482910\nRequest from: Chrome on Mac", None, id="request-below"),
    pytest.param("Verification code 482910\nItems in your cart", None, id="items-below"),
    # A code alone on the next line stays a code.
    pytest.param("Your verification code:\nK7X9QZ\nIt expires soon.", None, id="letters-next-line"),
    # French and Arabic.
    pytest.param("Votre code de v\u00e9rification est 482910", None, id="french-verification"),
    pytest.param("Votre code est : 482910", None, id="french-your-code"),
    pytest.param("Code de confirmation : 482910", None, id="french-confirmation"),
    pytest.param("Votre code WhatsApp : 482-910", None, id="french-brand"),
    pytest.param("Code de connexion Telegram : 48291", None, id="french-login"),
    pytest.param("482910 est votre code Instagram.", None, id="french-is-your"),
    pytest.param("G-482910 est votre code de validation Google.", None, id="french-google"),
    pytest.param("Entrez le code 482910", None, id="french-enter"),
    pytest.param("Utilisez 482910 pour vous connecter", None, id="french-use"),
    pytest.param(
        "\u0631\u0645\u0632 WhatsApp \u0627\u0644\u062e\u0627\u0635 \u0628\u0643: 482-910",
        None,
        id="arabic",
    ),
    pytest.param("code dyal verification: 482910", None, id="darija-verification"),
    # A service sender still counts.
    pytest.param("Here: 552901", "security@kestrel.example", id="service-address"),
]


@pytest.mark.parametrize("text, sender", ONE_TIME_CODES)
def test_one_time_codes_drop_the_message(text, sender):
    assert screen_one(screener(), text, sender=sender) == (None, {"one_time_code": 1})


WEAK_CODES = [
    # The bare word "code" is not enough for a drop, so the number goes and the words stay.
    pytest.param("the code is 482910.", "the code is [code].", None, id="code-is"),
    pytest.param("code is 7720", "code is [code]", None, id="code-is-at-the-end"),
    pytest.param(
        "the port in the code is 8000.",
        "the port in the code is [code].",
        None,
        id="port-in-the-code",
    ),
    pytest.param("Code: 482910", "Code: [code]", None, id="code-label"),
    pytest.param("CODE\n482910", "CODE\n[code]", None, id="code-then-next-line"),
    pytest.param(
        "the code they sent me is 482910",
        "the code they sent me is [code]",
        None,
        id="words-between",
    ),
    pytest.param("the code was 482910", "the code was [code]", None, id="code-was"),
    pytest.param("482910 is the code", "[code] is the code", None, id="is-the-code"),
    pytest.param(
        "send me the code, it's 4829", "send me the code, it's [code]", None, id="code-its"
    ),
    pytest.param("l code dyalk howa 482910", "l code dyalk howa [code]", None, id="darija-is"),
    # One soft signal cannot settle a number that can be a port or a count.
    pytest.param(
        "use 8080 to log in locally", "use [code] to log in locally", None, id="use-a-port"
    ),
    pytest.param("Enter 1200 to continue", "Enter [code] to continue", None, id="enter-a-count"),
    pytest.param("Log in with 5521", "Log in with [code]", None, id="log-in-with-a-short-number"),
    pytest.param(
        "use neo4j to verify", "use [code] to verify", None, id="lowercase-word-with-a-digit"
    ),
    pytest.param(
        "the cache entry 4096 expires in 5 minutes",
        "the cache entry [code] expires in 5 minutes",
        None,
        id="short-number-expires",
    ),
    pytest.param(
        "use LUX-1234 to verify", "use [code] to verify", None, id="ticket-key-after-a-verb"
    ),
    # Capitals right after a strong phrase: a service may send them, and code cannot tell.
    pytest.param(
        "Your verification code is QWERTY", "Your verification code is [code]", None, id="capitals"
    ),
    pytest.param(
        "Your Slack confirmation code: ABC-DEF",
        "Your Slack confirmation code: [code]",
        None,
        id="dashed-capitals",
    ),
    # A sha shape right after a phrase: a commit or a code.
    pytest.param(
        "a1b2c3d Fix the OTP\n8c937ab Add the tests\n",
        "a1b2c3d Fix the OTP\n[code] Add the tests\n",
        None,
        id="log-line-after-otp",
    ),
    pytest.param(
        "the auth code 8c937ab broke the test",
        "the auth code [code] broke the test",
        None,
        id="sha-after-auth-code",
    ),
    pytest.param(
        "Your verification code is a1b2c3d",
        "Your verification code is [code]",
        None,
        id="code-with-a-sha-shape",
    ),
    pytest.param(
        "8c937ab is your code, I checked",
        "[code] is your code, I checked",
        None,
        id="sha-is-your-code",
    ),
    pytest.param("#4821 is your code", "#[code] is your code", None, id="hash-is-your-code"),
    # A service name near a number, not right before it: dev chat says these about a connector.
    pytest.param(
        "we removed the sms code, saved 4000",
        "we removed the sms code, saved [code]",
        None,
        id="sms-code-far",
    ),
    pytest.param(
        "he sent me the WhatsApp code, it is 482910",
        "he sent me the WhatsApp code, it is [code]",
        None,
        id="whatsapp-code-far",
    ),
    pytest.param(
        "Use 482910 as your Instagram code",
        "Use [code] as your Instagram code",
        None,
        id="as-your-brand-code",
    ),
    # A sender that can be a person.
    pytest.param(
        "I sent 1500 today", "I sent [code] today", "Ahmed Accounts", id="person-with-a-role-word"
    ),
]


@pytest.mark.parametrize("text, expected, sender", WEAK_CODES)
def test_a_weak_code_is_masked_and_the_message_stays(text, expected, sender):
    assert screen_one(screener(), text, sender=sender) == (expected, {"one_time_code": 1})


def test_a_year_from_a_security_sender_is_no_code():
    text = "Your account review is due in 2026"
    assert screen_one(screener(), text, sender="Security") == (text, {})


@pytest.mark.parametrize("sender", ["Tamsin (via Google Docs)", "Amazon Rivers Book Club"])
def test_a_service_word_inside_a_name_is_no_service_sender(sender):
    text = "the room holds 1400 people"
    assert screen_one(screener(), text, sender=sender) == (text, {})


@pytest.mark.parametrize(
    "text, sender",
    [
        pytest.param(
            "PR 4821 merged, 1200 lines changed", "noreply@github.com", id="pr-from-noreply"
        ),
        pytest.param("Standup moved to room 1204", "no-reply@calendar.example.com", id="room"),
        pytest.param("Review of PR 1234 is done", "Security Review Bot", id="pr-from-a-bot"),
        pytest.param("The build took 1500 ms and PR 1234 passed", "auth lane", id="units"),
        pytest.param("Invoice 48210 is paid", "Karim Accounts", id="invoice"),
        pytest.param("LUX-1234 is closed", "noreply@linear.app", id="ticket-key"),
    ],
)
def test_a_number_that_names_something_else_stays_whatever_the_sender(text, sender):
    # The address in a sender is its own class; only the text and the code count are asked about.
    kept, counts = screen_one(screener(), text, sender=sender)
    assert kept == text
    assert "one_time_code" not in counts


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
    # Chat markup and a JSON escape beside the token.
    pytest.param(f"<{RESET_URL}|Reset>", id="slack-label"),
    pytest.param(f"**{RESET_URL}**", id="markdown-bold"),
    pytest.param(f"{RESET_URL}|x", id="pipe"),
    pytest.param(f'{{"url":"{RESET_URL}\\"}}', id="json-escape"),
    pytest.param(f"{RESET_URL},and", id="comma-glued"),
    pytest.param(f"{RESET_URL}*and", id="star-glued"),
    pytest.param(f"{RESET_URL}\\x", id="backslash-glued"),
    pytest.param(f"{RESET_URL}$x", id="dollar-glued"),
    # Words glued together.
    pytest.param(f"https://app.example.com/verifyemail/{LINK_TOKEN}", id="verifyemail"),
    pytest.param(f"https://app.example.com/setpassword/{LINK_TOKEN}", id="setpassword"),
    pytest.param(f"https://app.example.com/pwreset/{LINK_TOKEN}", id="pwreset"),
    pytest.param(f"https://app.example.com/magiclogin/{LINK_TOKEN}", id="magiclogin"),
    pytest.param(f"https://app.example.com/authenticate/{LINK_TOKEN}", id="authenticate"),
    pytest.param(f"https://www.notion.so/loginwithemail#{LINK_TOKEN}", id="loginwithemail"),
    # A tracking link has no word of its own, so the sentence right before it counts.
    pytest.param(f"Use this link to log in: {WRAPPED_LINK}", id="link-to-log-in"),
    pytest.param(f"Click this link to sign in: {WRAPPED_LINK}", id="link-to-sign-in"),
    pytest.param(f"Here is your one-time link: {WRAPPED_LINK}", id="one-time-link"),
    pytest.param(f"Your login link: {WRAPPED_LINK}", id="login-link-then-url"),
    pytest.param(
        "Please verify your email: https://grove.example/v/abc", id="verify-then-short-url"
    ),
    pytest.param("Your login link is below: https://grove.example/go/abcdef", id="gap-words"),
    # French.
    pytest.param(
        f"R\u00e9initialisez votre mot de passe : {WRAPPED_LINK}", id="french-reset-phrase"
    ),
    pytest.param(f"Lien de connexion : {WRAPPED_LINK}", id="french-login-link"),
    pytest.param("Votre lien magique : https://x.example/l/abc", id="french-magic-link"),
    pytest.param(f"https://app.example.com/reinitialiser/{LINK_TOKEN}", id="french-reset-path"),
    pytest.param(f"https://app.example.com/connexion/{LINK_TOKEN}", id="french-login-path"),
    pytest.param(f"https://app.example.com/mot-de-passe/{LINK_TOKEN}", id="french-password-path"),
    # A host rule holds a path, so a reset page on the same host still counts.
    pytest.param("https://www.notion.so/invite/Zx81kQ9vLm2Pq7Rt9abc", id="notion-invite"),
    pytest.param(
        "https://raw.githubusercontent.com/x-arc-ai/private/auth/Zx81kQ9vLm2Pq7Rt9abc",
        id="raw-host-token-no-sha",
    ),
    pytest.param("https://docs.google.com/reset/Zx81kQ9vLm2Pq7Rt9abc", id="google-host-reset"),
    pytest.param("https://www.figma.com/reset/Zx81kQ9vLm2Pq7Rt9abc", id="figma-host-reset"),
]


@pytest.mark.parametrize("text", RESET_LINKS)
def test_reset_and_magic_links_drop_the_message(text):
    assert screen_one(screener(), text) == (None, {"reset_link": 1})


WEAK_LINKS = [
    # The token holds no digit or no letter, which a real token can: 1 in 17 at 16 characters.
    pytest.param(
        "https://app.example.com/reset-password/KqvNxPmZtRwLmsXaHjbVc", "[link]", id="no-digit"
    ),
    pytest.param(
        "https://app.example/reset-password/zxkqvlmpqrtwybnmdhgf", "[link]", id="lowercase-only"
    ),
    pytest.param("https://app.example.com/reset/4829103746582910", "[link]", id="no-letter"),
    pytest.param(
        "https://github.com/password_reset/abcdefghijklmnopqrstuvwx",
        "[link]",
        id="no-digit-code-host",
    ),
    pytest.param(
        "https://x.example/verify/1302448938567139324", "[link]", id="long-id-after-a-word"
    ),
    # A URL ends at "|", so a Slack label keeps its words.
    pytest.param(
        "<https://app.example.com/reset-password/KqvNxPmZtRwLmsXaHjbVc|Reset it>",
        "<[link]|Reset it>",
        id="slack-label-stays",
    ),
    # A code in the query beside a link word.
    pytest.param(
        "see https://app.example.com/verify?code=482910 now", "see [link] now", id="code-in-query"
    ),
    pytest.param("https://app.example.com/auth?code=K7X9QZ", "[link]", id="letters-code-in-query"),
    # A link word in the host, or a token before the word: the shape allows either reading.
    pytest.param(f"https://login.example.com/{LINK_TOKEN}", "[link]", id="word-in-the-host"),
    pytest.param(
        f"https://passwordreset.microsoftonline.com/?ru={LINK_TOKEN}",
        "[link]",
        id="word-glued-in-the-host",
    ),
    pytest.param("auth.example.com/0193a2b4c5d6e7f8a9b0", "[link]", id="host-no-scheme"),
    pytest.param(
        f"https://app.example.com/{LINK_TOKEN}/verify-email", "[link]", id="token-before-the-word"
    ),
    # A long id after a slug of words on a host with no page rule.
    pytest.param(
        "https://blog.example.com/login-flow-notes-3f2a9c1e7b4d5a6f8e0c1b2a3d4f5e6a",
        "[link]",
        id="slug-with-a-long-id",
    ),
    # A phrase that dev chat says about a feature, with the URL further off.
    pytest.param(
        "The login link flow is in https://docs.example.com/guides/sessions",
        "The login link flow is in [link]",
        id="login-link-far",
    ),
    pytest.param(
        "Plan:\n1. Add magic links later.\n\nRef: https://nextjs.org/docs/app\n",
        "Plan:\n1. Add magic links later.\n\nRef: [link]\n",
        id="magic-links-far",
    ),
    pytest.param(
        "use this link to log in, it works once: https://bit.ly/3xYzQ7a",
        "use this link to log in, it works once: [link]",
        id="link-to-log-in-far",
    ),
    # Common words: only the URL right after them counts.
    pytest.param(
        f"Sign in to Kestrel: {WRAPPED_LINK}", "Sign in to Kestrel: [link]", id="sign-in-to"
    ),
    pytest.param(
        f"Confirm your email: {WRAPPED_LINK}", "Confirm your email: [link]", id="confirm-your-email"
    ),
    pytest.param(
        f"Verify your account: {WRAPPED_LINK}",
        "Verify your account: [link]",
        id="verify-your-account",
    ),
    pytest.param(
        f"Set a new password here: {WRAPPED_LINK}",
        "Set a new password here: [link]",
        id="set-a-new-password",
    ),
    pytest.param(
        f"This link expires in 15 minutes: {WRAPPED_LINK}",
        "This link expires in 15 minutes: [link]",
        id="link-expires",
    ),
]


@pytest.mark.parametrize("text, expected", WEAK_LINKS)
def test_a_weak_link_is_masked_and_the_message_stays(text, expected):
    assert screen_one(screener(), text) == (expected, {"reset_link": 1})


def test_a_long_tool_output_with_one_phrase_loses_only_its_url():
    text = f"{'x' * 5000}\nthe magic links line\nhttps://docs.example.com/a\n{'y' * 5000}"
    kept, counts = screen_one(screener(), text)
    assert kept == text.replace("https://docs.example.com/a", "[link]")
    assert counts == {"reset_link": 1}


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
    # A number after a lone ")" group: the group loses its ")" and reads as an empty candidate.
    pytest.param(
        "1 ) (415) 555-0132", "1 ) [phone 1]", {"contact_phone": 1}, id="after-lone-paren"
    ),
    pytest.param(
        "call 3 ) (020) 7946 0958 today",
        "call 3 ) [phone 1] today",
        {"contact_phone": 1},
        id="after-lone-paren-mid-sentence",
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
    # A WhatsApp mention of an unsaved contact, with and without its bidi isolates.
    pytest.param(
        "@\u2068+212 612-345678\u2069 can you check",
        "@\u2068[phone 1]\u2069 can you check",
        {"contact_phone": 1},
        id="mention-of-a-number-isolated",
    ),
    pytest.param(
        "@+212612345678 hi",
        "@[phone 1] hi",
        {"contact_phone": 1},
        id="mention-of-a-number",
    ),
    pytest.param(
        "@06 12 34 56 78 hi",
        "@[phone 1] hi",
        {"contact_phone": 1},
        id="mention-of-a-local-number",
    ),
    # A number glued to a label or a name: a digit before the mark is what a version has.
    pytest.param(
        "Tel.06 12 34 56 78",
        "Tel.[phone 1]",
        {"contact_phone": 1},
        id="label-dot",
    ),
    pytest.param(
        "Tél.+212 612 345 678",
        "Tél.[phone 1]",
        {"contact_phone": 1},
        id="label-dot-plus",
    ),
    pytest.param(
        "T.0612345678",
        "T.[phone 1]",
        {"contact_phone": 1},
        id="one-letter-label",
    ),
    pytest.param(
        "Tel-0612345678",
        "Tel-[phone 1]",
        {"contact_phone": 1},
        id="label-dash",
    ),
    pytest.param(
        "Orla,0612345678",
        "Orla,[phone 1]",
        {"contact_phone": 1},
        id="name-comma",
    ),
    pytest.param(
        "Orla-0612345678",
        "Orla-[phone 1]",
        {"contact_phone": 1},
        id="name-dash",
    ),
    pytest.param(
        "x1,+33612345678",
        "x1,[phone 1]",
        {"contact_phone": 1},
        id="plus-form-after-a-digit",
    ),
    pytest.param(
        "Tel0612345678",
        "Tel[phone 1]",
        {"contact_phone": 1},
        id="label-no-mark",
    ),
    pytest.param(
        "whatsapp0612345678",
        "whatsapp[phone 1]",
        {"contact_phone": 1},
        id="whatsapp-label-no-mark",
    ),
    pytest.param(
        "415 555-0132",
        "[phone 1]",
        {"contact_phone": 1},
        id="us-space-then-dash",
    ),
    # Two numbers given together.
    pytest.param(
        "0612345678/0698765432",
        "[phone 1]/[phone 2]",
        {"contact_phone": 2},
        id="pair-slash",
    ),
    pytest.param(
        "06 12 34 56 78/06 98 76 54 32",
        "[phone 1]/[phone 2]",
        {"contact_phone": 2},
        id="pair-slash-spaced",
    ),
    pytest.param(
        "0612345678,0698765432",
        "[phone 1],[phone 2]",
        {"contact_phone": 2},
        id="pair-comma",
    ),
    pytest.param(
        "0612345678-0698765432",
        "[phone 1]-[phone 2]",
        {"contact_phone": 2},
        id="pair-dash",
    ),
    pytest.param(
        "+33612345678,+33698765432",
        "[phone 1],[phone 2]",
        {"contact_phone": 2},
        id="pair-plus-comma",
    ),
    pytest.param(
        "415-555-0132/415-555-0133",
        "[phone 1]/[phone 2]",
        {"contact_phone": 2},
        id="pair-us",
    ),
    # A link writes "@" as %40 and "+" as %2B.
    pytest.param(
        "https://app.example.com/unsubscribe?e=orla.vance%40gmail.example&t=1",
        "https://app.example.com/unsubscribe?e=[email 1]&t=1",
        {"contact_email": 1},
        id="percent-email",
    ),
    pytest.param(
        "https://app.example.com/call?to=%2B212612345678",
        "https://app.example.com/call?to=%2B[phone 1]",
        {"contact_phone": 1},
        id="percent-phone",
    ),
    pytest.param(
        "https://api.whatsapp.com/send/?phone=%2B212612345678&text=hi",
        "https://api.whatsapp.com/send/?phone=%2B[phone 1]&text=hi",
        {"contact_phone": 1},
        id="whatsapp-redirect",
    ),
    pytest.param(
        "viber://chat?number=%2b212612345678",
        "viber://chat?number=%2b[phone 1]",
        {"contact_phone": 1},
        id="percent-phone-lower-case",
    ),
    # A place settles that bare digits are a phone.
    pytest.param(
        "https://web.whatsapp.com/send?phone=212612345678",
        "https://web.whatsapp.com/send?phone=[phone 1]",
        {"contact_phone": 1},
        id="whatsapp-web-host",
    ),
    pytest.param(
        "https://app.example.com/call?text=hi&phone=212612345678",
        "https://app.example.com/call?text=hi&phone=[phone 1]",
        {"contact_phone": 1},
        id="phone-query-any-host",
    ),
    pytest.param(
        "tel:212612345678",
        "tel:[phone 1]",
        {"contact_phone": 1},
        id="tel-link-bare-digits",
    ),
    # Regional and short profile hosts, and a handle that a label or a scheme settles.
    pytest.param(
        "https://ma.linkedin.com/in/orla-vance",
        "https://ma.linkedin.com/in/[handle 1]",
        {"contact_handle": 1},
        id="linkedin-regional",
    ),
    pytest.param(
        "https://web.facebook.com/orla.vance",
        "https://web.facebook.com/[handle 1]",
        {"contact_handle": 1},
        id="facebook-web",
    ),
    pytest.param(
        "https://fr-fr.facebook.com/orla.vance",
        "https://fr-fr.facebook.com/[handle 1]",
        {"contact_handle": 1},
        id="facebook-locale",
    ),
    pytest.param(
        "https://fb.com/orla.vance",
        "https://fb.com/[handle 1]",
        {"contact_handle": 1},
        id="fb-com",
    ),
    pytest.param(
        "https://fb.me/orla.vance",
        "https://fb.me/[handle 1]",
        {"contact_handle": 1},
        id="fb-me",
    ),
    pytest.param(
        "https://m.me/orla.vance",
        "https://m.me/[handle 1]",
        {"contact_handle": 1},
        id="messenger-link",
    ),
    pytest.param(
        "https://t.me/s/orlavance",
        "https://t.me/s/[handle 1]",
        {"contact_handle": 1},
        id="telegram-preview",
    ),
    pytest.param(
        "https://www.facebook.com/profile.php?id=100012345678",
        "https://www.facebook.com/profile.php?id=[handle 1]",
        {"contact_handle": 1},
        id="facebook-profile-id",
    ),
    pytest.param(
        "tg://resolve?domain=orlavance",
        "tg://resolve?domain=[handle 1]",
        {"contact_handle": 1},
        id="telegram-scheme",
    ),
    pytest.param(
        "skype:orlavance?call",
        "skype:[handle 1]?call",
        {"contact_handle": 1},
        id="skype-scheme",
    ),
    pytest.param(
        "IG: orla.vance",
        "IG: [handle 1]",
        {"contact_handle": 1},
        id="instagram-label",
    ),
    pytest.param(
        "Telegram: orlavance",
        "Telegram: [handle 1]",
        {"contact_handle": 1},
        id="telegram-label",
    ),
    pytest.param(
        "discord: orlavance",
        "discord: [handle 1]",
        {"contact_handle": 1},
        id="discord-label",
    ),
    # Marks around an address are no part of it, and they do not save it.
    pytest.param(
        "Orla -orla.vance@gmail.example",
        "Orla -[email 1]",
        {"contact_email": 1},
        id="dash-before",
    ),
    pytest.param(
        "Contact:-orla.vance@gmail.example",
        "Contact:-[email 1]",
        {"contact_email": 1},
        id="label-dash-before",
    ),
    pytest.param(
        "+orla@gmail.example",
        "+[email 1]",
        {"contact_email": 1},
        id="plus-before",
    ),
    pytest.param(
        "%orla@gmail.example",
        "%[email 1]",
        {"contact_email": 1},
        id="percent-before",
    ),
    pytest.param(
        "...orla@gmail.example",
        "...[email 1]",
        {"contact_email": 1},
        id="dots-before",
    ),
    pytest.param(
        "orla.vance@gmail.example-Orla",
        "[email 1]-Orla",
        {"contact_email": 1},
        id="dash-after-word",
    ),
    pytest.param(
        "orla.vance@gmail.example- she is the lead",
        "[email 1]- she is the lead",
        {"contact_email": 1},
        id="dash-after-space",
    ),
    pytest.param(
        "orla.vance@gmail.example-",
        "[email 1]-",
        {"contact_email": 1},
        id="dash-at-end",
    ),
    pytest.param(
        "orla@gmail.com-0612345678",
        "[email 1]-[phone 1]",
        {"contact_email": 1, "contact_phone": 1},
        id="dash-then-phone",
    ),
    # A handle that reads like code or a file stays masked where only a person fits.
    pytest.param(
        "ask @karim.js and @dr.jane.md",
        "ask [handle 1] and [handle 2]",
        {"contact_handle": 2},
        id="handle-with-a-file-suffix",
    ),
    pytest.param(
        "@447700900123 and @212612345678",
        "[handle 1] and [handle 2]",
        {"contact_handle": 2},
        id="phone-length-mentions",
    ),
    pytest.param(
        "@2pac and @9lives",
        "[handle 1] and [handle 2]",
        {"contact_handle": 2},
        id="digit-led-names",
    ),
    pytest.param(
        "@415.555.0132 hi",
        "[handle 1] hi",
        {"contact_handle": 1},
        id="dotted-phone-mention",
    ),
    pytest.param(
        "ping `@orla` on Telegram",
        "ping `[handle 1]` on Telegram",
        {"contact_handle": 1},
        id="handle-in-backticks",
    ),
    pytest.param(
        "12345+tamsin@users.noreply.github.com",
        "[email 1]",
        {"contact_email": 1},
        id="github-noreply-holds-a-login",
    ),
    pytest.param(
        "ask @a-very-long-login-name-of-thirty-five-chars",
        "ask [handle 1]",
        {"contact_handle": 1},
        id="handle-past-39-characters",
    ),
]


@pytest.mark.parametrize("text, expected, counts", CONTACTS)
def test_contact_identifiers(text, expected, counts):
    assert screen_one(screener(), text) == (expected, counts)


def test_an_encoded_contact_is_one_value_with_its_plain_form():
    text = (
        "orla@brightwell-mail.example or orla%40brightwell-mail.example, "
        "+212612345678 or %2B212612345678"
    )
    assert screen_one(screener(), text) == (
        "[email 1] or [email 1], [phone 1] or %2B[phone 1]",
        {"contact_email": 2, "contact_phone": 2},
    )


def test_an_own_address_stays_when_a_link_encodes_it():
    text = "https://x.example/?e=me%40grove-dev.example"
    assert screen_one(screener(own=("me@grove-dev.example",)), text) == (text, {})


# The forms that glue a value to a word character. Every class must read through each of them.
GLUE_FORMS = [
    pytest.param("*{}*", id="bold-control"),
    pytest.param("~{}~", id="strikethrough-control"),
    pytest.param("_{}_", id="italics"),
    pytest.param("*_{}_*", id="bold-italics"),
    pytest.param("__{}__", id="double-underscore"),
    pytest.param("see _{}_ today", id="italics-mid-sentence"),
    pytest.param("(_{}_).", id="italics-in-parentheses"),
    pytest.param("x\\n{}\\ny", id="json-newline"),
    pytest.param("x\\t{}\\ty", id="json-tab"),
    pytest.param("x\\r\\n{}\\r\\ny", id="json-crlf"),
    pytest.param("x\\u00a0{}\\u00a0y", id="json-unicode-escape"),
    pytest.param("x\\\\n{}\\\\ny", id="json-nested-twice"),
    pytest.param("x\\\\\\\\n{}\\\\\\\\ny", id="json-nested-three-times"),
]
MASKED_VALUES = [
    pytest.param(OPENAI_KEY, {"credential": 1}, id="openai-key"),
    pytest.param(AWS_KEY_ID, {"credential": 1}, id="aws-key-id"),
    pytest.param(GITHUB_TOKEN, {"credential": 1}, id="github-token"),
    pytest.param(SLACK_TOKEN, {"credential": 1}, id="slack-token"),
    pytest.param(JWT, {"credential": 1}, id="jwt"),
    pytest.param(CARD_VISA, {"payment_card": 1}, id="card-grouped"),
    pytest.param(CARD_DIGITS, {"payment_card": 1}, id="card-digits"),
    pytest.param("+212 612 345 678", {"contact_phone": 1}, id="phone-international"),
    pytest.param("0612345678", {"contact_phone": 1}, id="phone-local"),
    pytest.param("(415) 555-0132", {"contact_phone": 1}, id="phone-parentheses"),
    pytest.param("@orlavance", {"contact_handle": 1}, id="handle"),
    pytest.param("tamsin.reyes@brightwell-mail.example", {"contact_email": 1}, id="email"),
]


@pytest.mark.parametrize("form", GLUE_FORMS)
@pytest.mark.parametrize("value, counts", MASKED_VALUES)
def test_no_glue_form_hides_a_masked_value(form, value, counts):
    kept, found = screen_one(screener(), form.format(value))
    assert found == counts
    assert value not in kept


@pytest.mark.parametrize("form", GLUE_FORMS)
@pytest.mark.parametrize(
    "text, value, cls",
    [
        pytest.param("Verification code: 482910", "482910", "one_time_code", id="phrase-code"),
        pytest.param("your code is 482910", "482910", "one_time_code", id="your-code-is"),
        pytest.param("the code is 482910.", "482910", "one_time_code", id="code-ends-a-sentence"),
        pytest.param(
            "https://app.example/reset?token=Zx81kQ9vLm2Pq7Rt",
            "Zx81kQ9vLm2Pq7Rt",
            "reset_link",
            id="reset-link",
        ),
        pytest.param(
            "https://app.example/magic/Zx81kQ9vLm2Pq7Rt",
            "Zx81kQ9vLm2Pq7Rt",
            "reset_link",
            id="magic-link",
        ),
    ],
)
def test_no_glue_form_hides_a_code_or_a_link(form, text, value, cls):
    kept, counts = screen_one(screener(), form.format(text))
    assert counts == {cls: 1}
    assert kept is None or value not in kept


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("Your code:\\n482910\\nthanks", id="code-in-the-next-escaped-line"),
        pytest.param("Verification code\\n482910\\nexpires", id="phrase-then-escaped-line"),
        pytest.param(
            'a JSON tool result: {"stdout": "Your code:\\n482910\\ndone"}', id="tool-output"
        ),
    ],
)
def test_a_code_after_an_escaped_line_break_drops(text):
    assert screen_one(screener(), text) == (None, {"one_time_code": 1})


@pytest.mark.parametrize(
    "text, expected, counts",
    [
        pytest.param(
            "https:\\/\\/app.example\\/magic\\/Zx81kQ9vLm2Pq7Rt",
            None,
            {"reset_link": 1},
            id="magic-link",
        ),
        pytest.param(
            "https:\\\\\\/\\\\\\/app.example\\\\\\/magic\\\\\\/Zx81kQ9vLm2Pq7Rt",
            None,
            {"reset_link": 1},
            id="magic-link-nested-twice",
        ),
        pytest.param(
            '{"url":"https:\\/\\/app.example\\/reset\\/Zx81kQ9vLm2Pq7Rt","ok":true}',
            None,
            {"reset_link": 1},
            id="reset-link-in-json",
        ),
        pytest.param(
            f"https:\\/\\/hooks.slack.com\\/services\\/{SLACK_WEBHOOK_PATH.replace('/', '\\/')}",
            "https:\\/\\/hooks.slack.com\\/services\\/[credential]",
            CRED,
            id="webhook-path",
        ),
        pytest.param(
            f"postgres:\\/\\/grove:{DB_SECRET}@localhost",
            "postgres:\\/\\/grove:[credential]@localhost",
            CRED,
            id="url-userinfo",
        ),
        pytest.param(
            "https:\\/\\/t.me\\/orlavance",
            "https:\\/\\/t.me\\/[handle 1]",
            {"contact_handle": 1},
            id="profile-link",
        ),
        pytest.param(
            "https:\\/\\/wa.me\\/212612345678",
            "https:\\/\\/wa.me\\/[phone 1]",
            {"contact_phone": 1},
            id="whatsapp-link",
        ),
    ],
)
def test_a_slash_escape_hides_no_value_of_a_link(text, expected, counts):
    assert screen_one(screener(), text) == (expected, counts)


def test_a_tool_result_that_holds_json_as_a_string():
    """The realistic form: text that holds a JSON dump, so each line break is two characters."""
    text = json.dumps(
        {
            "stdout": f"Charged card\n{CARD_VISA}\nCall\n0612345678\n{AWS_KEY_ID}\nCC\n@orlavance\n"
            f"API_KEY={OPENAI_KEY}\ndone",
            "exit": 0,
        }
    )
    kept, counts = screen_one(screener(), text)
    for value in (CARD_VISA, "0612345678", AWS_KEY_ID, "orlavance", OPENAI_KEY):
        assert value not in kept
    assert counts == {
        "credential": 2,
        "payment_card": 1,
        "contact_phone": 1,
        "contact_handle": 1,
    }


def test_a_dotted_handle_goes_in_every_glue_form():
    assert screen_one(screener(), "_@orla.vance_") == ("_[handle 1]_", {"contact_handle": 1})
    # As written, this one also reads as the address n@orla.vance, and the earlier span wins.
    kept, counts = screen_one(screener(), "x\\n@orla.vance\\ny")
    assert "orla" not in kept
    assert counts == {"contact_email": 1}


@pytest.mark.parametrize(
    "text, expected",
    [
        pytest.param(
            '{\\"password\\": \\"correct horse\\"}',
            '{\\"password\\": \\[credential]\\"}',
            id="escaped-quotes-keep-the-whole-value",
        ),
        pytest.param(
            '{\\"api_key\\": \\"abcd1234\\"}',
            '{\\"api_key\\": \\[credential]\\"}',
            id="escaped-quotes-around-a-name",
        ),
        pytest.param("_API_KEY=abcd1234_", "_API_KEY=[credential]_", id="italic-assignment"),
        pytest.param(
            "*Password:* hunter2x", "*Password:* [credential]", id="bold-label-with-colon"
        ),
        pytest.param(
            "_Password:_ hunter2x", "_Password:_ [credential]", id="italic-label-with-colon"
        ),
        pytest.param("*Password*: hunter2x", "*Password*: [credential]", id="bold-label"),
        pytest.param(
            "**API_KEY**: abcd1234", "**API_KEY**: [credential]", id="markdown-bold-label"
        ),
        pytest.param("`API_KEY`: abcd1234", "`API_KEY`: [credential]", id="code-span-label"),
    ],
)
def test_a_glue_form_hides_no_assignment(text, expected):
    assert screen_one(screener(), text) == (expected, CRED)


@pytest.mark.parametrize(
    "text, expected, counts",
    [
        # Reading alone would cut each of these in two and store a part of it.
        pytest.param(
            "here it is sk-proj-Ab3_-dEf6hIj9kLm2nOp5qRs8tUv1wXy4z",
            "here it is [credential]",
            CRED,
            id="key-that-holds-underscore-hyphen",
        ),
        pytest.param(
            "password: a*_bcde1 ok",
            "password: [credential]",
            CRED,
            id="password-star-underscore",
        ),
        pytest.param(
            "api_key: abc\\ndef1 ok", "api_key: [credential] ok", CRED, id="escape-inside"
        ),
        pytest.param(
            f"postgres://grove:_{DB_SECRET}@localhost/x",
            "postgres://grove:[credential]@localhost/x",
            CRED,
            id="userinfo-that-starts-with-underscore",
        ),
        pytest.param(
            "write to tamsin_@brightwell-mail.example",
            "write to [email 1]",
            {"contact_email": 1},
            id="address-that-ends-its-local-part-with-underscore",
        ),
    ],
)
def test_a_value_that_holds_a_glue_form_goes_whole(text, expected, counts):
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
    pytest.param("salary _25000 MAD_", "salary _[amount]_", id="italic-amount"),
    pytest.param("salary _25k_", "salary _[amount]_", id="italic-k-suffix"),
    pytest.param("salary\\n25000 MAD\\nthanks", "salary\\n[amount]\\nthanks", id="escaped-line"),
    # A long figure stays an amount under the bound on its groups.
    pytest.param("a raise to 1 200 000 EUR", "a raise to [amount]", id="three-space-groups"),
    pytest.param(
        "a raise to 1,000,000,000,000,000,000 USD", "a raise to [amount]", id="six-comma-groups"
    ),
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
# The forms a person writes pay in, each with the text it must become.
PAY_FORMS = [
    # A bare figure before a period.
    ("25000 a month", "[amount] a month"),
    ("25000 monthly", "[amount] monthly"),
    ("25000 every month", "[amount] every month"),
    ("25000/month", "[amount]/month"),
    ("25000 / mois", "[amount] / mois"),
    ("25000/mois", "[amount]/mois"),
    ("25000 par mois", "[amount] par mois"),
    ("25000 per mo", "[amount] per mo"),
    ("300000 a year", "[amount] a year"),
    ("300000 per year", "[amount] per year"),
    ("300000 par an", "[amount] par an"),
    ("he is paid 25000 a month", "he is paid [amount] a month"),
    ("I pay Tamsin 25000 every month", "I pay Tamsin [amount] every month"),
    # Nothing tells this one from "25000 a month", so the opt-in class masks it (SC10).
    ("we ingest 25000 a month", "we ingest [amount] a month"),
    # A bare figure right after a weak pay word.
    ("a bonus of 5000", "a bonus of [amount]"),
    ("bonus: 5000", "bonus: [amount]"),
    ("bonus is 5000", "bonus is [amount]"),
    ("a raise of 5000", "a raise of [amount]"),
    ("my rate is 300", "my rate is [amount]"),
    ("rate: 3000", "rate: [amount]"),
    ("day rate 3000", "day rate [amount]"),
    ("paid 25000", "paid [amount]"),
    ("he is paid 25000", "he is paid [amount]"),
    ("paying 25000", "paying [amount]"),
    ("we are paying him 25000", "we are paying him [amount]"),
    ("pay me 25000", "pay me [amount]"),
    ("I pay Tamsin 25000", "I pay Tamsin [amount]"),
    ("she makes 25000", "she makes [amount]"),
    ("I make 25000", "I make [amount]"),
    ("makes 25,000", "makes [amount]"),
    ("she makes 2000", "she makes [amount]"),
    ("pay me 2000", "pay me [amount]"),
    # A free gap after a weak pay word.
    ("the rate we agreed is 3000", "the rate we agreed is [amount]"),
    ("they pay me roughly 25000", "they pay me roughly [amount]"),
    ("my bonus was only 5000", "my bonus was only [amount]"),
    ("bonus this year is 5000", "bonus this year is [amount]"),
    # Nothing tells this one from a sum, so the opt-in class masks it (SC10).
    ("make the timeout 3000", "make the timeout [amount]"),
    # A figure of four digits or more after a near word.
    ("base 120000", "base [amount]"),
    ("net 18000", "net [amount]"),
    ("offer: 120000", "offer: [amount]"),
    ("he gets 25000", "he gets [amount]"),
    ("prime de 5000", "prime de [amount]"),
    # What may follow that figure.
    ("paid 25000 this year", "paid [amount] this year"),
    ("paid 25000, thanks", "paid [amount], thanks"),
    ("rate: 3000\nsee the sheet", "rate: [amount]\nsee the sheet"),
    # A bare figure a few words after a strong pay word.
    ("salary raised to 25000", "salary raised to [amount]"),
    ("salary was raised to 25000", "salary was raised to [amount]"),
    ("salary - 25000", "salary - [amount]"),
    ("salary -> 25000", "salary -> [amount]"),
    ("salary at 25000", "salary at [amount]"),
    ("my salary in 2026 was 25000", "my salary in 2026 was [amount]"),
    ("my salary is 2000", "my salary is [amount]"),
    # Words that say pay, each with an amount that names its money or has a suffix.
    ("total comp 250k", "total comp [amount]"),
    ("TC is $250k", "TC is [amount]"),
    ("base 120k", "base [amount]"),
    ("OTE 150k", "OTE [amount]"),
    ("offer: 120k", "offer: [amount]"),
    ("they offered me $120k", "they offered me [amount]"),
    ("take home is 18k", "take home is [amount]"),
    ("take-home 18000", "take-home [amount]"),
    ("package 300k", "package [amount]"),
    ("package of 300000 dirhams", "package of [amount]"),
    ("he gets 25000 dh", "he gets [amount]"),
    ("he gets 25000 dh tomorrow", "he gets [amount] tomorrow"),
    ("net 18000 dh", "net [amount]"),
    ("5000 bucks a month", "[amount] a month"),
    ("5 grand a month", "[amount] a month"),
    # French and Arabic.
    ("je suis pay\u00e9 25000 dh", "je suis pay\u00e9 [amount]"),
    ("pay\u00e9e 25000 dh", "pay\u00e9e [amount]"),
    ("il gagne 25k", "il gagne [amount]"),
    ("je touche 25000 dh", "je touche [amount]"),
    ("ils m'ont propos\u00e9 30000 dh brut", "ils m'ont propos\u00e9 [amount] brut"),
    ("r\u00e9mun\u00e9ration 25000 dh", "r\u00e9mun\u00e9ration [amount]"),
    ("remuneration: 25000 MAD", "remuneration: [amount]"),
    ("prime de 5000 dh", "prime de [amount]"),
    ("augmentation de 2000 dh", "augmentation de [amount]"),
    ("ma paye est de 25000", "ma paye est de [amount]"),
    ("salaire dyali 25000", "salaire dyali [amount]"),
    ("TJM 3000", "TJM [amount]"),
    (
        "\u0631\u0627\u062a\u0628\u064a 25000 \u062f\u0631\u0647\u0645",
        "\u0631\u0627\u062a\u0628\u064a [amount]",
    ),
    ("\u0627\u0644\u0631\u0627\u062a\u0628 25000", "\u0627\u0644\u0631\u0627\u062a\u0628 [amount]"),
    # A range leaves no end behind.
    ("25,000-30,000 MAD salary", "[amount] salary"),
    ("salary 25000-30000", "salary [amount]"),
    ("salary 25000 to 30000", "salary [amount]"),
    ("25 000 - 30 000 MAD a month", "[amount] a month"),
    ("salary between 25000 and 30000 MAD", "salary between [amount]"),
    (
        "They offered 25,000-30,000 MAD salary for the role",
        "They offered [amount] salary for the role",
    ),
    ("Salary range is 25 000 - 30 000 MAD a month", "Salary range is [amount] a month"),
    ("MAD 25,000 - 30,000 per month", "[amount] per month"),
    ("de 25000 \u00e0 30000 dh par mois", "de [amount] par mois"),
    ("25-30k salary", "[amount] salary"),
    ("$25k-$30k salary", "[amount] salary"),
    ("paid 25000-30000 last year", "paid [amount] last year"),
    ("paid 25000 and 3 days off", "paid [amount] and 3 days off"),
]
PAY += [pytest.param(text, expected, id=text) for text, expected in PAY_FORMS]


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
    pytest.param("git@heroku.com:brightwell2.git", id="remote-of-the-git-user"),
    pytest.param("rsync -a . deploy@box.example.com:/var/www", id="scp-absolute-path"),
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
    # A bare figure that the words around it do not make pay, with the class on.
    pytest.param("the rate limit is 3000 per minute", id="rate-limit-per-minute"),
    pytest.param("rate limit: 3000", id="rate-limit-with-a-label"),
    pytest.param("rate: 3000 req/s", id="rate-with-a-unit"),
    pytest.param("rate is 300%", id="rate-in-percent"),
    pytest.param("salary up 300% and 500 %", id="salary-in-percent"),
    pytest.param("makes 5000 requests a day", id="makes-then-a-noun"),
    pytest.param("raises 5000 errors", id="raises-then-a-noun"),
    pytest.param("paid plan has 5000 seats", id="paid-plan"),
    pytest.param("pay attention to line 300.", id="pay-attention"),
    pytest.param("paid in 2026.", id="year-after-paid"),
    pytest.param("the bonus for 2025.", id="year-after-bonus"),
    pytest.param("1200 tests a month", id="figure-then-a-noun-then-a-period"),
    pytest.param("rate: 3000/s", id="figure-per-second"),
    pytest.param("the package has 300k downloads", id="package-with-other-words"),
    pytest.param("package 300k downloads", id="package-then-a-noun"),
    pytest.param("make sure the port is 8000.", id="make-sure-the-port"),
    pytest.param("base 256", id="base-then-a-figure"),
    pytest.param("net 443", id="net-then-a-figure"),
    pytest.param("the request gets 200", id="gets-then-a-figure"),
    pytest.param("tc 3000 and ote 3000 and tjm 3000", id="short-words-in-lower-case"),
    pytest.param("the salary 2026 budget", id="year-after-salary"),
    pytest.param("salary slip #48213", id="reference-after-salary"),
    pytest.param("salary in /tmp/300/x", id="path-after-salary"),
    pytest.param("salary 3.12345", id="decimal-after-salary"),
    pytest.param("salary PO-2026-00417", id="order-number-after-salary"),
    pytest.param("salary is late and the five other things 300", id="figure-far-from-salary"),
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
    # The guards hold after a value started to run on past its first symbol.
    pytest.param("password={password}", id="template-with-braces"),
    pytest.param("DB_PASSWORD={db_password}", id="env-template"),
    pytest.param("password: <your-password>", id="placeholder"),
    pytest.param("password: [REDACTED]", id="redacted"),
    pytest.param("the password is (optional)", id="optional-in-brackets"),
    pytest.param("password=%(password)s", id="format-placeholder"),
    pytest.param("token=get_token()", id="a-call"),
    pytest.param("connect(password=password,user=user)", id="same-name-then-next-argument"),
    pytest.param("connect(password=pw,user=user)", id="short-name-then-next-argument"),
    pytest.param("token = uuid4()", id="callee-with-a-digit"),
    pytest.param("password = base64.b64encode(raw)", id="dotted-callee-with-a-digit"),
    pytest.param("token: Optional[str] = None", id="generic-annotation"),
    pytest.param("secret: Record<string, string>", id="ts-generic"),
    pytest.param("password: str | None = None", id="union-annotation"),
    pytest.param("password: None  # no auth on the dev store", id="none-with-a-comment"),
    pytest.param("token = oauth2.token", id="reference-with-a-digit-to-a-token"),
    pytest.param("secret = s3.secret", id="reference-with-a-digit-to-a-secret"),
    pytest.param("password=$password", id="lower-case-variable"),
    pytest.param("token=$token", id="lower-case-variable-token"),
    pytest.param("API_KEY=$API_KEY", id="upper-case-variable"),
    pytest.param("API_KEY=${API_KEY}", id="braced-variable"),
    pytest.param("    api_key=settings.api_key,", id="reference-in-a-call"),
    pytest.param("if password == confirm:", id="a-comparison"),
    pytest.param("token::Token", id="a-path-in-code"),
    pytest.param('cypher-shell -u neo4j -p "$NEO4J_PASSWORD"', id="client-flag-variable"),
    pytest.param("docker login -u me --password-stdin", id="password-stdin"),
    pytest.param("docker run -p 8080:80 img", id="docker-maps-a-port"),
    pytest.param("psql -h db -p 5432 -U orla", id="psql-names-a-port"),
    pytest.param("redis-cli -h cache -p 6379", id="redis-names-a-port"),
    pytest.param("mysql -u root -p grove_db", id="mysql-names-a-database"),
    pytest.param("--token is required, see --password", id="flags-in-prose"),
    pytest.param("fields = ['password', 'email']", id="a-list-of-field-names"),
    pytest.param(
        "- name: API_KEY\n  valueFrom:\n    secretKeyRef:\n      name: api\n      key: apiKey",
        id="value-from-a-secret",
    ),
    pytest.param("password:\n  user: orla", id="a-mapping-under-the-name"),
    pytest.param("- name: API_KEY\n  value: ${API_KEY}", id="value-is-a-variable"),
    pytest.param("BYPASS=abcd1234 and compass: north", id="words-that-end-in-pass"),
    pytest.param("OLDPWD=/tmp/grove2", id="old-working-folder"),
    # Dev talk beside the password word, where no value follows.
    pytest.param("password sha256 hashing is done in the writer", id="password-sha256"),
    pytest.param("password oauth2 flow needs a redirect", id="password-oauth2"),
    pytest.param("password reset-link expires in 1h", id="password-reset-link"),
    pytest.param("pwd /tmp/grove", id="working-folder"),
    pytest.param("we moved the password to argon2id", id="password-to-an-algorithm"),
    pytest.param("the password expiry is 3600 seconds", id="password-expiry-with-a-unit"),
    pytest.param("password policy: min 8 chars", id="password-policy"),
    pytest.param("the password for prod is in the vault", id="password-is-in-a-place"),
    pytest.param("login: https://app.example.com", id="login-url"),
    pytest.param("login: db.example.com:8080", id="login-host-and-port"),
    pytest.param("log in with google / github", id="log-in-with-providers"),
    # A cookie line with no secret, and a session id outside one.
    pytest.param("Cookie: theme=dark; lang=en", id="plain-cookies"),
    pytest.param(
        "Set-Cookie: theme=dark; Path=/v2/items; Domain=app2.example.com; "
        "Expires=2026-11-02T00:00:00Z; Max-Age=3600",
        id="cookie-attributes",
    ),
    pytest.param("Cookie: side=left", id="cookie-name-that-holds-sid"),
    pytest.param("git checkout -b my-branch", id="git-branch-flag"),
    pytest.param("see session_012XQTypAwWMW7QG7EmGeu5j for the run", id="session-id-in-a-link"),
    pytest.param("authorization: required for v2", id="authorization-in-prose"),
    pytest.param("Authorization: Bearer $TOKEN", id="authorization-variable"),
    # A model API's ids and token counts stay beside a context word.
    pytest.param(
        '{"id":"msg_01ABCDefGHIjkLMnoPQRstUV","type":"message",'
        '"usage":{"input_tokens":12,"output_tokens":300}}',
        id="message-id-beside-counters",
    ),
    pytest.param(
        "request_id=req_011CabcDEFghiJKLmno123 input_tokens=1200 output_tokens=300",
        id="request-id-beside-counters",
    ),
    pytest.param(
        "API Error: 529 overloaded, request_id: req_011CabcDEFghiJKLmno123", id="api-error"
    ),
    pytest.param(
        '{"tool_use_id":"toolu_01ABcdEFghIJklMNopQRstUV","content":"token limit reached"}',
        id="tool-id-beside-the-word-token",
    ),
    pytest.param(
        '{"id":"chatcmpl-AbCdEfGhIjKlMnOpQrStUvWxYz012",'
        '"usage":{"prompt_tokens":12,"completion_tokens":5}}',
        id="completion-id-beside-counters",
    ),
    pytest.param(
        '{"call_id":"call_AbCdEfGhIjKlMnOpQrStUvWx","output":"token limit"}', id="call-id"
    ),
    pytest.param(
        '{"response_id":"resp_0193f2a47b3c7d2e8f1a2b3c4d5e6f70","usage":{"input_tokens":12}}',
        id="response-id-hex-tail",
    ),
    pytest.param("session_012XQTypAwWMW7QG7EmGeu5j used 1200 tokens", id="session-id-and-a-count"),
    pytest.param("the token limit hit on msg_013ZvaZCMHLNnXjNJJKqJ2EF, retrying", id="id-in-prose"),
    pytest.param("run 01HZX3K9Q4M7V2P8R6T1Y5W0NB used input_tokens=12", id="ulid-beside-a-counter"),
    pytest.param("run 01HZX3K9Q4M7V2P8R6T1Y5W0NB used 1,200 tokens", id="ulid-beside-a-count"),
    # A path or a route with capitals and a digit is no key, and "/api/" is no context word.
    pytest.param("see web/src/components/v2/TokenPanel.tsx for the token refresh", id="next-path"),
    pytest.param("the api route lives in src/app/api/v1/SourceList.tsx", id="next-api-route"),
    pytest.param("docs/D14/API-keys-plan.md", id="doc-path-with-keys"),
    pytest.param("GET /api/v2/Items/ListAll returns 200", id="api-route"),
    pytest.param(
        "the api design is in thoughts/runs/2026-11-01-grove-build/lanes/screening/DESIGN.md",
        id="capitalised-file-name",
    ),
    pytest.param("GET /api/v2/Kq8vN2xP7mZ4tR9wLm3s returns 200", id="id-in-an-api-route"),
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
    # A group that is only ")" before a "(" group, or before a run of more than 15 digits.
    pytest.param("f(1 ) (2)", id="call-with-spaced-parentheses"),
    pytest.param("1 ) (2", id="lone-paren-group"),
    pytest.param("0 ) (123", id="lone-paren-group-leading-zero"),
    pytest.param("step 1 ) (2 items)", id="list-with-spaced-parentheses"),
    pytest.param("see (1 ) (2 ) (3)", id="three-spaced-parentheses"),
    pytest.param("value = table( 1 ) ( 2 );", id="subscript-with-spaces"),
    pytest.param("7 ) 1234567890123456", id="lone-paren-before-long-run"),
    # "_" between two word characters joins them, so none of these holds a value.
    pytest.param("__init__ and __repr__", id="dunder-names"),
    pytest.param("self._client_key and self._cache_", id="private-names"),
    pytest.param("run_4111111111111111", id="digit-run-in-a-name"),
    pytest.param("id_0612345678", id="phone-shape-in-a-name"),
    pytest.param("user_@orlavance", id="handle-shape-in-a-name"),
    pytest.param(f"task_{OPENAI_KEY}", id="key-shape-in-a-name"),
    pytest.param("cost_25000 MAD and salary_25k and test_25k_rows", id="amount-shape-in-a-name"),
    pytest.param("_next/static/chunks/main.js and __tests__/login.spec.ts", id="markup-in-a-path"),
    # A redacted value is no value, and the next line is not read as one.
    pytest.param("API_KEY=***\nNEXT_NAME=1", id="redacted-value"),
    pytest.param("*args and **kwargs and char* name and a*b", id="stars-in-code"),
    # A slash escape in dev text reads as a slash.
    pytest.param("/^https?:\\/\\/(www\\.)?example\\.com\\/$/", id="javascript-regex"),
    pytest.param('{"path":"src\\/grove\\/screening\\/names.py"}', id="escaped-path"),
    pytest.param('"@types\\/node": "22.1.0"', id="escaped-npm-scope"),
    pytest.param("on 02\\/11\\/2026", id="escaped-date"),
    pytest.param(
        "https:\\/\\/github.com\\/org\\/repo\\/blob\\/3f2a9c1d8e7b6a5f4e3d2c1b0a9f8e7d6c5b4a39\\/x.py",
        id="escaped-code-host-page",
    ),
    # An escape in dev text reads as what it is.
    pytest.param('{"log": "line one\\nline two\\n"}', id="escaped-line-breaks"),
    pytest.param('print(\\"hello\\\\n\\")', id="escaped-quotes-in-code"),
    pytest.param("@pytest.fixture\\n@login_required\\ndef f():\\n    return 1", id="escaped-code"),
    pytest.param("C:\\temp\\new\\run_1234", id="windows-path"),
    # A file path is no bare host.
    pytest.param("see grove.screening.py/detectors and auth.py/tokens", id="file-path-is-no-host"),
    # A number beside a sign-in word that names something else, or has no purpose beside it.
    pytest.param("Use 5432 to connect to Postgres.", id="use-port-to-connect"),
    pytest.param("use 3000 to run the dev server", id="use-port-to-run"),
    pytest.param("Use 1200 as the limit.", id="use-as-a-limit"),
    pytest.param("Enter 8080 as the port to sign in to the dev box", id="enter-as-the-port"),
    pytest.param("Use port 5432 to log in to Postgres", id="use-port-word-to-log-in"),
    pytest.param("use 1200 lines max", id="use-with-a-unit"),
    pytest.param("use 2026 as the year", id="use-a-year"),
    pytest.param("use 8080 for the api", id="use-for"),
    pytest.param("type 8080 in the port field", id="type-in-a-field"),
    pytest.param("Log in with the admin user, then hit 8000.", id="log-in-with-a-word"),
    pytest.param("the test user can log in, 1934 pass", id="log-in-then-a-count"),
    # How long a thing lasts is no code by itself.
    pytest.param("The token expires in 3600 seconds.", id="token-expires-in"),
    pytest.param("The session expires in 1800.", id="session-expires-in"),
    pytest.param("Valid for 1200 requests.", id="valid-for-requests"),
    pytest.param(
        "use the login flow from PR 1234, it expires in 5 minutes", id="expires-beside-a-pr"
    ),
    pytest.param("run 1234 is valid for 10 minutes", id="valid-beside-a-run"),
    pytest.param("expires in 2026", id="expires-in-a-year"),
    # A count with a noun, whatever the phrase before it.
    pytest.param("the WhatsApp code parsed 8400 messages", id="whatsapp-messages"),
    pytest.param("The WhatsApp code: 2300 messages in 14 chats", id="whatsapp-label-messages"),
    pytest.param("do we need the telegram code? it pulled 5000 msgs", id="telegram-msgs"),
    pytest.param("the google code in the calendar connector reads 1200 events", id="google-events"),
    pytest.param("the recovery code restored 1200 snapshots", id="recovery-snapshots"),
    pytest.param("launch code ready, 4200 added", id="launch-code-far"),
    pytest.param("le code est 1200 lignes", id="french-code-lines"),
    pytest.param("votre code est propre", id="french-your-code-prose"),
    # A name is no code: an algorithm or a format right after a phrase.
    pytest.param("we store the OTP sha256 only", id="otp-sha256"),
    pytest.param("sign the OTP HS256 with the key", id="otp-hs256"),
    pytest.param("the OTP x509 cert", id="otp-x509"),
    # A reference before "is your code".
    pytest.param("PR 1234 is your code, right?", id="pr-is-your-code"),
    pytest.param("line 1420 is your code", id="line-is-your-code"),
    pytest.param("issue #4821 is your code to fix", id="issue-is-your-code"),
    # A code page is no link, whatever phrase the text says.
    pytest.param(
        "The magic link bug is fixed in https://github.com/X-Arc-ai/grove/pull/14", id="phrase-pr"
    ),
    pytest.param(
        "The page says verify your email. PR: https://github.com/X-Arc-ai/grove/pull/15",
        id="verify-pr",
    ),
    pytest.param(
        "Magic link ticket is here https://linear.app/x-arc/issue/LUX-123/magic-link",
        id="phrase-ticket",
    ),
    pytest.param(
        "reset your password in the CLI: https://github.com/X-Arc-ai/grove/blob/main/README.md",
        id="strong-phrase-code-page",
    ),
    # Common words with a URL that does not follow them.
    pytest.param(
        "Sign in to Vercel and open https://vercel.com/x-arc/grove/deployments",
        id="sign-in-then-other-words",
    ),
    pytest.param(
        "sign in to staging first: https://staging.grove.x-arc.ai/", id="sign-in-two-words"
    ),
    pytest.param(
        "we need a screen to set a new password, design is at https://figma.com/file/abc/grove",
        id="set-a-new-password-far",
    ),
    pytest.param(
        "after signup the user must confirm your email, docs: https://docs.example.com/auth",
        id="confirm-far",
    ),
    # A slug of words that ends in a short id, and the pages with a rule of their own.
    pytest.param(
        "Good read: https://medium.com/@tamsin/how-to-reset-a-neo4j-password-3f2a9c1e7b4d",
        id="medium-reset",
    ),
    pytest.param(
        "https://towardsdatascience.com/activation-functions-explained-3f2a9c1e7b4d",
        id="blog-activation",
    ),
    pytest.param(
        "Notes: https://www.notion.so/xarc/Login-flow-notes-3f2a9c1e7b4d5a6f8e0c1b2a3d4f5e6a",
        id="notion-page",
    ),
    pytest.param(
        "https://www.notion.so/Auth-rework-plan-0193f2a47b3c7d2e8f1a2b3c4d5e6f70",
        id="notion-root-page",
    ),
    pytest.param(
        "curl -O https://raw.githubusercontent.com/nextauthjs/next-auth/3f2a9c1e7b4d5a6f8e0c1b2a3d4f5e6a7b8c9d0e/README.md",
        id="raw-sha",
    ),
    pytest.param("https://www.figma.com/file/AbCdEfGhIjKlMnOpQrStUv/Login-flow", id="figma-file"),
    pytest.param(
        "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQrStUvWxYz0123456/edit#heading=h.auth",
        id="google-doc",
    ),
    # A client id is public, so an authorize link with no other value is no token.
    pytest.param(
        "https://github.com/login/oauth/authorize?client_id=Iv1.8a61f9b3a7aba766&scope=repo",
        id="authorize-client-id",
    ),
    # Contact shapes that name no person.
    pytest.param("v1.0612345678", id="version-with-a-long-part"),
    pytest.param("1.0612345678", id="decimal-with-a-long-part"),
    pytest.param("x1-0612345678", id="digit-before-a-dash"),
    pytest.param("runs/0123456789", id="path-digit-run"),
    pytest.param("1,000,000,000", id="thousands"),
    pytest.param("s3://b/2026/0123456789/0123456789", id="path-of-digit-runs"),
    pytest.param("foo-0.1.20240101", id="package-with-a-dated-build"),
    pytest.param("https://api.x.com/v2/tweets", id="api-subdomain"),
    pytest.param("https://docs.x.com/overview", id="docs-subdomain"),
    pytest.param("https://developers.facebook.com/docs/graph-api", id="facebook-docs"),
    pytest.param("https://developer.x.com/en/docs", id="x-docs"),
    pytest.param("Telegram: the bot is ready", id="service-label-in-prose"),
    pytest.param("Telegram: 12 messages", id="service-label-and-a-count"),
    pytest.param("ig: that's fine", id="slang-label"),
    pytest.param("git@github.com-work:org/repo.git", id="ssh-host-alias"),
    pytest.param("deploy@git.brightwell.example-work:org/repo.git", id="ssh-host-alias-any-host"),
    pytest.param("ssh deploy@web.prod-01", id="numbered-host"),
    pytest.param("deploy@web.prod-01:/var/log", id="numbered-host-path"),
    pytest.param("read @CLAUDE.md and @docs/store-map.md first", id="file-mention-capitals"),
    pytest.param("look at @README.md", id="readme-mention"),
    pytest.param(
        "check @package.json and @pyproject.toml and @detectors.py", id="file-mention-suffix"
    ),
    pytest.param("see @detectors.py:1051", id="file-mention-with-line"),
    pytest.param("@CLAUDE.md\nsummarise it", id="file-mention-first-on-line"),
    pytest.param("use @agent-code-reviewer on this", id="agent-mention"),
    pytest.param("Standup @10:30 tomorrow", id="at-a-time"),
    pytest.param("call @3pm, lunch @1pm, demo @14:30", id="at-times"),
    pytest.param("Meeting @9am", id="at-am"),
    pytest.param("2 seats @20 each", id="at-a-price"),
    pytest.param("5 units @120 each, total 600", id="at-a-unit-price"),
    pytest.param("Rdv @15h", id="at-french-hour"),
    pytest.param("Reunion demain @14h30", id="at-french-time"),
    pytest.param("upgrade to @1.2.3 and pin @latest", id="version-and-tag"),
    pytest.param("installed next @15.0.3", id="at-a-version"),
    pytest.param("use the logo @2x", id="at-a-scale"),
    pytest.param("add `@retry` to the function", id="named-decorator-in-backticks"),
    pytest.param(
        "use @field_validator and @model_validator here", id="pydantic-decorators-in-prose"
    ),
    pytest.param(
        "the @pytest decorator, @mcp.tool and @tenacity.retry", id="decorator-modules-in-prose"
    ),
    pytest.param("wrap it in @login_required", id="login-required"),
    pytest.param('server/grove/x.py:12:@field_validator("name")', id="grep-decorator-call"),
    pytest.param(
        "server/grove/x.py:42:    @retry(stop=stop_after_attempt(3))", id="grep-decorator-indented"
    ),
    pytest.param("42:@retry(stop=3)", id="line-number-decorator-call"),
    pytest.param("use @retry(stop=3) here", id="decorator-call-in-prose"),
    pytest.param("the `@cached(ttl=60)` line", id="decorator-call-in-backticks"),
    pytest.param(
        "the read runs under `@unit_of_work(timeout=...)` in tx.py", id="writer-decorator"
    ),
    pytest.param(
        "the Modal class uses @modal.enter(snap=True) and @modal.web_server(8000)",
        id="runner-decorators",
    ),
    pytest.param("Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>", id="commit-trailer"),
    pytest.param('user = make_user(email="test@example.com")', id="placeholder-address-in-code"),
    pytest.param(
        "docs say to use user@example.com as the placeholder", id="placeholder-address-in-prose"
    ),
    pytest.param("ssh -T git@github.com", id="ssh-user-git"),
    pytest.param("git clone ssh://git@github.com/X-Arc-ai/grove.git", id="ssh-url-user-git"),
    pytest.param("git@github.com: Permission denied (publickey).", id="ssh-error-line"),
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
