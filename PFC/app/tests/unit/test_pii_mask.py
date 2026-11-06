"""REQ-OUT-03: email masking keeps 2 local-part characters and masks the rest."""

import pytest

from pfc.services.pii import mask_email


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
