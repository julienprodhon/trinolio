"""The schema pinning and the merge, which are what keep one sync appendable to the last."""

from datetime import date, datetime, timedelta
from typing import Any

import polars as pl

from trinolio import store


def training(**overrides: object) -> dict[str, Any]:
    """A workout as Nolio sends it, with only the keys every response actually carries."""
    return {"nolio_id": 1, "date_start": "2026-01-02", "hour_start": "07:30:00"} | overrides


def test_absent_keys_become_null_rather_than_failing() -> None:
    """A field Nolio adds or drops must not break a sync, which is why the schema is pinned."""
    frame = store.trainings_frame([training()])

    assert frame.height == 1
    assert frame["load_coggan"].item() is None
    assert frame["start_city"].item() is None
    assert set(store.TRAININGS_RAW) <= set(frame.columns)


def test_int_and_float_for_one_field_share_a_dtype() -> None:
    """Nolio types the same field either way per workout, so inference would make two
    batches unappendable. This is the reason `TRAININGS_RAW` exists at all."""
    frame = store.trainings_frame(
        [training(nolio_id=1, elevation_gain=100), training(nolio_id=2, elevation_gain=100.5)]
    )

    assert frame.schema["elevation_gain"] == pl.Float64
    assert frame["elevation_gain"].to_list() == [100.0, 100.5]


def test_zones_survive_as_json_and_default_to_empty() -> None:
    frame = store.trainings_frame(
        [
            training(nolio_id=1, zones={"heartrate": [{"min": 0, "max": 131, "duration": 60}]}),
            training(nolio_id=2),
        ]
    )

    assert '"heartrate"' in frame.filter(pl.col("nolio_id") == 1)["zones"].item()
    assert frame.filter(pl.col("nolio_id") == 2)["zones"].item() == "{}"


def test_zones_serialize_the_same_whatever_order_the_channels_arrive_in() -> None:
    heartrate = [{"min": 0, "max": 131, "duration": 60}]
    watts = [{"min": 0, "max": 99, "duration": 60}]
    frame = store.trainings_frame(
        [
            training(nolio_id=1, zones={"heartrate": heartrate, "watts": watts}),
            training(nolio_id=2, zones={"watts": watts, "heartrate": heartrate}),
        ]
    )

    assert frame["zones"].n_unique() == 1


def test_start_local_needs_both_halves_of_the_wall_clock() -> None:
    """Date and hour are stored separately and are local time, not UTC. A workout Nolio
    gave no hour for gets a null rather than a midnight that was never recorded."""
    frame = store.trainings_frame(
        [
            training(nolio_id=1, date_start="2026-01-02", hour_start="07:30:00"),
            training(nolio_id=2, hour_start=None),
        ]
    )

    assert frame.filter(pl.col("nolio_id") == 1)["start_local"].item() == datetime(
        2026, 1, 2, 7, 30
    )
    assert frame.filter(pl.col("nolio_id") == 2)["start_local"].item() is None
    assert frame["date_start"].dtype == pl.Date


def test_date_end_parses_as_null_instead_of_raising() -> None:
    """It is empty on every workout Nolio has served, so the format can only be given."""
    frame = store.trainings_frame([training(date_end="")])

    assert frame["date_end"].item() is None


def test_wellness_collapses_a_day_to_one_value_keeping_the_higher() -> None:
    """Garmin's export files overlap on their boundary dates, so the same day arrives
    twice. Where the two disagree, which in practice is only ever vo2max, the higher wins."""
    frame = store.wellness_frame(
        [
            {"date": "2026-01-02", "type": "vo2max", "value": 54.0, "unit": None, "source": "a"},
            {"date": "2026-01-02", "type": "vo2max", "value": 55.0, "unit": None, "source": "a"},
            {"date": "2026-01-02", "type": "hrrest", "value": 42.0, "unit": None, "source": "a"},
        ]
    )

    assert frame.height == 2
    assert frame.filter(pl.col("type") == "vo2max")["value"].item() == 55.0


def test_newest_is_none_before_the_first_sync() -> None:
    assert store.newest(store.TRAININGS, "date_start") is None

    store.write(store.TRAININGS, store.trainings_frame([training(date_start="2026-01-02")]))

    assert store.newest(store.TRAININGS, "date_start") == date(2026, 1, 2)


def test_upsert_replaces_the_refetched_window_and_keeps_the_rest() -> None:
    """The trailing window is refetched every sync because RPE and load get filled in
    afterwards, so a re-sent workout has to overwrite rather than duplicate."""
    store.write(
        store.TRAININGS,
        store.trainings_frame(
            [training(nolio_id=1, date_start="2026-01-01"), training(nolio_id=2, rpe=0)]
        ),
    )

    fresh = store.trainings_frame([training(nolio_id=2, rpe=7), training(nolio_id=3)])
    result = store.upsert(store.TRAININGS, fresh, key="nolio_id", sort="date_start")

    stored = pl.read_parquet(store.TRAININGS)
    assert (result.added, result.changed, result.unchanged) == (1, 1, 0)
    assert result.changed_columns == ["rpe"]
    assert stored.height == 3
    assert stored.filter(pl.col("nolio_id") == 2)["rpe"].item() == 7
    assert stored["date_start"].is_sorted()


def test_upsert_counts_an_identical_refetch_as_unchanged_and_leaves_the_file_alone() -> None:
    workouts = store.trainings_frame([training(nolio_id=1), training(nolio_id=2, rpe=None)])
    store.write(store.TRAININGS, workouts)
    written = store.TRAININGS.stat().st_mtime_ns

    result = store.upsert(store.TRAININGS, workouts, key="nolio_id", sort="date_start")

    assert (result.added, result.changed, result.unchanged) == (0, 0, 2)
    assert store.TRAININGS.stat().st_mtime_ns == written


def test_upsert_drops_mirrored_rows_the_fetch_no_longer_returns() -> None:
    """A workout inside the refetched window that Nolio stopped returning was deleted there.
    Rows before the window were not asked for, so they stay."""
    store.write(
        store.TRAININGS,
        store.trainings_frame(
            [
                training(nolio_id=1, date_start="2026-01-01"),
                training(nolio_id=2, date_start="2026-02-01"),
                training(nolio_id=3, date_start="2026-02-02"),
            ]
        ),
    )

    fresh = store.trainings_frame([training(nolio_id=3, date_start="2026-02-02")])
    result = store.upsert(
        store.TRAININGS,
        fresh,
        key="nolio_id",
        sort="date_start",
        mirrored=pl.col("date_start") >= date(2026, 1, 15),
    )

    assert result.removed["nolio_id"].to_list() == [2]
    assert pl.read_parquet(store.TRAININGS)["nolio_id"].to_list() == [1, 3]


def test_upsert_keeps_unfetched_rows_when_nothing_is_mirrored() -> None:
    store.write(store.TRAININGS, store.trainings_frame([training(nolio_id=1)]))

    result = store.upsert(
        store.TRAININGS, store.trainings_frame([training(nolio_id=2)]), "nolio_id", "date_start"
    )

    assert result.removed.height == 0
    assert pl.read_parquet(store.TRAININGS).height == 2


def test_write_leaves_no_temporary_behind() -> None:
    store.write(store.TRAININGS, store.trainings_frame([training()]))

    assert store.TRAININGS.exists()
    assert not store.TRAININGS.with_suffix(".tmp").exists()


def workouts_at(sport_id: int, *starts: str) -> list[dict[str, Any]]:
    return [
        training(nolio_id=index, date_start=start[:10], hour_start=start[11:], sport_id=sport_id)
        for index, start in enumerate(starts, start=1)
    ]


def test_duplicate_candidates_flags_two_uploads_of_one_session() -> None:
    """The Zwift failure mode: one ride recorded twice, minutes apart, which Nolio never
    matched as the same session and so never deduplicated by source priority."""
    store.write(
        store.TRAININGS,
        store.trainings_frame(workouts_at(18, "2026-01-02 06:36:17", "2026-01-02 06:37:21")),
    )

    pairs = store.duplicate_candidates(store.TRAININGS)

    assert pairs.height == 1
    assert pairs["previous_id"].item() == 1
    assert pairs["nolio_id"].item() == 2


def test_duplicate_candidates_ignores_a_gap_wider_than_the_window() -> None:
    store.write(
        store.TRAININGS,
        store.trainings_frame(workouts_at(2, "2026-01-02 06:00:00", "2026-01-02 06:20:00")),
    )

    assert store.duplicate_candidates(store.TRAININGS).height == 0


def test_duplicate_candidates_never_pairs_across_sports() -> None:
    """A brick starts the run within seconds of ending the ride, and that is one session
    recorded correctly as two workouts, not a double upload."""
    store.write(
        store.TRAININGS,
        store.trainings_frame(
            [
                *workouts_at(14, "2026-01-02 06:00:00"),
                training(nolio_id=9, date_start="2026-01-02", hour_start="06:00:30", sport_id=2),
            ]
        ),
    )

    assert store.duplicate_candidates(store.TRAININGS).height == 0


def test_duplicate_candidates_skips_workouts_with_no_start_hour() -> None:
    """Two nulls are not two workouts a second apart, which is what a naive sort would
    make of them."""
    store.write(
        store.TRAININGS,
        store.trainings_frame(
            [
                training(nolio_id=1, sport_id=2, hour_start=None),
                training(nolio_id=2, sport_id=2, hour_start=None),
            ]
        ),
    )

    assert store.duplicate_candidates(store.TRAININGS).height == 0


def test_duplicate_candidates_reports_a_run_of_three_as_overlapping_pairs() -> None:
    store.write(
        store.TRAININGS,
        store.trainings_frame(
            workouts_at(18, "2026-01-02 06:00:00", "2026-01-02 06:01:00", "2026-01-02 06:02:00")
        ),
    )

    assert store.duplicate_candidates(store.TRAININGS).height == 2


def test_duplicate_window_is_honoured_when_given() -> None:
    store.write(
        store.TRAININGS,
        store.trainings_frame(workouts_at(2, "2026-01-02 06:00:00", "2026-01-02 06:20:00")),
    )

    assert store.duplicate_candidates(store.TRAININGS, timedelta(minutes=30)).height == 1


def test_duplicate_candidates_ignores_two_sessions_sharing_a_rewritten_start() -> None:
    """Nolio rewrites the start time on a hand upload, hour included, so the Zwift rides
    imported that way all claim the same minute. Duration and distance come from the file."""
    store.write(
        store.TRAININGS,
        store.trainings_frame(
            [
                training(nolio_id=1, sport_id=18, duration=4568, distance=37.995),
                training(nolio_id=2, sport_id=18, duration=2490, distance=18.87),
            ]
        ),
    )

    assert store.duplicate_candidates(store.TRAININGS).height == 0


def test_duplicate_candidates_flags_one_ride_recorded_by_two_devices() -> None:
    """The real shape: a race bike leg held by the watch and by the Edge, agreeing on what
    the ride was to within a rounding error."""
    store.write(
        store.TRAININGS,
        store.trainings_frame(
            [
                training(nolio_id=1, sport_id=14, duration=8935, distance=79.421),
                training(nolio_id=2, sport_id=14, duration=8886, distance=78.903),
            ]
        ),
    )

    assert store.duplicate_candidates(store.TRAININGS).height == 1


def test_duplicate_candidates_flags_a_sport_that_records_no_distance() -> None:
    """Climbing logs 0 km, so a zero distance is agreement and not a missing comparison."""
    store.write(
        store.TRAININGS,
        store.trainings_frame(
            [
                training(nolio_id=1, sport_id=31, duration=3600, distance=0.0),
                training(nolio_id=2, sport_id=31, duration=3605, distance=0.0),
            ]
        ),
    )

    assert store.duplicate_candidates(store.TRAININGS).height == 1
