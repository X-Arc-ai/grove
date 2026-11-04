"""Fresh screening policy for code-admitted invented bytes only."""

import hashlib
from datetime import datetime
from importlib import resources

from grove.config import ConfigError
from grove.screening import Denylist, Message, RenameMap, ScreenedSource, Screener, ScreeningConfig


def new_demo_screener() -> Screener:
    # Never reuse the nested mutable policy objects across operations. Exact input and
    # authenticated world checks are required at each caller before selecting this policy.
    return Screener(
        ScreeningConfig(
            rename=RenameMap((("Demo Restricted Name", "Demo Participant"),)),
            denylist=Denylist(("DEMO_REJECT_THIS_SOURCE",)),
            own_identifiers=frozenset(),
            sensitive_classes=frozenset(),
        )
    )


def lumen_fixture() -> bytes:
    raw = resources.files("grove.demo").joinpath("lumen-upload.json").read_bytes()
    if len(raw) != 361 or hashlib.sha256(raw).hexdigest() != (
        "c19b1926b6710c674e701840cbb4151b66abe91aad3276df1937a041623acc20"
    ):
        raise ConfigError("the invented upload fixture is unavailable")
    return raw


def screen_story(story_key: str) -> ScreenedSource:
    """Screen only a fixed invented story; callers cannot supply replacement text."""
    from grove.demo.corpus import stories
    from grove.demo_policy import DemoDenied, require_corpus_identity

    require_corpus_identity()
    story = next((item for item in stories() if item.key == story_key), None)
    if story is None:
        raise DemoDenied()
    screened = new_demo_screener().screen_source(
        "synthetic:" + story.key,
        [
            Message(
                key=row["key"], sender=None, text=row["text"], at=datetime.fromisoformat(row["at"])
            )
            for row in story.messages()
        ],
    )
    if (
        screened.dropped_whole
        or screened.counts
        or screened.content() != story.content()
        or len(screened.content()) > 32_768
    ):
        raise DemoDenied()
    return screened
