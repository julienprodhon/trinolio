"""The Garmin export read into Nolio's vocabulary.

This is the only source for wellness before the connector was linked, so a field dropped
here is a year of sleep that no longer exists anywhere queryable.
"""

import json
from collections.abc import Mapping, Sequence
from typing import Any

from trinolio import wellness


def write_export(folder: str, records: list[dict[str, Any]], name: str = "part.json") -> None:
    directory = wellness.ARCHIVE / folder
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps(records))


def by_type(rows: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    return {row["type"]: row["value"] for row in rows}


def test_daily_summary_fields_are_renamed_to_nolios_vocabulary() -> None:
    write_export(
        "daily-summary",
        [
            {
                "calendarDate": "2026-01-02",
                "restingHeartRate": 42,
                "totalKilocalories": 2400,
                "activeKilocalories": 900,
                "bmrKilocalories": 1500,
                "totalSteps": 8000,
            }
        ],
    )

    values = by_type(wellness.read_archive())
    assert values == {
        "hrrest": 42,
        "calories": 2400,
        "caloriesactive": 900,
        "caloriesaurepos": 1500,
        "numberofsteps": 8000,
    }


def test_a_field_garmin_left_empty_is_not_recorded_as_zero() -> None:
    """The same trap as Nolio's zeros, one layer earlier: a 0 resting HR would be averaged."""
    write_export("daily-summary", [{"calendarDate": "2026-01-02", "restingHeartRate": 0}])

    assert wellness.read_archive() == []


def test_sleep_totals_the_stages_the_night_actually_has() -> None:
    """Nolio's `sleep` is time asleep, so awake is carried separately and not summed in."""
    write_export(
        "sleep",
        [
            {
                "calendarDate": "2026-01-02",
                "deepSleepSeconds": 3600,
                "lightSleepSeconds": 14400,
                "remSleepSeconds": 5400,
                "awakeSleepSeconds": 900,
            }
        ],
    )

    values = by_type(wellness.read_archive())
    assert values["sleep"] == 3600 + 14400 + 5400
    assert values["awaketime"] == 900
    assert values["paradoxicalsleep"] == 5400


def test_a_night_from_before_rem_existed_sums_the_two_stages_it_has() -> None:
    """REM is absent from the archive before 2021-10-07. Those nights are still real sleep,
    they just cannot be compared to a later one; `data.sleep_complete` is what marks them."""
    write_export(
        "sleep",
        [{"calendarDate": "2020-01-02", "deepSleepSeconds": 3600, "lightSleepSeconds": 14400}],
    )

    values = by_type(wellness.read_archive())
    assert values["sleep"] == 3600 + 14400
    assert "paradoxicalsleep" not in values


def test_a_stage_of_zero_is_kept_while_a_missing_one_is_not() -> None:
    """Zero seconds of deep sleep is a measurement; an absent key is Garmin not reporting."""
    write_export(
        "sleep",
        [{"calendarDate": "2026-01-02", "deepSleepSeconds": 0, "lightSleepSeconds": 14400}],
    )

    values = by_type(wellness.read_archive())
    assert values["deepsleep"] == 0
    assert "lightsleep" in values


def test_the_sleep_score_is_dug_out_of_its_nesting() -> None:
    write_export(
        "sleep",
        [
            {
                "calendarDate": "2026-01-02",
                "lightSleepSeconds": 14400,
                "sleepScores": {"overallScore": 78},
            }
        ],
    )

    assert by_type(wellness.read_archive())["scoresommeil"] == 78


def test_a_night_with_no_scores_block_does_not_crash() -> None:
    write_export("sleep", [{"calendarDate": "2026-01-02", "lightSleepSeconds": 14400}])

    assert "scoresommeil" not in by_type(wellness.read_archive())


def test_weight_is_converted_out_of_grams() -> None:
    write_export(
        "biometrics",
        [{"metaData": {"calendarDate": "2026-01-02T18:05:26.148"}, "weight": {"weight": 52000.0}}],
        name="1_userBioMetrics.json",
    )

    rows = wellness.read_archive()
    assert rows[0]["value"] == 52.0
    assert rows[0]["date"] == "2026-01-02"


def test_only_the_biometrics_file_holding_weight_is_read() -> None:
    """The folder also holds zone settings and fitness age, which are not wellness rows."""
    write_export("biometrics", [{"zone1Floor": 98}], name="1_heartRateZones.json")

    assert wellness.read_archive() == []


def test_vo2max_is_skipped_when_garmin_has_no_estimate() -> None:
    write_export(
        "vo2max",
        [
            {"calendarDate": "2026-01-02", "vo2MaxValue": 54.0},
            {"calendarDate": "2026-01-03", "vo2MaxValue": None},
        ],
    )

    rows = wellness.read_archive()
    assert len(rows) == 1
    assert rows[0]["value"] == 54.0


def test_every_row_carries_its_unit_and_its_provenance() -> None:
    """`source` is what keeps archive rows distinguishable from the connector's once the
    two tables are unioned, since the archive has no Nolio id to tell them apart by."""
    write_export("daily-summary", [{"calendarDate": "2026-01-02", "restingHeartRate": 42}])

    row = wellness.read_archive()[0]
    assert row["unit"] == "bpm"
    assert row["source"] == "garmin-archive"


def test_every_emitted_type_has_a_unit_entry() -> None:
    """`read_archive` indexes `UNITS` directly, so a type added to a parser without one
    there raises a KeyError mid-run rather than writing a null."""
    emitted = {
        *wellness.DAILY_SUMMARY_FIELDS.values(),
        *wellness.SLEEP_STAGE_FIELDS.values(),
        "sleep",
        "scoresommeil",
        "vo2max",
        "weight",
    }

    assert emitted <= set(wellness.UNITS)


def test_overlapping_export_files_are_left_for_the_frame_to_collapse() -> None:
    """Garmin splits each folder into ~100-day files that repeat their boundary date.
    `read_archive` reports what it read; `store.wellness_frame` is what deduplicates."""
    write_export("vo2max", [{"calendarDate": "2026-01-02", "vo2MaxValue": 54.0}], name="a.json")
    write_export("vo2max", [{"calendarDate": "2026-01-02", "vo2MaxValue": 55.0}], name="b.json")

    assert len(wellness.read_archive()) == 2
