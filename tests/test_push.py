"""What the backfill still owes Nolio. No network: `push.send` is the only part that calls out."""

import json
from datetime import date
from typing import Any

import polars as pl

from trinolio import push, store


def measurement(**overrides: object) -> dict[str, Any]:
    row = {
        "date": "2020-01-01",
        "type": "hrrest",
        "value": 42.0,
        "unit": "bpm",
        "source": "garmin-archive",
    }
    return row | overrides


def store_archive(records: list[dict[str, Any]]) -> None:
    store.write(store.WELLNESS, store.wellness_frame(records))


def log_sent(entries: list[tuple[str, str]]) -> None:
    lines = [
        json.dumps({"type": metric_type, "date": day, "nolio_id": 1})
        for metric_type, day in entries
    ]
    push.PUSHED.write_text("\n".join(lines) + "\n")


def test_pending_is_everything_when_nothing_has_been_sent() -> None:
    store_archive([measurement(date="2020-01-01"), measurement(date="2020-01-02")])

    assert push.pending(["hrrest"], until=None).height == 2


def test_pending_skips_what_the_log_already_records() -> None:
    """The log is the resume point, appended after every single POST so that a run killed
    mid-backfill costs nothing but the request in flight."""
    store_archive([measurement(date="2020-01-01"), measurement(date="2020-01-02")])
    log_sent([("hrrest", "2020-01-01")])

    todo = push.pending(["hrrest"], until=None)
    assert todo.height == 1
    assert todo["date"].item() == date(2020, 1, 2)


def test_the_log_matches_on_type_as_well_as_date() -> None:
    """A day whose resting HR went up is not a day whose sleep did."""
    store_archive([measurement(type="hrrest"), measurement(type="sleep", value=25000.0)])
    log_sent([("hrrest", "2020-01-01")])

    todo = push.pending(["hrrest", "sleep"], until=None)
    assert todo["type"].to_list() == ["sleep"]


def test_pending_drops_non_positive_values() -> None:
    """Nolio rejects a non-positive `new_value` with a 400, so sending one costs a request
    out of the hourly budget and achieves nothing."""
    store_archive([measurement(date="2020-01-01", value=0.0), measurement(date="2020-01-02")])

    assert push.pending(["hrrest"], until=None).height == 1


def test_pending_stops_before_nolio_has_its_own_data() -> None:
    """`/update/metric/` upserts on (metric_id, date), so pushing past the connector's start
    would overwrite live values with archive ones."""
    store_archive([measurement(date="2020-01-01"), measurement(date="2026-08-01")])

    assert push.pending(["hrrest"], until=date(2026, 7, 19)).height == 1


def test_pending_only_offers_the_types_asked_for() -> None:
    store_archive([measurement(type="hrrest"), measurement(type="vo2max", value=54.0)])

    assert push.pending(["hrrest"], until=None)["type"].to_list() == ["hrrest"]


def test_pending_runs_oldest_first() -> None:
    """The backfill is measured in days, so an interrupted run should have closed the
    oldest end of the gap rather than a scatter through it."""
    store_archive([measurement(date=day) for day in ("2020-03-01", "2020-01-01", "2020-02-01")])

    assert push.pending(["hrrest"], until=None)["date"].is_sorted()


def test_every_default_type_is_actually_writable() -> None:
    """Only 4 of the 14 types Nolio serves have a documented numeric id; the rest cannot be
    written at all, so a default naming one would fail on the first request."""
    assert set(push.DEFAULT_TYPES) <= set(push.METRIC_IDS)


def test_sent_is_empty_rather_than_missing_before_the_first_push() -> None:
    """`pending` anti-joins against it on the very first run, when there is no log yet."""
    empty = push.sent()

    assert empty.height == 0
    assert empty["date"].dtype == pl.Date


def test_a_logged_push_reads_back_as_sent() -> None:
    """The round trip is the whole resume mechanism, and it runs after every single POST."""
    push.mark_sent("hrrest", date(2020, 1, 1), nolio_id=646275666)

    assert push.sent().row(0, named=True) == {
        "type": "hrrest",
        "date": date(2020, 1, 1),
        "nolio_id": 646275666,
    }


def test_the_log_appends_rather_than_replaces() -> None:
    """It is opened per value. Truncating would resend the whole backfill on the next run."""
    push.mark_sent("hrrest", date(2020, 1, 1), nolio_id=1)
    push.mark_sent("sleep", date(2020, 1, 1), nolio_id=2)

    assert push.sent().height == 2


def test_a_blank_line_in_the_log_is_survivable() -> None:
    """A run killed mid-write is the case the log exists to handle, so reading it back
    cannot be the thing that fails."""
    push.mark_sent("hrrest", date(2020, 1, 1), nolio_id=1)
    with push.PUSHED.open("a") as handle:
        handle.write("\n")

    assert push.sent().height == 1


def test_the_cutoff_is_none_before_the_first_sync() -> None:
    """Without it there is no known boundary, so `push-metrics` has nothing to stop at."""
    assert push.nolio_start() is None


def test_the_cutoff_is_where_nolios_own_wellness_begins() -> None:
    """`/update/metric/` upserts on (metric_id, date), so anything at or past this date
    would overwrite a live connector value with an archive one."""
    store.write(
        store.METRICS,
        store.metrics_frame(
            [
                {
                    "id": 1,
                    "date": "2026-07-19",
                    "hour": None,
                    "type": "hrrest",
                    "value": 42.0,
                    "unit": "bpm",
                    "source": "Garmin",
                },
                {
                    "id": 2,
                    "date": "2026-08-01",
                    "hour": None,
                    "type": "hrrest",
                    "value": 43.0,
                    "unit": "bpm",
                    "source": "Garmin",
                },
            ]
        ),
    )

    assert push.nolio_start() == date(2026, 7, 19)
