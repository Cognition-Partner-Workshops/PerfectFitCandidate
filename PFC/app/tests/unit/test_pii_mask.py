"""REQ-OUT-03: email masking keeps 2 local-part characters and masks the rest."""

import pytest

from pfc.services.analytics.dashboard import _legacy_mask
from pfc.services.pii import mask_email, mask_emails_in, mask_emails_in_json


@pytest.mark.parametrize(
    "email,expected",
    [
        ("AlexBorder@capgemini.com", "Al********@capgemini.com"),
        ("jonathan.smithers@example.com", "jo***************@example.com"),
        ("ab@x.com", "**@x.com"),
        ("a@x.com", "*@x.com"),
        ("abcdefghijk", "ab*********"),
    ],
)
def test_mask_email_keeps_two_and_masks_the_rest(email: str, expected: str) -> None:
    assert mask_email(email) == expected
    assert mask_email(expected) == expected


def test_long_address_shows_no_local_part_character_after_the_second() -> None:
    local = "christopher.alexander-montgomery.consulting"
    masked = mask_email(f"{local}@example.com")
    assert masked is not None
    shown, _, domain = masked.partition("@")
    assert domain == "example.com"
    assert shown == "ch" + "*" * (len(local) - 2)
    assert set(shown[2:]) == {"*"}


def test_stored_text_masked_under_the_old_rule_is_remasked() -> None:
    text = "Contact Al******er@capgemini.com or jo******.smithers@example.com; AB@x.com"
    assert mask_emails_in(text) == (
        "Contact Al********@capgemini.com or jo***************@example.com; **@x.com"
    )
    assert mask_emails_in(mask_emails_in(text)) == mask_emails_in(text)
    assert mask_emails_in_json({"a": ["Al******er@capgemini.com", 3]}) == {
        "a": ["Al********@capgemini.com", 3]
    }


def test_dashboard_legacy_mask_is_the_old_2_plus_6_rule() -> None:
    assert _legacy_mask("AlexBorder@capgemini.com") == "Al******er@capgemini.com"
    assert _legacy_mask("jonathan.smithers@example.com") == "jo******.smithers@example.com"
    assert _legacy_mask("Alex@x.com") == "Al**@x.com"
    assert _legacy_mask("ab@x.com") == "**@x.com"
