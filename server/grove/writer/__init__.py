"""The writer rule: only this package opens write transactions on the store.

The public API is in README.md beside this file.
"""

from grove.writer.api import apply_batch
from grove.writer.caller import Caller
from grove.writer.errors import (
    HTTP_STATUS,
    BatchDeclined,
    DeclinedReceipt,
    InvalidRecord,
    ReceiptUnavailable,
    Refusal,
    WriteFailed,
    WriteOutcomeUnknown,
)
from grove.writer.receipts import Receipt, ReceiptLine, render_receipt, short_id
from grove.writer.sources import record_connector, record_episode, record_source

__all__ = [
    "HTTP_STATUS",
    "BatchDeclined",
    "Caller",
    "DeclinedReceipt",
    "InvalidRecord",
    "Receipt",
    "ReceiptLine",
    "ReceiptUnavailable",
    "Refusal",
    "WriteFailed",
    "WriteOutcomeUnknown",
    "apply_batch",
    "record_connector",
    "record_episode",
    "record_source",
    "render_receipt",
    "short_id",
]
