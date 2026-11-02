"""A hostile long text costs linear time in every detector and in the whole screener.

The run sits in a subprocess with a timeout, so no test reads a clock. On a text this long a
quadratic pattern takes minutes, and a linear one a second.
"""

import subprocess
import sys

SCRIPT = """
from grove.screening import Screener, ScreeningConfig
from grove.screening import detectors as d

N = 200_000
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
    # Each of these was quadratic, and each is long enough to pass the timeout if it comes back.
    "login=" * 100_000,
    "creds=" * 100_000,
    "pay " + "111 " * 25_000,
    "a" + "\\u0301\\u0323" * 500_000,
    # The forms the read view writes as space: a long run must cost no more than a short one.
    "_" * N,
    "x" + "_" * 1_000_000 + "y",
    "x_" + " _" * (N // 2),
    "\\\\" * N,
    "\\\\" * N + "n",
    "\\\\" * N + "/",
    "\\\\/" * (N // 2),
    "\\\\n" * (N // 2),
    '\\\\"' * (N // 2),
]
screen = Screener(ScreeningConfig(sensitive_classes=frozenset({"pay_amount"})))
for text in TEXTS:
    for detector in (
        d.credentials, d.cards, d.one_time_codes, d.reset_links, d.contacts, d.pay_amounts
    ):
        detector(text)
    screen.screen_text(text)
print("done")
"""


def test_hostile_long_texts_finish_in_linear_time():
    done = subprocess.run(
        [sys.executable, "-c", SCRIPT], capture_output=True, text=True, timeout=60, check=True
    )
    assert done.stdout.strip() == "done"
