"""The code detectors of D14 screening. None calls a model.

Each detector takes a text and returns Hit spans in its offsets. The screener merges the spans of
every step and applies them. A Hit's repr shows its offsets and class, never the value.

False positives are kept low on Grove's own dev text: git shas, UUIDs, dates, times, versions,
decorators, npm scopes and order numbers stay untouched. Where code cannot tell, the candidate is
treated as sensitive (spec line 2552): it is masked or its message is dropped.
"""

import base64
import math
import re
import unicodedata
from bisect import bisect_left, bisect_right
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from itertools import chain
from urllib.parse import SplitResult, unquote, urlsplit

from grove.screening.classes import (
    CONTACT_EMAIL,
    CONTACT_HANDLE,
    CONTACT_PHONE,
    CREDENTIAL,
    ONE_TIME_CODE,
    PAY_AMOUNT,
    PAYMENT_CARD,
    RESET_LINK,
)


@dataclass(frozen=True)
class Hit:
    start: int
    end: int
    cls: str
    # Contacts only: the value as compared, which numbers distinct values. Never in a repr.
    key: str = field(default="", repr=False)


def _hits(cls: str, spans: Iterable[tuple[int, int]]) -> list[Hit]:
    return [Hit(start, end, cls) for start, end in spans if end > start]


def ascii_digits(text: str) -> str:
    """The decimal digits of a text as ASCII, so full-width and other scripts compare equal."""
    return "".join(str(unicodedata.decimal(ch)) for ch in text if ch.isdecimal())


# A scheme starts where a run of scheme characters starts, and is short: "a.a.a.a..." would
# otherwise try a scheme from every word, which is quadratic.
_SCHEME = r"(?<![a-z0-9+.-])[a-z][a-z0-9+.-]{0,31}://"
# "|" ends a URL: Slack writes <url|label>.
_URL = re.compile(rf"(?i)(?:{_SCHEME}|(?<![a-z0-9.-])www\.)[^\s<>\"'`|]+")
_URL_TAIL = ".,;:!?)]}'\"*\\"


def urls(text: str) -> Iterator[tuple[int, int]]:
    for match in _URL.finditer(text):
        yield match.start(), match.start() + len(match.group().rstrip(_URL_TAIL))


# Marks before an address are no part of it ("-orla@x.example", "Email:-orla@x.example"). They are
# read as one run from the start of the token, so "a.a.a..." stays linear. A link writes "@" as %40.
_EMAIL = re.compile(
    r"(?<![\w.+%-])[.+%-]*+(?P<address>[A-Za-z0-9][A-Za-z0-9._%+-]*(?:@|%40)"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*"
    r"\.[A-Za-z]{2,}(?!\w))"
)


class _Cover:
    """The characters some spans cover. A check costs the length of the span asked about, not the
    number of spans, so a long text with many hits stays linear."""

    def __init__(self, length: int, spans: Iterable[tuple[int, int]]) -> None:
        self._marks = bytearray(length)
        for start, end in spans:
            self._marks[start:end] = b"\x01" * (end - start)

    def holds(self, start: int, end: int) -> bool:
        return 0 not in self._marks[start:end]

    def touches(self, start: int, end: int) -> bool:
        return 1 in self._marks[start:end]


# Credentials ----------------------------------------------------------------------------------

_PEM = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----.*?"
    r"(?:-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|\Z)",
    re.DOTALL,
)
# Known key formats. The left edge refuses a word character or a hyphen, so "task-..." holds no
# "sk-" key. A long tail of words is a name ("vercel_project_settings_file"), so the prefixes that
# read like a word ask for a capital and a digit in the tail.
_RANDOM_TAIL = r"(?=[A-Za-z0-9_-]*[A-Z])(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{20,}"
_PREFIXED = re.compile(
    r"(?<![A-Za-z0-9_-])(?:"
    r"sk-[A-Za-z0-9_-]{20,}"
    r"|(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Za-z0-9])"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|xox[a-z](?:\.xox[a-z])?-[A-Za-z0-9%-]{10,}"
    r"|xapp-\d-[A-Za-z0-9-]{10,}"
    r"|[sr]k_(?:live|test)_[A-Za-z0-9]{10,}"
    r"|AIza[A-Za-z0-9_-]{30,}"
    r"|glpat-[A-Za-z0-9_-]{20,}"
    r"|hf_[A-Za-z0-9]{30,}"
    r"|npm_[A-Za-z0-9]{30,}"
    r"|eyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"
    r"|SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{30,}"
    r"|ya29\.[A-Za-z0-9_-]{20,}"
    r"|GOCSPX-[A-Za-z0-9_-]{20,}"
    r"|dop_v1_[0-9a-f]{64}"
    r"|ATATT[A-Za-z0-9_=-]{40,}"
    r"|gsk_[A-Za-z0-9]{40,}"
    r"|r8_[A-Za-z0-9]{30,}"
    r"|whsec_[A-Za-z0-9+/=]{24,}"
    r"|pplx-[A-Za-z0-9]{40,}"
    r"|xai-[A-Za-z0-9]{20,}"
    r"|shp(?:at|ss|ca|pa)_[0-9a-fA-F]{32}"
    r"|sbp_[0-9a-f]{40}"
    r"|dapi[0-9a-f]{32}"
    r"|pul-[0-9a-f]{40}"
    r"|SK[0-9a-f]{32}"
    r"|[0-9a-f]{32}-us\d{1,2}"
    r"|lsv2_(?:pt|sk)_[0-9a-f]{32}_[0-9a-f]{10}"
    r"|pypi-AgE[A-Za-z0-9_-]{30,}"
    r"|gl(?:rt|dt|ptt|soat|cbt)-[A-Za-z0-9_-]{20,}"
    r"|hvs\.[A-Za-z0-9_-]{20,}"
    r"|atlasv1-[A-Za-z0-9_-]{20,}"
    r"|dp\.(?:pt|st|ct|sa)\.[A-Za-z0-9_.-]{20,}"
    r"|AGE-SECRET-KEY-1[0-9A-Z]{40,}"
    r"|EAA[A-Z][A-Za-z0-9]{30,}"
    r"|IGQVJ[A-Za-z0-9_-]{30,}"
    rf"|(?:ntn_|re_|vercel_|vcp_|sntrys_|tgp_v1_|fo1_){_RANDOM_TAIL}"
    # Telegram and Discord bot tokens.
    r"|(?:bot)?\d{8,10}:AA[A-Za-z0-9_-]{30,}"
    r"|[MNO][A-Za-z0-9_-]{22,}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{25,}"
    r")"
)
# A webhook URL is its own secret: the host stays, the path after it goes.
_WEBHOOK = re.compile(
    r"(?i)(?:hooks\.slack\.com/(?:services|workflows|triggers)/"
    r"|discord(?:app)?\.com/api/webhooks/|webhook\.office\.com/webhookb2/)"
    r"(?P<value>[A-Za-z0-9/_@-]{16,})"
)
_BEARER = re.compile(r"(?i)(?<![A-Za-z])bearer(?:\s+|\s*[:=]\s*)(?P<value>[A-Za-z0-9._~+/=-]{8,})")
# "Authorization: Basic <base64 of user:password>".
_BASIC = re.compile(
    r"(?i)(?<![A-Za-z])basic\s+(?P<value>[A-Za-z0-9+/]{8,}={0,2})(?![A-Za-z0-9+/=_-])"
)
# scheme://user:secret@host keeps the user and the host; the user may be empty (redis://:x@h).
_USERINFO = re.compile(rf"(?i){_SCHEME}[^\s:/@]*:(?P<value>[^\s/@]+)@")
# curl -u user:secret and curl --user user:secret.
_CURL_USER = re.compile(
    r"(?i)(?<![\w-])curl(?![\w-])[^\n]{0,400}?(?<![\w-])(?:-u|--user)(?:\s+|=)[\"']?[^\s:\"']*:"
    r"(?P<value>[^\s\"']+)"
)
_QUERY_SECRET = re.compile(
    r"(?i)[?&](?:api[_-]?key|apikey|key|token|access_token|auth|secret|password|pwd|sig"
    r"|signature|x-amz-signature|x-amz-security-token|x-goog-signature)"
    r"=(?P<value>[^&#\s]+)"
)
# An environment suffix after the secret word: API_KEY_PROD, SECRET_KEY_BASE, token_v2.
_SUFFIX = (
    r"(?:PROD|PRODUCTION|LIVE|TEST|DEV|STAGING|STAGE|BASE|RO|RW|OLD|NEW|PRIMARY|SECONDARY|BACKUP"
    r"|V?\d{1,3})"
)
# Words that end like a glued secret word and say nothing about a secret.
_NOT_GLUED = r"(?:(?:by|com|encom|sur|tres|over|under|multi)pass|oldpwd)(?![a-z0-9])"
# A name that says secret: MODEL_API_KEY, GH_TOKEN, NEO4J_AUTH; api_key, client_secret, password;
# apiKey, clientSecret, accessToken; a word glued to it: PGPASSWORD, authtoken, dbPass.
_SECRET_NAME = (
    # The repeats are bounded, so a long dotted chain costs linear time, not quadratic.
    r"(?:[A-Z0-9]+_){1,6}(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASS|PWD|AUTH|CREDENTIALS?|COOKIE)"
    rf"(?:_{_SUFFIX}){{0,3}}"
    r"|(?:TOKEN|SECRET|PASSWORD|PASSWD)"
    r"|(?:[A-Za-z0-9]+[_.-]){0,6}(?i:api[_-]?key|apikey|secret|secret[_-]?key|token|password|passwd"
    r"|pwd|passphrase|private[_-]?key|access[_-]?key|credentials?|session[_-]?cookie"
    rf"|(?!{_NOT_GLUED})[a-z0-9]+(?:password|passwd|pass|pwd|token|secret))"
    rf"(?:[_-](?i:{_SUFFIX})){{0,3}}"
    r"|(?:api|secret|private|access|client|auth|signing|encryption)Key"
    # In a connection string, which the URL rule reads as one URL.
    r"|AccountKey|SharedAccessKey"
)
# The bare group is only the value's first piece, so a name inside the rest is still read. The value
# itself comes from _value_end. "==" and "=>" compare or map, and "::" is a path in code.
_ASSIGNMENT = re.compile(
    rf"(?<![A-Za-z0-9_])[\"']?(?P<name>{_SECRET_NAME})[\"']?\]?"
    r"(?P<sep>\s*(?::=|[:=](?![:=>]))\s*)"
    r"(?:\"(?P<dq>[^\"\n]+)\"|'(?P<sq>[^'\n]+)'|`(?P<bq>[^`\n]+)`"
    r"|(?P<bare>[^\s\"'`,;|()\[\]{}<>]*))"
)
# Words and types that a bare value can be in code, never a secret.
_NOT_A_VALUE = frozenset(
    "none null nil true false undefined required self this await async new yield lambda "
    "function return string str bytes int bool boolean number any unknown object optional "
    "secretstr secretbytes".split()
)
# form.value, process.env.X, Settings.API_KEY: a name, not a value. A digit among letters or a
# capital after the dot is how a password looks: hunter2.Bank1, rabat.Summer2026.
_CODE_REFERENCE = re.compile(
    r"(?:[a-z_$][a-z_$]*|[A-Z][a-z]+(?:[A-Z][a-z]+)*)(?:\.[a-z_$][a-z_$]*)*"
    r"\.(?:[a-z_$][A-Za-z_$]*|[A-Z][A-Z_]*)"
)
# settings.neo4j_password, oauth2.token, s3.secret: the last part says what it holds.
_REFERENCE_TO_SECRET = re.compile(
    r"[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*\.[A-Za-z0-9_$]*"
    r"(?i:key|token|secret|pass|passwd|password|pwd|pw|auth|credentials?)[A-Za-z0-9_$]*"
)
# get_token(), os.environ["X"], Optional[str]. A callee has at most one short run of digits (uuid4,
# sha256), and the text after the bracket closes it or runs on past the space: Tr0ub(4)dor3,
# abcd1234(x) and Summer(2024)! are values.
_CALLEE = r"(?:[A-Za-z_$][A-Za-z_$]*\d{0,3}|[a-z_]+\d{1,3}[a-z_]*)"
_CODE_CALL = re.compile(rf"{_CALLEE}(?:\.{_CALLEE})*[(\[<](?:[^)\]>]*|.*[)\]>])")
# $NAME, ${x}, $(x), $password, %(x)s. $uperS3cret and $ecret1234 are values.
_SHELL_REFERENCE = re.compile(r"\$(?:\{|\(|[A-Z_][A-Z0-9_]*$|[a-z_]+$)|%\(")
# SecretStr, ApiKey, AccessToken: a type in an annotation.
_TYPE_NAME = re.compile(r"(?:[A-Z][a-z]+)+(?:Str|String|Bytes|Type|Field|Key|Token|Secret|Value)")
# The .env form NAME=value at the start of a line: the value runs to the end of the line.
_ENV_LINE = re.compile(r"(?m)^[ \t]*(?:export[ \t]+)?$")
_ENV_LOOK_BACK = 40
_TRAILING_COMMENT = re.compile(r"[ \t]+#.*$|[ \t]+$")
# "my password is X", "pw: X", "the pin is 4821". A PIN is digits, so "the pin is 2026.10.1" is
# a version.
# French and Darija too: "le mot de passe est X", "le mdp c'est X", "password dyali: X",
# "l mdp dyal wifi howa X".
_PHRASE = re.compile(
    r"(?i)(?<![A-Za-z])(?:"
    r"(?P<word>password|passphrase|passwd|passcode|api[ \t]+key|secret\s+key|mot\s+de\s+passe|mdp"
    r"|wi-?fi\s+code|code\s+wi-?fi)"
    r"|(?P<weak>pw|pwd|pass)"
    r"|(?P<pin>pin(?:\s+code)?)"
    r")(?![A-Za-z])"
    r"(?:\s+dyal\w{0,2}(?:\s+[\w-]{1,20})??)?"
    r"(?P<sep>\s+(?:is|was|est|c['\u2019]est|howa)\s*+:?\s*+|\s*[:=]\s*+|\s+[-\u2013\u2014]+>?\s++"
    r"|\s*(?:->|=>|\u2192)\s*+)"
    r"(?:[\"'\u201c\u2018](?P<quoted>[^\"'\u201d\u2019\n]+)[\"'\u201d\u2019]"
    r"|(?P<value>[^\s\"'\u201c\u201d,;|()\[\]{}<>]*))"
)
_LABEL_SEP = re.compile(r"[:=>\u2192\u2013\u2014-]")
# "PIN 4821", "pin4821", "PIN-4821", "CVV: 123": no word between the label and the digits.
_BARE_PIN = re.compile(
    r"(?i)(?<![A-Za-z])(?:pin(?:\s+code)?[\s:=-]*(?P<pin>\d{4,12})"
    r"|(?:cvv2?|cvc2?|csc)[\s:=-]*(?P<cvv>\d{3,4}))(?!\w)(?!\.\d)"
)
# "password Tr0ub4dor3", "pwd Tr0ub4dor3": with only a space, the value must pass _mixed. The value
# sits in a lookahead, so "password password X" reads the second word too.
_SPACED_PASSWORD = re.compile(
    r"(?i)(?<![A-Za-z])(?:password|passwd|pwd|mot\s+de\s+passe|mdp)\s++"
    r"(?=(?P<value>[^\s\"'`]\S{3,}))"
)
# "the password for prod is X", "password to the admin panel: X", "I set the password to X": a few
# words may stand between. Only a value that must be one ends the search: a digit or a symbol.
_WORDS_THEN_VALUE = re.compile(
    r"(?i)(?<![A-Za-z])(?P<word>passwords?|passwd|passphrase|passcode|pwd|mot\s+de\s+passe|mdp)"
    r"(?![A-Za-z])\??(?:"
    r"(?:[ \t]+[\w'-]{1,20}){0,6}?[ \t]+(?:is|are|was|were|be|to|it'?s|est|c'est|howa)[ \t]+"
    # With no word between, a label is _ASSIGNMENT's: "password=x,user=y" is not one value.
    r"|(?:[ \t]+[\w'-]{1,20}){1,6}?[ \t]*[:=][ \t]*"
    r")(?=\S*[\d!@#$%^&*+=~?])(?P<value>\S+)"
)
# "the password for the wifi is lemonade": one plain word, after words that say which password.
# Only a preposition opens them, because "password manager is great" is a compound noun.
_PASSWORD_FOR_THEN_PLAIN = re.compile(
    r"(?i)(?<![A-Za-z])(?:passwords?|passwd|passphrase|passcode|pwd|mot\s+de\s+passe|mdp)[ \t]++"
    r"(?:for|of|to|on|at|de|du|pour)(?:[ \t]++[\w'-]{1,20}){1,3}?[ \t]++(?:is|was|est)[ \t]++"
    r"(?P<value>[A-Za-z-]{4,})(?=[.,;!?)]?(?:\s|$))"
)
_AND_MORE = re.compile(r"(?:[ \t]*,[ \t]*|[ \t]+(?:and|et)[ \t]+)(?P<value>\S+)")
_MORE_VALUES = 8
# "user orla pass X", "u: orla p: X".
_USER_THEN_SECRET = re.compile(
    r"(?i)(?<![A-Za-z])(?:"
    r"(?:user(?:name)?|login|identifiant)[ \t]*[:=]?[ \t]*\S{1,256}+[ \t]+(?:pass|pw|pwd)(?!\w)"
    r"[ \t]*[:=]?"
    r"|u[ \t]*:[ \t]*\S{1,256}+[ \t]+p[ \t]*:"
    r")[ \t]*(?P<value>\S+)"
)
_PHRASE_STOP = frozenset(
    "wrong correct incorrect invalid expired required missing empty reset changed saved stored "
    "the not now still very here there below above attached same different fine okay weak "
    "strong long short secure safe encrypted hashed visible shown hidden case-sensitive being "
    "never always also just only what that this your our their written listed included known "
    "unknown shared sent default rotated revoked too gone ready live active blank unset needed "
    "optional private public broken fixed set good bad new old simple per-world per-user "
    "enough sensitive valid from with inside under over then when where because since like "
    "plaintext bcrypt mandatory automatic unique available random temporary longer shorter "
    "stronger weaker easy hard none null true false".split()
)
# A key or a token is never one plain word, so a plain word after "the API key is" is prose ("the
# API key is configured") unless it is said as someone's own. A password can be one ("the password
# is sunshine"), and code cannot tell it from prose past the stop list and the prose endings, so it
# is sensitive (spec line 2552). The limit is one word of a sentence, such as "the password is
# plenty".
_PASSWORD_WORD = re.compile(r"(?i)pass(?:word|wd|phrase|code)s?|pwd|mot\s+de\s+passe|mdp")
_PLAIN_WORD = re.compile(r"[a-z]+(?:-[a-z]+)*|[A-Z][a-z]+(?:-[a-z]+)*")
_OWNER = re.compile(r"(?i)(?:(?<![a-z])(?:my|our|your|his|her|their)\s+(?:[a-z-]+\s+)?|wi-?fi\s+)$")
_PROSE_ENDING = re.compile(r"(?:ing|ed|ly)$")
# "login: orla / Tr0ub4dor3", "creds are admin:hunter2", "log in with admin / hunter2". The user
# part is as long as an email address at most, and it is never given back: "login=login=..." would
# rescan to the end of the text from each start, which is quadratic. A ":" pair takes no space, and
# a number or "//" after it is a port or a URL: "login: host:8080", "login: https://x".
_LOGIN_PAIR = re.compile(
    r"(?i)(?<![A-Za-z])(?:"
    r"(?:logins?|creds?|credentials?)(?:[ \t]*+[:=]|[ \t]++(?:are|is|was)(?!\w))"
    r"|(?P<spoken>log[ \t]+in[ \t]+with)"
    r")[ \t]*+[^\s/:]{1,256}+(?:[ \t]*+/[ \t]*+|:(?![/\s]))"
    r"(?P<value>[^\s\"',;]+)"
)
# Forms with no "=" or ":" after the name. Each holds its value in the group "value", and
# _VALUE_FORMS below says what each value must pass.
_DOUBLE_QUOTED = r"\"[^\"\n]+\""
_SINGLE_QUOTED = r"'[^'\n]+'"
_QUOTED = rf"{_DOUBLE_QUOTED}|{_SINGLE_QUOTED}"
# --password X, --token X, --api-key X.
_SECRET_FLAG = re.compile(
    r"(?<![\w-])--(?:[a-z0-9]+-){0,3}(?:password|passwd|pass|pwd|token|secret|api-?key|secret-key"
    r"|access-key)[ \t]+(?P<value>[^\s-]\S*)"
)
# Clients that take the password as a flag. "-p" means something else in others: docker run maps a
# port, psql and redis-cli name a port. "mysql -p db" names a database, so mysql's must be glued.
_CLIENT_FLAG = re.compile(
    r"(?<![\w-])(?:"
    r"(?:sshpass|(?:docker|podman)[ \t]+login|cypher-shell|mongosh|mongo)(?:[ \t]+\S+){0,8}?"
    r"[ \t]+-p[ \t]*"
    r"|(?:mysql|mysqldump|mariadb)(?:[ \t]+\S+){0,8}?[ \t]+-p"
    r"|redis-cli(?:[ \t]+\S+){0,8}?[ \t]+-a[ \t]*"
    r")(?P<value>[^\s-]\S*)"
)
# ALTER USER x WITH PASSWORD 'y', CREATE USER x IDENTIFIED BY 'y', SET PASSWORD 'y'.
_SQL_PASSWORD = re.compile(
    r"(?i)(?<![A-Za-z])(?:password|passwd|identified[ \t]+by(?:[ \t]+password)?)[ \t]+"
    rf"(?P<value>{_QUOTED})"
)
# auth=("neo4j", "y"), HTTPBasicAuth("orla", "y"), login("orla", "y").
_AUTH_PAIR = re.compile(
    r"(?:(?<!\w)auth[ \t]*=[ \t]*[(\[]|(?i:basic_?auth|login)[ \t]*\()[ \t]*"
    rf"(?:{_QUOTED})[ \t]*,[ \t]*(?P<value>{_QUOTED})"
)
# cfg.get("password", "y"), os.getenv("DB_PASSWORD", "y").
_NAME_THEN_STRING = re.compile(
    rf"(?<![A-Za-z0-9_])[\"'](?:{_SECRET_NAME})[\"'][ \t]*,[ \t]*(?P<value>{_QUOTED})"
)
# <password>y</password>, <apiKey>y</apiKey>.
_SECRET_TAG = re.compile(rf"<(?P<tag>{_SECRET_NAME})>(?P<value>[^<\n]+)</(?P=tag)>")
# The name on one field and the value on the next, in YAML on two lines, in JSON or an HTML input
# on one: "- name: DB_PASSWORD\n  value: y", {"name": "API_KEY", "value": "y"}.
_NAME_VALUE_PAIR = re.compile(
    r"(?<![\w-])(?i:name)[\"']?[ \t]*[:=][ \t]*[\"']?"
    rf"(?:{_SECRET_NAME})[\"']?(?:[^\n]{{0,60}}?|[ \t,;]*\n[ \t]*(?:-[ \t]*)?)"
    r"(?<![\w-])[\"']?(?i:value)[\"']?[ \t]*[:=][ \t]*"
    rf"(?P<value>{_QUOTED}|[^\s,}}\]>]+)"
)
# "password: >" and "password: |" hold the value on the next line.
_YAML_BLOCK = re.compile(
    rf"(?<![A-Za-z0-9_])(?:{_SECRET_NAME}):[ \t]*[|>][+-]?[ \t]*\n[ \t]+(?P<value>[^\n]+)"
)
# "orla@x.example:Tr0ub4dor3", the way a person shares an account. The user git is a remote.
_EMAIL_PAIR = re.compile(rf"(?!(?i:git)@){_EMAIL.pattern}:(?P<value>[^\s:/]\S*)")
# "admin:hunter2@db.example.com:5432/app" with no scheme. A label before an address is no user.
_BARE_USERINFO = re.compile(
    r"(?<![\w./:@=;-])"
    r"(?!(?i:e-?mail|mailto|mail|courriel|from|to|cc|bcc|contact|reply-to|sender|address):)"
    r"[\w.+-]{1,64}:(?P<value>[^\s:/@]{4,256})@(?:[\w-]+\.)+[\w-]+"
)
# On an Authorization line any scheme counts: Token, Digest, or none.
_AUTH_HEADER = re.compile(
    r"(?i)(?<![\w-])authorization[\"']?[ \t]*[:=][ \t]*[\"']?(?:[A-Za-z]+[ \t]+)?"
    r"(?P<value>[A-Za-z0-9._~+/=:-]{8,})"
)
# A Cookie header, curl -b and curl --cookie. The body is bounded and consumed, so a long line of
# these stays linear. "-b" alone is also git's flag, so it needs a pair after it.
_COOKIE_HEADER = re.compile(
    r"(?i)(?<![\w-])(?:(?:set-)?cookie[ \t]*:|--cookie|-b(?=[ \t]+[\"']?[^\s\"'=]+=))[ \t=]*[\"']?"
    r"(?P<pairs>[^\n\"']{0,4096})"
)
_COOKIE_PAIR = re.compile(r"(?<![^\s;,])(?P<name>[^\s=;,]++)=(?P<value>[^\s;,]+)")
_COOKIE_ATTRIBUTES = frozenset("path domain expires".split())
_SESSION_COOKIE = re.compile(
    r"(?i)sess|(?<![a-z])sid(?![a-z])|auth|token|jwt|login|csrf|xsrf|secret"
)
# The words mean a secret anywhere in a name, so the left edge is open: openaiKey, authtoken,
# AccountKey. The entropy test still guards the value. "api" keeps its edge, and "/api/" names a
# route, not a key.
_CONTEXT = re.compile(
    r"(?i)(?:(?:keys?|tokens?|secrets?|passwords?|credentials?)"
    r"|(?<![a-z/])(?:api(?!/)|authorization|jetons?|cl[e\u00e9]f?s?|mots?\s+de\s+passe|mdp))"
    r"(?![a-z])"
)
# A count of tokens says nothing about a key: input_tokens, maxTokens, "1200 tokens".
_TOKEN_COUNTER = re.compile(
    r"(?i)(?:input|output|prompt|completion|total|max|cached|reasoning|cache[a-z_]{0,20})"
    r"[_-]?tokens(?![a-z])"
    r"|(?<![\w.])\d[\d,.]{0,20}[km]?[ \t]+tokens(?![a-z])"
)
# Dev text puts git shas, hashes and ids next to these words all the time, so a hex token, a UUID
# (also one after a prefix such as t_screening_) or a model API's id needs the context word right
# before it: "api key 3f2a...", "the token is 0193...", "DD_APP_KEY_RO=3f2a...".
_PLAIN_ID = re.compile(
    r"[0-9a-f]+|[0-9A-F]+"
    r"|(?:[A-Za-z][A-Za-z0-9]*_)*[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
    r"|(?:msg|req|toolu|srvtoolu|call|resp|session|chatcmpl)[_-][A-Za-z0-9]+"
)
_CONTEXT_JUST_BEFORE = re.compile(
    r"(?i)(?:(?:keys?|tokens?|secrets?|passwords?|credentials?)|(?<![a-z])api)(?![a-z])"
    r"(?:_[a-z0-9]+){0,3}\W*(?:(?:is|was)\W+)?$"
)
_JUST_BEFORE_CHARS = 120
# "=" is base64 padding at the end of a token and never its start, so NAME=value splits into the
# name and the value.
_TOKEN = re.compile(r"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{20,}={0,2}(?![A-Za-z0-9+/=_-])")
ENTROPY_BITS = 3.5


def shannon_bits(token: str) -> float:
    counts = Counter(token)
    return -sum(n / len(token) * math.log2(n / len(token)) for n in counts.values())


def _random_looking(token: str) -> bool:
    classes = sum(
        (
            any(ch.islower() for ch in token),
            any(ch.isupper() for ch in token),
            any(ch.isdigit() for ch in token),
        )
    )
    # A path ("server/grove/x") is not a key; base64 with a slash has all three classes.
    if "/" in token and (classes < 3 or _path_like(token)):
        return False
    return classes >= 2 and shannon_bits(token) >= ENTROPY_BITS


_SEGMENT = re.compile(r"[A-Za-z0-9]+")
_PIECE = re.compile(r"[A-Z]?[a-z]+|[A-Z]+\d*|\d+")
_WORD_PIECE = re.compile(r"(?=[A-Za-z]*[aeiouyAEIOUY])[A-Z]?[a-z]{2,}")
_MANY_CONSONANTS = re.compile(r"(?i)[bcdfghjklmnpqrstvwxz]{5}")
_TAG = re.compile(r"[A-Z]{1,4}\d*")


def _is_word(piece: str) -> bool:
    return _WORD_PIECE.fullmatch(piece) is not None and _MANY_CONSONANTS.search(piece) is None


def wordy(token: str) -> bool:
    """Words, numbers and short tags joined by '-', '_', '.' or camelCase: a model id
    (claude-opus-4-1-20250805), a slug (login-fails-for-oauth2-users), a dated branch, a class
    name (ScreeningReceiptV2Response). A random key mixes classes inside every piece, so it fails
    on a short piece, on a second run of digits in one segment or on a run of consonants."""
    has_word = False
    for segment in re.split(r"[-_.]", token):
        if not segment:
            continue
        if not _SEGMENT.fullmatch(segment):
            return False
        if segment.isdigit():
            continue
        if len(segment) <= 5:
            has_word = has_word or _is_word(segment)
            continue
        digit_runs = 0
        for piece in _PIECE.findall(segment):
            digit_runs += piece[-1].isdigit()
            if _is_word(piece):
                has_word = True
            elif not (piece.isdigit() or _TAG.fullmatch(piece)):
                return False
        if digit_runs > 1:
            return False
    return has_word


def _path_like(token: str) -> bool:
    """Every part reads as words, a number, a short tag or a capitalised file name: web/src/v2/
    TokenPanel, docs/D14/API-keys-plan, lanes/screening/DESIGN. A random part fails, so base64 with
    a slash stays a candidate."""

    def reads(part: str) -> bool:
        if len(part) <= 5 or part.isdigit() or wordy(part):
            return True
        return part.isupper() and part.isalpha() and _is_word(part.capitalize())

    return all(reads(part) for part in token.split("/"))


def _same_name(name: str, value: str) -> bool:
    """api_key=api_key, token=token, self.token = token, apiKey: apiKey."""

    def squash(text: str) -> str:
        return re.sub(r"[_-]", "", text).lower()

    last = re.split(r"[._-]", name)[-1]
    return squash(value) in (squash(name), squash(last))


# Up to here a value is one token. What ends it is the next space, the next field or the line end.
_VALUE_MAX = 256
_VALUE_PIECE = re.compile(r"[^\s\"'`,;|()\[\]{}<>]*")
_NEXT_FIELD = re.compile(
    r"[,;]\s*[\w.-]+\s*[:=]|\s+[\w.-]+[ \t]*[:=](?=\s)|[ \t]+(?:#|//|-{1,2}[A-Za-z])"
)
_WHITESPACE = re.compile(r"\s")
_CLOSERS = {")": "(", "]": "[", "}": "{", ">": "<"}
_OPENERS = frozenset(_CLOSERS.values())
# After these names a label says the rest of the line is the value: a passphrase holds spaces.
_PASSPHRASE_NAME = re.compile(r"(?i)(?:password|passwd|passphrase|passcode|mot\s+de\s+passe|mdp)$")


def _trimmed_end(text: str, start: int, end: int) -> int:
    """Without a trailing ',', ';', space or a closer that opens nowhere in the value."""
    while end > start:
        last = text[end - 1]
        if last in ",; \t\r" or (last in _CLOSERS and _CLOSERS[last] not in text[start:end]):
            end -= 1
        else:
            break
    return end


def _value_end(text: str, start: int, *, to_line_end: bool = False) -> int:
    """Where a bare value ends. A password can hold any symbol, so only a space cuts it, or the
    next field: "P@ss,w0rd9" goes whole, and "password=pw,user=user" stops at the comma. Bounded,
    so a long line of these stays linear."""
    end = min(len(text), start + _VALUE_MAX)
    line_end = text.find("\n", start, end)
    if line_end != -1:
        end = line_end
    next_field = _NEXT_FIELD.search(text, start, end)
    if next_field is not None:
        end = next_field.start()
    if not to_line_end:
        space = _WHITESPACE.search(text, start, end)
        if space is not None:
            end = space.start()
    return _trimmed_end(text, start, end)


def _bare_value_is_secret(name: str, sep: str, value: str, *, env: bool = False) -> bool:
    """False for what code settles as no value: a language word, a reference, a call, a type, or
    the same name passed on. Only the value's first token can settle it. `env` is the .env form,
    which holds no code, so a reference or a call there is a value."""
    if not value.strip():
        return False
    first = value.split(maxsplit=1)[0].rstrip(",;")
    # {password}, <your-password>, [REDACTED] and (optional) are placeholders. A digit among the
    # letters says a value: {Tr0ub4dor3}.
    if first[:1] in _OPENERS:
        return any(ch.isdigit() for ch in value) and any(ch.isalpha() for ch in value)
    piece = _VALUE_PIECE.match(first).group()
    if len(value) < 4 or not piece or first.endswith(":") or piece.lower() in _NOT_A_VALUE:
        return False
    # Only marks: a redaction ("***") or the end of a chat label ("*Password:* x", in the view).
    if not any(ch.isalnum() for ch in first):
        return False
    if _SHELL_REFERENCE.match(first) or _same_name(name, first):
        return False
    if env:
        return True
    if (
        _CODE_CALL.fullmatch(first)
        or _CODE_REFERENCE.fullmatch(first)
        or _REFERENCE_TO_SECRET.fullmatch(first)
    ):
        return False
    # client_secret: SecretStr is an annotation.
    return not (":" in sep and _TYPE_NAME.fullmatch(first))


def _label_runs_to_line_end(name: str, sep: str) -> bool:
    """A passphrase holds spaces. A variable in a command ends at the space, though: "docker run -e
    DB_PASSWORD=x img"."""
    return _PASSPHRASE_NAME.search(name) is not None and not (name.isupper() and "=" in sep)


def _assigned_value(text: str, match: re.Match[str]) -> tuple[int, int] | None:
    name, sep, start = match["name"], match["sep"], match.start("bare")
    env_end = _env_value_end(text, match)
    if env_end is not None:
        end, env = max(env_end, match.end("bare")), True
    else:
        to_line_end = _label_runs_to_line_end(name, sep)
        end, env = max(_value_end(text, start, to_line_end=to_line_end), match.end("bare")), False
    return (start, end) if _bare_value_is_secret(name, sep, text[start:end], env=env) else None


def _env_value_end(text: str, match: re.Match[str]) -> int | None:
    """NAME=value at the start of a line, the .env form: the value runs to the end of the line,
    so "JWT_SECRET=correct horse battery staple" is masked whole. A trailing comment stays."""
    name = match["name"]
    if match["sep"] != "=" or name != name.upper():
        return None
    # Only "export " can come before the name, so a short look back settles it.
    before = max(0, match.start() - _ENV_LOOK_BACK)
    if _ENV_LINE.search(text, before, match.start()) is None:
        return None
    line_end = text.find("\n", match.end())
    line_end = len(text) if line_end == -1 else line_end
    tail = _TRAILING_COMMENT.search(text, match.start("bare"), line_end)
    return tail.start() if tail else line_end


def _phrase_value(text: str, match: re.Match[str]) -> tuple[int, int] | None:
    if match["quoted"] is not None:
        value, start = match["quoted"], match.start("quoted")
        if value.lower() in _PHRASE_STOP:
            return None
        return start, start + len(value)
    start = match.start("value")
    if match["pin"] is not None:
        value = match["value"].rstrip(".,;:!?)")
        return (start, start + len(value)) if re.fullmatch(r"\d{4,12}", value) else None
    word, sep = match["word"] or match["weak"], match["sep"]
    # "is" leaves prose open. A label, a dash or an arrow says the rest of the line is the value.
    labelled = _LABEL_SEP.search(sep) is not None
    to_line_end = labelled and _label_runs_to_line_end(word, sep)
    whole = text[start : _value_end(text, start, to_line_end=to_line_end)]
    value = whole.rstrip(".,;:!?)")
    # "!" is kept in the mask: a sentence seldom ends here in one, and a password can.
    end = start + len(whole.rstrip(".:?"))
    first = value.split(maxsplit=1)[0] if value else ""
    if len(value) < 4 or first.lower() in _PHRASE_STOP:
        return None
    if not _bare_value_is_secret(word, sep, whole):
        return None
    if match["weak"] is not None:
        # "pass" and "pw" are common words: "pass: 1200" is a test count, "pass: Tr0ub4dor3" is
        # a password. The value must mix in a digit or a symbol and not be a number.
        mixed = any(not ch.isalpha() for ch in value) and not value.replace(",", "").isdigit()
        return (start, end) if mixed else None
    if _PLAIN_WORD.fullmatch(value):
        if _PROSE_ENDING.search(value.lower()):
            return None
        said_as_own = _OWNER.search(text, max(0, match.start() - 40), match.start()) is not None
        if not (labelled or said_as_own or _PASSWORD_WORD.fullmatch(word)):
            return None
    return start, end


_BASIC_LOOK_BACK = 200
_VALUE_SYMBOLS = frozenset("!@#$%^&*+=~?")
# Names with a digit that follow these words in dev talk: "password sha256 hashing is done".
_NOT_A_PASSWORD = frozenset(
    "md5 sha1 sha224 sha256 sha384 sha512 argon2 argon2i argon2d argon2id pbkdf2 oauth1 oauth2 "
    "base64 utf8 hs256 rs256 es256 x509 aes128 aes256 ed25519 rsa2048 rsa4096 http2".split()
)


def _mixed(value: str) -> bool:
    """Letters with a digit or a symbol, where no "=" or ":" says a value follows: no prose word,
    no number, no path, no placeholder and no reference."""
    if len(value) < 4 or value[0] in _OPENERS or _SHELL_REFERENCE.match(value):
        return False
    if "/" in value and _path_like(value):
        return False
    if not any(ch.isalpha() for ch in value) or value.lower() in _NOT_A_PASSWORD:
        return False
    return any(ch.isdigit() or ch in _VALUE_SYMBOLS for ch in value)


def _a_string(value: str) -> bool:
    return value.lower() not in _PHRASE_STOP


def _a_plain_password(value: str) -> bool:
    return _a_string(value) and _PROSE_ENDING.search(value.lower()) is None


def _a_digit(value: str) -> bool:
    return any(ch.isdigit() for ch in value)


def _assigned_to_a_secret(value: str) -> bool:
    return _bare_value_is_secret("value", ":", value)


# Each form and what its value must pass.
_VALUE_FORMS = (
    (_SECRET_FLAG, _mixed),
    (_CLIENT_FLAG, _mixed),
    (_SQL_PASSWORD, _a_string),
    (_AUTH_PAIR, _a_string),
    (_NAME_THEN_STRING, _mixed),
    (_SECRET_TAG, _a_string),
    (_NAME_VALUE_PAIR, _assigned_to_a_secret),
    (_YAML_BLOCK, _a_string),
    (_EMAIL_PAIR, _mixed),
    (_BARE_USERINFO, _mixed),
    (_USER_THEN_SECRET, _mixed),
    (_AUTH_HEADER, _a_digit),
    (_PASSWORD_FOR_THEN_PLAIN, _a_plain_password),
)


def _said_span(text: str, match: re.Match[str]) -> tuple[int, int]:
    """The span of a value that a form holds, without its quotes or the mark after it."""
    start, end = match.span("value")
    if end - start > 1 and text[start] in "\"'" and text[end - 1] == text[start]:
        return start + 1, end - 1
    return start, start + len(text[start : _trimmed_end(text, start, end)].rstrip(".:?"))


def _value_form_spans(text: str) -> Iterator[tuple[int, int]]:
    for pattern, passes in _VALUE_FORMS:
        for match in pattern.finditer(text):
            start, end = _said_span(text, match)
            if end > start and passes(text[start:end]):
                yield start, end


def _spoken_values(text: str) -> Iterator[tuple[int, int]]:
    """ "the password for prod is X", and the rest of a list: "passwords are X and Y"."""
    for match in _WORDS_THEN_VALUE.finditer(text):
        start, end = _said_span(text, match)
        value = text[start:end]
        # A number needs the word password and no unit: "the password expiry is 3600 seconds".
        number = value.isdecimal() and len(value) >= 4 and _UNIT_AFTER.match(text, end) is None
        if not (number or _mixed(value)):
            continue
        yield start, end
        for _ in range(_MORE_VALUES):
            more = _AND_MORE.match(text, end)
            if more is None:
                break
            start, end = _said_span(text, more)
            if not _mixed(text[start:end]):
                break
            yield start, end


def _cookie_values(text: str) -> Iterator[tuple[int, int]]:
    """Cookie values only where a header or a curl flag says these are cookies. A name that says
    session goes whatever its value; another goes when its value mixes letters and digits, so
    "theme=dark" stays."""
    for header in _COOKIE_HEADER.finditer(text):
        offset = header.start("pairs")
        for pair in _COOKIE_PAIR.finditer(header["pairs"]):
            name, value = pair["name"], pair["value"]
            if name.lower() in _COOKIE_ATTRIBUTES:
                continue
            mixed = len(value) >= 8 and _a_digit(value) and any(ch.isalpha() for ch in value)
            if mixed or _SESSION_COOKIE.search(name):
                yield offset + pair.start("value"), offset + pair.end("value")


def _bearer_is_secret(value: str) -> bool:
    if any(ch.isdigit() for ch in value):
        return True
    # A token with no digit is a secret when it is long, mixes case and is not words joined.
    mixed_case = any(ch.islower() for ch in value) and any(ch.isupper() for ch in value)
    return len(value) >= 20 and mixed_case and not wordy(value)


def _basic_is_secret(text: str, match: re.Match[str]) -> bool:
    try:
        decoded = base64.b64decode(match["value"] + "=" * (-len(match["value"]) % 4), validate=True)
    except ValueError:
        decoded = b""
    # A plain word also decodes, to bytes that are not text: "basic understanding" stays.
    if b":" in decoded and decoded.isascii() and decoded.decode().isprintable():
        return True
    before = text[max(0, match.start() - _BASIC_LOOK_BACK) : match.start()].lower()
    return "authoriz" in before.rpartition("\n")[2] and not wordy(match["value"])


def credentials(text: str) -> list[Hit]:
    spans = [m.span() for m in _PEM.finditer(text)]
    spans += [m.span() for m in _PREFIXED.finditer(text)]
    spans += [m.span("value") for m in _WEBHOOK.finditer(text)]
    spans += [m.span("value") for m in _BEARER.finditer(text) if _bearer_is_secret(m["value"])]
    spans += [m.span("value") for m in _BASIC.finditer(text) if _basic_is_secret(text, m)]
    spans += [m.span("value") for m in _USERINFO.finditer(text)]
    spans += [m.span("value") for m in _CURL_USER.finditer(text)]
    spans += [m.span("value") for m in _QUERY_SECRET.finditer(text)]
    for match in _ASSIGNMENT.finditer(text):
        for group in ("dq", "sq", "bq"):
            if match.group(group) is not None:
                spans.append(match.span(group))
        if match.group("bare") is not None:
            span = _assigned_value(text, match)
            if span is not None:
                spans.append(span)
    for match in _PHRASE.finditer(text):
        span = _phrase_value(text, match)
        if span is not None:
            spans.append(span)
    spans += [m.span("pin") if m["pin"] else m.span("cvv") for m in _BARE_PIN.finditer(text)]
    for match in _SPACED_PASSWORD.finditer(text):
        span = _said_span(text, match)
        if _mixed(text[span[0] : span[1]]):
            spans.append(span)
    for match in _LOGIN_PAIR.finditer(text):
        value = match["value"].rstrip(".,;:!?)")
        # "log in with google / github": with no label, the value must pass _mixed.
        if match["spoken"] and not _mixed(value):
            continue
        if len(value) >= 4 and value.lower() not in _PHRASE_STOP and not value.isdecimal():
            spans.append((match.start("value"), match.start("value") + len(value)))
    spans += _spoken_values(text)
    spans += _value_form_spans(text)
    spans += _cookie_values(text)
    spans += _context_tokens(text)
    return _hits(CREDENTIAL, spans)


def _context_tokens(text: str) -> list[tuple[int, int]]:
    """A random-looking token is a credential only with a context word on its line, or when it is
    alone on its line under a line that holds one: a person puts a key in a fence under a sentence.
    """
    spans = []
    in_urls = _Cover(len(text), urls(text))
    offset = 0
    above = ""
    above_says = False
    for line in text.split("\n"):
        says = _CONTEXT.search(_TOKEN_COUNTER.sub(" ", line)) is not None
        alone = line.strip().strip("`'\"")
        if says or above_says:
            for match in _TOKEN.finditer(line):
                start, end = offset + match.start(), offset + match.end()
                token = match.group()
                under_a_word = above_says and token == alone
                if not (says or under_a_word):
                    continue
                if not _random_looking(token) or in_urls.holds(start, end) or wordy(token):
                    continue
                if _PLAIN_ID.fullmatch(token.rstrip("=")):
                    # A few words fit in the window, so a long line costs no more per token.
                    window = max(0, match.start() - _JUST_BEFORE_CHARS)
                    before = _CONTEXT_JUST_BEFORE.search(line, window, match.start())
                    tail = above[-_JUST_BEFORE_CHARS:]
                    if not before and not (under_a_word and _CONTEXT_JUST_BEFORE.search(tail)):
                        continue
                spans.append((start, end))
        # A blank line or a fence sits between a sentence and its value.
        if alone and not line.lstrip().startswith("```"):
            above, above_says = line, says
        offset += len(line) + 1
    return spans


# Payment cards --------------------------------------------------------------------------------

# Digit groups split by a space or a dash, and in a one-separator form also a dot, a tab, two
# spaces or " - ". Not after "+" (a phone), not inside a longer token such as a UUID ("-7abc") or
# a version ("1.2"). A file suffix (".txt") may follow.
_DIGIT_RUN = re.compile(
    r"(?<![\w+])(?<!\w[-.])\d+(?:(?:[ -]|[ \t]{2}|\t| - |\.)\d+)*(?!\w)(?!-\w)(?!\.\d)"
)
_GROUP = re.compile(r"\d+")
_CARD_GROUPED = ([4, 6, 5], [4, 6, 4])
_MAX_CARD_GROUPS = 5
_PLAIN_SEPARATORS = (" ", "-")
# An unbroken run needs a card network's first digit, so an epoch time or a snowflake id (first
# digit 1) that passes Luhn by chance stays.
_NETWORK_DIGITS = "23456"


def luhn_valid(digits: str) -> bool:
    total = 0
    for index, ch in enumerate(reversed(digits)):
        value = int(ch)
        if index % 2:
            value = value * 2 - 9 if value > 4 else value * 2
        total += value
    return total % 10 == 0


def _card_shape(lengths: list[int]) -> bool:
    """One unbroken run, groups of four with a short last group, or the 4-6-5 and 4-6-4 forms.

    The shape keeps dates and order numbers out: "112-3456789-1234567" is no card.
    """
    if len(lengths) == 1:
        return True
    if lengths in _CARD_GROUPED:
        return True
    return all(n == 4 for n in lengths[:-1]) and 1 <= lengths[-1] <= 4


def _card_groups(text: str, part: list[tuple[int, int]]) -> bool:
    lengths = [end - start for start, end in part]
    if not (13 <= sum(lengths) <= 19 and _card_shape(lengths)):
        return False
    if len(part) == 1:
        start, end = part[0]
        # A long decimal ("2730541312030.5") is a number, whatever the last group does.
        if text[end : end + 1] == "." and text[end + 1 : end + 2].isdecimal():
            return False
        if start and text[start - 1] == "." and text[start - 2 : start - 1].isdecimal():
            return False
        return ascii_digits(text[start]) in _NETWORK_DIGITS
    separators = {text[left[1] : right[0]] for left, right in zip(part, part[1:], strict=False)}
    # "4111.1111.1111.1111" counts; a mix of odd separators is a table or a list.
    return separators <= set(_PLAIN_SEPARATORS) or len(separators) == 1


def cards(text: str) -> list[Hit]:
    spans = []
    for run in _DIGIT_RUN.finditer(text):
        groups = [(run.start() + g.start(), run.start() + g.end()) for g in _GROUP.finditer(run[0])]
        first = 0
        while first < len(groups):
            found = None
            # Longest first, so a trailing CVV group does not hide the card before it. A card
            # has at most five groups (4-4-4-4-3), which bounds the work on a long digit table.
            for last in range(min(len(groups), first + _MAX_CARD_GROUPS) - 1, first - 1, -1):
                part = groups[first : last + 1]
                # Every character of a group is one digit, so the shape is checked first and
                # the digits are built only for a candidate.
                if not _card_groups(text, part):
                    continue
                if luhn_valid(ascii_digits("".join(text[s:e] for s, e in part))):
                    found = last
                    break
            if found is None:
                first += 1
            else:
                spans.append((groups[first][0], groups[found][1]))
                first = found + 1
    return _hits(PAYMENT_CARD, spans)


# One-time codes and reset links ---------------------------------------------------------------

# Step 2 grades each candidate. A strong signal is the spec's action: the message is dropped. A
# weak candidate is masked and the message stays, so a drop never pays for what code cannot settle
# and no value is stored either way (SC6, SC8).

_OTC_CODE = r"(?:[A-Za-z]{1,4}-)?(?:\d{4,8}|\d{3}[ -]\d{3})"
_CODE_TOKEN = re.compile(rf"(?<![\w.:/-])(?:{_OTC_CODE})(?!\w)(?![.:/,-]\d)")
# Right after a strong phrase a code may hold letters too, when it holds a digit. "python3" and
# "utf8" are words with a number, "sha256" and "x509" are names, and the "(?i:" keeps "SHA256" out.
_ALNUM_CODE = (
    r"(?=[A-Za-z0-9-]*\d)(?![a-z]{3,}\d{1,2}(?![A-Za-z0-9-]))"
    r"(?!(?i:(?:sha|hs|rs|es|ps)\d{3}|x509|base\d\d)(?![A-Za-z0-9-]))"
    r"(?:[A-Za-z0-9]{4,10}|[A-Za-z0-9]{3,4}-[A-Za-z0-9]{3,4})"
)
_IS = r"(?:is|was|est|howa)"
_RIGHT_AFTER = re.compile(
    rf"\s*+(?:{_IS}\s*+)?[:=-]?\s*+(?P<code>{_OTC_CODE}|{_ALNUM_CODE})(?![\w-])(?![.:/,]\w)"
)
_RIGHT_AFTER_DIGITS = re.compile(
    rf"\s*+(?:{_IS}\s*+)?[:=-]?\s*+(?P<code>{_OTC_CODE})(?![\w-])(?![.:/,]\w)"
)
# A service may send capitals only ("ABC-DEF"), and code cannot tell those from a word said loud,
# so a strong phrase and then capitals are masked.
_LETTER_CODE = re.compile(
    rf"\s*+(?:{_IS}\s*+)?[:=-]?\s*+(?P<code>[A-Z]{{5,10}}|[A-Z]{{3,4}}-[A-Z]{{3,4}})(?![\w-])"
)
# A short sha reads like a code. A real code can have this shape too, so it is masked.
_SHA_SHAPE = re.compile(r"(?=\d*[a-f])[0-9a-f]{7,}")
# A number that can be a port or a count, a lowercase word with a digit, a ticket key. One soft
# signal cannot settle these.
_AMBIGUOUS_CODE = re.compile(r"\d{4,5}|(?=[a-z0-9-]*[a-z])[a-z0-9-]+|[A-Za-z]{3,4}-\d+")
# The services that text a code. "signal" and "github" are left out: "the signal code" and "the
# github code" are dev talk, and their real messages match "your ... code" or "launch code".
_OTC_BRANDS = (
    "whatsapp|telegram|instagram|facebook|snapchat|uber|discord|tiktok|microsoft|apple|amazon"
    "|paypal|google"
)
# These name a live code: a number near one drops the message. The last one is Arabic for "code".
_OTC_PHRASE = re.compile(
    r"(?i)(?<!\w)(?:"
    r"(?:verification|security|confirmation|access|login|log-in|log\s+in|sign-in|sign\s+in"
    r"|signin|2fa|mfa|two-factor|2-step)\s+code"
    r"|code\s+de\s+(?:v[e\u00e9]rification|confirmation|s[e\u00e9]curit[e\u00e9]|connexion"
    r"|validation)"
    r"|one[-\s]time\s+(?:code|passcode|password|pin)"
    r"|otp|passcode|your\s+code\s+is|your\s+code:|enter(?:ing)?\s+(?:the|this|your)\s+code"
    r"(?:\s+below)?"
    r"|(?:do\s+not|don['\u2019]?t|never)\s+share\s+(?:this|your|the)\s+code"
    r"|to\s+verify\s+your"
    r"|\u0631\u0645\u0632"
    r")(?!\w)"
)
# Dev chat says these about a connector or a feature ("the WhatsApp code parsed 8400 messages"). A
# code right after one is strong, and a number further off is weak.
_OTC_SERVICE_PHRASE = re.compile(
    rf"(?i)(?<!\w)(?:registration|activation|recovery|pairing|sms|{_OTC_BRANDS})\s+code(?!\w)"
)
# These count only with the code right after them: "update your code at line 1420" is dev talk,
# "Your Uber code: 4829" is not. "auth code" is OAuth talk unless a code follows it.
_OTC_YOUR = re.compile(
    rf"(?i)(?<!\w)(?:your|votre)(?:\s+[\w-]+){{0,3}}?\s+code(?:\s+(?:{_OTC_BRANDS}))?(?!\w)"
)
_OTC_NEAR_ONLY = re.compile(
    r"(?i)(?<!\w)(?:"
    r"auth(?:entication)?\s+code|launch\s+code|account\s+code|secure\s+code|guard\s+code"
    r"|2fa|mfa|totp"
    r"|(?:use|using|enter|type|input|utilisez|entrez|saisissez)"
    r"(?:\s+(?:the|this|your|following|le|ce|votre)){0,2}\s+code"
    r")(?!\w)"
)
# "Kestrel code: 482910" on its own line: a brand this list has not heard of. Digits only.
_LABEL_CODE = re.compile(
    r"(?im)^[ \t]*(?:<#>[ \t]*)?(?P<label>[a-z][\w-]{1,30})[ \t]+code[ \t]*(?=:)"
)
_NOT_A_CODE_WORDS = (
    "error|exit|status|http|response|return|source|zip|postal|area|country|promo|discount|coupon"
    "|product|sample|example|test|python|java"
)
_NOT_A_CODE_LABEL = frozenset(
    f"{_NOT_A_CODE_WORDS}|the|this|that|my|our|js|ts|go|rust|dev|old|new|legacy|client|server"
    "|main|unit|color|colour|qr|bar|dial|total".split("|")
)
# "482910 is your Instagram code", "Code:482910 is your verification".
_IS_YOUR = re.compile(
    rf"(?i)(?<![\w./-])(?<!\d:)(?P<code>{_OTC_CODE}|{_ALNUM_CODE})\s+(?:is\s+your|est\s+votre)"
    r"(?P<label>(?:\s+[\w-]+){0,3}?)\s+(?:code|otp|passcode|pin|password|verification|login"
    r"|sign-in)(?!\w)"
)
_SOFT_PURPOSE = (
    r"(?:to\s+(?:log[\s-]*in|login|sign[\s-]*in|signin|verify|confirm|continue|approve"
    r"|authori[sz]e|complete|reset)"
    r"|on\s+the\s+(?:log[\s-]*in|login|sign[\s-]*in|signin)\s+page|pour\s+vous\s+connecter)"
)
_SOFT_VERB = r"(?:use|using|enter|type|input|reply\s+with|utilisez|entrez|saisissez)"
_SOFT_CODE = rf"(?P<code>{_OTC_CODE}|{_ALNUM_CODE})(?![\w-])(?![.:/,]\w)"
# The soft signals. Each alone is weak for a number in _AMBIGUOUS_CODE and strong for any other
# code, and two of them in one message settle it. "Use 8080 to log in" is a port; "Use 5521 to log
# in. It expires in 5 minutes." is not.
_SIGN_IN_PURPOSE = re.compile(rf"(?i)(?<!\w){_SOFT_PURPOSE}(?!\w)")
_VALIDITY = re.compile(
    r"(?i)(?<!\w)(?:(?:will\s+)?expires?(?:\s+in)?|(?:is\s+)?valid\s+for)\s+"
    r"(?:\d+|an?|one|two|three|five|ten|fifteen|thirty)[\s-]*"
    r"(?:s|secs?|seconds?|m|mins?|minutes?|h|hrs?|hours?)(?!\w)"
)
_DO_NOT_SHARE = re.compile(
    r"(?i)(?<!\w)(?:do\s+not|don['\u2019]?t|never)\s+(?:share|give|forward)(?!\w)"
)
# "Use 482910 to sign in", but not "Use 8080 as the port to sign in".
_VERB_THEN_PURPOSE = re.compile(
    rf"(?i)(?<!\w){_SOFT_VERB}\s++{_SOFT_CODE}(?![ \t]+as(?!\w))"
    rf"[^.!?\n]{{0,40}}?(?<!\w){_SOFT_PURPOSE}(?!\w)"
)
_PURPOSE_THEN_VERB = re.compile(
    rf"(?i)(?<!\w){_SOFT_PURPOSE}(?!\w)[^.!?\n]{{0,40}}?(?<!\w){_SOFT_VERB}\s++{_SOFT_CODE}"
)
# "Login: 482910", "Kestrel sign-in: 482910", "Sign in with 482910", "Your login token is 482910".
_SIGN_IN_LABEL = re.compile(
    r"(?i)(?<!\w)(?:log[\s-]?in|sign[\s-]?in|signin|verification)"
    r"(?:\s+(?:token|with|using)(?:\s+code)?(?!\w)|(?=\s*+(?:[:=]|is(?!\w))))"
)
_CODE_THEN_LOGIN = re.compile(
    rf"(?i)(?<![\w./-])(?P<code>{_OTC_CODE})\s*+[-\u2013]\s*+[\w-]{{1,30}}\s+"
    r"(?:log[\s-]?in|sign[\s-]?in)(?!\w)"
)
# The bare word "code" is not enough for a drop (Grove's own chats say "the code is 1200 lines"),
# so these are masked, and only when the number ends a sentence: "the code is 482910.",
# "the code they sent me is 482910", "Code: 482910", "send me the code, it's 4829".
_BARE_CODE = re.compile(
    rf"(?i)(?<![\w-])code(?!\w)(?P<words>(?:[ \t,]++[\w'\u2019-]++){{0,4}}?)\s*+(?:{_IS}\s*+)?"
    rf"[:=]?\s*+(?P<code>{_OTC_CODE}|{_ALNUM_CODE})(?!\w)(?=\s*(?:$|[.;!?](?:\s|$)|\n))"
)
_IS_THE_CODE = re.compile(
    rf"(?i)(?<![\w./-])(?<!\d:)(?P<code>{_OTC_CODE})\s+(?:is\s+the|est\s+le)\s+code(?!\w)"
)
_DEV_CODE_BEFORE = re.compile(rf"(?i)(?<![a-z])(?:{_NOT_A_CODE_WORDS})\s+$")
# A number that names something else: "PR 1234", "#4821", "line 1420", "port 8080".
_REFERENCE_BEFORE = re.compile(
    r"(?i)(?:(?<![a-z])(?:prs?|pull|issues?|tickets?|lines?|ports?|steps?|rows?|builds?|runs?|jobs?"
    r"|pid|orders?|items?|pages?|version|errors?|status|exit|http|rooms?|invoices?)\s*#?\s*"
    r"|#\s*)$"
)
# A number with a unit or a noun on its own line: "1500 ms", "1200 lines". A line below can start
# with such a word and say nothing about the number: "Your OTP: 482910\nTime left: 5 minutes".
_UNIT_AFTER = re.compile(
    r"(?i)[ \t]*(?:lines?|lignes?|loc|ms|s|secs?|seconds?|mins?|minutes?|hrs?|hours?|h|days?"
    r"|issues?|rows?|files?|tests?|bytes?|kb|mb|gb|tokens?|users?|items?|times?|requests?|calls?"
    r"|records?|words?|chars?|characters?|px|errors?|commits?|prs?|messages?|msgs?|chats?|events?"
    r"|snapshots?|sessions?)(?![a-z0-9])|[ \t]*%"
)
_TICKET_KEY = re.compile(r"[A-Za-z]{3,4}-\d+")
_OTC_SENDER = re.compile(
    r"(?i)(?<![a-z])(?:no-?reply|do-?not-?reply|verify|verification|security|auth|accounts?"
    r"|otp)(?![a-z])"
)
# A service as the whole sender name. A person's name can hold one of these words.
_OTC_BRAND_SENDER = re.compile(
    rf"(?i)\s*(?:{_OTC_BRANDS}|signal|meta|twitter)"
    r"(?:\s+(?:verify|verification|security|code|codes|alerts?|auth))?\s*"
)
_YEAR = re.compile(r"(?:19|20)\d\d")
_WINDOW = 40
_REFERENCE_CHARS = 12


def _year_like(code: str) -> bool:
    return _YEAR.fullmatch(code) is not None


def _names_something_else(text: str, start: int, end: int) -> bool:
    """A reference word before the number or a unit after it: the number is not a code."""
    before = _REFERENCE_BEFORE.search(text, max(0, start - _REFERENCE_CHARS), start)
    return before is not None or _UNIT_AFTER.match(text, end) is not None


def _code_said_right_after(text: str, end: int, pattern: re.Pattern[str]) -> tuple[int, int] | None:
    match = pattern.match(text, end)
    if match is None or _UNIT_AFTER.match(text, match.end("code")) is not None:
        return None
    return match.span("code")


class _CodeTokens:
    """The number-shaped tokens of a text, so a phrase looks only at the ones near it."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.all = list(_CODE_TOKEN.finditer(text))
        self._starts = [code.start() for code in self.all]

    def near(self, phrase: re.Match[str]) -> Iterator[tuple[int, int]]:
        text = self.text
        # A code is at most 13 characters, so this slice holds every code the window can reach.
        low = bisect_left(self._starts, phrase.start() - _WINDOW - 13)
        high = bisect_right(self._starts, phrase.end() + _WINDOW)
        for code in self.all[low:high]:
            before = phrase.start() - code.end()
            after = code.start() - phrase.end()
            if not (0 <= before <= _WINDOW or 0 <= after <= _WINDOW):
                continue
            if _names_something_else(text, code.start(), code.end()):
                continue
            # A ticket key near a phrase ("the OTP fix is LUX-1234") is not a code.
            if _TICKET_KEY.fullmatch(code.group()):
                continue
            # A year near a phrase ("the verification code ships in 2026") is not a code,
            # unless it is said as the code: "code: 2026".
            gap = text[phrase.end() : code.start()] if after >= 0 else "x"
            if _year_like(code.group()) and not re.fullmatch(r"\s*(?:is|:|-|=)?\s*", gap):
                continue
            yield code.span()


def _graded_codes(
    text: str, sender: str | None
) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """The candidates of a text as (strong, weak). A strong one drops the message."""
    strong: list[tuple[int, int]] = []
    weak: list[tuple[int, int]] = []

    def said(span: tuple[int, int] | None) -> None:
        if span is not None:
            (weak if _SHA_SHAPE.fullmatch(text, *span) else strong).append(span)

    def said_right_after(end: int, pattern: re.Pattern[str] = _RIGHT_AFTER) -> None:
        said(_code_said_right_after(text, end, pattern))

    signals = sum(p.search(text) is not None for p in (_SIGN_IN_PURPOSE, _VALIDITY, _DO_NOT_SHARE))

    def soft(span: tuple[int, int] | None) -> None:
        if span is not None:
            ambiguous = _AMBIGUOUS_CODE.fullmatch(text, *span) is not None
            (weak if ambiguous and signals < 2 else strong).append(span)

    for match in _IS_YOUR.finditer(text):
        start, end = match.span("code")
        reference = _REFERENCE_BEFORE.search(text, max(0, start - _REFERENCE_CHARS), start)
        if reference is None or match["label"]:
            said((start, end))
        # "PR 1234 is your code" names a pull request. "#4821 is your code" is less clear.
        elif reference.group().startswith("#"):
            weak.append((start, end))
    for phrase in _OTC_YOUR.finditer(text):
        # "your error code is 1045" is dev talk.
        if phrase.group().split()[-2].lower() not in _NOT_A_CODE_LABEL:
            said_right_after(phrase.end())
    for phrase in _OTC_NEAR_ONLY.finditer(text):
        said_right_after(phrase.end())
    for label in _LABEL_CODE.finditer(text):
        if label["label"].lower() not in _NOT_A_CODE_LABEL:
            said_right_after(label.end(), _RIGHT_AFTER_DIGITS)

    tokens = _CodeTokens(text)
    for phrase in _OTC_PHRASE.finditer(text):
        said_right_after(phrase.end())
        letters = _LETTER_CODE.match(text, phrase.end())
        if letters is not None:
            weak.append(letters.span("code"))
        strong += tokens.near(phrase)
    for phrase in _OTC_SERVICE_PHRASE.finditer(text):
        said_right_after(phrase.end())
        weak += tokens.near(phrase)

    for pattern in (_VALIDITY, _DO_NOT_SHARE):
        for phrase in pattern.finditer(text):
            for span in tokens.near(phrase):
                soft(span)
    for pattern in (_VERB_THEN_PURPOSE, _PURPOSE_THEN_VERB):
        for match in pattern.finditer(text):
            if not _names_something_else(text, *match.span("code")):
                soft(match.span("code"))
    for label in _SIGN_IN_LABEL.finditer(text):
        soft(_code_said_right_after(text, label.end(), _RIGHT_AFTER))
    for match in _CODE_THEN_LOGIN.finditer(text):
        soft(match.span("code"))

    for match in _BARE_CODE.finditer(text):
        start, end = match.span("code")
        code = match["code"]
        before = _DEV_CODE_BEFORE.search(text, max(0, match.start() - 24), match.start())
        # With words between, only a number: "the code runs on arm64x." names no code.
        if (
            before is not None
            or _TICKET_KEY.fullmatch(code)
            or _names_something_else(text, start, end)
            or (match["words"] and (_year_like(code) or not code.isdecimal()))
        ):
            continue
        weak.append((start, end))
    for match in _IS_THE_CODE.finditer(text):
        if not _names_something_else(text, *match.span("code")):
            weak.append(match.span("code"))

    if sender and (_OTC_SENDER.search(sender) or _OTC_BRAND_SENDER.fullmatch(sender)):
        # "Karim Accounts" can be a person. An address, one word or a brand is a service.
        service = "@" in sender or len(sender.split()) == 1 or _OTC_BRAND_SENDER.fullmatch(sender)
        for code in tokens.all:
            if (
                _year_like(code.group())
                or _TICKET_KEY.fullmatch(code.group())
                or _names_something_else(text, code.start(), code.end())
            ):
                continue
            (strong if service else weak).append(code.span())
    return strong, weak


def one_time_codes(text: str, sender: str | None = None) -> list[Hit]:
    """The strong candidates: a hit drops the message."""
    return _hits(ONE_TIME_CODE, _graded_codes(text, sender)[0])


def weak_codes(text: str, sender: str | None = None) -> list[Hit]:
    """The weak candidates of a message that stays: each number becomes [code]."""
    return _hits(ONE_TIME_CODE, _graded_codes(text, sender)[1])


# Whole components of a path or query, after camelCase is split: "verifier" and "authors" are
# not link words, "resetPassword" is.
_LINK_WORD = re.compile(
    r"(?<![a-z])(?:reset|resetpassword|resetpw|pwreset|setpassword|password|passwordreset"
    r"|passwordless|passwd|magic|magiclink|magiclogin|verify|verifyemail|emailverify|verification"
    r"|confirm|confirmation|signin|sign-in|sign_in|login|log-in|loginlink|loginwithemail|auth"
    r"|oauth|authenticate|token|otp|invite|invitation|forgot|recover|recovery|activate|activation"
    r"|r[e\u00e9]initialiser|connexion|mot-de-passe)"
    r"(?![a-z])"
)
_CAMEL = re.compile(r"([a-z])([A-Z])")
# These settle it: with any URL in the message, the message is dropped.
_LINK_PHRASE = re.compile(
    r"(?i)reset\s+your\s+password|password\s+reset\s+link|sign\s+in\s+with\s+this\s+link"
    r"|forgot\s+your\s+password|link\s+to\s+reset|activate\s+your\s+account"
    r"|r[e\u00e9]initiali[sz]ez\s+votre\s+mot\s+de\s+passe"
)
# Dev chat says these about a feature. With the URL right after, the message is dropped; with a URL
# further off, the URL is masked.
_LINK_NEAR_PHRASE = re.compile(
    r"(?i)magic\s+link|(?:log|sign)[\s-]?in\s+link|verify\s+your\s+email"
    r"|link\s+to\s+(?:log|sign)[\s-]?in|one[-\s]time\s+link|lien\s+magique|lien\s+de\s+connexion"
)
# Common words. Only the URL right after one counts, and it is masked. A tracking link has no
# link word of its own, so these sentences are what is left to catch it.
_LINK_SAID_PHRASE = re.compile(
    r"(?i)sign\s+in\s+to\s+[\w-]+|confirm\s+your\s+email|verify\s+your\s+account"
    r"|set\s+a\s+new\s+password|link\s+expires\s+in\s+\w+\s+\w+"
)
_LINK_GAP = re.compile(r"[\s:,.\u2192<>(-]*+(?:(?:is|here|below|follows)[\s:,.\u2192<>(-]*+){0,2}")
_LINK_PIECE = re.compile(r"[^/?&=#;*\\,$]+")
_TOKEN_CHARS = re.compile(r"[A-Za-z0-9_~.%-]+")
# A file name in a link is not a token: "session_tokens_v1.py".
_FILE_PIECE = re.compile(
    r"(?i).*\.(?:py|js|jsx|ts|tsx|md|json|ya?ml|toml|txt|html?|css|rs|go|java|rb|sh|sql|lock|cfg"
    r"|ini|csv|log|xml|png|jpe?g|svg|pdf)"
)
# A short code in a query beside a link word: "/verify?code=482910".
_LINK_CODE_QUERY = re.compile(r"(?i)(?:^|&)(?:code|otp|pin)=[^&]+")
# OAuth says a client id is public, so it is no token.
_PUBLIC_QUERY = re.compile(r"(?i)(?:^|(?<=&))client_id=[^&]*")
# Host with no scheme ("app.example.com/m/x"): a phone shows it as a link. Only the link check
# reads these, so a file path never counts as a URL for the other detectors.
_BARE_HOST_URL = re.compile(
    r"(?i)(?<![\w.@/:-])(?:[a-z0-9-]+\.)+(?P<tld>[a-z]{2,24})/[^\s<>\"'`|]+"
)
_FILE_TLD = frozenset("py js jsx ts tsx json yaml yml toml txt html css java rb sql lock".split())
# Pages that a link word and a long value do not make a reset link: a sha, a branch or a page id
# there is no token. Each rule holds a path, so a reset page on the same host still counts.
_CODE_HOST_PAGE = re.compile(
    r"(?i)/[^/]+/[^/]+/(?:blob|tree|commits?|compare|releases|pulls?|issues|actions|raw|blame"
    r"|wiki|tags?|discussions|merge_requests|-)(?:/|$)"
)
_ISSUE_TRACKER_PAGE = re.compile(r"(?i)/[^/]+/(?:issue|project|view|team|document)/")
_RAW_SHA_PAGE = re.compile(r"/[^/]+/[^/]+/[0-9a-f]{40}/")
_NOTION_PAGE = re.compile(r"(?:/[^/]+)?/[^/]*-[0-9a-f]{32}/?$")
_GOOGLE_DOC_PAGE = re.compile(r"/(?:document|spreadsheets|presentation)/d/")
_FIGMA_PAGE = re.compile(r"/(?:file|design)/")
_PAGE_RULES = {
    "github.com": _CODE_HOST_PAGE,
    "www.github.com": _CODE_HOST_PAGE,
    "gitlab.com": _CODE_HOST_PAGE,
    "bitbucket.org": _CODE_HOST_PAGE,
    "raw.githubusercontent.com": _RAW_SHA_PAGE,
    "linear.app": _ISSUE_TRACKER_PAGE,
    "notion.so": _NOTION_PAGE,
    "www.notion.so": _NOTION_PAGE,
    "docs.google.com": _GOOGLE_DOC_PAGE,
    "figma.com": _FIGMA_PAGE,
    "www.figma.com": _FIGMA_PAGE,
}


def _long_value(piece: str) -> bool:
    """16 or more URL-safe characters that are no file name and no slug of words."""
    return (
        len(piece) >= 16
        and _TOKEN_CHARS.fullmatch(piece) is not None
        and _FILE_PIECE.fullmatch(piece) is None
        and not wordy(piece)
    )


def _slug_id(piece: str) -> str | None:
    """The last part of a slug of three or more words: "how-to-reset-a-password-3f2a9c1e7b4d"."""
    head, _, tail = piece.rpartition("-")
    return tail if head.count("-") >= 2 and wordy(head) else None


def _token_like(piece: str) -> bool:
    return (
        _long_value(piece)
        and any(ch.isdigit() for ch in piece)
        and any(ch.isalpha() for ch in piece)
        and _slug_id(piece) is None
    )


def _weak_token(piece: str) -> bool:
    """A long value that may be a token: letters only, digits only, or a long id after a slug. A
    random token with no digit is 1 in 17 at 16 characters, so these never stay."""
    if not _long_value(piece):
        return False
    slug_id = _slug_id(piece)
    return slug_id is None or len(slug_id) >= 16


def _bare_host_urls(text: str) -> Iterator[tuple[int, int]]:
    for match in _BARE_HOST_URL.finditer(text):
        if match["tld"].lower() not in _FILE_TLD:
            yield match.start(), match.start() + len(match.group().rstrip(_URL_TAIL))


def _code_page(parts: SplitResult) -> bool:
    rule = _PAGE_RULES.get((parts.hostname or "").lower())
    return rule is not None and rule.match(parts.path) is not None


def _link_word_at(rest: str) -> int | None:
    """Where the first link word starts in `rest`. The word search reads a copy with camelCase
    split, and the token check must read the original pieces: split, a random key reads as words."""
    split = _CAMEL.sub(r"\1-\2", rest)
    word = _LINK_WORD.search(split.lower())
    if word is None:
        return None
    # Each split added one character at its boundary, so count those before the word.
    boundaries = (m.start() + 1 + n for n, m in enumerate(_CAMEL.finditer(rest)))
    return word.start() - sum(1 for at in boundaries if at < word.start())


def _graded_links(text: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """The links of a text as (strong, weak). A strong one drops the message."""
    with_scheme = list(urls(text))
    # A check costs the span asked about, so many links in one text stay linear.
    in_urls = _Cover(len(text), with_scheme)
    found = with_scheme + [
        (start, end) for start, end in _bare_host_urls(text) if not in_urls.touches(start, end)
    ]
    strong: list[tuple[int, int]] = []
    weak: list[tuple[int, int]] = []
    pages = []
    for start, end in found:
        link = text[start:end]
        try:
            # With no scheme the host would read as a path, and its words as link words.
            parts = urlsplit(link if "://" in link else f"//{link}")
        except ValueError:
            # A URL the parser refuses cannot be cleared by code, so it is treated as a link.
            strong.append((start, end))
            continue
        if _code_page(parts):
            continue
        pages.append((start, end))
        # The fragment too: a hash-route app keeps its path there ("/#/reset-password/X").
        rest = f"{parts.path}?{_PUBLIC_QUERY.sub('', parts.query)}#{parts.fragment}"
        word_at = _link_word_at(rest)
        # The token follows the word: "/reset-password?token=X", "/magic/X".
        if word_at is not None and any(
            _token_like(piece.group()) for piece in _LINK_PIECE.finditer(rest, word_at)
        ):
            strong.append((start, end))
        elif (word_at is not None or _LINK_WORD.search((parts.hostname or "").lower())) and any(
            _weak_token(piece.group()) for piece in _LINK_PIECE.finditer(rest)
        ):
            weak.append((start, end))
        elif word_at is not None and _LINK_CODE_QUERY.search(parts.query):
            weak.append((start, end))
    if not pages:
        return strong, weak

    url_at = dict(pages)

    def url_right_after(phrase: re.Match[str]) -> int | None:
        # The gap pattern can match nothing, so it always gives a match.
        at = _LINK_GAP.match(text, phrase.end()).end()
        return at if at in url_at else None

    if _LINK_PHRASE.search(text):
        strong += pages
    near = list(_LINK_NEAR_PHRASE.finditer(text))
    if any(url_right_after(phrase) is not None for phrase in near):
        strong += pages
    elif near:
        weak += pages
    for phrase in _LINK_SAID_PHRASE.finditer(text):
        at = url_right_after(phrase)
        if at is not None:
            weak.append((at, url_at[at]))
    return strong, weak


def reset_links(text: str) -> list[Hit]:
    """The strong candidates: a hit drops the message."""
    return _hits(RESET_LINK, _graded_links(text)[0])


def weak_links(text: str) -> list[Hit]:
    """The weak candidates of a message that stays: each URL becomes [link]."""
    return _hits(RESET_LINK, _graded_links(text)[1])


# Contact identifiers --------------------------------------------------------------------------

# A real remote: the user git, or a path with "/" that is no port ("x@host:5432/app").
_SCP_PATH = re.compile(r":(?!\d+/)[\w~.-]*/")
# After an address a "-" is mostly a note ("orla@x.example-thanks"). A host alias in a remote and a
# numbered host are the two forms where it is part of the host: "git@github.com-work:org/repo.git",
# "deploy@web.prod-01".
_HOST_ALIAS = re.compile(rf"-[\w.-]*{_SCP_PATH.pattern}")
_HOST_LABELS = frozenset("prod local internal lan corp intra stage staging".split())
# Addresses that name no person: a commit trailer, an SSH user and the hosts of RFC 2606. A login
# stays masked (<digits>+login@users.noreply.github.com), and so does ".example", which a person's
# address in a test uses.
_MACHINE_ADDRESSES = frozenset(
    "noreply@anthropic.com noreply@github.com git@github.com git@gitlab.com "
    "git@bitbucket.org".split()
)
_PLACEHOLDER_HOSTS = frozenset("example.com example.org example.net".split())
# "icon@2x.png" is a file name.
_FILE_SUFFIXES = frozenset("png jpg jpeg gif svg webp ico bmp tif tiff".split())
_FILE_TAIL = re.compile(r"\.(?:txt|zip|json|jsonl|csv|pdf|md|html?|log)$", re.IGNORECASE)

# Dates, times and ISO timestamps are blanked before the phone search, so a phone never runs
# into one and one is never read as a phone.
_PHONE_BARRIERS = re.compile(
    r"(?<!\d)(?:19|20)\d\d-\d\d-\d\d(?:[T ]\d\d:\d\d(?::\d\d(?:\.\d+)?)?(?:Z|[+-]\d\d:?\d\d)?)?"
    r"(?!\d)"
    r"|(?<!\d)\d{1,2}[./-]\d{1,2}[./-](?:19|20)\d\d(?!\d)"
    r"|(?<!\d)\d{1,2}:\d\d(?::\d\d)?(?:\.\d+)?(?!\d)"
)
# What glues a number to the word before it: a label or a name, then a mark ("Tel.06 12 34 56 78",
# "Orla,0612345678"), or a phone label with no mark ("Tel0612345678"). A digit before the mark is a
# version or a decimal, which the guard of _PHONE keeps. The glue is read as a space.
_PHONE_GLUE = re.compile(
    r"(?<=[^\W\d_])[-.,](?=[+(0]\d)"
    r"|(?<![^\W\d_])(?i:tel|t\u00e9l|gsm|mob|fax|phone|whatsapp|wa)(?=[+0]\d)"
)
_PHONE_DIGITS = r"\d[\d().\- ]*\d\)?(?!\w)(?!-\w)(?!\.\d)"
# A number after "word:" is a phone when it has a phone's shape (tel:+44..., Tel:0612...). After
# "word/", "word-", "word." or "word," only a + form is, because a + is no version or decimal and a
# path holds plain digit runs. "@" with no word before it is a mention of an unsaved contact, while
# "foo@1.2.3" is a package.
_PHONE = re.compile(
    r"(?<![\w+$\u20ac\u00a3\u00a5])(?:(?=\+)|(?<!\w@)(?<!\w[-.,/]))"
    rf"(?:\+\s?|\()?{_PHONE_DIGITS}"
)
# Right after a phone, a mark starts the next number: "0612345678/0698765432". A + form needs no
# help here: _PHONE reads it, so a long list of them costs no second walk.
_NEXT_PHONE = re.compile(rf"[/,-](?P<number>\(?{_PHONE_DIGITS})")
# A place settles that the digits are a phone, with no + or 0: a WhatsApp link, a vCard field, a
# phone= query, a tel: link, a label ("Phone: 4155550132") or words that give a number ("my number
# is 4155550132", "call me at 4155550132"). An order number has none of these.
_PHONE_PLACE = re.compile(
    r"(?i)(?:(?<![\w.-])wa\.me/\+?|(?<![a-z])(?:waid=|phone=|tel:)\+?"
    r"|(?<![a-z])(?:tel|t[e\u00e9]l[e\u00e9]phone|phone|mobile|cell|gsm|whatsapp)"
    r"[ \t]*+[:=][ \t]*+\+?"
    r"|(?<![a-z])(?:my|our|your|his|her|their)[ \t]++(?:(?:phone|mobile|cell|whatsapp)[ \t]++)?"
    r"(?:number|num[e\u00e9]ro)[ \t]++(?:is|est)[ \t]++\+?"
    r"|(?<![a-z])(?:call|text|ring|reach|whatsapp)[ \t]++(?:me|him|her|them|us)[ \t]++"
    r"(?:on|at)[ \t]++\+?"
    r")(?P<number>\d{7,15})(?!\d)"
)
# A URL writes + as %2B, as WhatsApp's own redirect does. The digits are the number.
_ENCODED_PHONE = re.compile(r"(?i)(?<![\w%])%2B(?P<number>\d{9,15})(?!\d)")
# The US form: 415-555-0132, 415.555.0132, 415 555 0132, 415 555-0132, 1-415-555-0132.
_NANP = re.compile(r"[2-9]\d\d(?:([-. ])\d{3}\1| \d{3}-)\d{4}|1([-. ])[2-9]\d\d\2\d{3}\2\d{4}")
_IPV4 = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")
_DIGIT_GROUP = re.compile(r"\d+")
_DOUBLE_SEPARATOR = re.compile(r"[ .-]{2,}")
# A group ends at a space, or at a dash before a 0: "0612345678-0698765432" is two numbers.
_GROUP_OF_PHONE = re.compile(r"[^ -]+(?:-(?!0\d)[^ -]*)*")
_DECIMAL = re.compile(r"0[.,]\d+")

# A handle may hold hyphens, like a GitHub login, and no length limit cuts it: a limit would leave a
# long one's tail in the stored text.
_HANDLE = re.compile(r"(?<![\w@])@(?P<name>\w[\w.-]*\w)")
# Words after "@" in code and chat tools that never name a person.
_NOT_HANDLES = frozenset(
    "everyone here channel media import keyframes supports charset param returns return type "
    "typedef deprecated see example throws override tailwind apply "
    # CSS at-rules, Tailwind v4 included.
    "layer theme font-face font-feature-values counter-style container custom-variant plugin "
    "source utility variant config page namespace property starting-style scope "
    # TypeScript directives and JSDoc tags.
    "ts-ignore ts-expect-error ts-nocheck ts-check jsx link default since author internal "
    "template remarks module license private public protected readonly callback enum event "
    "fires implements interface memberof inheritdoc typeparam category "
    # npm dist tags.
    "latest next canary beta".split()
)
# Names that a person's handle does not take, by shape. A number too short for a phone is a price
# or the hour of a time (@20, @10:30), and a phone-length one stays a mention (@447700900123). Then
# a time (@3pm, @15h, @14h30), a short dotted version (@1.2.3), a scale (@2x), a file mention (a
# mention with "/" never gets here) and an agent. ".md", ".js" and ".sh" also end a person's handle
# (@dr.jane.md, @karim.js), so those need a stem in capitals (@CLAUDE.md).
_NOT_A_HANDLE_SHAPE = re.compile(
    r"\d{1,6}|\d{1,2}(?i:[ap]m|h\d{0,2})|\d{1,3}(?:\.\d{1,3}){1,2}|\d{1,2}[xX]"
    r"|[\w.-]+\.(?:py|ts|tsx|jsx|json|toml|ya?ml|txt|lock|css|html|sql)"
    r"|[A-Z0-9_-]+\.(?:md|js|sh)"
    r"|agent-[\w.-]+"
)
# Python decorators that a text names anywhere: "the `@property` on x", "use @pytest.fixture".
_DECORATOR_NAMES = frozenset(
    "property staticmethod classmethod dataclass abstractmethod cached_property contextmanager "
    "asynccontextmanager wraps lru_cache cache total_ordering singledispatch overload final "
    "runtime_checkable pytest field_validator model_validator model_serializer validator "
    "computed_field retry login_required unit_of_work".split()
)
_DECORATOR_MODULES = frozenset(
    "pytest functools dataclasses abc typing contextlib app router api bp blueprint click celery "
    "mock patch unittest pydantic mcp tenacity modal".split()
)
# A line may start with a line number from a tool's output and a diff marker.
_LINE_PREFIX = r"(?:[ \t]*\d+(?:\t|\u2192))?(?:[+\- ](?=[ \t]*(?:@|def\s|async\s|class\s)))?[ \t]*"
_LINE_START = re.compile(rf"^{_LINE_PREFIX}", re.MULTILINE)
_NEXT_LINE = re.compile(
    rf"{_LINE_PREFIX}(?:(?P<code>def\s|async\s+def\s|class\s)|@(?P<name>\w[\w.]*))"
)
_DECORATOR_STACK = 8
_WORD_LOOK_BACK = 300
# A link to a social profile holds the handle. A GitHub link is not in the list: its first path
# part is mostly an organisation, and dev text holds repository links all the time. The subdomains
# stay narrow per host, because any subdomain would read docs.x.com/overview as a profile.
_PROFILE_NAME = r"[A-Za-z0-9_][A-Za-z0-9_.-]{1,63}+"
_PROFILE_URL = re.compile(
    r"(?i)(?<![\w.-])(?:https?://)?(?:"
    r"(?:www\.|m\.|mobile\.)?(?:t\.me(?:/s)?|telegram\.me|instagram\.com|x\.com|twitter\.com"
    r"|threads\.net|tiktok\.com|snapchat\.com/add|fb\.com|fb\.me|m\.me)"
    r"|(?:www\.|m\.|mobile\.|web\.|[a-z]{2}-[a-z]{2}\.)?facebook\.com"
    r"|(?:www\.|m\.|mobile\.|[a-z]{2}\.)?linkedin\.com/in"
    r")(?:/profile\.php\?(?:[^\s#]{0,200}&)?id=|/@?)"
    rf"(?P<name>{_PROFILE_NAME})"
)
# A service label or a scheme and one handle-shaped word: "IG: orla.vance", "skype:orla?call". A
# short number is a count ("Telegram: 12 messages").
_LABELLED_HANDLE = re.compile(
    r"(?i)(?<![\w.-])(?:"
    r"(?:ig|insta|instagram|telegram|tg|skype|snap|snapchat|discord)[ \t]*:[ \t]*@?"
    r"|tg://resolve\?(?:[^\s#]{0,200}&)?domain="
    r")(?!\d{1,6}(?![\w.-]))"
    rf"(?P<name>{_PROFILE_NAME})(?!['\w])"
)
_NOT_A_PROFILE = (
    frozenset(
        "i home search explore hashtag intent share sharer login joinchat s p reel reels stories "
        "tv settings messages notifications groups pages watch events about help privacy legal "
        "tos policies status web download developers business ads jobs company school feed in pub "
        "directory signup accounts oauth dialog plugins tag discover music video live profile.php "
        # The words that follow a service label in prose: "Telegram: the bot is ready".
        "a an and or is are no yes bot bots api app chat chats channel channels group connector "
        "webhook message link user users account".split()
    )
    | _PHRASE_STOP
    | _NOT_A_VALUE
)


def phone_key(text: str) -> str:
    """Digits only; a 00 prefix is the international +."""
    digits = ascii_digits(text)
    return digits[2:] if digits.startswith("00") else digits


@dataclass(frozen=True, repr=False)
class OwnIdentifiers:
    """The person's own emails, phones and handles: kept, never counted."""

    emails: frozenset[str] = frozenset()
    phones: frozenset[str] = frozenset()
    handles: frozenset[str] = frozenset()

    @classmethod
    def parse(cls, values: Iterable[str]) -> "OwnIdentifiers":
        emails, phones, handles = set(), set(), set()
        for value in values:
            value = value.strip()
            if "@" in value[1:]:
                emails.add(value.lower())
            elif value.startswith("@"):
                handles.add(value[1:].lower())
            elif len(phone_key(value)) >= 6:
                phones.add(phone_key(value))
            elif value:
                handles.add(value.lower())
        return cls(frozenset(emails), frozenset(phones), frozenset(handles))

    def __len__(self) -> int:
        return len(self.emails) + len(self.phones) + len(self.handles)

    def __repr__(self) -> str:
        return f"OwnIdentifiers(count={len(self)})"


def _blank(text: str, pattern: re.Pattern[str], fill: str = "x") -> str:
    return pattern.sub(lambda m: fill * len(m.group()), text)


def _phone_shape(candidate: str) -> bool:
    if _NANP.fullmatch(candidate):
        return True
    # The first digit in any script: an Arabic-Indic zero is a zero. A candidate can be empty: a
    # group that is only ")" loses it in _without_lone_paren ("1 ) (2").
    lead = candidate[:1]
    if lead.isdecimal():
        lead = ascii_digits(lead)
    if _DOUBLE_SEPARATOR.search(candidate) or lead not in ("+", "0", "("):
        return False
    # "0.5 0.25 0.125" is a list of decimals, not a number.
    if any(_DECIMAL.fullmatch(group) for group in candidate.split(" ")):
        return False
    # "(148273645 bytes)" opens a parenthesis for a size, "0 1 2 3 4" is a list of ids,
    # "000123456" is a padded counter and "010.000.000.001" is an address.
    if lead == "(" and ")" not in candidate:
        return False
    if sum(len(group) == 1 for group in _DIGIT_GROUP.findall(candidate)) >= 3:
        return False
    if ascii_digits(candidate).startswith("000") or _IPV4.fullmatch(candidate):
        return False
    return 9 <= len(ascii_digits(candidate)) <= 15


def _without_lone_paren(text: str, start: int, end: int) -> int:
    """The end of a candidate without a closing parenthesis that opens nowhere in it."""
    if text[end - 1] == ")" and "(" not in text[start:end]:
        return end - 1
    return end


def _phone_spans(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """The phones in one candidate run.

    Two numbers split by one space read as one run of more than 15 digits, so a run that is not
    one phone is cut into groups and read again: a group that starts with + or ( starts a new
    number, and so does a group that starts with 0 once nine digits are taken.
    """
    end = _without_lone_paren(text, start, end)
    if _phone_shape(text[start:end]):
        return [(start, end)]
    groups = [
        (start + m.start(), start + m.end()) for m in _GROUP_OF_PHONE.finditer(text[start:end])
    ]
    counts = [len(ascii_digits(text[s:e])) for s, e in groups]
    spans = []
    first = 0
    while first < len(groups):
        last, digits = first, counts[first]
        while last + 1 < len(groups):
            lead = text[groups[last + 1][0]]
            starts_number = lead in "+(" or (lead == "0" and digits >= 9)
            if starts_number or digits + counts[last + 1] > 15:
                break
            last += 1
            digits += counts[last]
        piece_end = _without_lone_paren(text, groups[first][0], groups[last][1])
        if _phone_shape(text[groups[first][0] : piece_end]):
            spans.append((groups[first][0], piece_end))
            first = last + 1
        else:
            first += 1
    return spans


def _decorator(text: str, at: int, end: int, first_on_line: set[int]) -> bool:
    """`@name(` anywhere, because a mention takes no call. Or `@name` that ends its line, first on
    its line, before a def or a class.

    The design reads any `@name` that ends its line as a decorator. A chat line can be only
    "@orla", so this build asks for the def or class below it, through a stack of decorators.
    """
    if text.startswith("(", end):
        return True
    if at not in first_on_line:
        return False
    position = end
    for _ in range(_DECORATOR_STACK):
        line_end = text.find("\n", position)
        if line_end == -1:
            return False
        rest = text[position:line_end].strip()
        if rest and not rest.startswith("#"):
            return False
        below = _NEXT_LINE.match(text, line_end + 1)
        if below is None:
            return False
        if below["code"] is not None or text.startswith("(", below.end()):
            return True
        position = below.end()
    return False


def _named_decorator(name: str) -> bool:
    lowered = name.lower()
    return lowered in _DECORATOR_NAMES or (
        "." in lowered and lowered.split(".", 1)[0] in _DECORATOR_MODULES
    )


def _phones(blanked: str) -> Iterator[tuple[int, int]]:
    for match in _PHONE.finditer(blanked):
        found = _phone_spans(blanked, *match.span())
        while found:
            yield from found
            after = _NEXT_PHONE.match(blanked, found[-1][1])
            found = _phone_spans(blanked, *after.span("number")) if after else []


def _placed_numbers(text: str) -> list[tuple[int, int]]:
    places = chain(_PHONE_PLACE.finditer(text), _ENCODED_PHONE.finditer(text))
    return [match.span("number") for match in places]


def _non_overlapping(spans: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    kept: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if kept and start < kept[-1][1]:
            continue
        kept.append((start, end))
    return kept


def _word_before(text: str, at: int) -> str:
    """The word that ends at `at`, within a short look back: a longer one reads as no URL, and
    the address in it is then masked as an email, which is the safe side."""
    pieces = text[max(0, at - _WORD_LOOK_BACK) : at].rsplit(None, 1)
    return pieces[-1] if pieces else ""


def contacts(text: str, own: OwnIdentifiers | None = None) -> list[Hit]:
    own = own or OwnIdentifiers()
    hits: list[Hit] = []
    emails = []
    # "admin:hunter2@db.example.com": the credential step masks the secret, and the host stays.
    userinfo = {m.start("value") for m in _BARE_USERINFO.finditer(text) if _mixed(m["value"])}
    for match in _EMAIL.finditer(text):
        start, end = match.span("address")
        address = match["address"]
        if start in userinfo:
            continue
        # user:secret@host in a URL is its userinfo (the credential step masks it). A label such
        # as "Email:" or a vCard field before the address is no URL.
        if start and text[start - 1] == ":" and "://" in _word_before(text, start):
            continue
        last_label = address.rsplit(".", 1)[1].lower()
        remote = text.startswith(":", end) and (
            address.lower().startswith("git@") or _SCP_PATH.match(text, end) is not None
        )
        host = text.startswith("-", end) and (
            last_label in _HOST_LABELS or _HOST_ALIAS.match(text, end) is not None
        )
        if remote or host or last_label in _FILE_SUFFIXES:
            continue
        # A file name such as "orla@x.example.txt" keeps its suffix out of the address.
        trimmed = _FILE_TAIL.sub("", address)
        if trimmed != address and "." in trimmed.rpartition("@")[2]:
            address, end = trimmed, start + len(trimmed)
        emails.append((start, end))
        key = unquote(address).lower()
        names_no_person = key in _MACHINE_ADDRESSES or key.rpartition("@")[2] in _PLACEHOLDER_HOSTS
        if key not in own.emails and not names_no_person:
            hits.append(Hit(start, end, CONTACT_EMAIL, key))
    # An own email is kept whole: no phone or handle is cut out of it.
    in_emails = _Cover(len(text), emails)

    blanked = _blank(_blank(text, _PHONE_BARRIERS), _PHONE_GLUE, " ")
    phones = _non_overlapping([*_phones(blanked), *_placed_numbers(text)])
    for start, end in phones:
        if in_emails.touches(start, end):
            continue
        key = phone_key(text[start:end])
        if key not in own.phones:
            hits.append(Hit(start, end, CONTACT_PHONE, key))

    first_on_line = {m.end() for m in _LINE_START.finditer(text)} if "@" in text else set()
    handles = []
    for match in _HANDLE.finditer(text):
        start, end = match.span()
        name = match["name"]
        if text.startswith("/", end) or name.lower() in _NOT_HANDLES or _named_decorator(name):
            continue
        if _NOT_A_HANDLE_SHAPE.fullmatch(name):
            continue
        if _decorator(text, start, end, first_on_line) or in_emails.touches(start, end):
            continue
        handles.append((start, end, name))
    for match in chain(_PROFILE_URL.finditer(text), _LABELLED_HANDLE.finditer(text)):
        name = match["name"].rstrip("._-")
        start = match.start("name")
        if len(name) >= 2 and name.lower() not in _NOT_A_PROFILE:
            handles.append((start, start + len(name), name))
    covered = 0
    for start, end, name in sorted(handles):
        if start < covered:
            continue
        covered = end
        if name.lower() not in own.handles:
            hits.append(Hit(start, end, CONTACT_HANDLE, name.lower()))
    return hits


# Pay amounts ----------------------------------------------------------------------------------

# The groups are bounded: an amount has few, and a long table of three-digit numbers would rescan
# to its end from each group, which is quadratic.
_NUMBER = r"(?:\d{1,3}(?:[,. ]\d{3}){1,6}(?:[.,]\d{1,2})?|\d+(?:[.,]\d+)?)"
_MULTIPLIER = r"(?:\s?(?:k|K|m|M|bn)(?![A-Za-z]))?"
_SYMBOL = r"(?:(?:US|CA|AU|NZ|HK|SG)?\$|[€£¥₹])"
_CODES = r"(?i:USD|EUR|GBP|MAD|AED|CHF|CAD|AUD|SAR)"
_WORDS_AFTER = (
    r"(?i:dhs?|euros?|dollars?|dirhams?|pounds?|bucks?|grand"
    r"|درهم|دراهم)"
)
# An amount that names its money.
_MARKED = (
    rf"{_SYMBOL}\s?{_NUMBER}{_MULTIPLIER}"
    rf"|{_CODES}\s?{_NUMBER}{_MULTIPLIER}"
    rf"|{_NUMBER}{_MULTIPLIER}\s?(?:{_CODES}|{_WORDS_AFTER}|{_SYMBOL})"
)
_SUFFIXED = rf"{_NUMBER}(?:[kKmM]|bn)"
_BARE = r"\d{1,3}(?:[,. ]\d{3}){1,6}|\d{3,}+"
_AMOUNT = re.compile(rf"(?<![\w.,])(?:(?P<marked>{_MARKED})|{_SUFFIXED})(?!\w)")
# Three digits or more with no marker. A percentage is no sum, and "-" after a word character is a
# reference or a date ("PO-2026-00417") or the far end of a range, which _whole_range reads.
_FIGURE = re.compile(rf"(?<![\w.,/#])(?<!\w-)(?:{_BARE})(?:[.,]\d{{1,2}})?(?!\w|[.,]\d|\s?%)")

_PERIOD = (
    r"per\s+(?:month|mo|hour|day|year|annum)|par\s+(?:mois|an|heure|jour)"
    r"|every\s+(?:month|year|week)|an?\s+(?:month|hour|year)|monthly|hourly|yearly|annual"
)
_SLASH_PERIOD = r"/\s?(?:month|mois|mo|year|yr|hour|hr|week|wk)"
# Says pay by itself, so any figure a few words on is a sum.
_STRONG_PAY = (
    r"salary|salaries|salaires?|wages?|income|stipends?|retainers?|compensation|total\s+comp"
    r"|r[eé]mun[eé]rations?|earn(?:s|ed|ing|ings)?|gagn(?:e|es|ons|ez|ent)"
    r"|paie|pay[eé]e?s?|take(?:\s+|-)home|(?-i:TC|OTE|TJM)"
    r"|(?:my|our|his|her|their|the)\s+pay"
    r"|(?:ال)?راتب"
    r"(?:ي|ك|ه|ها|نا|هم)?"
)
# Also names other things, so a figure a few words on needs no noun after it: "makes 5000 requests".
# "rate limit", "make sure" and "pay attention" are never pay, whatever follows.
_WEAK_PAY = (
    r"pay(?!\s++attention)|pays|paid|paying|bonus(?:es)?|raises?"
    r"|rates?(?![\s-]++limit)|makes?(?!\s++sure)"
)
# Mostly names other things ("base 256", "net 443"). A marked or suffixed amount counts only a few
# words after the word, and a figure must also have four digits or more.
_NEAR_PAY = r"base|package|net|gets?|offers?|offered|comp|touche|prime|augmentation|propos[eé]e?"
_PAY_WORD = re.compile(
    rf"(?i)(?:(?<!\w)(?:(?P<strong>{_STRONG_PAY})|(?P<weak>{_WEAK_PAY})|(?P<near>{_NEAR_PAY})"
    rf"|{_PERIOD})|{_SLASH_PERIOD})(?!\w)"
)
_PERIOD_AFTER = re.compile(rf"(?i)\s*+(?:{_PERIOD}|{_SLASH_PERIOD})(?!\w)")
# What may follow a sum that needs no noun after it. Any other word reads as its noun.
_NO_NOUN_AFTER = re.compile(
    r"(?i)[ \t]*+(?:\Z|[^ \t\w/]"
    rf"|(?:{_PERIOD}|{_SLASH_PERIOD}"
    r"|this|last|next|now|net|gross|brut|and|or|but|plus|for|in|on|at|to|from|after|before|when|if"
    r"|so|et|pour)(?!\w))"
)
_YEAR_BEFORE = re.compile(r"(?i)(?<!\w)(?:in|on|for|since|from|until|by|during|en|depuis)\s++\Z")
_YEAR_BEFORE_CHARS = 12
_RANGE_JOIN = r"(?:\s*+[-–—]\s*+|\s++(?i:to|and|et|à)\s++)"
# The far end needs a marker or three digits: "paid 25000 and 3 days off" holds no range.
_RANGE_END = re.compile(rf"{_RANGE_JOIN}(?:{_MARKED}|{_SUFFIXED}|{_BARE})(?!\w)")
_RANGE_START = re.compile(rf"(?<![\w.,])(?:{_MARKED}|{_SUFFIXED}|{_NUMBER}){_RANGE_JOIN}\Z")
_RANGE_START_CHARS = 48
PAY_WINDOW_WORDS = 8
PAY_FOLLOW_WORDS = 5
_NEAR_FIGURE_CHARS = 4
_WORD_TOKEN = re.compile(r"\S+")


def _whole_range(text: str, start: int, end: int) -> tuple[int, int]:
    """A range leaves no end behind: "25,000-30,000 MAD", "salary between 25000 and 30000"."""
    before = _RANGE_START.search(text, max(0, start - _RANGE_START_CHARS), start)
    after = _RANGE_END.match(text, end)
    return (start if before is None else before.start(), end if after is None else after.end())


def _no_noun_after(text: str, start: int, end: int) -> bool:
    return _NO_NOUN_AFTER.match(text, _whole_range(text, start, end)[1]) is not None


def _names_a_year(text: str, figure: re.Match[str]) -> bool:
    """ "the salary 2026 budget" and "paid in 2026" name a year; "my salary is 2000" gives a sum."""
    if not _year_like(figure.group()):
        return False
    start = figure.start()
    before = _YEAR_BEFORE.search(text, max(0, start - _YEAR_BEFORE_CHARS), start)
    return before is not None or not _no_noun_after(text, *figure.span())


def _follows(word_ends: list[int], at: int) -> bool:
    """A listed word ends at most PAY_FOLLOW_WORDS words before the word at `at`."""
    last = bisect_right(word_ends, at) - 1
    return last >= 0 and at - word_ends[last] <= PAY_FOLLOW_WORDS


def pay_amounts(text: str) -> list[Hit]:
    """A sum the words around it say is pay: a currency amount within about eight words of a
    compensation word, or a bare figure before a period or after a pay word. The words stay."""
    starts = [m.start() for m in _WORD_TOKEN.finditer(text)]

    def word_index(offset: int) -> int:
        return bisect_right(starts, offset) - 1

    def word_ends(group: str) -> list[int]:
        return [word_index(w.end() - 1) for w in pay_words if w[group]]

    pay_words = list(_PAY_WORD.finditer(text))
    strong, weak, near = word_ends("strong"), word_ends("weak"), word_ends("near")
    words = [(word_index(w.start()), word_index(w.end() - 1)) for w in pay_words if not w["near"]]
    firsts = [first for first, _ in words]
    spans = []
    for amount in _AMOUNT.finditer(text):
        first, last = word_index(amount.start()), word_index(amount.end() - 1)
        # Only the pay words that can be in range; one spans at most a few words ("per month").
        low = bisect_left(firsts, first - PAY_WINDOW_WORDS - 3)
        high = bisect_right(firsts, last + PAY_WINDOW_WORDS)
        if any(
            w_first - last <= PAY_WINDOW_WORDS and first - w_last <= PAY_WINDOW_WORDS
            for w_first, w_last in words[low:high]
        ) or (_follows(near, first) and (amount["marked"] or _no_noun_after(text, *amount.span()))):
            spans.append(amount.span())
    for figure in _FIGURE.finditer(text):
        at = word_index(figure.start())
        if _PERIOD_AFTER.match(text, figure.end()) is not None:
            spans.append(figure.span())
        elif not _names_a_year(text, figure) and (
            _follows(strong, at)
            or (
                (
                    _follows(weak, at)
                    or (_follows(near, at) and len(figure.group()) >= _NEAR_FIGURE_CHARS)
                )
                and _no_noun_after(text, *figure.span())
            )
        ):
            spans.append(figure.span())
    return _hits(PAY_AMOUNT, (_whole_range(text, *span) for span in spans))
