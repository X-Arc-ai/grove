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
from urllib.parse import SplitResult, urlsplit

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
_URL = re.compile(rf"(?i)(?:{_SCHEME}|(?<![a-z0-9.-])www\.)[^\s<>\"'`]+")
_URL_TAIL = ".,;:!?)]}'\""


def urls(text: str) -> Iterator[tuple[int, int]]:
    for match in _URL.finditer(text):
        yield match.start(), match.start() + len(match.group().rstrip(_URL_TAIL))


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
# "sk-" key.
_PREFIXED = re.compile(
    r"(?<![A-Za-z0-9_-])(?:"
    r"sk-[A-Za-z0-9_-]{20,}"
    r"|(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Za-z0-9])"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}"
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
    r")"
)
# A webhook URL is its own secret: the host stays, the path after it goes.
_WEBHOOK = re.compile(
    r"(?i)(?:hooks\.slack\.com/(?:services|workflows|triggers)/"
    r"|discord(?:app)?\.com/api/webhooks/)(?P<value>[A-Za-z0-9/_-]{16,})"
)
_BEARER = re.compile(r"(?i)(?<![A-Za-z])bearer\s+(?P<value>[A-Za-z0-9._~+/=-]{16,})")
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
    r"|signature)"
    r"=(?P<value>[^&#\s]+)"
)
# An environment suffix after the secret word: API_KEY_PROD, SECRET_KEY_BASE, token_v2.
_SUFFIX = (
    r"(?:PROD|PRODUCTION|LIVE|TEST|DEV|STAGING|STAGE|BASE|RO|RW|OLD|NEW|PRIMARY|SECONDARY|BACKUP"
    r"|V?\d{1,3})"
)
# A name that says secret: MODEL_API_KEY, GH_TOKEN, NEO4J_AUTH; api_key, client_secret, password;
# apiKey, clientSecret, accessToken.
_SECRET_NAME = (
    # The repeats are bounded, so a long dotted chain costs linear time, not quadratic.
    r"(?:[A-Z0-9]+_){1,6}(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASS|PWD|AUTH|CREDENTIALS?)"
    rf"(?:_{_SUFFIX}){{0,3}}"
    r"|(?:TOKEN|SECRET|PASSWORD|PASSWD)"
    r"|(?:[A-Za-z0-9]+[_.-]){0,6}(?i:api[_-]?key|apikey|secret|secret[_-]?key|token|password|passwd"
    r"|pwd|passphrase|private[_-]?key|access[_-]?key|credentials?)"
    rf"(?:[_-](?i:{_SUFFIX})){{0,3}}"
    r"|(?:api|secret|private|access|client|auth|signing|encryption)Key"
    r"|[a-z][a-z0-9]*(?:Secret|Token|Password)"
)
_ASSIGNMENT = re.compile(
    rf"(?<![A-Za-z0-9_])[\"']?(?P<name>{_SECRET_NAME})[\"']?(?P<sep>\s*[:=]\s*)"
    r"(?:\"(?P<dq>[^\"\n]+)\"|'(?P<sq>[^'\n]+)'|`(?P<bq>[^`\n]+)`"
    r"|(?P<bare>[^\s\"'`,;()\[\]{}<>]+))"
)
# Words and types that a bare value can be in code, never a secret.
_NOT_A_VALUE = frozenset(
    "none null nil true false undefined required self this await async new yield lambda "
    "function return string str bytes int bool boolean number any unknown object optional "
    "secretstr secretbytes".split()
)
# settings.api_key, process.env.MODEL_API_KEY, Settings.API_KEY: a name, not a value.
_CODE_REFERENCE = re.compile(
    r"(?:[a-z_$][a-z0-9_$]*|[A-Z][a-z]+(?:[A-Z][a-z]+)*)(?:\.[a-z_$][a-z0-9_$]*)*"
    r"\.[A-Za-z_$][A-Za-z0-9_$]*"
)
# SecretStr, ApiKey, AccessToken: a type in an annotation.
_TYPE_NAME = re.compile(r"(?:[A-Z][a-z]+)+(?:Str|String|Bytes|Type|Field|Key|Token|Secret|Value)")
# The .env form NAME=value at the start of a line: the value runs to the end of the line.
_ENV_LINE = re.compile(r"(?m)^[ \t]*(?:export[ \t]+)?$")
_ENV_LOOK_BACK = 40
_TRAILING_COMMENT = re.compile(r"[ \t]+#.*$|[ \t]+$")
# "my password is X", "pw: X", "the pin is 4821". A PIN is digits, so "the pin is 2026.10.1" is
# a version.
_PHRASE = re.compile(
    r"(?i)(?<![A-Za-z])(?:"
    r"(?P<word>password|passphrase|passwd|passcode|api\s+key|secret\s+key)"
    r"|(?P<weak>pw|pwd|pass)"
    r"|(?P<pin>pin(?:\s+code)?)"
    r")(?![A-Za-z])"
    r"(?P<sep>\s+(?:is|was)\s*+:?\s*+|\s*[:=]\s*+|\s+[-\u2013\u2014]+>?\s++|\s*(?:->|=>|\u2192)\s*+)"
    r"(?:[\"'\u201c\u2018](?P<quoted>[^\"'\u201d\u2019\n]+)[\"'\u201d\u2019]"
    r"|(?P<value>[^\s\"'\u201c\u201d,;()\[\]{}<>]+))"
)
_PHRASE_STOP = frozenset(
    "wrong correct incorrect invalid expired required missing empty reset changed saved stored "
    "the not now still very here there below above attached same different fine okay weak "
    "strong long short secure safe encrypted hashed visible shown hidden case-sensitive being "
    "never always also just only what that this your our their written listed included known "
    "unknown shared sent default rotated revoked too gone ready live active blank unset needed "
    "optional private public broken fixed set good bad new old simple per-world per-user "
    "enough sensitive valid".split()
)
# A plain word after "password is" is prose ("the API key is configured") unless it is said as
# someone's own: "my password is sunshine", "the wifi password is lemonade".
_PLAIN_WORD = re.compile(r"[a-z]+(?:-[a-z]+)*|[A-Z][a-z]+(?:-[a-z]+)*")
_OWNER = re.compile(r"(?i)(?:(?<![a-z])(?:my|our|your|his|her|their)\s+(?:[a-z-]+\s+)?|wi-?fi\s+)$")
_PROSE_ENDING = re.compile(r"(?:ing|ed|ly)$")
# "login: orla / Tr0ub4dor3", "creds: admin / Tr0ub4dor3". The user part is as long as an email
# address at most, and it is never given back: "login=login=..." would rescan to the end of the
# text from each start, which is quadratic.
_LOGIN_PAIR = re.compile(
    r"(?i)(?<![A-Za-z])(?:logins?|creds?|credentials?)\s*+[:=]\s*+[^\s/:]{1,256}+\s*+/\s*+"
    r"(?P<value>[^\s\"',;]+)"
)
_CONTEXT = re.compile(
    r"(?i)(?<![a-z])(?:keys?|tokens?|secrets?|passwords?|credentials?|api|authorization)"
    r"(?![a-z])"
)
# Dev text puts git shas, hashes and ids next to these words all the time, so a hex token or a
# UUID (also one after a prefix such as t_screening_) needs the context word right before it:
# "api key 3f2a...", "the token is 0193...", "DD_APP_KEY_RO=3f2a...".
_HEX_OR_UUID = re.compile(
    r"[0-9a-f]+|[0-9A-F]+"
    r"|(?:[A-Za-z][A-Za-z0-9]*_)*[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
)
_CONTEXT_JUST_BEFORE = re.compile(
    r"(?i)(?<![a-z])(?:keys?|tokens?|secrets?|passwords?|credentials?|api)(?![a-z])"
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
    if "/" in token and classes < 3:
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


def _same_name(name: str, value: str) -> bool:
    """api_key=api_key, token=token, self.token = token, apiKey: apiKey."""

    def squash(text: str) -> str:
        return re.sub(r"[_-]", "", text).lower()

    last = re.split(r"[._-]", name)[-1]
    return squash(value) in (squash(name), squash(last))


def _bare_value_is_secret(text: str, name: str, sep: str, start: int, value: str) -> bool:
    after = text[start + len(value) : start + len(value) + 1]
    if len(value) < 4 or value.lower() in _NOT_A_VALUE:
        return False
    # $VAR and ${VAR} are references, a call, a subscript or a generic is code, settings.api_key
    # is a name, and a value equal to its name passes the variable on.
    if value.startswith("$") or after in ("(", "[", "<") or _CODE_REFERENCE.fullmatch(value):
        return False
    if _same_name(name, value):
        return False
    # client_secret: SecretStr is an annotation.
    return not (":" in sep and _TYPE_NAME.fullmatch(value))


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
    value = match["value"].rstrip(".,;:!?)")
    start = match.start("value")
    if match["pin"] is not None:
        return (start, start + len(value)) if re.fullmatch(r"\d{4,12}", value) else None
    if len(value) < 4 or value.lower() in _PHRASE_STOP:
        return None
    if not _bare_value_is_secret(text, match["word"] or match["weak"], match["sep"], start, value):
        return None
    if match["weak"] is not None:
        # "pass" and "pw" are common words: "pass: 1200" is a test count, "pass: Tr0ub4dor3" is
        # a password. The value must mix in a digit or a symbol and not be a number.
        mixed = any(not ch.isalpha() for ch in value) and not value.replace(",", "").isdigit()
        return (start, start + len(value)) if mixed else None
    if _PLAIN_WORD.fullmatch(value):
        said_as_own = _OWNER.search(text, max(0, match.start() - 40), match.start()) is not None
        labelled = re.search(r"[:=]", match["sep"]) is not None
        if _PROSE_ENDING.search(value.lower()) or not (labelled or said_as_own):
            return None
    return start, start + len(value)


_BASIC_LOOK_BACK = 200


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
        bare = match.group("bare")
        if bare is not None and _bare_value_is_secret(
            text, match["name"], match["sep"], match.start("bare"), bare
        ):
            end = _env_value_end(text, match) or match.end("bare")
            spans.append((match.start("bare"), max(end, match.end("bare"))))
    for match in _PHRASE.finditer(text):
        span = _phrase_value(text, match)
        if span is not None:
            spans.append(span)
    for match in _LOGIN_PAIR.finditer(text):
        value = match["value"].rstrip(".,;:!?)")
        if len(value) >= 4 and value.lower() not in _PHRASE_STOP:
            spans.append((match.start("value"), match.start("value") + len(value)))
    spans += _context_tokens(text)
    return _hits(CREDENTIAL, spans)


def _context_tokens(text: str) -> list[tuple[int, int]]:
    """A random-looking token is a credential only with a context word on its line."""
    spans = []
    in_urls = _Cover(len(text), urls(text))
    offset = 0
    for line in text.split("\n"):
        if _CONTEXT.search(line):
            for match in _TOKEN.finditer(line):
                start, end = offset + match.start(), offset + match.end()
                token = match.group()
                if not _random_looking(token) or in_urls.holds(start, end) or wordy(token):
                    continue
                hex_or_uuid = _HEX_OR_UUID.fullmatch(token.rstrip("="))
                # A few words fit in the window, so a long line costs no more per token.
                window = max(0, match.start() - _JUST_BEFORE_CHARS)
                if hex_or_uuid and not _CONTEXT_JUST_BEFORE.search(line, window, match.start()):
                    continue
                spans.append((start, end))
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

_OTC_CODE = r"(?:[A-Za-z]{1,4}-)?(?:\d{4,8}|\d{3}[ -]\d{3})"
_CODE_TOKEN = re.compile(rf"(?<![\w.:/-])(?:{_OTC_CODE})(?!\w)(?![.:/,-]\d)")
# Right after a strong phrase a code may hold letters too, when it holds a digit.
# "python3" and "utf8" are words with a number, not codes.
_ALNUM_CODE = (
    r"(?=[A-Za-z0-9-]*\d)(?![a-z]{3,}\d{1,2}(?![A-Za-z0-9-]))"
    r"(?:[A-Za-z0-9]{4,10}|[A-Za-z0-9]{3,4}-[A-Za-z0-9]{3,4})"
)
_RIGHT_AFTER = re.compile(
    rf"\s*+(?:(?:is|was)\s*+)?[:=-]?\s*+(?P<code>{_OTC_CODE}|{_ALNUM_CODE})(?![\w-])(?![.:/,]\w)"
)
_RIGHT_AFTER_DIGITS = re.compile(
    rf"\s*+(?:(?:is|was)\s*+)?[:=-]?\s*+(?P<code>{_OTC_CODE})(?![\w-])(?![.:/,]\w)"
)
# The services that text a code. "signal" and "github" are left out: "the signal code" and "the
# github code" are dev talk, and their real messages match "your ... code" or "launch code".
_OTC_BRANDS = (
    "whatsapp|telegram|instagram|facebook|snapchat|uber|discord|tiktok|microsoft|apple|amazon"
    "|paypal|google"
)
_OTC_PHRASE = re.compile(
    r"(?i)(?<!\w)(?:"
    r"(?:verification|security|confirmation|access|login|log-in|log\s+in|sign-in|sign\s+in"
    r"|signin|2fa|mfa|two-factor|2-step|registration|launch|activation|recovery|pairing|sms"
    rf"|{_OTC_BRANDS})\s+code"
    r"|one[-\s]time\s+(?:code|passcode|password|pin)"
    r"|otp|passcode|your\s+code\s+is|your\s+code:|enter(?:ing)?\s+(?:the|this|your)\s+code"
    r"(?:\s+below)?"
    r"|(?:do\s+not|don['\u2019]?t|never)\s+share\s+(?:this|your|the)\s+code"
    r"|to\s+verify\s+your"
    r")(?!\w)"
)
# These count only with the code right after them: "update your code at line 1420" is dev talk,
# "Your Uber code: 4829" is not. "auth code" is OAuth talk unless a code follows it.
_OTC_NEAR_ONLY = re.compile(
    r"(?i)(?<!\w)(?:your(?:\s+[\w-]+){0,3}?\s+code|auth(?:entication)?\s+code)(?!\w)"
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
    rf"(?i)(?<![\w./-])(?<!\d:)(?P<code>{_OTC_CODE}|{_ALNUM_CODE})\s+is\s+your"
    r"(?:\s+[\w-]+){0,3}?\s+(?:code|otp|passcode|pin|password|verification)(?!\w)"
)
# "code is" alone is weak: "the code is 1200 lines". It counts only when the code ends a sentence.
_CODE_IS = re.compile(
    rf"(?i)(?<!\w)code\s+is\s*+:?\s*+(?P<code>{_OTC_CODE})(?!\w)(?=\s*(?:$|[.;!?](?:\s|$)|\n))"
)
_DEV_CODE_BEFORE = re.compile(rf"(?i)(?<![a-z])(?:{_NOT_A_CODE_WORDS})\s+$")
# A number that names something else: "PR 1234", "#4821", "line 1420", "port 8080".
_REFERENCE_BEFORE = re.compile(
    r"(?i)(?:(?<![a-z])(?:prs?|pull|issues?|tickets?|lines?|ports?|steps?|rows?|builds?|runs?|jobs?"
    r"|pid|orders?|items?|pages?|version|errors?|status|exit|http)\s*#?\s*|#\s*)$"
)
# A number with a unit or a noun: "1500 ms", "1200 lines".
_UNIT_AFTER = re.compile(
    r"(?i)\s*(?:lines?|loc|ms|s|secs?|seconds?|mins?|minutes?|hrs?|hours?|h|days?|issues?|rows?"
    r"|files?|tests?|bytes?|kb|mb|gb|tokens?|users?|items?|times?|requests?|calls?|records?"
    r"|words?|chars?|characters?|px|errors?|commits?|prs?)(?![a-z0-9])|\s*%"
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


def one_time_codes(text: str, sender: str | None = None) -> list[Hit]:
    spans = [m.span("code") for m in _IS_YOUR.finditer(text)]
    for match in _CODE_IS.finditer(text):
        if _DEV_CODE_BEFORE.search(text, max(0, match.start() - 24), match.start()) is None:
            spans.append(match.span("code"))
    for phrase in _OTC_NEAR_ONLY.finditer(text):
        # "your error code is 1045" is dev talk.
        if phrase.group().split()[-2].lower() in _NOT_A_CODE_LABEL:
            continue
        said = _code_said_right_after(text, phrase.end(), _RIGHT_AFTER)
        if said is not None:
            spans.append(said)
    for label in _LABEL_CODE.finditer(text):
        if label["label"].lower() not in _NOT_A_CODE_LABEL:
            said = _code_said_right_after(text, label.end(), _RIGHT_AFTER_DIGITS)
            if said is not None:
                spans.append(said)
    codes = list(_CODE_TOKEN.finditer(text))
    code_starts = [code.start() for code in codes]
    for phrase in _OTC_PHRASE.finditer(text):
        said = _code_said_right_after(text, phrase.end(), _RIGHT_AFTER)
        if said is not None:
            spans.append(said)
        # Only the codes that can sit inside the window; a code is at most 13 characters.
        low = bisect_left(code_starts, phrase.start() - _WINDOW - 13)
        high = bisect_right(code_starts, phrase.end() + _WINDOW)
        for code in codes[low:high]:
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
            spans.append(code.span())
    if sender and (_OTC_SENDER.search(sender) or _OTC_BRAND_SENDER.fullmatch(sender)):
        spans += [c.span() for c in codes if not _year_like(c.group())]
    return _hits(ONE_TIME_CODE, spans)


# Whole components of a path or query, after camelCase is split: "verifier" and "authors" are
# not link words, "resetPassword" is.
_LINK_WORD = re.compile(
    r"(?<![a-z])(?:reset|resetpassword|password|passwordreset|passwordless|passwd|magic"
    r"|magiclink|verify|verification|confirm|confirmation|signin|sign-in|sign_in|login|log-in"
    r"|auth|oauth|token|otp|invite|invitation|forgot|recover|recovery|activate|activation)"
    r"(?![a-z])"
)
_CAMEL = re.compile(r"([a-z])([A-Z])")
_LINK_PHRASE = re.compile(
    r"(?i)reset\s+your\s+password|password\s+reset\s+link|magic\s+link"
    r"|sign\s+in\s+with\s+this\s+link|(?:log|sign)[\s-]?in\s+link|verify\s+your\s+email"
    r"|forgot\s+your\s+password|link\s+to\s+reset|activate\s+your\s+account"
)
_LINK_PIECE = re.compile(r"[^/?&=#;]+")
# A file name in a link is not a token: "session_tokens_v1.py".
_FILE_PIECE = re.compile(
    r"(?i).*\.(?:py|js|jsx|ts|tsx|md|json|ya?ml|toml|txt|html?|css|rs|go|java|rb|sh|sql|lock|cfg"
    r"|ini|csv|log|xml|png|jpe?g|svg|pdf)"
)
# Host with no scheme ("app.example.com/m/x"): a phone shows it as a link. Only the link check
# reads these, so a file path never counts as a URL for the other detectors.
_BARE_HOST_URL = re.compile(r"(?i)(?<![\w.@/:-])(?:[a-z0-9-]+\.)+(?P<tld>[a-z]{2,24})/[^\s<>\"'`]+")
_FILE_TLD = frozenset("py js jsx ts tsx json yaml yml toml txt html css java rb sql lock".split())
# A code host's own pages: a sha, a branch or a file name there is no token.
_CODE_HOSTS = frozenset({"github.com", "www.github.com", "gitlab.com", "bitbucket.org"})
_CODE_HOST_PAGE = re.compile(
    r"(?i)/[^/]+/[^/]+/(?:blob|tree|commits?|compare|releases|pulls?|issues|actions|raw|blame"
    r"|wiki|tags?|discussions|merge_requests|-)(?:/|$)"
)
_ISSUE_TRACKER_PAGE = re.compile(r"(?i)/[^/]+/(?:issue|project|view|team|document)/")


def _token_like(piece: str) -> bool:
    return (
        len(piece) >= 16
        and re.fullmatch(r"[A-Za-z0-9_~.%-]+", piece) is not None
        and any(ch.isdigit() for ch in piece)
        and any(ch.isalpha() for ch in piece)
        and _FILE_PIECE.fullmatch(piece) is None
        and not wordy(piece)
    )


def _bare_host_urls(text: str) -> Iterator[tuple[int, int]]:
    for match in _BARE_HOST_URL.finditer(text):
        if match["tld"].lower() not in _FILE_TLD:
            yield match.start(), match.start() + len(match.group().rstrip(_URL_TAIL))


def _code_page(parts: SplitResult) -> bool:
    host = (parts.hostname or "").lower()
    if host in _CODE_HOSTS:
        return _CODE_HOST_PAGE.match(parts.path) is not None
    return host == "linear.app" and _ISSUE_TRACKER_PAGE.match(parts.path) is not None


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


def reset_links(text: str) -> list[Hit]:
    with_scheme = list(urls(text))
    # A check costs the span asked about, so many links in one text stay linear.
    in_urls = _Cover(len(text), with_scheme)
    found = with_scheme + [
        (start, end) for start, end in _bare_host_urls(text) if not in_urls.touches(start, end)
    ]
    if found and _LINK_PHRASE.search(text):
        return _hits(RESET_LINK, found)
    spans = []
    for start, end in found:
        link = text[start:end]
        try:
            # With no scheme the host would read as a path, and its words as link words.
            parts = urlsplit(link if "://" in link else f"//{link}")
        except ValueError:
            # A URL the parser refuses cannot be cleared by code, so it is treated as a link.
            spans.append((start, end))
            continue
        if _code_page(parts):
            continue
        # The fragment too: a hash-route app keeps its path there ("/#/reset-password/X").
        rest = f"{parts.path}?{parts.query}#{parts.fragment}"
        word_at = _link_word_at(rest)
        # The token follows the word: "/reset-password?token=X", "/magic/X".
        if word_at is not None and any(
            _token_like(piece.group()) for piece in _LINK_PIECE.finditer(rest, word_at)
        ):
            spans.append((start, end))
    return _hits(RESET_LINK, spans)


# Contact identifiers --------------------------------------------------------------------------

_EMAIL = re.compile(
    r"(?<![\w.+%-])[A-Za-z0-9][A-Za-z0-9._%+-]*@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*"
    r"\.[A-Za-z]{2,}(?![\w-])"
)
_SCP_PATH = re.compile(r":[\w~/.-]")
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
# A number after "word:" is a phone when it has a phone's shape (tel:+44..., Tel:0612...). After
# "word/" only a + form is: wa.me/+44..., while a path holds plain digit runs.
_PHONE = re.compile(
    r"(?<![\w+@$\u20ac\u00a3\u00a5])(?<!\w[-.,])(?:(?<!\w/)|(?=\+))"
    r"(?:\+\s?|\()?\d[\d().\- ]*\d\)?(?!\w)(?!-\w)(?!\.\d)"
)
# A WhatsApp link or vCard field holds the number with no + or 0: the place settles it.
_WHATSAPP_NUMBER = re.compile(
    r"(?i)(?:(?<![\w.-])wa\.me/\+?|api\.whatsapp\.com/send/?\?(?:[^\s#]{0,200}&)?phone=\+?"
    r"|(?<![a-z])waid=\+?)(?P<number>\d{7,15})(?!\d)"
)
# The US form: 415-555-0132, 415.555.0132, 415 555 0132, 1-415-555-0132, one separator throughout.
_NANP = re.compile(r"[2-9]\d\d([-. ])\d{3}\1\d{4}|1([-. ])[2-9]\d\d\2\d{3}\2\d{4}")
_IPV4 = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")
_DIGIT_GROUP = re.compile(r"\d+")
_DOUBLE_SEPARATOR = re.compile(r"[ .-]{2,}")
_GROUP_OF_PHONE = re.compile(r"[^ ]+")
_DECIMAL = re.compile(r"0[.,]\d+")

# A GitHub login holds hyphens and runs to 39 characters.
_HANDLE = re.compile(r"(?<![\w@])@(?P<name>\w[\w.-]{0,37}\w)(?![\w])")
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
    "fires implements interface memberof inheritdoc typeparam category".split()
)
# Python decorators that a text names anywhere: "the `@property` on x", "use @pytest.fixture".
_DECORATOR_NAMES = frozenset(
    "property staticmethod classmethod dataclass abstractmethod cached_property contextmanager "
    "asynccontextmanager wraps lru_cache cache total_ordering singledispatch overload final "
    "runtime_checkable".split()
)
_DECORATOR_MODULES = frozenset(
    "pytest functools dataclasses abc typing contextlib app router api bp blueprint click celery "
    "mock patch unittest pydantic".split()
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
# part is mostly an organisation, and dev text holds repository links all the time.
_PROFILE_URL = re.compile(
    r"(?i)(?<![\w.-])(?:https?://)?(?:www\.|m\.|mobile\.)?"
    r"(?:t\.me|telegram\.me|instagram\.com|x\.com|twitter\.com|facebook\.com|threads\.net"
    r"|tiktok\.com|snapchat\.com/add|linkedin\.com/in)"
    r"/@?(?P<name>[A-Za-z0-9_][A-Za-z0-9_.-]{1,63})"
)
_NOT_A_PROFILE = frozenset(
    "i home search explore hashtag intent share sharer login joinchat s p reel reels stories tv "
    "settings messages notifications groups pages watch events about help privacy legal tos "
    "policies status web download developers business ads jobs company school feed in pub "
    "directory signup accounts oauth dialog plugins tag discover music video live".split()
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


def _blank(text: str, pattern: re.Pattern[str]) -> str:
    return pattern.sub(lambda m: "x" * len(m.group()), text)


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
    """`@name(`, or `@name` that ends its line, first on its line, before a def or a class.

    The design reads any `@name` that ends its line as a decorator. A chat line can be only
    "@orla", so this build asks for the def or class below it, through a stack of decorators.
    """
    if at not in first_on_line:
        return False
    if text.startswith("(", end):
        return True
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


def _whatsapp_numbers(text: str) -> list[tuple[int, int]]:
    return [match.span("number") for match in _WHATSAPP_NUMBER.finditer(text)]


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
    for match in _EMAIL.finditer(text):
        start, end = match.span()
        # user:secret@host in a URL is its userinfo (the credential step masks it). A label such
        # as "Email:" or a vCard field before the address is no URL. git@host:path is an SSH
        # remote.
        if start and text[start - 1] == ":" and "://" in _word_before(text, start):
            continue
        if _SCP_PATH.match(text, end) or match[0].rsplit(".", 1)[1].lower() in _FILE_SUFFIXES:
            continue
        # A file name such as "orla@x.example.txt" keeps its suffix out of the address.
        address = match[0]
        trimmed = _FILE_TAIL.sub("", address)
        if trimmed != address and "." in trimmed.rpartition("@")[2]:
            address, end = trimmed, start + len(trimmed)
        emails.append((start, end))
        key = address.lower()
        if key not in own.emails:
            hits.append(Hit(start, end, CONTACT_EMAIL, key))
    # An own email is kept whole: no phone or handle is cut out of it.
    in_emails = _Cover(len(text), emails)

    blanked = _blank(text, _PHONE_BARRIERS)
    phones = [
        span for match in _PHONE.finditer(blanked) for span in _phone_spans(blanked, *match.span())
    ]
    phones = _non_overlapping([*phones, *_whatsapp_numbers(text)])
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
        if _decorator(text, start, end, first_on_line) or in_emails.touches(start, end):
            continue
        handles.append((start, end, name))
    for match in _PROFILE_URL.finditer(text):
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
_SYMBOL = r"(?:(?:US|CA|AU|NZ|HK|SG)?\$|[\u20ac\u00a3\u00a5\u20b9])"
_CODES = r"(?i:USD|EUR|GBP|MAD|AED|CHF|CAD|AUD|SAR)"
_WORDS_AFTER = r"(?i:dhs?|euros?|dollars?|dirhams?|pounds?)"
_AMOUNT = re.compile(
    rf"(?<![\w.,])(?:"
    rf"{_SYMBOL}\s?{_NUMBER}{_MULTIPLIER}"
    rf"|{_CODES}\s?{_NUMBER}{_MULTIPLIER}"
    rf"|{_NUMBER}{_MULTIPLIER}\s?(?:{_CODES}|{_WORDS_AFTER}|{_SYMBOL})"
    rf"|{_NUMBER}(?:[kKmM]|bn)"
    rf")(?!\w)"
)
_PAY_WORD = re.compile(
    r"(?i)(?:(?<!\w)(?:salary|salaries|salaire|paie|pay|pays|paid|paying|wages?|compensation"
    r"|bonus(?:es)?|raises?|stipends?|retainers?|rates?|makes?"
    r"|per\s+(?:month|hour|day|year|annum)|par\s+(?:mois|an|heure|jour)"
    r"|every\s+(?:month|year|week)|an?\s+(?:month|hour|year)|monthly|hourly|yearly|annual"
    r"|income|earn(?:s|ed|ing|ings)?)"
    r"|/\s?(?:month|mo|year|yr|hour|hr|week|wk))"
    r"(?!\w)"
)
# A strong pay word and then a bare figure: "my salary is 25000", "I earn 25,000/month".
_PAY_FIGURE = re.compile(
    r"(?i)(?<!\w)(?:salary|salaire|wages?|income|stipend|retainer|compensation|earn(?:s|ed|ing)?"
    r"|(?:my|our|his|her|their|the)\s+pay)"
    r"(?P<gap>(?:\s+(?:is|was|of|about|around|roughly|approx\.?|approximately|now|currently|net"
    r"|gross)){0,4}\s*+[:=]?\s*+)"
    r"(?P<figure>\d{1,3}(?:[,. ]\d{3})+|\d{3,})(?![\w]|[.,]\d)"
)
PAY_WINDOW_WORDS = 8
_WORD_TOKEN = re.compile(r"\S+")


def pay_amounts(text: str) -> list[Hit]:
    """A currency amount within about eight words of a compensation word. The words stay."""
    pay_words = list(_PAY_WORD.finditer(text))
    if not pay_words:
        return []
    starts = [m.start() for m in _WORD_TOKEN.finditer(text)]

    def word_index(offset: int) -> int:
        return bisect_right(starts, offset) - 1

    words = [(word_index(w.start()), word_index(w.end() - 1)) for w in pay_words]
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
        ):
            spans.append(amount.span())
    for figure in _PAY_FIGURE.finditer(text):
        # "the salary 2026 budget" names a year; "my salary is 2000" gives a figure.
        if _year_like(figure["figure"]) and not figure["gap"].strip():
            continue
        spans.append(figure.span("figure"))
    return _hits(PAY_AMOUNT, spans)
