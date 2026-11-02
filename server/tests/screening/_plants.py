"""Synthetic plants for the screening tests. Invented names only; no value is real.

Keys are joined at run time from parts, so no secret scanner reads a whole key in the repo, the
same way the name lint builds its words.
"""

from pathlib import Path

from grove.screening import Denylist, RenameMap, Screener, ScreeningConfig


def joined(*parts: str) -> str:
    return "".join(parts)


OPENAI_KEY = joined("sk-", "proj-", "Ab3dEf6hIj9kLm2nOp5qRs8tUv1wXy4z")
ANTHROPIC_KEY = joined("sk-", "ant-", "api03-", "Zx9Qw8Er7Ty6Ui5Op4As3Df2Gh1Jk0Lz")
AWS_KEY_ID = joined("AKIA", "QX7RT2LMNB4VZC8D")
GITHUB_TOKEN = joined("gh", "p_", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8")
GITHUB_PAT = joined("github", "_pat_", "11ABCDEFG0123456789_abcdefghijklmnop")
SLACK_TOKEN = joined("xo", "xb-", "123456789012-abcdefABCDEF")
STRIPE_KEY = joined("sk", "_live_", "4eC39HqLyjWDarjtT1zdp7dc")
GOOGLE_KEY = joined("AI", "za", "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q")
GITLAB_TOKEN = joined("gl", "pat-", "xYz12AbC34dEf56GhI78")
HF_TOKEN = joined("hf", "_", "AbCdEfGhIjKlMnOpQrStUvWxYz012345")
NPM_TOKEN = joined("npm", "_", "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789")
JWT = joined(
    "eyJ", "hbGciOiJIUzI1NiJ9", ".", "eyJ", "zdWIiOiJvcmxhLXZhbmNlIn0", ".", "dozjgNryP4J3jVmNHl0w"
)
PEM_BLOCK = joined(
    "-----BEGIN OPENSSH ",
    "PRIVATE KEY-----\n",
    "b3BlbnNzaC1rZXktdjEAAAAABG5vbmU\n",
    "QyNTUxOQAAACBzeW50aGV0aWM\n",
    "-----END OPENSSH ",
    "PRIVATE KEY-----",
)
PGP_BLOCK = joined(
    "-----BEGIN PGP ",
    "PRIVATE KEY BLOCK-----\n\n",
    "lQOYBGVkY2UBCADKq8vN2xP7mZ4tR9wLm3sXaHj5bVc8nQw2eRt6yUi9oPa3sDf7\n",
    "=AbCd\n",
    "-----END PGP ",
    "PRIVATE KEY BLOCK-----",
)
GITHUB_OAUTH_TOKEN = joined("gh", "o_", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8")
GITHUB_SERVER_TOKEN = joined("gh", "s_", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8")
# The other providers a chat or a dev session shows, each in its real shape.
PROVIDER_KEYS = {
    "sendgrid": joined(
        "SG", ".", "Kq8vN2xP7mZ4tR9wLm3sXa", ".", "Hj5bVc8nQw2eRt6yUi9oPa3sDf7gHk1lZx4cVb0nMq8"
    ),
    "google-oauth-token": joined(
        "ya", "29.", "a0AfB_byC8Kq8vN2xP7mZ4tR9wLm3sXaHj5bVc8nQw2eRt6yUi9oPa3sDf7gHk1lZx4cVb0nMq8"
    ),
    "google-oauth-secret": joined("GOCS", "PX-", "Kq8vN2xP7mZ4tR9wLm3sXaHj5bVc"),
    "digitalocean": joined("dop", "_v1_", "3f9a8c7d6e5b4a3f2e1d0c9b8a7f6e5d" * 2),
    "atlassian": joined(
        "ATA",
        "TT",
        "3xFfGF0Kq8vN2xP7mZ4tR9wLm3sXaHj5bVc8nQw2eRt6yUi9oPa3sDf7gHk1lZx4cVb0nMq8=A1B2C3D4",
    ),
    "groq": joined("gs", "k_", "Kq8vN2xP7mZ4tR9wLm3sXaHj5bVc8nQw2eRt6yUi9oPa3sDf"),
    "replicate": joined("r8", "_", "Kq8vN2xP7mZ4tR9wLm3sXaHj5bVc8nQw2eRt6"),
    "stripe-webhook": joined("wh", "sec_", "Kq8vN2xP7mZ4tR9wLm3sXaHj5bVc8nQw"),
    "perplexity": joined("pp", "lx-", "Kq8vN2xP7mZ4tR9wLm3sXaHj5bVc8nQw2eRt6yUi9oPa"),
}
SLACK_WEBHOOK_PATH = joined("T01ABCD2EFG/", "B02HIJK3LMN/", "aBcDeFgHiJkLmNoPqRsTuVwX")
DISCORD_WEBHOOK_PATH = joined(
    "1234567890123456789/", "aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789-_abcdefghijklmnopqr"
)
ENTROPY_TOKEN = "aB3dE5fG7hJ9kL1mN3pQ5rS7tU9wX"
DB_SECRET = "hunter2pass"

CARD_VISA = "4111 1111 1111 1111"
CARD_DIGITS = "4111111111111111"

THIRD_EMAIL = "tamsin.reyes@brightwell-mail.example"
THIRD_PHONE = "+44 7700 900123"
THIRD_PHONE_DIGITS = "447700900123"

OWN_EMAIL = "me@grove-dev.example"
OWN_PHONE = "+212 600 000 111"
OWN_HANDLE = "@juniper_k"

# The rename map and the denylist of the tests. "Orla" and "Orla Vance" share a label, so the
# longest form must win at a position for "Vance" to go too.
RENAME = {"Orla Vance": "Co-founder", "Orla": "Co-founder", "O. Vance": "Co-founder"}
DENYLIST = ["Brightwell Logistics", "Halvard Quist"]


def luhn_check_digit(prefix: str) -> str:
    """The digit that makes prefix + digit pass Luhn. Written here, apart from the code."""
    total = 0
    for index, ch in enumerate(reversed(prefix)):
        value = int(ch)
        if index % 2 == 0:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return str((10 - total % 10) % 10)


def screener(
    *,
    rename: dict[str, str] | None = None,
    denylist: list[str] | None = None,
    own: tuple[str, ...] = (),
    pay: bool = False,
) -> Screener:
    return Screener(
        ScreeningConfig(
            rename=RenameMap.from_mapping(rename or {}),
            denylist=Denylist(denylist or []),
            own_identifiers=frozenset(own),
            sensitive_classes=frozenset({"pay_amount"} if pay else ()),
        )
    )


def screen_one(screen: Screener, text: str, *, sender: str | None = None):
    """The screened text (None when the message was dropped) and the counts."""
    screened = screen.screen_text(text, sender=sender)
    kept = screened.messages[0].text if screened.messages else None
    return kept, dict(screened.counts)


def private_file(directory: Path, name: str, content: str | bytes, mode: int = 0o600) -> Path:
    path = directory / name
    path.write_bytes(content.encode() if isinstance(content, str) else content)
    path.chmod(mode)
    return path
