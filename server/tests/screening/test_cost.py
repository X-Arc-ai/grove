"""A hostile long text costs linear time in every detector.

The run sits in a subprocess with a timeout, so no test reads a clock. On a text this long a
quadratic pattern takes minutes, and a linear one a second.
"""

import subprocess
import sys

SCRIPT = """
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
]
for text in TEXTS:
    for detector in (
        d.credentials, d.cards, d.one_time_codes, d.reset_links, d.contacts, d.pay_amounts
    ):
        detector(text)
print("done")
"""


def test_hostile_long_texts_finish_in_linear_time():
    done = subprocess.run(
        [sys.executable, "-c", SCRIPT], capture_output=True, text=True, timeout=60, check=True
    )
    assert done.stdout.strip() == "done"
