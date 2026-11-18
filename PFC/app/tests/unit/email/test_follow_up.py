import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from pfc.services.email.follow_up import (
    FollowUpSettings,
    build_ics,
    next_business_day,
    validate_settings,
)


@pytest.mark.parametrize(
    ("sent", "holidays", "expected"),
    [
        ("2026-01-02", [], "2026-01-05"),
        ("2026-01-03", [], "2026-01-05"),
        ("2026-01-04", [], "2026-01-05"),
        ("2026-01-01", [], "2026-01-02"),
        ("2026-01-02", ["2026-01-05"], "2026-01-06"),
        ("2026-01-02", ["2026-01-05", "2026-01-06"], "2026-01-07"),
    ],
)
def test_business_day(sent: str, holidays: list[str], expected: str) -> None:
    due = next_business_day(
        datetime.fromisoformat(sent).replace(tzinfo=UTC), UTC, holidays=holidays
    )
    assert due.isoformat() == expected + "T09:00:00+00:00"


def test_local_date_and_dst() -> None:
    london = next_business_day(
        datetime(2026, 10, 23, 15, tzinfo=ZoneInfo("Europe/London")), ZoneInfo("Europe/London")
    )
    assert london.isoformat() == "2026-10-26T09:00:00+00:00"
    zone = ZoneInfo("America/New_York")
    due = next_business_day(datetime(2026, 3, 7, 0, 30, tzinfo=UTC), zone)
    assert due.isoformat() == "2026-03-09T09:00:00-04:00"
    assert next_business_day(datetime(2026, 1, 6, 0, 1, tzinfo=UTC), zone).day == 6
    with pytest.raises(ValueError):
        next_business_day(datetime(2026, 1, 1))


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="OS timezone switching requires tzset")
def test_default_local_timezone_resolves_future_dst(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        monkeypatch.setenv("TZ", "Europe/London")
        time.tzset()
        result = next_business_day(datetime(2026, 10, 23, 15, tzinfo=ZoneInfo("Europe/London")))
        assert result.isoformat() == "2026-10-26T09:00:00+00:00"
    finally:
        monkeypatch.undo()
        time.tzset()


@pytest.mark.parametrize(
    "values",
    [
        {"email.follow_up.enabled": 1},
        {"email.follow_up.enabled": "false"},
        {"email.follow_up.enabled": None},
        {"email.follow_up.time": "24:00"},
        {"email.follow_up.time": "9:00"},
        {"email.follow_up.holidays": ["2026-02-30"]},
        {"email.follow_up.holidays": "2026-01-01"},
        {"email.follow_up.holidays": ("2026-01-01",)},
        {"email.follow_up.holidays": [datetime(2026, 1, 1)]},
        {"email.follow_up.holidays": [20260101]},
        {"email.follow_up.holidays": ["20260101"]},
    ],
)
def test_invalid_settings(values: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        validate_settings(values)


def test_settings_model_defaults_partial_and_leap_dates() -> None:
    assert FollowUpSettings.model_validate({}).model_dump(by_alias=True) == {
        "email.follow_up.enabled": True,
        "email.follow_up.time": "09:00",
        "email.follow_up.holidays": [],
    }
    for values in (
        {},
        {"email.follow_up.holidays": []},
        {"unrelated.setting": False},
        {
            "email.follow_up.enabled": False,
            "email.follow_up.time": "23:59",
            "email.follow_up.holidays": ["2028-02-29"],
        },
    ):
        validate_settings(values)


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"email.follow_up.enabled": "true"}, "Follow-up enabled must be a boolean"),
        ({"email.follow_up.time": "25:00"}, "Follow-up time must be HH:MM"),
        ({"email.follow_up.holidays": ["2026-1-1"]}, "Follow-up holidays must be ISO dates"),
    ],
)
def test_settings_validation_messages(values: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        validate_settings(values)


def test_ics_escaping_and_folding() -> None:
    content = build_ics(
        "key",
        "é" * 80 + ",;\\\nnext",
        "",
        datetime(2026, 1, 2, 9, tzinfo=UTC),
        datetime(2026, 1, 1, 12, tzinfo=UTC),
    )
    assert all(len(line) <= 75 for line in content.split(b"\r\n"))
    unfolded = content.decode().replace("\r\n ", "")
    assert r"\,\;\\\nnext" in unfolded
    assert "DTSTART:20260102T090000Z\r\nDTEND:20260102T091500Z" in unfolded
    assert "TRIGGER:PT0M" in unfolded
    assert "DTSTAMP:20260101T120000Z" in unfolded
    assert "TRANSP:TRANSPARENT" in unfolded
    assert content.endswith(b"END:VCALENDAR\r\n")
