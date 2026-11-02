"""The code detectors of D14 screening. None calls a model.

Each detector takes a text and returns Hit spans in its offsets. The screener merges the spans of
every step and applies them. A Hit's repr shows its offsets and class, never the value.

False positives are kept low on Grove's own dev text: git shas, UUIDs, dates, times, versions,
decorators, npm scopes and order numbers stay untouched. Where code cannot tell, the candidate is
treated as sensitive (spec line 2552): it is masked or its message is dropped.
"""

import math
import re
import unicodedata
from bisect import bisect_left, bisect_right
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from urllib.parse import urlsplit

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
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
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
    r")"
)
_BEARER = re.compile(r"(?i)(?<![A-Za-z])bearer\s+(?P<value>[A-Za-z0-9._~+/=-]{16,})")
# scheme://user:secret@host keeps the user and the host.
_USERINFO = re.compile(rf"(?i){_SCHEME}[^\s:/@]+:(?P<value>[^\s/@]+)@")
_QUERY_SECRET = re.compile(
    r"(?i)[?&](?:api[_-]?key|apikey|key|token|access_token|auth|secret|password|pwd|sig"
    r"|signature)"
    r"=(?P<value>[^&#\s]+)"
)
# A name that says secret: MODEL_API_KEY, GH_TOKEN, NEO4J_AUTH; api_key, client_secret, password;
# apiKey, clientSecret, accessToken.
_SECRET_NAME = (
    # The repeats are bounded, so a long dotted chain costs linear time, not quadratic.
    r"(?:[A-Z0-9]+_){1,6}(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASS|PWD|AUTH|CREDENTIALS?)"
    r"|(?:TOKEN|SECRET|PASSWORD|PASSWD)"
    r"|(?:[A-Za-z0-9]+[_.-]){0,6}(?i:api[_-]?key|apikey|secret|secret[_-]?key|token|password|passwd"
    r"|pwd|passphrase|private[_-]?key|access[_-]?key|credentials?)"
    r"|(?:api|secret|private|access|client|auth|signing|encryption)Key"
    r"|[a-z][a-z0-9]*(?:Secret|Token|Password)"
)
_ASSIGNMENT = re.compile(
    rf"(?<![A-Za-z0-9_])[\"']?(?:{_SECRET_NAME})[\"']?\s*[:=]\s*"
    r"(?:\"(?P<dq>[^\"\n]+)\"|'(?P<sq>[^'\n]+)'|`(?P<bq>[^`\n]+)`"
    r"|(?P<bare>[^\s\"'`,;()\[\]{}<>]+))"
)
_NOT_A_VALUE = frozenset({"none", "null", "nil", "true", "false", "undefined", "required"})
_CODE_REFERENCE = re.compile(r"[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)+")
# "my password is X", "the pin is 4821". A PIN is digits, so "the pin is 2026.10.1" is a version.
_PHRASE = re.compile(
    r"(?i)(?<![A-Za-z])(?:(?P<word>password|passphrase|passwd|api\s+key|secret\s+key)"
    r"|(?P<pin>pin(?:\s+code)?))(?:\s+(?:is|was)\s+|\s*[:=]\s*)[\"'\u201c]?"
    r"(?P<value>[^\s\"'\u201d]+)"
)
_PHRASE_STOP = frozenset(
    "wrong correct incorrect invalid expired required missing empty reset changed saved stored "
    "the not now still very here there below above attached same different fine okay weak "
    "strong long short secure safe encrypted hashed visible shown hidden case-sensitive being "
    "never always also just only what that this your our their written listed included known "
    "unknown shared sent default rotated revoked too".split()
)
_CONTEXT = re.compile(
    r"(?i)(?<![a-z])(?:keys?|tokens?|secrets?|passwords?|credentials?|api)(?![a-z])"
)
# Dev text puts git shas, hashes and ids next to these words all the time, so a hex token or a
# UUID needs the context word just before it: "api key 3f2a...", "the token is 0193...".
_HEX_OR_UUID = re.compile(
    r"[0-9a-f]+|[0-9A-F]+|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
)
_CONTEXT_JUST_BEFORE = re.compile(
    r"(?i)(?<![a-z])(?:keys?|tokens?|secrets?|passwords?|credentials?|api)(?![a-z])\W*"
    r"(?:\w+\W+){0,2}$"
)
_JUST_BEFORE_CHARS = 120
# "=" only as base64 padding, so NAME=value splits into the name and the value.
_TOKEN = re.compile(r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/_-]{20,}={0,2}(?![A-Za-z0-9+/=_-])")
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


def _bare_value_is_secret(text: str, match: re.Match[str]) -> bool:
    value = match.group("bare")
    after = text[match.end() : match.end() + 1]
    if len(value) < 4 or value.lower() in _NOT_A_VALUE:
        return False
    # $VAR and ${VAR} are references, a call or a subscript is code, settings.api_key is a name.
    if value.startswith("$") or after in ("(", "[") or _CODE_REFERENCE.fullmatch(value):
        return False
    return True


def credentials(text: str) -> list[Hit]:
    spans = [m.span() for m in _PEM.finditer(text)]
    spans += [m.span() for m in _PREFIXED.finditer(text)]
    spans += [
        m.span("value") for m in _BEARER.finditer(text) if any(c.isdigit() for c in m["value"])
    ]
    spans += [m.span("value") for m in _USERINFO.finditer(text)]
    spans += [m.span("value") for m in _QUERY_SECRET.finditer(text)]
    for match in _ASSIGNMENT.finditer(text):
        for group in ("dq", "sq", "bq"):
            if match.group(group) is not None:
                spans.append(match.span(group))
        if match.group("bare") is not None and _bare_value_is_secret(text, match):
            spans.append(match.span("bare"))
    for match in _PHRASE.finditer(text):
        value = match["value"].rstrip(".,;:!?)")
        if match["pin"] is not None:
            if not re.fullmatch(r"\d{4,12}", value):
                continue
        elif len(value) < 4 or value.lower() in _PHRASE_STOP:
            continue
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
                if not _random_looking(match.group()) or in_urls.holds(start, end):
                    continue
                hex_or_uuid = _HEX_OR_UUID.fullmatch(match.group().rstrip("="))
                # Three words fit in the window, so a long line costs no more per token.
                window = max(0, match.start() - _JUST_BEFORE_CHARS)
                if hex_or_uuid and not _CONTEXT_JUST_BEFORE.search(line, window, match.start()):
                    continue
                spans.append((start, end))
        offset += len(line) + 1
    return spans


# Payment cards --------------------------------------------------------------------------------

# Digit groups split by single spaces or dashes. Not after "+" (a phone), not inside a longer
# token such as a UUID ("-7abc") or a version ("1.2"). A file suffix (".txt") may follow.
_DIGIT_RUN = re.compile(r"(?<![\w+])(?<!\w[-.])\d+(?:[ -]\d+)*(?!\w)(?!-\w)(?!\.\d)")
_GROUP = re.compile(r"\d+")
_CARD_GROUPED = ([4, 6, 5], [4, 6, 4])
_MAX_CARD_GROUPS = 5


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
                lengths = [e - s for s, e in part]
                if not (13 <= sum(lengths) <= 19 and _card_shape(lengths)):
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
_OTC_PHRASE = re.compile(
    r"(?i)(?<!\w)(?:"
    r"(?:verification|security|confirmation|access|login|log-in|log\s+in|sign-in|sign\s+in"
    r"|signin|authentication|auth|2fa|mfa|two-factor|2-step)\s+code"
    r"|one[-\s]time\s+(?:code|passcode|password|pin)"
    r"|otp|passcode|your\s+code\s+is|your\s+code:|enter\s+(?:the|this)\s+code"
    r")(?!\w)"
)
# "code is" alone is weak: "the code is 1200 lines". It counts only when the code ends the clause.
_CODE_IS = re.compile(
    rf"(?i)(?<!\w)code\s+is\s*:?\s*(?P<code>{_OTC_CODE})(?!\w)(?=\s*(?:$|[.,;!?)\n]))"
)
_OTC_SENDER = re.compile(
    r"(?i)(?<![a-z])(?:no-?reply|do-?not-?reply|verify|verification|security|auth|accounts?"
    r"|otp)(?![a-z])"
)
_YEAR = re.compile(r"(?:19|20)\d\d")
_WINDOW = 40


def _year_like(code: str) -> bool:
    return _YEAR.fullmatch(code) is not None


def one_time_codes(text: str, sender: str | None = None) -> list[Hit]:
    spans = [m.span("code") for m in _CODE_IS.finditer(text)]
    codes = list(_CODE_TOKEN.finditer(text))
    code_starts = [code.start() for code in codes]
    for phrase in _OTC_PHRASE.finditer(text):
        # Only the codes that can sit inside the window; a code is at most 13 characters.
        low = bisect_left(code_starts, phrase.start() - _WINDOW - 13)
        high = bisect_right(code_starts, phrase.end() + _WINDOW)
        for code in codes[low:high]:
            before = phrase.start() - code.end()
            after = code.start() - phrase.end()
            if not (0 <= before <= _WINDOW or 0 <= after <= _WINDOW):
                continue
            # A year near a phrase ("the verification code ships in 2026") is not a code,
            # unless it is said as the code: "code: 2026".
            gap = text[phrase.end() : code.start()] if after >= 0 else "x"
            if _year_like(code.group()) and not re.fullmatch(r"\s*(?:is|:|-|=)?\s*", gap):
                continue
            spans.append(code.span())
    if sender and _OTC_SENDER.search(sender):
        spans += [c.span() for c in codes if not _year_like(c.group())]
    return _hits(ONE_TIME_CODE, spans)


# Whole components of a path or query, after camelCase is split: "verifier" and "authors" are
# not link words, "resetPassword" is.
_LINK_WORD = re.compile(
    r"(?<![a-z])(?:reset|resetpassword|password|passwordreset|passwordless|passwd|magic"
    r"|magiclink|verify|verification|confirm|confirmation|signin|sign-in|sign_in|login|log-in"
    r"|auth|oauth|token|otp|invite|invitation)(?![a-z])"
)
_CAMEL = re.compile(r"([a-z])([A-Z])")
_LINK_PHRASE = re.compile(
    r"(?i)reset\s+your\s+password|password\s+reset\s+link|magic\s+link"
    r"|sign\s+in\s+with\s+this\s+link|(?:log|sign)[\s-]?in\s+link|verify\s+your\s+email"
)
_LINK_PIECE = re.compile(r"[^/?&=#;]+")


def _token_like(piece: str) -> bool:
    return (
        len(piece) >= 16
        and re.fullmatch(r"[A-Za-z0-9_~.%-]+", piece) is not None
        and any(ch.isdigit() for ch in piece)
        and any(ch.isalpha() for ch in piece)
    )


def reset_links(text: str) -> list[Hit]:
    found = list(urls(text))
    if found and _LINK_PHRASE.search(text):
        return _hits(RESET_LINK, found)
    spans = []
    for start, end in found:
        try:
            parts = urlsplit(text[start:end])
        except ValueError:
            # A URL the parser refuses cannot be cleared by code, so it is treated as a link.
            spans.append((start, end))
            continue
        rest = _CAMEL.sub(r"\1-\2", f"{parts.path}?{parts.query}")
        word = _LINK_WORD.search(rest.lower())
        # The token follows the word: "/reset-password?token=X", "/magic/X". A GitHub blob link
        # has its sha before the file name "verify_x.py", so it stays.
        if word and any(
            _token_like(piece.group()) for piece in _LINK_PIECE.finditer(rest, word.start())
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
_PHONE = re.compile(
    r"(?<![\w+@$\u20ac\u00a3\u00a5])(?<!\w[-./:,])"
    r"(?:\+\s?|\()?\d[\d().\- ]*\d\)?(?!\w)(?!-\w)(?!\.\d)"
)
_DOUBLE_SEPARATOR = re.compile(r"[ .-]{2,}")
_GROUP_OF_PHONE = re.compile(r"[^ ]+")
_DECIMAL = re.compile(r"0[.,]\d+")

_HANDLE = re.compile(r"(?<![\w@])@(?P<name>\w[\w.]{0,28}\w)(?![\w])")
# Words after "@" in code and chat tools that never name a person.
_NOT_HANDLES = frozenset(
    "everyone here channel media import keyframes supports charset param returns return type "
    "typedef deprecated see example throws override tailwind apply".split()
)
_DECORATED = re.compile(r"\s*(?:@|def\s|async\s+def\s|class\s)")
_LINE_INDENT = re.compile(r"^[ \t]*", re.MULTILINE)


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
    if _DOUBLE_SEPARATOR.search(candidate) or not candidate.startswith(("+", "0", "(")):
        return False
    # "0.5 0.25 0.125" is a list of decimals, not a number.
    if any(_DECIMAL.fullmatch(group) for group in candidate.split(" ")):
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
    """`@name(` or `@name` at the end of a line before a def or class, first on its line.

    The design reads any `@name` that ends its line as a decorator. A chat line can be only
    "@orla", so this build asks for the def or class on the next line too.
    """
    if at not in first_on_line:
        return False
    following = text[end : end + 1]
    if following == "(":
        return True
    line_end = text.find("\n", end)
    if line_end == -1 or text[end:line_end].strip():
        return False
    return _DECORATED.match(text, line_end + 1) is not None


def contacts(text: str, own: OwnIdentifiers | None = None) -> list[Hit]:
    own = own or OwnIdentifiers()
    hits: list[Hit] = []
    emails = []
    for match in _EMAIL.finditer(text):
        start, end = match.span()
        # user:secret@host is a URL's userinfo (the credential step masks it), and
        # git@host:path is an SSH remote.
        if (
            start
            and text[start - 1] == ":"
            and text[max(0, start - 7) : start].lower() != "mailto:"
        ):
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
    for start, end in phones:
        if in_emails.touches(start, end):
            continue
        key = phone_key(text[start:end])
        if key not in own.phones:
            hits.append(Hit(start, end, CONTACT_PHONE, key))

    first_on_line = {m.end() for m in _LINE_INDENT.finditer(text)} if "@" in text else set()
    for match in _HANDLE.finditer(text):
        start, end = match.span()
        name = match["name"]
        if text.startswith("/", end) or name.lower() in _NOT_HANDLES:
            continue
        if _decorator(text, start, end, first_on_line) or in_emails.touches(start, end):
            continue
        if name.lower() not in own.handles:
            hits.append(Hit(start, end, CONTACT_HANDLE, name.lower()))
    return hits


# Pay amounts ----------------------------------------------------------------------------------

_NUMBER = r"(?:\d{1,3}(?:[,. ]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d+)?)"
_MULTIPLIER = r"(?:\s?(?:k|K|m|M|bn)(?![A-Za-z]))?"
_SYMBOL = "[$\u20ac\u00a3\u00a5\u20b9]"
_CODES = r"(?:USD|EUR|GBP|MAD|AED|CHF|CAD|AUD|SAR)"
_WORDS_AFTER = r"(?:dhs?|DHS?|Dhs?|euros?|dollars?|dirhams?|pounds?)"
_AMOUNT = re.compile(
    rf"(?<![\w.,])(?:"
    rf"{_SYMBOL}\s?{_NUMBER}{_MULTIPLIER}"
    rf"|{_CODES}\s?{_NUMBER}{_MULTIPLIER}"
    rf"|{_NUMBER}{_MULTIPLIER}\s?(?:{_CODES}|{_WORDS_AFTER}|{_SYMBOL})"
    rf"|{_NUMBER}[kK]"
    rf")(?!\w)"
)
_PAY_WORD = re.compile(
    r"(?i)(?<!\w)(?:salary|salaries|pay|pays|paid|paying|wages?|compensation|bonus(?:es)?"
    r"|raises?|stipends?|retainers?|rates?|per\s+(?:month|hour|day|year|annum)"
    r"|an?\s+(?:month|hour|year)|monthly|hourly|yearly|annual|income|earn(?:s|ed|ing|ings)?)"
    r"(?!\w)"
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
    return _hits(PAY_AMOUNT, spans)
