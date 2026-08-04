"""The encodings `data.py` undoes. Every one of these is a silently wrong number if it regresses."""

from typing import Any

import polars as pl
import pytest

from trinolio import data, store


def training(**overrides: object) -> dict[str, Any]:
    return {"nolio_id": 1, "date_start": "2026-01-02", "hour_start": "07:30:00"} | overrides


def store_trainings(records: list[dict[str, Any]]) -> None:
    store.write(store.TRAININGS, store.trainings_frame(records))


def store_wellness(live: list[dict[str, Any]], archive: list[dict[str, Any]]) -> None:
    store.write(store.METRICS, store.metrics_frame(live))
    store.write(store.WELLNESS, store.wellness_frame(archive))


def measurement(**overrides: object) -> dict[str, Any]:
    row = {"date": "2026-01-02", "type": "sleep", "value": 1.0, "unit": None, "source": "Garmin"}
    return row | overrides


def reading(**overrides: object) -> dict[str, Any]:
    return {"id": 1} | measurement(**overrides) | {"hour": "08:00:00"}


def test_zero_is_read_as_not_recorded() -> None:
    """Nolio writes 0, never null, for what it did not measure, and on these columns that is
    the common case. Raw, `avg_watt.mean()` read 3.4 W against a true 112 W."""
    store_trainings([training(rpe=0, avg_watt=0, load_coggan=0, feeling=0)])

    row = data.trainings().row(0, named=True)
    assert row["rpe"] is None
    assert row["avg_watt"] is None
    assert row["load_coggan"] is None
    assert row["feeling"] is None


def test_zero_stays_zero_where_it_is_a_measurement() -> None:
    """A treadmill run really does climb 0 m and a gym session really does cover 0 km, so
    nulling these would invent missing data rather than uncover it."""
    store_trainings([training(distance=0, elevation_gain=0, duration=0)])

    row = data.trainings().row(0, named=True)
    assert row["distance"] == 0
    assert row["elevation_gain"] == 0
    assert row["duration"] == 0


def test_sports_are_read_from_the_stable_id_not_the_localized_name() -> None:
    store_trainings([training(sport_id=2, sport="Course à pied")])

    row = data.trainings().row(0, named=True)
    assert row["sport_en"] == "Running"
    assert row["discipline"] == "run"


def test_an_unknown_sport_id_surfaces_as_null_in_both_columns() -> None:
    """A closed `other` would silently understate swim volume the day Nolio adds an
    open-water id. Null gets noticed in a `group_by`; a wrong bucket does not."""
    store_trainings([training(sport_id=999)])

    row = data.trainings().row(0, named=True)
    assert row["sport_en"] is None
    assert row["discipline"] is None


def test_every_mapped_sport_lands_in_a_discipline() -> None:
    """The two tables are hand-typed and guarded against each other at import; this checks
    the guard is actually reachable from the mapping the frame uses."""
    assert set(data.DISCIPLINE_BY_ID) == set(data.SPORTS)
    assert set(data.DISCIPLINE_BY_ID.values()) == {"run", "bike", "swim", "other"}


def test_profile_weight_is_renamed_out_of_the_way() -> None:
    """Nolio stamps the athlete's profile weight onto every workout, so averaging it reads
    as a weight trend that was never measured. The measured series is wellness `weight`."""
    store_trainings([training(weight=52.0)])

    frame = data.trainings()
    assert frame["weight_profile"].item() == 52.0
    assert "weight" not in frame.columns


def test_the_connector_wins_where_it_overlaps_the_archive() -> None:
    """They overlap by the days around the connector being linked. The connector is the
    live feed and the archive is a frozen export, so the archive row is dropped."""
    store_wellness(
        live=[reading(date="2026-01-02", value=200.0)],
        archive=[measurement(date="2026-01-02", value=100.0, source="garmin-archive")],
    )

    frame = data.wellness()
    assert frame.height == 1
    assert frame["value"].item() == 200.0
    assert frame["source"].item() == "Garmin"


def test_nolio_serving_one_day_under_two_ids_collapses() -> None:
    store_wellness(
        live=[reading(id=1, value=100.0), reading(id=2, value=100.0)],
        archive=[],
    )

    assert data.wellness().height == 1


def test_two_different_readings_on_a_day_are_both_kept() -> None:
    """A second measurement is data, not a duplicate, and only the values can tell them
    apart since Nolio issues an id either way."""
    store_wellness(
        live=[reading(id=1, value=100.0), reading(id=2, value=180.0)],
        archive=[],
    )

    assert data.wellness().height == 2


def test_sleep_is_flagged_complete_only_on_nights_that_report_rem() -> None:
    """Garmin reported no REM before 2021 and still misses scattered nights since. On those
    nights `sleep` is deep + light exactly, so a trend crossing them steps for no reason."""
    store_wellness(
        live=[],
        archive=[
            measurement(date="2026-01-02", type="sleep", value=7.0, source="garmin-archive"),
            measurement(
                date="2026-01-02", type="paradoxicalsleep", value=1.0, source="garmin-archive"
            ),
            measurement(date="2026-01-03", type="sleep", value=7.0, source="garmin-archive"),
            measurement(date="2026-01-03", type="hrrest", value=42.0, source="garmin-archive"),
        ],
    )

    nights = data.wellness().filter(pl.col("type") == "sleep").sort("date")
    assert nights["sleep_complete"].to_list() == [True, False]


def test_sleep_complete_is_null_on_everything_that_is_not_sleep() -> None:
    store_wellness(
        live=[], archive=[measurement(type="hrrest", value=42.0, source="garmin-archive")]
    )

    assert data.wellness()["sleep_complete"].item() is None


def test_metric_names_the_types_that_are_opaque_without_it() -> None:
    """`type` is kept because it is what round-trips to `/update/metric/`."""
    store_wellness(
        live=[],
        archive=[
            measurement(type=name, source="garmin-archive")
            for name in ("paradoxicalsleep", "caloriesaurepos", "scoresommeil", "hrrest")
        ],
    )

    named = dict(data.wellness().select("type", "metric").iter_rows())
    assert named == {
        "paradoxicalsleep": "rem_sleep",
        "caloriesaurepos": "resting_calories",
        "scoresommeil": "sleep_score",
        "hrrest": "resting_hr",
    }


def test_an_unrecognised_metric_type_is_null_not_a_guess() -> None:
    store_wellness(live=[], archive=[measurement(type="whatevernolioaddsnext", source="a")])

    assert data.wellness()["metric"].item() is None


HEARTRATE = [
    {"min": 0, "max": 131, "duration": 600},
    {"min": 131, "max": 164, "duration": 300},
    {"min": 164, "max": 180, "duration": 60},
]
WATTS = [{"min": 0, "max": 99, "duration": 400}, {"min": 100, "max": 135, "duration": 200}]


def test_zones_unpack_whatever_channels_a_workout_happens_to_have() -> None:
    """A power ride with no heart rate and a swim with no watts are both normal, so a fixed
    struct would drop whichever channel it was not told about."""
    store_trainings(
        [
            training(nolio_id=1, zones={"heartrate": HEARTRATE}),
            training(nolio_id=2, zones={"heartrate": HEARTRATE, "watts": WATTS}),
        ]
    )

    frame = data.zones()
    assert frame.height == len(HEARTRATE) * 2 + len(WATTS)
    assert set(frame["channel"].unique()) == {"heartrate", "watts"}


def test_zone_ordinals_are_derived_per_channel_and_start_at_one() -> None:
    """Heart rate buckets meet exactly while watts leave a unit between them, so the
    numbering cannot assume either shape."""
    store_trainings([training(zones={"heartrate": HEARTRATE, "watts": WATTS})])

    frame = data.zones()
    by_channel = {
        channel: group.sort("min")["zone"].to_list()
        for (channel,), group in frame.group_by("channel")
    }
    assert by_channel == {"heartrate": [1, 2, 3], "watts": [1, 2]}


def test_the_same_bucket_gets_the_same_ordinal_in_every_workout() -> None:
    store_trainings(
        [training(nolio_id=1, zones={"heartrate": HEARTRATE}), training(nolio_id=2, zones={})]
    )

    frame = data.zones()
    assert frame.filter(pl.col("min") == 131)["zone"].unique().to_list() == [2]


def test_a_ladder_redefined_mid_history_raises_instead_of_renumbering() -> None:
    """A global rank over two overlapping ladders would silently renumber every workout on
    both sides of the change. An ordinal nobody can trust is worse than no ordinal."""
    store_trainings(
        [
            training(nolio_id=1, zones={"heartrate": [{"min": 0, "max": 131, "duration": 60}]}),
            training(nolio_id=2, zones={"heartrate": [{"min": 0, "max": 120, "duration": 60}]}),
        ]
    )

    with pytest.raises(RuntimeError, match="overlap"):
        data.zones()


def test_a_ladder_that_only_grows_a_bucket_is_not_an_overlap() -> None:
    """Adding a sixth zone above the fifth is a new bucket, not a redefinition, and has to
    keep working."""
    store_trainings(
        [
            training(nolio_id=1, zones={"heartrate": HEARTRATE}),
            training(
                nolio_id=2,
                zones={"heartrate": [*HEARTRATE, {"min": 180, "max": 196, "duration": 30}]},
            ),
        ]
    )

    assert data.zones()["zone"].max() == 4
