"""A hostile long text costs linear time in every detector and in the whole screener.

Each row has a budget of CPU time, which the kernel enforces in a subprocess, so load on the machine
does not count and no test reads a clock. On a text this long a linear row costs about a second of
CPU, and a quadratic one costs minutes.
"""

import subprocess
import sys

import pytest

# Seconds of CPU for one row, about five times the slowest linear row on a slow runner.
ROW_BUDGET = 20
# Only a guard against a hang: the budget above is what a slow row meets.
HANG_LIMIT = 900

TEXTS = """
N = 100_000
TEXTS = [
    "otp" + " " * N,
    "code is" + " " * N,
    "password is" + " " * N,
    "password -" + " " * N,
    "my salary is" + " " * N,
    "your code" + " " * N,
    "Kestrel code:" + " " * N,
    "curl " * (N // 5),
    "api.whatsapp.com/send?" * (N // 22),
    "A_KEY=abcd1234 " * (N // 15),
    "a:b@c.example " * (N // 14),
    "1." * (N // 2),
    "a_" * (N // 2),
    "@a-" * (N // 3),
    "@a\\n" * (N // 3),
    "a." * (N // 2),
    "aB" * (N // 2),
    "www.a.example/x " * (N // 16),
    # Each of these was quadratic. Each is long enough to spend its budget if it comes back.
    "login=" * 50_000,
    "creds=" * 50_000,
    "pay " + "111 " * 25_000,
    "a" + "\\u0301\\u0323" * 100_000,
]
"""
# The forms the read view writes as space: a long run must cost no more than a short one.
READ_VIEW_TEXTS = """
N = 100_000
TEXTS = [
    "_" * N,
    "x" + "_" * 200_000 + "y",
    "x_" + " _" * (N // 2),
    "\\\\" * N,
    "\\\\" * N + "n",
    "\\\\" * N + "/",
    "\\\\/" * (N // 2),
    "\\\\n" * (N // 2),
    '\\\\"' * (N // 2),
]
"""
# The step 2 forms: each reads a few words on from its start, and a long word costs once.
STEP_TWO_TEXTS = """
N = 100_000
TEXTS = [
    "code " + "a" * N,
    "code " * (N // 5),
    "code" + " a" * (N // 2),
    "use " * (N // 4),
    "use " + "a-" * (N // 2),
    "to log in " * (N // 10),
    "to log in" + " " * N,
    "to log in use " * (N // 14),
    "expires in " * (N // 11),
    "do not share " * (N // 13),
    "login: " * (N // 7),
    "1234 - " * (N // 7),
    "pin" + " " * N,
    "pin " * (N // 4),
    "password " + "a" * N,
    "password " * (N // 9),
    "magic link " * (N // 11) + "https://a.example/x",
    "sign in to x " * (N // 13) + "https://a.example/x",
    "magic link https://a.example/x " * (N // 31),
    "https://a.example/login/" + "a-" * (N // 2),
    "https://a.example/login?code=1" * (N // 30),
]
"""
# The contact forms: a run of marks, a list of numbers and a long handle each cost once.
CONTACT_TEXTS = """
N = 100_000
TEXTS = [
    "-" * N,
    "-a" * (N // 2),
    "a%40" * (N // 4),
    "0612345678/" * (N // 11),
    "0612345678-" * (N // 11),
    "+33612345678," * (N // 13),
    "@" + "a-" * (N // 2),
    "@a@" * (N // 3),
    "ig: " * (N // 4),
    "a,0" * (N // 3),
    "tel" * (N // 3),
    "facebook.com/profile.php?" * (N // 25),
    "tg://resolve?" * (N // 13),
]
"""
# The pay forms: a long table of numbers, a long chain of range ends and a long gap each cost once.
PAY_TEXTS = """
N = 100_000
TEXTS = [
    "111 " * (N // 4),
    "25000-" * (N // 6),
    "25k-" * (N // 4),
    "25k to " * (N // 7),
    "salary 111 " * (N // 11),
    "pay " + "Orla " * (N // 5),
    "Pay " * (N // 4),
    "rate: 300k " * (N // 11),
    "25000" + " " * N + "a month",
    "take" + " " * N + "home",
    "pay" + " " * N + "x",
    "1k" + " " * N + "-",
    "1 - " * (N // 4),
]
"""
# The credential forms that read on past the first token: each stops within a bound.
CREDENTIAL_TEXTS = """
N = 100_000
TEXTS = [
    "password: a " * (N // 12),
    "password=a1b2 " * (N // 14),
    "token=," * (N // 7),
    "password for " * (N // 13),
    "password " + "a " * (N // 2),
    "passwords are a1b2 and " * (N // 23),
    "user a pass " * (N // 12),
    "mysql -x " * (N // 9),
    "redis-cli " + "a " * (N // 2),
    "--token " * (N // 8),
    "name: token " * (N // 12),
    "<token>" * (N // 7),
    '"password", ' * (N // 12),
    "cookie: " * (N // 8),
    "cookie: " + "a" * N,
    "cookie: " + "a=1;" * (N // 4),
    "-b a=" * (N // 5),
    "cache" * (N // 5) + "tokens",
    "1 tokens " * (N // 9),
    "a:b1cd@e.example:" * (N // 17),
    "orla@x.example:a1 " * (N // 18),
    "authorization: " * (N // 15),
    "$" * N,
    ("x" * 19 + "/") * (N // 20),
    "token\\n" + (("Kq8vN2xP7mZ4tR9wLm3s" + "\\n") * (N // 21)),
]
"""
RUN = """
import resource

from grove.screening import Screener, ScreeningConfig
from grove.screening import detectors as d

screen = Screener(ScreeningConfig(sensitive_classes=frozenset({"pay_amount"})))
hard = resource.getrlimit(resource.RLIMIT_CPU)[1]
for row, text in enumerate(TEXTS):
    used = resource.getrusage(resource.RUSAGE_SELF)
    # The kernel stops this process when a row spends its budget, so a slow row cannot run on.
    resource.setrlimit(
        resource.RLIMIT_CPU, (int(used.ru_utime + used.ru_stime) + ROW_BUDGET, hard)
    )
    print(row, flush=True)
    # The weak detectors share their passes with these two, so the same reads prove them.
    for detector in (
        d.credentials, d.cards, d.one_time_codes, d.reset_links, d.contacts, d.pay_amounts
    ):
        detector(text)
    screen.screen_text(text)
print("done")
"""


@pytest.mark.parametrize(
    "texts",
    [TEXTS, READ_VIEW_TEXTS, STEP_TWO_TEXTS, CONTACT_TEXTS, PAY_TEXTS, CREDENTIAL_TEXTS],
    ids=["all-steps", "read-view", "step-two", "contacts", "pay", "credentials"],
)
def test_hostile_long_texts_finish_in_linear_time(texts):
    script = f"ROW_BUDGET = {ROW_BUDGET}\n" + texts + RUN
    done = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=HANG_LIMIT
    )
    rows = done.stdout.split()
    assert done.returncode == 0, f"row {rows[-1:]} spent its budget or failed: {done.stderr[-300:]}"
    assert rows[-1] == "done"
