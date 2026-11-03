"""Connectors and the upload pipeline (A.4, D10, D14). docs/ingest.md is the contract.

Importing this package registers every parser of grove.ingest.parsers.
"""

from grove.ingest.connector import (
    MAX_READ_BYTES,
    MAX_RECORDS,
    ConnectorSpec,
    NotAnExport,
    ParsedChat,
    ParsedMessage,
    ReadBudget,
    Upload,
    chat_locator,
    clean_text,
    clip,
    in_time_order,
    message_key,
    one_episode,
    providers,
    register,
    silence_windows,
    spec_for,
    utc_from_epoch,
    utc_from_iso,
)

__all__ = [
    "MAX_READ_BYTES",
    "MAX_RECORDS",
    "ConnectorSpec",
    "NotAnExport",
    "ParsedChat",
    "ParsedMessage",
    "ReadBudget",
    "Upload",
    "chat_locator",
    "clean_text",
    "clip",
    "in_time_order",
    "message_key",
    "one_episode",
    "providers",
    "register",
    "silence_windows",
    "spec_for",
    "utc_from_epoch",
    "utc_from_iso",
]

# Registers the parsers. Last, so a parser module can import the names above.
from grove.ingest import parsers  # noqa: E402,F401
