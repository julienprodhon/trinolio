"""Pagination and flattening, with `get` stubbed out.

Nolio has no cursor and no `count`, so `fetch_trainings` walks the `to` bound backwards and stops
on its own judgement. Get that wrong and a sync silently returns part of the history, which
looks exactly like a quiet year of training.
"""

from typing import Any

import pytest

from trinolio import metrics, trainings


class FakeGet:
    """Serves queued responses and records the params each call was made with."""

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, path: str, **params: Any) -> Any:
        self.calls.append({"path": path} | params)
        return self.responses.pop(0) if self.responses else []


def workout(nolio_id: int, date_start: str) -> dict[str, Any]:
    return {"nolio_id": nolio_id, "date_start": date_start}


def test_a_single_page_ends_the_walk(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeGet([workout(1, "2026-01-02")])
    monkeypatch.setattr(trainings, "get", fake)

    assert len(trainings.fetch_trainings()) == 1


def test_the_to_bound_steps_back_one_day_past_the_oldest_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nolio's range is inclusive, so reusing the oldest date would refetch it forever."""
    fake = FakeGet([workout(1, "2026-01-05"), workout(2, "2026-01-03")], [workout(3, "2026-01-01")])
    monkeypatch.setattr(trainings, "get", fake)

    trainings.fetch_trainings()

    assert "to" not in fake.calls[0]
    assert fake.calls[1]["to"] == "2026-01-02"


def test_pages_accumulate_into_one_history(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeGet([workout(1, "2026-01-05")], [workout(2, "2026-01-03")], [])
    monkeypatch.setattr(trainings, "get", fake)

    assert sorted(record["nolio_id"] for record in trainings.fetch_trainings()) == [1, 2]


def test_a_workout_served_on_two_pages_is_kept_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ranges are inclusive at both ends, so consecutive pages overlap by design."""
    fake = FakeGet(
        [workout(1, "2026-01-05"), workout(2, "2026-01-03")],
        [workout(2, "2026-01-03"), workout(3, "2026-01-01")],
        [],
    )
    monkeypatch.setattr(trainings, "get", fake)

    assert len(trainings.fetch_trainings()) == 3


def test_a_page_of_nothing_new_stops_the_walk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Otherwise a range Nolio keeps answering identically loops until the rate limit."""
    fake = FakeGet(
        [workout(1, "2026-01-05")],
        [workout(1, "2026-01-05")],
        [workout(1, "2026-01-05")],
    )
    monkeypatch.setattr(trainings, "get", fake)

    assert len(trainings.fetch_trainings()) == 1
    assert len(fake.calls) == 2


def test_since_is_sent_on_every_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """It bounds the walk from below; dropping it after the first page would refetch the
    whole history behind the window."""
    fake = FakeGet([workout(1, "2026-01-05")], [workout(2, "2026-01-03")], [])
    monkeypatch.setattr(trainings, "get", fake)

    trainings.fetch_trainings("2026-01-01")

    assert all(call["from"] == "2026-01-01" for call in fake.calls)


def test_metrics_flatten_out_of_nolios_per_type_grouping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/get/user/meta/` returns every type at once, keyed by name, with the unit held once
    per bucket rather than on each point."""
    fake = FakeGet(
        {
            "hrrest": {
                "unit": "bpm",
                "data": [
                    {"id": 1, "date": "2026-01-02", "hour": None, "value": 42, "source": "Garmin"}
                ],
            },
            "vo2max": {
                "unit": "",
                "data": [
                    {"id": 2, "date": "2026-01-02", "hour": None, "value": 54, "source": "Garmin"}
                ],
            },
        }
    )
    monkeypatch.setattr(metrics, "get", fake)

    points = metrics.fetch_metrics()

    assert [point["type"] for point in points] == ["hrrest", "vo2max"]
    assert points[0]["unit"] == "bpm"
    # Nolio sends "" for a unitless type, and "" is not a unit.
    assert points[1]["unit"] is None


def test_metrics_ask_for_the_cap_so_the_warning_can_fire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`limit` applies per type and truncates silently, so `sync` can only notice the cap
    by asking for exactly it and counting what comes back."""
    fake = FakeGet({})
    monkeypatch.setattr(metrics, "get", fake)

    metrics.fetch_metrics("2026-01-01")

    assert fake.calls[0]["limit"] == metrics.LIMIT
    assert fake.calls[0]["from"] == "2026-01-01"
