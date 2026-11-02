"""The closed list of screening classes (D14 lines 2505 to 2520, slice row 4)."""

from grove.screening.classes import CLASSES, CONTACT_WORDS, MASKS, OPT_IN_CLASSES
from grove.store.enums import ENUMS


def test_the_class_list_is_the_slice_row_exactly():
    assert ENUMS["screening_class"] == (
        "credential",
        "payment_card",
        "one_time_code",
        "reset_link",
        "contact_email",
        "contact_phone",
        "contact_handle",
        "pay_amount",
        "denylisted",
        "renamed",
    )
    assert CLASSES is ENUMS["screening_class"]


def test_every_masked_class_has_a_mask_and_only_pay_is_opt_in():
    assert set(MASKS) | set(CONTACT_WORDS) == {
        "credential",
        "payment_card",
        "pay_amount",
        "contact_email",
        "contact_phone",
        "contact_handle",
    }
    assert OPT_IN_CLASSES == {"pay_amount"}
