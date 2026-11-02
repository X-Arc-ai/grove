"""The screening classes: the codes a receipt counts, and what replaces each masked span."""

from grove.store.enums import ENUMS

CLASSES: tuple[str, ...] = ENUMS["screening_class"]

CREDENTIAL = "credential"
PAYMENT_CARD = "payment_card"
ONE_TIME_CODE = "one_time_code"
RESET_LINK = "reset_link"
CONTACT_EMAIL = "contact_email"
CONTACT_PHONE = "contact_phone"
CONTACT_HANDLE = "contact_handle"
PAY_AMOUNT = "pay_amount"
DENYLISTED = "denylisted"
RENAMED = "renamed"

# The classes the person may opt into. P0 has one: "never store my pay".
OPT_IN_CLASSES = frozenset({PAY_AMOUNT})

MASKS = {
    CREDENTIAL: "[credential]",
    PAYMENT_CARD: "[card]",
    PAY_AMOUNT: "[amount]",
}
# A contact identifier becomes "[email 3]": the number tells two people apart without the value.
CONTACT_WORDS = {CONTACT_EMAIL: "email", CONTACT_PHONE: "phone", CONTACT_HANDLE: "handle"}
KEPT_OUT = "[kept out]"

_NAMED = {CREDENTIAL, PAYMENT_CARD, ONE_TIME_CODE, RESET_LINK, PAY_AMOUNT, DENYLISTED, RENAMED}
if _NAMED | set(CONTACT_WORDS) != set(CLASSES):
    raise RuntimeError("grove.screening.classes and the screening_class enum disagree")
