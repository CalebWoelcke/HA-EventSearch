from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from scout.dates import in_window, parse_event_time
from scout.geo import haversine_km
from scout.textutil import event_key, extract_json

TZ = ZoneInfo("America/Edmonton")
NOW = datetime(2026, 10, 1, 16, 0, tzinfo=TZ)


@pytest.mark.parametrize(
    "text",
    [
        '{"events": []}',
        '```json\n{"events": []}\n```',
        'Here you go:\n{"events": []}\nHope that helps!',
        'Sure! [note] then {"events": []}',
    ],
)
def test_extract_json_variants(text):
    assert extract_json(text) == {"events": []}


def test_extract_json_failures():
    with pytest.raises(ValueError):
        extract_json(None)
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_parse_date_only_and_naive_datetime():
    t = parse_event_time("2026-10-04", "", TZ)
    assert t.all_day and t.start == date(2026, 10, 4)
    t = parse_event_time("2026-10-04 19:30", "", TZ)
    assert not t.all_day and t.start.tzinfo is not None and t.start.hour == 19


def test_parse_mixed_end_and_bad_end():
    t = parse_event_time("2026-10-04", "2026-10-05T22:00", TZ)
    assert t.end == date(2026, 10, 5)
    t = parse_event_time("2026-10-04T19:00", "2026-10-01", TZ)
    assert t.end is None  # end before start is discarded
    assert parse_event_time("next Saturday", "", TZ) is None


def test_window():
    assert in_window(parse_event_time("2026-10-03", "", TZ), NOW, 7)
    assert not in_window(parse_event_time("2026-09-30", "", TZ), NOW, 7)
    assert not in_window(parse_event_time("2026-10-20", "", TZ), NOW, 7)
    # multi-day festival that started yesterday is still on
    assert in_window(parse_event_time("2026-09-30", "2026-10-02", TZ), NOW, 7)
    # timed event earlier today that has finished
    assert not in_window(parse_event_time("2026-10-01T09:00", "2026-10-01T11:00", TZ), NOW, 7)


def test_event_key_ignores_noise():
    assert event_key("The Trews — Live at the Starlite!", "2026-10-04T20:00") == event_key(
        "Trews live at Starlite", "2026-10-04"
    )
    assert event_key("Trivia Night", "2026-10-04") != event_key("Trivia Night", "2026-10-11")


def test_haversine():
    # Edmonton to Calgary is roughly 280 km in a straight line
    assert 270 < haversine_km(53.5461, -113.4938, 51.0447, -114.0719) < 290
