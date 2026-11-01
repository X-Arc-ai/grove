"""Failure reports never carry an R1 word.

CI logs go public with the repo, and a failing test prints its assertion values and captured
output. The words come from the name lint, which builds them from parts.
"""

import re

import pytest

from grove.lints.no_names import WORDS

_WORD = re.compile("|".join(WORDS), re.IGNORECASE)


def redact(text: str) -> str:
    return _WORD.sub(lambda match: "#" * len(match.group()), text)


def _clean(report) -> None:
    if report.longrepr is not None and _WORD.search(str(report.longrepr)):
        report.longrepr = redact(str(report.longrepr))
    report.sections = [(title, redact(content)) for title, content in report.sections]


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    _clean(outcome.get_result())


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    outcome = yield
    _clean(outcome.get_result())
